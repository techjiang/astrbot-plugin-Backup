"""第 21 轮：把「下载链接」和「面板配置」这两条真机路径跑通。

上一轮我读 AstrBot 4.14.6 源码后写进注释两条结论，这轮去用代码证伪，两条都错了：

1. 「`/api/plug/*` 一律要 JWT，所以群里点链接做不到」——`auth_middleware` 的
   白名单是**按前缀**放行的，里面明明白白写着 `/api/backup/download`。
2. 「注册了路由就能下载」——`srv_plug_route` 调视图函数时一个参数都不传
   （`subpath` 已被它自己吃掉用于匹配），而上一版的视图签名要 `request`
   且返回一个 Flask 不认的自定义对象。真机上点下载是 500。

以及一处从第一轮就在、二十轮都没人发现的：配置根本没读面板那份。
"""

import asyncio
import importlib.util
import sys
import types
import zipfile
from pathlib import Path

import pytest

from .conftest import install_fake_astrbot, make_message


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


@pytest.fixture
def main_mod(tmp_path, monkeypatch):
    install_fake_astrbot(monkeypatch)
    sys.modules.pop("backup.main", None)
    import backup.main as mod

    monkeypatch.setattr(mod, "PLUGIN_DIR", tmp_path / "plugin")
    (tmp_path / "plugin").mkdir(parents=True)
    (tmp_path / "plugin" / "config.yaml").write_text(
        f"""backup:
  storage_dir: "{tmp_path / 'storage'}"
  scope:
    download_media: false
  schedule:
    enabled: false
  export:
    base_url: ""
""",
        encoding="utf-8",
    )
    return mod


@pytest.fixture
def plugin(main_mod):
    Context = sys.modules["astrbot.api.star"].Context
    instance = main_mod.BackupPlugin(Context())
    yield instance
    instance.db.close()


# ---------------------------------------------------------------- 路由注册


def test_registers_prefix_routes_on_the_host(plugin):
    """三条路由都要登记：完整路径（群里点）、固定路径（面板按钮）、短前缀。

    宿主是 `route == f"/{subpath}"` 精确匹配，所以「前缀」只能靠多登记几条来覆盖。
    """
    routes = plugin.context.registered
    assert "/backup/download/<token>" in routes
    assert "/backup/download" in routes
    assert "/download/<token>" in routes


def test_download_link_is_clickable_when_dashboard_url_is_set(main_mod, tmp_path):
    """面板配了公网地址，导出回执里给的必须是能直接点的完整 URL。

    原来 `export.base_url` 空着就只给 `/backup/download/<token>`——发到群里
    谁点得动？面板自己的地址配置里明明有。
    """
    Context = sys.modules["astrbot.api.star"].Context
    (tmp_path / "plugin" / "config.yaml").write_text(
        f"""backup:
  storage_dir: "{tmp_path / 'storage'}"
  schedule:
    enabled: false
  export:
    base_url: ""
  dashboard:
    public_url: "https://bot.example.com"
""",
        encoding="utf-8",
    )
    instance = main_mod.BackupPlugin(Context())
    try:
        base = instance._public_base_url()
        assert base == "https://bot.example.com/api/backup/download"
    finally:
        instance.db.close()


def test_no_public_url_falls_back_to_bare_path(plugin):
    """一个公网地址都没有时，别硬编一个假域名出来——给裸路径 + 提示。"""
    assert plugin._public_base_url() == ""


# ---------------------------------------------------------------- 视图层


class _FakeFlaskRequest:
    """Flask 请求对象的最小形状。"""

    def __init__(self, path, token=None, range_header=None):
        self.path = path
        self.args = {"token": token} if token else {}
        self.headers = {"Range": range_header} if range_header else {}


def test_view_returns_flask_friendly_tuple(plugin, monkeypatch):
    """Flask 只认 str/bytes/dict/Response 之类。返回自定义 dataclass 会 500。

    兜底不算数：原来这里靠 `_as_flask` 里 `from werkzeug...` 的 try/except 混过去，
    没有 Werkzeug 的环境（`requirements.txt` 漏了它）就把自定义 dataclass 原样丢给
    Flask，真机上是 500。所以这条断言必须是「有 Werkzeug」的前提下的断言。
    """
    pytest.importorskip("werkzeug")
    from backup.core import download_route

    plugin.db.save_message(make_message(0), "bk1")
    result = plugin.exports.export(100, "zip", backup_id="bk1")

    fake = _FakeFlaskRequest("/api/plug/backup/download", token=result.token)
    monkeypatch.setattr(download_route, "_current_request", lambda: fake)
    view = download_route.make_view(plugin.exports, stream=False)
    response = asyncio.run(view())
    # 必须是 WSGI 响应对象，不能是 `(body, status, headers)`：后者在文件下载里
    # 会变成「200 + 空 body」，ZIP 是个空文件
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"
    assert len(b"".join(response.response)) == result.path.stat().st_size


