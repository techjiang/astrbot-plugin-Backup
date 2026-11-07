"""配置项与代码行为的契约测试。

一轮排查发现 `config.yaml` 里有 5 个开关写了没人读：群公告、群文件、私聊归档、
默认导出格式、定时总闸。配置项写着「能关」，代码里关不掉，比没有这个配置更糟。
这些用例把开关两头都钉住。
"""

import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from backup.core.auto_trigger import AutoTrigger
from backup.core.backup_engine import BackupEngine, PlatformAdapter
from backup.core.commands import PermissionChecker
from backup.core.export_manager import ExportManager
from backup.core.event_hooks import PRIVATE_ARCHIVE_GROUP, EventHooks
from backup.core.parser import parse_announcements, parse_group_files, parse_message
from backup.core.scheduler import BackupScheduler

from .conftest import make_message


class NotifyPlatform:
    """带群公告和群文件列表的假平台。"""

    def __init__(self, notices=None, files=None, boom=False):
        self.notices = notices or []
        self.files = files or []
        self.boom = boom
        self.calls = []

    async def get_group_info(self, group_id):
        return {"group_id": group_id, "group_name": "契约群"}

    async def get_group_members(self, group_id):
        return []

    async def get_group_history(self, group_id, count=50, offset=0):
        return []

    async def get_group_notice(self, group_id):
        self.calls.append("notice")
        if self.boom:
            raise RuntimeError("接口挂了")
        return {"notices": self.notices}

    async def get_group_files(self, group_id):
        self.calls.append("files")
        if self.boom:
            raise RuntimeError("接口挂了")
        return {"files": self.files}


def run_backup(db, tmp_path, platform, config=None):
    engine = BackupEngine(db, tmp_path / "media", config or {})
    return engine.backup_group(100, platform, incremental=False, download_media=False)


class TestAnnouncementsAndFiles:
    async def test_saves_when_enabled(self, db, tmp_path):
        platform = NotifyPlatform(
            notices=[{"content": "明天维护", "sender": {"nickname": "群主"}}],
            files=[{"file_id": "f1", "file_name": "资料.zip", "file_size": 1024}],
        )
        await run_backup(db, tmp_path, platform)
        assert [a["content"] for a in db.get_announcements(100)] == ["明天维护"]
        assert [f["name"] for f in db.get_group_files(100)] == ["资料.zip"]

    async def test_skips_when_disabled(self, db, tmp_path):
        platform = NotifyPlatform(notices=[{"content": "x"}], files=[{"file_id": "f1"}])
        await run_backup(
            db,
            tmp_path,
            platform,
            config={"scope": {"group_announcements": False, "group_files": False}},
        )
        assert platform.calls == []
        assert db.get_announcements(100) == []
        assert db.get_group_files(100) == []

    async def test_failure_does_not_fail_backup(self, db, tmp_path):
        platform = NotifyPlatform(boom=True)
        record = await run_backup(db, tmp_path, platform)
        assert record.status.value == "completed"
        assert "群公告" in (record.error or "") and "群文件" in (record.error or "")

    def test_rescan_replaces_snapshot(self, db):
        db.save_announcements(100, [{"content": "旧公告", "published_at": None}])
        db.save_announcements(100, [{"content": "新公告", "published_at": None}])
        assert [a["content"] for a in db.get_announcements(100)] == ["新公告"]

    def test_parse_tolerates_odd_shapes(self):
        assert parse_announcements(None, 1) == []
        assert parse_group_files({"data": {"files": [{"file_name": "a.bin"}]}}, 1)[0]["name"] == "a.bin"

    async def test_adapter_probes_alternate_names(self):
        class Plat:
            async def get_group_notices(self, group_id):
                return [1]

        assert await PlatformAdapter(Plat()).get_group_announcements(1) == [1]


