"""用**真 AstrBot**跑一遍接线层。

这个文件存在的理由：前面所有用例都自己造 `AstrMessageEvent`、自己造注册器。
那套假设从来没被外部东西检验过——`filter.command` 到底是不是「注册 handler」、
`CommandFilter` 会不会 `inspect` 签名并跳过前两个参数、`EventHooks.handle_notice`
的第四个返回值有没有人接、`platform_insts` 里是不是真有个 webchat。

装一份 AstrBot（`pip install -e <astrbot 源码>`）就能把这些全部变成可执行的断言。
没装时整组 skip——不是「不用验」，是「这个环境验不了」。
"""

import asyncio
import uuid

import pytest

from .conftest import load_plugin_for_real, real_astrbot_available

pytestmark = pytest.mark.skipif(
    not real_astrbot_available(), reason="需要真 AstrBot（pip install -e astrbot）"
)


# --------------------------------------------------------------------------
# 事件：真 `AiocqhttpMessageEvent`，不是自己写的 fake
# --------------------------------------------------------------------------


class FakeOneBot:
    """只假一件事：OneBot 客户端就是平台本身，没有它就没有消息。"""

    def __init__(self):
        self.actions = []
        self.sent = []

    async def send_group_msg(self, **params):
        self.sent.append(params)
        return {"message_id": len(self.sent)}

    async def send_private_msg(self, **params):
        self.sent.append(params)
        return {"message_id": len(self.sent)}

    async def call_action(self, action, **params):
        self.actions.append((action, params))
        if action == "get_group_msg_history":
            return {"messages": []}
        if action == "get_group_info":
            return {"group_id": params.get("group_id"), "group_name": "测试群", "member_count": 3}
        if action == "get_group_member_list":
            return [{"user_id": 7, "nickname": "阿七", "role": "owner"}]
        raise RuntimeError(action)


def _platform_meta():
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    return PlatformMetadata(name="aiocqhttp", description="", id="acc-1")


def make_event(text="大家好", user_id="7", group_id="100", bot=None):
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    abm = AstrBotMessage()
    abm.self_id = "999"
    abm.session_id = group_id
    abm.message_id = uuid.uuid4().hex[:8]
    abm.type = MessageType.GROUP_MESSAGE
    abm.group = Group(group_id=group_id, group_name="测试群", group_owner=user_id)
    abm.group_id = group_id
    abm.sender = MessageMember(user_id=user_id, nickname="阿七")
    abm.message = [Plain(text)]
    abm.message_str = text
    abm.raw_message = {
        "post_type": "message",
        "message_type": "group",
        "group_id": int(group_id),
        "user_id": int(user_id),
        "message_id": abm.message_id,
        "raw_message": text,
    }
    return AiocqhttpMessageEvent(text, abm, _platform_meta(), group_id, bot or FakeOneBot())


def make_notice(notice_type, sub_type, user_id=123, group_id=100, message_id="n1"):
    """照 `_convert_handle_notice_event` 的真实产物造：`OTHER_MESSAGE` + 空消息链。"""
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    abm = AstrBotMessage()
    abm.self_id = "999"
    abm.session_id = str(group_id)
    abm.message_id = message_id
    abm.type = MessageType.OTHER_MESSAGE
    abm.group_id = str(group_id)
    abm.sender = MessageMember(user_id=str(user_id), nickname=str(user_id))
    abm.message = []
    abm.message_str = ""
    abm.raw_message = {
        "post_type": "notice",
        "notice_type": notice_type,
        "sub_type": sub_type,
        "group_id": group_id,
        "user_id": user_id,
    }
    return AiocqhttpMessageEvent("", abm, _platform_meta(), str(group_id), FakeOneBot())


def plugin_handlers():
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    return {
        h.handler_name: h
        for h in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
    }


async def wake_and_activate(event):
    """跑真 `WakingCheckStage`，看哪些 handler 被激活。"""
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage

    stage = WakingCheckStage()
    await stage.initialize(_pipeline_ctx())
    await stage.process(event)
    return [h.handler_name for h in (event.get_extra("activated_handlers") or [])]


async def run_activated(event):
    """跑真 `StarRequestSubStage`——真正调用 handler 的那一层。"""
    from astrbot.core.pipeline.process_stage.method.star_request import (
        StarRequestSubStage,
    )

    stage = StarRequestSubStage()
    await stage.initialize(_pipeline_ctx())
    async for _ in stage.process(event):
        pass
    return event


