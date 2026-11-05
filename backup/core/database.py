"""SQLite 存储层。所有 SQL 收敛在这里，其余模块只与 Database 打交道。"""

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .models import BackupRecord, BackupStatus, GroupInfo, GroupMember, Message, MessageType

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  group_id INTEGER NOT NULL,
  sender_id INTEGER NOT NULL,
  sender_name TEXT,
  timestamp DATETIME NOT NULL,
  message_type TEXT NOT NULL,
  content TEXT,
  raw_content TEXT,
  reply_to TEXT,
  is_forward BOOLEAN DEFAULT 0,
  media_paths TEXT,
  forward_chain TEXT,
  backup_id TEXT NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(message_id, group_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_group ON messages(group_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_msg_sender ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_msg_type ON messages(message_type);
CREATE INDEX IF NOT EXISTS idx_msg_backup ON messages(backup_id);

CREATE TABLE IF NOT EXISTS media_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT NOT NULL,
  group_id INTEGER,
  original_url TEXT,
  local_path TEXT,
  filename TEXT,
  file_size INTEGER,
  mime_type TEXT,
  sha256 TEXT,
  downloaded_at DATETIME,
  UNIQUE(message_id, original_url)
);
CREATE INDEX IF NOT EXISTS idx_media_msg ON media_files(message_id);

CREATE TABLE IF NOT EXISTS backup_records (
  backup_id TEXT PRIMARY KEY,
  group_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at DATETIME,
  finished_at DATETIME,
  message_count INTEGER DEFAULT 0,
  new_count INTEGER DEFAULT 0,
  package_count INTEGER DEFAULT 0,
  media_count INTEGER DEFAULT 0,
  total_size INTEGER DEFAULT 0,
  is_incremental BOOLEAN DEFAULT 0,
  base_backup_id TEXT,
  checksum TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_rec_group ON backup_records(group_id, started_at);

CREATE TABLE IF NOT EXISTS group_info (
  group_id INTEGER PRIMARY KEY,
  group_name TEXT,
  owner_id INTEGER,
  created_at DATETIME,
  member_count INTEGER,
  max_members INTEGER,
  group_level INTEGER,
  last_synced_at DATETIME
);

CREATE TABLE IF NOT EXISTS group_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  nickname TEXT,
  card TEXT,
  role TEXT,
  join_time DATETIME,
  last_active DATETIME,
  synced_at DATETIME,
  UNIQUE(group_id, user_id)
);

-- 一次备份到底覆盖了哪些消息。messages.backup_id 只能记「最后归档它的那次备份」，
-- 全量重跑会把归属全部接手，于是「最早那份备份里有什么」就没地方问了——而这个
-- 问题恰恰是恢复老备份时要回答的。
CREATE TABLE IF NOT EXISTS backup_sources (
  backup_id TEXT NOT NULL,
  group_id INTEGER NOT NULL,
  message_id TEXT NOT NULL,
  PRIMARY KEY (backup_id, group_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_src_msg ON backup_sources(group_id, message_id);

-- 每份备份自己的群信息/成员快照。group_info / group_members 是「当前状态」，
-- 随每次备份覆盖；换台机器恢复时，库里根本没有对应的群那一行，导出包也就带
-- 不出群名和成员，HTML 里一片空白。
CREATE TABLE IF NOT EXISTS backup_bundle (
  backup_id TEXT NOT NULL,
  group_id INTEGER NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (backup_id, group_id)
);

-- 导出时的「可信快照」证据：群名/群信息/成员表的指纹。
-- 备份包里的每一个字段都是包作者写的，谁都能改完再把这个包自己的指纹重算一遍。
-- 唯一改不了的是导出当时写在本机库里的这一行——恢复时拿它比对，篡改才拦得住。
CREATE TABLE IF NOT EXISTS export_evidence (
  backup_id TEXT NOT NULL,
  group_id INTEGER NOT NULL,
  snapshot TEXT NOT NULL,
  evidence TEXT NOT NULL,
  exported_at DATETIME,
  PRIMARY KEY (backup_id, group_id)
);

CREATE TABLE IF NOT EXISTS backup_meta (
  group_id INTEGER PRIMARY KEY,
  last_timestamp DATETIME,
  last_message_id TEXT
);

CREATE TABLE IF NOT EXISTS download_tokens (
  token TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  expires REAL NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  -- 首次兑换的时刻（NULL = 还没人领过）。`Range` 续传靠它区分
  -- 「同一次下载的后续片段」和「链接被转发后第二个人来领」
  opened_at REAL
);

CREATE TABLE IF NOT EXISTS schedules (
  group_id INTEGER PRIMARY KEY,
  cron TEXT NOT NULL,
  enabled BOOLEAN DEFAULT 1,
  updated_at DATETIME
);

-- 群公告与群文件列表。scope.group_announcements / scope.group_files 两个开关
-- 一直写在配置里，但备份时没人读——开关是空头支票。这两张表把它们接上：
-- 都是「当前状态」表，每次备份整群覆盖。
CREATE TABLE IF NOT EXISTS group_announcements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL,
  content TEXT,
  sender_id INTEGER,
  sender_name TEXT,
  published_at DATETIME,
  raw_content TEXT,
  synced_at DATETIME,
  UNIQUE(group_id, content, published_at)
);
CREATE INDEX IF NOT EXISTS idx_ann_group ON group_announcements(group_id, published_at);

CREATE TABLE IF NOT EXISTS group_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL,
  file_id TEXT NOT NULL,
  name TEXT,
  size INTEGER,
  busid INTEGER,
  upload_time DATETIME,
  uploader_id INTEGER,
  uploader_name TEXT,
  raw_content TEXT,
  synced_at DATETIME,
  UNIQUE(group_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_gfile_group ON group_files(group_id, upload_time);
"""

# 实时归档写入的 messages.backup_id。它不是一次备份，只是「已经在库里了」的标记。
REALTIME_BACKUP_ID = "realtime"

# 老库升级：messages 的唯一键从 (message_id, group_id, backup_id) 收紧到
# (message_id, group_id)。不做这一步，升级前建的库会继续堆重复消息。
_MESSAGES_UNIQUE = ("message_id", "group_id")


def _migrate_messages_unique(conn) -> None:
    """把旧库的 messages 表按新唯一键重建，顺带做一次去重。"""
    indexes = conn.execute("PRAGMA index_list('messages')").fetchall()
    for idx in indexes:
        if not idx["unique"]:
            continue
        cols = tuple(
            r["name"]
            for r in conn.execute(f"PRAGMA index_info('{idx['name']}')").fetchall()
        )
        if cols == _MESSAGES_UNIQUE:
            return  # 已经是新结构
        if set(cols) == {*_MESSAGES_UNIQUE, "backup_id"}:
            break
    else:
        return  # 结构不认识，不擅自动手

    conn.executescript(
        """
        BEGIN;
        CREATE TABLE messages_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id TEXT NOT NULL,
          group_id INTEGER NOT NULL,
          sender_id INTEGER NOT NULL,
          sender_name TEXT,
          timestamp DATETIME NOT NULL,
          message_type TEXT NOT NULL,
          content TEXT,
          raw_content TEXT,
          reply_to TEXT,
          is_forward BOOLEAN DEFAULT 0,
          media_paths TEXT,
          forward_chain TEXT,
          backup_id TEXT NOT NULL,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(message_id, group_id)
        );
        INSERT OR REPLACE INTO messages_new
          (message_id, group_id, sender_id, sender_name, timestamp, message_type,
           content, raw_content, reply_to, is_forward, media_paths, forward_chain,
           backup_id, created_at)
        SELECT message_id, group_id, sender_id, sender_name, timestamp, message_type,
               content, raw_content, reply_to, is_forward, media_paths, forward_chain,
               backup_id, created_at
        FROM messages ORDER BY id;
        DROP TABLE messages;
        ALTER TABLE messages_new RENAME TO messages;
        CREATE INDEX IF NOT EXISTS idx_msg_group ON messages(group_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_msg_sender ON messages(sender_id);
        CREATE INDEX IF NOT EXISTS idx_msg_type ON messages(message_type);
        CREATE INDEX IF NOT EXISTS idx_msg_backup ON messages(backup_id);
        COMMIT;
        """
    )


def _migrate_record_columns(conn) -> None:
    """老库的 backup_records 没有 new_count 列。

    `CREATE TABLE IF NOT EXISTS` 只管「表不存在时建表」，已有的表它不会补列。
    升级到这一版后第一次读记录就会撞 `no such column`，整个插件起不来。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info('backup_records')")}
    for name, ddl in (
        ("new_count", "ALTER TABLE backup_records ADD COLUMN new_count INTEGER DEFAULT 0"),
        ("package_count", "ALTER TABLE backup_records ADD COLUMN package_count INTEGER DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(ddl)
    conn.commit()


def _migrate_token_columns(conn) -> None:
    """老库的 download_tokens 没有 opened_at 列。

    这张表的 token 有效期 24 小时、重启后仍然有效，所以升级那一刻库里一定还有
    活着的 token。新代码按 `opened_at` 判断「领过没有」，列不存在就是
    `no such column` —— 每次下载都 500，而备份本身看不出问题。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info('download_tokens')")}
    if cols and "opened_at" not in cols:
        conn.execute("ALTER TABLE download_tokens ADD COLUMN opened_at REAL")
        conn.commit()


# 老库升级：media_files 加 (message_id, original_url) 唯一键，顺带去掉重复行。
_MEDIA_UNIQUE = ("message_id", "original_url")


def _migrate_media_unique(conn) -> None:
    for idx in conn.execute("PRAGMA index_list('media_files')").fetchall():
        if not idx["unique"]:
            continue
        cols = tuple(
            r["name"]
            for r in conn.execute(f"PRAGMA index_info('{idx['name']}')").fetchall()
        )
        if cols == _MEDIA_UNIQUE:
            return
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE media_files_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          message_id TEXT NOT NULL,
          group_id INTEGER,
          original_url TEXT,
          local_path TEXT,
          filename TEXT,
          file_size INTEGER,
          mime_type TEXT,
          sha256 TEXT,
          downloaded_at DATETIME,
          UNIQUE(message_id, original_url)
        );
        INSERT OR REPLACE INTO media_files_new
          (message_id, group_id, original_url, local_path, filename, file_size,
           mime_type, sha256, downloaded_at)
        SELECT message_id, group_id, original_url, local_path, filename, file_size,
               mime_type, sha256, downloaded_at
        FROM media_files ORDER BY id;
        DROP TABLE media_files;
        ALTER TABLE media_files_new RENAME TO media_files;
        CREATE INDEX IF NOT EXISTS idx_media_msg ON media_files(message_id);
        COMMIT;
        """
    )


_DT_FMT = "%Y-%m-%d %H:%M:%S.%f"


def _group_info_to_dict(info) -> dict:
    return {
        "group_id": info.group_id,
        "group_name": info.group_name,
        "owner_id": info.owner_id,
        "created_at": to_db(info.created_at),
        "member_count": info.member_count,
        "max_members": info.max_members,
        "group_level": info.group_level,
    }


def _member_to_dict(member) -> dict:
    return {
        "user_id": member.user_id,
        "group_id": member.group_id,
        "nickname": member.nickname,
        "card": member.card,
        "role": member.role,
        "join_time": to_db(member.join_time),
        "last_active": to_db(member.last_active),
    }


def group_info_from_dict(raw: dict, fallback_group_id: int = 0):
    """快照 dict → GroupInfo。字段缺失时按空值兜底。"""
    from .models import GroupInfo

    if not raw:
        return None
    return GroupInfo(
        group_id=int(raw.get("group_id") or fallback_group_id),
        group_name=str(raw.get("group_name") or ""),
        owner_id=int(raw.get("owner_id") or 0),
        created_at=from_db(raw.get("created_at")),
        member_count=int(raw.get("member_count") or 0),
        max_members=int(raw.get("max_members") or 0),
        group_level=int(raw.get("group_level") or 0),
    )


def members_from_list(raw_members, fallback_group_id: int = 0) -> list:
    """快照 dict 列表 → GroupMember 列表。"""
    from .models import GroupMember

    out = []
    for raw in raw_members or []:
        try:
            out.append(
                GroupMember(
                    user_id=int(raw.get("user_id") or 0),
                    group_id=int(raw.get("group_id") or fallback_group_id),
                    nickname=str(raw.get("nickname") or ""),
                    card=str(raw.get("card") or ""),
                    role=str(raw.get("role") or "member"),
                    join_time=from_db(raw.get("join_time")),
                    last_active=from_db(raw.get("last_active")),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def _jsonable_row(row) -> dict:
    """快照条目 → 可 json 序列化的 dict。

    解析器给的公告/文件条目里，`published_at` / `upload_time` 是 `datetime`，
    直接塞进 `json.dumps` 会抛 `TypeError: Object of type datetime is not JSON
    serializable`。而 `save_bundle` 是在备份主流程里调的——**一条带发布时间的
    公告就能让整轮备份被判 failed，消息却已经全落库了**。用户看到「备份失败」，
    实际数据都在，最坏的是他会以为没备上而反复重跑。
    """
    out = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            out[key] = to_db(_naive_dt(value))
        else:
            out[key] = value
    return out


def to_db(dt: Optional[datetime | str]) -> Optional[str]:
    """datetime → 库里的时间字符串。

    已经是字符串的原样放过：从导出包恢复时，包里的时间是 JSON 里取出来的
    ISO 串，调用方不该为了「写回库」再把它解析一遍——多一层转换就多一处
    时区/精度丢信息的地方。None 与空串都当作「没有」。
    """
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    return dt.strftime(_DT_FMT)


def _naive_dt(value):
    """时间规整成「本地 naive」，好让 to_db 只有一种格式化路径。

    库里时间统一是 naive 本地时间；tz-aware 值直接落库会连时区一起丢掉，
    读回来凭空多几小时（增量水位因此判错「哪条更新」）。
    """
    if isinstance(value, datetime):
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
    return value


def from_db(value) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    for fmt in (_DT_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value)[:26], fmt)
        except ValueError:
            continue
    return None


class Database:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        _migrate_messages_unique(self._conn)
        _migrate_media_unique(self._conn)
        _migrate_record_columns(self._conn)
        _migrate_token_columns(self._conn)

    def close(self) -> None:
        self._conn.close()

    # ---------- 通用 ----------
    def execute(self, sql: str, params: Iterable = ()):
        cur = self._conn.execute(sql, tuple(params))
        self._conn.commit()
        return cur

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---------- 消息 ----------
    def save_message(self, msg: Message, backup_id: str) -> bool:
        """写入一条消息。返回是否「这条消息此前不在库里」。

        唯一键是 (message_id, group_id)：同一条消息被多次备份只留一行，
        backup_id 记最后一次归档它的那次备份。否则全量重跑、重试、恢复都会留下
        重复行，导出和聊天记录里就会出现同一条消息好几遍。
        """

        existed = self.query_one(
            "SELECT 1 FROM messages WHERE message_id = ? AND group_id = ? LIMIT 1",
            (msg.message_id, msg.group_id),
        )
        self.execute(
            """INSERT INTO messages
               (message_id, group_id, sender_id, sender_name, timestamp, message_type,
                content, raw_content, reply_to, is_forward, media_paths, forward_chain, backup_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(message_id, group_id) DO UPDATE SET
                 backup_id = excluded.backup_id,
                 -- 媒体路径取并集。实时归档（backup_id='realtime'）刻意不下载媒体，
                 -- 它写进来时 media_paths 是空的；直接覆盖会把上一次完整备份存下的
                 -- 图片/语音路径洗成 []，从此那条消息导出不带媒体、也下载不了——
                 -- 而实时归档的初衷恰恰是「群被炸时别丢东西」。反过来，同一份完整
                 -- 备份重跑时解析少了一两个附件，也不该把它们从库里抹掉。
                 media_paths = CASE
                   WHEN excluded.media_paths IN ('[]', '', 'null') THEN messages.media_paths
                   WHEN messages.media_paths IN ('[]', '', 'null') THEN excluded.media_paths
                   WHEN json_extract(messages.media_paths, '$') IS NULL
                     THEN excluded.media_paths
                   ELSE CASE
                     WHEN EXISTS (
                       SELECT 1 FROM json_each(messages.media_paths) old
                       WHERE NOT EXISTS (
                         SELECT 1 FROM json_each(excluded.media_paths) new
                         WHERE new.value = old.value
                       )
                     ) THEN (
                       SELECT json_group_array(value) FROM (
                         SELECT value FROM json_each(messages.media_paths)
                         UNION
                         SELECT value FROM json_each(excluded.media_paths)
                       )
                     )
                     ELSE excluded.media_paths
                   END
                 END,
                 -- 发送者名字同理：解析不出昵称时不要用空串盖掉已知的名字
                 sender_name = CASE
                   WHEN excluded.sender_name IN ('', 'null') THEN messages.sender_name
                   ELSE excluded.sender_name
                 END""",
            (
                msg.message_id,
                msg.group_id,
                msg.sender_id,
                msg.sender_name,
                to_db(msg.timestamp),
                msg.message_type.value,
                msg.content,
                msg.raw_content,
                msg.reply_to,
                1 if msg.is_forward else 0,
                json.dumps(msg.media_paths, ensure_ascii=False),
                json.dumps(msg.forward_chain, ensure_ascii=False),
                backup_id,
            ),
        )
        # upsert 的 rowcount 恒为 1，区分「新增」与「接管」只能靠写入前的探测
        return existed is None

    # ---------- 备份来源 ----------
    def add_sources(self, backup_id: str, messages: Iterable[Message]) -> None:
        """登记「这次备份覆盖了哪些消息」。与归属分开记：归属只有一个，来源可以多份。"""
        rows = [
            (backup_id, m.group_id, str(m.message_id))
            for m in messages
            if m is not None and m.message_id
        ]
        if not rows:
            return
        self._conn.executemany(
            """INSERT OR IGNORE INTO backup_sources (backup_id, group_id, message_id)
               VALUES (?,?,?)""",
            rows,
        )
        self._conn.commit()

    def get_source_messages(self, backup_id: str, group_id: Optional[int] = None) -> list[Message]:
        """按来源表取这次备份覆盖的消息。`group_id` 为空则取全部群。

        同一个 message_id 在多个群都有时按 (message_id, group_id) 区分——恢复时
        可能把一份备份搬到另一个群，不能靠消息 ID 单键认人。
        """
        sql = """SELECT m.* FROM messages m
                 JOIN backup_sources s
                   ON s.message_id = m.message_id AND s.group_id = m.group_id
                 WHERE s.backup_id = ?"""
        params: list = [backup_id]
        if group_id is not None:
            sql += " AND s.group_id = ?"
            params.append(group_id)
        sql += " ORDER BY m.timestamp ASC, m.id ASC"
        return [self._row_to_message(r) for r in self.query(sql, params)]

    def source_message_ids(self, backup_id: str, group_id: Optional[int] = None) -> set[str]:
        """来源清单里登记过的消息 ID，**不**管这些行还在不在库里。

        这条和 `get_source_messages` 的区别正是「被清理掉之后」：JOIN messages 会
        把已经删掉的行一并过滤掉，于是清单和库同时变小，差集永远为空。判断「这份
        备份原本有多少、后来少了多少」必须直接查清单表。
        """
        sql = "SELECT message_id FROM backup_sources WHERE backup_id = ?"
        params: list = [backup_id]
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        return {str(r["message_id"]) for r in self.query(sql, params)}

    def adopt_orphans(self, group_id: int, backup_id: str, message_ids) -> int:
        """把「归属的备份已经不存在」的消息改归给本次备份，并登记进来源清单。

        调用方只在确实翻到过新消息的那一轮里调用它：水位之后的页里全是已在库的
        消息，说明这一轮压根没打算重扫它们，不该顺手改归属。

        `/backup clean` 删记录时连带删了名下的消息行，但更早的版本、手工动过库、
        迁移失败都可能留下指向已消失 backup_id 的行。这些行占着唯一键
        (message_id, group_id)，后续备份看到「已在库里」直接跳过，于是它们永远进不了
        任何一次备份的来源清单——备份显示成功，恢复却是空的。

        只认一种无主：`backup_id` 在 backup_records 里找不到。

        **实时归档的消息不在这里收编。** 原来把 `backup_id = 'realtime'` 也算成无主，
        理由是「实时归档那批本来就该被本次备份覆盖」——覆盖是覆盖了（来源清单里会
        登记），但归属也被一起改成了本次备份。后果是 `/backup status` 那句
        「其中实时归档 N 条」永远归零，而 `count_realtime_only` 的注释里写的是
        「实时归档的归属故意不改，那是群被炸时的兜底数据」。两处口径互相打架，
        真按注释那样用（按 `backup_id='realtime'` 找兜底数据）就一条也找不到。
        现在归属保持不变，只登记来源清单。
        """
        ids = [str(i) for i in message_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        rows = self.query(
            f"""SELECT message_id, backup_id FROM messages
                WHERE group_id = ? AND message_id IN ({placeholders})
                  AND (backup_id = ?
                       OR backup_id NOT IN (SELECT backup_id FROM backup_records))""",
            (group_id, *ids, REALTIME_BACKUP_ID),
        )
        if not rows:
            return 0
        orphans = [r["message_id"] for r in rows if r["backup_id"] != REALTIME_BACKUP_ID]
        realtime = [r["message_id"] for r in rows if r["backup_id"] == REALTIME_BACKUP_ID]
        if orphans:
            self._conn.executemany(
                "UPDATE messages SET backup_id = ? WHERE group_id = ? AND message_id = ?",
                [(backup_id, group_id, mid) for mid in orphans],
            )
        # 登记来源清单对两种行都做：这份备份确实覆盖到了它们。实时归档只是不改归属
        self._conn.executemany(
            """INSERT OR IGNORE INTO backup_sources (backup_id, group_id, message_id)
               VALUES (?,?,?)""",
            [(backup_id, group_id, mid) for mid in orphans + realtime],
        )
        self._conn.commit()
        return len(orphans) + len(realtime)

    def count_realtime_only(self, group_id: int, backup_id: Optional[str] = None) -> int:
        """这个群里「只走过实时归档、还没被任何备份覆盖」的消息数。

        只看归属算不出来：实时归档的消息被某次备份扫到之后，归属仍然是
        `realtime`（那是故意的，群被炸时的兜底数据不能摘），但它已经在某份备份的
        来源清单里了。老写法按归属数，于是一次全量备份跑完，`/backup status`
        还在说「其中实时归档 N 条（还没被任何一次备份接手）」——永远不消失。
        """
        # 判据是「有没有落在**别的**备份的清单里」。只写「清单里没有它」就成了
        # `REALTIME_BACKUP_ID` 与 `REALTIME_BACKUP_ID` 相消的死条件，这个数恒为 0
        sql = """SELECT COUNT(*) AS c FROM messages m
                 WHERE m.group_id = ? AND m.backup_id = ?
                   AND NOT EXISTS (
                     SELECT 1 FROM backup_sources s
                     WHERE s.message_id = m.message_id AND s.group_id = m.group_id
                       AND s.backup_id <> ?
                 )"""
        params: list = [group_id, REALTIME_BACKUP_ID, REALTIME_BACKUP_ID]
        if backup_id:
            sql += " AND 0 = 1"  # 保留接口形状：指定备份时不重复计入
        row = self.query_one(sql, params)
        return int(row["c"]) if row else 0

    def count_sources(self, backup_id: str) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS c FROM backup_sources WHERE backup_id = ?", (backup_id,)
        )
        return int(row["c"]) if row else 0

    def source_group_ids(self, backup_id: str) -> list[int]:
        return [
            int(r["group_id"])
            for r in self.query(
                "SELECT DISTINCT group_id FROM backup_sources WHERE backup_id = ?",
                (backup_id,),
            )
        ]

    # ---------- 每份备份的群信息 / 成员快照 ----------
    def save_bundle(
        self,
        backup_id: str,
        group_id: int,
        group_info,
        members=None,
        announcements=None,
        group_files=None,
    ) -> None:
        """每份备份自己的群信息 / 成员 / 公告 / 文件清单快照。

        公告和文件清单也是「当时的样子」，必须跟着这份备份走：`group_announcements`
        那两张表是当前状态，任何一份备份导出时读到的都是「这个群现在有什么」。
        """
        payload = {
            "group_info": _group_info_to_dict(group_info) if group_info else None,
            "members": [_member_to_dict(m) for m in (members or [])],
        }
        if announcements is not None:
            payload["announcements"] = [_jsonable_row(a) for a in announcements]
        if group_files is not None:
            payload["group_files"] = [_jsonable_row(f) for f in group_files]
        self.execute(
            """INSERT OR REPLACE INTO backup_bundle (backup_id, group_id, payload)
               VALUES (?,?,?)""",
            (backup_id, group_id, json.dumps(payload, ensure_ascii=False)),
        )

    def get_bundle(self, backup_id: str, group_id: Optional[int] = None) -> dict:
        """取快照。不给 group_id 时：只有一份就直接用，多份则挑消息最多的那份。"""
        rows = self.query(
            "SELECT group_id, payload FROM backup_bundle WHERE backup_id = ?", (backup_id,)
        )
        if not rows:
            return {}
        if group_id is not None:
            for row in rows:
                if int(row["group_id"]) == int(group_id):
                    return json.loads(row["payload"])
        best = None
        for row in rows:
            payload = json.loads(row["payload"])
            score = len(payload.get("members") or [])
            if best is None or score > best[0]:
                best = (score, payload)
        return best[1] if best else {}

    def update_message_media(self, message_id: str, group_id: int, media_paths: list) -> int:
        """媒体补下后回填路径。"""
        cur = self.execute(
            """UPDATE messages SET media_paths = ?
               WHERE message_id = ? AND group_id = ?""",
            (json.dumps(list(media_paths), ensure_ascii=False), message_id, group_id),
        )
        return cur.rowcount if cur else 0

    def message_backup_id(self, message_id: str, group_id: int) -> str:
        row = self.query_one(
            "SELECT backup_id FROM messages WHERE message_id = ? AND group_id = ? LIMIT 1",
            (message_id, group_id),
        )
        return str(row["backup_id"] or "") if row else ""

    def message_exists(self, message_id: str, group_id: Optional[int]) -> bool:
        """这条消息是否已在库里。

        group_id 给 None 是「不知道群号」的问法（消息链解析不出群号时）：此时只按
        message_id 判，宁可当成已存在而跳过，也不要在恢复时把同一批消息再插一遍。
        唯一键是 (message_id, group_id)，给个 0 或 None 落库就是另一个群的消息。
        """
        if group_id is None:
            row = self.query_one(
                "SELECT 1 FROM messages WHERE message_id = ? LIMIT 1", (message_id,)
            )
        else:
            row = self.query_one(
                "SELECT 1 FROM messages WHERE message_id = ? AND group_id = ? LIMIT 1",
                (message_id, group_id),
            )
        return row is not None

    def get_messages(
        self,
        group_id: Optional[int] = None,
        backup_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Message]:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list = []
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        if backup_id is not None:
            sql += " AND backup_id = ?"
            params.append(backup_id)
        sql += " ORDER BY timestamp ASC, id ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._row_to_message(r) for r in self.query(sql, params)]

    def overwrite_message(self, msg: Message, backup_id: str = "") -> None:
        """整条覆盖写入（恢复的 overwrite 策略）。

        `save_message` 的 UPDATE 分支故意只动归属和媒体路径——日常备份不该把已有
        消息的内容改掉。要真覆盖就得显式调这里。
        """
        self.execute(
            """UPDATE messages
               SET sender_id = ?, sender_name = ?, timestamp = ?, message_type = ?,
                   content = ?, raw_content = ?, reply_to = ?, is_forward = ?,
                   media_paths = ?, forward_chain = ?
               WHERE message_id = ? AND group_id = ?""",
            (
                msg.sender_id,
                msg.sender_name,
                to_db(msg.timestamp),
                msg.message_type.value,
                msg.content,
                msg.raw_content,
                msg.reply_to,
                1 if msg.is_forward else 0,
                json.dumps(msg.media_paths, ensure_ascii=False),
                json.dumps(msg.forward_chain, ensure_ascii=False),
                msg.message_id,
                msg.group_id,
            ),
        )
        if backup_id:
            self.assign_message_backup(msg.message_id, msg.group_id, backup_id)

    def apply_merge(self, msg: Message) -> None:
        """把补齐后的字段写回去（恢复的 merge 策略）。

        `save_message` 的 UPDATE 分支不动内容，所以合并不是 upsert 能表达的事，
        单独写一条 UPDATE 更清楚。归属、媒体缓存等其它列一概不碰。
        """
        self.execute(
            """UPDATE messages
               SET content = ?, sender_name = ?, reply_to = ?, media_paths = ?
               WHERE message_id = ? AND group_id = ?""",
            (
                msg.content,
                msg.sender_name,
                msg.reply_to,
                json.dumps(msg.media_paths, ensure_ascii=False),
                msg.message_id,
                msg.group_id,
            ),
        )

    def get_message(self, message_id: str, group_id: int) -> Optional[Message]:
        row = self.query_one(
            "SELECT * FROM messages WHERE message_id = ? AND group_id = ?",
            (str(message_id), group_id),
        )
        return self._row_to_message(row) if row else None

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        try:
            mtype = MessageType(row["message_type"])
        except ValueError:
            mtype = MessageType.UNKNOWN
        return Message(
            message_id=row["message_id"],
            group_id=row["group_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"] or "",
            timestamp=from_db(row["timestamp"]) or datetime.min,
            message_type=mtype,
            content=row["content"] or "",
            raw_content=row["raw_content"] or "",
            reply_to=row["reply_to"],
            media_paths=json.loads(row["media_paths"] or "[]"),
            is_forward=bool(row["is_forward"]),
            forward_chain=json.loads(row["forward_chain"] or "[]"),
        )

    def latest_backup_message_key(self, group_id: int) -> Optional[tuple[datetime, str]]:
        """该群非实时归档消息里最新的一条，用作增量点基线。

        实时归档的行不参与：它按到达顺序写库，时间戳乱序，而且它不代表「备份已经
        覆盖到哪」——拿它当基线会让后续备份跳过一整个时间段的正常消息。
        """
        row = self.query_one(
            """SELECT timestamp, message_id FROM messages
               WHERE group_id = ? AND backup_id != ?
               ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (group_id, REALTIME_BACKUP_ID),
        )
        if row is None:
            return None
        ts = from_db(row["timestamp"])
        if ts is None:
            return None
        return ts, str(row["message_id"] or "")

    def latest_message_key(self, group_id: int) -> Optional[tuple[datetime, str]]:
        """库里该群最新一条「备份下来的」消息，用作续传水位。

        实时归档的行不算。它的时间戳按到达顺序写、可能乱序，而这里要回答的是
        「备份已经覆盖到哪」——`backup_group` 那边就是这么用的（见 candidates）。
        拿实时归档当水位，会把一整段正常消息判成「已经备过」而跳过。
        """
        row = self.query_one(
            """SELECT timestamp, message_id FROM messages
               WHERE group_id = ? AND backup_id != ?
               ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (group_id, REALTIME_BACKUP_ID),
        )
        if row is None:
            return None
        ts = from_db(row["timestamp"])
        if ts is None:
            return None
        return ts, str(row["message_id"] or "")

    def count_messages(
        self, group_id: Optional[int] = None, backup_id: Optional[str] = None
    ) -> int:
        sql = "SELECT COUNT(*) AS c FROM messages WHERE 1=1"
        params: list = []
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        if backup_id is not None:
            sql += " AND backup_id = ?"
            params.append(backup_id)
        row = self.query_one(sql, params)
        return int(row["c"]) if row else 0

    def delete_messages(self, group_id: int, message_ids: list[str]) -> int:
        if not message_ids:
            return 0
        marks = ",".join("?" * len(message_ids))
        cur = self.execute(
            f"DELETE FROM messages WHERE group_id = ? AND message_id IN ({marks})",
            [group_id, *message_ids],
        )
        return cur.rowcount

    def delete_sources(self, group_id: int, message_ids: list[str]) -> int:
        """撤销这些消息在**本群**的来源登记。

        消息被覆盖写入后，旧记录不能再说「我覆盖过这条」——否则按来源导出或恢复时
        取出来的还是覆盖前的内容。别的群同 ID 的消息不受影响。
        """
        if not message_ids:
            return 0
        marks = ",".join("?" * len(message_ids))
        cur = self.execute(
            f"DELETE FROM backup_sources WHERE group_id = ? AND message_id IN ({marks})",
            [group_id, *message_ids],
        )
        return cur.rowcount

    # ---------- 媒体 ----------
    def save_media(
        self,
        message_id: str,
        group_id: int,
        original_url: str,
        local_path: str,
        filename: str,
        file_size: int,
        mime_type: str,
        sha256: str,
    ) -> None:
        # 媒体补下、续传重跑、同一消息的多个 URL 都会走到这里。按
        # (message_id, original_url) 唯一，重复下载只更新落盘信息，不再多插一行，
        # 否则媒体数会随重跑次数线性膨胀
        self.execute(
            """INSERT INTO media_files
               (message_id, group_id, original_url, local_path, filename, file_size,
                mime_type, sha256, downloaded_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(message_id, original_url) DO UPDATE SET
                 group_id = excluded.group_id,
                 local_path = excluded.local_path,
                 filename = excluded.filename,
                 file_size = excluded.file_size,
                 mime_type = excluded.mime_type,
                 sha256 = excluded.sha256,
                 downloaded_at = excluded.downloaded_at""",
            (
                message_id,
                group_id,
                original_url,
                local_path,
                filename,
                file_size,
                mime_type,
                sha256,
                to_db(datetime.now()),
            ),
        )

    def get_media_for_message(self, message_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM media_files WHERE message_id = ?", (message_id,))

    def find_media_by_url(self, group_id: int, message_id: str, url: str) -> Optional[str]:
        """这条消息的这个 URL 已经落过盘吗？返回落盘路径。

        媒体去重原来只有一层「进程内 URL → 路径」的字典，重启即空。重启之后
        同一条消息的同一张图会被判定成「没下过」再下一遍、落成新文件，旧文件
        留在盘上没人认领——`/backup clean` 只删得掉库里登记过的那些，剩下的就是
        纯泄漏。库里这一行才是「下过没有」的真凭据。
        """
        row = self.query_one(
            """SELECT local_path FROM media_files
               WHERE message_id = ? AND original_url = ? AND group_id = ?
               ORDER BY id DESC LIMIT 1""",
            (message_id, url, group_id),
        )
        if not row:
            return None
        local = str(row["local_path"] or "")
        return local if local and Path(local).is_file() else None

    # ---------- 备份记录 ----------
    def save_record(self, record: BackupRecord) -> None:
        self.execute(
            """INSERT OR REPLACE INTO backup_records
               (backup_id, group_id, status, started_at, finished_at, message_count,
                new_count, package_count, media_count, total_size, is_incremental,
                base_backup_id, checksum, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.backup_id,
                record.group_id,
                record.status.value,
                to_db(record.started_at),
                to_db(record.finished_at),
                record.message_count,
                record.new_count,
                record.package_count,
                record.media_count,
                record.total_size,
                1 if record.is_incremental else 0,
                record.base_backup_id,
                record.checksum,
                record.error,
            ),
        )

    def get_record(self, backup_id: str) -> Optional[BackupRecord]:
        row = self.query_one("SELECT * FROM backup_records WHERE backup_id = ?", (backup_id,))
        return self._row_to_record(row) if row else None

    def list_records(self, group_id: Optional[int] = None, limit: int = 50) -> list[BackupRecord]:
        if group_id is None:
            rows = self.query(
                "SELECT * FROM backup_records ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.query(
                "SELECT * FROM backup_records WHERE group_id = ? ORDER BY started_at DESC LIMIT ?",
                (group_id, limit),
            )
        return [self._row_to_record(r) for r in rows]

    def latest_completed_record(self, group_id: int) -> Optional[BackupRecord]:
        """最近一次「真的落了消息」的备份。

        只按状态过滤会让一次什么都没备份的新记录顶掉上一次的实绩——那之后
        「最近备份」就永远显示 0 条。这里改为按消息数筛，全空时退回任意完成记录。
        """
        # PARTIAL 也要算数：它的意思是「消息都备上了，只是有几个媒体没下下来」。
        # 只认 COMPLETED 的话，一次媒体下载失败就会让 `/backup status` 报
        # 「还没有完成过备份」、`export latest` 导不出任何东西、`/backup list`
        # 里那条最完整的备份被当成不存在——而数据明明就在库里。
        ok = (BackupStatus.COMPLETED.value, BackupStatus.PARTIAL.value)
        marks = ",".join("?" * len(ok))
        row = self.query_one(
            f"""SELECT * FROM backup_records
                WHERE group_id = ? AND status IN ({marks}) AND message_count > 0
                ORDER BY started_at DESC LIMIT 1""",
            (group_id, *ok),
        ) or self.query_one(
            f"""SELECT * FROM backup_records
                WHERE group_id = ? AND status IN ({marks})
                ORDER BY started_at DESC LIMIT 1""",
            (group_id, *ok),
        )
        return self._row_to_record(row) if row else None

    def set_package_count(self, backup_id: str, count: int) -> None:
        """记下这份备份最近一次导出包里装了几条消息。

        恢复端「包里有几条」不能拿 `message_count` 说：那个数回答的是「这份备份
        覆盖保住了几条」，而导出包还含只在实时归档里的消息，两者本来就不等。
        写一个必然对不上的数进回执，比不写更糟——用户会以为包是坏的。
        """
        self.execute(
            "UPDATE backup_records SET package_count = ? WHERE backup_id = ?",
            (int(count or 0), backup_id),
        )

    def assign_message_backup(self, message_id: str, group_id: int, backup_id: str) -> None:
        """把已有消息挂到指定备份上（恢复时补齐归属）。"""
        self.execute(
            "UPDATE messages SET backup_id = ? WHERE message_id = ? AND group_id = ?",
            (backup_id, message_id, group_id),
        )

    def count_backup_source(self, backup_id: str, group_id: Optional[int] = None) -> int:
        """某次备份覆盖了多少条消息（来源清单口径）。

        这个数回答的是「这份备份保住了什么」。不能按 `messages.backup_id` 数：
        那个归属只有一个值，记的是最后归档这条消息的备份，一次全量重跑就把整群
        划到新备份名下，老备份数出来是 0 —— 而它明明还在那儿、还能恢复。

        升级前的老库没有来源清单，此时才退回按归属数。
        """
        sql = "SELECT COUNT(*) AS c FROM backup_sources WHERE backup_id = ?"
        params: list = [backup_id]
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        row = self.query_one(sql, params)
        if row and row["c"]:
            return int(row["c"])
        sql = "SELECT COUNT(*) AS c FROM messages WHERE backup_id = ?"
        params = [backup_id]
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        row = self.query_one(sql, params)
        return int(row["c"]) if row else 0

    def list_backup_ids_with_messages(self, group_id: int) -> list[str]:
        """该群里真正挂着消息的备份 ID。

        导出要的是「这次备份覆盖了哪些消息」，没有消息归属的备份记录不该进去，
        否则导出进度、条数统计全都会被空记录带偏。
        """
        rows = self.query(
            """SELECT DISTINCT backup_id FROM messages
               WHERE group_id = ? AND backup_id != ''""",
            (group_id,),
        )
        return [r["backup_id"] for r in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> BackupRecord:
        try:
            status = BackupStatus(row["status"])
        except ValueError:
            status = BackupStatus.FAILED
        return BackupRecord(
            backup_id=row["backup_id"],
            group_id=row["group_id"],
            status=status,
            started_at=from_db(row["started_at"]) or datetime.min,
            finished_at=from_db(row["finished_at"]),
            message_count=row["message_count"] or 0,
            new_count=(row["new_count"] if "new_count" in row.keys() else 0) or 0,
            package_count=(row["package_count"] if "package_count" in row.keys() else 0) or 0,
            media_count=row["media_count"] or 0,
            total_size=row["total_size"] or 0,
            is_incremental=bool(row["is_incremental"]),
            base_backup_id=row["base_backup_id"],
            checksum=row["checksum"] or "",
            error=row["error"],
        )

    def delete_record(self, backup_id: str) -> None:
        """删一条备份记录。

        连带删掉它名下的消息：`messages.backup_id` 是「最后一次归档这条消息的
        备份」，记录没了，这行归属就指向一个不存在的 ID，既不属于任何备份、又
        把唯一键 (message_id, group_id) 占住。占住的后果比丢行更严重：之后的备份
        看到「已在库里」就跳过，于是这批消息永远进不了任何备份的来源清单，恢复时
        一条也取不出来。

        只删归属是本条的，别人接手的那些一行不动。
        """
        self.execute("DELETE FROM messages WHERE backup_id = ?", (backup_id,))
        # 来源清单跟着记录一起走。留着它，`/backup verify <已删除的 ID>` 会按清单
        # 报出一堆「覆盖过的消息」，而记录已经不存在；恢复时也会先在清单里找到
        # 消息、再拿一条不存在的记录去比对，报出来的是一句看不懂的话。
        # 淘汰记录不动消息，别的备份对同一条消息的登记也不动——只删这一份的。
        self.execute("DELETE FROM backup_sources WHERE backup_id = ?", (backup_id,))
        # 快照和导出证据同属这条记录。以前只删 records/sources，bundle 和
        # export_evidence 留在库里；等某次导出复用到同一个 backup_id 时，恢复端会
        # 拿上一世代留下的证据去比对，正常包也能被判成「被改过」。
        self.execute("DELETE FROM backup_bundle WHERE backup_id = ?", (backup_id,))
        self.execute("DELETE FROM export_evidence WHERE backup_id = ?", (backup_id,))
        self.execute("DELETE FROM backup_records WHERE backup_id = ?", (backup_id,))

    # ---------- 群信息 / 成员 ----------
    def save_group_info(self, info: GroupInfo) -> None:
        self.execute(
            """INSERT OR REPLACE INTO group_info
               (group_id, group_name, owner_id, created_at, member_count, max_members,
                group_level, last_synced_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                info.group_id,
                info.group_name,
                info.owner_id,
                to_db(info.created_at),
                info.member_count,
                info.max_members,
                info.group_level,
                to_db(datetime.now()),
            ),
        )

    def get_group_info(self, group_id: int) -> Optional[GroupInfo]:
        row = self.query_one("SELECT * FROM group_info WHERE group_id = ?", (group_id,))
        if not row:
            return None
        return GroupInfo(
            group_id=row["group_id"],
            group_name=row["group_name"] or "",
            owner_id=row["owner_id"] or 0,
            created_at=from_db(row["created_at"]),
            member_count=row["member_count"] or 0,
            max_members=row["max_members"] or 0,
            group_level=row["group_level"] or 0,
        )

    def replace_members(self, group_id: int, members: list[GroupMember]) -> int:
        """整体覆盖一次成员快照，返回删掉的过期成员数。

        成员列表是「当前状态」而不是流水：`save_members` 只做 upsert，谁退群了永远
        不会消失，名单只会越攒越大。按快照语义整群替换，退群的人才会真的消失。
        """
        if not members:
            return 0
        now = to_db(datetime.now())
        keep = {int(m.user_id) for m in members}
        stale = [
            r["user_id"]
            for r in self.query(
                "SELECT user_id FROM group_members WHERE group_id = ?", (group_id,)
            )
            if int(r["user_id"]) not in keep
        ]
        if stale:
            marks = ",".join("?" * len(stale))
            self.execute(
                f"DELETE FROM group_members WHERE group_id = ? AND user_id IN ({marks})",
                [group_id, *stale],
            )
        for m in members:
            self.execute(
                """INSERT OR REPLACE INTO group_members
                   (group_id, user_id, nickname, card, role, join_time, last_active, synced_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    group_id,
                    m.user_id,
                    m.nickname,
                    m.card,
                    m.role,
                    to_db(m.join_time),
                    to_db(m.last_active),
                    now,
                ),
            )
        return len(stale)

    def save_members(self, group_id: int, members: list[GroupMember]) -> None:
        now = to_db(datetime.now())
        for m in members:
            self.execute(
                """INSERT OR REPLACE INTO group_members
                   (group_id, user_id, nickname, card, role, join_time, last_active, synced_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    group_id,
                    m.user_id,
                    m.nickname,
                    m.card,
                    m.role,
                    to_db(m.join_time),
                    to_db(m.last_active),
                    now,
                ),
            )

    def get_members(self, group_id: int) -> list[GroupMember]:
        rows = self.query("SELECT * FROM group_members WHERE group_id = ?", (group_id,))
        return [
            GroupMember(
                user_id=r["user_id"],
                group_id=r["group_id"],
                nickname=r["nickname"] or "",
                card=r["card"] or "",
                role=r["role"] or "member",
                join_time=from_db(r["join_time"]),
                last_active=from_db(r["last_active"]),
            )
            for r in rows
        ]

    # ---------- 保留策略 ----------
    def prune_records(
        self, keep_count: Optional[int] = None, keep_days: Optional[int] = None
    ) -> int:
        """按保留策略淘汰备份记录，返回淘汰的条数。

        淘汰 = 记录消失，消息**还在**，只是不再属于任何一次备份（走
        `backup_sources`）。这里必须和 `delete_record` 一致：只删 rows、留 messages，
        否则一次自动清理就能把群里的聊天记录连根拔掉，而备份插件最该保住的就是
        这些。`delete_record` 连消息一起删，因为它是显式「不要这条备份了」，
        记录没了还留着指向废墟的归属只会在唯一键上占位。

        `keep_days <= 0` 表示不做时间淘汰：留空或写 0 的配置不该被理解成「立刻
        全删」。
        """
        removed = 0
        doomed: list[str] = []
        if keep_days and keep_days > 0:
            cutoff = datetime.now() - timedelta(days=keep_days)
            doomed += [
                str(r["backup_id"])
                for r in self.query(
                    "SELECT backup_id FROM backup_records WHERE started_at < ?",
                    (to_db(cutoff),),
                )
            ]
        if keep_count and keep_count > 0:
            rows = self.query(
                """SELECT backup_id FROM backup_records
                   ORDER BY started_at DESC LIMIT -1 OFFSET ?""",
                (int(keep_count),),
            )
            doomed += [str(row["backup_id"]) for row in rows]
        for backup_id in dict.fromkeys(doomed):
            self.execute("DELETE FROM backup_records WHERE backup_id = ?", (backup_id,))
            # 记录走了，它的来源清单和快照也留不住；messages 一行不动，归属也不改，
            # 交给 `adopt_orphans` 在下一次备份时收编
            self.execute("DELETE FROM backup_sources WHERE backup_id = ?", (backup_id,))
            self.execute("DELETE FROM backup_bundle WHERE backup_id = ?", (backup_id,))
            self.execute("DELETE FROM export_evidence WHERE backup_id = ?", (backup_id,))
            removed += 1
        return removed

    def prune_media(self, keep_days: int) -> int:
        """删掉早于保留期的媒体文件（按消息时间），连落盘文件一起清。

        同一个 message_id 在别的群可能是一条新消息，而媒体是按消息 ID 命名落盘的。
        只看「这条旧消息的媒体」就删，会把新群还在用的那份文件一起删掉——那个群
        从此导不出图，`media_files` 里那行还指着不存在的路径。所以只要还有一条
        未过期的消息占着这个 ID，就不动。
        """
        if not keep_days or keep_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=keep_days)
        rows = self.query(
            """SELECT m.id, m.local_path FROM media_files m
               JOIN messages msg ON msg.message_id = m.message_id AND msg.group_id = m.group_id
               WHERE msg.timestamp < ?
                 AND NOT EXISTS (
                   SELECT 1 FROM messages keep
                   WHERE keep.message_id = m.message_id AND keep.timestamp >= ?
                 )""",
            (to_db(cutoff), to_db(cutoff)),
        )
        removed = 0
        for row in rows:
            path = Path(row["local_path"] or "")
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                continue
            self.execute("DELETE FROM media_files WHERE id = ?", (row["id"],))
        return removed

    # ---------- 导出证据 ----------
    def save_export_evidence(
        self, backup_id: str, group_id: int, snapshot: dict, evidence: str, exported_at=None
    ) -> None:
        """记下「这份备份导出时本机看到的群名/成员表」及其指纹。

        只写这一行就能挡住篡改：包里的快照和它比，改了就露。指纹单独存是为了
        比对时不用再把整份快照读出来算 hash。
        """
        self.execute(
            """INSERT OR REPLACE INTO export_evidence
               (backup_id, group_id, snapshot, evidence, exported_at)
               VALUES (?,?,?,?,?)""",
            (
                backup_id,
                group_id,
                json.dumps(snapshot or {}, ensure_ascii=False, sort_keys=True),
                evidence,
                to_db(exported_at or datetime.now()) if isinstance(exported_at, datetime)
                else to_db(datetime.now()),
            ),
        )

    def get_export_evidence(self, backup_id: str, group_id: Optional[int] = None) -> Optional[dict]:
        """取导出证据。`group_id` 为空时取这份备份最近一次导出留下的那行。"""
        sql = "SELECT * FROM export_evidence WHERE backup_id = ?"
        params: list = [backup_id]
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        sql += " ORDER BY exported_at DESC LIMIT 1"
        row = self.query_one(sql, params)
        if row is None:
            return None
        try:
            snapshot = json.loads(row["snapshot"] or "{}")
        except ValueError:
            snapshot = {}
        return {"snapshot": snapshot, "evidence": row["evidence"], "group_id": row["group_id"]}

    # ---------- 群公告 / 群文件 ----------
    def save_announcements(self, group_id: int, items: list[dict]) -> int:
        """整群覆盖式写入公告快照，返回条数。

        群号统一用参数：解析器把每条公告的群号放在 `group_id` 里，直接写会影响
        `INSERT OR IGNORE` 的去重键。写库时以整群为准，条目里的群号只在
        「参数缺失」时才被采信——恢复时目标群可能和包里的原群不同。
        """
        group_id = int(group_id or 0) or int((items or [{}])[0].get("group_id") or 0)
        now = to_db(datetime.now())
        # 条目里的 published_at 可能是 datetime，也可能是导出包里读回来的 ISO 串。
        # 直接传给 sqlite3 会抛 InterfaceError——整次备份在「存公告」这一步崩掉，
        # 而消息明明已经全部落库（备份报 failed）。
        def _when(value):
            return to_db(_naive_dt(value)) if value else None

        self.execute("DELETE FROM group_announcements WHERE group_id = ?", (group_id,))
        rows = [
            (
                group_id,
                str(a.get("content") or ""),
                int(a.get("sender_id") or 0),
                str(a.get("sender_name") or ""),
                _when(a.get("published_at")),
                str(a.get("raw") or ""),
                now,
            )
            for a in items or []
        ]
        if rows:
            self._conn.executemany(
                """INSERT OR IGNORE INTO group_announcements
                   (group_id, content, sender_id, sender_name, published_at, raw_content, synced_at)
                   VALUES (?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def get_announcements(self, group_id: int) -> list[dict]:
        return [
            dict(row)
            for row in self.query(
                "SELECT * FROM group_announcements WHERE group_id = ? ORDER BY published_at",
                (group_id,),
            )
        ]

    def save_group_files(self, group_id: int, items: list[dict]) -> int:
        """整群覆盖式写入群文件列表快照，返回**实际入库**的条数。

        文件以 (群, file_id) 唯一。平台给了两条重复的 file_id（同名文件被重新上传
        过、或者列表里天然有重复项）时，`INSERT OR REPLACE` 只留后一条——旧写法
        返回的是输入条数，比库里多出来一个，调用方按它报数就会说谎。
        """
        now = to_db(datetime.now())
        self.execute("DELETE FROM group_files WHERE group_id = ?", (group_id,))

        def _when(value):
            return to_db(_naive_dt(value)) if value else None

        rows = [
            (
                group_id,
                str(f.get("file_id") or f.get("name") or ""),
                str(f.get("name") or ""),
                int(f.get("size") or 0),
                int(f.get("busid") or 0),
                _when(f.get("upload_time")),
                int(f.get("uploader_id") or 0),
                str(f.get("uploader_name") or ""),
                str(f.get("raw") or ""),
                now,
            )
            for f in items or []
            if (f.get("file_id") or f.get("name"))
        ]
        if rows:
            self._conn.executemany(
                """INSERT OR REPLACE INTO group_files
                   (group_id, file_id, name, size, busid, upload_time,
                    uploader_id, uploader_name, raw_content, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()
        row = self.query_one(
            "SELECT COUNT(*) AS c FROM group_files WHERE group_id = ?", (group_id,)
        )
        return int(row["c"]) if row else 0

    def get_group_files(self, group_id: int) -> list[dict]:
        return [
            dict(row)
            for row in self.query(
                "SELECT * FROM group_files WHERE group_id = ? ORDER BY upload_time",
                (group_id,),
            )
        ]

    # ---------- 定时任务 ----------
    def save_schedule(self, group_id: int, cron: str, enabled: bool = True) -> None:
        self.execute(
            """INSERT OR REPLACE INTO schedules (group_id, cron, enabled, updated_at)
               VALUES (?,?,?,?)""",
            (group_id, cron, 1 if enabled else 0, to_db(datetime.now())),
        )

    def list_schedules(self) -> list[dict]:
        rows = self.query("SELECT * FROM schedules ORDER BY group_id")
        return [
            {"group_id": r["group_id"], "cron": r["cron"], "enabled": bool(r["enabled"])}
            for r in rows
        ]

    def delete_schedule(self, group_id: int) -> None:
        self.execute("DELETE FROM schedules WHERE group_id = ?", (group_id,))

    # ---------- 下载 token ----------
    def save_token(self, token: str, path: str, expires: float) -> None:
        self.execute(
            "INSERT OR REPLACE INTO download_tokens (token, path, expires, opened_at)"
            " VALUES (?, ?, ?, NULL)",
            (token, str(path), float(expires)),
        )

    def get_token(self, token: str) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT token, path, expires, opened_at FROM download_tokens WHERE token = ?",
            (token,),
        )

    def open_token(self, token: str, now: Optional[float] = None) -> None:
        """记下「第一次兑换」的时刻。已经有值就不动——后续片段不算新的开头。"""
        self.execute(
            "UPDATE download_tokens SET opened_at = COALESCE(opened_at, ?) WHERE token = ?",
            (time.time() if now is None else now, token),
        )

    def has_been_opened(self, token: str, now: Optional[float] = None, grace: float = 0.0) -> bool:
        """这条 token 有没有**别人**先领过（`grace` 秒内算同一次下载的开头）。

        原来是「整份取走就删 token」，于是拿不到任何「领过」的痕迹：_Range_
        片段请求必须在整份取走之前到达（浏览器先探测再续传），而续传请求同样
        过 `open_token` 这一关——两件事互斥，只能二选一：

        - 片段不删 token（为了让续传过得去）→ 链接被转发后第二个人照样能整份下载；
        - 片段删 token（为了「仅一次」）→ 探测之后真正的续传全是 410。

        守住的必须是「仅可使用一次」这句承诺，所以改成记 `opened_at`。
        """
        row = self.get_token(token)
        opened = row["opened_at"] if row is not None else None
        if opened is None:
            return False
        return (time.time() if now is None else now) - float(opened) > grace

    def delete_token(self, token: str) -> None:
        self.execute("DELETE FROM download_tokens WHERE token = ?", (token,))

    def purge_expired_tokens(self, now: float) -> Optional[int]:
        """清掉过期 token。`now` 给 None 表示按真实时间。

        原来调用方传的是「文件 mtime 的截止刻」——`time.time() - keep_days*86400`，
        比 `time.time()` 小整整一个保留期。于是 `expires < cutoff` 永远不成立，
        过期 token 从来没被清过，`download_tokens` 只会越攒越多。
        """
        cur = self.execute(
            "DELETE FROM download_tokens WHERE expires < ?", (time.time() if now is None else now,)
        )
        return cur.rowcount if cur else 0
