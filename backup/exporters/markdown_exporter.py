"""Markdown 导出：按天分文件，适合归档阅读。"""

from pathlib import Path

from .base import BaseExporter, ExportContext, display_name, failed_url, is_image, media_href


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
                    lines.append(self._media_line(media, ctx))
                    lines.append("")
                lines.append("---")
                lines.append("")
            (out_dir / f"{day}.md").write_text("\n".join(lines), encoding="utf-8")
        return out_dir

    @classmethod
    def _media_line(cls, media: str, ctx: ExportContext) -> str:
        """一行媒体引用。

        原文直接写 `![图片](/root/.astrbot-backup/media/20240501/x.png)`——导出机的
        绝对路径。在别人的机器上打开这份 markdown，图片全是破图，链接点开是
        「文件不存在」，而文件就躺在同一个包的 `media/` 里。

        这份文件在 `<包根>/markdown/` 下，所以回包根要走一层 `../`。少了这一层，
        地址会被解析成 `markdown/media/...`，仍然是死链。
        """
        failed = failed_url(media)
        name = display_name(media, ctx)
        if failed:
            return f"[{name}（下载失败）]({failed})"
        href = media_href(media, ctx, prefix="../")
        if href:
            return f"![{name}]({href})" if is_image(media) else f"[{name}]({href})"
        return f"{name}（未随本包导出）"
