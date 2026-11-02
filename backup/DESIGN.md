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
core/event_hooks.py   AstrBot 事件 → 实时归档 / 触发式备份（鸭子类型，可单测）
core/paths.py      存储目录解析 + Web 根越界拦截
core/download_route.py  token → 文件流（与框架无关的纯逻辑）
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
红线要求链接带 token 且过期。token → (path, expires) 存 `download_tokens` 表而不是内存 dict：
插件重启后链接仍然有效，过期即删。`resolve_token` 除了查过期，还要求文件落在 `exports/` 之内
（防止路径穿越），`take_token` 供一次性下载。`core/download_route.py` 的 `handle_download`
把请求路径变成文件流，不依赖具体 Web 框架，方便接任何宿主。

**存储目录为什么不在插件目录里**
插件通常装在 AstrBot 的 `data/plugins` 下，面板一旦把 `data` 静态托管，`storage/` 就被挂在公网。
默认落到 `~/.astrbot-backup`；配了 `storage_dir` 就按配置解析，但必须不在 `storage.web_roots` 内，
否则启动即抛 `UnsafeStoragePath`（红线 3）。相对路径按插件目录解析而不是 CWD，
避免启动位置变化导致数据「跑」到别处。

**消息为什么按 (message_id, group_id) 唯一**
唯一键留着 backup_id，同一批消息每备份一次就多一行。全量重跑、失败重试、恢复都会触发，
结果是导出和 HTML 聊天记录里同一条消息出现好几遍，群消息总数也翻倍。
现在一条消息只留一行、`backup_id` 记最后一次归档它的备份，代价是「某次备份覆盖了哪些消息」
要按归属反查（`count_backup_source` / `list_backup_ids_with_messages` 就是为此而生）。
老库在启动时自动重建表并去重，升级不丢数据。

**空跑的备份为什么不顶掉上一次**
只按状态挑「最近一次备份」，会让一次什么都没备份的新记录盖掉上次的实绩，
之后 `/backup status` 永远显示 0 条。`latest_completed_record` 先按 `message_count > 0` 挑，
没有才退回任意完成记录。

**ZIP 恢复为什么也要比校验和**
只在「消息来自库」时比校验和，等于给 ZIP 路径放了行：包里的 manifest 只要自洽，
内容被换过也能一路写进目标群。现在两条路径一视同仁——包里解析出的消息必须和记录里的
校验和一致。顺带修了解析器：导出器把消息摊平成顶层 dict，之前只认消息链，从 ZIP 读回来
`content` 会变成空串，校验和必然不符。

**恢复为什么强制 `--confirm`**
`RestoreEngine.restore` 自身检查 `confirm` 参数，不止依赖命令层。任何调用路径都必须显式确认。

**实时归档的数据为什么不能被后续备份「接管」**
消息唯一键去掉 backup_id 之后，全量备份会把库里已有的消息统统改成归自己，包括实时归档
那条 `backup_id='realtime'`。看着只是换了个归属，实际是：`export latest` 只按最近一次
备份取数，实时归档那批消息根本不属于任何一次备份——归属一被接手，它们就从「最近一次
备份」里消失，而这恰好是群被炸时最要紧的那一段。

所以现在的规则：
- 已在库里的消息，备份不重写、不改归属（`message_exists` 先问一句，命中就跳过并计数为 `skipped`）；
- 增量点只用「非实时归档」的消息推进（`latest_backup_message_key`），否则一条晚到的
  实时归档会把水位顶到未来，之后整个时间段的正常消息全被跳过；
- `export latest` 在最近一次备份之外，把 `realtime` 的那部分补进来；若上次备份已经把
  全群接手、更早的备份名下已无消息，则退回整群导出——单条按归属取数时，「一次全量重跑」
  会把历史从 `latest` 里抹掉。

**中断续传为什么不能只看增量点**
增量点只有整轮跑完才 `advance`。备份在第 3 页超时中断时，库里已经落了 150 条，增量点
还停在上一轮——直接重跑会把这些重拉一遍、媒体重下一遍（`media_files` 还会多插一份，
`total_size` 跟着翻倍）。现在额外取「库里最新的非实时归档消息」做下界，两道闸取更靠后的
那个：续传就真的只是往下跑。

一个坑：水位只在增量盘跑过之后生效。全量重跑时库里可能已经堆了实时归档的消息（时间靠后），
拿它当水位会把正常消息一起跳掉——这一点是写完测试才发现的，见
`TestRealtimeArchiveOwnership.test_realtime_archive_does_not_advance_incremental_point`。

**ZIP 包为什么还要查「东西齐不齐」**
manifest 自洽只能证明「没人改过清单」，证明不了「清单里的东西都在」。恢复前 `verify_zip`
现在同时要求：包内有消息文件（`json/` 下至少一个）、manifest 里登记的 `media_files`
全部存在于包内。少媒体会在恢复前就说出来是哪几个，而不是恢复完发现聊天记录里全是图片占位。

