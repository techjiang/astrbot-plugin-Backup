"""内置网关：群内那条下载链接到底通不通。

这个文件存在的理由，和 round29 是同一件事的两面。round29 假设「宿主面板能把
插件路由挂在 `/api/v1/plugins/extensions/...` 上」——那是**新版 FastAPI 面板**
的形状，而 AstrBot 4.14.6 装出来的面板还是 Quart，`astrbot.api.web` 根本不存在。

于是「把链接挂在宿主面板上」这件事，在**当前真实可用的那版宿主**上没有验过。
这轮把它验了，结论是两条硬约束一起把那条路堵死（见 `core/gateway.py` 顶部）。
所以这里不测「注册了几条路由」——注册成功和真能下到文件是两件事——只测：

1. 宿主那套匹配/鉴权规则，真的会让插件路由拿不到文件（用源码里的规则复现）；
2. 内置网关在**真 HTTP 请求**下能下到文件、能续传、过期给 410；
3. 链接拼装指向那个真能用的入口。
"""

import socket
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from .conftest import real_astrbot_available


def _run(coro):
    import asyncio

    return asyncio.run(coro)

# ---------------------------------------------------------------- 宿主规则复现


def _astrbot_source():
    """取 AstrBot 的 dashboard 源码文件。没装就 skip。"""
    try:
        import astrbot.dashboard.server as server
    except Exception:
        pytest.skip("需要真 AstrBot")
    return Path(server.__file__)


def test_plugin_route_with_path_param_can_never_match():
    """`srv_plug_route` 拿 `route == f"/{subpath}"` 比字面量，带参的路由永远比不中。

    这不是「我的猜测」：规则直接取自宿主源码。用同样的比较把注册过的形状逐个
    试一遍，确认带 `<token>` 的那几条在真实 subpath 上全部落空——也就是群里点
    链接落到的是宿主的「未找到该路由」。
    """
    source = _astrbot_source().read_text(encoding="utf-8")
    assert 'route == f"/{subpath}"' in source, "宿主的匹配方式变了，这里的结论要重新核"

    from backup.main import BackupPlugin

    registered = list(BackupPlugin.DOWNLOAD_ROUTES)
    real_subpaths = [
        "backup/download/AbCdEf123",  # 群里点的就是这一条
        "download/AbCdEf123",
        "backup/backup/download/AbCdEf123",
    ]

    def host_matches(subpath):
        return [r for r in registered if r == f"/{subpath}"]

    for subpath in real_subpaths:
        assert host_matches(subpath) == [], (
            f"{subpath} 居然命中了——宿主的匹配方式变了，网关的必要性要重新评估"
        )


def test_plugin_routes_are_outside_the_auth_whitelist():
    """插件能挂的 `/api/plug/*` 不在免鉴权白名单里；白名单里那条是宿主自己的。"""
    source = _astrbot_source().read_text(encoding="utf-8")
    # 白名单是源码里的常量，插件无法扩展
    assert "allowed_endpoints" in source
    assert '"/api/backup/download"' in source

    allowed = ["/api/auth/login", "/api/file", "/api/platform/webhook", "/api/stat/start-time", "/api/backup/download"]

    def whitelisted(path):
        return any(path.startswith(prefix) for prefix in allowed)

    # 插件能挂的地方：一律要面板 JWT，而群里点链接的人没有
    assert not whitelisted("/api/plug/backup/download/tok")
    # 唯一免鉴权的那条前缀属于宿主：BackupRoute 已经占了 `/backup/download`
    backup_route_src = Path(
        __import__("astrbot.dashboard.routes.backup", fromlist=["BackupRoute"]).__file__
    ).read_text(encoding="utf-8")
    assert '"/backup/download": ("GET"' in backup_route_src, (
        "宿主不再占用 /api/backup/download，插件的方案可以重议"
    )
    assert whitelisted("/api/backup/download")


# ---------------------------------------------------------------- 网关真跑


@pytest.fixture()
def live_gateway(tmp_path):
    """一个真的监听在回环端口上的网关。"""
    from backup.core.database import Database
    from backup.core.export_manager import ExportManager
    from backup.core.gateway import DownloadGateway

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    db = Database(str(tmp_path / "t.sqlite"))
    exports = ExportManager(db, export_dir, tmp_path / "media", {})
    gateway = DownloadGateway(exports, "127.0.0.1", 0)
    assert gateway.start(), "网关起不来"
    try:
        yield gateway, exports, export_dir
    finally:
        gateway.stop()
        db.close()


