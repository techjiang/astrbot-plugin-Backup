"""这一轮修的六个问题，每个都能先复现再修。"""

import json
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backup.core.commands import _config_set, render_status
from backup.core.database import REALTIME_BACKUP_ID, Database
from backup.core.download_route import PREFIX, handle_download
from backup.core.export_manager import ExportManager, parse_expire
from backup.core.models import BackupRecord, BackupStatus, MessageType
from backup.core.parser import parse_message
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.verifier import compute_checksum

from .conftest import BASE_TIME, make_message


def _record(backup_id="bk1", checksum="", group_id=100) -> BackupRecord:
    return BackupRecord(
        backup_id=backup_id,
        group_id=group_id,
        status=BackupStatus.COMPLETED,
        started_at=BASE_TIME,
        finished_at=BASE_TIME + timedelta(minutes=1),
        message_count=3,
        checksum=checksum,
    )


def _seed(db, n=3, backup_id="bk1", group_id=100):
    msgs = [make_message(i, group_id=group_id) for i in range(n)]
    for m in msgs:
        db.save_message(m, backup_id)
    db.save_record(_record(backup_id, compute_checksum(msgs), group_id))
    return msgs


class TestExportOverwrite:
    """同一秒内导出两次会撞在同一个路径上，后一份覆盖前一份。"""

    def test_two_exports_in_one_second_do_not_share_a_path(self, db, tmp_path):
        _seed(db)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        first = manager.export(100, "zip", backup_id="bk1")
        second = manager.export(100, "zip", backup_id="bk1")
        assert first.path != second.path
        assert manager.resolve_token(first.token) == first.path
        assert manager.resolve_token(second.token) == second.path

    def test_different_groups_in_same_second_do_not_collide(self, db, tmp_path):
        _seed(db, group_id=100)
        _seed(db, group_id=200, backup_id="bk2")
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        a = manager.export(100, "txt", backup_id="bk1")
        b = manager.export(200, "txt", backup_id="bk2")
        assert a.path != b.path


class TestExpiryParsing:
    def test_seconds_unit_is_understood(self):
        assert parse_expire("30s") == 30

    @pytest.mark.parametrize("value", ["0", "0h", "", "abc"])
    def test_zero_or_garbage_falls_back_to_a_day(self, value):
        # 0 秒 = 导出完链接立刻失效，等于红线 4 要求的「可下载」根本没实现
        assert parse_expire(value) == 24 * 3600


class TestDownloadRoute:
    """链接是发给群成员的，浏览器下大文件一定会发 Range。"""

    def _prepare(self, db, tmp_path):
        _seed(db)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        result = manager.export(100, "zip", backup_id="bk1")
        return manager, result

    def test_token_is_consumed_after_first_use(self, db, tmp_path):
        manager, result = self._prepare(db, tmp_path)
        assert handle_download(PREFIX + result.token, manager).ok
        assert handle_download(PREFIX + result.token, manager).status == 410

    def test_range_request_returns_partial_content(self, db, tmp_path):
        manager, result = self._prepare(db, tmp_path)
        size = result.path.stat().st_size
        resp = handle_download(PREFIX + result.token, manager, range_header="bytes=0-9")
        assert resp.status == 206
        assert resp.headers["Content-Range"] == f"bytes 0-9/{size}"
        assert resp.headers["Content-Length"] == "10"
        body = b"".join(resp.iter_chunks(size=4))
        assert body == result.path.read_bytes()[:10]

    def test_range_from_the_middle_is_sliced_correctly(self, db, tmp_path):
        manager, result = self._prepare(db, tmp_path)
        raw = result.path.read_bytes()
        start = len(raw) // 2
        resp = handle_download(PREFIX + result.token, manager, range_header=f"bytes={start}-")
        assert resp.status == 206
        assert b"".join(resp.iter_chunks(size=64)) == raw[start:]

    def test_unsatisfiable_range_is_rejected(self, db, tmp_path):
        manager, result = self._prepare(db, tmp_path)
        huge = result.path.stat().st_size + 100
        resp = handle_download(PREFIX + result.token, manager, range_header=f"bytes={huge}-")
        assert resp.status == 416

    def test_full_download_still_works_without_range(self, db, tmp_path):
        manager, result = self._prepare(db, tmp_path)
        resp = handle_download(PREFIX + result.token, manager)
        assert resp.status == 200
        assert resp.headers["Accept-Ranges"] == "bytes"
        assert b"".join(resp.iter_chunks()) == result.path.read_bytes()