class TestPrivateArchive:
    def _dm_event(self):
        class E:
            group_id = None
            message_str = "悄悄话"
            sender_id = 5
            message_obj = {
                "message_id": "dm1",
                "timestamp": int(datetime(2024, 5, 1, 10, 0).timestamp() * 1000),
                "message": [{"type": "plain", "data": {"text": "悄悄话"}}],
            }

        return E()

    def test_off_by_default(self, db):
        hooks = EventHooks(db, AutoTrigger())
        assert hooks.archive_message(self._dm_event()) is None
        assert db.count_messages() == 0

    def test_on_saves_under_virtual_group(self, db):
        hooks = EventHooks(db, AutoTrigger(), archive_private=True)
        msg = hooks.archive_message(self._dm_event())
        assert msg is not None and msg.group_id == PRIVATE_ARCHIVE_GROUP
        assert db.count_messages(PRIVATE_ARCHIVE_GROUP) == 1
        # 虚拟群号是负数，永远不会和真实群号撞
        assert PRIVATE_ARCHIVE_GROUP < 0


class TestPermissionPrivate:
    class Member:
        is_admin = False
        role = "member"

    class Dm:
        message_type = "private"
        is_admin = False

    def test_private_allowed_when_config_open(self):
        assert PermissionChecker(True, True).check_event(self.Dm()).allowed is True

    def test_private_denied_when_config_closed(self):
        assert PermissionChecker(True, False).check_event(self.Dm()).allowed is False

    def test_group_member_still_denied(self):
        assert PermissionChecker(True, True).check_event(self.Member()).allowed is False


class TestSchedulerMasterSwitch:
    def test_disabled_keeps_row_but_schedules_nothing(self, db):
        sched = BackupScheduler(db, lambda gid: None, enabled=False)
        assert sched.add_job(100, "0 3 * * *") is True
        assert [r["group_id"] for r in db.list_schedules()] == [100]
        assert sched.restore_jobs() == 0


class TestExportDefaults:
    def test_default_format_is_used_when_omitted(self, db, tmp_path):
        db.save_message(make_message(0), "bk1")
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
        result = manager.export_latest(100, "sqlite")
        assert result.format == "sqlite"


class TestFlatContentFallback:
    def test_chain_content_not_duplicated(self):
        """`{'content': [...]}` 是消息链形态，不能连着平铺兜底再进一次。"""
        msg = parse_message(
            {"message_id": "c1", "content": [{"type": "plain", "data": {"text": "只一次"}}]}
        )
        assert msg.content == "只一次"

    def test_flat_export_row_still_parsed(self):
        """导出摊平的 dict（content/message_type 在顶层）仍然要能解析回来。"""
        msg = parse_message(
            {"message_id": "c2", "content": "回灌内容", "message_type": "text", "timestamp": 1714000000}
        )
        assert msg.content == "回灌内容"


class TestWatermarkIgnoresRealtime:
    def test_realtime_rows_do_not_set_watermark(self, db):
        db.save_message(make_message(0), "realtime")
        assert db.latest_message_key(100) is None
        db.save_message(make_message(1), "bk1")
        assert db.latest_message_key(100) == (make_message(1).timestamp, "m1")


class TestZipContents:
    def test_announcements_and_files_travel_with_package(self, db, tmp_path):
        db.save_message(make_message(0), "bk1")
        db.save_announcements(100, [{"content": "带走我", "published_at": datetime(2024, 5, 1)}])
        db.save_group_files(100, [{"file_id": "f1", "name": "a.zip", "size": 1}])
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
        result = manager.export(100, "zip", backup_id="bk1")
        with zipfile.ZipFile(result.path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            assert zf.read("announcements.json")
            assert zf.read("group_files.json")
        assert manifest["announcements"][0]["content"] == "带走我"
        assert manifest["group_files"][0]["name"] == "a.zip"


class TestHtmlUsesLocalMedia:
    def test_media_index_recovers_missing_path(self, db, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        local = media_dir / "real.png"
        local.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        msg = make_message(0, mtype=None)
        msg.media_paths = ["/gone/away/real.png"]
        db.save_message(msg, "bk1")
        db.save_media(
            message_id=msg.message_id,
            group_id=msg.group_id,
            original_url="http://x/real.png",
            local_path=str(local),
            filename=local.name,
            file_size=local.stat().st_size,
            mime_type="image/png",
            sha256="0",
        )
        manager = ExportManager(db, tmp_path / "exports", media_dir, {})
        ctx = manager.build_context(100)
        assert ctx.media_index == {"/gone/away/real.png": local}
        html = (tmp_path / "exports" / "out.html")
        from backup.exporters.html_exporter import HtmlExporter

        html.write_text(HtmlExporter().render(ctx), encoding="utf-8")
        assert "data:image/png;base64," in html.read_text(encoding="utf-8")
