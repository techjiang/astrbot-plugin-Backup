"""第 16 轮：三处「平时看不出来、真出事才发现」的问题。

每个场景都先复现、再修，改完同一个脚本重跑看着它变正确，最后固化在这里。
"""

import glob
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.database import Database
from backup.core.export_manager import ExportManager
from backup.core.models import BackupRecord, BackupStatus
from backup.core.parser import parse_message
from backup.core.restore_engine import RestoreEngine, RestoreError
from backup.core.verifier import compute_checksum, compute_manifest_checksum

from .conftest import BASE_TIME

GROUP = 999


def raw(i: int, group_id: int = GROUP) -> dict:
    return {
        "message_id": f"m{i}",
        "group_id": group_id,
        "sender": {"user_id": 1, "nickname": "甲"},
        "timestamp": (BASE_TIME + timedelta(minutes=i)).timestamp(),
        "message": [{"type": "text", "data": {"text": f"消息 {i}"}}],
    }


class _NewestFirst:
    """平台每次从最新往回翻、每页最多 50 条。

    这是很多适配器的真实形态：`offset` 是从最新一条往回的偏移，不是全局序号。
    上一轮备份已经把最新的一段写进库，于是这一次的第一页整页都是「翻过的消息」。
    """

    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def get_group_info(self, group_id=None, **kwargs):
        return {"group_id": 100, "group_name": "群"}

    async def get_group_members(self, group_id=None, **kwargs):
        return []

    async def get_group_history(self, group_id=None, count=50, offset=0, **kwargs):
        # 引擎是按 group_id= 关键字调的，这里照收；方向由平台自己决定：最新在前
        self.calls += 1
        return self.items[offset : offset + 50]


async def test_page_of_already_seen_messages_does_not_end_paging(db, tmp_path):
    """第一页全是翻过的消息，不代表后面没有了。

    以前只要碰到一页「整页都翻过」就判分页到头——「每次都从最新往回翻」的平台
    在第二次备份时第一页必然长这样，于是剩下更旧的一大段一条都不备，
    状态还是 `completed`。
    """
    engine = BackupEngine(db, tmp_path / "media", {"media": {}})
    # 群 100 的历史：最新在前（平台就是这样吐回来的）
    items = [raw(i, group_id=100) for i in range(119, -1, -1)]
    # 上一轮已把最旧的 20 条（m0..m19）写进库
    for i in range(20):
        db.save_message(parse_message(raw(i, group_id=100), 100), "realtime")

    platform = _NewestFirst(items)
    record = await engine.backup_group(100, platform, incremental=True, download_media=False)

    assert record.status is BackupStatus.COMPLETED
    # 第 41 轮起不再按「这一页给得少」收工，所以最后会多问一次空页：3 页数据 + 1 次确认
    assert platform.calls == 4, "120 条 / 每页 50，必须翻满 3 页再确认一次到头"
    assert db.count_messages(100) == 120
    assert {m.message_id for m in db.get_messages(group_id=100)} == {
        f"m{i}" for i in range(120)
    }


async def test_self_exported_package_can_be_restored(db, tmp_path):
    """自己导出的包，恢复时不能被自己的「夹带」判据拒掉。

    `messages.backup_id` 记的是「最后归档这条消息的那次备份」，只有一份。本机还
    留着更老的一份备份、它也覆盖过同一条消息时，归属就落在老备份名下——
    而这条消息本来就在这份记录的包里。
    """
    for i in range(5):
        db.save_message(parse_message(raw(i), GROUP), "bk_now")
    now = datetime.now()
    db.save_record(
        BackupRecord(
            backup_id="bk_now",
            group_id=GROUP,
            status=BackupStatus.COMPLETED,
            started_at=now,
            finished_at=now,
            message_count=5,
        )
    )
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    result = exports.export(GROUP, "zip", "bk_now")

    # 更老的一份备份仍在，m0/m1 的归属被它接着
    db.save_record(
        BackupRecord(
            backup_id="bk_old",
            group_id=GROUP,
            status=BackupStatus.COMPLETED,
            started_at=now - timedelta(days=5),
            finished_at=now - timedelta(days=5),
            message_count=2,
        )
    )
    db.execute(
        "UPDATE messages SET backup_id = 'bk_old' WHERE group_id = ? AND message_id IN ('m0','m1')",
        (GROUP,),
    )

    restored = await RestoreEngine(db).restore(
        "bk_now", 888, "skip", archive=str(result.path), confirm=True
    )

    assert restored.inserted == 5
    assert db.count_messages(888) == 5


