"""第 41 轮：按**真群规模**跑一遍（几千条消息、上千人），走备份 → 增量 → 导出。

前面 40 轮验过的东西，绝大多数是在「3 条消息、1 个成员」这种规模上验的。
红线 6 写的是「大群（2000 人以上）必须分页拉取」——分页确实做了，但这个插件
从没按 2000 人的群的量级真跑过一次。照那个规模跑，四处露出来：

1. **每页给得比半页少就收工。** `if len(batch) < PAGE_SIZE // 2: break` ——
   平台把单页封顶在 20 条（NapCat/Lagrange 一类实现常见），第一页 20 < 25
   当场退出。120 条的群备到 20 条，状态 `completed`。第 31 轮修过一次同一条
   判据，但只放宽到「半页」，等于把「每页 30」修好了、「每页 20」还在。
2. **写库一行一次 commit。** 3000 条消息 21 秒，2 万条 175 秒（约 114 条/秒）。
   每行一次 `commit()`，默认 `journal_mode=DELETE` + `synchronous=FULL`，
   每一次都要 fsync 一遍。群里 2 万条历史就是半个多小时，期间机器人卡着。
3. **一页失败整轮作废。** 500 条的群，第 5 页网络抖一下（`RuntimeError`），
   整次备份判 `failed`：已经备上的 200 条留在库里，但回执说失败、没有重试。
   红线 7 要的「超时兜底 + 断点续传」只做到了一半（下一轮能补，这一轮白跑）。
4. **超限的媒体把 `.part` 留在盘上。** `_fetch` 发现文件过大直接 raise，
   临时文件不清；`download` 的清理只在「大小为 0」时触发。20 万条消息里
   哪怕 1% 超限，媒体目录里就是两千个半截文件，`/backup clean` 一个也认不出。
5. **跑到一半重启，记录永远停在 `running`。** 记录是「开跑时先写一条
   running、跑完再改状态」，进程被杀/宿主重启就卡在中间：`/backup list`
   永远显示这条 `running`、`/backup status` 认不出它保住了什么、恢复被
   「状态为 running，不可恢复」挡回去——而它其实已经保住了上千条消息。

顺手补一条**文档与代码打架**的：README 说私聊消息落在虚拟群号 `-1`，
`list` / `export -1 html` 照常用；而 `parse_group_arg` 要求 5 位以上数字，
`-1` 一路返回 None，那几条命令只会回「请指定群号」。
"""

import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backup.core.backup_engine import BackupEngine
from backup.core.commands import parse_group_arg
from backup.core.database import Database
from backup.core.media_downloader import MediaDownloader
from backup.core.models import BackupRecord, BackupStatus
from backup.core.parser import parse_message

BASE = datetime(2024, 5, 1, 10, 0, 0)
GROUP = 100


def raw(i: int, group_id: int = GROUP) -> dict:
    return {
        "message_id": f"m{i}",
        "group_id": group_id,
        "sender": {"user_id": 7, "nickname": "甲"},
        "timestamp": (BASE + timedelta(seconds=i)).timestamp(),
        "message": [{"type": "text", "data": {"text": f"消息 {i}"}}],
    }


def history(n: int) -> list[dict]:
    return [raw(i) for i in range(n)]  # 旧 → 新


class _Capped:
    """每页**封顶 20 条**的平台。

    这是这类实现的真面目：它认 `count`，但自己有上限；或者干脆不认，固定给一页。
    两种在引擎看来是同一件事——「我要 50，它给 20」。
    """

    def __init__(self, items, per_page=20):
        self.items = items
        self.per_page = per_page
        self.calls = 0

    async def get_group_info(self, group_id=None, **kwargs):
        return {"group_id": GROUP, "group_name": "大群"}

    async def get_group_members(self, group_id=None, **kwargs):
        return []

    async def get_group_announcements(self, group_id=None, **kwargs):
        return []

    async def get_group_files(self, group_id=None, **kwargs):
        return []

    async def get_group_history(self, group_id=None, count=50, offset=0, **kwargs):
        self.calls += 1
        return self.items[offset : offset + min(count, self.per_page)]


class _CappedBySeq:
    """只认 `message_seq`（OneBot v11），每次最多 20 条。"""

    def __init__(self, items, per_page=20):
        self.items = items
        self.per_page = per_page
        self.calls = 0

    async def get_group_info(self, group_id=None, **kwargs):
        return {"group_id": GROUP, "group_name": "大群"}

    async def get_group_members(self, group_id=None, **kwargs):
        return []

    async def get_group_announcements(self, group_id=None, **kwargs):
        return []

    async def get_group_files(self, group_id=None, **kwargs):
        return []

    async def get_group_history(self, group_id=None, count=50, message_seq=None, **kwargs):
        self.calls += 1
        if message_seq is None:
            window = self.items[-self.per_page :]
        else:
            end = next(
                (i for i, m in enumerate(self.items) if str(m["message_id"]) == str(message_seq)),
                None,
            )
            if end is None:
                return []
            window = self.items[max(0, end - self.per_page + 1) : end + 1]
        return list(reversed(window))  # OneBot 给「新 → 旧」


