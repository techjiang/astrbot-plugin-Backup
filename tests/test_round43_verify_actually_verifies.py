"""第 43 轮：`/backup verify <备份ID>` 从来没校验过任何东西。

这一条的起点是发版前把 README 的「命令速查」表逐行跑一遍。跑到 verify 那行：

```
/backup verify bk1
✅ 本机记录校验和：1a992468c9be26dc
```

那个 ✅ 是个装饰。它只是把 `backup_records.checksum` 里存着的字符串截了前 16 位
打出来——库里那批消息被改成什么样，这一句都是同一个形状、同一个 ✅。

而「重算一遍再比」在这里完全做得到：来源清单 + 现存的库就是备份当时那批数据。
`core/verifier.compute_checksum` 早就写好了（确定性哈希，与顺序无关），
`RestoreEngine` 也一直在用它，只有这条命令没用。

同一个函数的另一条分支（给了备份包）是真的在校验——所以问题更隐蔽：
用户见过它报「✅ 包与自身清单一致」，就以为这个命令整体可信。
"""

import pytest

from .conftest import load_plugin_for_real, real_astrbot_available


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """真插件实例，配置注入（注入才不会写仓库里的 config.yaml）。"""
    if not real_astrbot_available():
        pytest.skip("需要真 AstrBot（pip install astrbot==4.14.6）")
    monkeypatch.setenv("HOME", str(tmp_path))

    class _Cfg(dict):
        def save_config(self):
            return True

    instance, _ctx, _mod = load_plugin_for_real(
        monkeypatch, config=_Cfg({"storage_dir": str(tmp_path / "storage")})
    )
    yield instance
    try:
        instance.gateway.stop()
    except Exception:
        pass
    try:
        instance.db.close()
    except Exception:
        pass


class _Plat:
    """够备份引擎跑一轮的最小平台。参数名要真（`group_id`），不是 `g`——
    引擎按 `inspect.signature` 重映射参数名，写成 `g` 会被当成「签名不对」跳过。
    """

    async def get_group_info(self, group_id):
        return {"group_id": group_id, "group_name": "测试群"}

    async def get_group_members(self, group_id):
        return []

    async def get_group_announcements(self, group_id):
        return []

    async def get_group_files(self, group_id):
        return []

    async def get_group_history(self, group_id, count=50, offset=0):
        from datetime import datetime

        if offset:
            return []
        return [
            {
                "message_id": f"m{i}",
                "group_id": group_id,
                "sender": {"user_id": 1, "nickname": "甲"},
                "timestamp": datetime(2024, 5, 1, 10, i).timestamp(),
                "message": [{"type": "text", "data": {"text": f"消息 {i}"}}],
            }
            for i in range(4)
        ]


async def _say(plugin, text):
    import types

    event = types.SimpleNamespace(
        message_str=text,
        get_group_id=lambda: "10001",
        is_private_chat=lambda: False,
        is_admin=lambda: True,
        get_sender_id=lambda: "7",
        plain_result=lambda t: t,
    )
    return [r async for r in plugin.backup(event)]


async def test_verify_recomputes_the_checksum_instead_of_showing_it(plugin):
    """库里的消息被改过，verify 必须报 ❌。旧代码上这条后半段是红的。"""
    record = await plugin.engine.backup_group(
        10001, _Plat(), incremental=False, download_media=False
    )
    assert record.message_count == 4

    ok = (await _say(plugin, f"backup verify {record.backup_id}"))[0]
    assert ok.startswith("✅"), ok
    assert "4 条消息" in ok, ok

    plugin.db.execute("UPDATE messages SET content = '被改过' WHERE message_id = 'm2'")
    bad = (await _say(plugin, f"backup verify {record.backup_id}"))[0]
    assert bad.startswith("❌"), bad
    assert "校验不一致" in bad, bad


async def test_verify_says_early_records_cannot_be_checked(plugin):
    """老记录没有校验和：不能报 ✅（假装验过），也不能报 ❌（谎称损坏）。"""
    from backup.core.models import BackupRecord, BackupStatus

    plugin.db.save_record(
        BackupRecord(
            backup_id="old1",
            group_id=10001,
            status=BackupStatus.COMPLETED,
            started_at=None,
            finished_at=None,
            message_count=0,
            media_count=0,
            total_size=0,
            is_incremental=False,
            base_backup_id=None,
            checksum="",
            error=None,
        )
    )
    text = (await _say(plugin, "backup verify old1"))[0]
    assert text.startswith("⚠️"), text
    assert "没有校验和" in text, text
    assert "❌" not in text


async def test_verify_reports_messages_wiped_from_the_library(plugin):
    """清单里登记过、库里一条不剩：要明说「可能已被清理」，并指向包。"""
    record = await plugin.engine.backup_group(
        10001, _Plat(), incremental=False, download_media=False
    )
    plugin.db.execute("DELETE FROM messages")
    text = (await _say(plugin, f"backup verify {record.backup_id}"))[0]
    assert text.startswith("❌"), text
    assert "一条都没剩下" in text, text
    assert "备份包路径" in text, text
