"""第 22 轮：导出链接里那个域名到底从哪来，以及依赖漏项。

两条都是二十一轮没人测过的：

1. `_public_base_url` 会去读 `config["dashboard"]`，可 `_conf_schema.json` 和
   `config.yaml` 里**根本没有** `dashboard` 这一项——面板上既看不到也填不了，
   文案却写着「留空则用面板公网地址自动拼」。群成员点得动的链接因此永远拼不出。
2. `_as_flask` 里 `from werkzeug.wrappers import ...` 是 try/except 兜底，而
   `requirements.txt` 没写 werkzeug。缺了它不报错，只是每次下载都退化——单测里
   对 Flask 求 `response.body` 看不见这一层，只有真 Werkzeug 才暴露。
"""

import sys

import pytest

from .conftest import install_fake_astrbot, preload_host_for_lineage, real_astrbot_available


class _Req:
    """Flask/星野请求的最小形状：路径 + query + headers。"""

    def __init__(self, path="/", headers=None, scheme="http"):
        self.path = path
        self.full_path = path
        self.args = {}
        self.headers = headers or {}
        self.scheme = scheme


# ---------------------------------------------------------------- 域名来源


@pytest.fixture
def main_mod(monkeypatch):
    """只为能 `import backup.main`：这些用例不构造插件实例。"""
    preload_host_for_lineage()
    import backup.main as mod

    return mod


@pytest.mark.parametrize(
    "host",
    ["localhost:6185", "127.0.0.1:6185", "192.168.1.7:6185", "10.0.0.2", "172.17.0.3:8080"],
)
def test_private_host_is_never_used_for_a_shared_link(main_mod, host):
    """内网地址拼出来的链接发到群里没人点得动，不如老实说「没配」。"""
    from backup.main import BackupPlugin

    assert BackupPlugin._host_from_request(_Req(headers={"Host": host})) == ""


def test_public_host_from_request_is_used(main_mod):
    from backup.main import BackupPlugin

    assert (
        BackupPlugin._host_from_request(_Req(headers={"Host": "bot.example.com"}))
        == "http://bot.example.com"
    )


def test_reverse_proxy_forwarded_headers_win(main_mod):
    """反代后面 `Host` 是内网地址，真实域名在 `X-Forwarded-Host`。"""
    from backup.main import BackupPlugin

    req = _Req(
        headers={
            "Host": "127.0.0.1:6185",
            "X-Forwarded-Host": "backup.asoe.cn",
            "X-Forwarded-Proto": "https",
        }
    )
    assert BackupPlugin._host_from_request(req) == "https://backup.asoe.cn"


def test_forwarded_header_with_port_and_list(main_mod):
    """多级反代时头里是逗号列表，取第一跳。"""
    from backup.main import BackupPlugin

    req = _Req(headers={"X-Forwarded-Host": "bot.example.com:8443, proxy.local"})
    assert BackupPlugin._host_from_request(req) == "http://bot.example.com:8443"


