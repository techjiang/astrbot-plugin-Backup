"""第 34 轮：备份钩子把机器人变成了话痨。

这一轮的验证方式和第 32 轮一样——装真 AstrBot、跑真 `WakingCheckStage` /
`StarRequestSubStage` / `ProcessStage`。撞出来的两处都在**同一行代码的副作用**上：

1. `on_message` / `on_notice` 为了让钩子被调用，把 `event.is_at_or_wake_command`
   置真（第 32 轮的修法）。可这个标记不只对钩子生效——`ProcessStage` 跑完所有
   handler 之后，只要「没发过消息 + 被唤醒 + 没禁 LLM」，就继续把这条事件交给
   `AgentRequestSubStage`。于是：**群里每来一条消息，机器人回一句 LLM 内容**；
   退群/撤回这类通知，机器人在群里现编一段话。

   旧代码上实测：一条群消息 → `agent_sub_stage` 被调用一次、`event.send` 收到
   一整个 MessageChain；一条 `group_recall` 通知同样。

2. 配置里 `provider_settings.enable = false` 时不会触发，所以本地试一下就
   「没事」——真部署里它默认是开的。

修法是让钩子自己把默认 LLM 链路关掉（`should_call_llm(False)`），而不是把
`is_at_or_wake_command` 回滚掉：回滚了钩子又进不了 `ProcessStage`，等于把第
32 轮的问题原样还回去。
"""

import asyncio
import types

import pytest

from .conftest import load_plugin_for_real, real_astrbot_available

pytestmark = pytest.mark.skipif(
    not real_astrbot_available(), reason="需要真 AstrBot（pip install astrbot==4.14.6）"
)

if real_astrbot_available():
    from .test_round28_real_astrbot_loader import make_event, make_notice


async def _avalue(value):
    return value


def _pipeline_context(config):
    """`PipelineContext` 真要的那三个字段。其余 stage 用到的顺手补上。"""
    from astrbot.core.pipeline.context import PipelineContext

    conversation = types.SimpleNamespace(
        get_curr_conversation_id=lambda *a, **k: _avalue(None),
        new_conversation=lambda *a, **k: _avalue(None),
    )
    context = types.SimpleNamespace(
        get_config=lambda: config,
        get_using_provider=lambda *a, **k: None,
        get_using_tts_provider=lambda *a, **k: None,
        conversation_manager=conversation,
        persona_manager=None,
        provider_manager=None,
        plugin_manager=None,
    )

    plugin_manager = types.SimpleNamespace(context=context)

    class _PluginManager:
        context = plugin_manager.context

    return PipelineContext(config, _PluginManager(), "default")


class _SpyAgent:
    """顶替 `AgentRequestSubStage`：真被调用就产出回复。

    真实宿主里 `AgentRequestSubStage` 一旦被调用就会去问模型、然后把答案设成
    `event` 的结果，`RespondStage` 再把它发出去。所以「这个替身有没有被调用」
    就是「机器人会不会开口」。`call_llm` 一并记下来，用作「钩子确实关掉了」
    的直接证据。
    """

    def __init__(self, reply="LLM 回复"):
        self.calls = []
        self.reply = reply

    async def process(self, event):
        from astrbot.core.message.message_event_result import MessageEventResult

        self.calls.append((event.message_obj.type.value, event.call_llm))
        if self.reply:
            event.set_result(MessageEventResult().message(self.reply))
        if False:  # 让它是异步生成器，和真实 sub stage 同形
            yield


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """一个跑得起来的真 pipeline + 一个只负责记账的 agent 替身。"""
    monkeypatch.setenv("ASTRBOT_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        "backup.main.assert_writable_target", lambda root, roots: tmp_path / "storage"
    )
    plugin, _context, _module = load_plugin_for_real(monkeypatch)

    import astrbot.core as host

    from astrbot.core.pipeline.process_stage.stage import ProcessStage
    from astrbot.core.pipeline.scheduler import PipelineScheduler
    from astrbot.core.platform.astr_message_event import AstrMessageEvent

    config = host.astrbot_config
    agent = _SpyAgent()
    sent = []


    async def _capture(self, chain):
        sent.append(str(chain))

    # `AiocqhttpMessageEvent.send` 会真的去连 OneBot。这里只截住「有没有发」。
    # 不能再往 `AstrMessageEvent.send` 里转发：基类实现会把 token 上报给平台指标，
    # 在测试进程里就是一次真实的网络请求。
    monkeypatch.setattr(AstrMessageEvent, "send", _capture)

    scheduler = PipelineScheduler(_pipeline_context(config))
    asyncio.run(scheduler.initialize())
    for stage in scheduler.stages:
        if isinstance(stage, ProcessStage):
            stage.agent_sub_stage = agent

    import functools

    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    # 注册表里的 handler 在加载时就被绑成了 `functools.partial(..., 那个实例)`。
    # 本用例建的是**另一个**插件实例：不重绑的话钩子跑的是加载时那个实例，
    # 断言看的是这个实例 —— 两边不是同一份 storage。
    registered = []
    for handler in star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent
    ):
        raw = handler.handler
        while isinstance(raw, functools.partial):
            raw = raw.func
        if getattr(raw, "__self__", None) is not None:
            raw = raw.__func__
        handler.handler = functools.partial(raw, plugin)
        registered.append(handler.handler_name)
    assert {"on_message", "on_notice"} <= set(registered), registered

    yield types.SimpleNamespace(
        plugin=plugin, scheduler=scheduler, agent=agent, sent=sent,
        stage=next(s for s in scheduler.stages if isinstance(s, ProcessStage)),
    )
    try:
        plugin.gateway.stop()
    except Exception:
        pass


