"""第 20 轮：按 AstrBot 4.14.6 的真实源码核对接口。

前面 19 轮都在自洽的假设里找矛盾——代码和测试用的是同一套臆想出来的 API。
这轮把 AstrBot 4.14.6 的源码拉下来逐处对照，发现「实机联调才能确认」的那几处
根本不是猜测问题，是形状对不上：

- `ComponentType` 是 `class ComponentType(str, Enum)`，`str()` 得到
  `"ComponentType.Plain"`，查表永远失败 → 图片/语音/文件/@/引用全丢；
- 适配器不提供 `get_group_history`，拉数据要走 OneBot `call_action`；
  而 OneBot 的历史消息接口只认 `message_seq`，不认 offset；
- `is_admin()` / `is_private_chat()` / `get_group_id()` 都是**方法**，原来按
  布尔属性和普通属性读；
- OneBot notice 事件被包成普通消息事件，原始事件在 `message_obj.raw_message`。

每个用例都是先写脚本复现、改完同一脚本重跑看着它变正确，再固化在这里。
"""

import asyncio
import inspect
import json
import zipfile
from datetime import datetime
from enum import Enum

import pytest

from backup.core.auto_trigger import AutoTrigger
from backup.core.backup_engine import BackupEngine, PlatformAdapter
from backup.core.commands import PermissionChecker
from backup.core.database import Database
from backup.core.event_hooks import EventHooks, extract_group_id, notice_payload
from backup.core.export_manager import ExportManager
from backup.core.parser import parse_message
from backup.core.scheduler import BackupScheduler
from backup.core.verifier import verify_zip

from .conftest import BASE_TIME, FakePlatform, make_message, raw_history

GROUP = 100


# ---------------------------------------------------------------- AstrBot 替身
class ComponentType(str, Enum):
    """照抄 astrbot.core.message.components.ComponentType 的形状。"""

    Plain = "Plain"
    Image = "Image"
    Record = "Record"
    File = "File"
    At = "At"
    Reply = "Reply"
    Nodes = "Nodes"


class Component:
    """消息段：字段直接挂在对象上，没有 OneBot 那层 `data`。"""

    def __init__(self, ctype, **kwargs):
        self.type = ctype
        for key, value in kwargs.items():
            setattr(self, key, value)


class MessageType(Enum):
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"
    OTHER_MESSAGE = "OtherMessage"


class AstrBotMessageLike:
    """照抄 astrbot.core.platform.astrbot_message.AstrBotMessage 的形状。"""

    def __init__(self, chain, group_id="", raw=None, sender_id="7"):
        self.type = MessageType.GROUP_MESSAGE if group_id else MessageType.FRIEND_MESSAGE
        self.self_id = "1"
        self.session_id = group_id or sender_id
        self.message_id = "m1"
        self.group = (
            type("Group", (), {"group_id": group_id, "group_name": "真实群"})()
            if group_id
            else None
        )
        self.sender = type("Member", (), {"user_id": sender_id, "nickname": "甲"})()
        self.message = chain
        self.message_str = ""
        self.raw_message = raw if raw is not None else {}
        self.timestamp = int(BASE_TIME.timestamp())

    @property
    def group_id(self):
        return self.group.group_id if self.group else ""


class AstrEventLike:
    """照抄 AstrMessageEvent 的形状：取数据的都是方法，不是属性。"""

    def __init__(self, chain=None, group_id="", raw=None, role="member", sender_id="7"):
        self.message_obj = AstrBotMessageLike(chain or [], group_id, raw, sender_id)
        self.message_str = ""
        self.role = role

    def get_group_id(self):
        return self.message_obj.group_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname

    def get_self_id(self):
        return self.message_obj.self_id

    def get_message_type(self):
        return self.message_obj.type

    def get_message_str(self):
        return self.message_str

    def is_private_chat(self):
        return self.message_obj.type is MessageType.FRIEND_MESSAGE

    def is_admin(self):
        return self.role == "admin"

    def plain_result(self, text):
        return text