class TestZipTampering:
    """包和 manifest 一起被改：内容对得上清单，但对不上本地记录。"""

    def _tampered(self, archive: Path, out: Path, extra: list[dict]) -> Path:
        with zipfile.ZipFile(archive) as zin:
            files = {n: zin.read(n) for n in zin.namelist()}
        man = json.loads(files["manifest.json"])
        old = json.loads(files["json/daily/2024-05-01.json"])
        merged = old + extra
        files["json/daily/2024-05-01.json"] = json.dumps(merged, ensure_ascii=False).encode()
        man["checksum"] = compute_checksum([parse_message(m, 100) for m in merged])
        # 攻击者连清单登记条数一起改（不然就成了「漏登记」而不是「伪造」）
        man["message_count"] = len(merged)
        files["manifest.json"] = json.dumps(man, ensure_ascii=False).encode()
        with zipfile.ZipFile(out, "w") as zout:
            for name, data in files.items():
                zout.writestr(name, data)
        return out

    async def test_forged_package_is_rejected(self, db, tmp_path):
        _seed(db)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        good = manager.export(100, "zip", backup_id="bk1")
        evil = self._tampered(
            good.path,
            tmp_path / "evil.zip",
            [
                {
                    "message_id": "mX",
                    "group_id": 100,
                    "sender_id": 9,
                    "timestamp": "2024-05-01T10:00:00",
                    "message_type": "text",
                    "content": "攻击者塞的",
                }
            ],
        )
        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore("bk1", 999, archive=evil, confirm=True)
        assert db.count_messages(999) == 0

    async def test_untouched_package_still_restores(self, db, tmp_path):
        _seed(db)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        good = manager.export(100, "zip", backup_id="bk1")
        result = await RestoreEngine(db).restore("bk1", 300, archive=good.path, confirm=True)
        assert result.inserted == 3
        assert db.count_messages(300) == 3


