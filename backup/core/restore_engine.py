"""恢复引擎。恢复是危险操作：先校验完整性，再按策略写入。"""

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .database import Database
from .models import Message
from .parser import parse_message
from .verifier import compute_checksum, verify_zip


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

    def verify_backup(self, backup_id: str, archive: Optional[str | Path] = None) -> tuple[bool, str]:
        """先查记录：不存在/未完成/校验和不符都拒绝。"""
        record = self.db.get_record(backup_id)
        if record is None:
            raise RestoreError(f"备份 {backup_id} 不存在")
        from .models import BackupStatus

        if record.status not in (BackupStatus.COMPLETED, BackupStatus.PARTIAL):
            raise RestoreError(f"备份 {backup_id} 状态为 {record.status.value}，不可恢复")
        if archive is not None:
            ok, reason = verify_zip(archive, record.checksum or None)
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

        self.verify_backup(backup_id, archive)
        record = self.db.get_record(backup_id)
        target = target_group_id or record.group_id

        messages = self._load_messages(backup_id, archive)
        if messages and record.checksum:
            # 无论消息来自库还是 ZIP 包，都要跟记录里的校验和对上。
            # 只在 archive 为空时校验等于放行整个 ZIP 路径：包的 manifest 只要自洽，
            # 内容被换过也能一路写进目标群。
            actual = compute_checksum(messages)
            if actual != record.checksum:
                raise RestoreError("消息校验和不一致，备份可能已损坏")

        inserted = skipped = overwritten = 0
        for msg in messages:
            msg.group_id = target
            exists = self.db.message_exists(msg.message_id, target)
            if exists:
                if strategy == "skip":
                    skipped += 1
                    continue
                if strategy == "overwrite":
                    self.db.delete_messages(target, [msg.message_id])
                    overwritten += 1
                # merge：保留原有，仅补齐缺失字段（这里等同跳过，但不计数为 skip）
            if self.db.save_message(msg, backup_id):
                inserted += 1
            elif not exists:
                # 已存在（含 merge 策略）：补上这次恢复的归属，否则导出按备份取数会看不到它
                self.db.assign_message_backup(msg.message_id, target, backup_id)

        return RestoreResult(
            backup_id=backup_id,
            target_group_id=target,
            inserted=inserted,
            skipped=skipped,
            overwritten=overwritten,
            checksum_ok=True,
            message="恢复完成",
        )

    def _load_messages(self, backup_id: str, archive: Optional[str | Path]) -> list[Message]:
        if archive is None:
            return self.db.get_messages(backup_id=backup_id)

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
                return [parse_message(m) for m in raw]
            # ZIP 导出把 JSON 放在 json/daily/ 下，这里按同样路径取
            for name in sorted(names):
                if name.startswith("json/daily/") and name.endswith(".json"):
                    for m in json.loads(zf.read(name).decode("utf-8")):
                        messages.append(parse_message(m))
        if not messages:
            raise RestoreError("备份包内没有可恢复的消息")
        return messages
