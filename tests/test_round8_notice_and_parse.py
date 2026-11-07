"""第 8 轮：通知钩子的接线/取值，以及解析产物要稳定。

两条都是「按文档走一遍就撞墙」级别：
- 通知钩子写好了没人注册 → 退群聚合、解散预警、离群前备份三个功能全部没上线；
- 类型取值顺序反了 → OneBot 的 group_decrease 被当成类型，所有退群通知认不出来。
"""

import pytest

from backup.core.auto_trigger import AutoTrigger
from backup.core.event_hooks import (
    DECREASE_SUBTYPES,
    DISBAND_SUBTYPES,
    KICK_SUBTYPES,
    EventHooks,
    classify_group_event,
    is_message_echo,
    notice_candidates,
)
from backup.core.parser import parse_message, _join_texts
from backup.core.restore_engine import RestoreEngine, RestoreError


def make_hooks(db, **kwargs):
    kwargs.setdefault("cooldown_seconds", 1800)
    return EventHooks(db, AutoTrigger({"mass_leave_threshold": 3}), **kwargs)


class TestSubtypeSets:
    def test_leave_is_not_bot_kick(self):
        """`leave` 是「有成员退群」，不是「机器人被踢」，放进 KICK_SUBTYPES 会让
        群里随便一个人退群都做一次离群备份。"""
        assert "leave" not in KICK_SUBTYPES
        assert "kick" not in KICK_SUBTYPES

    def test_onebot_kick_me_is_covered(self):
        assert "kick_me" in KICK_SUBTYPES

    def test_three_sets_do_not_swallow_each_other(self):
        """同时命中两个集合会被 handle_notice 的 if/else 判成退群聚合。"""
        assert not (DECREASE_SUBTYPES & DISBAND_SUBTYPES)
        assert not (DECREASE_SUBTYPES & KICK_SUBTYPES)


class TestClassify:
    def test_sub_type_wins_over_notice_type(self):
        assert classify_group_event(
            {"group_id": 5, "notice_type": "group_decrease", "sub_type": "leave"}
        ) == (5, "leave")

    def test_falls_back_to_notice_type(self):
        assert classify_group_event({"group_id": 5, "notice_type": "group_file_upload"}) == (
            5,
            "group_file_upload",
        )

    def test_wrapped_payload(self):
        payload = {"post_type": "notice", "notice": {"group_id": 7, "sub_type": "disband"}}
        assert notice_candidates(payload)[1]["group_id"] == 7
        assert classify_group_event(payload) == (None, "")

    def test_message_echo_detected(self):
        assert is_message_echo({"post_type": "message", "group_id": 1}) is True
        assert is_message_echo({"post_type": "notice", "sub_type": "leave"}) is False


class TestHandleNoticeIntegration:
    def test_wrapped_notice_is_handled(self, db):
        hooks = make_hooks(db)
        payload = {"post_type": "notice", "notice": {"group_id": 100, "sub_type": "disband"}}
        assert hooks.handle_notice(payload)[:2] == (100, "disband")
        assert hooks.handle_notice(payload)[2] is False  # 冷却内不重复

    def test_message_echo_never_aggregates(self, db):
        hooks = make_hooks(db)
        for _ in range(10):
            assert hooks.handle_notice({"post_type": "message", "group_id": 100})[2] is False

    def test_mass_leave_triggers_at_threshold(self, db):
        hooks = make_hooks(db)
        results = [
            hooks.handle_notice(
                {"group_id": 100, "notice_type": "group_decrease", "sub_type": "leave"}
            )[2]
            for _ in range(4)
        ]
        assert results == [False, False, True, False]

    def test_bot_kick_triggers_backup(self, db):
        hooks = make_hooks(db)
        payload = {"group_id": 100, "notice_type": "group_decrease", "sub_type": "kick_me"}
        assert hooks.handle_notice(payload)[2] is True

    def test_member_kick_is_aggregated_not_final_backup(self, db):
        hooks = make_hooks(db)
        payload = {"group_id": 100, "notice_type": "group_decrease", "sub_type": "kick"}
        assert hooks.handle_notice(payload)[2] is False


class TestProbe:
    def test_describe_event_reports_group_id(self, db):
        hooks = make_hooks(db)

        class Event:
            group_id = 42
            message_obj = {"message_id": "m1"}

        assert "42" in hooks.describe_event(Event())

    def test_describe_event_flags_unknown_group(self, db):
        hooks = make_hooks(db)

        class Event:
            message_obj = {"message_id": "m1"}

        assert "失败" in hooks.describe_event(Event())

    def test_describe_never_leaks_content(self, db):
        hooks = make_hooks(db)

        class Event:
            group_id = 42
            message_str = "这是群聊里的悄悄话"
            message_obj = {"message_id": "m1", "content": "这也是悄悄话"}

        out = hooks.describe_event(Event())
        assert "悄悄话" not in out
        assert "42" in out

    def test_describe_notice(self, db):
        hooks = make_hooks(db)
        out = hooks.describe_notice(
            {"group_id": 100, "notice_type": "group_decrease", "sub_type": "leave"}
        )
        assert "leave" in out and "100" in out


class TestParseStability:
    def test_at_segment_does_not_add_double_space(self):
        """`[at, text]` 拼成 `@张三  看这个` 会出现在几乎每条 @ 消息里。"""
        msg = parse_message(
            {
                "message_id": "m1",
                "sender": {"user_id": 1},
                "message": [
                    {"type": "at", "data": {"qq": "1", "name": "张三"}},
                    {"type": "text", "data": {"text": " 看这个"}},
                ],
            },
            100,
        )
        assert msg.content == "@张三 看这个"

    def test_ascii_segments_keep_a_space(self):
        assert _join_texts(["hello", "world"]) == "hello world"

    def test_cjk_segments_are_not_padded(self):
        assert _join_texts(["你好", "世界"]) == "你好世界"

    def test_existing_whitespace_is_respected(self):
        """段内原有空白一个字节都不动，只在段与段之间判要不要补。"""
        assert _join_texts(["a  b", "c"]) == "a  b c"
        assert _join_texts(["a ", "b"]) == "a b"

    def test_parse_is_deterministic(self):
        raw = {
            "message_id": "m1",
            "sender": {"user_id": 1},
            "message": [
                {"type": "at", "data": {"qq": "1", "name": "张三"}},
                {"type": "text", "data": {"text": " 看这个"}},
            ],
        }
        assert parse_message(raw, 100).content == parse_message(raw, 100).content


class TestRestoreMissingArchive:
    async def test_missing_zip_says_file_not_found(self, db, tmp_path):
        engine = RestoreEngine(db)
        with pytest.raises(RestoreError) as exc:
            await engine.restore("b1", 100, archive=tmp_path / "nope.zip", confirm=True)
        assert "不存在" in str(exc.value)
