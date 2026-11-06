"""第 37 轮：真浏览器/下载器会做的事，网关一件都接不住。

这一轮的起点是「**把下载链接当成真的用过一次**」——不是 `urllib.urlopen` 一次
拉完就完事，而是照着现实里的顺序打：先 HEAD 探元信息、再 Range 探一刀、断线
后续传、下载器并发开多个连接、链接被转发给第二个人。

四处，全部在旧代码上复现过：

1. **HEAD 把一次性 token 烧掉。** 微信/QQ/Telegram 的链接预览、下载器探大小、
   浏览器「另存为」对话框，都先发 HEAD。发完一次 HEAD，群成员真正点开就是
   「链接已失效或已被使用」——而链接从头到尾只有一个人点过。
2. **HEAD 的响应里有两个 `Content-Length`。** `handle_download` 填了文件大小，
   网关又补了一句 `Content-Length: 0`（因为 HEAD 不发 body）。`http.client`
   读成 `"102404, 0"`，`urllib` 直接抛；按第二个理解的话，存下来的是 0 字节 ZIP。
3. **并发打就 500。** 网关是 `ThreadingHTTPServer`，每个请求一个线程，而
   `Database` 只有**一条** sqlite 连接、`check_same_thread=False`。两三个下载
   同时进来就 `cannot start a transaction within a transaction` /
   `SystemError: error return without exception set`，请求拿到 500。真实现场
   更糟：备份主流程在往库里写，群成员在点链接。
4. **后缀 Range / 多段 Range 回 416。** `bytes=-500` 是断点续传工具探文件大小
   的常用写法，原来直接判「不可满足」；多段 `bytes=0-9,20-29` 则被截成第一段
   当成完整响应发出去——回 206 带着半截内容，客户端拼出损坏的 ZIP。
"""

import http.client
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from backup.core.database import Database
from backup.core.download_route import PREFIX, handle_download
from backup.core.export_manager import ExportManager
from backup.core.gateway import DownloadGateway
from backup.core.models import Message, MessageType
from datetime import datetime

PAYLOAD = b"PK\x03\x04" + bytes(range(256)) * 400


@pytest.fixture()
def exports(tmp_path):
    db = Database(str(tmp_path / "t.sqlite"))
    manager = ExportManager(db, tmp_path / "exports", tmp_path / "media", {})
    yield manager
    db.close()


@pytest.fixture()
def gateway(exports):
    instance = DownloadGateway(exports, "127.0.0.1", 0)
    assert instance.start(), instance.last_error
    try:
        yield instance
    finally:
        instance.stop()


def _issue(exports, name="a.zip", payload=PAYLOAD):
    path = exports.export_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    token, _ = exports._issue_token(path)
    return token, path


# ---------------------------------------------------------------- HEAD 不占名额


def test_head_does_not_burn_the_token(gateway, exports):
    """链接预览、下载器先探大小，都是 HEAD。它只问元信息，不该把文件领走。

    旧代码里 HEAD 和 GET 走同一条路，token 当场被删。用户在群里看到卡片、
    点开就是 410，而链接根本没人下过。
    """
    token, path = _issue(exports)
    conn = http.client.HTTPConnection("127.0.0.1", gateway.bound_port, timeout=5)
    conn.request("HEAD", f"{PREFIX}{token}")
    response = conn.getresponse()
    assert response.status == 200
    conn.close()

    # 探完还能真的下
    with urllib.request.urlopen(gateway.url_for(token), timeout=5) as resp:
        assert resp.read() == PAYLOAD


def test_head_reports_the_real_size_exactly_once(gateway, exports):
    """HEAD 的 `Content-Length` 必须是文件大小，且**只能有一个**。

    旧代码发两个同名头（`102404` 和 `0`）。`http.client` 把它们合成
    `"102404, 0"`，`urllib` 直接抛异常；宽松一点的客户端按 0 字节处理，
    存下来是个空 ZIP，报「文件已损坏」。
    """
    token, path = _issue(exports)
    conn = http.client.HTTPConnection("127.0.0.1", gateway.bound_port, timeout=5)
    conn.request("HEAD", f"{PREFIX}{token}")
    response = conn.getresponse()
    values = [v for (k, v) in response.getheaders() if k.lower() == "content-length"]
    conn.close()
    assert values == [str(len(PAYLOAD))], values


def test_head_on_unknown_token_is_410_not_404(gateway):
    """HEAD 也要说清「这条链接没了」，别回一个看着正常的 200。"""
    conn = http.client.HTTPConnection("127.0.0.1", gateway.bound_port, timeout=5)
    conn.request("HEAD", f"{PREFIX}nosuchtoken")
    response = conn.getresponse()
    status = response.status
    conn.close()
    assert status == 410


