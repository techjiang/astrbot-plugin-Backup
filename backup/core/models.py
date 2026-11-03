"""数据模型。"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MessageType(Enum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    MIXED = "mixed"
    FORWARD = "forward"
    REPLY = "reply"
    AT = "at"
    UNKNOWN = "unknown"


class BackupStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class Message:
    message_id: str
    group_id: int
    sender_id: int
    sender_name: str
    timestamp: datetime
    message_type: MessageType
    content: str
    raw_content: str
    reply_to: Optional[str] = None
    media_paths: list[str] = field(default_factory=list)
    is_forward: bool = False
    forward_chain: list[dict] = field(default_factory=list)


@dataclass
class BackupRecord:
    backup_id: str
    group_id: int
    status: BackupStatus
    started_at: datetime
    finished_at: Optional[datetime] = None
    # 这份备份覆盖住多少条消息（来源清单口径），不是「这次新存了几条」
    message_count: int = 0
    # 这次真正新存进库的条数；重跑一轮全是已在库的消息时它是 0，而 message_count 不是
    new_count: int = 0
    media_count: int = 0
    total_size: int = 0
    is_incremental: bool = False
    base_backup_id: Optional[str] = None
    checksum: str = ""
    error: Optional[str] = None

    @property
    def duration(self) -> float:
        end = self.finished_at or datetime.now()
        return max((end - self.started_at).total_seconds(), 0.0)


@dataclass
class GroupMember:
    user_id: int
    group_id: int
    nickname: str = ""
    card: str = ""
    role: str = "member"
    join_time: Optional[datetime] = None
    last_active: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        return self.card or self.nickname or str(self.user_id)


@dataclass
class GroupInfo:
    group_id: int
    group_name: str = ""
    owner_id: int = 0
    created_at: Optional[datetime] = None
    member_count: int = 0
    max_members: int = 0
    group_level: int = 0