class OneBotClientLike:
    """照抄 aiocqhttp.CQHttp：`__getattr__` 把任意名字变成一次 call_action。"""

    def __init__(self, history, per_call=20):
        self.history = history
        self.per_call = per_call
        self.calls = []
        self.requested_names = []

    def __getattr__(self, item):
        self.requested_names.append(item)

        def call(*args, **kwargs):
            return self.call_action(item, **kwargs)

        return call

    async def call_action(self, action, **params):
        self.calls.append((action, dict(params)))
        if action == "get_group_msg_history":
            seq = params.get("message_seq")
            if seq is None:
                end = len(self.history)
            else:
                end = next(
                    (i for i, m in enumerate(self.history) if m["message_id"] == seq),
                    len(self.history),
                )
            start = max(0, end - self.per_call)
            return {"messages": self.history[start:end]}
        if action == "get_group_info":
            return {"group_id": GROUP, "group_name": "真实群", "member_count": 3}
        if action == "get_group_member_list":
            return [{"user_id": i, "nickname": f"成员{i}", "role": "member"} for i in range(3)]
        if action == "_get_group_notice":
            return [{"content": "重要公告", "publish_time": int(BASE_TIME.timestamp())}]
        if action == "get_group_root_files":
            return {"files": [{"file_id": "f1", "file_name": "资料.zip", "file_size": 9}]}
        return None


class PlatformLike:
    """只有 get_client()，别的一概没有——和真实 aiocqhttp 适配器一样。"""

    def __init__(self, client):
        self._client = client

    def get_client(self):
        return self._client


# ---------------------------------------------------------------- 解析器
def test_component_enum_type_is_recognized():
    """`ComponentType` 是 str-Enum：`str()` 给出 `ComponentType.Plain`，不能直接查表。

    Python 3.11 上 `str(ComponentType.Plain)` 是 `"ComponentType.Plain"`（3.12 才
    改成返回 value）。原来的 `str(type).lower()` 得到 `componenttype.plain`，
    每种段落进 `_TYPE_KEYS` 都是 None，最后走 else 分支——图片、语音、视频、文件
    全被当成文本，`media_paths` 恒空。
    """
    chain = [Component(ComponentType.Image, file="https://x/a.png", url="https://x/a.png")]
    msg = parse_message(AstrBotMessageLike(chain), GROUP)
    assert msg.media_paths == ["https://x/a.png"]
    assert msg.message_type.value == "image"


def test_full_chain_survives_real_astrbot_components():
    """@ / 引用 / 图片 / 语音 / 文件 全部要能认出来。"""
    chain = [
        Component(ComponentType.Reply, id="m0", sender_nickname="乙", message_str="原话"),
        Component(ComponentType.At, qq="9", name="丙"),
        Component(ComponentType.Plain, text="看这个"),
        Component(ComponentType.Image, file="https://x/a.png", url="https://x/a.png"),
        Component(ComponentType.Record, file="https://x/v.amr", url="https://x/v.amr"),
        Component(ComponentType.File, name="资料.zip", file_="", url="https://x/f.zip"),
    ]
    msg = parse_message(AstrBotMessageLike(chain), GROUP)
    assert msg.reply_to == "m0"
    assert msg.media_paths == ["https://x/a.png", "https://x/v.amr", "https://x/f.zip"]
    assert "@丙" in msg.content and "看这个" in msg.content
    assert msg.message_type.value == "mixed"


def test_file_segment_property_is_not_touched():
    """AstrBot 的 `File.file` 是会触发同步下载的 property，绝不能碰。"""

    class FileLike:
        type = ComponentType.File
        name = "资料.zip"
        url = "https://x/f.zip"

        @property
        def file(self):  # pragma: no cover - 被碰到就直接失败
            raise AssertionError("File.file 被读了：会触发同步下载")

    msg = parse_message(AstrBotMessageLike([FileLike()]), GROUP)
    assert msg.media_paths == ["https://x/f.zip"]


def test_raw_content_keeps_onebot_payload():
    """raw_content 是「保底」用的：AstrBotMessage 对象不能直接 json，要落 raw_message。"""
    raw = {"post_type": "message", "message_id": "m1", "group_id": GROUP}
    msg = parse_message(AstrBotMessageLike([], raw=raw), GROUP)
    assert json.loads(msg.raw_content)["message_id"] == "m1"


