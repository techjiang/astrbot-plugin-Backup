"""第 20 轮（续）：把 `main.py` 的接线跑起来。

前 350 个用例全在 `core/` 里，`main.py` 覆盖率是 0——而这一轮查出来的
「定时任务拿不到平台」「`/backup now` 恒报没有适配器」两处问题恰好都在
`main.py`。这里用一份**照 AstrBot 4.14.6 形状写的** astrbot 替身把插件真的
实例化，跑通「事件进来 → 归档 → 命令回话」的主线。
"""

import asyncio
import sys
import types
from datetime import datetime, timedelta

import pytest


class _Result:
    """`event.plain_result()` 的返回值，只记录文案。"""

    def __init__(self, text):
        self.text = text


class _Filter:
    class EventMessageType:
        ALL = "ALL"

    @staticmethod
    def command(name):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def event_message_type(_kind):
        def deco(fn):
            return fn

        return deco


def _status_of(response) -> int:
    """响应状态码统一成 int。

    装上 Werkzeug 之后 `_as_flask` 回的是 `werkzeug.wrappers.Response`，它的
    `status` 是 `HTTPStatus` 枚举（`404 NOT FOUND`）。和 int 相等但类型不同，
    断言里用 `== 404` 会在某些写法下给出反直觉的结果，比较前统一转一下。
    """
    status = getattr(response, "status", response)
    # `werkzeug.wrappers.Response.status` 是 `"404 NOT FOUND"` 这样的字符串；
    # `HTTPStatus` 枚举和 int 都能直接 int()。取前面的数字部分
    if isinstance(status, str):
        status = status.split(" ", 1)[0]
    try:
        return int(status)
    except (TypeError, ValueError):
        return int(getattr(status, "value", 0))

