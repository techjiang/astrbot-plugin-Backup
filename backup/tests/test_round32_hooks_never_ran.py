"""第 32 轮：把「钩子从来没被调到过」这件事钉死。

前面几轮的用例都在验证「钩子内部逻辑对不对」，没人验证「钩子有没有被调用」。
用真 AstrBot 4.14.6 的 `WakingCheckStage` + `StarRequestSubStage` 跑下来，
三个功能（实时归档、退群聚合、解散/被踢备份）一次都没执行过——代码在、
文档在、用例绿，入口是堵的。
"""

import asyncio
import types

import astrbot.api.event  # noqa: F401  先把 astrbot.api.event 拉起来再碰 platform
import pytest

from backup.core.parser import parse_message
from backup.core.event_hooks import parse_event_message, extract_group_id

from .conftest import load_plugin_for_real, real_astrbot_available
from .test_round28_real_astrbot_loader import FakeOneBot, _platform_meta, make_event, make_notice

pytestmark = pytest.mark.skipif(
    not real_astrbot_available(), reason="需要真 AstrBot（pip install -e astrbot）"
)


def _pipeline_ctx():
    """`WakingCheckStage` 只用到这几个字段。"""
    from astrbot.core import astrbot_config

    class Ctx:
        def __init__(self):
            self.astrbot_config = astrbot_config
            self.db_helper = None
            self.provider_manager = None
            self.plugin_manager = types.SimpleNamespace(
                context=types.SimpleNamespace(
                    get_config=lambda: astrbot_config,
                    get_using_provider=lambda *a, **k: None,
                    conversation_manager=None,
                    persona_manager=None,
                    provider_manager=None,
                )
            )

    return Ctx()


async def _through_pipeline(event):
    """跑真 stage：唤醒检查 → handler 调用。返回被激活的 handler 名。"""
    from astrbot.core.pipeline.process_stage.method.star_request import (
        StarRequestSubStage,
    )
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage

    wake = WakingCheckStage()
    await wake.initialize(_pipeline_ctx())
    await wake.process(event)
    activated = [h.handler_name for h in (event.get_extra("activated_handlers") or [])]
    sub = StarRequestSubStage()
    await sub.initialize(_pipeline_ctx())
    async for _ in sub.process(event):
        pass
    return activated


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRBOT_DATA_PATH", str(tmp_path))
    instance, _context, _module = load_plugin_for_real(monkeypatch)
    return instance


def test_plain_group_message_reaches_the_archive_hook(plugin):
    """没人 @、也没敲命令的普通群消息 → `on_message` 被激活，并且真的写进了库。

    旧代码在这里是 `[]`：`on_message` 上挂着 `CommandFilter`，命令匹配
    `message_str.startswith("__backup_archive")` 永远不成立，钩子一次都不跑。
    """
    event = make_event("大家好")
    activated = asyncio.run(_through_pipeline(event))
    assert "on_message" in activated, f"普通消息没唤醒归档钩子：{activated}"
    rows = {dict(r)["message_id"] for r in plugin.db.query("SELECT message_id FROM messages")}
    assert event.message_obj.message_id in rows


def test_notice_reaches_the_notice_hook_exactly_once(plugin):
    """一条退群通知只被一个钩子认领，窗口里人数正好 +1。

    两个钩子都挂 `ALL`，认领标记挂在 event 上（同一条事件是同一个对象）。
    没有这道闸，同一条通知会数两次——阈值 10 的群退了 5 个人就触发备份。
    """
    event = make_notice("group_decrease", "leave", user_id=555, group_id=100)
    activated = asyncio.run(_through_pipeline(event))
    assert activated == ["on_notice"], f"通知被别的钩子接走了：{activated}"
    window = plugin.auto_trigger._leaves.get(100) or ()
    assert len(window) == 1, f"同一条退群通知被数了 {len(window)} 次"


def test_mass_leave_threshold_counts_real_people(plugin):
    """10 个人退群 → 触发一次备份；而不是 5 个人就触发。

    退群聚合是按次数累加的，两个钩子各数一次等于把阈值砍半。
    """
    triggered = []

    async def spy(self, group_id, event=None):
        triggered.append(group_id)
        return None

    type(plugin)._backup = spy
    for i in range(9):
        asyncio.run(_through_pipeline(make_notice("group_decrease", "leave", user_id=900 + i, group_id=100)))
    assert triggered == [], f"只退了 9 个人就触发了备份：{triggered}"
    asyncio.run(_through_pipeline(make_notice("group_decrease", "leave", user_id=999, group_id=100)))
    assert triggered == [100], f"第 10 个人退群没触发备份：{triggered}"


def test_notice_is_not_archived_as_a_message(plugin):
    """通知事件不能以「一条消息」的身份进库。

    `_convert_handle_notice_event` 给通知造的 `AstrBotMessage` 自带
    `message_id`、`timestamp`、`sender`，`raw_message` 里是整份 OneBot 事件。
    把它当消息解析，碰巧会被 `is_event_payload` 拦下——但那条路依赖
    `post_type` 出现在正确的位置，改动一次就可能漏。这里直接钉住行为。
    """
    event = make_notice("group_decrease", "leave", user_id=555, group_id=100)
    before = {dict(r)["message_id"] for r in plugin.db.query("SELECT message_id FROM messages")}
    plugin.hooks.archive_message(event)
    after = {dict(r)["message_id"] for r in plugin.db.query("SELECT message_id FROM messages")}
    assert after == before


def test_raw_event_dict_is_not_treated_as_the_message_body(plugin):
    """`message_obj.raw_message` 是**事件**，不是消息本体。

    它有 `message_id` / `group_id` / `timestamp`，看起来很像一条消息；直接丢给
    `parse_message` 会整份当成「一段」，扣出一个既不像消息 ID、也不像正文的
    `content` 写进库，真正文/媒体/引用全丢。消息本体在 `message_obj` 自己身上。
    """
    raw = {
        "post_type": "message",
        "message_type": "group",
        "message_id": "real-1",
        "group_id": 100,
        "user_id": 7,
        "raw_message": "真正的那句话",
        "sender": {"user_id": 7, "nickname": "阿七"},
    }
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    obj = AstrBotMessage()
    obj.self_id = "999"
    obj.session_id = obj.message_id = obj.group_id = "100"
    obj.type = MessageType.GROUP_MESSAGE
    obj.group = Group(group_id="100", group_name="测试群", group_owner="7")
    obj.sender = MessageMember(user_id="7", nickname="阿七")
    obj.message = [Plain("真正的那句话")]
    obj.message_str = "真正的那句话"
    obj.raw_message = raw
    event = AiocqhttpMessageEvent("真正的那句话", obj, _platform_meta(), "100", FakeOneBot())

    msg = parse_event_message(event, extract_group_id(event))
    assert msg is not None
    assert msg.content == "真正的那句话"

    # 而「只剩 raw_message 可用」这条老路不该把事件当正文
    only_raw = types.SimpleNamespace(raw_message=raw, message_str="", message_obj=None)
    assert parse_event_message(only_raw, 100) is None

    # 反例：raw_message 本身就是一条聊天消息（导出/回灌形态），照常解析
    flat = {"message_id": "m1", "timestamp": 1700000000, "content": "hi", "message_type": "text"}
    assert parse_message(flat, 100).content == "hi"
