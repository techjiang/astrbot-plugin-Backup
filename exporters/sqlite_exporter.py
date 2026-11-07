"""SQLite 导出：完整结构化数据，可直接二次查询。"""

import sqlite3
from pathlib import Path
from typing import Optional

from .base import BaseExporter, ExportContext


class SqliteExporter(BaseExporter):
    extension = ".sqlite"
    format_name = "sqlite"

    def export(self, ctx: ExportContext, output_path: str | Path) -> Path:
        path = self.ensure_path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(
                """
                CREATE TABLE messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  message_id TEXT NOT NULL,
                  group_id INTEGER NOT NULL,
                  sender_id INTEGER NOT NULL,
                  sender_name TEXT,
                  timestamp TEXT NOT NULL,
                  message_type TEXT NOT NULL,
                  content TEXT,
                  reply_to TEXT,
                  is_forward INTEGER DEFAULT 0,
                  media_paths TEXT,
                  backup_id TEXT
                );
                CREATE INDEX idx_msg_group ON messages(group_id, timestamp);
                CREATE INDEX idx_msg_sender ON messages(sender_id);
                CREATE TABLE group_info (
                  group_id INTEGER PRIMARY KEY, group_name TEXT, owner_id INTEGER,
                  member_count INTEGER, max_members INTEGER, group_level INTEGER
                );
                CREATE TABLE group_members (
                  group_id INTEGER, user_id INTEGER, nickname TEXT, card TEXT,
                  role TEXT, join_time TEXT, PRIMARY KEY(group_id, user_id)
                );
                """
            )
            import json

            conn.executemany(
                """INSERT INTO messages
                   (message_id, group_id, sender_id, sender_name, timestamp, message_type,
                    content, reply_to, is_forward, media_paths, backup_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        m.message_id,
                        m.group_id,
                        m.sender_id,
                        m.sender_name,
                        m.timestamp.isoformat() if m.timestamp else "",
                        m.message_type.value,
                        m.content,
                        m.reply_to,
                        1 if m.is_forward else 0,
                        json.dumps(m.media_paths, ensure_ascii=False),
                        ctx.backup_id,
                    )
                    for m in ctx.messages
                ],
            )
            if ctx.group_info:
                gi = ctx.group_info
                conn.execute(
                    "INSERT OR REPLACE INTO group_info VALUES (?,?,?,?,?,?)",
                    (gi.group_id, gi.group_name, gi.owner_id, gi.member_count,
                     gi.max_members, gi.group_level),
                )
            if ctx.members:
                conn.executemany(
                    "INSERT OR REPLACE INTO group_members VALUES (?,?,?,?,?,?)",
                    [
                        (mb.group_id, mb.user_id, mb.nickname, mb.card, mb.role,
                         mb.join_time.isoformat() if mb.join_time else None)
                        for mb in ctx.members
                    ],
                )
            conn.commit()
        finally:
            conn.close()
        return path