def _issue(exports, export_dir: Path, name: str, payload: bytes) -> str:
    target = export_dir / name
    target.write_bytes(payload)
    token, _expires = exports._issue_token(target)
    return token


PAYLOAD = b"PK\x03\x04" + bytes(range(256)) * 60


def test_gateway_serves_the_file_over_real_http(live_gateway):
    """一条真 HTTP 请求，拿回来的字节必须和导出的完全一样。"""
    gateway, exports, export_dir = live_gateway
    token = _issue(exports, export_dir, "a.zip", PAYLOAD)

    with urllib.request.urlopen(gateway.url_for(token), timeout=5) as resp:
        body = resp.read()
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/zip"
        assert resp.headers["Accept-Ranges"] == "bytes"
    assert body == PAYLOAD


def test_gateway_supports_range_resume(live_gateway):
    """浏览器下大文件一定先探测再生效，回不了 206 就是「下了一半断了」。"""
    gateway, exports, export_dir = live_gateway
    token = _issue(exports, export_dir, "b.zip", PAYLOAD)
    url = gateway.url_for(token)

    probe = urllib.request.Request(url, headers={"Range": "bytes=0-"})
    with urllib.request.urlopen(probe, timeout=5) as resp:
        assert resp.status == 206
        assert resp.headers["Content-Range"] == f"bytes 0-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
        assert resp.read() == PAYLOAD

    # 同一个 token 的后续片段：不算「又领走一份」
    again = urllib.request.Request(url, headers={"Range": "bytes=100-199"})
    with urllib.request.urlopen(again, timeout=5) as resp:
        assert resp.status == 206
        assert resp.read() == PAYLOAD[100:200]


def test_gateway_returns_410_for_unknown_token(live_gateway):
    """token 不对 = 过期或已用过，410。带上 token 的形状，别让用户以为链接拼错了。"""
    gateway, _exports, _dir = live_gateway
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(gateway.url_for("nosuchtoken"), timeout=5)
    assert exc.value.code == 410


def test_gateway_returns_404_when_there_is_no_token_at_all(live_gateway):
    """空 token 是「路径不对」，不是「过期」——410 会把人引导去查有效期。"""
    gateway, _exports, _dir = live_gateway
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(gateway.url_for(""), timeout=5)
    assert exc.value.code == 404


