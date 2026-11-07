"""纯文本导出：每行 [时间] 昵称: 内容。"""

from pathlib import Path

from .base import BaseExporter, ExportContext, display_name, failed_url, media_href


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
                if not media:
                    continue
                lines.append(f"    {self._media_line(media, ctx)}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @classmethod
    def _media_line(cls, media: str, ctx: ExportContext) -> str:
        """一行附件。

        原来是 `<附件> {media_paths 原值}`——那串是导出机上的绝对路径，
        `/root/.astrbot-backup/media/20240501/x.pdf`。在别人机器上打开这份 txt，
        照着这行去找文件，永远找不到；而文件就在同一个包里的 `media/` 下。

        所以有包内地址就写包内地址，没有就只写文件名并说明为什么点不动。
        下载失败的媒体留原始 URL——那是唯一还能追的线索。
        """
        failed = failed_url(media)
        if failed:
            return f"<附件> {display_name(media, ctx)}（下载失败，原始链接：{failed}）"
        href = media_href(media, ctx)
        if href:
            return f"<附件> {href}"
        return f"<附件> {display_name(media, ctx)}（未随本包导出）"
