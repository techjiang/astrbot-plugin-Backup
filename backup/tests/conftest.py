import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backup.core.database import Database  # noqa: E402
from backup.core.models import GroupInfo, GroupMember, Message, MessageType  # noqa: E402

BASE_TIME = datetime(2024, 5, 1, 10, 0, 0)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "db" / "backup.sqlite")
    yield database
    database.close()


def make_message(i: int, group_id: int = 100, sender: int = 1, content: str = None, mtype=None) -> Message:
    return Message(
        message_id=f"m{i}",
        group_id=group_id,
        sender_id=sender,
        sender_name=f"用户{sender}",
        timestamp=BASE_TIME + timedelta(minutes=i),
        message_type=mtype or MessageType.TEXT,
        content=content if content is not None else f"消息内容 {i}",
        raw_content="{}",
    )


@pytest.fixture
def messages():
    return [make_message(i) for i in range(5)]


class FakePlatform:
    """假平台：可配置历史消息和失败行为。"""

    def __init__(self, history=None, members=None, info=None, page_size=50):
        self.history = history or []
        self.members = members or []
        self.info = info or {"group_id": 100, "group_name": "测试群"}
        self.page_size = page_size
        self.history_calls = 0

    async def get_group_info(self, group_id):
        return self.info

    async def get_group_members(self, group_id):
        return self.members

    async def get_group_history(self, group_id, count=50, offset=0):
        self.history_calls += 1
        return self.history[offset : offset + count]


def raw_history(n: int, group_id: int = 100) -> list[dict]:
    """构造平台原始消息 dict。"""
    out = []
    for i in range(n):
        out.append(
            {
                "message_id": f"m{i}",
                "group_id": group_id,
                "sender": {"user_id": 1 + i % 3, "nickname": f"用户{i % 3}"},
                "timestamp": (BASE_TIME + timedelta(minutes=i)).timestamp(),
                "message": [{"type": "text", "data": {"text": f"消息 {i}"}}],
            }
        )
    return out


# ---- AstrBot 替身：让 main.py 在容器里也能 import（没有真 AstrBot）----


def install_real_astrbot(monkeypatch):
    """若装了 AstrBot，删掉替身、让真包生效；否则装替身。

    `sys.modules` 的优先级高于 `sys.path`：一旦装了替身，后面再 import 拿到的
    还是替身。所以这里不"二选一铺一套"，只做一件事——**装了真的就用真的**。
    """
    import importlib.util

    if importlib.util.find_spec("astrbot") is not None:
        for name in [n for n in list(sys.modules) if n == "astrbot" or n.startswith("astrbot.")]:
            if getattr(sys.modules[name], "__astrbot_stub__", False):
                monkeypatch.delitem(sys.modules, name, raising=False)
        return None
    return install_fake_astrbot(monkeypatch)


def install_fake_astrbot(monkeypatch):
    class AstrMessageEvent:  # noqa: N801
        pass

    class Context:
        def __init__(self):
            self.registered = []

        def register_web_api(self, route, view, methods, desc):  # noqa: ARG002
            self.registered.append(route)

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_a, **_k):
        def deco(cls):
            return cls

        return deco

    class Filter:
        class EventMessageType:
            ALL = "ALL"

        @staticmethod
        def command(_name):
            return lambda fn: fn

        @staticmethod
        def event_message_type(_kind):
            return lambda fn: fn

    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.filter = Filter
    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register
    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []
    # 打上标记：`install_real_astrbot` 靠它认出「这是替身，真货来了就撤」
    for _mod in (pkg,):
        _mod.__astrbot_stub__ = True
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.__astrbot_stub__ = True
    api.event = api_event
    api.star = api_star
    for name, mod in (
        ("astrbot", pkg),
        ("astrbot.api", api),
        ("astrbot.api.event", api_event),
        ("astrbot.api.star", api_star),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    return Context


# ---- 真 AstrBot 才走的分支 ----

#: 真 AstrBot 的 `register_command` 用 `get_handler_full_name`（`<module>_<name>`）
#: 做注册表键，**而且命中已有条目时直接返回旧的**（`get_handler_or_create`）。
#: 两份不同的插件代码撞进同一条用例进程时，第二次注册拿回的是上一份代码的
#: handler —— 用例于是永远测不到新函数。所以「真 AstrBot 下加载插件」这件事
#: 统一走这个 reset。
def reset_star_registries():
    from astrbot.core.star.star import star_map, star_registry
    from astrbot.core.star.star_handler import star_handlers_registry

    star_map.clear()
    star_registry.clear()
    star_handlers_registry.star_handlers_map.clear()
    star_handlers_registry._handlers.clear()


def real_astrbot_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("astrbot") is not None


def load_plugin_for_real(monkeypatch, plugin_dir=None, config=None):
    """用真 AstrBot 的 `Star`/`Context`/注册表把插件**真加载一遍**。

    返回值 `(plugin, context, module)`。加载方式照 `star_manager` 的做法：
    以目录名当模块名（`metadata.yaml` 的 `name: backup`）import 那个包。

    `plugin_dir` 默认指向仓库里的 `backup/`；想造场景（比如改名、加一个坏文件）
    就传一份临时 copy 的路径。
    """
    import importlib
    import importlib.util
    import sys
    from pathlib import Path

    plugin_dir = Path(plugin_dir or Path(__file__).resolve().parent.parent)
    reset_star_registries()

    name = "backup_under_test"
    for mod_name in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
        del sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(
        name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    main_mod = importlib.import_module(f"{name}.main")

    from astrbot.core.star.context import Context

    context = Context(
        event_queue=None,
        config=_astrbot_host_config(),
        db=None,
        provider_manager=None,
        platform_manager=_FakePlatformManager(),
        conversation_manager=None,
        message_history_manager=None,
        persona_manager=None,
        astrbot_config_mgr=None,
        knowledge_base_manager=None,
        cron_manager=None,
    )
    plugin = main_mod.BackupPlugin(context=context, config=config)

    # `star_manager` 在实例化插件之后会做一次绑定（`star_manager.py` 里
    # 「Restore decorator-registered callables before binding the current
    # instance」那段）：把注册表里的**裸函数**取回来，再
    # `functools.partial(raw_handler, instance)`。
    # 不做这一步，注册表里躺着的是 `BackupPlugin.backup` 这个**未绑定**函数，
    # `call_handler` 调 `handler(event)` 就少一个 `self`——
    # `TypeError: missing 1 required positional argument: 'event'`。
    # 这段不补，本文件搭的场景和真实加载差着最后一步，测出来的东西不可信。
    import functools
    from astrbot.core.star.star_handler import star_handlers_registry

    for handler in star_handlers_registry.get_handlers_by_module_name(f"{name}.main"):
        raw = handler.handler.func if isinstance(handler.handler, functools.partial) else handler.handler
        handler.handler = functools.partial(raw, plugin)

    return plugin, context, main_mod


def _astrbot_host_config():
    from astrbot.core import astrbot_config

    return astrbot_config


class _FakePlatformManager:
    """`Context.get_platform_inst` 会遍历它。空的是真的要测的那一种情形。"""

    platform_insts: list = []

    def get_insts(self):
        return list(self.platform_insts)
