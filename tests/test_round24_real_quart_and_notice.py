"""第 24 轮：把面板下载链路放进真的 Quart 里跑了一遍，四处都塌了。

前几轮的下载用例全是拿假的 Request 对象喂 `make_view`，断言的是我们自己造出来的
形状——它们互相证明，谁也碰不到真实世界。这一轮装上真 Quart（AstrBot 面板用的就是
它）、照 `dashboard/server.py` 的 `srv_plug_route` 写转发，一次请求就撞出三个 500 / 404。

1. `/api/plug/backup/download/<token>` 一律 404——宿主前缀削错了长度；
2. 同一个路径在没装 Flask 的进程里直接抛 `RuntimeError`，500 错误页；
3. 群主/管理员身份仍然解析不出来：发送者在 `message_obj.sender` 上，而两级探测
   用的是裸 `getattr`；`sender.role` 是 dict 时也读不到；
4. 同一条通知被 `on_message` 和 `on_notice` 各数一次退群，阈值提前一半就到。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from backup.core.auto_trigger import AutoTrigger
from backup.core.commands import PermissionChecker
from backup.core.download_route import (
    _strip_host_prefix,
    handle_download,
    make_view,
)
from backup.core.event_hooks import EventHooks
from backup.tests.conftest import install_fake_astrbot, preload_host_for_lineage, real_astrbot_available


# ---------------------------------------------------------------- 1 & 2. 下载路由


@pytest.mark.parametrize(
    "path",
    [
        "/api/plug/backup/download/tok",  # AstrBot 4.14 的真实挂载点
        "/plug/backup/download/tok",
        "/web/backup/download/tok",
        "/backup/download/tok",
    ],
)
def test_host_prefix_is_stripped_down_to_our_route(path):
    """宿主加的那层前缀长度不固定，削成固定长度必然错位。

    原来写死 `path[4:]`（只削 `/api`），`/api/plug/backup/download/<token>` 被削成
    `/plug/backup/download/<token>`——既不是 `/api/backup/download/...`，也不以
    `/backup/download/` 开头，`handle_download` 回 404。而 404 和「链接已失效」
    在用户眼里是同一句话。
    """
    assert _strip_host_prefix(path) == "/backup/download/tok"


@pytest.fixture
def main_mod(tmp_path, monkeypatch):
    preload_host_for_lineage()
    sys.modules.pop("backup.main", None)
    import backup.main as mod

    monkeypatch.setattr(mod, "PLUGIN_DIR", tmp_path / "plugin")
    (tmp_path / "plugin").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin" / "config.yaml").write_text(
        f"""backup:
  storage_dir: "{tmp_path / 'storage'}"
  scope:
    download_media: false
  schedule:
    enabled: false
