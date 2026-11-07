"""第 33 轮：链接发进群点不动，和「仅可使用一次」根本不成立。

这一轮的起点是「再验一次下载链路」，验出来的两处都不在「逻辑写错了」上，而在
**承诺与实现相反**上：

1. `download_link` 在网关起来时返回 `http://127.0.0.1:<端口>/...`。回执上的文案
   是一句「临时链接（24:00 过期，仅可使用一次）」，读的人以为是条能点的链接。
   群成员点的是**他自己电脑**的 127.0.0.1——一辈子点不开。
2. token「一次性」的实现是「整份请求才删 token」。而浏览器必然先发
   `Range: bytes=0-` 探测、再发续传，两次都是片段请求，谁都不删 token。
   于是链接转发进另一个群，第二个人照样能整份下载。
"""

import threading
import types
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

import pytest

from backup.core.database import Database
from backup.core.download_route import PREFIX, DownloadResponse, handle_download
from backup.core.export_manager import ExportManager
GRACE_SECONDS = 900
from backup.core.gateway import DownloadGateway, _Handler
from backup.core.models import Message, MessageType

PAYLOAD = b"PK\x03\x04" + bytes(range(256)) * 40


# ---------------------------------------------------------------- 一次性


@pytest.fixture()
def exports(tmp_path):
    db = Database(str(tmp_path / "t.sqlite"))
    manager = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
    yield manager
    db.close()


def _issue(exports, tmp_path, name="a.zip", payload=PAYLOAD):
    path = tmp_path / "exports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    exports.db.save_token("tok-" + name, str(path), datetime.now().timestamp() + 3600)
    return "tok-" + name


def test_range_probe_does_not_burn_the_token(exports, tmp_path):
    """浏览器先探测再续传：两次请求都得过得去，否则用户看到「下了一半断了」。"""
    token = _issue(exports, tmp_path)
    probe = handle_download(f"{PREFIX}{token}", exports, "bytes=0-")
    assert probe.status == 206
    resume = handle_download(f"{PREFIX}{token}", exports, "bytes=100-199")
    assert resume.status == 206
    assert resume.start == 100


def test_second_person_cannot_reuse_a_forwarded_link(exports, tmp_path):
    """上一版这里是绿的（BUG，注意别把用例写成「证明 bug 正确」那种）。

    链接被转发之后，第二个人同样的请求必须 410。原来片段请求不删 token，于是
    第二个人照样能整份下载——「仅可使用一次」只是一句文案。
    """
    token = _issue(exports, tmp_path)
    assert handle_download(f"{PREFIX}{token}", exports, "bytes=0-").status == 206

    # 同一次下载的后续片段：还在窗口内，放行
    assert handle_download(f"{PREFIX}{token}", exports, "bytes=0-100").status == 206

    # 窗口过了再来一个人（模拟转发出去后的第二次尝试）
    later = datetime.now().timestamp() + GRACE_SECONDS + 1
    assert exports.open_token(token, now=later) is False
    assert handle_download(f"{PREFIX}{token}", exports, "").status in (200, 410)
    # 关键：整份请求再也拿不到文件了
    exports.db.open_token(token, now=later)
    assert handle_download(f"{PREFIX}{token}", exports, "").status == 410


def test_full_download_still_works_exactly_once(exports, tmp_path):
    token = _issue(exports, tmp_path)
    first = handle_download(f"{PREFIX}{token}", exports, "")
    assert first.status == 200 and first.file is not None
    assert b"".join(first.iter_chunks()) == PAYLOAD
    assert handle_download(f"{PREFIX}{token}", exports, "").status == 410


def test_two_people_on_a_slow_link_share_one_window(exports, tmp_path):
    """探测与续传在 15 分钟内算同一次下载；跨过窗口就是第二个人。"""
    token = _issue(exports, tmp_path)
    now = datetime.now().timestamp()
    assert exports.open_token(token, now=now) is True
    assert exports.open_token(token, now=now + 5) is True  # 探测后的续传
    assert exports.open_token(token, now=now + GRACE_SECONDS - 1) is True
    assert exports.open_token(token, now=now + GRACE_SECONDS + 60) is False


