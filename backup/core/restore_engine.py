"""恢复引擎。恢复是危险操作：先校验完整性，再按策略写入。"""

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .database import Database
from .models import Message
from .parser import parse_message
from .verifier import compute_checksum, read_manifest, verify_zip


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
        """先查记录：不存在/未完成都拒绝；给了备份包就连包一起查。

        包的校验和只跟包自己的 manifest 比。拿备份记录里的校验和去比会误伤
        `export latest` 的包——那个包含实时归档与更早的数据，天然不等于记录里
        「本次新增」的那一撮。
        """
        record = self.db.get_record(backup_id)
        if record is None:
            raise RestoreError(f"备份 {backup_id} 不存在")
        from .models import BackupStatus

        if record.status not in (BackupStatus.COMPLETED, BackupStatus.PARTIAL):
            raise RestoreError(f"备份 {backup_id} 状态为 {record.status.value}，不可恢复")
        if archive is not None:
            ok, reason = verify_zip(archive)
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
        self._check_content(record, messages, archive)

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
        if archive is not None:
            declared = (read_manifest(archive) or {}).get("checksum")
            if declared and actual != declared:
                raise RestoreError("备份包内容与其清单不一致，备份可能已损坏")
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
        owners = {self.db.message_backup_id(m.message_id, m.group_id) for m in messages}
        return owners == {record.backup_id}

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