# ---------------------------------------------------------------- 平台适配
@pytest.mark.asyncio
async def test_onebot_message_seq_paging_reads_everything(tmp_path):
    """OneBot 的历史消息只认 message_seq。

    原来的适配器传 `group_id/count/offset`：客户端 `__getattr__` 收下、忽略
    offset，每次返回同一批最新消息。引擎的去重兜底立刻判定「分页到头」，
    于是每个群只备份最新的一页，状态还写 `completed`。
    """
    db = Database(tmp_path / "db.sqlite")
    client = OneBotClientLike(raw_history(120), per_call=20)
    engine = BackupEngine(db, tmp_path / "media", {})
    record = await engine.backup_group(GROUP, PlatformLike(client), download_media=False)
    assert record.status.value == "completed"
    assert db.count_messages(GROUP) == 120
    assert record.message_count == 120


@pytest.mark.asyncio
async def test_fill_up_does_not_drop_the_tail(tmp_path):
    """补齐一页时不能截断。

    平台一次只给 20 条、引擎要 50 条，适配器会连要三轮拿到 60 条。如果截成 50
    条，被截掉的 10 条对应的锚点已经推过去了，之后再也翻不回来——那一段消息
    永久丢失，而备份报 completed。
    """
    db = Database(tmp_path / "db.sqlite")
    client = OneBotClientLike(raw_history(120), per_call=20)
    engine = BackupEngine(db, tmp_path / "media", {})
    await engine.backup_group(GROUP, PlatformLike(client), download_media=False)
    stored = {m.message_id for m in db.get_messages(GROUP)}
    assert stored == {f"m{i}" for i in range(120)}


@pytest.mark.asyncio
async def test_onebot_actions_reach_announcements_and_files(tmp_path):
    """公告 / 文件清单要走 OneBot action（`_get_group_notice` / `get_group_root_files`）。"""
    db = Database(tmp_path / "db.sqlite")
    client = OneBotClientLike(raw_history(2))
    engine = BackupEngine(db, tmp_path / "media", {})
    record = await engine.backup_group(GROUP, PlatformLike(client), download_media=False)
    assert record.status.value == "completed", record.error
    bundle = db.get_bundle(record.backup_id, GROUP)
    assert bundle["announcements"][0]["content"] == "重要公告"
    assert bundle["group_files"][0]["name"] == "资料.zip"


@pytest.mark.asyncio
async def test_announcement_with_publish_time_does_not_fail_the_backup(tmp_path):
    """带发布时间的公告不能让整轮备份判 failed。

    `published_at` 是 datetime，`json.dumps` 直接抛
    `TypeError: Object of type datetime is not JSON serializable`。而
    `save_bundle` 在主流程里——消息全落库了，备份却写 failed。
    """
    db = Database(tmp_path / "db.sqlite")
    engine = BackupEngine(db, tmp_path / "media", {})
    record = await engine.backup_group(
        GROUP, PlatformLike(OneBotClientLike(raw_history(3))), download_media=False
    )
    assert record.status.value == "completed", record.error
    assert db.get_bundle(record.backup_id, GROUP)


@pytest.mark.asyncio
async def test_adapter_does_not_pollute_client_with_fake_attributes(tmp_path):
    """探测客户端时不能把 `__getattr__` 造出来的假属性当成真方法。"""
    db = Database(tmp_path / "db.sqlite")
    client = OneBotClientLike(raw_history(3))
    engine = BackupEngine(db, tmp_path / "media", {})
    await engine.backup_group(GROUP, PlatformLike(client), download_media=False)
    # `bot` / `api` / `client` 这些探测名不该变成一次 OneBot 调用
    assert "bot" not in client.requested_names
    assert "api" not in client.requested_names


# ---------------------------------------------------------------- 通知钩子
def test_notice_payload_reads_message_obj_raw_message():
    """OneBot notice 的原始事件在 `message_obj.raw_message` 里。

    原来的写法只读 `event.post_type` / `event.message_obj.post_type`，两个位置
    都是 None，于是每条通知都摊成 `{'group_id': '100'}`，`sub_type` 恒空——
    退群聚合、群解散、机器人被踢一次都没触发过。
    """
    raw = {
        "post_type": "notice",
        "notice_type": "group_decrease",
        "sub_type": "leave",
        "group_id": GROUP,
        "user_id": 7,
    }
    payload = notice_payload(AstrEventLike(raw=raw, group_id=str(GROUP)))
    assert payload["notice_type"] == "group_decrease"
    assert payload["sub_type"] == "leave"


