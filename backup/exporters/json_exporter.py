"""JSON 导出：按日期分片，适合程序处理。目录输出。"""

import json
from pathlib import Path

from .base import BaseExporter, ExportContext


class JsonExporter(BaseExporter):
    extension = ".json"
    format_name = "json"

    def export(self, ctx: ExportContext, output_path: str | Path) -> Path:
        """output_path 是目录：写入 index.json + 每天的 daily/*.json。"""
        out_dir = self.ensure_path(output_path)
        daily_dir = out_dir / "daily"
        daily_dir.mkdir(parents=True, exist_ok=True)

        shards = []
        for day, messages in self.group_by_date(ctx.messages):
            shard_path = daily_dir / f"{day}.json"
            shard_path.write_text(
                self.dumps([self.message_to_dict(m) for m in messages]), encoding="utf-8"
            )
            shards.append({"date": day, "file": f"daily/{day}.json", "count": len(messages)})

        index = {
            "group": {
                "group_id": ctx.group_id,
                "group_name": ctx.group_name,
                "member_count": len(ctx.members),
            },
            "backup_id": ctx.backup_id,
            "total_messages": len(ctx.messages),
            "participants": [
                {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in p.items()}
                for p in self.participants(ctx.messages)
            ],
            "shards": shards,
        }
        index_path = out_dir / "index.json"
        index_path.write_text(self.dumps(index), encoding="utf-8")
        return out_dir