def _run(pipeline, event):
    """跑一遍真 pipeline，返回这条事件。

    账本每次清零：同一个 fixture 里连跑几条事件时，上一条留下的 `sent` 会被
    误当成这一条的回执。
    """
    pipeline.agent.calls.clear()
    pipeline.sent.clear()
    pipeline.stage.agent_sub_stage = pipeline.agent
    event.send = lambda chain: _noop(pipeline, chain)
    asyncio.run(pipeline.scheduler.execute(event))
    return event


async def _noop(pipeline, chain):
    pipeline.sent.append(str(chain))


# ---------------------------------------------------------------- 群消息
def test_bot_does_not_reply_to_every_group_message(pipeline):
    """普通群消息只该被归档，不该换来一句 LLM 回复。

    旧代码：`agent_sub_stage` 被调用一次、`send` 收到一整个 MessageChain。
    根因是钩子为了被调用把 `is_at_or_wake_command` 置真，回头没关掉默认 LLM。
    """
    event = _run(pipeline, make_event("今天吃啥"))
    assert event.call_llm is False, "钩子必须显式关掉默认 LLM 链路（should_call_llm(False)）"

    # 归档照旧：这条消息必须还在库里
    rows = [
        dict(r)["message_id"]
        for r in pipeline.plugin.db.query("SELECT message_id FROM messages")
    ]
    assert event.message_obj.message_id in rows


def test_archive_hook_still_runs_for_group_message(pipeline):
    """修的是「别回话」，不是「别归档」——钩子必须仍然被调用。"""
    event = make_event("大家好")
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage

    wake = WakingCheckStage()
    asyncio.run(wake.initialize(pipeline.scheduler.ctx))
    asyncio.run(wake.process(event))
    activated = [h.handler_name for h in (event.get_extra("activated_handlers") or [])]
    assert activated == ["on_message"], activated


# ---------------------------------------------------------------- 通知
def test_bot_does_not_reply_to_a_recall_notice(pipeline):
    """退群/撤回通知：机器人在群里现编一段话。

    旧代码：通知事件照样进 `AgentRequestSubStage`，`type` 是 `OTHER_MESSAGE`。
    这不是「回复错人」，是「对系统事件发一条聊天消息」。
    """
    event = _run(pipeline, make_notice("group_recall", "", user_id=7, group_id=100))
    assert event.call_llm is False, "通知钩子也必须关掉默认 LLM 链路"


def test_notice_hook_still_runs(pipeline):
    """同上：关掉 LLM 不等于关掉钩子。"""
    event = make_notice("group_decrease", "leave", user_id=5, group_id=100)
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage

    wake = WakingCheckStage()
    asyncio.run(wake.initialize(pipeline.scheduler.ctx))
    asyncio.run(wake.process(event))
    activated = [h.handler_name for h in (event.get_extra("activated_handlers") or [])]
    assert activated == ["on_notice"], activated


# ---------------------------------------------------------------- 契约
def test_hooks_turn_off_default_llm_explicitly():
    """`should_call_llm(False)` 是这件事的**契约**，不是顺带的效果。

    钩子必须显式关掉默认 LLM：靠 `is_at_or_wake_command` 回滚是走不通的
    （回滚了钩子就进不了 ProcessStage），靠「配置里关掉 AI」是把责任推给用户。
    """
    import inspect

    from backup.main import BackupPlugin

    for name in ("on_message", "on_notice"):
        source = inspect.getsource(getattr(BackupPlugin, name))
        assert "_silence_default_llm" in source, f"{name} 没有关掉默认 LLM 链路"

    helper = inspect.getsource(BackupPlugin._silence_default_llm)
    assert "should_call_llm(False)" in helper


def test_silence_helper_tolerates_old_hosts():
    """老宿主/替身上没有 `should_call_llm` 时不能抛——那是「禁不掉」，不是错误。"""

    from backup.main import BackupPlugin

    class _Old:
        pass

    BackupPlugin._silence_default_llm(_Old())  # 不抛即通过

    class _New:
        def __init__(self):
            self.seen = None

        def should_call_llm(self, value):
            self.seen = value

    host = _New()
    BackupPlugin._silence_default_llm(host)
    assert host.seen is False


# ---------------------------------------------------------------- 另一个方向
def test_command_still_replies(pipeline):
    """反过来也要成立：`/backup help` 这种命令必须照常回话。

    钩子动手之前，这条事件已经被 `WakingCheckStage` 判成「要处理」了；钩子只是
    顺手把标记又置了一遍。真正的风险是「插件把整个宿主静音」——这条用例盯的就是
    别把 `should_call_llm(False)` 用在所有事件上。
    """
    before = make_event("/backup help")
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage

    wake = WakingCheckStage()
    asyncio.run(wake.initialize(pipeline.scheduler.ctx))
    asyncio.run(wake.process(before))
    activated = [h.handler_name for h in (before.get_extra("activated_handlers") or [])]
    assert "backup" in activated, f"命令没被唤醒：{activated}"

    event = _run(pipeline, make_event("/backup help"))
    # 命令本身不回话由命令层决定，但插件不该为此静音整个会话
    assert pipeline.agent.calls or event.call_llm is True
