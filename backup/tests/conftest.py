import sys
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