**实时归档为什么放在事件钩子而不是等定时备份**
群被炸、机器人被踢时，平台历史消息接口往往已经拿不到了。`EventHooks.archive_message`
在消息进群那一刻就写库（只写库、不下载媒体），定时/增量备份再补齐媒体与成员快照。
钩子只做判断，AstrBot 事件字段全部鸭子类型读取，适配器改名不影响核心逻辑。

**Event 适配**
真实平台方法名在不同 AstrBot 版本/适配器下不一致（`get_group_msg_history` / `get_group_history` / `get_history`），`PlatformAdapter` 逐一探测，全都没有就返回空数据而不是抛异常，避免一个 API 变更拖垮整个备份。

**恢复校验为什么分两层**
备份记录里的 `checksum` 覆盖的是「那次备份新增的消息」，不是整群。`export latest`
的包还会带上实时归档与更早的数据，两者天然不相等——早先拿记录的校验和去比包的
manifest，结果是「自己导出的包自己恢复不了」。现在：`verify_backup` 只把包和包自己的
manifest 比（外加 CRC 与文件齐全性），内容是否可信交给 `_check_content`：包路径比包
清单，库路径比记录。记录只覆盖得住整群时（一次全量备份接手了所有消息）记录与库
不再等值，`_record_owns` 会识别出来，不误判成「库被改过」。

**翻页为什么需要去重兜底**
部分适配器不认 `offset`，每页都回同一批消息。库里已经有它们，`message_exists` 让每条
都被跳过，`len(batch) == PAGE_SIZE` 又永远成立——循环不会结束，备份卡在「进行中」，
群越大越像死掉。现在每页先按 message_id 去重，整页都翻过就判定分页到头，正常收工。

**媒体表为什么按 (message_id, original_url) 唯一**
媒体补下、续传重跑、同一条消息的多个附件都会调 `save_media`。没有唯一键时重跑一次就
多插一份，`media_files` 随重跑次数膨胀，`total_size` 跟着虚高。老库启动时自动重建并去重
（`_migrate_media_unique`）。

**导出文件名为什么要带随机后缀**
文件名精度只到秒：连点两次导出、或者一次批量导出多个群，同一秒内就会撞在同一个路径
上。后一份覆盖前一份，前一个 token 指向的是别人的文件。现在路径末尾加 6 位随机串。

**链接为什么一次性、而 Range 请求要放行**
链接是发给群成员的，转发出去就不该被反复领取，所以默认兑换一次即作废。但大文件下载
时浏览器会先发 `Range: bytes=0-` 探测再续传，那是同一轮下载的第二次请求——放行
`Range` 的前提是这一条下载从一开始就没被别处领走，因此 `handle_download` 仍然先兑换
token，再把区间切片返回 206。回不了续传的话，用户拿到的是半个 ZIP。

**清理为什么不再删消息**
`delete_record` 会连带删掉 `backup_id` 名下的消息。可消息按 `(message_id, group_id)`
唯一、一条消息只归属最后一次归档它的备份，删一条老记录等于把那个群的历史连根拔掉，
连别的备份还在用的都不管了。`prune_records` 只删记录；占地方的是导出文件和媒体，
分别由 `clean_exports` 与 `prune_media` 处理。`keep_days` / `keep_count` 写 0 是
「不淘汰」，不是「立刻全删」。

**恢复时怎么认出被整体改过的包**
包内 manifest 和内容可以一起被改：改完用恢复端自己的算法重算校验和填回去，清单与内容
依然「自洽」。所以恢复端只信两个不会被包作者碰的东西——本地库里的归属，和备份记录
本身：包里消息的归属必须是这条记录（同群实时归档除外），否则就是夹带；库里不认识的
消息，只有记录名下那些已经全在包里时才算夹带（换机器恢复时它们本来就不在库里，
不能算）。判断不了就放行，`--confirm` 仍是最后那道闸门。

**时区为什么要在解析时就抹平**
时间戳在库里是 `%Y-%m-%d %H:%M:%S.%f` 字符串，tz-aware 值落库时会连带丢掉时区，
读回来凭空多出几小时。增量水位是 `(timestamp, message_id)` 比较的，一个脏点写下去，
那段时间的正常消息全被判成「更旧」而跳过，`advance` 遇到两种时间还会直接抛 TypeError。
`_to_datetime` 统一换算成本地时间再去掉时区。

**cron 为什么不交给 APScheduler 校验**
APScheduler 是延迟导入，缺依赖时降级为「只落库不调度」。降级后若还用它的
`CronTrigger` 校验，`schedule set` 会永远失败。自己校验 5 段：数字在界内、步长非零、
区间不反着写（`5-3` 是写错不是「每三天」）。

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