def test_mass_leave_triggers_backup_on_real_events():
    hooks = EventHooks(None, AutoTrigger({"mass_leave_threshold": 3}), on_leave=True)
    raw = {
        "post_type": "notice",
        "notice_type": "group_decrease",
        "sub_type": "leave",
        "group_id": GROUP,
        "user_id": 7,
    }
    event = AstrEventLike(raw=raw, group_id=str(GROUP))
    results = [hooks.handle_notice(notice_payload(event), event) for _ in range(3)]
    assert [r[2] for r in results] == [False, False, True]


def test_disband_and_kick_trigger_backup():
    hooks = EventHooks(None, AutoTrigger({}), on_leave=True, on_disband=True)
    disband = {"post_type": "notice", "notice_type": "group_disband", "group_id": GROUP}
    assert hooks.handle_notice(disband)[2] is True
    kick = {
        "post_type": "notice",
        "notice_type": "group_decrease",
        "sub_type": "kick_me",
        "group_id": GROUP,
    }
    assert hooks.handle_notice(kick)[2] is True


def test_ordinary_chat_is_not_a_leave():
    """普通聊天不能被当成退群：post_type=message 的通知一律不理。"""
    hooks = EventHooks(None, AutoTrigger({"mass_leave_threshold": 1}), on_leave=True)
    payload = {"post_type": "message", "message_type": "group", "group_id": GROUP}
    assert hooks.handle_notice(payload)[2] is False


# ---------------------------------------------------------------- 权限 / 群号
def test_private_chat_allowed_uses_methods():
    """`is_private_chat()` 是方法，按布尔属性读会一直判「不是私聊」。"""
    event = AstrEventLike(group_id="")
    assert PermissionChecker(require_admin=True, allow_private=True).check_event(event).allowed
    denied = PermissionChecker(require_admin=True, allow_private=False).check_event(event)
    assert not denied.allowed
    assert "私聊" in denied.reason


def test_admin_check_uses_methods():
    """`is_admin()` 是方法：管理员发命令不能被挡回去。"""
    event = AstrEventLike(group_id=str(GROUP), role="admin")
    assert PermissionChecker(require_admin=True).check_event(event).allowed
    member = AstrEventLike(group_id=str(GROUP), role="member")
    assert not PermissionChecker(require_admin=True).check_event(member).allowed


def test_group_id_comes_from_get_group_id():
    """AstrBot 事件没有 `group_id` 属性，只有 `get_group_id()`。"""
    event = AstrEventLike(group_id=str(GROUP))
    assert extract_group_id(event) == GROUP
    assert extract_group_id(AstrEventLike(group_id="")) is None


# ---------------------------------------------------------------- 记录状态
@pytest.mark.asyncio
async def test_partial_backup_still_counts_as_latest(tmp_path):
    """媒体下载失败降级为 partial，但它仍然是「最近一次备份」。

    只认 completed 的话，一次媒体失败就会让 `/backup status` 报「还没有完成过
    备份」、`export latest` 导不出东西——而消息明明全在库里。
    """
    from backup.core.models import BackupStatus

    db = Database(tmp_path / "db.sqlite")
    engine = BackupEngine(db, tmp_path / "media", {})
    record = await engine.backup_group(
        GROUP, FakePlatform(history=raw_history(3)), download_media=False
    )
    record.status = BackupStatus.PARTIAL
    record.error = "1 个媒体文件下载失败"
    db.save_record(record)
    latest = db.latest_completed_record(GROUP)
    assert latest is not None and latest.backup_id == record.backup_id


# ---------------------------------------------------------------- 导出 / 下载
@pytest.mark.asyncio
async def test_directory_export_is_downloadable(tmp_path):
    """json / markdown 导出的是目录，链接必须真能下东西。

    直接把目录交给下载路由：`Content-Type` 退成 octet-stream，读流时
    `open(dir)` 抛 `IsADirectoryError`——用户看到的是一条下不动的链接。
    """
    from backup.core.download_route import handle_download

    db = Database(tmp_path / "db.sqlite")
    for i in range(3):
        db.save_message(make_message(i), "bk1")
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    for fmt in ("json", "markdown"):
        result = exports.export(GROUP, fmt, backup_id="bk1")
        response = handle_download(result.download_path, exports)
        assert response.status == 200
        assert b"".join(response.iter_chunks())


