"""纯文本导出：每行 [时间] 昵称: 内容。"""

from pathlib import Path

from .base import BaseExporter, ExportContext


class TxtExporter(BaseExporter):
    extension = ".txt"
    format_name = "txt"

    def export(self, ctx: ExportContext, output_path: str | Path) -> Path:
        path = self.ensure_path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {ctx.group_name}（{ctx.group_id}）聊天记录", f"# 共 {len(ctx.messages)} 条消息", ""]
        current_day = None
        for m in sorted(ctx.messages, key=lambda x: (x.timestamp, str(x.message_id))):
            day = m.timestamp.strftime("%Y-%m-%d")
            if day != current_day:
                lines.append(f"===== {day} =====")
                current_day = day
            content = (m.content or "").replace("\n", " ⏎ ")
            lines.append(f"[{self.fmt_time(m.timestamp)}] {m.sender_name or m.sender_id}: {content}")
            for media in m.media_paths:
                if media:
                    lines.append(f"    <附件> {media}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
