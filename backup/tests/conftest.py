import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backup.core.database import Database  # noqa: E402
from backup.core.models import GroupInfo, GroupMember, Message, MessageType  # noqa: E402

BASE_TIME = datetime(2024, 5, 1, 10, 0, 0)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "db" / "backup.sqlite")
    yield database
    database.close()


def make_message(i: int, group_id: int = 100, sender: int = 1, content: str = None, mtype=None) -> Message:
    return Message(
        message_id=f"m{i}",
        group_id=group_id,
        sender_id=sender,
        sender_name=f"用户{sender}",
        timestamp=BASE_TIME + timedelta(minutes=i),
        message_type=mtype or MessageType.TEXT,
        content=content if content is not None else f"消息内容 {i}",
        raw_content="{}",
    )


@pytest.fixture
def messages():
    return [make_message(i) for i in range(5)]


class FakePlatform:
    """假平台：可配置历史消息和失败行为。"""

    def __init__(self, history=None, members=None, info=None, page_size=50):
        self.history = history or []
        self.members = members or []
        self.info = info or {"group_id": 100, "group_name": "测试群"}
        self.page_size = page_size
        self.history_calls = 0

    async def get_group_info(self, group_id):
        return self.info

    async def get_group_members(self, group_id):
        return self.members

    async def get_group_history(self, group_id, count=50, offset=0):
        self.history_calls += 1
        return self.history[offset : offset + count]


def raw_history(n: int, group_id: int = 100) -> list[dict]:
    """构造平台原始消息 dict。"""
    out = []
    for i in range(n):
        out.append(
            {
                "message_id": f"m{i}",
                "group_id": group_id,
                "sender": {"user_id": 1 + i % 3, "nickname": f"用户{i % 3}"},
                "timestamp": (BASE_TIME + timedelta(minutes=i)).timestamp(),
                "message": [{"type": "text", "data": {"text": f"消息 {i}"}}],
            }
        )
    return out


# ---- AstrBot 替身：让 main.py 在容器里也能 import（没有真 AstrBot）----


def install_fake_astrbot(monkeypatch):
    class AstrMessageEvent:  # noqa: N801
        pass

    class Context:
        def __init__(self):
            self.registered = []

        def register_web_api(self, route, view, methods, desc):  # noqa: ARG002
            self.registered.append(route)

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_a, **_k):
        def deco(cls):
            return cls

        return deco

    class Filter:
        class EventMessageType:
            ALL = "ALL"

        @staticmethod
        def command(_name):
            return lambda fn: fn

        @staticmethod
        def event_message_type(_kind):
            return lambda fn: fn

    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.filter = Filter
    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = Context
    api_star.Star = Star
    api_star.register = register
    pkg = types.ModuleType("astrbot")
    pkg.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.event = api_event
    api.star = api_star
    for name, mod in (
        ("astrbot", pkg),
        ("astrbot.api", api),
        ("astrbot.api.event", api_event),
        ("astrbot.api.star", api_star),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    return Context