@pytest.fixture
def engine(db, tmp_path):
    return BackupEngine(db, tmp_path / "media", {"scope": {"download_media": False}})


# ---------------------------------------------------------------- 1. 分页漏备
async def test_capped_page_size_does_not_end_paging(engine, db):
    """每页只给 20 条（要 50）不代表没了。120 条的群必须备满 120。"""
    platform = _Capped(history(120))
    record = await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    assert db.count_messages(GROUP) == 120, f"只备到 {db.count_messages(GROUP)} 条"
    assert record.message_count == 120
    assert record.status is BackupStatus.COMPLETED


async def test_capped_seq_paging_does_not_end_paging(engine, db):
    """只认 message_seq、每次 20 条：凑够 50 靠补齐，收工判据不能看「给得少」。"""
    platform = _CappedBySeq(history(120))
    record = await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    assert db.count_messages(GROUP) == 120, f"只备到 {db.count_messages(GROUP)} 条"
    assert record.status is BackupStatus.COMPLETED


async def test_paging_stops_on_empty_page_not_on_short_page(engine, db):
    """翻到底的判据是「平台回空页 / 原地打转」，不是「这一页不足半页」。"""
    platform = _Capped(history(70), per_page=20)
    await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    assert db.count_messages(GROUP) == 70
    # 20/20/20/10/空 —— 最后一次必须真去问过，才知道没了
    assert platform.calls >= 5


# ---------------------------------------------------------------- 2. 写库开销
async def test_large_group_backup_is_not_glacial(engine, db):
    """3000 条消息的写入不该按「一条一次 fsync」的节奏走。

    旧代码 3000 条约 21 秒（约 140 条/秒），2 万条的消息量就是半小时。这里卡
    一个宽松的上限：真出问题（退回逐行提交）会明显超；正常实现有 10 倍余量。
    """
    platform = _Capped(history(3000), per_page=50)
    started = time.monotonic()
    record = await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    elapsed = time.monotonic() - started
    assert record.message_count == 3000
    assert db.count_messages(GROUP) == 3000
    assert elapsed < 8.0, f"3000 条用了 {elapsed:.1f}s，逐行提交又回来了"


async def test_batch_write_keeps_rows_visible_outside(engine, db):
    """批量提交只是攒事务，写完必须真的落库——不能被下一次异常回滚掉。"""
    platform = _Capped(history(60), per_page=20)
    await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    db.close()
    again = Database(db.path)
    try:
        assert again.count_messages(GROUP) == 60
    finally:
        again.close()


# ---------------------------------------------------------------- 3. 一页失败
class _FlakyPage(_Capped):
    """第 4 页起网络抖一次，之后恢复正常。"""

    def __init__(self, items, per_page=50, fail_on=4, times=1):
        super().__init__(items, per_page)
        self.fail_on = fail_on
        self.times = times
        self.failed = 0

    async def get_group_history(self, group_id=None, count=50, offset=0, **kwargs):
        # `times=1` 时只抖一次（重试能救回来），`times` 大就一直是坏的
        if self.calls + 1 >= self.fail_on and self.failed < self.times:
            self.calls += 1
            self.failed += 1
            raise RuntimeError("网络超时")
        return await super().get_group_history(group_id, count=count, offset=offset, **kwargs)


async def test_one_bad_page_does_not_throw_away_the_whole_backup(engine, db):
    """单页失败要重试；重试成功就照常跑完，不能再「整轮 failed」。"""
    platform = _FlakyPage(history(300), per_page=50, fail_on=4)
    record = await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    assert db.count_messages(GROUP) == 300, f"只备到 {db.count_messages(GROUP)} 条"
    assert record.status is BackupStatus.COMPLETED


async def test_page_failure_that_never_recovers_keeps_progress(engine, db):
    """重试也失败：降成 partial，说清第几页、已保住多少，不能一条不留地报 failed。"""
    platform = _FlakyPage(history(300), per_page=50, fail_on=4, times=99)
    record = await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    assert record.status is BackupStatus.PARTIAL
    assert db.count_messages(GROUP) == 150, "前三页必须留住"
    assert record.message_count == 150
    assert "第 4 页" in (record.error or "")


async def test_next_run_after_page_failure_finishes_the_job(engine, db):
    """断点续传：上一轮卡在第 4 页，这一轮要能把剩下的补齐。"""
    platform = _FlakyPage(history(300), per_page=50, fail_on=4, times=99)
    await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    healthy = _Capped(history(300), per_page=50)
    record = await engine.backup_group(GROUP, healthy, incremental=True, download_media=False)
    assert db.count_messages(GROUP) == 300
    assert record.status is BackupStatus.COMPLETED


