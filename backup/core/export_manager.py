"""导出调度：统一入口 + ZIP 打包 + 带 token 的临时下载链接。

红线 3：导出文件落在 storage/exports，不放到 Web 可访问目录。
红线 4：链接必须带 token 且过期。
"""

import hashlib
import json
import secrets
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..exporters import get_exporter
from ..exporters.base import ExportContext
from ..exporters.html_exporter import HtmlExporter
from .database import Database
from .models import Message
from .verifier import compute_checksum

EXPIRY_UNITS = {"m": 60, "h": 3600, "d": 86400}


def parse_expire(value) -> int:
    """'24h' → 86400 秒。"""
    text = str(value or "24h").strip().lower()
    if text.isdigit():
        return int(text)
    unit = text[-1]
    if unit in EXPIRY_UNITS:
        try:
            return int(float(text[:-1]) * EXPIRY_UNITS[unit])
        except ValueError:
            pass
    return 24 * 3600


@dataclass
class ExportResult:
    format: str
    path: Path
    size: int
    message_count: int
    token: str
    expires_at: datetime

    @property
    def link(self) -> str:
        return f"/backup/download/{self.token}"


class ExportManager:
    def __init__(
        self,
        db: Database,
        export_dir: str | Path,
        media_dir: str | Path,
        config: Optional[dict] = None,
    ):
        self.db = db
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir = Path(media_dir)
        cfg = config or {}
        self.expire_seconds = parse_expire((cfg.get("export") or {}).get("download_link_expire", "24h"))
        self._tokens: dict[str, dict] = {}

    def build_context(self, group_id: int, backup_id: Optional[str] = None) -> ExportContext:
        messages = self.db.get_messages(group_id=group_id, backup_id=backup_id)
        return ExportContext(
            messages=messages,
            group_info=self.db.get_group_info(group_id),
            members=self.db.get_members(group_id),
            backup_id=backup_id or "",
        )

    def export(
        self,
        group_id: int,
        fmt: str,
        backup_id: Optional[str] = None,
        own_user_id: Optional[int] = None,
    ) -> ExportResult:
        fmt = (fmt or "sqlite").lower()
        ctx = self.build_context(group_id, backup_id)
        if not ctx.messages:
            raise ValueError("该群没有可导出的消息")

        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        target = self.export_dir / f"{group_id}_{stamp}_{fmt}"

        if fmt == "zip":
            path = self._export_zip(ctx, target)
        elif fmt in ("html", "htm"):
            path = HtmlExporter(own_user_id=own_user_id).export(ctx, target.with_suffix(".html"))
        else:
            exporter = get_exporter(fmt)
            if fmt in ("json", "markdown", "md"):
                path = exporter.export(ctx, target)
            else:
                path = exporter.export(ctx, target.with_suffix(exporter.extension))

        size = path.stat().st_size if path.is_file() else sum(
            f.stat().st_size for f in path.rglob("*") if f.is_file()
        )
        token, expires = self._issue_token(path)
        return ExportResult(
            format=fmt,
            path=path,
            size=size,
            message_count=len(ctx.messages),
            token=token,
            expires_at=expires,
        )

    def _export_zip(self, ctx: ExportContext, base: Path) -> Path:
        """ZIP = 六种格式 + 媒体文件 + manifest。"""
        work = base / "_work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)
        get_exporter("sqlite").export(ctx, work / "messages.sqlite")
        get_exporter("json").export(ctx, work / "json")
        get_exporter("csv").export(ctx, work / "messages.csv")
        get_exporter("txt").export(ctx, work / "messages.txt")
        get_exporter("markdown").export(ctx, work / "markdown")
        (work / "chat.html").write_text(HtmlExporter().render(ctx), encoding="utf-8")

        zip_path = base.with_suffix(".zip")
        manifest = {
            "backup_id": ctx.backup_id,
            "group_id": ctx.group_id,
            "group_name": ctx.group_name,
            "message_count": len(ctx.messages),
            "member_count": len(ctx.members),
            "exported_at": datetime.now().isoformat(),
            "checksum": compute_checksum(ctx.messages),
            "author": {"name": "科技酱", "homepage": "https://www.asoe.cn"},
        }
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in work.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(work))
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for m in ctx.messages:
                for media in m.media_paths:
                    p = Path(media)
                    if p.is_file() and self._is_inside(p, self.media_dir):
                        zf.write(p, Path("media") / p.parent.name / p.name)
        shutil.rmtree(work, ignore_errors=True)
        return zip_path

    @staticmethod
    def _is_inside(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    # ---------- 下载 token ----------
    def _issue_token(self, path: Path) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(24)
        expires = datetime.now().timestamp() + self.expire_seconds
        self._tokens[token] = {"path": str(path), "expires": expires}
        return token, datetime.fromtimestamp(expires)

    def resolve_token(self, token: str) -> Optional[Path]:
        item = self._tokens.get(token)
        if not item:
            return None
        if item["expires"] < time.time():
            self._tokens.pop(token, None)
            return None
        path = Path(item["path"])
        return path if path.exists() else None

    def clean_exports(self, max_age_days: int = 1) -> int:
        """删过期导出文件与其 token。返回删除数量。"""
        now = time.time()
        removed = 0
        for token, item in list(self._tokens.items()):
            if item["expires"] < now:
                self._tokens.pop(token, None)
        cutoff = now - max_age_days * 86400
        for f in self.export_dir.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    shutil.rmtree(f) if f.is_dir() else f.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    @staticmethod
    def file_checksum(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()
