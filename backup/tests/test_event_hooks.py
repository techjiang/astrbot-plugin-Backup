"""事件钩子测试：实时归档 + 触发式备份。用鸭子类型假事件，不依赖 AstrBot。"""

from datetime import datetime

import pytest

from backup.core.auto_trigger import AutoTrigger
from backup.core.event_hooks import (
    EventHooks,
    classify_group_event,
    extract_group_id,
    parse_event_message,
)


class FakeEvent:
    def __init__(self, group_id=100, message_id="e1", text="你好", sender_id=7, name="阿七"):
        self.group_id = group_id
        self.message_id = message_id
        self.message_str = text
        self.sender_id = sender_id
        self.sender_name = name
        self.message_obj = {
            "message_id": message_id,
            "group_id": group_id,
            "timestamp": int(datetime(2024, 5, 1, 10, 0).timestamp() * 1000),
            "sender": {"user_id": sender_id, "nickname": name},
            "message": [{"type": "plain", "data": {"text": text}}],
        }


def make_hooks(db, **kwargs):
    return EventHooks(db, AutoTrigger({"mass_leave_threshold": 3}), **kwargs)


class TestExtract:
    def test_group_id_from_attr(self):
        assert extract_group_id(FakeEvent(group_id=42)) == 42

    def test_group_id_from_message_obj(self):
        class E:
            message_obj = {"group_id": 88}

        assert extract_group_id(E()) == 88

    def test_dm_returns_none(self):
        class E:
            pass

        assert extract_group_id(E()) is None

    def test_parse_event_message_keeps_identity(self):
        msg = parse_event_message(FakeEvent(), 100)
        assert msg.sender_id == 7
        assert msg.sender_name == "阿七"
        assert "你好" in msg.content

    def test_empty_event_returns_none(self):
        class E:
            message_str = ""
            message_obj = None

        assert parse_event_message(E(), 100) is None


class TestArchive:
    def test_archives_message(self, db):
        hooks = make_hooks(db)
        msg = hooks.archive_message(FakeEvent())
        assert msg is not None
        assert db.count_messages(100) == 1

    def test_same_message_not_duplicated(self, db):
        hooks = make_hooks(db)
        hooks.archive_message(FakeEvent())
        assert hooks.archive_message(FakeEvent()) is None
        assert db.count_messages(100) == 1

    def test_archive_disabled(self, db):
        hooks = make_hooks(db, archive=False)
        assert hooks.archive_message(FakeEvent()) is None
        assert db.count_messages(100) == 0

    def test_archive_does_not_write_message_content_to_log(self, db, caplog):
        hooks = make_hooks(db)
        hooks.archive_message(FakeEvent(text="这是私密内容"))
        assert "这是私密内容" not in caplog.text


class TestTriggers:
    def test_mass_leave_triggers_at_threshold(self, db):
        hooks = make_hooks(db)
        assert [hooks.note_group_decrease(100) for _ in range(3)] == [False, False, True]

    def test_cooldown_blocks_second_trigger(self, db):
        hooks = make_hooks(db)
        for _ in range(3):
            hooks.note_group_decrease(100)
        assert hooks.note_group_decrease(100) is False

    def test_disband_triggers(self, db):
        hooks = make_hooks(db)
        assert hooks.note_group_event(100, "disband") is True

    def test_kick_triggers_once(self, db):
        hooks = make_hooks(db)
        assert hooks.note_group_event(100, "kick") is True
        assert hooks.note_group_event(100, "kick") is False

    def test_unknown_subtype_ignored(self, db):
        assert make_hooks(db).note_group_event(100, "poke") is False

    def test_classify_group_event(self):
        assert classify_group_event({"group_id": 5, "sub_type": "Decrease"}) == (5, "decrease")
        assert classify_group_event({"group": "5", "notice_type": "disband"})[1] == "disband"
        assert classify_group_event({}) == (None, "")


class FakeDownloader:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def download(self, url, message_id, timestamp):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return f"/media/{message_id}.jpg"


def with_media_event(group_id=100, message_id="m-media"):
    event = FakeEvent(group_id=group_id, message_id=message_id)
    event.message_obj["message"] = [
        {"type": "image", "data": {"url": "https://cdn.example.com/a.jpg"}}
    ]
    return event


class TestHandleNotice:
    def test_leave_aggregates_until_threshold(self, db):
        hooks = make_hooks(db)
        for _ in range(3):
            group_id, subtype, should = hooks.handle_notice({"group_id": 100, "sub_type": "leave"})
        assert group_id == 100
        assert subtype == "leave"
        assert should is True  # 阈值 3，最后一次刚好触发

    def test_leave_below_threshold_does_not_backup(self, db):
        hooks = make_hooks(db)
        _, _, should = hooks.handle_notice({"group_id": 100, "sub_type": "leave"})
        assert should is False

    def test_disband_triggers(self, db):
        hooks = make_hooks(db)
        _, subtype, should = hooks.handle_notice({"group_id": 100, "sub_type": "disband"})
        assert subtype == "disband"
        assert should is True

    def test_kick_triggers_once_within_cooldown(self, db):
        hooks = make_hooks(db)
        assert hooks.handle_notice({"group_id": 100, "sub_type": "kick_off"})[2] is True
        assert hooks.handle_notice({"group_id": 100, "sub_type": "kick_off"})[2] is False

    def test_notice_without_group_falls_back_to_event(self, db):
        hooks = make_hooks(db)
        event = FakeEvent(group_id=555)
        group_id, _, _ = hooks.handle_notice({"sub_type": "disband"}, event)
        assert group_id == 555

    def test_unknown_payload_returns_none(self, db):
        hooks = make_hooks(db)
        assert hooks.handle_notice({})[0] is None

    def test_disband_disabled_by_config(self, db):
        hooks = make_hooks(db, on_disband=False)
        assert hooks.handle_notice({"group_id": 100, "sub_type": "disband"})[2] is False

    def test_kick_disabled_by_config(self, db):
        hooks = make_hooks(db, on_leave=False)
        assert hooks.handle_notice({"group_id": 100, "sub_type": "kick"})[2] is False


class TestArchiveWithMedia:
    async def test_backfills_media_paths(self, db):
        hooks = make_hooks(db)
        downloader = FakeDownloader()
        msg = await hooks.archive_with_media(with_media_event(), downloader)
        assert downloader.calls == 1
        assert msg.media_paths == ["/media/m-media.jpg"]
        stored = db.get_messages(group_id=100)
        assert stored[0].media_paths == ["/media/m-media.jpg"]

    async def test_media_failure_marks_and_keeps_message(self, db):
        hooks = make_hooks(db)
        msg = await hooks.archive_with_media(with_media_event(), FakeDownloader(fail=True))
        assert msg.media_paths[0].startswith("[下载失败]")
        assert db.count_messages(100) == 1  # 消息照常入库（红线 2）

    async def test_without_downloader_only_archives(self, db):
        hooks = make_hooks(db)
        msg = await hooks.archive_with_media(with_media_event(), None)
        assert msg.media_paths == ["https://cdn.example.com/a.jpg"]

    async def test_duplicate_message_not_downloaded_twice(self, db):
        hooks = make_hooks(db)
        event = with_media_event()
        await hooks.archive_with_media(event, FakeDownloader())
        downloader = FakeDownloader()
        assert await hooks.archive_with_media(event, downloader) is None
        assert downloader.calls == 0