class _PipelineCtx:
    """pipeline 阶段只用到这几个字段，其余不碰。"""

    def __init__(self):
        import types

        from astrbot.core import astrbot_config

        self.astrbot_config = astrbot_config
        self.astrbot_config_id = "default"
        self.db_helper = None
        self.provider_manager = None
        self.plugin_manager = types.SimpleNamespace(
            context=types.SimpleNamespace(
                conversation_manager=None,
                persona_manager=None,
                provider_manager=None,
            )
        )


def _pipeline_ctx():
    return _PipelineCtx()


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRBOT_DATA_PATH", str(tmp_path))
    instance, _context, _module = load_plugin_for_real(monkeypatch)
    return instance


# --------------------------------------------------------------------------
# 1. 加载：注册表里有没有、事件监听器是不是真的会被调用
# --------------------------------------------------------------------------


def test_handlers_are_in_registry_with_a_command_filter(plugin):
    """两个钩子都得在注册表里，且**带 CommandFilter**。

    AstrBot 往 `star_handlers_registry` 里塞东西的唯一入口是 register 系列
    装饰器。只挂 `@filter.event_message_type` 的话 handler 会进注册表，
    但 `WakingCheckStage` 对「event_filters 为空」的 handler 直接 `continue`——
    注册表里有名字、插件详情里能看到，但永远不会被调用。实测过：旧代码
    `on_message` / `on_notice` 的 filters 只有 `EventMessageTypeFilter`。
    """
    from astrbot.core.star.filter.command import CommandFilter

    handlers = plugin_handlers()
    for name in ("on_message", "on_notice"):
        assert name in handlers, f"{name} 没进注册表"
        kinds = {type(f) for f in handlers[name].event_filters}
        assert CommandFilter in kinds, f"{name} 缺注册位 CommandFilter，永远不会被调到"
        assert any(
            isinstance(f, CommandFilter) and f.command_name.startswith("__backup")
            for f in handlers[name].event_filters
        ), f"{name} 的注册位命令名不该是用户能敲到的名字"


def test_command_filter_can_inspect_the_hook_signature(plugin):
    """`CommandFilter.init_handler_md` 会 `inspect.signature` 并跳过前两个参数。

    `async def on_notice(self)` 只有一个参数，这条路径是 `TypeError`——真实
    加载器下插件直接加载失败。这里把「签名能被 inspect 且参数解析为空」钉住。
    """
    from astrbot.core.star.filter.command import CommandFilter

    for name in ("on_message", "on_notice"):
        cmd = next(
            f for f in plugin_handlers()[name].event_filters if isinstance(f, CommandFilter)
        )
        assert cmd.handler_params == {}, f"{name} 不该有额外的必填参数"


def test_plain_group_message_does_not_wake_anything(plugin):
    """没人 @ 机器人、也没唤醒前缀的普通群消息：一个 handler 都不该被激活。

    `EventMessageTypeFilter.ALL` 在 AstrBot 里不等于「无条件」——没唤醒的事件
    到不了 ProcessStage。把这条钉住，以后往钩子上加东西时不会误以为「反正会跑」。
    """
    event = make_event("大家好")
    activated = asyncio.run(wake_and_activate(event))
    assert activated == []


def test_command_is_dispatched_and_replies(plugin):
    """唤醒的命令能真跑到 `BackupPlugin.backup`，并产出一条回执。

    旧代码的 `backup(self, event)` 在这里是 `TypeError: missing 1 required
    positional argument`——`call_handler` 调的是 `handler(event)`。
    """
    from astrbot.core import astrbot_config

    prefix = (astrbot_config.get("wake_prefix") or ["/"])[0]
    event = make_event(f"{prefix}backup help")
    activated = asyncio.run(wake_and_activate(event))
    assert "backup" in activated

    # `StarRequestSubStage` 每跑完一个 handler 都会 `event.clear_result()`，
    # 所以跑完之后 `get_result()` 一定是空的——回执在真 pipeline 里是边跑边交给
    # respond 阶段的。要看「有没有回话」，就得把 `call_handler` 包一层，在每个
    # handler 收尾、`clear_result()` 之前那一刻抓（`star_request` 是
    # `from ... import call_handler` 拿的引用，得补它那一份）。
    replies = []

    from astrbot.core.pipeline.context_utils import call_handler

    original = call_handler

    import astrbot.core.pipeline.process_stage.method.star_request as star_request

    async def capturing(event_arg, handler, *args, **kwargs):
        result = None
        try:
            async for item in original(event_arg, handler, *args, **kwargs):
                result = event_arg.get_result() or result
                yield item
        finally:
            result = result or event_arg.get_result()
            if result is not None:
                replies.append(result.get_plain_text())

    star_request.call_handler = capturing
    try:
        asyncio.run(run_activated(event))
    finally:
        star_request.call_handler = original

    assert replies, "命令 handler 没有产出任何回执"
    assert "/backup" in replies[0]


