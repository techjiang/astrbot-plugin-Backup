"""第 40 轮：`/backup config set` 的每一句「✅ 已设置」都是假的。

这一轮的起点不是读代码，是把 schema 里的**每一项**都 `config set` 一遍，然后回头
问运行中的组件「你变了吗」。15 项，15 项全没变：

```python
p = BackupPlugin(Context())          # 组件在这个时刻把配置抄成自己的属性
await _cmd_config(ev, ["set", "media.timeout", "5"])   # 改的是 config 字典
p.engine.media.timeout               # → 30（还是构造时那个）
```

回执写的是「✅ `media.timeout` = `5`」——它只说配置字典变了，一个字都没提
「这次运行仍然按 30 秒超时」。用户按回执以为改好了，实际得重载插件。

三个连带问题一起查了：

1. `scope.group_members` 这一项在 `_conf_schema.json` 和 README 里都写着，代码里
   **一次都没读过**——关掉它照样每轮拉一次全员名单，2000 人的群就是一次几千条。
2. 私聊归档按 `scope.private_messages` 落进 `-1` 这个虚拟群号，可
   `parse_group_arg` 的下限是 5 位数字，`/backup export -1 zip` 永远回
   「请指定群号」。数据在库里，没有任何命令导得出来。
3. `dashboard.public_url` 也没人读，`_dashboard_host()` 是全仓零调用点的死函数。
   这和上一轮删掉的 `_public_base_url` 是同一个形状，只是它多活了一轮。
"""

import asyncio
import shutil
import sys
import types
from pathlib import Path

import pytest

from backup.core.commands import parse_group_arg

from .conftest import load_plugin_for_real, real_astrbot_available


# ---------------------------------------------------------------- 夹具


class _Ev:
    """命令层要的最小事件：群内、管理员、能回话。"""

    message_str = ""

    def __init__(self, group_id="100", private=False, admin=True):
        self._group_id = group_id
        self._private = private
        self._admin = admin
        self.message_obj = None

    def get_group_id(self):
        return self._group_id

    def is_private_chat(self):
        return self._private

    def is_admin(self):
        return self._admin

    def get_sender_id(self):
        return "7"

    def plain_result(self, text):
        return text


def _run(coro):
    return asyncio.run(coro)


def _replies(event, command, args):
    async def _go():
        return [text async for text in command(event, args)]

    return _run(_go())


