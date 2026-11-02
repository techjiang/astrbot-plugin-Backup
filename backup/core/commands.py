"""命令层：解析参数、拼回复文案。与 AstrBot 解耦，便于单测。"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .database import Database

SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}

HELP_TEXT = """**Backup 高级备份** · 命令速查

`/backup help` 帮助
`/backup now [群号]` 立即备份
`/backup schedule set <群号> "<cron>"` 设置定时备份
`/backup schedule list` 查看定时任务
`/backup schedule remove <群号>` 移除定时任务
`/backup list [群号]` 备份列表
`/backup export [群号] <格式>` 导出（sqlite/json/csv/txt/markdown/html/zip）
`/backup restore <备份ID> [目标群号] [--strategy skip|overwrite|merge] --confirm` 恢复
`/backup status [群号]` 当前群备份状态
`/backup config get|set <键> [值]` 配置查看/修改
`/backup clean [天数]` 清理旧备份
`/backup verify <备份ID>` 校验完整性
`/backup diff <备份ID1> <备份ID2>` 对比差异"""


def human_size(size: int) -> str:
    num = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}GB"


def parse_group_arg(token: Optional[str]) -> Optional[int]:
    if token and re.fullmatch(r"-?\d{5,20}", token.strip()):
        return int(token.strip())
    return None


@dataclass
class Permission:
    allowed: bool
    reason: str = ""


class PermissionChecker:
    """仅群主/管理员可执行备份操作（可配置关闭）。"""

    def __init__(self, require_admin: bool = True):
        self.require_admin = require_admin

    def check_event(self, event) -> Permission:
        if not self.require_admin:
            return Permission(True)
        for name in ("is_admin", "is_group_admin", "is_owner"):
            fn = getattr(event, name, None)
            if callable(fn):
                try:
                    if fn():
                        return Permission(True)
                except Exception:
                    pass
            elif isinstance(fn, bool) and fn:
                return Permission(True)
        role = getattr(event, "sender_role", None) or getattr(event, "role", None)
        if isinstance(role, str) and role.lower() in ("admin", "owner", "administrator"):
            return Permission(True)
        return Permission(False, "仅群主或管理员可执行备份操作")


def format_record_line(record) -> str:
    base = "<增量>" if record.is_incremental else "<全量>"
    return (
        f"`{record.backup_id}` {base} {record.message_count} 条 / "
        f"{human_size(record.total_size)} / {record.status.value}"
    )


def render_backup_list(db: Database, group_id: Optional[int] = None, limit: int = 10) -> str:
    records = db.list_records(group_id, limit=limit)
    if not records:
        return "还没有任何备份记录。"
    head = f"最近的 {len(records)} 条备份："
    return "\n".join([head] + [f"- {format_record_line(r)}" for r in records])


def render_status(db: Database, group_id: int) -> str:
    info = db.get_group_info(group_id)
    record = db.latest_completed_record(group_id)
    members = db.get_members(group_id)
    lines = [f"**群 {info.group_name if info else group_id}**（{group_id}）"]
    lines.append(f"消息总数：{db.count_messages(group_id)}")
    lines.append(f"成员数：{len(members)}")
    if record:
        lines.append(f"最近备份：{record.started_at:%Y-%m-%d %H:%M:%S}")
        lines.append(f"消息数：{record.message_count} · 媒体数：{record.media_count}")
        lines.append(f"大小：{human_size(record.total_size)}")
    else:
        lines.append("还没有完成过备份")
    return "\n".join(lines)


def render_diff(db: Database, id1: str, id2: str) -> str:
    r1, r2 = db.get_record(id1), db.get_record(id2)
    if not r1 or not r2:
        return "备份 ID 不存在，无法对比。"
    m1 = {m.message_id for m in db.get_messages(backup_id=id1)}
    m2 = {m.message_id for m in db.get_messages(backup_id=id2)}
    only1, only2 = m1 - m2, m2 - m1
    return (
        f"**备份对比**\n- `{id1}`：{len(m1)} 条\n- `{id2}`：{len(m2)} 条\n"
        f"- 仅在 {id1}：{len(only1)} 条\n- 仅在 {id2}：{len(only2)} 条\n"
        f"- 相同：{len(m1 & m2)} 条"
    )


def render_progress(done: int, total_hint: int = 0) -> str:
    if total_hint:
        ratio = min(done / max(total_hint, 1), 1.0)
        bars = int(ratio * 10)
        return f"[{'█' * bars}{'░' * (10 - bars)}] {done}/{total_hint}"
    return f"已备份 {done} 条…"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"


def retention_cutoff(days: int) -> datetime:
    return datetime.now() - timedelta(days=days)
