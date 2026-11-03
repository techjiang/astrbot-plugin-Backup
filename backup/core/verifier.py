"""备份完整性：确定性校验和 + 备份包校验。"""

import hashlib
import json
from datetime import date, datetime
import zipfile
from pathlib import Path
from typing import Iterable, Optional

from .models import Message

# 校验和只覆盖消息的业务字段，忽略自增 id / backup_id，保证同一批数据稳定
_CHECKSUM_FIELDS = ("message_id", "group_id", "sender_id", "timestamp", "message_type", "content")


def _sort_key(m: Message):
    # 时间戳可能是 datetime.min（老库/解析失败），也可能同一毫秒多条，最后用
    # (group_id, message_id) 兜底，保证同一批数据无论从库里还是从包里来都一样
    return (
        m.timestamp or datetime.min,
        m.group_id or 0,
        str(m.message_id),
        m.sender_id or 0,
        m.content or "",
    )


def compute_checksum(messages: Iterable[Message]) -> str:
    """对一组消息算 SHA-256。顺序无关：先排序再哈希。"""
    h = hashlib.sha256()
    for m in sorted(messages, key=_sort_key):
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


# 包清单里「可被独立校验」的部分：把它们和消息一起哈希，改动清单就能被发现
_MANIFEST_FIELDS = ("group_id", "group_name", "group_info", "members", "trusted")


def compute_manifest_checksum(manifest: dict) -> str:
    """清单指纹：覆盖群号、群名、群信息、成员列表。

    消息的校验和证明不了这几样。包里的群名、成员表是谁都能改的「自由文本」，
    改完清单依然自洽（checksum 字段只覆盖消息），恢复时就把伪造的群名和成员
    写进目标群。导出时把这几样一起算进指纹，恢复时对上才算没被动过。
    """
    payload = {k: (manifest or {}).get(k) for k in _MANIFEST_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _normalize(value):
    """把快照里的时间、数字规整成同一种写法，好让「导出端写的」和「库里读出来的」逐字节可删。"""
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def diff_snapshot(actual: dict, trusted) -> list[str]:
    """包里自带的群信息/成员表，和可信快照对不上就返回差异说明。

    `trusted` 是导出时按本机库写进清单的那份（`manifest['trusted']`）。老包没有
    这个字段，返回空表——不能因为少个新字段把旧包全判成损坏。
    """
    if not isinstance(trusted, dict):
        return []
    problems: list[str] = []
    real_info = (actual or {}).get("group_info") or {}
    want_info = trusted.get("group_info") or {}
    for key in ("group_name", "owner_id", "member_count"):
        got, want = _normalize(real_info.get(key)), _normalize(want_info.get(key))
        if want and got != want:
            problems.append(f"群信息 {key}（包里 {got!r}，应为 {want!r}）")
    real_members = {
        str(m.get("user_id")): _normalize(m) for m in ((actual or {}).get("members") or [])
    }
    want_members = {str(m.get("user_id")): _normalize(m) for m in (trusted.get("members") or [])}
    if want_members and real_members != want_members:
        problems.append(f"成员列表（包里 {len(real_members)} 人，应为 {len(want_members)} 人）")
    return problems


def verify_zip(path: str | Path, expected_checksum: Optional[str] = None) -> tuple[bool, str]:
    """校验 ZIP 备份包：CRC 全量扫描 + manifest 校验和比对 + 包内文件齐不齐。

    `expected_checksum` 只该传包自己的 manifest 校验和。传「备份记录里的校验和」
    会误伤：记录只覆盖那次备份新增的消息，而 `export latest` 的包还含实时归档
    与更早的数据，两者本来就不该相等。内容与备份记录是否一致，由恢复时按消息
    集比对，见 `RestoreEngine.restore`。
    """
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
            # 清单被整体改写过吗？消息校验和只管 messages，群名/群成员改了它照样
            # 对得上。带 manifest_checksum 的包（新导出的）必须自洽，老包没有这个
            # 字段就跳过——不能因为少了新字段把旧包全判成损坏
            declared = manifest.get("manifest_checksum")
            if declared and declared != compute_manifest_checksum(manifest):
                return False, "备份包的群信息与清单指纹不符，备份可能被修改"
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