def _install_fake_astrbot(monkeypatch):
    """装一份最小但形状正确的 astrbot 包，让 `main.py` 能被导入。"""

    class AstrMessageEvent:  # noqa: N801 - 照原名
        pass

    class Context:
        def __init__(self, platform_manager=None):
            self.platform_manager = platform_manager

        def get_platform_inst(self, platform_id):
            for inst in getattr(self.platform_manager, "platform_insts", []):
                if inst.meta().id == platform_id:
                    return inst
            return None

        def get_platform(self, name):
            for inst in getattr(self.platform_manager, "platform_insts", []):
                if inst.meta().name == name:
                    return inst
            return None

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_args, **_kwargs):
        def deco(cls):
            return cls

        return deco

    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.filter = _Filter

    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register

    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.event = api_event
    api.star = api_star

    for name, mod in (
        ("astrbot", pkg),
        ("astrbot.api", api),
        ("astrbot.api.event", api_event),
        ("astrbot.api.star", api_star),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    _install_fake_astrbot(monkeypatch)
    Context = sys.modules["astrbot.api.star"].Context
    sys.modules.pop("backup.main", None)
    import backup.main as main_mod

    monkeypatch.setattr(main_mod, "PLUGIN_DIR", tmp_path / "plugin")
    (tmp_path / "plugin").mkdir(parents=True)
    (tmp_path / "plugin" / "config.yaml").write_text(
        f"""backup:
  storage_dir: "{tmp_path / 'storage'}"
  scope:
    download_media: false
  schedule:
    enabled: false
""",
        encoding="utf-8",
    )

    class Meta:
        id = "id-a"
        name = "aiocqhttp"

    class Adapter:
        def __init__(self):
            self.calls = []

        def meta(self):
            return Meta()

        def get_client(self):
            return self

        async def call_action(self, action, **params):  # noqa: ARG002
            self.calls.append(action)
            if action == "get_group_msg_history":
                return {"messages": []}
            return None

    manager = types.SimpleNamespace(platform_insts=[Adapter()])
    instance = main_mod.BackupPlugin(Context(manager))
    instance._adapter = manager.platform_insts[0]
    yield instance
    instance.db.close()


def _event(plugin, group_id=100, text="看这个"):
    """照 AstrBot 形状的事件：取数据都是方法，原始事件在 message_obj.raw_message。"""

    class ComponentType:
        Plain = "Plain"
        Image = "Image"

    class Comp:
        def __init__(self, ctype, **kwargs):
            self.type = ctype
            for key, value in kwargs.items():
                setattr(self, key, value)

    class MessageType:
        GROUP_MESSAGE = "GroupMessage"

    class Msg:
        def __init__(self):
            self.type = MessageType.GROUP_MESSAGE
            self.self_id = "1"
            self.message_id = "e1"
            self.group = types.SimpleNamespace(group_id=str(group_id))
            self.sender = types.SimpleNamespace(user_id="7", nickname="甲")
            self.message = [Comp(ComponentType.Plain, text=text)]
            self.message_str = text
            self.raw_message = {}
            self.timestamp = int(datetime.now().timestamp())

        @property
        def group_id(self):
            return self.group.group_id

    class Ev:
        def __init__(self):
            self.message_obj = Msg()
            self.message_str = text
            self.role = "admin"

        def get_group_id(self):
            return self.message_obj.group_id

        def get_sender_id(self):
            return "7"

        def get_self_id(self):
            return "1"

        def get_platform_id(self):
            return "id-a"

        def get_platform_name(self):
            return "aiocqhttp"

        def get_message_type(self):
            return self.message_obj.type

        def is_private_chat(self):
            return False

        def is_admin(self):
            return True

        def plain_result(self, text):
            return _Result(text)

    return Ev()


@pytest.mark.asyncio
async def test_resolve_platform_finds_the_event_adapter(plugin):
    """`Context.get_platform` 是方法。原来 `getattr(...)` 拿到 bound method 当平台用，
    每个 `_call` 都打在方法对象上全返回 None，`/backup now` 只会回一句
    「当前没有可用的平台适配器」。"""
    event = _event(plugin)
    assert plugin._resolve_platform(event) is plugin._adapter
    # 定时任务没有事件：也要能落到唯一那个平台上
    assert plugin._resolve_platform(None) is plugin._adapter


@pytest.mark.asyncio
async def test_realtime_archive_through_the_hook(plugin):
    """事件进来就落库，且媒体/引用认得出（AstrBot 段类型是 str-Enum）。"""
    event = _event(plugin)
    await plugin.on_message(event)
    stored = plugin.db.get_messages(100)
    assert len(stored) == 1
    assert stored[0].content == "看这个"


@pytest.mark.asyncio
async def test_backup_now_replies_with_a_record(plugin):
    """`/backup now` 要走通：拿到平台、跑一轮、回一句人话。"""
    event = _event(plugin)
    replies = [r.text async for r in plugin._cmd_now(event, [])]
    assert any("开始备份" in r for r in replies)
    assert any("备份" in r and "结束" in r for r in replies)
    assert "当前没有可用的平台适配器" not in "".join(replies)


@pytest.mark.asyncio
async def test_notice_hook_triggers_backup_for_kick(plugin):
    """机器人被踢要走触发式备份，且不在群里回话。"""
    event = _event(plugin)
    event.message_obj.type = "OtherMessage"
    event.message_obj.raw_message = {
        "post_type": "notice",
        "notice_type": "group_decrease",
        "sub_type": "kick_me",
        "group_id": 100,
    }
    await plugin._handle_notice(event)
    assert plugin.db.list_records(100)


@pytest.mark.asyncio
async def test_help_lists_restore_archive_form(plugin):
    """帮助里那句「备份包路径也能恢复」得和代码一致。"""
    from backup.core.commands import HELP_TEXT

    assert "备份包路径" in HELP_TEXT


def test_config_schema_matches_yaml():
    """`_conf_schema.json`（面板用）和 `config.yaml`（代码用）必须同源。

    两处默认值一旦漂移，用户就会看到「面板里写着 90 天，实际按 30 天清理」。
    """
    import json
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    config = (yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {})["backup"]

    def flatten_schema(node, prefix=""):
        out = {}
        for key, spec in node.items():
            if spec.get("type") == "object":
                out.update(flatten_schema(spec.get("items") or {}, f"{prefix}{key}."))
            else:
                out[f"{prefix}{key}"] = spec.get("default")
        return out

    def flatten_config(node, prefix=""):
        out = {}
        for key, value in node.items():
            if isinstance(value, dict):
                out.update(flatten_config(value, f"{prefix}{key}."))
            else:
                out[f"{prefix}{key}"] = value
        return out

    expected = flatten_schema(schema)
    actual = flatten_config(config)
    assert set(expected) == set(actual), set(expected) ^ set(actual)
    mismatched = {k: (expected[k], actual[k]) for k in expected if expected[k] != actual[k]}
    assert not mismatched, mismatched


def test_plugin_ships_marketplace_logo():
    """AstrBot 只认 `logo.png` 作为插件图标（`logo_fname = "logo.png"`）。

    只有 SVG 的话，插件市场里就是一个空白方块。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    logo = root / "logo.png"
    assert logo.is_file()
    head = logo.read_bytes()[:8]
    assert head == b"\x89PNG\r\n\x1a\n"
    assert (root / "assets" / "logo.svg").is_file()


def test_download_view_accepts_query_token(plugin, tmp_path):
    """面板按钮那条：固定路径 + query token。

    视图函数**不能**要求位置参数——`srv_plug_route` 是
    `await view_handler(*args, **kwargs)` 调的，而 `subpath` 已经被它自己吃掉
    当路由匹配用了，视图收到的是零个参数。所以这里显式走 `stream=True` 拿原始
    结果，另外用无参调用钉住「签名不会因为少传参数而 TypeError」。
    """
    from backup.core.download_route import make_view

    for i in range(2):
        from .conftest import make_message

        plugin.db.save_message(make_message(i), "bk1")
    result = plugin.exports.export(100, "zip", backup_id="bk1")
    view = make_view(plugin.exports, stream=True)

    class Request:
        args = {"token": result.token}
        path = "/api/plug/backup/download"
        headers = {}

    response = asyncio.run(view(Request()))
    assert _status_of(response) == 200
    assert response.headers["Content-Type"] == "application/zip"


def test_download_view_runs_without_arguments(plugin, monkeypatch):
    """宿主一个参数都不传也要能跑：没有 token 就 404，不能 TypeError。

    `status` 在装上 Werkzeug 之后是 `HTTPStatus` 枚举（`HTTPStatus.NOT_FOUND`），
    和 `404` 相等但单独的 `404` 不完全等于它——比较前统一转成 int。

    这一条是上一版的真 bug：`make_view` 生成的视图签名是 `view(request=None)`
    但里面又 `getattr(request, ...)` 之后直接把自定义数据类返回给 Flask——
    前者在真实调用里拿到的是 0 个参数（`srv_plug_route` 把 subpath 吃掉了），
    后者 Flask 序列化不了，结果就是面板里点下载 500。
    """
    from backup.core import download_route

    # 不在任何请求上下文里：找请求对象那一步不能抛
    monkeypatch.setattr(download_route, "_current_request", lambda: None)
    view = download_route.make_view(plugin.exports, stream=True)
    response = asyncio.run(view())
    assert _status_of(response) == 404


def test_register_download_api_is_optional(plugin):
    """宿主没有 register_web_api 时不能抛异常——注册不上不影响备份本身。"""

    class NoRegistrar:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, item):
            return getattr(self._inner, item)

    plugin.context = NoRegistrar(plugin.context)
    assert plugin.register_download_api() is False
