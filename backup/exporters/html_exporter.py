"""HTML 导出：自包含单文件，仿聊天界面，含内嵌 SVG logo 与作者信息。"""

import base64
import html
import mimetypes
from pathlib import Path
from typing import Optional

from .base import BaseExporter, ExportContext

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "chat.html"
MAX_INLINE_BYTES = 6 * 1024 * 1024  # 超过 6MB 的媒体不内联，只留路径

_TYPE_LABEL = {
    "image": "[图片]",
    "voice": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[合并转发]",
}


class HtmlExporter(BaseExporter):
    extension = ".html"
    format_name = "html"

    def __init__(self, template_path: Optional[Path] = None, own_user_id: Optional[int] = None):
        self.template_path = Path(template_path) if template_path else TEMPLATE_PATH
        # 导出者视角：没有自己的 user_id 时全部靠左（对方），避免乱猜
        self.own_user_id = own_user_id

    def export(self, ctx: ExportContext, output_path: str | Path) -> Path:
        path = self.ensure_path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(ctx), encoding="utf-8")
        return path

    def render(self, ctx: ExportContext) -> str:
        template = self.template_path.read_text(encoding="utf-8")
        messages = sorted(ctx.messages, key=lambda m: (m.timestamp, str(m.message_id)))
        days = self.group_by_date(messages)
        participants = self.participants(messages)

        body_parts: list[str] = []
        for day, day_messages in days:
            body_parts.append(
                f'<div class="day" id="day-{html.escape(day)}">{html.escape(day)} · {len(day_messages)} 条</div>'
            )
            for m in day_messages:
                body_parts.append(self._render_message(m, ctx))

        time_range = ""
        if messages:
            time_range = (
                f"{messages[0].timestamp:%Y-%m-%d %H:%M} ~ {messages[-1].timestamp:%Y-%m-%d %H:%M}"
            )

        replacements = {
            "GROUP_NAME": html.escape(ctx.group_name),
            "GROUP_ID": str(ctx.group_id),
            "MEMBER_COUNT": str(len(ctx.members)),
            "MESSAGE_COUNT": str(len(messages)),
            "TIME_RANGE": html.escape(time_range),
            "SENDER_OPTIONS": "".join(
                f'<option value="{p["user_id"]}">{html.escape(p["name"])}（{p["count"]}）</option>'
                for p in participants
            ),
            "DAY_NAV": "".join(
                f'<a href="#day-{html.escape(day)}">{html.escape(day)}<span>{len(msgs)}</span></a>'
                for day, msgs in days
            ),
            "MESSAGES_HTML": "\n".join(body_parts) or '<div class="empty">该备份没有任何消息</div>',
        }
        out = template
        for key, value in replacements.items():
            out = out.replace("{{" + key + "}}", value)
        return out

    # ---------- 单条消息 ----------
    def _render_message(self, m, ctx: ExportContext) -> str:
        mine = self.own_user_id is not None and m.sender_id == self.own_user_id
        initial = html.escape((m.sender_name or str(m.sender_id))[:1].upper())
        text_for_search = html.escape((m.content or "").lower())

        bubble_parts: list[str] = []
        if m.reply_to:
            bubble_parts.append(f'<div class="quote">引用 {html.escape(str(m.reply_to))}</div>')
        if m.content:
            bubble_parts.append(html.escape(m.content))
        elif m.message_type.value in _TYPE_LABEL:
            bubble_parts.append(_TYPE_LABEL[m.message_type.value])

        if m.is_forward and m.forward_chain:
            nodes = "".join(
                f'<div class="node"><strong>{html.escape(str(n.get("sender_name", "")))}</strong>：'
                f'{html.escape(str(n.get("content", "")))}</div>'
                for n in m.forward_chain
            )
            bubble_parts.append(
                f'<details class="forward"><summary>合并转发 {len(m.forward_chain)} 条</summary>{nodes}</details>'
            )

        for media in m.media_paths:
            bubble_parts.append(self._render_media(media))

        return (
            f'<div class="msg{" mine" if mine else ""}" data-sender="{m.sender_id}" data-text="{text_for_search}">'
            f'<div class="avatar">{initial}</div>'
            f'<div class="body"><div class="who"><span>{html.escape(m.sender_name or str(m.sender_id))}</span>'
            f'<span>{m.timestamp:%H:%M:%S}</span></div>'
            f'<div class="bubble">{"".join(bubble_parts)}</div></div></div>'
        )

    def _render_media(self, media: str) -> str:
        if not media:
            return ""
        p = Path(media)
        is_image = p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        data_uri = self._inline(p) if is_image else None
        if data_uri:
            return f'<img class="media" src="{data_uri}" alt="{html.escape(p.name)}" loading="lazy">'
        if is_image:
            return f'<a class="file" href="{html.escape(media)}" target="_blank" rel="noopener">🖼 {html.escape(p.name)}</a>'
        return f'<a class="file" href="{html.escape(media)}" target="_blank" rel="noopener">📎 {html.escape(p.name)}</a>'

    @staticmethod
    def _inline(path: Path) -> Optional[str]:
        try:
            if not path.is_file() or path.stat().st_size > MAX_INLINE_BYTES:
                return None
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{b64}"
        except OSError:
            return None
