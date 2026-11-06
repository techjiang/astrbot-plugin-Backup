"""第 36 轮：网关换个实例就起不来，而且日志说的是错的。

起点不是读代码，是把**插件真加载两遍**——这是宿主在「面板点了重载」「改了
`gateway.port`」「进程重启」时做的事。`core/gateway.py` 的 `start()` 只有一句
`except OSError`，不管绑不上的原因是「端口被别人占着」还是「主机名写错、
端口越界」，都落一句 `下载网关启动失败 ...: OSError`。

三处：

1. 宿主重载插件时，如果旧实例的 `terminate()` 没被调到（旧实例还在，端口还
   占着），`start()` 绑定失败 → `_httpd` 留在 None → `gateway.running` 是
   False，而**配置里明明写着 enabled=true**。上层于是给出「网关没起来」的提示，
   而真正的错因是「端口被占」，提示里一个字都没提。
2. 日志 `网关启动失败 %s:%s：%s` 里，`host` 打的是**配置里的** host（默认
   `127.0.0.1`），`port` 打的是 `self.port`——端口写 0 时那是「让系统挑」，
   打出来是 `127.0.0.1:0`。看日志的人会以为配错了，而实际端口根本不是 0。
3. `_public_base_url` 是**第二个**「写全了、没有任何调用点」——上一轮修掉的是
   `_public_base_url` 里面的 `_host_from_request`（它有调用点了），函数本身
   依然全仓只有它自己的定义和用例。它拼的是**面板那条路**的链接前缀
   （`/api/plug/backup/download`），而面板那条路在 4.14 上已经被证伪（见
   `download_route.py` 末尾），真正的链接由 `download_link` 拼网关那条。留着
   它只会让下一个人以为还有第二条路可走。
"""

import asyncio
import socket

import pytest

from .conftest import load_plugin_for_real, real_astrbot_available

pytestmark = pytest.mark.skipif(
    not real_astrbot_available(), reason="需要真 AstrBot（pip install astrbot==4.14.6）"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _config(tmp_path, port: int, **extra) -> dict:
    """固定端口的意义就在这：`base_url` 填了对外的 `域名:端口`，网关只能绑那个。"""
    cfg = {
        "storage_dir": str(tmp_path),
        "gateway": {"enabled": True, "host": "127.0.0.1", "port": port},
    }
    cfg.update(extra)
    return cfg


def _run(coro):
    """单独跑一个协程。

    不用 `get_event_loop()`：整轮跑下来、前面有用例已经 `asyncio.run` 过之后，
    主线程上就没有当前事件循环了，那会抛 `RuntimeError: There is no current
    event loop`。`asyncio.run` 每次都新建一个，跑完即关。
    """
    return asyncio.run(coro)


def _fresh(cfg: dict) -> dict:
    """每次加载都要一份新的 dict：插件会往 `gateway` 这层写东西。"""
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in cfg.items()}


# ---------------------------------------------------------------------------
# 1. 端口被别人占着：得说「被占用」，不能只说「起不来」
# ---------------------------------------------------------------------------


def test_port_taken_says_so_instead_of_a_bare_oserror(monkeypatch, tmp_path, caplog):
    """端口占着时，日志里要出现「端口」和真正的端口号，而不是 `:0`。

    旧代码：`logger.warning("下载网关启动失败 %s:%s：%s", host, self.port, type)`，
    配的端口是 0（默认，让系统挑），打出来就是 `127.0.0.1:0`——而实际想绑的是
    系统挑出来的那个，用户看到的和被占用的一点关系都没有。
    """
    port = _free_port()
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", port))
    squatter.listen(1)
    try:
        caplog.clear()
        plugin, _ctx, _mod = load_plugin_for_real(
            monkeypatch, config=_config(tmp_path, port)
        )
        assert plugin.gateway.running is False
        text = caplog.text
        assert "端口" in text, f"日志里没说是端口占着：{text}"
        assert str(port) in text, f"日志里没给真正的端口号：{text}"
        assert "127.0.0.1:0" not in text, f"打的还是「让系统挑」那个 0：{text}"
    finally:
        squatter.close()


def test_bad_host_is_not_reported_as_port_conflict(monkeypatch, tmp_path, caplog):
    """主机名/端口写错不是「被占用」。两件事混成一句话，用户会去查错的那一项。"""
    caplog.clear()
    plugin, _ctx, _mod = load_plugin_for_real(
        monkeypatch,
        config={
            "storage_dir": str(tmp_path),
            "gateway": {"enabled": True, "host": "no-such-host.invalid", "port": 0},
        },
    )
    assert plugin.gateway.running is False
    assert "占用" not in caplog.text