# ---------------------------------------------------------------- 并发


def test_concurrent_downloads_do_not_blow_up_the_shared_connection(gateway, exports):
    """八个线程同时下载，一个 500 都不该有。

    网关每个请求一个线程，`Database` 只有**一条**连接、`check_same_thread=False`
    ——那只是「不拦你」，不是「允许多线程同时用」。旧代码里 `execute` / `query` /
    `executemany` 直接摸 `self._conn`，两个线程交错就得到
    `cannot start a transaction within a transaction`、
    `cannot commit - no transaction is active`，重则
    `SystemError: error return without exception set`，网关外层兜成 500。

    每个线程连下 3 条，让碰撞概率贴近真实现场（一个人下完还有下一个人）。
    """
    tokens = [_issue(exports, f"c{i}.zip")[0] for i in range(24)]
    failures: list[str] = []
    bucket = [tokens[i::8] for i in range(8)]

    def pull(group):
        for token in group:
            try:
                with urllib.request.urlopen(gateway.url_for(token), timeout=10) as resp:
                    if resp.read() != PAYLOAD:
                        failures.append(f"{token}: 内容不符")
            except urllib.error.HTTPError as e:
                failures.append(f"{token}: HTTP {e.code}")
            except Exception as e:  # noqa: BLE001 - 测试要把所有形态都记下来
                failures.append(f"{token}: {type(e).__name__}")

    threads = [threading.Thread(target=pull, args=(g,)) for g in bucket]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == [], failures


def test_download_while_backup_is_writing(gateway, exports):
    """真实现场：备份在往库里写，群成员在点链接。两边共用一条连接。

    写两个线程（单条写入 + 批量登记来源各一个），下载三个线程，跑三轮——
    这类竞态单跑一次可能碰不上，写成"一次就断言"等于把它测成"偶尔绿"。
    """
    db = exports.db
    failures: list[str] = []
    stop = threading.Event()

    def writer(tag):
        """模拟备份主流程：两种写法都在碰那条连接。"""
        i = 0
        while not stop.is_set():
            message = Message(
                message_id=f"w{tag}-{i}",
                group_id=100,
                sender_id=1,
                sender_name="u",
                timestamp=datetime.now(),
                message_type=MessageType.TEXT,
                content=f"消息 {i}",
                raw_content="{}",
            )
            try:
                db.save_message(message, "bk")
                db.add_sources("bk", [message])
                db.save_members(100, [])
            except Exception as e:  # noqa: BLE001 - 写入侧的异常同样算失败
                failures.append(f"writer{tag}: {type(e).__name__}: {e}")
                return
            i += 1

    def pull(token):
        try:
            with urllib.request.urlopen(gateway.url_for(token), timeout=10) as resp:
                if resp.read() != PAYLOAD:
                    failures.append(f"{token}: 内容不符")
        except urllib.error.HTTPError as e:
            failures.append(f"{token}: HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{token}: {type(e).__name__}")

    writers = [threading.Thread(target=writer, args=(n,)) for n in range(2)]
    for w in writers:
        w.start()
    try:
        for _ in range(3):
            tokens = [_issue(exports, f"m{secrets.token_hex(3)}.zip")[0] for _ in range(3)]
            threads = [threading.Thread(target=pull, args=(t,)) for t in tokens]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    finally:
        stop.set()
        for w in writers:
            w.join()
    assert failures == [], failures


