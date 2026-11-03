"""恢复引擎。恢复是危险操作：先校验完整性，再按策略写入。"""

import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .database import (
    REALTIME_BACKUP_ID,
    Database,
    group_info_from_dict,
    members_from_list,
)
from .models import Message
from .parser import parse_message
from .verifier import compute_checksum, read_manifest, verify_zip

logger = logging.getLogger(__name__)


class RestoreError(Exception):
    """恢复前置校验失败。"""


@dataclass
class RestoreResult:
    backup_id: str
    target_group_id: int
    inserted: int
    skipped: int
    overwritten: int
    checksum_ok: bool
    message: str = ""


class RestoreEngine:
    STRATEGIES = ("skip", "overwrite", "merge")

    def __init__(self, db: Database):
        self.db = db

    def verify_backup(
        self,
        backup_id: str,
        archive: Optional[str | Path] = None,
        expected_checksum: Optional[str] = None,
    ) -> tuple[bool, str]:
        """先查记录：不存在/未完成都拒绝；给了备份包就连包一起查。

        包的校验和只跟包自己的 manifest 比。拿备份记录里的校验和去比会误伤
        `export latest` 的包——那个包含实时归档与更早的数据，天然不等于记录里
        「本次新增」的那一撮。

        `expected_checksum` 是调用方手里那份「可信指纹」（命令层传的是备份记录里的
        校验和）：包内 manifest 被一起改过时，只有它能把篡改认出来。
        """
        record = self.db.get_record(backup_id)
        if record is None and archive is None:
            raise RestoreError(f"备份 {backup_id} 不存在（本机没有这条记录，也没有给备份包）")
        if record is not None:
            from .models import BackupStatus

            if record.status not in (BackupStatus.COMPLETED, BackupStatus.PARTIAL):
                raise RestoreError(f"备份 {backup_id} 状态为 {record.status.value}，不可恢复")
        if archive is not None:
            # 本地没有记录时只能信包自己的 manifest：这正是「换台机器、把包拿出来
            # 恢复」的场景，包里必须自带够用的元数据
            from pathlib import Path as _Path

            if not _Path(str(archive)).is_file():
                # 不过滤会把「路径打错」和「包真被改过」混成同一句「校验失败」
                raise RestoreError(f"备份文件不存在：{archive}")
            ok, reason = verify_zip(archive, expected_checksum)
            if not ok:
                raise RestoreError(f"备份文件校验失败：{reason}")
        return True, "校验通过"

    async def restore(
        self,
        backup_id: str,
        target_group_id: Optional[int] = None,
        strategy: str = "skip",
        archive: Optional[str | Path] = None,
        confirm: bool = False,
    ) -> RestoreResult:
        if not confirm:
            raise RestoreError("恢复需要二次确认：请在命令后加 --confirm")
        if strategy not in self.STRATEGIES:
            raise RestoreError(f"未知冲突策略：{strategy}")

        # 记录里的校验和在这里只做定位参考，不能当硬性门槛：`export latest` 的包
        # 含实时归档与更早的数据，天然不等于它。包和记录是不是同一批消息，交给
        # _check_content：只有群里每条消息都归这条记录管时才要求两者相等。
        self.verify_backup(backup_id, archive)
        record = self.db.get_record(backup_id)
        manifest = read_manifest(archive) if archive is not None else {}
        # 包里的群号优先于本地记录：同一台机器上可能有一条同名记录指向别的群，
        # 而包的 manifest 写的是它真正备份的那个群
        source_group = self._source_group_id(record, manifest)
        target = target_group_id or source_group
        if not target:
            raise RestoreError("无法确定目标群号，请显式指定：/backup restore <备份ID> <群号>")

        messages = self._load_messages(backup_id, archive, source_group)
        if not messages:
            raise RestoreError("备份里没有可恢复的消息")
        self._check_content(record, messages, archive)

        # 一份备份可能横跨多个群（全量备份把库里同群的旧数据一起收了进来，或者
        # 恢复时把消息重新归属给了不存在的记录）。整个搬进目标群等于凭空造出
        # 一堆「别人群里的消息」，只保留有归属的那部分。
        stray_groups = {m.group_id for m in messages if source_group and m.group_id != source_group}
        if stray_groups:
            logger.warning(
                "备份 %s 含 %s 个其他群的消息，只恢复群 %s 的部分",
                backup_id,
                len(stray_groups),
                source_group,
            )
            messages = [m for m in messages if m.group_id == source_group]

        inserted = skipped = overwritten = 0
        for msg in messages:
            msg.group_id = target
            existed = self.db.message_exists(msg.message_id, target)
            if existed:
                if strategy == "skip":
                    skipped += 1
                    continue
                if strategy == "overwrite":
                    self.db.delete_messages(target, [msg.message_id])
                    overwritten += 1
                # merge：保留原有，仅补齐缺失字段（这里等同跳过，但不计数为 skip）
            if self.db.save_message(msg, backup_id):
                inserted += 1
            elif not existed:
                # 没插入也没在目标群存在过：旧归属（恢复前就在别的记录名下）要补成
                # 这次恢复的记录，否则导出/统计按备份取数时会看不到它
                self.db.assign_message_backup(msg.message_id, target, backup_id)

        # 换台机器恢复时本地没有记录也没有快照。缺了它们：`/backup list` 看不到这次
        # 恢复、导出的包里没有群名和成员、按备份取数也取不到——包白恢复了。
        self._ensure_record(backup_id, target, messages, manifest)
        self._ensure_snapshot(backup_id, target, manifest)
        self.db.add_sources(backup_id, [m for m in messages if m.group_id == target])

        return RestoreResult(
            backup_id=backup_id,
            target_group_id=target,
            inserted=inserted,
            skipped=skipped,
            overwritten=overwritten,
            checksum_ok=True,
            message="恢复完成",
        )

    def _trusted_checksum(self, backup_id: str) -> Optional[str]:
        """记录里那份校验和，是本地库里唯一没被包作者碰过的指纹。"""
        record = self.db.get_record(backup_id)
        return (record.checksum or "") if record else None

    def _check_content(self, record, messages: list[Message], archive) -> None:
        """内容与备份记录对不上的处理。

        备份记录里的校验和覆盖的是「这次备份新增的消息」，不是整群，所以分四种情况：
        - 从库里恢复：消息集应当与记录完全一致，不一致就是库被改过；
        - 从 ZIP 包恢复：包自带 manifest 校验和（verify_backup 已跟包 CRC 一起查过，
          这里再算一遍，防止包内各份导出自相矛盾），不以记录为准；
        - 记录本身没有校验和（老库）：只能靠包清单把关。
        """
        if not messages:
            raise RestoreError("备份包内没有可恢复的消息")
        actual = compute_checksum(messages)
        if record is None:
            # 本机没有这条记录（另一台机器 / 记录已被 /backup clean 淘汰）：能信的
            # 只有包自己的清单，上面已随 CRC 一起校验过，这里不再拿库去比对
            return
        if archive is not None:
            declared = (read_manifest(archive) or {}).get("checksum")
            if declared and actual != declared:
                raise RestoreError("备份包内容与其清单不一致，备份可能已损坏")
            # 清单和内容可以一起被改：改完用恢复端自己的算法重算校验和填回去，
            # 上面这条就形同虚设。所以再拿库里那份可信指纹（备份记录的 checksum）
            # 对一遍消息的「身份」：列表里出现记录之外的消息，说明夹带了东西。
            stray = self._messages_outside_record(record, messages, archive)
            if stray:
                raise RestoreError(
                    f"备份包含 {len(stray)} 条不属于备份 {record.backup_id} 的消息，拒绝恢复"
                )
            return
        # 记录里的 checksum 只覆盖「那次备份新增的消息」。整群恢复（一次全新的
        # 全量备份把库里所有消息都接手过）之后，按库取出的是全群消息，天然不等于
        # 任意单次备份的校验和。能比就比，比不了说明这次记录管不住这批数据，放过。
        if record.checksum and actual != record.checksum:
            if not self._record_owns(record, messages):
                return
            raise RestoreError("消息校验和不一致，备份可能已损坏")

    def _record_owns(self, record, messages: list[Message]) -> bool:
        """库里这批消息是不是全归这条备份记录管。

        只要有一条消息的归属不是它，就说明消息集比这条记录大，校验和对不上是
        预期之内的事，不能拿来判断「库被改过」。一条消息都不属于它，则更没得比。
        """
        owners = {
            self.db.message_backup_id(m.message_id, m.group_id)
            for m in messages
            if self.db.message_exists(m.message_id, m.group_id)
        }
        if not owners:
            return False
        return owners == {record.backup_id}

    def _messages_outside_record(self, record, messages: list[Message], archive) -> list[str]:
        """包里哪些消息不属于这条记录。

        证据只有本地库和记录本身：

        - 库里认得、归属却不是这条记录的（同群「实时归档」除外——`export latest`
          的包本来就含它们）是夹带的；
        - 库里不认识的，只有在记录名下的消息已经全在包里时才算夹带：否则那批陌生
          消息对应的正是记录里那些消息（换台机器恢复就长这样）。

        包里的 manifest 谁都能改，不拿它当证据。判断不了就放行，恢复前的二次确认
        仍然是那道真正的人肉闸门。
        """
        realtime = self.db.query_one(
            """SELECT COUNT(*) AS c FROM messages WHERE group_id = ? AND backup_id = ?""",
            (record.group_id, REALTIME_BACKUP_ID),
        )
        allowed = {record.backup_id}
        if realtime and realtime["c"] > 0:
            allowed.add(REALTIME_BACKUP_ID)
        stray = [
            m.message_id
            for m in messages
            if self.db.message_exists(m.message_id, m.group_id)
            and self.db.message_backup_id(m.message_id, m.group_id) not in allowed
        ]
        if stray:
            return stray

        # 此外，记录里有的消息就是这条备份的全部：包里出现本机不认识、又不在记录
        # 里的消息时，只有当记录名下这些消息「已经都在包里」才可以说是夹带。
        # 反过来说，换机器恢复时记录名下的消息一条都不在包里，那批陌生消息对应的
        # 正是它们，不能算夹带。
        record_ids = self._record_message_ids(record)
        if not record_ids:
            return []
        known_in_pack = {
            m.message_id for m in messages if self.db.message_exists(m.message_id, m.group_id)
        }
        strangers = [m.message_id for m in messages if m.message_id not in known_in_pack]
        if strangers and record_ids <= known_in_pack:
            return strangers
        return []

    def _record_message_ids(self, record) -> set[str]:
        """这条记录名下、本机库里确实有的消息 ID。"""
        return {
            row["message_id"]
            for row in self.db.query(
                "SELECT message_id FROM messages WHERE group_id = ? AND backup_id = ?",
                (record.group_id, record.backup_id),
            )
        }

    # ---------- 记录与快照补齐 ----------
    def _source_group_id(self, record, manifest: dict) -> Optional[int]:
        """这份备份是给哪个群备的：包清单 → 本地记录。"""
        raw = (manifest or {}).get("group_id")
        if raw:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        return record.group_id if record is not None else None

    def _ensure_record(self, backup_id: str, target: int, messages: list, manifest: dict) -> None:
        """本地没有这条记录就按包清单补一条，让后面的 list/export/diff 认得它。"""
        if self.db.get_record(backup_id) is not None:
            return
        from .models import BackupRecord, BackupStatus

        raw = (manifest or {}).get("record") or {}
        started = self._parse_dt(raw.get("started_at")) or datetime.now()
        self.db.save_record(
            BackupRecord(
                backup_id=backup_id,
                group_id=target,
                status=BackupStatus.COMPLETED,
                started_at=started,
                finished_at=self._parse_dt(raw.get("finished_at")) or datetime.now(),
                message_count=len(messages),
                total_size=0,
                checksum=(manifest or {}).get("checksum") or compute_checksum(messages),
                error="由备份包恢复时重建的记录",
            )
        )
        logger.info("按备份包重建记录 %s → 群 %s", backup_id, target)

    def _ensure_snapshot(self, backup_id: str, target: int, manifest: dict) -> None:
        """没有快照就用包清单里的群信息/成员补一份。"""
        if self.db.get_bundle(backup_id, target):
            return
        info = group_info_from_dict((manifest or {}).get("group_info") or {}, target)
        members = members_from_list((manifest or {}).get("members") or [], target)
        if info is None and not members:
            return
        self.db.save_bundle(backup_id, target, info, members)

    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=None)
        except ValueError:
            return None

    def _load_messages(
        self, backup_id: str, archive: Optional[str | Path], source_group: Optional[int] = None
    ) -> list[Message]:
        """取要恢复的消息。

        archive 为空时不再直接按 `messages.backup_id` 取——那个归属会被后续的全量
        备份接手，老备份名下就空了。先查来源清单，清单没有（升级前的老库）才退回
        按归属取。
        """
        if archive is None:
            messages = self.db.get_source_messages(backup_id, source_group)
            if not messages:
                messages = self.db.get_messages(backup_id=backup_id, group_id=source_group)
            return messages

        path = Path(archive)
        if not zipfile.is_zipfile(path):
            raise RestoreError("不是合法的 ZIP 备份包")
        messages: list[Message] = []
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "messages.json" in names:
                raw = json.loads(zf.read("messages.json").decode("utf-8"))
                if isinstance(raw, dict):
                    raw = raw.get("messages", [])
                return [parse_message(m, source_group) for m in raw]
            # ZIP 导出把 JSON 放在 json/daily/ 下，这里按同样路径取
            for name in sorted(names):
                if name.startswith("json/daily/") and name.endswith(".json"):
                    for m in json.loads(zf.read(name).decode("utf-8")):
                        messages.append(parse_message(m, source_group))
        if not messages:
            raise RestoreError("备份包内没有可恢复的消息")
        return messages
