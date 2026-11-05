"""第 25 轮：拿 AstrBot 4.14.6 里 `platform_manager` / `components` / `srv_plug_route`
的真实形状去照，四处都对不上。

前 24 轮我一直在核 `message_obj` 的字段名和 `sub_type` 的取值（这两处这次核完是
对的），却没核「谁把事件送进钩子」「平台实例从哪来」「消息段的地址放在哪一层」。
这四处都不报错，只是悄悄不对：

1. `PlatformManager.platform_insts` 的第一位永远是 **webchat**，没给 key 时
   `_platform_by` 直接取 `insts[0]` —— 定时备份拉的是 webchat，一条数据也拉不到；
2. `Image.url` 才是图片地址，段上要是带了个 `data`，地址只在段上，`media_paths`
   就是空的（纯图消息还会因此整条解析不出内容）；
3. OneBot 的 notice 被适配器包成「普通消息事件」投进管道，`on_message` 收到的
   是一条 `OTHER_MESSAGE`——它不该进消息库；
4. 下载路由登记顺序：宿主按 `route == f"/{subpath}"` 逐个比，带 `<token>` 的
   永远比不中，能命中的是 `"/backup/download"` 那条。
"""

from __future__ import annotations

import types
from datetime import datetime

import pytest

from backup.core.auto_trigger import AutoTrigger
from backup.core.event_hooks import EventHooks
from backup.core.parser import is_event_payload, parse_message
from backup.tests.conftest import install_fake_astrbot

GROUP = 100


# ------------------------------------------------------- 2. 段上的 url 才是地址


class _Component:
    """AstrBot 4.14 的 `Image` / `Record` / `File` 的最小形状。"""

    def __init__(self, type_, **kwargs):
        self.type = type_
        for key, value in kwargs.items():
            setattr(self, key, value)


def _parse(segments):
    return parse_message(
        {
            "message_id": "m1",
            "group_id": GROUP,
            "sender": {"user_id": 1},
            "timestamp": datetime(2024, 5, 1, 10, 0, 0).timestamp(),
            "message": segments,
        },
        GROUP,
    )


def test_image_url_on_component_is_not_lost_when_component_carries_data():
    """`Image.url` 在段上，`data` 里只有文件名——地址必须从段上取。"""
    image = _Component("Image", file="a.jpg", url="https://gchat.qpic.cn/x.jpg", data={"file": "a.jpg"})
    msg = _parse([image])
    assert msg.media_paths == ["https://gchat.qpic.cn/x.jpg"]


def test_image_without_data_attribute_still_works():
    plain_image = _Component("Image", file="", url="https://gchat.qpic.cn/b.jpg", path="")
    assert _parse([plain_image]).media_paths == ["https://gchat.qpic.cn/b.jpg"]


def test_onebot_raw_segment_keeps_working():
    """OneBot 原始 dict 的地址在 `data` 里，这条路不能因为上面的改动断掉。"""
    raw = {"type": "image", "data": {"file": "b.jpg", "url": "https://gchat.qpic.cn/raw.jpg"}}
    assert _parse([raw]).media_paths == ["https://gchat.qpic.cn/raw.jpg"]


def test_voice_url_on_component_is_kept():
    record = _Component("Record", file="a.amr", url="https://gchat.qpic.cn/a.amr", data={})
    assert _parse([record]).media_paths == ["https://gchat.qpic.cn/a.amr"]


# ------------------------------------------- 3. 通知事件不该进消息库


def test_notice_payload_is_recognised_as_event():
    raw = {"post_type": "notice", "notice_type": "group_decrease", "group_id": GROUP}
    assert is_event_payload({"raw_message": raw}) is True
    assert is_event_payload(raw) is True


def test_chat_message_is_not_an_event_payload():
    raw = {"post_type": "message", "message_type": "group", "group_id": GROUP}
    assert is_event_payload({"raw_message": raw}) is False
    assert is_event_payload({}) is False
    assert is_event_payload(None) is False