class _InjectedConfig(dict):
    """面板注入的那份配置。

    `AstrBotConfig` 是个 dict 子类、自己带 `save_config()`。这里照这个契约造一份
    最小实现：`config set` 会走「写回注入的那份」这条分支，于是**不会**去动仓库里
    的 `config.yaml`——那样跑一次用例就把默认配置改了。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1
        return True


@pytest.fixture
def plugin_factory(tmp_path, monkeypatch):
    """造一个真的插件实例：真 AstrBot 的 `Star`/`Context` + 真配置加载链。

    插件目录整个 copy 到临时目录：`/backup config set` 在「没有注入配置」时会
    写 `PLUGIN_DIR/config.yaml`，不能让它写回仓库。
    """
    source = Path(__file__).resolve().parent.parent
    plugin_dir = tmp_path / "plug"
    shutil.copytree(
        source,
        plugin_dir,
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )

    created = []

    def make(config=None):
        injected = _InjectedConfig(storage_dir=str(tmp_path / "storage"))
        for key, value in (config or {}).items():
            injected[key] = value
        instance, _ctx, _mod = load_plugin_for_real(
            monkeypatch,
            plugin_dir=plugin_dir,
            config=injected,
        )
        created.append(instance)
        return instance

    yield make

    for instance in created:
        try:
            instance.gateway.stop()
        except Exception:
            pass
        try:
            instance.scheduler.shutdown()
        except Exception:
            pass
        try:
            instance.db.close()
        except Exception:
            pass


def _runtime_snapshot(plugin) -> dict:
    """**运行中的组件**现在手上的值，不是 `self.config` 里的值。"""
    return {
        "permission.require_admin": plugin.permission.require_admin,
        "permission.allow_private": plugin.permission.allow_private,
        "media.max_file_size": plugin.engine.media.max_file_size,
        "media.max_concurrent": plugin.engine.media.max_concurrent,
        "media.retry_count": plugin.engine.media.retry_count,
        "media.timeout": plugin.engine.media.timeout,
        "export.download_link_expire": plugin.exports.expire_seconds,
        "export.base_url": plugin.exports.base_url,
        "schedule.enabled": plugin.scheduler.enabled,
        "scope.group_messages": plugin.hooks.archive,
        "scope.private_messages": plugin.hooks.archive_private,
        "auto_trigger.mass_leave_threshold": plugin.auto_trigger.mass_leave_threshold,
        "auto_trigger.on_group_disband": plugin.auto_trigger.on_group_disband,
        "auto_trigger.on_bot_kicked": plugin.hooks.on_leave,
        "gateway.enabled": plugin.gateway.running,
    }


#: 每一项都是「配置里改一下，运行中的行为必须跟着变」。值挑得和默认值不同，
#: 而且不选「改成默认值」那种改法——那种改法在旧代码上会因为「反正一样」而蒙对。
TAKES_EFFECT = [
    ("permission.require_admin", "false"),
    ("permission.allow_private", "false"),
    ("media.max_file_size", "1MB"),
    ("media.max_concurrent", "1"),
    ("media.retry_count", "1"),
    ("media.timeout", "5"),
    ("export.download_link_expire", "1m"),
    ("export.base_url", "https://bak.example.com"),
    ("schedule.enabled", "false"),
    ("scope.group_messages", "false"),
    ("scope.private_messages", "true"),
    ("auto_trigger.mass_leave_threshold", "3"),
    ("auto_trigger.on_group_disband", "false"),
    ("auto_trigger.on_bot_kicked", "false"),
    ("gateway.enabled", "false"),
]


@pytest.mark.parametrize("key,raw", TAKES_EFFECT)
def test_config_set_changes_the_running_component(plugin_factory, key, raw):
    """`config set` 之后，干活的组件必须用新值。旧代码上 15 条全红。"""
    plugin = plugin_factory()
    before = _runtime_snapshot(plugin)
    replies = _replies(_Ev(), plugin._cmd_config, ["set", key, raw])
    after = _runtime_snapshot(plugin)

    assert f"✅ `{key}`" in replies[-1], replies[-1]
    assert before[key] != after[key], (
        f"`{key}` 改成了 {raw}，回执说已设置，但运行中的组件仍是 {before[key]!r}；"
        f"回执：{replies[-1]}"
    )


def test_media_limits_reach_the_downloader_that_does_the_work(plugin_factory):
    """不只是属性变了，实际下载要按新上限走。

    `max_concurrent` 落在 `asyncio.Semaphore` 里，而信号量额度在构造时就定死了——
    只改属性不改信号量，等于没改。
    """
    plugin = plugin_factory()
    _replies(_Ev(), plugin._cmd_config, ["set", "media.max_concurrent", "1"])
    _replies(_Ev(), plugin._cmd_config, ["set", "media.max_file_size", "2MB"])

    media = plugin.engine.media
    assert media.max_file_size == 2 * 1024 ** 2
    assert media.max_concurrent == 1
    # `Semaphore._value` 是内部字段，但这个断言的意义就是「额度真的重建了」：
    # 只 `media.max_concurrent = 1` 时它仍然是 3
    assert media._sem._value == 1


def test_export_base_url_takes_effect_without_a_reload(plugin_factory):
    """填完 `export.base_url` 再导出，链接就得带着它。

    这里必须断言 `ExportManager` 自己那份 `base_url`，不能只断言
    `download_link()` 的返回值：`download_link` 每次都直接读 `self.config`，所以
    **旧代码上它也是对的**（第一版这条用例因此蒙过）。真正滞后的是
    `ExportManager.base_url`——它在 `__init__` 里抄了一份，`ExportResult.link`
    用的就是那一份。用户照着回执的提示（「把 `export.base_url` 填成网关的对外
    地址」）填好、重新导出，拿到的 `link` 还是裸路径。
    """
    plugin = plugin_factory()
    assert plugin.download_link("tok") == "/backup/download/tok"
    assert plugin.exports.base_url == ""

    replies = _replies(_Ev(), plugin._cmd_config, ["set", "export.base_url", "https://bak.example.com"])
    assert "✅" in replies[-1]

    assert plugin.exports.base_url == "https://bak.example.com"
    assert plugin.download_link("tok") == "https://bak.example.com/backup/download/tok"
    # 导出产物自带的 link 也得跟着走：那条链接是用户真正复制走了的东西
    plugin.db.save_message(
        __import__("backup.core.models", fromlist=["Message"]).Message(
            message_id="m1",
            group_id=100,
            sender_id=1,
            sender_name="甲",
            timestamp=__import__("datetime").datetime(2024, 5, 1, 10, 0, 0),
            message_type=__import__("backup.core.models", fromlist=["MessageType"]).MessageType.TEXT,
            content="你好",
            raw_content="{}",
        ),
        "bk1",
    )
    result = plugin.exports.export_latest(100, "zip")
    assert result.link == f"https://bak.example.com/backup/download/{result.token}", result.link


async def test_schedule_switch_actually_unhooks_running_jobs(plugin_factory):
    """`schedule.enabled=false` 要把已经挂上去的任务撤下来，而不是只记个标记。

    原来这个开关只在构造时读：`/backup config set schedule.enabled false` 回执
    「已设置」，而已经挂在调度器上的任务照旧在第二天凌晨三点跑一遍备份。

    这条必须是 async：`AsyncIOScheduler.start()` 要有运行中的事件循环，没有就
    降级成「只落库不调度」（那条降级本身是对的，见 `scheduler._ensure_scheduler`），
    在那种状态下「任务撤没撤」根本无从验起。
    """
    plugin = plugin_factory()
    plugin.scheduler.add_job(100, "0 3 * * *")
    sched = plugin.scheduler._scheduler
    assert sched is not None, "有事件循环时调度器应该真起来"
    assert [j.id for j in sched.get_jobs()] == ["backup_group_100"]

    async def set_key(key, raw):
        return [text async for text in plugin._cmd_config(_Ev(), ["set", key, raw])][-1]

    assert "✅" in await set_key("schedule.enabled", "false")
    assert [j.id for j in sched.get_jobs()] == []
    # 库里的 cron 记录要留着：再打开总闸就全回来，不能连配置一起删
    assert [row["group_id"] for row in plugin.db.list_schedules()] == [100]

    assert "✅" in await set_key("schedule.enabled", "true")
    assert [j.id for j in plugin.scheduler._scheduler.get_jobs()] == ["backup_group_100"]


def test_gateway_can_be_turned_off_and_on_at_runtime(plugin_factory):
    """网关开关是绑 socket，不是读个值：关掉要真的 shutdown，打开要真的 listen。"""
    plugin = plugin_factory()
    assert plugin.gateway.running

    _replies(_Ev(), plugin._cmd_config, ["set", "gateway.enabled", "false"])
    assert not plugin.gateway.running

    _replies(_Ev(), plugin._cmd_config, ["set", "gateway.enabled", "true"])
    assert plugin.gateway.running


def test_storage_dir_change_is_refused_honestly(plugin_factory, tmp_path):
    """`storage_dir` 热切等于把库和文件劈成两半，改不了——但必须说出来。"""
    plugin = plugin_factory()
    original = plugin.storage
    target = tmp_path / "elsewhere"

    replies = _replies(_Ev(), plugin._cmd_config, ["set", "storage_dir", str(target)])
    text = replies[-1]
    assert "重启" in text, text
    assert str(original) in text, text
    # 运行中的存储目录一个字节都不能动
    assert plugin.storage == original
    assert plugin.db.path.parent.parent == original


# ---------------------------------------------------------------- 成员开关


class _RecordingPlatform:
    """记下「成员列表被要过几次」。"""

    def __init__(self, history=None, members=None):
        self.history = history or []
        self.members = members or []
        self.member_calls = 0

    async def get_group_info(self, group_id):
        return {"group_id": group_id, "group_name": "测试群"}

    async def get_group_members(self, group_id):
        self.member_calls += 1
        return list(self.members)

    async def get_group_announcements(self, group_id):
        return []

    async def get_group_files(self, group_id):
        return []

    async def get_group_history(self, group_id, count=50, offset=0):
        return self.history[offset : offset + count]


def _history(n, group_id=100):
    out = []
    for i in range(n):
        out.append(
            {
                "message_id": f"m{i}",
                "group_id": group_id,
                "sender": {"user_id": 1, "nickname": "甲"},
                "timestamp": 1714000000 + i,
                "message": [{"type": "text", "data": {"text": f"消息 {i}"}}],
            }
        )
    return out


def test_group_members_switch_is_honoured(tmp_path):
    """关掉 `scope.group_members` 就别再去要全员名单。

    这一项原来是**空头支票**：schema 里写着、README 里写着「成员列表」，代码里
    一次都没读过。2000 人的群每轮多一次几千条的请求，而用户关它的理由
    （省时间、省配额、平台不给权限）一条都没兑现。
    """
    from backup.core.backup_engine import BackupEngine
    from backup.core.database import Database

    db = Database(tmp_path / "db.sqlite")
    try:
        off = BackupEngine(
            db,
            tmp_path / "media",
            {"scope": {"group_members": False, "download_media": False}},
        )
        platform = _RecordingPlatform(_history(3), [{"user_id": 1, "nickname": "甲"}])
        _run(off.backup_group(100, platform, incremental=False, download_media=False))
        assert platform.member_calls == 0
        assert db.get_members(100) == []
    finally:
        db.close()

    db = Database(tmp_path / "db2.sqlite")
    try:
        on = BackupEngine(
            db,
            tmp_path / "media2",
            {"scope": {"group_members": True, "download_media": False}},
        )
        platform = _RecordingPlatform(_history(3), [{"user_id": 1, "nickname": "甲"}])
        _run(on.backup_group(100, platform, incremental=False, download_media=False))
        assert platform.member_calls == 1
        assert [m.user_id for m in db.get_members(100)] == [1]
    finally:
        db.close()


def test_group_members_switch_can_be_flipped_at_runtime(plugin_factory):
    """`config set scope.group_members false` 之后立刻不再拉。"""
    plugin = plugin_factory()
    _replies(_Ev(), plugin._cmd_config, ["set", "scope.group_members", "false"])
    assert plugin.config["scope"]["group_members"] is False

    platform = _RecordingPlatform(_history(2), [{"user_id": 1, "nickname": "甲"}])
    _run(plugin.engine.backup_group(100, platform, incremental=False, download_media=False))
    assert platform.member_calls == 0


# ---------------------------------------------------------------- 私聊归档能寻址


def test_private_archive_group_id_is_addressable():
    """`/backup export -1 zip` 得能解析出群号。

    私聊归档落在虚拟群号 `-1`。`parse_group_arg` 原来的下限是 5 位数字，于是
    这条命令永远回「请指定群号」——消息在库里，没有任何命令导得出来。
    """
    from backup.core.models import PRIVATE_ARCHIVE_GROUP

    assert PRIVATE_ARCHIVE_GROUP == -1
    assert parse_group_arg("-1") == PRIVATE_ARCHIVE_GROUP
    # 普通的群号别被这次放宽搞坏
    assert parse_group_arg("123456") == 123456
    assert parse_group_arg("-100123") == -100123
    assert parse_group_arg("abc") is None
    assert parse_group_arg("0") is None
    assert parse_group_arg(None) is None


def _private_event(text="私聊内容"):
    class Msg:
        type = "FriendMessage"
        self_id = "1"
        message_id = "pm1"
        sender = types.SimpleNamespace(user_id="7", nickname="甲")
        message = [types.SimpleNamespace(type="Plain", text=text)]
        message_str = text
        raw_message = {}
        timestamp = 1714000000

    class Ev:
        message_obj = Msg()
        message_str = text

        def get_group_id(self):
            return ""

        def get_sender_id(self):
            return "7"

        def is_private_chat(self):
            return True

        def is_admin(self):
            return False

        def plain_result(self, reply):
            return reply

    return Ev()


def test_private_archive_can_be_listed_and_exported(plugin_factory):
    """私聊归档写进库了，就得能 status 出来、能导出成包。"""
    from backup.core.models import PRIVATE_ARCHIVE_GROUP

    plugin = plugin_factory({"scope": {"private_messages": True}})
    plugin.hooks.archive_message(_private_event())
    assert plugin.db.count_messages(PRIVATE_ARCHIVE_GROUP) == 1

    status = _replies(_Ev(), plugin._cmd_status, ["-1"])[-1]
    assert "私聊归档" in status, status
    assert "消息总数：1" in status, status
    # 内部约定的负数群号不该原样甩到用户脸上
    assert "群 -1" not in status, status

    receipt = _replies(_Ev(), plugin._cmd_export, ["-1", "zip"])[-1]
    assert "导出完成" in receipt, receipt
    assert "消息 1 条" in receipt, receipt


def test_private_archive_export_produces_a_real_package(plugin_factory):
    """回执说导出完成，盘上就得有一个真包，里头真有那条私聊。"""
    import zipfile

    from backup.core.models import PRIVATE_ARCHIVE_GROUP

    plugin = plugin_factory({"scope": {"private_messages": True}})
    plugin.hooks.archive_message(_private_event("别删我"))
    result = plugin.exports.export_latest(PRIVATE_ARCHIVE_GROUP, "zip")

    assert result.path.is_file()
    with zipfile.ZipFile(result.path) as zf:
        assert "chat.html" in zf.namelist()
        payload = zf.read("messages.txt").decode("utf-8")
    assert "别删我" in payload


# ---------------------------------------------------------------- 死配置


def test_no_dead_config_keys():
    """schema 里每一层配置项，都要有代码读它。

    `dashboard.public_url` 和 `scope.group_members` 都是这个形状：面板上摆着、
    README 里写着，代码里零引用。用户填了不生效，比面板上没有这一项更糟。
    """
    import json
    import re

    root = Path(__file__).resolve().parent.parent
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    sources = "\n".join(
        p.read_text(encoding="utf-8") for p in root.rglob("*.py") if "tests" not in p.parts
    )

    leaves = []

    def walk(node, prefix):
        for key, spec in node.items():
            if spec.get("type") == "object":
                walk(spec.get("items") or {}, prefix + [key])
            else:
                leaves.append((prefix + [key], spec))

    walk(schema, [])

    dead = []
    for path, spec in leaves:
        leaf = path[-1]
        # 只认「按名字取这一项」的写法：`.get("leaf")` / `["leaf"]" / `endswith("leaf")`
        patterns = (
            rf'\.get\(\s*"{re.escape(leaf)}"',
            rf"\.get\(\s*'{re.escape(leaf)}'",
            rf'\["{re.escape(leaf)}"\]',
            rf"endswith\(\s*\"{re.escape(leaf)}\"",
        )
        if not any(re.search(p, sources) for p in patterns):
            dead.append(".".join(path))

    assert not dead, f"这些配置项在代码里没人读：{dead}"


def test_dashboard_section_is_removed():
    """`dashboard.public_url` 连同 `_dashboard_host()` 一起删了。

    第 33 轮删过同形状的 `_public_base_url`（写全了、零调用点）。`_dashboard_host`
    是第二个：它自己去翻宿主的 `cmd_config.json`，可它自己也没有任何调用点；
    两条旧用例只是把它 `monkeypatch` 掉，没有一条在验它有用。
    """
    root = Path(__file__).resolve().parent.parent
    main_src = (root / "main.py").read_text(encoding="utf-8")
    assert "_dashboard_host" not in main_src
    assert "dashboard" not in __import__("json").loads(
        (root / "_conf_schema.json").read_text(encoding="utf-8")
    )
