"""导出器注册表。"""

from .base import BaseExporter, ExportContext
from .csv_exporter import CsvExporter
from .html_exporter import HtmlExporter
from .json_exporter import JsonExporter
from .markdown_exporter import MarkdownExporter
from .sqlite_exporter import SqliteExporter
from .txt_exporter import TxtExporter

EXPORTERS = {
    "sqlite": SqliteExporter,
    "json": JsonExporter,
    "csv": CsvExporter,
    "txt": TxtExporter,
    "markdown": MarkdownExporter,
    "md": MarkdownExporter,
    "html": HtmlExporter,
}

SUPPORTED_FORMATS = ["sqlite", "json", "csv", "txt", "markdown", "html", "zip"]


def get_exporter(fmt: str):
    cls = EXPORTERS.get(fmt.lower())
    if cls is None:
        raise ValueError(f"不支持的导出格式: {fmt}")
    return cls()


__all__ = [
    "BaseExporter",
    "ExportContext",
    "EXPORTERS",
    "SUPPORTED_FORMATS",
    "get_exporter",
    "SqliteExporter",
    "JsonExporter",
    "CsvExporter",
    "TxtExporter",
    "MarkdownExporter",
    "HtmlExporter",
]
