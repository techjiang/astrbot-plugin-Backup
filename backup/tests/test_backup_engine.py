from datetime import datetime, timedelta

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.models import BackupStatus, GroupInfo, GroupMember

from .conftest import FakePlatform, raw_history


def make_engine(db, tmp_path, config=None):
    return BackupEngine(db, tmp_path / "media", config or {"media": {}})


@pytest.mark.asyncio
async def test_full_backup_saves_all_messages(db, tmp_path):
    platform = FakePlatform(
        history=raw_history(120),
        members=[{"user_id": 1, "nickname": "甲", "card": "卡片甲", "role": "owner"}],
        info={"group_id": 100, "group_name": "测试群", "member_count": 3},
    )
    engine = make_engine(db, tmp_path)
    record = await engine.backup_group(100, platform, incremental=False, download_media=False)

    assert record.status is BackupStatus.COMPLETED
    assert record.message_count == 120
    assert db.count_messages(100) == 120
    assert platform.history_calls >= 3  # 分页拉取
    assert db.get_group_info(100).group_name == "测试群"
    assert len(db.get_members(100)) == 1


@pytest.mark.asyncio
async def test_incremental_backup_skips_existing(db, tmp_path):
    platform = FakePlatform(history=raw_history(10))
    engine = make_engine(db, tmp_path)
    await engine.backup_group(100, platform, incremental=False, download_media=False)

    platform.history = raw_history(15)
    record = await engine.backup_group(100, platform, incremental=True, download_media=False)

    assert record.is_incremental is True
    assert record.message_count == 5
    assert db.count_messages(100) == 15


@pytest.mark.asyncio
async def test_media_download_failure_does_not_block(db, tmp_path, monkeypatch):
    history = raw_history(3)
    history[1]["message"] = [
        {"type": "image", "data": {"url": "https://example.invalid/a.png"}}
    ]
    platform = FakePlatform(history=history)
    engine = make_engine(db, tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(engine.media, "download", boom)
    record = await engine.backup_group(100, platform, incremental=False, download_media=True)

    assert record.message_count == 3
    assert record.status is BackupStatus.PARTIAL
    saved = db.get_messages(100)
    assert any("[下载失败]" in p for m in saved for p in m.media_paths)


@pytest.mark.asyncio
async def test_backup_record_has_correct_counts(db, tmp_path):
    platform = FakePlatform(history=raw_history(7))
    engine = make_engine(db, tmp_path)
    record = await engine.backup_group(100, platform, incremental=False, download_media=False)

    stored = db.get_record(record.backup_id)
    assert stored.message_count == 7
    assert stored.media_count == 0
    assert stored.finished_at is not None
    assert stored.checksum == record.checksum


@pytest.mark.asyncio
async def test_checksum_is_deterministic(db, tmp_path):
    platform = FakePlatform(history=raw_history(5))
    engine = make_engine(db, tmp_path)
    r1 = await engine.backup_group(100, platform, incremental=False, download_media=False)

    db2 = type(db)(tmp_path / "db" / "second.sqlite")
    platform2 = FakePlatform(history=raw_history(5))
    engine2 = BackupEngine(db2, tmp_path / "media2", {"media": {}})
    r2 = await engine2.backup_group(100, platform2, incremental=False, download_media=False)
    db2.close()

    assert r1.checksum == r2.checksum


@pytest.mark.asyncio
async def test_backup_failure_marks_record_failed(db, tmp_path):
    class BrokenPlatform(FakePlatform):
        async def get_group_members(self, group_id):
            raise RuntimeError("api error")

    engine = make_engine(db, tmp_path)
    record = await engine.backup_group(100, BrokenPlatform(history=[]), incremental=False)
    assert record.status is BackupStatus.FAILED
    assert "api error" in record.error
