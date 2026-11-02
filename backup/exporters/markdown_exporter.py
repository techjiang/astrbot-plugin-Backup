"""Markdown 导出：按天分文件，适合归档阅读。"""

from pathlib import Path

from .base import BaseExporter, ExportContext


class MarkdownExporter(BaseExporter):
    extension = ".md"
    format_name = "markdown"

    def export(self, ctx: ExportContext, output_path: str | Path) -> Path:
        out_dir = self.ensure_path(output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        days = self.group_by_date(ctx.messages)

        parts = [
            f"# {ctx.group_name} 聊天记录",
            "",
            f"- 群号：{ctx.group_id}",
            f"- 备份 ID：`{ctx.backup_id}`",
            f"- 消息总数：{len(ctx.messages)}",
            f"- 消息天数：{len(days)}",
            "",
            "## 目录",
            "",
        ]
        for day, messages in days:
            parts.append(f"- [{day}](./{day}.md)（{len(messages)} 条）")
        (out_dir / "README.md").write_text("\n".join(parts) + "\n", encoding="utf-8")

        for day, messages in days:
            lines = [f"# {day}", ""]
            for m in messages:
                time_str = self.fmt_time(m.timestamp, with_date=False)
                lines.append(f"**{m.sender_name or m.sender_id}** · {time_str}")
                lines.append("")
                if m.reply_to:
                    lines.append(f"> 引用 `{m.reply_to}`")
                    lines.append("")
                if m.content:
                    lines.append(m.content)
                    lines.append("")
                for media in m.media_paths:
                    if not media:
                        continue
                    if media.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        lines.append(f"![图片]({media})")
                    else:
                        lines.append(f"[附件]({media})")
                    lines.append("")
                lines.append("---")
                lines.append("")
            (out_dir / f"{day}.md").write_text("\n".join(lines), encoding="utf-8")
        return out_dir