def test_only_one_of_two_simultaneous_first_visits_wins(exports):
    """同一条链接被两个人同时点开：只有一个能拿到文件。

    这是「仅可使用一次」最容易被绕过的地方。旧代码分三步（查有没有被领过 /
    打时间戳 / 删 token），三步各自加锁，两个线程能交错着全通过——两个人一起
    把文件下走，回执上那句「仅可使用一次」在并发下是假的。
    """
    wins = 0
    for round_index in range(30):
        token = f"race-{round_index}"
        path = exports.export_dir / f"race{round_index}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PAYLOAD)
        exports.db.save_token(token, str(path), time.time() + 3600)
        statuses: list[int] = []
        barrier = threading.Barrier(2)

        def hit():
            barrier.wait()
            statuses.append(handle_download(f"{PREFIX}{token}", exports, None).status)

        threads = [threading.Thread(target=hit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wins += len([s for s in statuses if s == 200])

    # 30 轮、每轮两个并发整份请求：每轮只该有一个赢家
    assert wins == 30, f"{wins}/30 轮里两个人同时下到了文件"


# ---------------------------------------------------------------- Range 的两种合法写法


def test_suffix_range_returns_the_tail(exports):
    """`bytes=-500`：要末尾 500 字节。断点续传工具探文件大小时就发这个。

    旧代码把那句「后缀区间对 ZIP 没意义」写成了 `return None`，而 `None` 在调用处
    等于「不可满足」→ 416。下载器看到 416 就放弃了，用户看到「下载失败」。
    """
    token, path = _issue(exports)
    response = handle_download(f"{PREFIX}{token}", exports, "bytes=-500")
    assert response.status == 206
    assert response.headers["Content-Range"] == f"bytes {len(PAYLOAD) - 500}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
    assert b"".join(response.iter_chunks()) == PAYLOAD[-500:]


def test_multi_range_is_served_whole_instead_of_a_truncated_206(exports):
    """多段 Range 要回 `multipart/byteranges`，回不了就整份发 200。

    旧代码取第一段、回 206 带着 10 个字节，客户端以为这是它要的全部——
    拼出来是坏的 ZIP。宁可 200 给整份，也不要 206 给半截。
    """
    token, path = _issue(exports)
    response = handle_download(f"{PREFIX}{token}", exports, "bytes=0-9,20-29")
    assert response.status == 200
    assert response.headers["Content-Length"] == str(len(PAYLOAD))
    assert b"".join(response.iter_chunks()) == PAYLOAD


def test_unsatisfiable_range_says_where_the_file_ends(exports):
    """416 要带 `Content-Range: bytes */<总长>`，客户端据此重新对齐。"""
    token, path = _issue(exports)
    response = handle_download(f"{PREFIX}{token}", exports, f"bytes={len(PAYLOAD) + 10}-")
    assert response.status == 416
    assert response.headers["Content-Range"] == f"bytes */{len(PAYLOAD)}"


def test_garbage_range_is_ignored_not_rejected(exports):
    """看不懂的 Range 按 RFC 7233 忽略，整份发 200。

    旧代码把「语法不认」和「要不到」混成一件事，一律回 416——文件好好躺着，
    用户看到「下载失败」。
    """
    token, path = _issue(exports)
    response = handle_download(f"{PREFIX}{token}", exports, "bytes=abc-def")
    assert response.status == 200
    assert b"".join(response.iter_chunks()) == PAYLOAD


# ---------------------------------------------------------------- 端到端


def test_preview_then_download_then_second_person(gateway, exports):
    """现实顺序：链接预览 → 真正下载 → 转发给第二个人。

    旧代码在第一步就挂了——HEAD 把 token 烧掉，真正下载和第二个人的请求全是
    410，而这条链接从头到尾只有一个人点过。这一条钉住两件事：HEAD 不占名额，
    整份下载之后链接立刻作废（不是等 15 分钟窗口过去）。
    """
    token, _path = _issue(exports)
    port = gateway.bound_port

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("HEAD", f"{PREFIX}{token}")
    assert conn.getresponse().status == 200  # 预览抓卡片，不占名额
    conn.close()

    with urllib.request.urlopen(gateway.url_for(token), timeout=5) as resp:
        assert resp.status == 200
        assert resp.read() == PAYLOAD

    # 第二个人拿着转发来的链接点开：410
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(gateway.url_for(token), timeout=5)
    assert exc.value.code == 410


def test_partial_then_rest_of_the_file_is_still_allowed(exports):
    """先探一刀、再从探测点接到末尾：这是一次下载的两个动作，不是两个人。

    留这条用例是为了把**边界写清楚**：分段下载共享一个会话窗口
    （`GRACE_SECONDS`），窗口内谁发片段都放行——断点续传必须如此，
    aria2/IDM 那种多连接分段下载更是同时从几个不同偏移要数据。
    代价是「窗口期内第二条完整链接」做不到百分之百拦住，这一点在
    `DESIGN.md` 里写明了，不假装它不存在。
    """
    token, _path = _issue(exports)
    probe = handle_download(f"{PREFIX}{token}", exports, "bytes=0-0")
    assert probe.status == 206
    rest = handle_download(f"{PREFIX}{token}", exports, "bytes=1-")
    assert rest.status == 206
    assert b"".join(rest.iter_chunks()) == PAYLOAD[1:]


def test_gateway_survives_a_half_closed_connection(gateway, exports):
    """客户端下到一半就断（手机切网、用户取消）：网关不能因此挂掉，别人还要能下。"""
    token, _path = _issue(exports, "half.zip")
    port = gateway.bound_port
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(f"GET {PREFIX}{token} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        sock.recv(128)
        # 不发完就关，模拟半途断开
    other, _ = _issue(exports, "other.zip")
    with urllib.request.urlopen(gateway.url_for(other), timeout=5) as resp:
        assert resp.read() == PAYLOAD