async def test_message_owned_by_older_backup_before_this_one_is_still_rejected(db, tmp_path):
    """反向那条线不动：动手之前就归别人的消息，出现在包里就是夹带。"""
    for i in range(5):
        db.save_message(parse_message(raw(i), GROUP), "bk_now")
    now = datetime.now()
    db.save_record(
        BackupRecord(
            backup_id="bk_now",
            group_id=GROUP,
            status=BackupStatus.COMPLETED,
            started_at=now,
            finished_at=now,
            message_count=5,
        )
    )
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    result = exports.export(GROUP, "zip", "bk_now")

    # 这份记录「出手之前」m0 就已经归另一份备份了
    db.save_record(
        BackupRecord(
            backup_id="bk_earlier",
            group_id=GROUP,
            status=BackupStatus.COMPLETED,
            started_at=now - timedelta(days=5),
            finished_at=now - timedelta(days=5),
            message_count=1,
        )
    )
    db.execute(
        "UPDATE messages SET backup_id = 'bk_earlier' WHERE group_id = ? AND message_id = 'm0'",
        (GROUP,),
    )
    record = db.get_record("bk_now")
    engine = RestoreEngine(db)
    assert engine._owned_by_other_before(record, "m0") is True
    assert engine._owned_by_other_before(record, "m3") is False


async def test_announcements_and_group_files_travel_with_the_package(db, tmp_path):
    """群公告 / 群文件备份时采了，但导出包只带消息和成员——换台机器恢复完就没了。"""
    for i in range(3):
        db.save_message(parse_message(raw(i), GROUP), "bk_full")
    now = datetime.now()
    db.save_record(
        BackupRecord(
            backup_id="bk_full",
            group_id=GROUP,
            status=BackupStatus.COMPLETED,
            started_at=now,
            finished_at=now,
            message_count=3,
        )
    )
    db.save_announcements(
        GROUP,
        [
            {
                "content": "本群禁止广告",
                "sender_id": 1,
                "sender_name": "群主",
                "published_at": BASE_TIME,
                "raw": "",
            }
        ],
    )
    db.save_group_files(
        GROUP,
        [
            {
                "file_id": "f1",
                "name": "群规.pdf",
                "size": 1024,
                "upload_time": BASE_TIME,
                "uploader_id": 1,
                "uploader_name": "群主",
                "raw": "",
            }
        ],
    )
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    result = exports.export(GROUP, "zip", "bk_full")

    manifest = json.loads(zipfile.ZipFile(result.path).read("manifest.json"))
    assert manifest["announcements"][0]["content"] == "本群禁止广告"
    assert manifest["group_files"][0]["name"] == "群规.pdf"

    # 另一台机器：空库
    fresh = Database(tmp_path / "other" / "backup.sqlite")
    try:
        await RestoreEngine(fresh).restore(
            "bk_full", 777, "skip", archive=str(result.path), confirm=True
        )
        assert [a["content"] for a in fresh.get_announcements(777)] == ["本群禁止广告"]
        assert [f["name"] for f in fresh.get_group_files(777)] == ["群规.pdf"]
    finally:
        fresh.close()


async def test_announcement_text_running_board_does_not_break_restore(db, tmp_path):
    """包里的时间是 JSON 里的字符串，写回库时不能再当成 datetime 去 strftime。"""
    for i in range(2):
        db.save_message(parse_message(raw(i), GROUP), "bk_a")
    now = datetime.now()
    db.save_record(
        BackupRecord(
            backup_id="bk_a",
            group_id=GROUP,
            status=BackupStatus.COMPLETED,
            started_at=now,
            finished_at=now,
            message_count=2,
        )
    )
    db.save_announcements(
        GROUP,
        [{"content": "公告", "sender_id": 0, "sender_name": "", "published_at": now, "raw": ""}],
    )
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    result = exports.export(GROUP, "zip", "bk_a")

    fresh = Database(tmp_path / "other2" / "backup.sqlite")
    try:
        restored = await RestoreEngine(fresh).restore(
            "bk_a", 555, "skip", archive=str(result.path), confirm=True
        )
        assert restored.inserted == 2
        assert len(fresh.get_announcements(555)) == 1
    finally:
        fresh.close()


async def test_manifest_checksum_covers_announcements(db, tmp_path):
    """公告/文件列表改了，指纹要跟着变——以前它们不在指纹覆盖范围内。"""
    manifest = {
        "group_id": GROUP,
        "group_name": "群",
        "announcements": [{"content": "原文"}],
        "group_files": [],
    }
    before = compute_manifest_checksum(manifest)
    manifest["announcements"] = [{"content": "被改过"}]
    assert compute_manifest_checksum(manifest) != before