def test_dashboard_section_is_gone_because_nothing_read_it():
    """`dashboard.public_url` 从面板上撤掉了——它从来没被读过。

    这一条原来断言的是**反方向**：「面板上看不到的配置等于不存在」，所以要把它
    写进 schema。前提没错，结论错在另一半：写进面板之后，得真有人读它。第 33 轮
    删掉 `_public_base_url` 之后，全仓只有 `_dashboard_host()` 还在翻宿主配置，而
    那个函数本身零调用点——它的两个用例（`monkeypatch.setattr(main_mod,
    "_dashboard_host", ...)` 和本条）都只是把它按住，没有一条在验它有用。

    面板上摆一个填了不生效的输入框，比不摆更糟：用户按提示填了面板地址、重新
    导出，链接还是裸路径。所以整块撤掉，链接来源只剩 `export.base_url` →
    请求进来的域名 → 裸路径。
    """
    import json
    from pathlib import Path

    import yaml

    plugin_dir = Path(__file__).resolve().parent.parent
    schema = json.loads((plugin_dir / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "dashboard" not in schema

    config = yaml.safe_load((plugin_dir / "config.yaml").read_text(encoding="utf-8"))["backup"]
    assert "dashboard" not in config

    main_src = (plugin_dir / "main.py").read_text(encoding="utf-8")
    assert "_dashboard_host" not in main_src


def test_export_receipt_is_a_complete_usable_url(main_mod, monkeypatch):
    """`/backup export` 的回执里必须是一个**点得动**的完整 URL，不能用裸路径糊过去。

    这一条原来断言「链接 = 面板地址 + 挂载前缀」，依据是「`/api/backup/download`
    在面板鉴权白名单里，所以群成员点得到」。把 AstrBot 4.14.6 装进来核过之后，那个
    前提有两处不成立（见 `core/gateway.py` 顶部）：白名单里那条属于宿主自己的
    `BackupRoute`，而插件能挂的 `/api/plug/*` 既要过 JWT、路径又比不中带参数的形状。

    所以「点得动」的来源有三层：`export.base_url` → 这条命令进来的域名 → 网关
    回环地址。这一条**原先把第一、第二层写反了**：断言「带 `Host: bot.example.com`
    时链接必须是 `http://127.0.0.1:<端口>`」——那正是 32 轮修掉的 bug（`download_link`
    收了 `request` 却从没用过），把「群成员点自己电脑的 127.0.0.1」当成了正确答案。
    """
    Context = sys.modules["astrbot.api.star"].Context
    instance = main_mod.BackupPlugin(Context())
    try:
        assert instance.gateway.running, "内置网关默认应已启动"

        # 请求是从哪个域名进来的，链接就用哪个域名——反代后面这是唯一现成的真相
        link = instance.download_link("tok", _Req(headers={"Host": "bot.example.com"}))
        assert link == "http://bot.example.com/backup/download/tok", link

        # 反代/穿透：真实域名在 X-Forwarded-Host，协议在 X-Forwarded-Proto
        link = instance.download_link(
            "tok",
            _Req(headers={"X-Forwarded-Host": "bak.example.com", "X-Forwarded-Proto": "https"}),
        )
        assert link == "https://bak.example.com/backup/download/tok", link

        # 内网/本机地址不认：链接发到群里没人点得动。
        # 原来这里断言「退回网关回环地址」——那等于把「群里那条链接是 127.0.0.1」
        # 锁成了正确答案（第 33 轮修掉）。回环地址只对**本机**有意义，用户拿到
        # 手里点的是他自己电脑的 127.0.0.1，一辈子点不开。所以退回裸路径，
        # 让上层把「点不动」说出来。
        link = instance.download_link("tok", _Req(headers={"Host": "192.168.1.5:8080"}))
        assert link == "/backup/download/tok", link

        # 配了对外地址就跟着它走（优先级最高，压过请求域名）
        instance.config["export"]["base_url"] = "https://bak.example.com"
        assert instance.download_link("tok") == "https://bak.example.com/backup/download/tok"
        assert (
            instance.download_link("tok", _Req(headers={"Host": "other.example.com"}))
            == "https://bak.example.com/backup/download/tok"
        )
    finally:
        instance.gateway.stop()
        instance.db.close()


def test_export_receipt_degrades_to_a_bare_path_without_gateway_or_base_url(main_mod):
    """网关没开、也没有对外地址时，宁可给出「点不动」的裸路径，也不假装能用。"""
    Context = sys.modules["astrbot.api.star"].Context
    instance = main_mod.BackupPlugin(Context())
    try:
        instance.gateway.stop()
        instance.config["export"]["base_url"] = ""
        link = instance.download_link("tok")
        assert link == "/backup/download/tok", link
    finally:
        instance.db.close()


# ---------------------------------------------------------------- 依赖


def test_werkzeug_is_a_declared_dependency():
    """下载回执走 Werkzeug Response，requirements 里不写就是线上 500 / 空文件。"""
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "werkzeug" in text.lower()


def test_download_view_is_a_real_http_response():
    """装上 Werkzeug 就必须是它，不能被 try/except 兜成自定义对象。"""
    pytest.importorskip("werkzeug")
    from werkzeug.wrappers import Response

    from backup.core.download_route import DownloadResponse, _as_flask

    wrapped = _as_flask(
        DownloadResponse(status=200, body=b"abc", headers={"Content-Type": "text/plain"})
    )
    assert isinstance(wrapped, Response)
    assert b"".join(wrapped.response) == b"abc"


def test_missing_werkzeug_does_not_crash_the_export(monkeypatch):
    """没装 Werkzeug 也不能炸，只能退化——退化时状态码和文件路径还得在。"""
    import sys

    from backup.core import download_route

    def no_werkzeug(*_a, **_k):
        raise ImportError("no werkzeug")

    monkeypatch.setitem(sys.modules, "werkzeug", no_werkzeug)
    monkeypatch.setitem(sys.modules, "werkzeug.wrappers", no_werkzeug)
    response = download_route._as_flask(
        download_route.DownloadResponse(status=206, body=b"", headers={}, start=10, length=5)
    )
    assert response.status == 206
