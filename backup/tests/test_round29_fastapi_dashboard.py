"""用**真 FastAPI 面板**的转发链路跑一遍下载请求。

这个文件存在的理由，和前几轮的教训是同一件事：前二十九轮里，下载相关的用例
一直拿我照 Quart 旧面板写的假 Request 喂视图。AstrBot 新面板换成了 FastAPI，
插件路由挂在 `/api/v1/plugins/extensions/<插件名>/` 下，转发逻辑是
`astrbot/dashboard/api/plugins.py::_call_plugin_extension` + `asgi_runtime.py`
里那两个 contextvar。这些形状一条都不是我假想出来的，所以不能拿假想的东西去验。

装上 `fastapi` + `astrbot` 就整组可跑（`PYTHONPATH=<astrbot 源码>`），否则 skip。

修之前实测（同一个脚本、同一条请求）：

    群成员点 /api/v1/plugins/extensions/backup/backup/download/<token>
      → 200 application/json  {"status":200,"body":{...}}   ← 下载下来是一个 JSON
    带上 Range 续传
      → 200（该是 206），body 还是那个 JSON
    token 无效
      → 200 包着 {"status":410}                              ← 状态码埋在 body 里
"""

import asyncio

import pytest

from .conftest import real_astrbot_available

fastapi = pytest.importorskip("fastapi", reason="需要真 fastapi")
pytest.importorskip("httpx", reason="需要 httpx 的 ASGITransport")
if not real_astrbot_available():
    pytest.skip("需要真 AstrBot", allow_module_level=True)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import Response  # noqa: E402

from astrbot.api.web import PluginRequest, bind_request_context  # noqa: E402
from astrbot.dashboard.api import plugins as plugins_api  # noqa: E402
from astrbot.dashboard.asgi_runtime import FastAPIAppAdapter  # noqa: E402

from backup.core.database import Database  # noqa: E402
from backup.core.download_route import make_view  # noqa: E402
from backup.core.export_manager import ExportManager  # noqa: E402

PAYLOAD = b"PK" + b"x" * 1000


@pytest.fixture()
def host(tmp_path):
    """照 AstrBot 新版面板的样子搭一台宿主：路由挂载点、contextvar、coerce。"""
    db = Database(str(tmp_path / "t.db"))
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
    archive = tmp_path / "exports" / "a.zip"
    archive.write_bytes(PAYLOAD)

    app = FastAPI()
    adapter = FastAPIAppAdapter(app)
    adapter.config = {}
    star_context = type("SC", (), {"registered_web_apis": []})()
    app.state.dashboard_app_adapter = adapter
    app.state.core_lifecycle = type("LC", (), {"star_context": star_context})()

    view = make_view(exports)
    for route in (
        "/backup/backup/download",
        "/backup/backup/download/<token>",
        "/backup/download",
    ):
        star_context.registered_web_apis.append((route, view, ["GET"], "download"))

    @app.get("/api/v1/plugins/extensions/{plugin_path:path}")
    async def extension(plugin_path: str, request: Request):
        """面板真实的转发：绑 `PluginRequest` 上下文，再叫插件视图。"""
        plugin_request = PluginRequest(
            request, path_params={}, plugin_name="backup", username="admin"
        )
        with bind_request_context(plugin_request):
            return await plugins_api._call_plugin_extension(
                plugin_path, request, "admin"
            )

    class Host:
        def __init__(self):
            self.app = app
            self.exports = exports
            self.db = db
            self.archive = archive

        async def get(self, url, headers=None):
            from httpx import ASGITransport, AsyncClient

            async with AsyncClient(
                transport=ASGITransport(app=self.app), base_url="http://bot"
            ) as client:
                return await client.get(url, headers=headers or {})

    yield Host()
    db.close()


def run(coro):
    return asyncio.run(coro)


BASE = "/api/v1/plugins/extensions/backup/backup/download"


def test_group_member_click_gets_the_real_file(host):
    """群里点链接：拿到的是 ZIP 本身，不是「一个描述下载的 JSON」。"""
    token, _ = host.exports._issue_token(host.archive)
    resp = run(host.get(f"{BASE}/{token}"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.content == PAYLOAD


def test_token_in_query_works_too(host):
    """只有固定前缀那条路由时，token 只能从 query 里拿（新版面板就是这样）。"""
    token, _ = host.exports._issue_token(host.archive)
    resp = run(host.get(f"{BASE}?token={token}"))
    assert resp.status_code == 200
    assert resp.content == PAYLOAD


def test_range_request_returns_206(host):
    """浏览器下大文件先探一段再续传：必须是 206 + 切片，不是 200 + 整份。"""
    token, _ = host.exports._issue_token(host.archive)
    resp = run(host.get(f"{BASE}/{token}", headers={"Range": "bytes=0-9"}))
    assert resp.status_code == 206
    assert resp.content == PAYLOAD[:10]
    assert "content-range" in {k.lower() for k in resp.headers}


def test_expired_token_is_410_not_200(host):
    """链接失效要真的回 410。

    修之前状态码只写在 `DownloadResponse.status` 上，Starlette 那一层把它当普通
    对象序列化，HTTP 状态永远是 200——用户看到「下下来了」，文件里是 `{}`。
    """
    resp = run(host.get(f"{BASE}/nope"))
    assert resp.status_code == 410
    assert "text/plain" in resp.headers["content-type"]


def test_missing_path_is_404(host):
    resp = run(host.get(BASE))
    assert resp.status_code == 404


def test_legacy_mount_still_accepts_fixed_prefix(host):
    """旧面板的形状（`/api/plug/backup/download?token=`）不能因为改新版而坏掉。"""
    token, _ = host.exports._issue_token(host.archive)
    plugin_request_route = f"/api/plug/backup/download?token={token}"

    @host.app.get("/api/plug/{plugin_path:path}")
    async def legacy(plugin_path: str, request: Request):  # pragma: no cover
        plugin_request = PluginRequest(
            request, path_params={}, plugin_name="backup", username="admin"
        )
        with bind_request_context(plugin_request):
            return await plugins_api._call_plugin_extension(
                plugin_path, request, "admin"
            )

    resp = run(host.get(plugin_request_route))
    assert resp.status_code == 200
    assert resp.content == PAYLOAD


def test_link_prefix_follows_the_host_mount(monkeypatch):
    """路由前缀跟着宿主挂载点走：写死旧版 `/api/plug`，新版面板上点开是 404。"""
    from backup import main as main_mod

    monkeypatch.setattr(main_mod, "_astrbot_extensions_mount", lambda: True)
    plugin = object.__new__(main_mod.BackupPlugin)
    assert plugin._host_mount() == (
        "/api/v1/plugins/extensions/backup",
        "/backup/download",
    )

    monkeypatch.setattr(main_mod, "_astrbot_extensions_mount", lambda: False)
    assert plugin._host_mount() == ("/api/plug", "/backup/download")
