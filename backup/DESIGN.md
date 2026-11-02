# Backup 高级备份 · 设计说明

## 分层

```
main.py            命令注册 + 权限 + 事件钩子（唯一依赖 AstrBot 的文件）
core/commands.py   命令文案渲染、权限判定（不依赖 AstrBot，可单测）
core/backup_engine.py   备份主流程（分页拉取 → 解析 → 下载 → 入库 → 校验）
core/restore_engine.py  恢复（校验 → 按策略写入）
core/export_manager.py  导出调度 + ZIP + 临时下载 token
core/parser.py     平台消息归一化（AstrBot 对象 / dict 双兼容）
core/database.py   SQLite 存储（所有 SQL 收敛在此）
core/incremental.py 增量点（timestamp + message_id 复合排序）
core/media_downloader.py 限速 / 重试 / 去重 / 大小上限
core/scheduler.py  APScheduler 定时任务（缺失依赖时降级为仅落库）
core/auto_trigger.py 退群聚合、解散预警触发
core/verifier.py   确定性校验和 + ZIP 校验
exporters/         6 种格式，统一 export(ctx, path)
```

## 关键决策

**增量点为什么不用 message_id**
不同平台 ID 格式不同，且可能回退（如群迁移后重排）。`(timestamp, message_id)` 复合排序在消息乱序或同毫秒并列时仍然稳定。

**校验和为什么忽略自增 id / backup_id**
同一批数据在两次备份里 id 必然不同，backup_id 更不同。只哈希 `message_id / group_id / sender_id / timestamp / message_type / content` 并按同样顺序排序，才能做到"同数据同校验和"，恢复前比对才有意义。

**媒体下载失败为什么只记 partial**
红线要求单文件失败不能阻塞整体。失败 URL 替换成 `[下载失败] {url}` 留在消息里，用户能追溯；备份记录状态降为 `partial` 而不是 `failed`。

**导出为什么不直接给文件路径**
红线要求链接带 token 且过期。`ExportManager` 内存里维护 `token → (path, expires)`，`resolve_token` 校验是否过期，过期即作废。导出目录在 `storage/exports`，不在 Web 根目录。

**恢复为什么强制 `--confirm`**
`RestoreEngine.restore` 自身检查 `confirm` 参数，不止依赖命令层。任何调用路径都必须显式确认。

**Event 适配**
真实平台方法名在不同 AstrBot 版本/适配器下不一致（`get_group_msg_history` / `get_group_history` / `get_history`），`PlatformAdapter` 逐一探测，全都没有就返回空数据而不是抛异常，避免一个 API 变更拖垮整个备份。

## 红线对照

| 红线 | 落地位置 |
| --- | --- |
| 日志不打印消息内容 | 全仓日志只打 message_id / 群号 / 异常类型 |
| 媒体失败不阻塞 | `BackupEngine._download_media` 内 try/except，计失败数 |
| 备份不在 Web 目录 | `storage/exports`，靠 token 转发 |
| 链接带 token 且过期 | `ExportManager._issue_token` / `resolve_token` |
| 恢复二次确认 + 校验 | `RestoreEngine.restore(confirm=True)` + `verify_backup` |
| 大群分页 | `PAGE_SIZE = 50` 循环拉取 |
| 超时兜底 | aiohttp `ClientTimeout`、APscheduler `misfire_grace_time` |
| pathlib 路径 | 全部用 `Path`；`safe_filename` 剥离目录部分 |
| SVG 素材自产 | `assets/*.svg`、`templates/chat.html` 内联 SVG |
| 作者信息 | `metadata.yaml`、`README.md`、HTML 导出页页脚 |
