"""增量备份跟踪器。

不用 message_id 做增量：不同平台 ID 格式不同且可能回退。
用 (timestamp, message_id) 复合排序，持久化在 backup_meta 表。
"""

from datetime import datetime
from typing import Optional

from .database import Database, from_db, to_db


class IncrementalTracker:
    def __init__(self, db: Database):
        self.db = db

    def get_incremental(self, group_id: int) -> Optional[tuple[datetime, str]]:
        """返回上次备份的 (timestamp, message_id)；从未备份过返回 None。"""
        row = self.db.query_one(
            "SELECT last_timestamp, last_message_id FROM backup_meta WHERE group_id = ?",
            (group_id,),
        )
        if not row:
            return None
        ts = from_db(row["last_timestamp"])
        if ts is None:
            return None
        return ts, row["last_message_id"] or ""

    def get_incremental_point(self, group_id: int) -> tuple[datetime, str]:
        """兼容旧接口，未备份过时返回 (datetime.min, "")。"""
        return self.get_incremental(group_id) or (datetime.min, "")

    def is_newer(self, group_id: int, msg: tuple[datetime, str]) -> bool:
        """判断消息是否晚于增量点。"""
        point = self.get_incremental(group_id)
        if point is None:
            return True
        return tuple(msg) > tuple(point)

    def update_incremental_point(
        self, group_id: int, timestamp: datetime, message_id: str
    ) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO backup_meta (group_id, last_timestamp, last_message_id)
               VALUES (?,?,?)""",
            (group_id, to_db(timestamp), message_id),
        )

    def advance(self, group_id: int, messages: list) -> None:
        """用一批消息里最新的一条推进增量点。messages 需含 timestamp/message_id。"""
        latest = None
        for m in messages:
            key = (m.timestamp, str(m.message_id))
            if latest is None:
                latest = (key, m.timestamp, str(m.message_id))
            else:
                # 复合比较：timestamp 优先，相同则比 message_id
                if m.timestamp > latest[1] or (
                    m.timestamp == latest[1] and str(m.message_id) > latest[2]
                ):
                    latest = (key, m.timestamp, str(m.message_id))
        if latest:
            self.update_incremental_point(group_id, latest[1], latest[2])

    def reset(self, group_id: int) -> None:
        self.db.execute("DELETE FROM backup_meta WHERE group_id = ?", (group_id,))