@pytest.mark.asyncio
async def test_verify_accepts_its_own_export(tmp_path):
    """`/backup verify <包>` 不能拿记录的校验和当期望值。

    记录的 checksum 覆盖「这次备份覆盖的消息」，而 `export latest` 的包还含只在
    实时归档里的消息，两者天然不等。拿它比，自己刚导出的包会被判「被修改」。
    """
    db = Database(tmp_path / "db.sqlite")
    engine = BackupEngine(db, tmp_path / "media", {})
    record = await engine.backup_group(
        GROUP, FakePlatform(history=raw_history(3)), download_media=False
    )
    extra = make_message(9)
    extra.message_id = "realtime1"
    db.save_message(extra, "realtime")
    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    package = exports.export_latest(GROUP, "zip")
    # 记录校验和 ≠ 包内校验和（包多了实时归档那条）
    assert verify_zip(package.path)[0]
    assert record.checksum


# ---------------------------------------------------------------- 定时
def test_schedule_restore_skips_disabled_rows(tmp_path):
    """`schedule.enabled=false` 的那一行不该在重启后被挂回调度器。"""
    db = Database(tmp_path / "db.sqlite")
    calls = []

    async def run(group_id):
        calls.append(group_id)

    db.save_schedule(GROUP, "0 3 * * *", enabled=False)
    scheduler = BackupScheduler(db, run, enabled=True)
    assert scheduler.restore_jobs() == 0


# ---------------------------------------------------------------- 往返一致性
@pytest.mark.parametrize(
    "mtype,content,media,reply,is_forward",
    [
        ("image", "", ["https://x/a.png"], None, False),
        ("mixed", "@丙看这个", ["https://x/a.png", "https://x/b.amr"], "m0", False),
        ("voice", "", ["https://x/v.amr"], None, False),
        ("file", "[文件] 资料.zip", ["https://x/f.zip"], None, False),
        ("text", "你好", [], "m0", False),
        ("forward", "", [], None, True),
    ],
)
def test_flat_message_round_trips(mtype, content, media, reply, is_forward):
    """导出器摊平的那条 dict 要能原样读回来。

    原来 `content` 为空就把整条消息判成 `unknown`、媒体/引用/转发链全丢——
    纯图片、纯语音、纯文件消息**自己导出的包自己恢复不了**，报的还是
    「备份可能已损坏」。另外平铺 dict 会被当成一个消息段遍历，`content` 被
    当正文捡走，平铺分支永远进不去。
    """
    flat = {
        "message_id": "x1",
        "group_id": GROUP,
        "sender_id": 7,
        "sender_name": "甲",
        "timestamp": "2024-05-01T17:50:00",
        "message_type": mtype,
        "content": content,
        "reply_to": reply,
        "media_paths": media,
        "is_forward": is_forward,
        "forward_chain": [{"sender_name": "乙", "content": "原话", "timestamp": ""}]
        if is_forward
        else [],
    }
    msg = parse_message(flat, GROUP)
    assert msg.message_type.value == mtype
    assert msg.content == content
    assert msg.media_paths == media
    assert msg.reply_to == reply
    assert msg.is_forward is is_forward


@pytest.mark.asyncio
async def test_export_package_can_be_restored_end_to_end(tmp_path):
    """自己导出的包必须能自己恢复，且校验和一致。

    端到端脚本里这一步撞上的正是「纯图片/混合消息从包里读回来变了样」，
    校验和对不上，恢复被判「备份可能已损坏」。
    """
    from backup.core.export_manager import ExportManager
    from backup.core.models import BackupStatus
    from backup.core.restore_engine import RestoreEngine

    db = Database(tmp_path / "db.sqlite")
    engine = BackupEngine(db, tmp_path / "media", {})
    record = await engine.backup_group(
        GROUP, FakePlatform(history=raw_history(6)), download_media=False
    )
    record.status = BackupStatus.PARTIAL
    db.save_record(record)

    exports = ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {}})
    package = exports.export_latest(GROUP, "zip")
    assert verify_zip(package.path)[0]

    restored = await RestoreEngine(db).restore(
        record.backup_id, 200, archive=package.path, confirm=True
    )
    assert restored.inserted == package.message_count
    assert restored.total == package.message_count
