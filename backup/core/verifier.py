"""备份完整性：确定性校验和 + 备份包校验。"""

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Iterable, Optional

from .models import Message

# 校验和只覆盖消息的业务字段，忽略自增 id / backup_id，保证同一批数据稳定
_CHECKSUM_FIELDS = ("message_id", "group_id", "sender_id", "timestamp", "message_type", "content")


def compute_checksum(messages: Iterable[Message]) -> str:
    """对一组消息算 SHA-256。顺序无关：先排序再哈希。"""
    h = hashlib.sha256()
    for m in sorted(messages, key=lambda x: (x.timestamp, str(x.message_id))):
        payload = {
            "message_id": m.message_id,
            "group_id": m.group_id,
            "sender_id": m.sender_id,
            "timestamp": m.timestamp.isoformat() if m.timestamp else "",
            "message_type": m.message_type.value if hasattr(m.message_type, "value") else str(m.message_type),
            "content": m.content or "",
        }
        h.update(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(archive: str | Path) -> dict:
    """读备份包里的 manifest。读不到返回空 dict。"""
    try:
        with zipfile.ZipFile(archive) as zf:
            return json.loads(zf.read("manifest.json").decode("utf-8"))
    except (KeyError, zipfile.BadZipFile, ValueError):
        return {}


def verify_zip(path: str | Path, expected_checksum: Optional[str] = None) -> tuple[bool, str]:
    """校验 ZIP 备份包：CRC 全量扫描 + manifest 校验和比对 + 包内文件齐不齐。"""
    p = Path(path)
    if not p.is_file():
        return False, "备份文件不存在"
    if not zipfile.is_zipfile(p):
        return False, "不是合法的 ZIP 备份包"
    try:
        with zipfile.ZipFile(p) as zf:
            bad = zf.testzip()
            if bad:
                return False, f"文件损坏：{bad}"
            names = set(zf.namelist())
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except KeyError:
                manifest = {}
            except ValueError:
                return False, "manifest.json 无法解析"
            if expected_checksum:
                if not manifest:
                    return False, "缺少 manifest.json，无法校验"
                if manifest.get("checksum") != expected_checksum:
                    return False, "校验和不一致，备份可能被修改"
            # manifest 自洽不等于包完整：消息文件和媒体都可能被挑走，
            # 恢复前就该说清，而不是恢复完发现缺了一段
            if not any(n.endswith(".json") and n.startswith("json/") for n in names):
                return False, "包内没有消息文件"
            expected_media = {
                n
                for n in (manifest.get("media_files") or [])
                if isinstance(n, str) and n
            }
            absent = sorted(expected_media - names)
            if absent:
                return False, f"包内缺媒体文件 {len(absent)} 个，最早缺失：{absent[0]}"
    except zipfile.BadZipFile:
        return False, "ZIP 解析失败"
    return True, "校验通过"
