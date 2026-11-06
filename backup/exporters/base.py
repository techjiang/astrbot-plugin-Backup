"""导出器基类。"""

import json
from pathlib import Path as _Path
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


def media_member_name(message_id: str, media: str) -> str:
    """媒体在 ZIP 包里的成员名：`media/<日期目录>/<message_id>_<文件名>`。

    导出和恢复必须用**同一个函数**算出这个名字。两边各写一遍是这类 bug 的老家：
    导出端把文件按 A 命名塞进包，恢复端按 B 去包里找，找不到就静默跳过——
    包里有文件、库里没有路径，看着像「恢复成功」。
    """
    path = _Path(str(media or ""))
    name = path.name
    # 幂等：从包里恢复出来的文件本来就叫 `<message_id>_<原名>`，再拼一次就成了
    # `m2_m2_m2_photo.png`，恢复→导出→恢复每转一圈长一截。判断「已经带过前缀」
    # 就够了，不必知道最初的文件名是什么。
    if message_id and not name.startswith(f"{message_id}_"):
        name = f"{message_id}_{name}"
    return f"media/{path.parent.name or 'unknown'}/{name}"


def media_href(media: str, ctx: Optional[ExportContext], prefix: str = "") -> str:
    """媒体在**这份产物里**可点得动的地址；没有就返回空串。

    `ctx.media_links` 只在 ZIP 导出时才有值，里面是包内相对路径。`prefix` 是
    「从这份文件所在目录走回包根」的那一段——`chat.html` 在包根所以是空，
    `markdown/<日期>.md` 在子目录里所以是 `../`。少了这一段，markdown 里的图片
    会解析成 `markdown/media/...`，一样是死链。

    返回空串表示「这里给不出可用地址」，调用方要显示文件名并说明省略原因，
    绝不能把导出机的绝对路径当链接写出去。
    """
    if ctx is None or not ctx.media_links:
        return ""
    link = ctx.media_links.get(media) or ctx.media_links.get(str(media)) or ""
    return f"{prefix}{link}" if link else ""


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

    # ---------- 媒体引用 ----------
    media_href = staticmethod(media_href)
    media_member_name = staticmethod(media_member_name)

    @classmethod
    def dumps(cls, obj) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)


FAILED_MARK = "[下载失败]"

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def is_image(media: str) -> bool:
    return _Path(str(media or "").split("?")[0]).suffix.lower() in _IMAGE_SUFFIXES


def failed_url(media: str) -> Optional[str]:
    """媒体字符串是不是「下载失败」的标记；是就返回它记着的原始 URL。"""
    text = str(media or "")
    if text.startswith(FAILED_MARK):
        return text[len(FAILED_MARK):].strip() or None
    return None


def display_name(media: str, ctx: Optional[ExportContext] = None) -> str:
    """给人看的文件名。

    路径可能是本机绝对路径、包内相对路径、或者一个原始 URL。三种都只取最后一段，
    且不能因为取不到就把井号/问号带出来（`http://x/a.png?sign=...` 会变成
    `a.png?sign=...`，导出产物里很难看）。
    """
    if ctx is not None and ctx.media_index:
        hit = ctx.media_index.get(media) or ctx.media_index.get(str(media))
        if hit:
            return _Path(hit).name
    failed = failed_url(media)
    if failed:
        media = failed
    text = str(media or "")
    base = text.split("?", 1)[0].split("#", 1)[0].rstrip("/").replace("\\", "/")
    return base.rsplit("/", 1)[-1] or text or "附件"
