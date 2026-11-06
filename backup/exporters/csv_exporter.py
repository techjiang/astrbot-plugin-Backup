"""CSV 导出：Excel 可直接打开。加 UTF-8 BOM 防中文乱码。"""

import csv
from pathlib import Path

from .base import BaseExporter, ExportContext, display_name, failed_url, media_href


class CsvExporter(BaseExporter):
    extension = ".csv"
    format_name = "csv"

    HEADER = ["时间", "发送者ID", "发送者", "类型", "内容", "引用", "媒体文件"]

    def export(self, ctx: ExportContext, output_path: str | Path) -> Path:
        path = self.ensure_path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(ctx.messages, key=lambda m: (m.timestamp, str(m.message_id)))
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(self.HEADER)
            for m in ordered:
                writer.writerow(
                    [
                        self.fmt_time(m.timestamp),
                        m.sender_id,
                        m.sender_name,
                        m.message_type.value,
                        (m.content or "").replace("\r", " "),
                        m.reply_to or "",
                        ";".join(self._cell(x, ctx) for x in m.media_paths if x),
                    ]
                )
        return path

    @classmethod
    def _cell(cls, media: str, ctx: ExportContext) -> str:
        """媒体单元格。

        原来直接写 `media_paths` 原值（导出机上的绝对路径）。这份 CSV 打开在
        别人的 Excel 里，那一列指的是一台不存在的机器上的文件。

        包内相对地址优先；没有就只留文件名——一列「文件名」至少还能在包里的
        `media/` 下按名字找回来。下载失败的留原始 URL。
        """
        failed = failed_url(media)
        if failed:
            return f"{display_name(media, ctx)}（下载失败：{failed}）"
        return media_href(media, ctx) or display_name(media, ctx)