""",
        encoding="utf-8",
    )
    return mod


@pytest.fixture
def plugin(main_mod):
    Context = sys.modules["astrbot.api.star"].Context
    instance = main_mod.BackupPlugin(Context())
    yield instance
    instance.db.close()


def test_real_quart_serves_the_whole_zip(plugin):
    """装上真 Quart、照 `srv_plug_route` 的写法转发，群成员点链接真的能下到东西。

    这一条能同时钉住上一轮那三个 bug：路径前缀削错（404）、请求对象在上下文外
    被读（500）、以及返回的东西 Quart 不认（500）。假 Request 的用例一个都测不到。
    """
    quart = pytest.importorskip("quart")
    app = quart.Quart(__name__)
    view = make_view(plugin.exports)

    @app.route("/api/plug/<path:subpath>", methods=["GET", "POST"])
    async def srv_plug_route(subpath):  # noqa: ARG001 - 就是照真实签名写的
        # `srv_plug_route` 里那句是 `await view_handler(*args, **kwargs)`：
        # subpath 被它自己吃掉当路由匹配用了，视图收到零个参数
        return await view()

    payload = b"PK\x03\x04" + b"x" * 2048
    target = plugin.exports.export_dir / "pkg.zip"
    target.write_bytes(payload)
    token, _ = plugin.exports._issue_token(target)

    async def run():
        client = app.test_client()
        full = await client.get(f"/api/plug/backup/download/{token}")
        return full, await full.get_data()

    response, body = asyncio.run(run())
    assert response.status_code == 200, body[:200]
    assert body == payload
    assert response.headers.get("Content-Length") == str(len(payload))


def test_real_quart_range_request_resumes(plugin):
    """浏览器下大文件先发 `Range: bytes=0-` 探测，必须回 206 而不是从头再来。"""
    quart = pytest.importorskip("quart")
    app = quart.Quart(__name__)
    view = make_view(plugin.exports)

    @app.route("/api/plug/<path:subpath>", methods=["GET", "POST"])
    async def srv_plug_route(subpath):  # noqa: ARG001
        return await view()

    payload = b"PK\x03\x04" + b"y" * 1024
    target = plugin.exports.export_dir / "ranged.zip"
    target.write_bytes(payload)
    token, _ = plugin.exports._issue_token(target)

    async def run():
        client = app.test_client()
        response = await client.get(
            f"/api/plug/backup/download/{token}", headers={"Range": "bytes=100-"}
        )
        return response, await response.get_data()

    response, body = asyncio.run(run())
    assert response.status_code == 206
    assert body == payload[100:]
    assert response.headers.get("Content-Range") == f"bytes 100-{len(payload) - 1}/{len(payload)}"


def test_missing_token_is_410_not_500(plugin):
    """没有 token 时给一句人话（410），别给 500 错误页。"""
    quart = pytest.importorskip("quart")
    app = quart.Quart(__name__)
    view = make_view(plugin.exports)

    @app.route("/api/plug/<path:subpath>", methods=["GET", "POST"])
    async def srv_plug_route(subpath):  # noqa: ARG001
        return await view()

    async def run():
        client = app.test_client()
        return await client.get("/api/plug/backup/download/nope")

    assert asyncio.run(run()).status_code == 410


def test_request_object_outside_context_does_not_explode(plugin, monkeypatch):
    """框架装了、但当前没有请求上下文时读请求对象会抛——视图不能因此 500。"""
    from backup.core import download_route

    def boom(*_a, **_k):
        raise RuntimeError("Working outside of request context.")

    monkeypatch.setattr(download_route, "_current_request", boom)
    view = make_view(plugin.exports, stream=True)
    response = asyncio.run(view())
    assert int(str(getattr(response, "status", response)).split(" ", 1)[0]) == 404


def test_quart_is_preferred_over_flask_for_the_request_object(monkeypatch):
    """面板跑在 Quart 上，而 astrbot 为了序列化也 import 了 flask。

    先试 Flask 会拿到一个「当前没有活动请求」的代理，一读 `.path` 就
    `RuntimeError: Working outside of request context`——视图 500。
    """
    pytest.importorskip("quart")
    pytest.importorskip("flask")
    from backup.core import download_route

    class _Req:
        path = "/api/plug/backup/download/tok"
        headers = {}
        args = {}

    import quart

    monkeypatch.setattr(quart, "has_request_context", lambda: True, raising=False)
    sentinel = _Req()
    monkeypatch.setattr(quart, "request", sentinel, raising=False)
    # 拿到的是 Quart 的那一个，不是 Flask 那个「没有活动请求」的代理
    assert download_route._current_request() is sentinel


# ---------------------------------------------------------------- 3. 群管身份


class _Group:
    def __init__(self, group_id="100", owner=None, admins=None):
        self.group_id = group_id
        self.group_owner = owner
        self.group_admins = admins or []


class _Member:
    def __init__(self, user_id="7", role=None):
        self.user_id = user_id
        self.nickname = "张三"
        if role is not None:
            self.role = role


class _AstrBotMessage:
    def __init__(self, group, sender):
        self.group = group
        self.sender = sender
        self.self_id = "10000"
        self.message_id = "1"
        self.message = []
        self.message_str = ""
        self.raw_message = {"post_type": "message"}
        self.type = type("T", (), {"value": "GroupMessage"})()

    @property
    def group_id(self):
        return self.group.group_id if self.group else ""


class _Event:
    """`AstrMessageEvent` 只提供方法，没有 `sender` / `group_id` 属性。"""

    def __init__(self, abm, role="member"):
        self.message_obj = abm
        self.role = role
        self.message_str = abm.message_str

    def get_group_id(self):
        return self.message_obj.group_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname or ""

    def is_private_chat(self):
        return False

    def is_admin(self):
        return self.role == "admin"


def test_group_owner_is_recognised_from_message_obj():
    """群主的 QQ 在 `message_obj.group.group_owner` 上，发送者在 `message_obj.sender`。

    两级探测原来用的是裸 `getattr`：`AstrMessageEvent` 根本没有 `sender` 属性，
    拿到 None 就直接放弃——于是真正的群主发 `/backup now` 被判成没权限。
    """
    abm = _AstrBotMessage(_Group(owner="7"), _Member("7"))
    assert PermissionChecker(True).check_event(_Event(abm)).allowed


def test_group_admin_list_with_int_ids_still_matches():
    """适配器自己组装的 `group_admins` 常是 int 列表，发送者 ID 却是字符串。

    `"123" != 123` 会让管理员被判成没权限；两边都归一成字符串再比。
    """
    abm = _AstrBotMessage(_Group(admins=[123, 456]), _Member("123"))
    assert PermissionChecker(True).check_event(_Event(abm)).allowed


def test_sender_role_as_dict_is_recognised():
    """有的适配器把身份写成 `event.sender = {"role": "admin"}` 这样的 dict。"""
    class _DictSenderEvent:
        sender = {"role": "admin"}
        role = "member"
        message_obj = None

        def is_private_chat(self):
            return False

        def is_admin(self):
            return False

    assert PermissionChecker(True).check_event(_DictSenderEvent()).allowed


def test_plain_member_is_still_denied():
    """放开的是「能认出群管」，不是「谁都能备份」。"""
    abm = _AstrBotMessage(_Group(owner="999", admins=["888"]), _Member("7"))
    assert not PermissionChecker(True).check_event(_Event(abm)).allowed


# ---------------------------------------------------------------- 4. 通知重复投递


def _notice_event(group_id="100", user_id="7", message_id="9001", sub_type="leave"):
    raw = {
        "post_type": "notice",
        "notice_type": "group_decrease",
        "sub_type": sub_type,
        "group_id": int(group_id),
        "user_id": int(user_id),
    }
    abm = _AstrBotMessage(_Group(group_id), _Member(user_id))
    abm.message_id = message_id
    abm.raw_message = raw
    return _Event(abm)


def test_same_notice_is_counted_once_per_event(db):
    """同一条通知会同时进 `on_message` 和 `on_notice`（两个 handler 都挂在 ALL 上）。

    退群聚合是按「次数」累加的：数两遍，阈值 10 的群 5 个人就触发备份。
    """
    hooks = EventHooks(db, AutoTrigger({"mass_leave_threshold": 10}))
    event = _notice_event(message_id="9001")
    first = hooks.seen_notice(event)
    second = hooks.seen_notice(event)
    assert first is False
    assert second is True


def test_different_notices_are_not_deduped(db):
    """去重用的是事件指纹，不是「这个群最近处理过通知」——不同的人退群都要算。"""
    hooks = EventHooks(db, AutoTrigger({"mass_leave_threshold": 10}))
    assert hooks.seen_notice(_notice_event(message_id="9001")) is False
    assert hooks.seen_notice(_notice_event(message_id="9002")) is False


def test_mass_leave_threshold_is_not_halved(db):
    """10 个人退群才触发；被重复投递数两遍的话，5 个人就触发了。"""
    hooks = EventHooks(db, AutoTrigger({"mass_leave_threshold": 10}))
    fired = []
    for i in range(5):
        event = _notice_event(message_id=f"n{i}", user_id=str(100 + i))
        for _ in range(2):  # 两个 handler 各来一次
            if hooks.seen_notice(event):
                continue
            _, _, should, _ = hooks.handle_notice({"notice_type": "group_decrease", "sub_type": "leave", "group_id": 100}, event)
            if should:
                fired.append(i)
    assert fired == []  # 5 个人，不该触发

    event = _notice_event(message_id="n-last", user_id="999")
    hooks.seen_notice(event)
    _, _, should, _ = hooks.handle_notice(
        {"notice_type": "group_decrease", "sub_type": "leave", "group_id": 100}, event
    )
    # 退群计数用的是 10 分钟滑动窗口，而上面 5 次循环和这一句都在同一瞬间跑完——
    # 本来就是要数满 10 次才触发。这里补到第 10 次，第 10 个人才该触发
    assert should is False
    for i in range(4):
        nxt = _notice_event(message_id=f"n-last{i}", user_id=str(500 + i))
        hooks.seen_notice(nxt)
        _, _, should, _ = hooks.handle_notice(
            {"notice_type": "group_decrease", "sub_type": "leave", "group_id": 100}, nxt
        )
    assert should is True


def test_fingerprint_uses_ids_not_content(db):
    """指纹里只该有 ID / 群号 / 类型，不能把消息正文带进去（红线 1）。"""
    event = _notice_event()
    event.message_obj.message_str = "这是一句不该出现在任何地方的话"
    from backup.core.event_hooks import event_fingerprint

    assert "不该出现" not in event_fingerprint(event)
    assert "9001" in event_fingerprint(event)