class TestCleanRetention:
    """清理旧备份不该连聊天记录一起删。"""

    def test_prune_records_keeps_messages(self, db):
        _seed(db, n=3, backup_id="bk_old")
        old = db.get_record("bk_old")
        db.execute(
            "UPDATE backup_records SET started_at = ? WHERE backup_id = ?",
            ((datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S.%f"), "bk_old"),
        )
        removed = db.prune_records(keep_days=90)
        assert removed == 1
        assert db.get_record("bk_old") is None
        # 消息只归属最后一次归档它的备份，删记录顺手删消息等于把整个群抹掉
        assert db.count_messages(100) == 3
        assert old is not None

    def test_prune_records_keeps_most_recent_n(self, db):
        for i in range(5):
            db.save_record(
                BackupRecord(
                    backup_id=f"bk{i}",
                    group_id=100,
                    status=BackupStatus.COMPLETED,
                    started_at=BASE_TIME + timedelta(days=i),
                )
            )
        assert db.prune_records(keep_count=2) == 3
        assert {r.backup_id for r in db.list_records(100)} == {"bk3", "bk4"}

    def test_zero_keep_days_means_no_pruning(self, db):
        _seed(db)
        assert db.prune_records(keep_days=0) == 0
        assert db.get_record("bk1") is not None

    def test_export_files_survive_auto_clean_with_zero_days(self, db, tmp_path):
        _seed(db)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        result = manager.export(100, "txt", backup_id="bk1")
        assert manager.clean_exports(0) == 0
        assert result.path.exists()


class TestConfigGuard:
    """`config set` 写进去的值会存盘并影响下一次启动，格式错了插件就起不来。"""

    def _config(self):
        return {
            "media": {"max_file_size": "100MB", "retry_count": 3, "max_concurrent": 3},
            "export": {"download_link_expire": "24h"},
            "retention": {"keep_days": 90},
        }

    def test_garbage_file_size_is_rejected(self):
        cfg = self._config()
        ok, reason = _config_set(cfg, "media.max_file_size", "garbage")
        assert not ok and "100MB" in reason
        assert cfg["media"]["max_file_size"] == "100MB"

    def test_garbage_expire_is_rejected(self):
        cfg = self._config()
        assert not _config_set(cfg, "export.download_link_expire", "forever")[0]

    def test_zero_concurrency_is_rejected(self):
        cfg = self._config()
        assert not _config_set(cfg, "media.max_concurrent", "0")[0]

    def test_normal_values_still_go_through(self):
        cfg = self._config()
        assert _config_set(cfg, "media.max_file_size", "512MB")[0]
        assert _config_set(cfg, "export.download_link_expire", "7d")[0]
        assert cfg["export"]["download_link_expire"] == "7d"


class TestTimezone:
    """tz-aware 时间戳入库会丢时区，读回来凭空多几个小时，把增量水位带脏。"""

    def test_iso_timestamp_with_offset_is_normalized(self):
        raw = {
            "message_id": "m1",
            "group_id": 100,
            "timestamp": "2024-05-01T18:00:00+08:00",
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        msg = parse_message(raw, 100)
        assert msg.timestamp.tzinfo is None
        # 带 +08:00 的 18:00 落在本地时间刻度上，入库读回不会再漂
        assert msg.timestamp == datetime(2024, 5, 1, 18, 0).astimezone().replace(tzinfo=None)

    def test_aware_and_naive_land_on_the_same_instant(self):
        aware = {
            "message_id": "m1",
            "group_id": 100,
            "timestamp": "2024-05-01T18:00:00+08:00",
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        naive = {
            "message_id": "m2",
            "group_id": 100,
            "timestamp": "2024-05-01T10:00:00",
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        expected = datetime(2024, 5, 1, 18, 0).astimezone().replace(tzinfo=None)
        assert parse_message(aware, 100).timestamp == expected
        assert all(
            isinstance(parse_message(r, 100).timestamp, datetime)
            and parse_message(r, 100).timestamp.tzinfo is None
            for r in (aware, naive)
        )

    def test_stored_timestamp_round_trips(self, db):
        msg = parse_message(
            {
                "message_id": "m1",
                "group_id": 100,
                "timestamp": "2024-05-01T18:00:00+08:00",
                "message": [{"type": "text", "data": {"text": "hi"}}],
            },
            100,
        )
        db.save_message(msg, "bk1")
        back = db.get_messages(100)[0]
        assert back.timestamp.tzinfo is None
        assert back.timestamp == msg.timestamp


class TestStatusRealtime:
    def test_status_reports_realtime_only_messages(self, db):
        db.save_message(make_message(1), REALTIME_BACKUP_ID)
        text = render_status(db, 100)
        assert "实时归档 1 条" in text


class TestExportLatestTampering:
    """`export latest` 的包含实时归档，篡改识别不能因此放行。"""

    def _seed_with_realtime(self, db):
        msgs = _seed(db)
        db.save_message(make_message(9, content="实时归档那条"), REALTIME_BACKUP_ID)
        return msgs

    async def test_latest_package_restores_realtime_messages(self, db, tmp_path):
        self._seed_with_realtime(db)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        good = manager.export_latest(100, "zip")
        result = await RestoreEngine(db).restore("bk1", 300, archive=good.path, confirm=True)
        assert result.inserted == 4
        assert any(m.message_id == "m9" for m in db.get_messages(300))

    async def test_extra_message_in_latest_package_is_rejected(self, db, tmp_path):
        self._seed_with_realtime(db)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        good = manager.export_latest(100, "zip")
        with zipfile.ZipFile(good.path) as zin:
            files = {n: zin.read(n) for n in zin.namelist()}
        man = json.loads(files["manifest.json"])
        old = json.loads(files["json/daily/2024-05-01.json"])
        merged = old + [
            {
                "message_id": "mX",
                "group_id": 100,
                "sender_id": 9,
                "timestamp": "2024-05-01T10:00:00",
                "message_type": "text",
                "content": "夹带的",
            }
        ]
        files["json/daily/2024-05-01.json"] = json.dumps(merged, ensure_ascii=False).encode()
        man["checksum"] = compute_checksum([parse_message(m, 100) for m in merged])
        files["manifest.json"] = json.dumps(man, ensure_ascii=False).encode()
        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zout:
            for name, data in files.items():
                zout.writestr(name, data)
        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore("bk1", 999, archive=evil, confirm=True)
        assert db.count_messages(999) == 0

    async def test_package_on_a_fresh_machine_is_not_misjudged(self, db, tmp_path):
        """换台机器恢复：本地库没有这些消息，不能因为「查不到」就一律拒收。"""
        self._seed_with_realtime(db)
        manager = ExportManager(db, tmp_path / "exports", tmp_path / "media")
        good = manager.export_latest(100, "zip")
        fresh = Database(tmp_path / "fresh" / "backup.sqlite")
        try:
            fresh.save_record(_record("bk1", db.get_record("bk1").checksum))
            result = await RestoreEngine(fresh).restore("bk1", 300, archive=good.path, confirm=True)
            assert result.inserted == 4
        finally:
            fresh.close()
