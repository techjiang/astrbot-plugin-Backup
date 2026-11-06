"""导出器基类。"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..core.models import GroupInfo, GroupMember, Message


@dataclass
class ExportContext:
    messages: list[Message]
    group_info: Optional[GroupInfo] = None
    members: list[GroupMember] = field(default_factory=list)
    media_index: dict[str, Path] = field(default_factory=dict)
    # 媒体路径 → **可点得动的地址**。ZIP 包里是包内相对路径（`media/20240501/x.png`），
    # 单文件导出时为空。空着不代表「没媒体」，只代表「这里没有可引用的地址」——
    # 那时候导出器只能内联、或者明说省略，不能把导出机的绝对路径当链接写进 HTML。
    media_links: dict[str, str] = field(default_factory=dict)
    backup_id: str = ""
    # 导出前的消息集校验和。导出器拼出来的清单要跟它一致，ZIP 包才恢复得回去
    checksum: str = ""
    # 群信息 + 成员表的「可信快照」，写进 ZIP 清单供恢复端比对；见 verifier.diff_snapshot
    trusted: dict = field(default_factory=dict)

    @property
    def group_name(self) -> str:
        return self.group_info.group_name if self.group_info and self.group_info.group_name else "群聊"

    @property
    def group_id(self) -> int:
        if self.group_info:
            return self.group_info.group_id
        return self.messages[0].group_id if self.messages else 0


class BaseExporter:
    """所有导出器实现 export(ctx, output_path)。"""

    extension = ".txt"
    format_name = "base"

    def export(self, ctx: ExportContext, output_path: str | Path) -> Path:
        raise NotImplementedError

    # ---------- 公用小工具 ----------
    @staticmethod
    def ensure_path(output_path: str | Path) -> Path:
        return Path(output_path)

    @staticmethod
    def fmt_time(dt: datetime, with_date: bool = True) -> str:
        return dt.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")

    @staticmethod
    def participants(messages: list[Message]) -> list[dict]:
        stats: dict[int, dict] = {}
        for m in messages:
            row = stats.setdefault(
                m.sender_id,
                {
                    "user_id": m.sender_id,
                    "name": m.sender_name or str(m.sender_id),
                    "count": 0,
                    "first": m.timestamp,
                    "last": m.timestamp,
                },
            )
            row["count"] += 1
            row["first"] = min(row["first"], m.timestamp)
            row["last"] = max(row["last"], m.timestamp)
        return sorted(stats.values(), key=lambda r: -r["count"])

    @staticmethod
    def group_by_date(messages: list[Message]) -> list[tuple[str, list[Message]]]:
        buckets: dict[str, list[Message]] = {}
        for m in sorted(messages, key=lambda x: (x.timestamp, str(x.message_id))):
            buckets.setdefault(m.timestamp.strftime("%Y-%m-%d"), []).append(m)
        return list(buckets.items())

    @staticmethod
    def message_to_dict(m: Message) -> dict:
        return {
            "message_id": m.message_id,
            "group_id": m.group_id,
            "sender_id": m.sender_id,
            "sender_name": m.sender_name,
            "timestamp": m.timestamp.isoformat() if m.timestamp else "",
            "message_type": m.message_type.value,
            "content": m.content,
            "reply_to": m.reply_to,
            "media_paths": m.media_paths,
            "is_forward": m.is_forward,
            "forward_chain": m.forward_chain,
        }

    @classmethod
    def dumps(cls, obj) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)
