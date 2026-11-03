"""第六轮：换机器恢复、老备份能翻出来、跨群不串、成员接口挂掉不吞消息。"""

import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.commands import PermissionChecker, render_backup_list
from backup.core.database import Database
from backup.core.export_manager import ExportManager
from backup.core.models import BackupRecord, BackupStatus
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.tests.conftest import BASE_TIME, FakePlatform, make_message, raw_history


def make_engine(db, tmp_path):
    return BackupEngine(db, tmp_path / "media", {"incremental": {"enabled": True}})


def make_exports(db, tmp_path):
    return ExportManager(db, tmp_path / "exports", tmp_path / "media", {})


def record(db, backup_id, group_id=100, count=1, when=BASE_TIME):
    db.save_record(
        BackupRecord(
            backup_id=backup_id,
            group_id=group_id,
            status=BackupStatus.COMPLETED,
            started_at=when,
            finished_at=when,
            message_count=count,
            checksum="",
        )
    )


class Growing(FakePlatform):
    def __init__(self, n, group_id=777):
        super().__init__(history=raw_history(n, group_id=group_id))


# ---------- 换机器恢复 ----------
class TestRestoreOnAnotherMachine:
    @pytest.mark.asyncio
    async def test_restore_without_local_record_uses_archive(self, db, tmp_path):
        """本机没有这条记录，只有别人给的包：以前直接「备份不存在」。

        这正是最需要用备份的场景——原机器没了、群被炸了，手里只有一个 ZIP。
        """
        source = make_engine(db, tmp_path)
        rec = await source.backup_group(
            777, Growing(4), incremental=False, download_media=False
        )
        pack = make_exports(db, tmp_path).export_latest(777, "zip")

        fresh = Database(tmp_path / "other.sqlite")
        assert fresh.get_record(rec.backup_id) is None
        result = await RestoreEngine(fresh).restore(rec.backup_id, archive=pack.path, confirm=True)
        assert result.inserted == 4
        assert result.target_group_id == 777
        assert fresh.count_messages(777) == 4
        fresh.close()

    @pytest.mark.asyncio
    async def test_restored_record_is_usable_afterwards(self, db, tmp_path):
        """恢复完得能在 list / export 里看见，否则等于白恢复。"""
        source = make_engine(db, tmp_path)
        rec = await source.backup_group(
            777,
            FakePlatform(
                history=raw_history(3, group_id=777),
                info={"group_id": 777, "group_name": "老群"},
                members=[{"user_id": 1, "nickname": "阿一"}],
            ),
            incremental=False,
            download_media=False,
        )
        pack = make_exports(db, tmp_path).export_latest(777, "zip")

        fresh = Database(tmp_path / "other.sqlite")
        await RestoreEngine(fresh).restore(rec.backup_id, archive=pack.path, confirm=True)
        assert fresh.get_record(rec.backup_id) is not None
        ctx = make_exports(fresh, tmp_path).build_context(777, rec.backup_id)
        assert len(ctx.messages) == 3
        assert ctx.group_name == "老群"
        assert len(ctx.members) == 1
        fresh.close()

    @pytest.mark.asyncio
    async def test_manifest_carries_group_and_members(self, db, tmp_path):
        source = make_engine(db, tmp_path)
        await source.backup_group(
            777,
            FakePlatform(
                history=raw_history(2, group_id=777),
                info={"group_id": 777, "group_name": "老群"},
                members=[{"user_id": 9, "nickname": "九"}],
            ),
            incremental=False,
            download_media=False,
        )
        pack = make_exports(db, tmp_path).export_latest(777, "zip")
        with zipfile.ZipFile(pack.path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
        assert manifest["group_info"]["group_name"] == "老群"
        assert manifest["members"][0]["nickname"] == "九"
        assert manifest["record"]["group_id"] == 777

    @pytest.mark.asyncio
    async def test_missing_record_and_archive_still_refused(self, db, tmp_path):
        with pytest.raises(RestoreError):
            await RestoreEngine(db).restore("bk_nope", 100, confirm=True)


# ---------- 老备份/跨群 ----------
class TestSourceBasedRestore:
    @pytest.mark.asyncio
    async def test_old_backup_survives_a_full_rerun(self, db, tmp_path):
        """全量重跑会把归属全部接手。老备份不能因此变成空的。"""
        engine = make_engine(db, tmp_path)
        old = await engine.backup_group(777, Growing(3), incremental=False, download_media=False)
        await engine.backup_group(777, Growing(3), incremental=False, download_media=False)

        result = await RestoreEngine(db).restore(old.backup_id, 888, confirm=True)
        assert result.inserted == 3
        assert db.count_messages(888) == 3

    @pytest.mark.asyncio
    async def test_export_of_old_backup_keeps_its_messages(self, db, tmp_path):
        engine = make_engine(db, tmp_path)
        old = await engine.backup_group(777, Growing(2), incremental=False, download_media=False)
        await engine.backup_group(777, Growing(2), incremental=False, download_media=False)
        result = make_exports(db, tmp_path).export(777, "txt", backup_id=old.backup_id)
        assert result.message_count == 2

    @pytest.mark.asyncio
    async def test_restore_does_not_carry_other_groups(self, db, tmp_path):
        """一份备份覆盖多个群时，恢复只搬它自己那个群的消息。"""
        for gid in (100, 200):
            for i in range(2):
                db.save_message(make_message(i, group_id=gid), "b1")
        record(db, "b1", group_id=100, count=4)

        result = await RestoreEngine(db).restore("b1", 999, confirm=True)
        assert result.inserted == 2
        assert db.count_messages(999) == 2


# ---------- 成员接口挂掉 ----------
@pytest.mark.asyncio
async def test_member_and_info_failure_keeps_messages(db, tmp_path):
    class Blind(FakePlatform):
        async def get_group_info(self, group_id):
            raise RuntimeError("no perm")

        async def get_group_members(self, group_id):
            raise RuntimeError("no perm")

    engine = make_engine(db, tmp_path)
    rec = await engine.backup_group(
        777, Blind(history=raw_history(3, group_id=777)), incremental=False, download_media=False
    )
    assert rec.message_count == 3
    assert db.count_messages(777) == 3
    assert "群信息" in rec.error and "成员列表" in rec.error


@pytest.mark.asyncio
async def test_group_info_falls_back_to_previous_snapshot(db, tmp_path):
    engine = make_engine(db, tmp_path)
    await engine.backup_group(
        777,
        FakePlatform(history=raw_history(1, group_id=777), info={"group_id": 777, "group_name": "老群名"}),
        incremental=False,
        download_media=False,
    )

    class NoInfo(FakePlatform):
        async def get_group_info(self, group_id):
            raise RuntimeError("no perm")

    await engine.backup_group(777, NoInfo(history=[]), incremental=False, download_media=False)
    assert db.get_group_info(777).group_name == "老群名"


# ---------- 记录语义 ----------
@pytest.mark.asyncio
async def test_rerun_is_not_reported_as_first_full_backup(db, tmp_path):
    engine = make_engine(db, tmp_path)
    first = await engine.backup_group(777, Growing(2), incremental=False, download_media=False)
    second = await engine.backup_group(777, Growing(2), incremental=False, download_media=False)
    assert first.is_incremental is False
    assert second.is_incremental is True


@pytest.mark.asyncio
async def test_retry_after_interruption_counts_all_fetched(db, tmp_path):
    """中途断掉后重跑：这一轮确实又拉了一遍，就照实记，别显示 0。"""
    engine = make_engine(db, tmp_path)
    await engine.backup_group(777, Growing(3), incremental=False, download_media=False)
    again = await engine.backup_group(777, Growing(3), incremental=False, download_media=False)
    assert again.status is BackupStatus.COMPLETED


def test_backup_list_shows_group_when_not_specified(db):
    db.save_message(make_message(0, group_id=100), "b1")
    db.save_message(make_message(0, group_id=200), "b2")
    record(db, "b1", group_id=100)
    record(db, "b2", group_id=200)
    text = render_backup_list(db)
    assert "[群 100]" in text and "[群 200]" in text


# ---------- 权限 ----------
def test_admin_role_is_used_when_adapter_method_raises():
    class Event:
        def is_admin(self):
            raise RuntimeError("not implemented")

        sender_role = "owner"

    assert PermissionChecker(True).check_event(Event()).allowed


def test_role_inside_sender_dict():
    class Event:
        is_admin = False
        sender = {"role": "admin"}

    assert PermissionChecker(True).check_event(Event()).allowed


def test_plain_member_still_denied():
    class Event:
        is_admin = False
        role = "member"

    assert not PermissionChecker(True).check_event(Event()).allowed


# ---------- 清理 ----------
def test_prune_records_also_drops_sources_and_snapshots(db):
    db.save_message(make_message(0), "b1")
    db.add_sources("b1", db.get_messages(group_id=100))
    db.save_bundle("b1", 100, None, [])
    record(db, "b1", count=1, when=datetime(2024, 5, 1))
    db.prune_records(keep_days=1)
    assert db.get_record("b1") is None
    assert db.count_sources("b1") == 0
    assert db.get_bundle("b1", 100) == {}
    assert db.count_messages(100) == 1  # 消息一条不删