def _notice_event(notice_type, sub_type=None, user_id="7"):
    """aiocqhttp `_convert_handle_notice_event` 造出来的东西的等价物。"""
    raw = {
        "post_type": "notice",
        "notice_type": notice_type,
        "group_id": GROUP,
        "user_id": user_id,
        "self_id": "999",
    }
    if sub_type:
        raw["sub_type"] = sub_type
    obj = types.SimpleNamespace(
        type=types.SimpleNamespace(value="GroupMessage"),  # MessageType.GROUP_MESSAGE
        message_id="9c1f0e",
        group=types.SimpleNamespace(group_id=str(GROUP)),
        sender=types.SimpleNamespace(user_id=str(user_id), nickname=str(user_id)),
        message=[],
        message_str="",
        raw_message=raw,
        timestamp=int(datetime.now().timestamp()),
    )
    return types.SimpleNamespace(message_obj=obj, message_str="", group_id=str(GROUP))


@pytest.mark.parametrize(
    "notice_type,sub_type",
    [
        ("group_decrease", "leave"),
        ("group_decrease", "kick_me"),
        ("group_upload", None),
        ("group_ban", "ban"),
        ("group_recall", None),
    ],
)
def test_notice_never_lands_in_message_table(db, notice_type, sub_type):
    """通知在 `on_message` 里也是一条 `OTHER_MESSAGE` 事件，但不该被归档成聊天记录。"""
    hooks = EventHooks(db, AutoTrigger({"mass_leave_threshold": 3}))
    assert hooks.archive_message(_notice_event(notice_type, sub_type)) is None
    assert db.query("SELECT message_id FROM messages") == []


def test_real_message_still_archived(db):
    """拦通知不能顺手把聊天消息也拦掉。"""
    hooks = EventHooks(db, AutoTrigger({}))
    obj = types.SimpleNamespace(
        type=types.SimpleNamespace(value="GroupMessage"),
        message_id="real-1",
        group=types.SimpleNamespace(group_id=str(GROUP)),
        sender=types.SimpleNamespace(user_id="7", nickname="甲"),
        message=[_Component("Plain", text="晚上开会")],
        message_str="晚上开会",
        raw_message={"post_type": "message", "message_type": "group", "group_id": GROUP},
        timestamp=int(datetime(2024, 5, 1, 10, 0, 0).timestamp()),
    )
    event = types.SimpleNamespace(message_obj=obj, message_str="晚上开会", group_id=str(GROUP))
    msg = hooks.archive_message(event)
    assert msg is not None and msg.content == "晚上开会"
    assert db.message_exists("real-1", GROUP)


# --------------------------------- 1. 定时备份挑平台：别挑 webchat


class _Meta:
    def __init__(self, name, id_):
        self.name, self.id = name, id_


class _StubPlatform:
    def __init__(self, name, id_, client=None):
        self._meta = _Meta(name, id_)
        self.client = client

    def meta(self):
        return self._meta

    def get_client(self):
        return self.client


def _build_plugin(monkeypatch, tmp_path, platforms):
    install_fake_astrbot(monkeypatch)  # 让 main.py 在没有 AstrBot 的机器上也能 import
    from backup.main import BackupPlugin

    context = types.SimpleNamespace(
        registered=[],
        register_web_api=lambda *a, **k: None,
        platform_manager=types.SimpleNamespace(platform_insts=platforms),
    )
    return BackupPlugin(
        context=context,
        config={
            "storage": {"storage_dir": str(tmp_path / "storage")},
            "retention": {"auto_clean": False},
            "schedule": {"enabled": False},
        },
    )


def test_scheduled_backup_skips_webchat(monkeypatch, tmp_path):
    """`platform_insts[0]` 是 webchat，定时备份不能拿它去拉历史消息。"""
    webchat = _StubPlatform("webchat", "webchat")
    qq = _StubPlatform("aiocqhttp", "qq-main", client=object())
    plugin = _build_plugin(monkeypatch, tmp_path, [webchat, qq])
    assert plugin._resolve_platform(None).meta().name == "aiocqhttp"


def test_platform_without_client_falls_back_to_first(monkeypatch, tmp_path):
    """纯 webchat 部署：没有客户端可选，退回第一个，不抛异常。"""
    webchat = _StubPlatform("webchat", "webchat")
    plugin = _build_plugin(monkeypatch, tmp_path, [webchat])
    assert plugin._resolve_platform(None).meta().name == "webchat"


def test_download_routes_register_fixed_prefix_first(monkeypatch, tmp_path):
    """带 `<token>` 的 route 在宿主侧永远比不中，固定前缀那条必须排第一。"""
    plugin = _build_plugin(monkeypatch, tmp_path, [])
    assert plugin.DOWNLOAD_ROUTES[0] == "/backup/download"