# --------------------------------------------------------------------------
# 2. 通知：真形状下退群聚合不能被数两遍
# --------------------------------------------------------------------------


def test_notice_returns_four_values_and_marks_aggregation(plugin):
    """接线层要能拿到「这次数过一个人了」这个信号。

    旧签名返回三个值，`main.py` 按三个值解包——改一处漏一处就是 `ValueError`，
    所以这里把契约钉在返回值形状上。
    """
    payload = {"notice_type": "group_decrease", "sub_type": "leave", "group_id": 100}
    result = plugin.hooks.handle_notice(payload)
    assert len(result) == 4
    group_id, subtype, should_backup, counted = result
    assert (group_id, subtype, counted) == (100, "leave", True)


def test_same_notice_through_both_handlers_counts_once(plugin):
    """真 pipeline 会把同一条 notice 送给两个钩子，退群只能数一次。

    实测（旧代码）：阈值 3 的群，真退 2 个人 + 一次重投就把阈值顶满；按
    AstrBot 的广播分发，每一条通知都会走两遍，所以阈值直接减半。
    """
    plugin.hooks.trigger.threshold = 2

    fired = []

    async def fake_backup(group_id, event=None):
        fired.append(group_id)
        return None

    plugin._backup = fake_backup

    async def round_trip(event):
        """AstrBot 的分发是广播式的：同一条 notice 必然进两个钩子。"""
        await plugin.on_notice(event)
        await plugin.on_message(event)

    asyncio.run(round_trip(make_notice("group_decrease", "leave", user_id=1, message_id="a")))
    # 已经数过去重窗口里的那一次之后，同一个群里再退一个人只该 +1
    assert plugin.hooks.trigger.record_leave(100, None) == 2, "同一条通知被数了两次"


def test_kick_series_by_one_admin_counts_every_victim(plugin):
    """一个管理员连踢 10 个人 = 10 次退群，不能并成 1 次。

    通知里 `operator_id` 是同一个（那个管理员），`user_id` 是每个被踢的人。
    指纹如果带上 operator，10 条通知会归一成同一个键，退群聚合永远到不了阈值
    ——反过来，如果指纹漏掉 `user_id`，同一秒内踢 10 个人又会并成 1 次。
    """
    counted = []
    for i in range(10):
        event = make_notice("group_decrease", "kick", user_id=200 + i, message_id=f"k{i}")
        event.message_obj.raw_message["operator_id"] = 7  # 同一个管理员
        counted.append(plugin._note_notice_once(event))
    assert all(counted), "10 次不同的踢人被判成了重复投递"


def test_replayed_notice_is_not_counted_twice(plugin):
    """同一条通知投两遍（`on_message` + `on_notice`）只认第一遍。"""
    event = make_notice("group_decrease", "leave", user_id=1, message_id="dup")
    assert plugin._note_notice_once(event) is True
    assert plugin._note_notice_once(event) is False


# --------------------------------------------------------------------------
# 3. 平台选择：`platform_insts` 的第一位不是真平台
# --------------------------------------------------------------------------


def test_platform_pick_skips_webchat(monkeypatch, plugin):
    """webchat 排在 `platform_insts` 最前面，它没有群历史——不能拿它备份。"""
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    class _WebChat:
        def meta(self):
            return PlatformMetadata(name="webchat", description="", id="web-1")

    class _Real:
        client = object()

        def meta(self):
            return PlatformMetadata(name="aiocqhttp", description="", id="acc-1")

    class _Manager:
        platform_insts = [_WebChat(), _Real()]

        def get_insts(self):
            return list(self.platform_insts)

    plugin.context.platform_manager = _Manager()
    picked = plugin._resolve_platform(None)
    assert picked.meta().name == "aiocqhttp"


def test_event_platform_wins_over_global(plugin):
    """事件自带 bot：直接用它，不去全局里翻。"""
    bot = FakeOneBot()
    event = make_event("hi", bot=bot)
    assert plugin._resolve_platform(event) is bot


# --------------------------------------------------------------------------
# 4. 面板配置：`_conf_schema.json` 的每一项都真的能读到
# --------------------------------------------------------------------------


