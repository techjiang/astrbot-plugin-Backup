"""SQLite 存储层。所有 SQL 收敛在这里，其余模块只与 Database 打交道。"""

import json
import sqlite3
from datetime import datetime
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
  UNIQUE(message_id, group_id, backup_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_group ON messages(group_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_msg_sender ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_msg_type ON messages(message_type);

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
  downloaded_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_media_msg ON media_files(message_id);

CREATE TABLE IF NOT EXISTS backup_records (
  backup_id TEXT PRIMARY KEY,
  group_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at DATETIME,
  finished_at DATETIME,
  message_count INTEGER DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS backup_meta (
  group_id INTEGER PRIMARY KEY,
  last_timestamp DATETIME,
  last_message_id TEXT
);

CREATE TABLE IF NOT EXISTS download_tokens (
  token TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  expires REAL NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schedules (
  group_id INTEGER PRIMARY KEY,
  cron TEXT NOT NULL,
  enabled BOOLEAN DEFAULT 1,
  updated_at DATETIME
);
"""

_DT_FMT = "%Y-%m-%d %H:%M:%S.%f"


def to_db(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime(_DT_FMT) if dt else None


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
        """写入消息。同 backup_id 下重复消息直接忽略，返回是否真的写入。"""
        cur = self.execute(
            """INSERT OR IGNORE INTO messages
               (message_id, group_id, sender_id, sender_name, timestamp, message_type,
                content, raw_content, reply_to, is_forward, media_paths, forward_chain, backup_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
        return cur.rowcount > 0

    def update_message_media(self, message_id: str, group_id: int, media_paths: list) -> int:
        """媒体补下后回填路径。"""
        cur = self.execute(
            "UPDATE messages SET media_paths = ? WHERE message_id = ? AND group_id = ?",
            (json.dumps(list(media_paths), ensure_ascii=False), message_id, group_id),
        )
        return cur.rowcount if cur else 0

    def message_exists(self, message_id: str, group_id: int) -> bool:
        return (
            self.query_one(
                "SELECT 1 FROM messages WHERE message_id = ? AND group_id = ? LIMIT 1",
                (message_id, group_id),
            )
            is not None
        )

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

    def count_messages(self, group_id: Optional[int] = None) -> int:
        if group_id is None:
            row = self.query_one("SELECT COUNT(*) AS c FROM messages")
        else:
            row = self.query_one("SELECT COUNT(*) AS c FROM messages WHERE group_id = ?", (group_id,))
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
        self.execute(
            """INSERT INTO media_files
               (message_id, group_id, original_url, local_path, filename, file_size,
                mime_type, sha256, downloaded_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
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

    # ---------- 备份记录 ----------
    def save_record(self, record: BackupRecord) -> None:
        self.execute(
            """INSERT OR REPLACE INTO backup_records
               (backup_id, group_id, status, started_at, finished_at, message_count,
                media_count, total_size, is_incremental, base_backup_id, checksum, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.backup_id,
                record.group_id,
                record.status.value,
                to_db(record.started_at),
                to_db(record.finished_at),
                record.message_count,
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
        row = self.query_one(
            """SELECT * FROM backup_records
               WHERE group_id = ? AND status = ?
               ORDER BY started_at DESC LIMIT 1""",
            (group_id, BackupStatus.COMPLETED.value),
        )
        return self._row_to_record(row) if row else None

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
            media_count=row["media_count"] or 0,
            total_size=row["total_size"] or 0,
            is_incremental=bool(row["is_incremental"]),
            base_backup_id=row["base_backup_id"],
            checksum=row["checksum"] or "",
            error=row["error"],
        )

    def delete_record(self, backup_id: str) -> None:
        self.execute("DELETE FROM messages WHERE backup_id = ?", (backup_id,))
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
            "INSERT OR REPLACE INTO download_tokens (token, path, expires) VALUES (?, ?, ?)",
            (token, str(path), float(expires)),
        )

    def get_token(self, token: str) -> Optional[sqlite3.Row]:
        return self.query_one(
            "SELECT token, path, expires FROM download_tokens WHERE token = ?", (token,)
        )

    def delete_token(self, token: str) -> None:
        self.execute("DELETE FROM download_tokens WHERE token = ?", (token,))

    def purge_expired_tokens(self, now: float) -> int:
        cur = self.execute("DELETE FROM download_tokens WHERE expires < ?", (now,))
        return cur.rowcount if cur else 0