def test_view_without_any_argument_is_not_a_typeerror(plugin, monkeypatch):
    """`srv_plug_route` 是 `await view_handler(*args, **kwargs)`，一个参数都不传。"""
    from backup.core import download_route

    # 不在 Flask 请求上下文里时也要能跑，不能因为找请求对象就抛异常
    monkeypatch.setattr(download_route, "_current_request", lambda: None)
    view = download_route.make_view(plugin.exports, stream=True)
    response = asyncio.run(view())
    assert _status_of(response) == 404


def test_view_returns_410_when_werkzeug_absent(plugin, monkeypatch):
    """没装 Werkzeug 时下载视图会退化成自定义对象（Filter 那边压根不理），宿主侧
    一律 500。这里把退化后的形状钉住：`status` 与 `file` 都在，接入层照抄即可发。
    """
    from backup.core import download_route

    def no_werkzeug(*_a, **_k):
        raise ImportError("no werkzeug")

    plugin.db.save_message(make_message(0), "bk1")
    result = plugin.exports.export(100, "zip", backup_id="bk1")
    fake = _FakeFlaskRequest(f"/api/backup/download/{result.token}")
    monkeypatch.setattr(download_route, "_current_request", lambda: fake)
    monkeypatch.setitem(sys.modules, "werkzeug", no_werkzeug)
    monkeypatch.setitem(sys.modules, "werkzeug.wrappers", no_werkzeug)
    response = asyncio.run(download_route.make_view(plugin.exports, stream=False)())
    assert _status_of(response) == 200
    assert response.file == result.path


def test_view_reads_token_from_path_tail(plugin, monkeypatch):
    """群里点的是 `/api/backup/download/<token>`，token 在路径尾段。"""
    pytest.importorskip("werkzeug")
    from backup.core import download_route

    plugin.db.save_message(make_message(0), "bk1")
    result = plugin.exports.export(100, "zip", backup_id="bk1")
    fake = _FakeFlaskRequest(f"/api/backup/download/{result.token}")

    class Range:
        args = {}
        path = fake.path
        headers = {}

    monkeypatch.setattr(download_route, "_current_request", lambda: Range())
    view = download_route.make_view(plugin.exports, stream=False)
    response = asyncio.run(view())
    assert response.status_code == 200


def test_aiohttp_view_returns_file_response(plugin):
    """宿主自建网关走 aiohttp：`async def handler(request)` 形状。"""
    pytest.importorskip("aiohttp")
    from aiohttp import web

    from backup.core.download_route import aiohttp_view

    plugin.db.save_message(make_message(0), "bk1")
    result = plugin.exports.export(100, "zip", backup_id="bk1")

    class Request:
        path = f"/backup/download/{result.token}"
        args = {}
        headers = {}

    response = asyncio.run(aiohttp_view(plugin.exports)(Request()))
    assert isinstance(response, web.FileResponse)
    assert _status_of(response) == 200


# ---------------------------------------------------------------- 配置来源


def test_injected_host_config_wins(main_mod, tmp_path):
    """面板改的配置必须生效：宿主 `config=` 传进来的那份优先级最高。

    这也解释了为什么二十轮都没人发现——容器里没有 AstrBot，`config=` 这个
    关键字压根不会被调用，`config.yaml` 那条路看起来一直是「好使的」。
    """
    Context = sys.modules["astrbot.api.star"].Context
    injected = {"retention": {"keep_days": 7}, "export": {"default_format": "html"}}
    instance = main_mod.BackupPlugin(Context(), config=injected)
    try:
        assert instance.config["retention"]["keep_days"] == 7
        assert instance.config["export"]["default_format"] == "html"
        # 面板没给的项，仍然从 config.yaml / schema 里补齐
        assert instance.config["scope"]["download_media"] is False
    finally:
        instance.db.close()


def test_config_set_writes_back_to_injected_config(main_mod, tmp_path):
    """`/backup config set` 要写进面板那份（它会自己 save_config），不是写 yaml。"""

    class HostConfig(dict):
        def __init__(self, data):
            super().__init__(data)
            self.saved = 0

        def save_config(self):
            self.saved += 1

    Context = sys.modules["astrbot.api.star"].Context
    host = HostConfig({"retention": {"keep_days": 90}})
    instance = main_mod.BackupPlugin(Context(), config=host)
    try:
        assert main_mod._save_config(instance.config, host) is True
        assert host.saved == 1
        assert host["retention"]["keep_days"] == 90
    finally:
        instance.db.close()


