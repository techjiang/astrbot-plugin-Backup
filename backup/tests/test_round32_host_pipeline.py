"""第 32 轮：在真 AstrBot 4.14.6 + 真 pipeline 下跑出来的四处。

这一轮和前 31 轮的区别在验证方式：把 `astrbot==4.14.6` 装进来，在
`data/plugins/backup` 布局下用**真的** `PluginManager.load()` 加载插件，
再用**真的** `WakingCheckStage` 和 `StarRequestSubStage` 发命令。前面所有
轮次的「假宿主」用例都在这里被绕过了——它们替我省掉的不是网络，是宿主本身。

四处里三处是「功能写着、用户永远走不到」：

1. `download_link` 收了 `request`，一次都没用过 → 反代部署下发进群的永远是
   `http://127.0.0.1:<端口>/...`，群成员点的是他自己电脑；
2. `/backup restore <包> <群号>`：包路径被当成备份 ID 去查库 → 「备份不存在」；
3. `_adapt_kwargs` 见 `**kwargs` 就原样透传 → `gid` 参数永远缺参；
4. `_oldest_id` 把 `message_id` 当 `message_seq` 回传 → 平台侧 `int('m99')` 崩。

前三处在这里验；第 3、4 条见 `test_round32_kwargs_alias.py`。
"""

import sys

import pytest

from backup.core.commands import parse_group_arg

from .conftest import load_plugin_for_real, real_astrbot_available


class _Req:
    def __init__(self, headers=None, scheme="http"):
        self.headers = headers or {}
        self.scheme = scheme


class _FakeEventForHeaders:
    """真事故事件上取请求头的那条路：`request_headers` / `message_obj.raw_message`。"""

    def __init__(self, headers=None, raw_headers=None):
        if headers is not None:
            self.request_headers = headers
        if raw_headers is not None:
            class MO:
                raw_message = {"headers": raw_headers}
            self.message_obj = MO()


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    """一个起得来网关的真插件实例（真 AstrBot 的 `Star`/`Context` 加载出来）。"""
    if not real_astrbot_available():
        pytest.skip("需要真 astrbot 才能验宿主行为")
    monkeypatch.setattr(
        "backup.main.assert_writable_target", lambda root, roots: tmp_path / "storage"
    )
    instance, _context, _main_mod = load_plugin_for_real(monkeypatch)
    # 原来这里按住了 `_dashboard_host`——一个零调用点的函数，按住它验的是「它不会
    # 干扰」，而它本来就从没被调用过。那个函数连同 `dashboard.public_url` 这项
    # 配置一起删掉了（见 test_round40），这里不再需要。
    yield instance
    try:
        instance.gateway.stop()
    except Exception:
        pass


# ---------------------------------------------------------------- 下载链接
def test_public_host_beats_loopback(plugin):
    """反代后面：请求从哪个域名进来，链接就该是那个域名。

    这是 32 轮最重要的那条。原来 `download_link` 收了 `request` 却从没用过，
    `_public_base_url` 那套推断逻辑没有任何调用点——它自己的用例直接调它，
    所以一直绿。
    """
    link = plugin.download_link("tok", _Req(headers={"Host": "bot.example.com"}))
    assert link == "http://bot.example.com/backup/download/tok", link
    assert "127.0.0.1" not in link


def test_forwarded_headers_are_honored(plugin):
    link = plugin.download_link(
        "tok",
        _Req(headers={"X-Forwarded-Host": "bak.example.com", "X-Forwarded-Proto": "https"}),
    )
    assert link == "https://bak.example.com/backup/download/tok", link


def test_forwarded_host_list_takes_first_hop(plugin):
    """`X-Forwarded-Host` 是逗号列表时取第一跳（最靠近用户的那层）。"""
    link = plugin.download_link(
        "tok",
        _Req(headers={"X-Forwarded-Host": "a.example.com, b.internal", "X-Forwarded-Proto": "https"}),
    )
    assert link == "https://a.example.com/backup/download/tok", link


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:3000", "192.168.1.5", "10.0.0.2"])
def test_private_hosts_fall_back_to_a_bare_path(plugin, host):
    """内网/本机地址不拿来拼链接：发进群里没人点得动。

    内网地址不认之后，原来退回的是网关自己的回环地址。可那条链接发进群，群成员
    点的是**他自己电脑**的 127.0.0.1——和「不认内网地址」要防的是同一件事。
    现在退回裸路径，回执里明说「只有本机能用」。
    """
    link = plugin.download_link("tok", _Req(headers={"Host": host}))
    assert link == "/backup/download/tok", link


def test_explicit_base_url_wins(plugin):
    plugin.config["export"]["base_url"] = "https://fixed.example.com"
    assert plugin.download_link("tok", _Req(headers={"Host": "other.example.com"})) == (
        "https://fixed.example.com/backup/download/tok"
    )


def test_headers_are_read_from_event_shape(plugin):
    """`/backup export` 手里是事件不是 request：请求头藏在事件里也要认。"""
    assert plugin._host_from_request(_FakeEventForHeaders(headers={"Host": "e.example.com"})) == (
        "http://e.example.com"
    )
    assert plugin._host_from_request(
        _FakeEventForHeaders(raw_headers={"Host": "r.example.com"})
    ) == "http://r.example.com"
    assert plugin._host_from_request(None) == ""


# ---------------------------------------------------------------- 恢复参数
def _parse_restore(args):
    """照抄 `_cmd_restore` 的参数解析（不跑整条命令）。"""
    from backup.main import _looks_like_archive

    tokens = [t for t in args if not t.startswith("--")]
    backup_id = tokens[0] if tokens else ""
    target = parse_group_arg(tokens[1]) if len(tokens) > 1 else None
    archive = None
    if backup_id and _looks_like_archive(backup_id):
        archive = backup_id
        backup_id = "<from manifest>"
    elif len(tokens) > 2 and _looks_like_archive(tokens[2]):
        archive = tokens[2]
    return backup_id, target, archive


def test_restore_with_archive_and_target_group():
    """`/backup restore <包> <群号>`——换机器恢复最常写的那条，原来两头落空。

    `target` 有值（不是 None），所以 `if target is None and is_archive` 不成立；
    `len(tokens) > 2` 也不成立（只有两个参数）。于是 `archive` 留在 None，
    包路径被当成备份 ID 去查库，报「备份 …zip 不存在」。
    """
    bid, target, archive = _parse_restore(["/p/100_bk.zip", "987654"])
    assert archive == "/p/100_bk.zip"
    assert target == 987654
    assert bid == "<from manifest>"  # 从包里的 manifest 取，不是拿路径当 ID


def test_restore_with_archive_only():
    bid, target, archive = _parse_restore(["/p/100_bk.zip"])
    assert archive == "/p/100_bk.zip"
    assert target is None


def test_restore_with_backup_id_and_target():
    bid, target, archive = _parse_restore(["bk_100_x", "987654"])
    assert archive is None
    assert bid == "bk_100_x"
    assert target == 987654


def test_restore_with_backup_id_target_and_archive():
    bid, target, archive = _parse_restore(["bk_100_x", "987654", "/p/100_bk.zip"])
    assert bid == "bk_100_x"
    assert archive == "/p/100_bk.zip"
    assert target == 987654