def test_injected_config_is_the_schema_shaped_one(monkeypatch, tmp_path):
    """AstrBot 以 `config=` 传进来的是**按 schema 摊平的 dict**，不是 `config.yaml`。

    两边形状不一样，读错就是「面板改了不生效」。这里比对默认值：注入的每一层
    都得是 dict，且关键默认值对得上 `config.yaml`。
    """
    from astrbot.core.config.astrbot_config import AstrBotConfig

    from ..main import _load_config

    schema_path = tmp_path / "schema.json"
    import json

    import yaml

    plugin_dir = __import__("pathlib").Path(__file__).resolve().parent.parent
    schema = json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8"))
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    injected = AstrBotConfig(
        config_path=str(tmp_path / "backup_config.json"), schema=schema
    )
    merged = _load_config(injected)
    yaml_conf = yaml.safe_load((plugin_dir / "config.yaml").read_text(encoding="utf-8"))
    yaml_conf = yaml_conf.get("backup", yaml_conf)
    for key, value in yaml_conf.items():
        assert key in merged, f"注入配置里少了 {key}"
        if isinstance(value, dict):
            assert isinstance(merged[key], dict), f"{key} 应该是 dict"
            for sub_key in value:
                assert sub_key in merged[key], f"注入配置里少了 {key}.{sub_key}"


# --------------------------------------------------------------------------
# 5. 端到端：真事件 → 真钩子 → 真命令 → 导出 → 下载 → 恢复
# --------------------------------------------------------------------------


class _Platform(FakeOneBot):
    """一台会说话的 OneBot：群历史、群信息、成员表都有。"""

    HISTORY = [
        {
            "message_id": f"h{i}",
            "group_id": 100,
            "user_id": 7,
            "time": 1750000000 + i,
            "raw_message": f"历史 {i}",
            "message": [{"type": "text", "data": {"text": f"历史 {i}"}}],
            "sender": {"user_id": 7, "nickname": "阿七"},
        }
        for i in range(3)
    ]

    async def call_action(self, action, **params):
        self.actions.append((action, params))
        if action == "get_group_msg_history":
            return {"messages": list(self.HISTORY)}
        if action == "get_group_info":
            return {"group_id": 100, "group_name": "真群", "member_count": 1}
        if action == "get_group_member_list":
            return [{"user_id": 7, "nickname": "阿七", "role": "owner"}]
        if action in ("get_group_notice", "get_group_root_files"):
            return []
        raise RuntimeError(action)


def test_end_to_end_through_real_events(tmp_path, monkeypatch):
    """一条真消息走完插件的一生：归档 → 备份 → 导出 → 下载 → 恢复。

    前面每一层都分别测过，但「分开都对、串起来不对」是这个仓库反复踩的坑
    （第 21 轮那个「视图收到零个参数」就是这么来的）。所以这一条不打桩，
    只把 OneBot 客户端换成会回话的。
    """
    monkeypatch.setenv("ASTRBOT_DATA_PATH", str(tmp_path))
    from backup.core.download_route import handle_download
    from .conftest import load_plugin_for_real

    plugin, _ctx, _mod = load_plugin_for_real(
        monkeypatch,
        config={
            "scope": {"group_messages": True, "download_media": False},
            "retention": {"auto_clean": False},
            "schedule": {"enabled": False},
            "permission": {"require_admin": False, "allow_private": False},
        },
    )
    bot = _Platform()

    async def flow():
        # 1) 实时归档
        await plugin.on_message(make_event("大家好", bot=bot))
        archived = plugin.db.get_messages(100, limit=100)
        assert archived, "实时归档一条都没进库"

        # 2) /backup now（真 WakingCheckStage 会削掉唤醒前缀，这里手工对齐）
        event = make_event("/backup now", bot=bot)
        event.is_wake = True
        event.is_at_or_wake_command = True
        event.message_str = event.message_str[1:]
        replies = [r.get_plain_text() async for r in plugin.backup(event)]
        assert replies and "备份" in replies[-1]

        records = plugin.db.list_records(100)
        assert records and records[0].status.value in ("completed", "partial")
        assert records[0].message_count > 0

        # 3) 导出 + token 兑换
        exported = plugin.exports.export_latest(100, "zip")
        response = handle_download(f"/backup/download/{exported.token}", plugin.exports)
        assert response.status == 200 and response.file is not None

        # 4) 恢复到另一个群
        result = await plugin.restore_engine.restore(
            records[0].backup_id, 777, "skip", confirm=True
        )
        assert result.target_group_id == 777
        assert plugin.db.get_messages(777, limit=100), "恢复没写进去任何消息"

    asyncio.run(flow())
