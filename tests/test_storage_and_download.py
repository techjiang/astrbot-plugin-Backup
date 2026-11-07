"""存储路径安全（红线 3）+ 下载 token（红线 4）。"""

import time
from datetime import datetime
from pathlib import Path

import pytest

from backup.core.download_route import PREFIX, handle_download
from backup.core.export_manager import ExportManager, parse_expire
from backup.core.paths import (
    LEGACY_ROOT,
    UnsafeStoragePath,
    assert_writable_target,
    default_root,
    resolve_storage_root,
)
from backup.tests.conftest import make_message


def _installed_dir(tmp_path):
    """照宿主装法的路径：`<data>/plugins/<插件目录名>`。"""
    plugin_dir = tmp_path / "data" / "plugins" / "astrbot-plugin-Backup"
    plugin_dir.mkdir(parents=True)
    return plugin_dir


def test_relative_storage_dir_resolves_under_plugin_dir(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    root = resolve_storage_root({"storage_dir": "storage"}, plugin_dir)
    assert root == (plugin_dir / "storage").resolve()
    assert root.is_dir()


def test_empty_storage_dir_uses_the_host_plugin_data_dir(tmp_path):
    """不给 storage_dir 时落在 `data/plugin_data/backup`——宿主自己管的那块。

    以前默认是 `~/.astrbot-backup`：宿主的插件数据清理、迁移、审计都扫不到它，
    用户卸载插件后那堆备份还留在原地，谁也不知道。上架规范要求落在插件数据目录，
    这里把它钉住。
    """
    plugin_dir = _installed_dir(tmp_path)
    root = resolve_storage_root({"storage_dir": ""}, plugin_dir)
    assert root == (tmp_path / "data" / "plugin_data" / "backup").resolve()
    assert root.is_dir()


def test_default_root_is_named_after_the_plugin(tmp_path):
    plugin_dir = _installed_dir(tmp_path)
    assert default_root(plugin_dir).name == "backup"
    assert default_root(plugin_dir).parent.name == "plugin_data"


def test_missing_storage_dir_falls_back_to_the_same_place(tmp_path):
    """`storage_dir` 整个不写和写空串是一回事。"""
    plugin_dir = _installed_dir(tmp_path)
    assert resolve_storage_root({}, plugin_dir) == resolve_storage_root(
        {"storage_dir": ""}, plugin_dir
    )


def test_legacy_home_dir_is_no_longer_the_default():
    """老路径只留作提醒，不许再被当成默认目录写进去。"""
    assert LEGACY_ROOT.name == ".astrbot-backup"


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
    """`base_url` 是**域名**，不是完整前缀——路径段得由插件补上。

    这一条原来断言 `f"https://example.com/{result.token}"`，注释写「base_url 就是
    链接前缀，不能再补一层 `/backup/download`」。那个理解是错的：网关只认
    `/backup/download/<token>`，按旧写法拼出来的链接在网关上一定是 404，而
    `download_link()`（`/backup export` 回执走的那条）一直拼的是带路径段的形状。
    同一个配置项、两条产出、两种拼法，其中一条从没被点开过。
    """
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
