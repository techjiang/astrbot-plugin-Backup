"""四个「跑起来才会撞上」的问题：ZIP 恢复被误拒、平台忽略 offset 死循环、
媒体表重复插、cron 写了非法值却放行。"""

import json
import zipfile

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.export_manager import ExportManager
from backup.core.models import BackupStatus
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.scheduler import validate_cron
from backup.core.verifier import compute_checksum

from .conftest import FakePlatform, make_message, raw_history


class TestExportLatestRestore:
    """`export latest` 的包含实时归档，校验和天然不等于备份记录里的那个。"""

    async def _seeded(self, db, tmp_path):
        engine = BackupEngine(db, tmp_path / "media", {})
        record = await engine.backup_group(
            100, FakePlatform(history=raw_history(5)), download_media=False
        )
        # 群活跃时最后几条只走了实时归档
        db.save_message(make_message(9, content="实时归档那条"), "realtime")
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        return record, manager

    async def test_restore_latest_zip_to_new_group(self, db, tmp_path):
        record, manager = await self._seeded(db, tmp_path)
        result = manager.export_latest(100, "zip")
        restored = await RestoreEngine(db).restore(
            record.backup_id, 300, archive=result.path, confirm=True
        )
        assert restored.inserted == 6
        assert db.count_messages(300) == 6
        assert any(m.content == "实时归档那条" for m in db.get_messages(300))

    async def test_restore_latest_zip_same_group(self, db, tmp_path):
        record, manager = await self._seeded(db, tmp_path)
        result = manager.export_latest(100, "zip")
        restored = await RestoreEngine(db).restore(
            record.backup_id, archive=result.path, confirm=True
        )
        assert restored.skipped == 6
        assert db.count_messages(100) == 6

    async def test_zip_whose_content_contradicts_its_manifest_is_rejected(self, db, tmp_path):
        record, manager = await self._seeded(db, tmp_path)
        good = manager.export_latest(100, "zip")
        broken = tmp_path / "broken.zip"
        with zipfile.ZipFile(good.path) as zin, zipfile.ZipFile(broken, "w") as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.startswith("json/daily/"):
                    doc = json.loads(data)
                    doc[0]["content"] = "被换过的内容"
                    data = json.dumps(doc, ensure_ascii=False).encode()
                zout.writestr(item, data)
        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore(record.backup_id, 300, archive=broken, confirm=True)
        assert db.count_messages(300) == 0


class TestPagingGuard:
    """平台不认 offset，每页都吐同一批消息。"""

    class _IgnoreOffset(FakePlatform):
        async def get_group_history(self, group_id, count=50, offset=0):
            self.history_calls += 1
            assert self.history_calls < 50, "翻页没有终止"
            return self.history[:count]

    async def test_ignored_offset_does_not_loop_forever(self, db, tmp_path):
        platform = self._IgnoreOffset(history=raw_history(120))
        record = await BackupEngine(db, tmp_path / "media", {}).backup_group(
            100, platform, download_media=False
        )
        assert record.status is BackupStatus.COMPLETED
        assert record.message_count == 50
        # 连续两页重复才算「分页到头」：只看到一页重复就不收工，否则「平台每次
        # 都从最新往回翻」这种常见形态会在第二次备份时漏掉后面所有更旧的消息
        assert platform.history_calls == 3

    async def test_normal_paging_still_reads_all_pages(self, db, tmp_path):
        platform = FakePlatform(history=raw_history(120))
        record = await BackupEngine(db, tmp_path / "media", {}).backup_group(
            100, platform, download_media=False
        )
        assert record.message_count == 120
        assert db.count_messages(100) == 120


class TestMediaDedup:
    def test_same_url_downloaded_twice_keeps_one_row(self, db):
        for _ in range(3):
            db.save_media("m1", 100, "http://u/a.png", "/tmp/a.png", "a.png", 10, "image/png", "h1")
        assert len(db.get_media_for_message("m1")) == 1

    def test_different_urls_of_same_message_are_kept(self, db):
        db.save_media("m1", 100, "http://u/a.png", "/tmp/a.png", "a.png", 10, "image/png", "h1")
        db.save_media("m1", 100, "http://u/b.png", "/tmp/b.png", "b.png", 20, "image/png", "h2")
        assert len(db.get_media_for_message("m1")) == 2


class TestCronValidation:
    @pytest.mark.parametrize(
        "cron",
        ["0 3 * * *", "*/15 0-6 * * 1-5", "0 3 1,15 * *", "30 2 * * 0"],
    )
    def test_accepts_valid(self, cron):
        assert validate_cron(cron)

    @pytest.mark.parametrize(
        "cron",
        ["*/0 * * * *", "0 3/0 * * *", "5-3 * * * *", "0 25 * * *", "0 3 * * 8", "0 3 * *", "nope"],
    )
    def test_rejects_invalid(self, cron):
        assert not validate_cron(cron)

    def test_schedule_set_refuses_bad_cron(self, db):
        from backup.core.scheduler import BackupScheduler

        scheduler = BackupScheduler(db, lambda gid: None)
        assert scheduler.add_job(100, "*/0 * * * *") is False
        assert db.list_schedules() == []


class TestChecksumStability:
    def test_unsorted_input_gives_same_checksum(self):
        msgs = [make_message(i) for i in range(6)]
        assert compute_checksum(msgs) == compute_checksum(list(reversed(msgs)))

    def test_same_timestamp_messages_are_ordered_stably(self):
        base = make_message(0)
        twins = [
            type(base)(**{**base.__dict__, "message_id": mid, "content": mid})
            for mid in ("z", "a", "m")
        ]
        assert compute_checksum(twins) == compute_checksum(list(reversed(twins)))
