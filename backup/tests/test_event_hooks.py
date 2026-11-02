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
