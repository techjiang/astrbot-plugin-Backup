"""CSV 导出：Excel 可直接打开。加 UTF-8 BOM 防中文乱码。"""

import csv
from pathlib import Path

from .base import BaseExporter, ExportContext


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
                        ";".join(p for p in m.media_paths if p),
                    ]
                )
        return path
