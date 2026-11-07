"""JSON 导出：按日期分片，适合程序处理。目录输出。"""

from pathlib import Path

from .base import BaseExporter, ExportContext, failed_url, media_href


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
                self.dumps([self._message_record(m, ctx) for m in messages]),
                encoding="utf-8",
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

    def _message_record(self, m, ctx: ExportContext) -> dict:
        """一条消息的 JSON。

        `media_paths` 在 ZIP 包里换成**包内地址**。这份 JSON 是「适合程序处理」
        的那一份，它的读者不会知道要先自己拼一层 `media/`——只留原值（导出机的
        绝对路径），任何照着它去读文件的程序都会 ENOENT，而文件就在同一个包里。

        包里没有那份媒体时（下载失败、导出时本机就没有）保留原值：那时唯一
        还能用的线索是原始 URL，把它换成一条指向不存在文件的相对地址只会更糟。
        """
        record = self.message_to_dict(m)
        links = [media_href(x, ctx) for x in m.media_paths]
        if any(links):
            record["media_paths"] = [
                link or original for link, original in zip(links, m.media_paths)
            ]
        failed = {x: failed_url(x) for x in m.media_paths if failed_url(x)}
        if failed:
            # 下载失败的原值就是 `[下载失败] <url>`，看不出哪一段是 URL。单独列一份
            # 免得读的人自己去猜那个前缀
            record["media_failed_urls"] = failed
        return record
