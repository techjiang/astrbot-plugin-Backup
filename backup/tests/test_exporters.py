import json
import zipfile
from pathlib import Path

import pytest

from backup.core.export_manager import ExportManager, parse_expire
from backup.core.models import GroupInfo, GroupMember
from backup.exporters import get_exporter
from backup.exporters.base import ExportContext
from backup.exporters.html_exporter import HtmlExporter

from .conftest import make_message


def ctx(messages=None):
    messages = messages or [make_message(i) for i in range(3)]
    return ExportContext(
        messages=messages,
        group_info=GroupInfo(group_id=100, group_name="测试群", member_count=3),
        members=[GroupMember(user_id=1, group_id=100, nickname="甲")],
        backup_id="bk_test",
    )


class TestBaseExporters:
    @pytest.mark.parametrize(
        "fmt,expected",
        [("csv", ".csv"), ("txt", ".txt"), ("sqlite", ".sqlite")],
    )
    def test_single_file_formats(self, fmt, expected, tmp_path):
        path = get_exporter(fmt).export(ctx(), tmp_path / f"out{expected}")
        assert path.is_file() and path.stat().st_size > 0
        assert path.suffix == expected

    def test_csv_has_bom_and_rows(self, tmp_path):
        path = get_exporter("csv").export(ctx(), tmp_path / "out.csv")
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert len(raw.decode("utf-8-sig").strip().splitlines()) == 4

    def test_txt_line_format(self, tmp_path):
        path = get_exporter("txt").export(ctx(), tmp_path / "out.txt")
        text = path.read_text(encoding="utf-8")
        assert "[2024-05-01 10:00:00] 用户1: 消息内容 0" in text

    def test_json_shards_by_date(self, tmp_path):
        out = get_exporter("json").export(ctx(), tmp_path / "jsonout")
        index = json.loads((out / "index.json").read_text(encoding="utf-8"))
        assert index["total_messages"] == 3
        assert index["shards"][0]["date"] == "2024-05-01"
        assert (out / index["shards"][0]["file"]).is_file()

    def test_markdown_daily_files(self, tmp_path):
        out = get_exporter("markdown").export(ctx(), tmp_path / "mdout")
        assert (out / "2024-05-01.md").is_file()
        assert "聊天记录" in (out / "README.md").read_text(encoding="utf-8")

    def test_sqlite_is_queryable(self, tmp_path):
        import sqlite3

        path = get_exporter("sqlite").export(ctx(), tmp_path / "out.sqlite")
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        conn.close()

    def test_unknown_format_rejected(self):
        with pytest.raises(ValueError):
            get_exporter("pdf")


class TestHtmlExporter:
    def test_generates_valid_html(self, tmp_path):
        html = HtmlExporter().render(ctx())
        assert html.lstrip().startswith("<!DOCTYPE html>")
        assert html.count("<html") == 1 and "</html>" in html
        assert "测试群" in html
        assert "{{" not in html  # 占位符全部替换

    def test_groups_messages_by_date(self):
        day2 = make_message(9)
        day2.timestamp = day2.timestamp.replace(day=2)
        html = HtmlExporter().render(ctx([make_message(0), day2]))
        assert 'id="day-2024-05-01"' in html
        assert 'id="day-2024-05-02"' in html

    def test_includes_media_images(self, tmp_path):
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        msg = make_message(0)
        msg.media_paths = [str(img)]
        html = HtmlExporter().render(ctx([msg]))
        assert "data:image/png;base64," in html

    def test_single_file_output(self, tmp_path):
        path = HtmlExporter().export(ctx(), tmp_path / "out.html")
        text = path.read_text(encoding="utf-8")
        assert len(list(tmp_path.iterdir())) == 1
        assert "<link" not in text and "<script src" not in text
        assert "科技酱" in text and "www.asoe.cn" in text

    def test_escapes_html_injection(self, tmp_path):
        msg = make_message(0, content="<script>alert(1)</script>")
        html = HtmlExporter().render(ctx([msg]))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestExportManager:
    def test_token_link_expires(self, tmp_path, db):
        for msg in [make_message(i) for i in range(3)]:
            db.save_message(msg, "bk1")
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {"download_link_expire": "1h"}})
        result = manager.export(100, "csv", backup_id="bk1")
        assert result.path.is_file()
        assert manager.resolve_token(result.token) == result.path
        assert manager.resolve_token("bogus") is None

    def test_zip_contains_all_formats(self, tmp_path, db):
        for msg in [make_message(i) for i in range(3)]:
            db.save_message(msg, "bk1")
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        result = manager.export(100, "zip", backup_id="bk1")
        with zipfile.ZipFile(result.path) as zf:
            names = zf.namelist()
            assert "manifest.json" in names
            assert "chat.html" in names
            assert "messages.sqlite" in names
            assert any(n.startswith("json/daily/") for n in names)
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["group_id"] == 100

    def test_export_empty_group_raises(self, tmp_path, db):
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        with pytest.raises(ValueError):
            manager.export(999, "csv")

    def test_parse_expire(self):
        assert parse_expire("24h") == 86400
        assert parse_expire("30m") == 1800
        assert parse_expire("2d") == 172800
        assert parse_expire(300) == 300
