"""第 31 轮：拉一个真 OneBot HTTP 服务，让备份跑最真实的翻页链路。

前面每一轮的分页用例都拿「自己写的假客户端」喂数据：签名、返回形状、朝向、
分页语义全是我自己定的。这轮起一个真 aiohttp 服务，客户端走真 HTTP、真 JSON，
只把「平台」当黑盒——于是三处一直没被验证过的东西露出来了。

1. `{"messages": [...]}` 外壳：`_fetch_page` 的 dict 分支永远走不到，
   `list(dict)` 拿到的是键名 `["messages"]`，一页 dict 被当成「1 条消息」；
2. 锚点取成了这一页最新那条（平台按「旧→新」返回时），下一页原地再取一遍；
3. 每页给得比 `PAGE_SIZE` 少就收工，120 条的群只备到一页。

这三条叠在一起的表现是同一个：备份报 `completed`，只存了几十条。
"""

import json

import pytest

from backup.core.backup_engine import BackupEngine, PlatformAdapter, _unwrap_messages
from backup.core.database import Database

pytest.importorskip("aiohttp")

from aiohttp import web  # noqa: E402

GROUP = 100


class FakeOneBot:
    """最小 OneBot v11 HTTP 服务：`/{action}` + JSON。"""

    def __init__(self, messages, per_call=None):
        self.messages = messages  # 旧→新
        self.per_call = per_call
        self.calls: list[tuple[str, dict]] = []
        self._runner = None
        self.base = ""

    async def start(self):
        app = web.Application()
        app.router.add_post("/{action}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    async def stop(self):
        await self._runner.cleanup()

    async def _handle(self, request):
        action = request.match_info["action"]
        payload = await request.json()
        self.calls.append((action, payload))
        return web.json_response({"status": "ok", "retcode": 0, "data": self._dispatch(action, payload)})

    def _dispatch(self, action, payload):
        if action == "get_group_msg_history":
            return {"messages": self._history(payload)}
        if action == "get_group_info":
            return {"group_id": GROUP, "group_name": "真服务群", "member_count": 2}
        if action == "get_group_member_list":
            return [{"user_id": 1, "nickname": "甲", "role": "owner"}]
        if action in ("_get_group_notice", "get_group_notice"):
            return []
        if action in ("get_group_root_files", "get_group_files"):
            return {}
        return None

    def _history(self, payload) -> list[dict]:
        """照 OneBot 语义：给了 message_seq 就「从这条往前数 count 条」。"""
        count = int(payload.get("count") or self.per_call or 50)
        seq = payload.get("message_seq")
        if seq is not None:
            end = next(
                (i for i, m in enumerate(self.messages) if str(m["message_id"]) == str(seq)),
                len(self.messages),
            )
        else:
            offset = int(payload.get("offset") or 0)
            end = len(self.messages) - offset
        start = max(0, end - count)
        return self.messages[start:end]

    def history_calls(self) -> list[dict]:
        return [p for a, p in self.calls if a == "get_group_msg_history"]


class HttpClient:
    """照 `aiocqhttp.CQHttp`：任意属性名 → 一次 call_action。"""

    def __init__(self, base):
        self.base = base
        self.session = None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda **kwargs: self.call_action(name, **kwargs)

    async def call_action(self, action, **params):
        import aiohttp

        if self.session is None:
            self.session = aiohttp.ClientSession()
        payload = {k: v for k, v in params.items() if v is not None}
        async with self.session.post(f"{self.base}/{action}", json=payload) as resp:
            body = await resp.json()
        if body.get("retcode", 0) != 0:
            raise RuntimeError(f"{action} 返回 {body}")
        return body.get("data")

    async def close(self):
        if self.session is not None:
            await self.session.close()


def _messages(n: int) -> list[dict]:
    return [
        {
            "message_id": 1000 + i,
            "group_id": GROUP,
            "time": 1730000000 + i,
            "sender": {"user_id": 7, "nickname": "甲"},
            "message_type": "group",
            "post_type": "message",
            "message": [{"type": "text", "data": {"text": f"m{i}"}}],
            "raw_message": f"m{i}",
        }
        for i in range(n)
    ]


@pytest.fixture
async def onebot():
    server = FakeOneBot(_messages(120))
    await server.start()
    client = HttpClient(server.base)
    yield server, client
    await client.close()
    await server.stop()


# ---------------------------------------------------------------- 外壳解包
def test_onebot_dict_envelope_is_unwrapped():
    """`{"messages": [...]}` 不能原样当消息列表：`list(dict)` 是键名。"""
    assert _unwrap_messages({"messages": [1, 2]}) == [1, 2]
    assert _unwrap_messages({"data": {"messages": [3]}}) == [3]
    assert _unwrap_messages({"data": [4]}) == [4]
    assert _unwrap_messages([]) == []
    # `{}` 是「这个群没有文件/公告」的正常回执
    assert _unwrap_messages({}) == []
    # 真拆不出来：交给上层降级成 failed，不能当成「空页」
    assert _unwrap_messages({"error": "boom"}) is None


@pytest.mark.asyncio
async def test_unwrappable_response_is_not_reported_as_success(tmp_path):
    """平台回一句 `{"error": ...}` 时不能当空群：要降 failed。"""

    class Broken(FakeOneBot):
        def _dispatch(self, action, payload):
            if action == "get_group_msg_history":
                return {"error": "not implemented"}
            return super()._dispatch(action, payload)

    server = Broken(_messages(3))
    await server.start()
    client = HttpClient(server.base)
    try:
        db = Database(tmp_path / "db.sqlite")
        engine = BackupEngine(db, tmp_path / "media", {})
        record = await engine.backup_group(GROUP, client, download_media=False)
        assert record.status.value == "failed"
        assert "历史消息接口" in (record.error or "")
    finally:
        await client.close()
        await server.stop()


# ---------------------------------------------------------------- 翻页
@pytest.mark.asyncio
async def test_full_backup_over_real_http_reads_every_message(tmp_path, onebot):
    """真 HTTP、真 JSON：120 条一条不少。"""
    server, client = onebot
    db = Database(tmp_path / "db.sqlite")
    engine = BackupEngine(db, tmp_path / "media", {})

    record = await engine.backup_group(GROUP, client, incremental=False, download_media=False)

    assert record.status.value == "completed", record.error
    assert db.count_messages(GROUP) == 120
    stored = {m.message_id for m in db.get_messages(GROUP)}
    assert stored == {str(1000 + i) for i in range(120)}
    # 翻页锚点必须是「这一页最旧那条」：给成最新那条，第二页就是同一页
    seqs = [p.get("message_seq") for p in server.history_calls()]
    assert seqs[0] is None
    assert all(seq is not None for seq in seqs[1:])
    # 前三次各推进一格；最后那次是「按同一个锚点再问一遍」——第 40 轮起不再
    # 靠「这一页给得少」判到头，必须真拿到空页才算完
    assert len(set(seqs[1:-1])) == len(seqs[1:-1])
    assert seqs[-1] == seqs[-2]


@pytest.mark.asyncio
async def test_newest_first_and_oldest_first_both_read_everything(tmp_path):
    """平台按「新→旧」或「旧→新」返回，结果必须一样。"""
    for reversed_order in (False, True):
        source = _messages(120)
        server = FakeOneBot(source if reversed_order else list(reversed(source)))
        await server.start()
        client = HttpClient(server.base)
        try:
            db = Database(tmp_path / f"db-{reversed_order}.sqlite")
            engine = BackupEngine(db, tmp_path / f"media-{reversed_order}", {})
            await engine.backup_group(GROUP, client, incremental=False, download_media=False)
            assert db.count_messages(GROUP) == 120, ("顺序", reversed_order)
        finally:
            await client.close()
            await server.stop()


@pytest.mark.asyncio
async def test_platform_ignoring_count_still_reads_everything(tmp_path):
    """平台每页只给 20 条（不认 count）：补齐必须真的补齐。"""
    server = FakeOneBot(_messages(120), per_call=20)
    await server.start()
    client = HttpClient(server.base)
    try:
        db = Database(tmp_path / "db.sqlite")
        engine = BackupEngine(db, tmp_path / "media", {})
        record = await engine.backup_group(GROUP, client, incremental=False, download_media=False)
        assert db.count_messages(GROUP) == 120
        assert record.message_count == 120
    finally:
        await client.close()
        await server.stop()


@pytest.mark.asyncio
async def test_paging_does_not_depend_on_a_single_page_being_enough(tmp_path):
    """每页给得比 PAGE_SIZE 少，也不能在第一页就收工。"""
    server = FakeOneBot(_messages(120), per_call=30)
    await server.start()
    client = HttpClient(server.base)
    try:
        db = Database(tmp_path / "db.sqlite")
        engine = BackupEngine(db, tmp_path / "media", {})
        await engine.backup_group(GROUP, client, incremental=False, download_media=False)
        assert db.count_messages(GROUP) == 120
    finally:
        await client.close()
        await server.stop()


@pytest.mark.asyncio
async def test_incremental_over_http_does_not_duplicate(tmp_path, onebot):
    """同一批数据备两次：库里不翻倍，条数不变。"""
    server, client = onebot
    db = Database(tmp_path / "db.sqlite")
    engine = BackupEngine(db, tmp_path / "media", {})
    await engine.backup_group(GROUP, client, incremental=False, download_media=False)
    await engine.backup_group(GROUP, client, incremental=True, download_media=False)
    assert db.count_messages(GROUP) == 120
