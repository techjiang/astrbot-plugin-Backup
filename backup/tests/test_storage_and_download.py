"""存储路径安全（红线 3）+ 下载 token（红线 4）。"""

import time
from datetime import datetime
from pathlib import Path

import pytest

from backup.core.download_route import PREFIX, handle_download
from backup.core.export_manager import ExportManager, parse_expire
from backup.core.paths import DEFAULT_ROOT, UnsafeStoragePath, assert_writable_target, resolve_storage_root
from backup.tests.conftest import make_message


def test_relative_storage_dir_resolves_under_plugin_dir(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    root = resolve_storage_root({"storage_dir": "storage"}, plugin_dir)
    assert root == (plugin_dir / "storage").resolve()
    assert root.is_dir()


def test_empty_storage_dir_uses_home_default(tmp_path):
    root = resolve_storage_root({"storage_dir": ""}, tmp_path)
    assert root == DEFAULT_ROOT.resolve()


def test_absolute_storage_dir_is_respected(tmp_path):
    target = tmp_path / "somewhere" / "else"
    root = resolve_storage_root({"storage_dir": str(target)}, tmp_path)
    assert root == target.resolve()


def test_storage_inside_web_root_is_rejected(tmp_path):
    web_root = tmp_path / "webroot"
    web_root.mkdir()
    inside = web_root / "backup-storage"
    inside.mkdir()
    with pytest.raises(UnsafeStoragePath):
        assert_writable_target(inside, [str(web_root)])


def test_storage_outside_web_root_passes(tmp_path):
    web_root = tmp_path / "webroot"
    web_root.mkdir()
    outside = tmp_path / "safe"
    outside.mkdir()
    assert assert_writable_target(outside, [str(web_root)]) == outside


def test_sibling_prefix_dir_is_not_inside(tmp_path):
    """webroot=/a/web 时 /a/web2 不该被误判为在内部。"""
    web_root = tmp_path / "web"
    web_root.mkdir()
    sibling = tmp_path / "web2"
    sibling.mkdir()
    assert assert_writable_target(sibling, [str(web_root)]) == sibling


@pytest.fixture
def exports(db, tmp_path):
    return ExportManager(db, tmp_path / "exports", tmp_path / "media", {"export": {"download_link_expire": "1h"}})


def _save(db, n=3, group_id=100):
    for i in range(n):
        db.save_message(make_message(i, group_id=group_id), "b1")


def test_token_persists_in_database_and_resolves(exports, db, tmp_path):
    file = tmp_path / "exports" / "x.zip"
    file.write_bytes(b"PK\x03\x04data")
    token, expires = exports._issue_token(file)

    # 换一个 manager 实例（模拟重启）仍能兑换
    again = ExportManager(db, tmp_path / "exports", tmp_path / "media")
    assert again.resolve_token(token) == file
    assert expires > datetime.now()


def test_expired_token_is_rejected(exports, db, tmp_path):
    file = tmp_path / "exports" / "old.zip"
    file.write_bytes(b"data")
    db.save_token("tok", str(file), time.time() - 1)
    assert exports.resolve_token("tok") is None
    assert db.get_token("tok") is None  # 过期即删


def test_token_outside_export_dir_is_rejected(exports, db, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    db.save_token("tok2", str(outside), time.time() + 3600)
    assert exports.resolve_token("tok2") is None


def test_take_token_is_one_shot(exports, db, tmp_path):
    file = tmp_path / "exports" / "once.zip"
    file.write_bytes(b"data")
    token, _ = exports._issue_token(file)
    assert exports.take_token(token) == file
    assert exports.resolve_token(token) is None


def test_download_route_serves_tokenized_file(exports, db, tmp_path):
    _save(db)
    result = exports.export(100, "txt")
    resp = handle_download(PREFIX + result.token, exports)
    assert resp.ok
    assert resp.file == result.path
    assert resp.headers["Content-Disposition"].startswith("attachment;")
    assert resp.headers["Cache-Control"] == "no-store"
    assert b"".join(resp.iter_chunks()) == result.path.read_bytes()


def test_download_route_expired_returns_410(exports, db, tmp_path):
    file = tmp_path / "exports" / "gone.zip"
    file.write_bytes(b"data")
    db.save_token("dead", str(file), time.time() - 5)
    resp = handle_download(PREFIX + "dead", exports)
    assert resp.status == 410


def test_download_route_unknown_path_returns_404(exports):
    assert handle_download("/other/thing", exports).status == 404
    assert handle_download(PREFIX, exports).status == 404


def test_download_route_strips_query(exports, db, tmp_path):
    _save(db)
    result = exports.export(100, "csv")
    resp = handle_download(PREFIX + result.token + "?x=1", exports)
    assert resp.ok


def test_link_includes_base_url(exports, db, tmp_path):
    _save(db)
    exports.base_url = "https://example.com"
    result = exports.export(100, "txt")
    assert result.link == f"https://example.com/backup/download/{result.token}"
    assert result.download_path == f"/backup/download/{result.token}"


def test_clean_exports_purges_expired_tokens(exports, db, tmp_path):
    file = tmp_path / "exports" / "old.zip"
    file.write_bytes(b"data")
    db.save_token("stale", str(file), time.time() - 10)
    removed = exports.clean_exports(max_age_days=1)
    # 过期 token 一律清，跟导出文件的保留天数无关
    assert removed >= 1
    assert db.get_token("stale") is None


def test_clean_exports_keeps_everything_when_days_is_zero(exports, db, tmp_path):
    """keep_days 写 0 是「别淘汰」，不是「立刻全删」——以前那个 0 会把整个导出目录清空。"""
    file = tmp_path / "exports" / "keep.zip"
    file.write_bytes(b"data")
    assert exports.clean_exports(max_age_days=0) == 0
    assert file.exists()


@pytest.mark.parametrize(
    "value,seconds",
    [("24h", 86400), ("30m", 1800), ("7d", 604800), ("90", 90), ("", 86400), (None, 86400)],
)
def test_parse_expire(value, seconds):
    assert parse_expire(value) == seconds