def test_gateway_rejects_paths_that_are_not_a_download(live_gateway):
    """不能把网关变成一个能列目录的东西。"""
    gateway, _exports, _dir = live_gateway
    root = f"http://127.0.0.1:{gateway.bound_port}"
    for path in ("/", "/backup/", "/backup/download/", "/exports/a.zip"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(root + path, timeout=5)
        assert exc.value.code == 404, path


def test_gateway_does_not_log_tokens(live_gateway, caplog):
    """token 就是一把钥匙，不该留在日志里（红线 1 的同一条道理）。"""
    gateway, exports, export_dir = live_gateway
    token = _issue(exports, export_dir, "c.zip", PAYLOAD)
    caplog.clear()
    with urllib.request.urlopen(gateway.url_for(token), timeout=5) as resp:
        resp.read()
    assert token not in caplog.text


def test_gateway_stop_releases_the_port(live_gateway):
    gateway, _exports, _dir = live_gateway
    port = gateway.bound_port
    gateway.stop()
    assert gateway.running is False
    # 端口真被放开了，不是「对象说停了但其实还占着」
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def test_gateway_start_failure_is_not_fatal(tmp_path):
    """端口被占只意味着「这条链接点不动」，不该让插件起不来。"""
    from backup.core.database import Database
    from backup.core.export_manager import ExportManager
    from backup.core.gateway import DownloadGateway

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    db = Database(str(tmp_path / "t.sqlite"))
    try:
        exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
        gateway = DownloadGateway(exports, "127.0.0.1", busy_port)
        assert gateway.start() is False
        assert gateway.running is False
    finally:
        blocker.close()
        db.close()


# ---------------------------------------------------------------- 接进插件


def test_export_receipt_never_hands_out_a_loopback_link(tmp_path, monkeypatch):
    """没配 `export.base_url` 时，回执里**不能**出现 `127.0.0.1`。

    第 30 轮这里的断言写的是 `link.startswith("http://127.0.0.1:<端口>/")`——
    也就是说，用例亲手把「发进群的链接是回环地址」这件事锁成了正确结果。
    群成员点的是他自己电脑的 127.0.0.1，一辈子点不开，而回执看起来像条正常
    链接。第 32 轮修过一次（`_public_base_url` 没有调用点），但只修了
    「没有网关」那条分支，网关一起来又原样漏着。

    现在：没有对外地址就只给裸路径 + 一句说明，宁可承认点不动。
    """
    from .conftest import load_plugin_for_real

    if not real_astrbot_available():
        pytest.skip("需要真 AstrBot")

    monkeypatch.setenv("HOME", str(tmp_path))
    plugin, _context, _mod = load_plugin_for_real(monkeypatch)
    try:
        assert plugin.gateway.running
        link = plugin.download_link("tok")
        assert "127.0.0.1" not in link and "localhost" not in link
        assert link == "/backup/download/tok"

        # 配了对外地址之后才是一条真链接
        plugin.config.setdefault("export", {})["base_url"] = "https://bot.example.com"
        link2 = plugin.download_link("tok")
        assert link2 == "https://bot.example.com/backup/download/tok"

        # 这条消息是从哪个域名进来的，就用哪个域名（反代/穿透下唯一现成的真相）
        plugin.config["export"]["base_url"] = ""
        event = types.SimpleNamespace(
            request_headers={"Host": "bot.example.com", "X-Forwarded-Proto": "https"}
        )
        assert plugin.download_link("tok", event) == "https://bot.example.com/backup/download/tok"

        # 内网地址一律不认：拼出来发进群里也是没人点得动
        lan = types.SimpleNamespace(request_headers={"Host": "192.168.1.9:6185"})
        assert plugin.download_link("tok", lan) == "/backup/download/tok"
    finally:
        plugin.gateway.stop()
        plugin.db.close()


def test_terminate_stops_the_gateway(tmp_path, monkeypatch):
    """插件卸载后不能留一个还在监听端口的线程。"""
    import asyncio

    from .conftest import load_plugin_for_real

    if not real_astrbot_available():
        pytest.skip("需要真 AstrBot")

    monkeypatch.setenv("HOME", str(tmp_path))
    plugin, _context, _mod = load_plugin_for_real(monkeypatch)
    port = plugin.gateway.bound_port
    assert port > 0
    asyncio.run(plugin.terminate())
    assert plugin.gateway.running is False
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


# ---------------------------------------------------------------- 端到端


def test_end_to_end_export_then_download_over_http(tmp_path, monkeypatch):
    """真插件 → 真导出 → 真 HTTP 下载 → 字节一致。

    这一条把「链接能用」落到最实在的地方：群成员点开之后拿到的 ZIP，必须和导出
    出来的那个文件逐字节相同。前几轮的用例都停在「视图返回了 200」「URL 拼对了」，
    从没有人把两边的内容比过一次。
    """
    import urllib.error

    from .conftest import load_plugin_for_real
    from backup.core.models import GroupInfo, Message, MessageType
    from datetime import datetime

    if not real_astrbot_available():
        pytest.skip("需要真 AstrBot")

    monkeypatch.setenv("HOME", str(tmp_path))
    plugin, _context, _mod = load_plugin_for_real(monkeypatch)
    try:
        assert plugin.gateway.running
        db = plugin.db
        for i in range(5):
            db.save_message(
                Message(
                    message_id=f"m{i}",
                    group_id=100,
                    sender_id=1,
                    sender_name="小明",
                    timestamp=datetime(2026, 11, 5, 12, i),
                    message_type=MessageType.TEXT,
                    content=f"第{i}条",
                    raw_content=f"第{i}条",
                ),
                backup_id="realtime",
            )
        db.save_group_info(
            GroupInfo(group_id=100, group_name="测试群", owner_id=1, member_count=2, max_members=200)
        )

        result = plugin.exports.export_latest(100, "zip")
        link = plugin.download_link(result.token)

        # 拿网关自己的回环地址去发请求——这是**测试**在做的事（和用户在同一台机器
        # 上），不代表这条链接能直接发进群。回执给的是裸路径，见上一条用例。
        assert link == f"/backup/download/{result.token}"
        local = plugin.gateway.url_for(result.token)
        with urllib.request.urlopen(local, timeout=10) as resp:
            assert resp.status == 200
            downloaded = resp.read()
        assert downloaded == result.path.read_bytes(), "下下来的 ZIP 和导出文件不一致"

        # 整份取走之后 token 作废：链接是一次性的，转发出去不能被反复领
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(local, timeout=10)
        assert exc.value.code == 410

        # 换一条新 token：浏览器的 Range 探测 + 续传都该是 206。
        # 注意按**这一次**导出的长度算——两次导出中间隔了一段时间戳，ZIP 字节数
        # 未必相同，拿前一份的长度去比会假失败。
        again = plugin.exports.export_latest(100, "zip")
        link2 = plugin.gateway.url_for(again.token)
        total2 = again.path.stat().st_size
        for rng, expect_len in (("bytes=0-", total2), ("bytes=100-199", 100)):
            req = urllib.request.Request(link2, headers={"Range": rng})
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 206
                assert len(resp.read()) == expect_len
    finally:
        plugin.gateway.stop()
        plugin.db.close()


async def test_receipt_link_is_downloadable_after_filling_base_url(tmp_path, monkeypatch):
    """`export.base_url` 填完之后，**回执里那条字符串本身**要能下载。

    这一条补的是我这轮亲自踩的坑：前面所有断言的落点是「链接拼得对不对」和
    「拿网关自己的 `url_for()` 能不能下」——后者绕开了回执。于是「用户照着回执
    复制走的那串字」从来没有被当成 URL 请求过一次。真跑一遍才发现：填了
    `base_url` 之后回执给的是 `http://127.0.0.1:<端口>/backup/download/<token>`，
    而网关确实就在那个端口上——**这条链接是能用的**（本机场景），只是它长得像
    上一轮被判定为「点不动」的那条。

    两件事必须同时成立，缺一个都说明 `base_url` 没接到底：
    - 回执里出现 base_url，而不是裸路径；
    - 拿回执里那串字直接请求，拿到的字节和导出文件一致。
    """
    import urllib.request

    from .conftest import load_plugin_for_real, real_astrbot_available

    if not real_astrbot_available():
        pytest.skip("需要真 AstrBot")

    monkeypatch.setenv("HOME", str(tmp_path))

    class _HostConfig(dict):
        """面板注入的那份（AstrBotConfig 的契约：dict + save_config）。

        必须注入：不注入时 `_save_config` 会退回写插件目录下的 `config.yaml`，
        跑一次用例就把仓库里的默认配置改了（还会把注释洗掉）。第一版这条用例
        就是这么把所有「默认配置」相关断言一起搞红的。
        """

        def save_config(self):
            return True

    plugin, _context, _mod = load_plugin_for_real(
        monkeypatch,
        config=_HostConfig({"storage_dir": str(tmp_path / "storage")}),
    )
    try:
        from datetime import datetime

        from backup.core.models import Message, MessageType

        for i in range(3):
            plugin.db.save_message(
                Message(
                    message_id=f"m{i}",
                    group_id=10001,
                    sender_id=1,
                    sender_name="小明",
                    timestamp=datetime(2026, 11, 5, 12, i),
                    message_type=MessageType.TEXT,
                    content=f"第{i}条",
                    raw_content=f"第{i}条",
                ),
                backup_id="realtime",
            )

        # 没配 base_url：裸路径 + 明说点不动。这几句的判据是「链接必须以 / 开头」，
        # 不是「链接里不能出现 127.0.0.1」——宿主自己给的 Host 本来就可能带它
        async def say(text):
            event = types.SimpleNamespace(
                message_str=text,
                get_group_id=lambda: "10001",
                is_private_chat=lambda: False,
                is_admin=lambda: True,
                get_sender_id=lambda: "7",
                plain_result=lambda t: t,
            )
            return [r async for r in plugin.backup(event)]

        first = (await say("backup export 10001 zip"))[0]
        link_line = [ln for ln in first.splitlines() if "临时链接" in ln][0]
        bare = link_line.split("：")[-1]
        assert bare.startswith("/backup/download/"), bare
        assert "⚠️" in first, "裸路径必须配一句「点不动」，否则用户以为已经能分享了"

        # 填上对外地址（这里就用网关自己的地址，充当"反代已就位"）
        base = f"http://127.0.0.1:{plugin.gateway.bound_port}"
        await say(f"backup config set export.base_url {base}")
        assert plugin.exports.base_url == base

        second = (await say("backup export 10001 zip"))[0]
        full = [ln for ln in second.splitlines() if "临时链接" in ln][0].split("：")[-1]
        assert full.startswith(base + "/backup/download/"), full
        assert "⚠️" not in second, "链接已经能用了，不该再挂「点不动」的提示"

        with urllib.request.urlopen(full, timeout=10) as resp:
            assert resp.status == 200
            downloaded = resp.read()
        assert downloaded.startswith(b"PK"), "下回来的不是 ZIP"
        # 一次性：第二次点回 410
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(full, timeout=10)
        assert exc.value.code == 410
    finally:
        plugin.gateway.stop()
        plugin.db.close()