# ---------------------------------------------------------------------------
# 2. 换实例：旧实例还在（宿主没调到 terminate）就绑不上
# ---------------------------------------------------------------------------


def test_second_instance_cannot_bind_and_knows_why(monkeypatch, tmp_path):
    """宿主重载插件、旧实例的 `terminate()` 没被调到时的现场。

    这时新实例的 `gateway.running` 是 False，而配置是 `enabled=true`。要有办法
    问出「为什么」——老代码里这个信息随 `OSError` 一起被吞掉了。
    """
    port = _free_port()
    cfg = _config(tmp_path, port)
    first, _c1, _m1 = load_plugin_for_real(monkeypatch, config=_fresh(cfg))
    assert first.gateway.running is True
    assert first.gateway.bound_port == port

    second, _c2, _m2 = load_plugin_for_real(monkeypatch, config=_fresh(cfg))
    assert second.gateway.running is False
    assert second.gateway.last_error, "起不来却问不出原因"
    assert str(port) in second.gateway.last_error

    first.gateway.stop()


def test_restart_after_terminate_binds_the_configured_port(monkeypatch, tmp_path):
    """反方向：正常 terminate 之后，固定端口必须能原样绑回来。"""
    port = _free_port()
    cfg = _config(tmp_path, port)
    first, _c1, _m1 = load_plugin_for_real(monkeypatch, config=_fresh(cfg))
    assert first.gateway.bound_port == port

    _run(first.terminate())
    assert first.gateway.running is False

    second, _c2, _m2 = load_plugin_for_real(monkeypatch, config=_fresh(cfg))
    try:
        assert second.gateway.running is True
        assert second.gateway.bound_port == port, "端口换了，配置里的对外地址就废了"
        assert not second.gateway.last_error
    finally:
        second.gateway.stop()


# ---------------------------------------------------------------------------
# 3. 死代码：`_public_base_url` 没有调用点
# ---------------------------------------------------------------------------


def test_public_base_url_is_gone(monkeypatch, tmp_path):
    """面板那条路的链接前缀拼装函数已经没人用了，留着就是第二条歧路。

    上一轮 `_host_from_request` 从「写了没人调」修成了「有调用点」，可它所在的
    `_public_base_url` 本身依然全仓零调用——拼出来的是
    `<host>/api/plug/backup/download`，而 `/api/plug/*` 在 4.14 上既匹配不到带
    token 的路径、又要面板 JWT（`download_route.py` 末尾有完整推导）。真实的
    链接前缀只有一处来源：`download_link` + `GATEWAY_PREFIX`。
    """
    plugin, _ctx, main_mod = load_plugin_for_real(monkeypatch, config=_config(tmp_path, 0))
    try:
        assert not hasattr(
            main_mod.BackupPlugin, "_public_base_url"
        ), "_public_base_url 还在：要么它有调用点，要么它该删"
        # 唯一的链接拼装入口，前缀是网关的
        from backup.core.gateway import GATEWAY_PREFIX

        assert plugin.download_link("tok").endswith(f"{GATEWAY_PREFIX}tok")
    finally:
        plugin.gateway.stop()


def _one_message(plugin):
    """往库里塞一条消息，否则 `/backup export` 只会回「该群没有可导出的消息」。"""
    from datetime import datetime

    from backup.core.models import Message, MessageType

    plugin.db.save_message(
        Message(
            message_id="m1",
            group_id=100,
            sender_id=7,
            sender_name="阿七",
            timestamp=datetime(2026, 1, 1, 12, 0),
            message_type=MessageType.TEXT,
            content="你好",
            raw_content="你好",
        ),
        backup_id="rt",
    )


def _text_of(response) -> str:
    return "".join(str(seg) for seg in getattr(response, "chain", []) or []) or str(response)


def test_export_receipt_carries_the_real_reason(monkeypatch, tmp_path):
    """端口被占时，`/backup export` 的回执要说出这个原因。

    老回执在这种场景下写的是「把 `gateway.enabled` 打开」——可配置里那项就是
    true。**提示指向一项本来就对的东西，等于没提示**，用户会照着改一遍再看到
    同样一句话。
    """
    from .test_round28_real_astrbot_loader import make_event

    port = _free_port()
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", port))
    squatter.listen(1)
    try:
        plugin, _ctx, _mod = load_plugin_for_real(
            monkeypatch, config=_config(tmp_path, port)
        )
        assert plugin.gateway.running is False
        _one_message(plugin)
        event = make_event("export 100 zip")

        async def go():
            return [_text_of(r) async for r in plugin.backup(event)]

        text = "\n".join(_run(go()))
        assert "端口已被占用" in text, text
        assert str(port) in text, text
        assert "把 `gateway.enabled` 打开" not in text, f"提示指向本来就对的一项：{text}"
    finally:
        squatter.close()