# ---------------------------------------------------------------- 4. 残渣
class _Chunks:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for c in self.chunks:
            yield c


class _Resp:
    def __init__(self, chunks, status=200, headers=None):
        self.content = _Chunks(chunks)
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, factory):
        self.factory = factory

    def get(self, url):
        return self.factory()

    async def close(self):
        return None


async def test_oversize_download_leaves_no_part_file(tmp_path):
    """超限中断的下载不能把半截 `.part` 留在媒体目录里。"""
    dl = MediaDownloader(tmp_path, {"max_file_size": "1KB", "retry_count": 1})
    session = _Session(lambda: _Resp([b"x" * 64 for _ in range(64)]))
    with pytest.raises(Exception):
        await dl.download("http://x/big.bin", "m1", BASE, session=session)
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftovers == [], f"留下残渣：{leftovers}"


async def test_failed_download_retry_does_not_leave_part_files(tmp_path):
    """重试用尽后同样不能留残渣——重试路径原来只在「大小为 0」时清。"""
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _Resp([], status=500)

    dl = MediaDownloader(tmp_path, {"retry_count": 2, "timeout": 1})
    with pytest.raises(Exception):
        await dl.download("http://x/down.bin", "m2", BASE, session=_Session(factory))
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


# ---------------------------------------------------------------- 5. 私聊群号
def test_private_archive_group_is_addressable_from_commands():
    """README 写着 `export -1 html` 照常用；`-1` 必须能被当成群号解析出来。"""
    assert parse_group_arg("-1") == -1
    assert parse_group_arg("10023") == 10023
    # 短群号也认（OneBot 的群号下限没人保证是 5 位，测试群常是 3 位）
    assert parse_group_arg("999") == 999
    # Telegram 超级群是 -100 开头的负数，不能因为「看着像负数」就判非法
    assert parse_group_arg("-1001234567890") == -1001234567890
    assert parse_group_arg("0") is None
    assert parse_group_arg("abc") is None


# ---------------------------------------------------------------- 6. 僵尸记录
def test_interrupted_backup_record_is_reconciled_on_startup(db):
    """进程被杀留下的 `running` 记录要能被认领：降级 + 登记来源，不再永远挂着。"""
    from backup.core.commands import render_backup_list

    base = datetime.now() - timedelta(hours=2)
    db.save_record(
        BackupRecord(
            backup_id="bk_half",
            group_id=GROUP,
            status=BackupStatus.RUNNING,
            started_at=base,
            message_count=0,
        )
    )
    for i in range(3):
        db.save_message(parse_message(raw(i), GROUP), "bk_half")

    fixed = db.reconcile_stale_records(base + timedelta(minutes=10))
    assert fixed == 1
    record = db.get_record("bk_half")
    assert record.status is BackupStatus.PARTIAL
    assert record.message_count == 3
    assert "中断" in (record.error or "")
    # 恢复端只认 completed / partial：修完之后这个备份真的能恢复
    assert record.status in (BackupStatus.COMPLETED, BackupStatus.PARTIAL)
    assert "running" not in render_backup_list(db, GROUP)
    # 那三条消息必须进这份备份的来源清单，否则导出/恢复都取不到
    assert db.count_sources("bk_half") == 3


def test_freshly_started_record_is_not_touched(db):
    """正在跑的那条不能被动：它还没结束，`running` 是事实。"""
    now = datetime.now()
    db.save_record(
        BackupRecord(
            backup_id="bk_live",
            group_id=GROUP,
            status=BackupStatus.RUNNING,
            started_at=now,
            message_count=0,
        )
    )
    assert db.reconcile_stale_records(now - timedelta(minutes=30)) == 0
    assert db.get_record("bk_live").status is BackupStatus.RUNNING


def test_reconcile_with_nothing_to_fix_is_a_noop(db):
    assert db.reconcile_stale_records(datetime.now()) == 0


# ---------------------------------------------------------------- 7. 整页回滚
async def test_page_write_is_all_or_nothing(engine, db, monkeypatch):
    """一页写到一半出错：这一页不能留下半批行，库里只能是整页的整数倍。"""
    platform = _Capped(history(150), per_page=50)
    original = db.save_message
    seen = {"n": 0}

    def boom(msg, backup_id):
        seen["n"] += 1
        if seen["n"] == 70:  # 第二页第 20 条
            raise RuntimeError("磁盘满了")
        return original(msg, backup_id)

    monkeypatch.setattr(db, "save_message", boom)
    record = await engine.backup_group(GROUP, platform, incremental=False, download_media=False)
    assert record.status is BackupStatus.FAILED
    # 第一页 50 条整页落库，第二页那 19 条随异常一起回滚
    assert db.count_messages(GROUP) == 50, db.count_messages(GROUP)