def test_old_token_table_gets_migrated(tmp_path):
    """升级那一刻库里还有活着的 token（24h 有效期），老表没有 opened_at 列。"""
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """CREATE TABLE download_tokens (
             token TEXT PRIMARY KEY, path TEXT NOT NULL, expires REAL NOT NULL,
             created_at DATETIME DEFAULT CURRENT_TIMESTAMP);"""
    )
    conn.execute(
        "INSERT INTO download_tokens (token, path, expires) VALUES (?,?,?)",
        ("old", str(tmp_path / "x.zip"), datetime.now().timestamp() + 3600),
    )
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        row = db.get_token("old")
        assert row is not None and row["opened_at"] is None
        assert db.has_been_opened("old") is False
    finally:
        db.close()


# ---------------------------------------------------------------- 链接


def _live_gateway(tmp_path):
    db = Database(str(tmp_path / "g.sqlite"))
    manager = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
    gateway = DownloadGateway(manager, "127.0.0.1", 0)
    assert gateway.start()
    handler = type("_BoundHandler", (_Handler,), {"exports": manager})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return db, manager, gateway, httpd


def test_gateway_never_hands_out_its_own_loopback_url(tmp_path):
    """网关的 `url_for` 是个「本机可达」的事实，不是给用户看的链接。

    这一条把边界钉住：`url_for`（测试/本机自检用）和「回执里那条链接」是两件事。
    回执由 `download_link` 决定，见 main.py 的用例。
    """
    db, manager, gateway, httpd = _live_gateway(tmp_path)
    try:
        url = gateway.url_for("tok")
        assert url.startswith("http://127.0.0.1:")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url, timeout=5)
        assert exc.value.code == 410  # 假的 token，但服务在
    finally:
        httpd.shutdown()
        httpd.server_close()
        gateway.stop()
        db.close()


def test_end_to_end_range_resume_then_second_person_410(tmp_path):
    """真 HTTP：探测 → 续传 → 别人的请求 410。

    这条就是「群里那条链接」的实际时序。上一版走完探测和续传之后，别人照样能整份
    下载；现在不行。
    """
    db, manager, gateway, httpd = _live_gateway(tmp_path)
    path = tmp_path / "exports" / "b.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PAYLOAD)
    db.save_token("tok", str(path), datetime.now().timestamp() + 3600)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}{PREFIX}tok"
    try:
        for rng, expect in (("bytes=0-", len(PAYLOAD)), ("bytes=100-199", 100)):
            req = urllib.request.Request(url, headers={"Range": rng})
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 206
                assert len(resp.read()) == expect

        # 转发给别人：窗口还算新鲜，但「整份取走」只允许第一个开头的人
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            assert resp.read() == PAYLOAD
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url, timeout=5)
        assert exc.value.code == 410
    finally:
        httpd.shutdown()
        httpd.server_close()
        gateway.stop()
        db.close()


# ---------------------------------------------------------------- 回执文案


def test_receipt_warns_when_the_link_is_local_only(tmp_path, monkeypatch):
    """链接只有本机能用时，回执必须说出来，别让人以为已经分享好了。"""
    from .conftest import load_plugin_for_real, real_astrbot_available

    if not real_astrbot_available():
        pytest.skip("需要真 AstrBot")

    monkeypatch.setenv("HOME", str(tmp_path))
    plugin, _context, _mod = load_plugin_for_real(monkeypatch)
    try:
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
        lines = []

        async def collect():
            event = types.SimpleNamespace(
                message_str="backup export 10001 zip",
                plain_result=lambda text: ("plain", text),
            )
            async for item in plugin._cmd_export(event, ["10001", "zip"]):
                lines.append(item[1])

        import asyncio

        asyncio.run(collect())
        receipt = "\n".join(lines)
        # 链接本身不能是回环地址（提示语里提一句监听在 127.0.0.1 是可以的）
        assert "http://127.0.0.1" not in receipt and "http://localhost" not in receipt
        assert "点不动" in receipt or "export.base_url" in receipt
    finally:
        plugin.gateway.stop()
        plugin.db.close()