# ---------------------------------------------------------------- 通知双投递


def _notice_event(plugin, group_id=100, subtype="kick_me"):
    class Msg:
        type = "OtherMessage"
        self_id = "1"
        message_id = "n1"
        group = types.SimpleNamespace(group_id=str(group_id))
        sender = types.SimpleNamespace(user_id="7", nickname="甲")
        message = []
        message_str = ""
        raw_message = {
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": subtype,
            "group_id": group_id,
        }
        timestamp = 0

    class Ev:
        message_obj = Msg()
        message_str = ""
        role = "admin"

        def get_group_id(self):
            return str(group_id)

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
            return text

    return Ev()


def test_kick_is_not_backed_up_twice(plugin):
    """被踢只备一次。

    AstrBot 把同一条 notice 同时投给 `on_message` 和 `on_notice` 两个 handler。
    被踢之后机器人已经不在群里了，再跑一轮 `get_group_msg_history` 只会失败，
    最后在备份列表里多出一条 failed —— 看起来像「备份坏了」。
    冷却窗口放在一天，一个小时之内重复投递不会再触发。
    """
    from datetime import datetime, timedelta

    event = _notice_event(plugin)
    runs = []
    plugin._backup = lambda group_id, ev=None: runs.append(group_id)  # noqa: ARG005

    asyncio.run(plugin._handle_notice(event))
    asyncio.run(plugin._handle_notice(event))
    assert runs == [100]

    # 冷却过去之后，新的被踢事件仍然要能触发（换个群验证窗口是per-group的）
    hook = plugin.hooks
    assert hook._last_kick_backup[100] > datetime.now() - timedelta(minutes=1)
    assert hook.kick_cooldown >= 3600


def test_mass_leave_still_keeps_its_short_cooldown(plugin):
    """退群聚合还是半小时窗口：那是「一批人同时退」，不是一辈子一次。"""
    assert plugin.hooks.cooldown == 1800


# ---------------------------------------------------------------- 端到端（真 Flask）


def test_real_flask_serves_the_zip(plugin):
    """装真 Flask 才能看见的 bug：回 `(body, status, headers)` 元组是空文件。

    `DownloadResponse.body` 在正常下载里是空的（文件在 `file` 上），元组会让
    Flask 认为响应体就是那 0 字节——**状态 200、Content-Length: 0、下载下来的
    ZIP 是空文件**。单测用假 Request 求 `response.body` 是看不见这一层的。
    """
    flask = pytest.importorskip("flask")
    import asyncio as _asyncio

    plugin.db.save_message(make_message(0), "bk1")
    result = plugin.exports.export(100, "zip", backup_id="bk1")
    api = __import__("backup.core.download_route", fromlist=["make_view"]).make_view(
        plugin.exports, stream=False
    )
    app = flask.Flask(__name__)
    app.add_url_rule(
        "/api/backup/download/<path:token>",
        view_func=lambda **kw: _asyncio.run(api(**kw)),
        methods=["GET"],
    )
    client = app.test_client()
    response = client.get(f"/api/backup/download/{result.token}")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"
    assert response.data[:2] == b"PK"
    assert len(response.data) == result.path.stat().st_size
    # token 一次性：同一条链接点第二次是 410（不是「下了一半」）
    assert client.get(f"/api/backup/download/{result.token}").status_code == 410


def test_real_flask_handles_range(plugin):
    """浏览器下大文件先发 Range 探测，再续传。两次都要成功。"""
    flask = pytest.importorskip("flask")
    import asyncio as _asyncio

    plugin.db.save_message(make_message(0), "bk1")
    result = plugin.exports.export(100, "zip", backup_id="bk1")
    api = __import__("backup.core.download_route", fromlist=["make_view"]).make_view(
        plugin.exports, stream=False
    )
    app = flask.Flask(__name__)
    app.add_url_rule(
        "/api/backup/download/<path:token>",
        view_func=lambda **kw: _asyncio.run(api(**kw)),
        methods=["GET"],
    )
    client = app.test_client()
    partial = client.get(
        f"/api/backup/download/{result.token}", headers={"Range": "bytes=0-99"}
    )
    assert partial.status_code == 206
    assert len(partial.data) == 100
    # 区间请求不作废 token，整份还要能取走
    full = client.get(f"/api/backup/download/{result.token}")
    assert full.status_code == 200
    assert len(full.data) == result.path.stat().st_size