# ---------------------------------------------------------------- 8. 并发同一个 URL
async def test_two_concurrent_downloads_of_one_url_both_succeed(tmp_path):
    """同一个 URL 被两处同时下（实时补下撞上定时备份）不该有人报失败。

    临时文件名原来固定是 `<正式名>.part`：先完成的那次把它换走了，另一次
    `replace` 找不到源文件，抛 `FileNotFoundError`，媒体被记成「下载失败」——
    而盘上明明有一份完整的。
    """
    body = b"x" * 4096

    class SlowChunks(_Chunks):
        """分块吐、块间让出事件循环——两个下载才真的交错在同一个文件上。"""

        async def iter_chunked(self, size):
            for chunk in self.chunks:
                await asyncio.sleep(0.01)
                yield chunk

    class SlowResp(_Resp):
        def __init__(self):
            super().__init__([body[i : i + 512] for i in range(0, len(body), 512)])
            self.content = SlowChunks(self.content.chunks)

    dl = MediaDownloader(tmp_path, {"retry_count": 1})
    session = _Session(lambda: SlowResp())
    first, second = await asyncio.gather(
        dl.download("http://x/same.png", "m1", BASE, session=session),
        dl.download("http://x/same.png", "m1", BASE, session=session),
    )
    assert first == second
    assert Path(first).read_bytes() == body
    assert [p for p in tmp_path.rglob("*.part")] == []


async def test_backup_with_media_survives_two_runs_at_once(tmp_path):
    """两个备份撞在一起（连点两次 `/backup now`）：媒体都算成功，不虚报失败。"""
    from aiohttp import web

    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 256
    app = web.Application()

    async def handler(request):
        await asyncio.sleep(0.02)
        return web.Response(body=payload, content_type="image/png")

    app.router.add_get("/{name}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        def media_raw(i):
            item = raw(i)
            item["message"] = [
                {"type": "image", "data": {"file": f"http://127.0.0.1:{port}/p{i}.png"}}
            ]
            return item

        class WithMedia(_Capped):
            def __init__(self):
                super().__init__([media_raw(i) for i in range(20)], per_page=20)

            async def get_group_history(self, group_id=None, count=50, offset=0, **kwargs):
                self.calls += 1
                return [] if offset else self.items

        db = Database(tmp_path / "db.sqlite")
        engine = BackupEngine(
            db,
            tmp_path / "media",
            {"scope": {"download_media": True}, "media": {"retry_count": 1, "max_concurrent": 3}},
        )
        first, second = await asyncio.gather(
            engine.backup_group(GROUP, WithMedia(), incremental=False, download_media=True),
            engine.backup_group(GROUP, WithMedia(), incremental=False, download_media=True),
        )
        for record in (first, second):
            assert record.status is BackupStatus.COMPLETED, record.error
        assert db.count_messages(GROUP) == 20
        assert [p for p in (tmp_path / "media").rglob("*.part")] == []
    finally:
        await runner.cleanup()


def test_private_group_commands_actually_work_end_to_end(db, tmp_path):
    """`-1` 不只是「能解析」：`status` / `list` / `export` 拿它真能跑。"""
    from backup.core.commands import render_status
    from backup.core.export_manager import ExportManager
    from backup.core.models import GroupInfo

    private = -1
    db.save_group_info(GroupInfo(group_id=private, group_name="私聊"))
    for i in range(3):
        # 私聊消息的原始数据里本来就没有群号，`parse_message` 才会用兜底群号；
        # 这里把 group_id 去掉，走的就是实时归档那条路
        item = raw(i)
        item.pop("group_id")
        db.save_message(parse_message(item, private), "bk_private")

    text = render_status(db, private)
    assert "私聊" in text and "3" in text

    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    result = exports.export_latest(private, "html")
    assert result.message_count == 3
    assert "私聊" in Path(result.path).read_text(encoding="utf-8")


def test_stale_record_with_tens_of_thousands_of_messages(db):
    """收编一条 2 万条的僵尸记录：不能因为拼 `IN (?,?,…)` 撞 SQLite 变量上限。"""
    base = datetime.now() - timedelta(hours=3)
    db.save_record(
        BackupRecord(
            backup_id="bk_big",
            group_id=GROUP,
            status=BackupStatus.RUNNING,
            started_at=base,
            message_count=0,
        )
    )
    with db.batch():
        for i in range(20000):
            db.save_message(parse_message(raw(i), GROUP), "bk_big")

    assert db.reconcile_stale_records(base + timedelta(minutes=5)) == 1
    record = db.get_record("bk_big")
    assert record.status is BackupStatus.PARTIAL
    assert record.message_count == 20000
    assert db.count_sources("bk_big") == 20000
