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

**为什么「备份来源」要和「归属」分开记**
`messages.backup_id` 只有一个值，记的是「最后归档这条消息的那次备份」。一次全量重跑
会把库里同群消息的归属全部接手，于是老备份名下变成空的——而恢复老备份恰恰要找它。
`backup_sources` 记的是「这次备份覆盖过哪些消息」，一份消息可以属于多份备份的来源，
不会被后续备份擦掉。`/backup restore <老ID>` 和 `/backup export ... <老ID>` 都从这里取数。
清单为空的（升级前的老库）才退回按归属取。

**恢复为什么不该只吃本地库**
「本地没有这条记录」以前直接被判成「备份不存在」。可最需要恢复的场景恰好是原机器没了、
群被炸了，手里只有一个 ZIP。现在：
- 包里的 manifest 写全了 `record` / `group_info` / `members`，够独立恢复；
- 本地查不到记录但有包时，按包自带的信息定目标群、重建记录与群信息快照；
- 恢复完把来源清单补上，之后 `list` / `export` / `diff` 都认得这次恢复。

不补这些的话，包恢复成功了却查不到、导不出、群里成员一片空白，等于白恢复。

**为什么一份备份只恢复它自己那个群的那些消息**
备份里可能混进别的群的消息：`_record_owns` 之外的历史、或恢复时被重新归属过的记录。整份
搬进目标群会凭空造出一堆「别人群里的消息」。恢复时按来源群过滤，日志说清丢了几个群。

**成员接口挂掉为什么不等于备份失败**
权限受限的适配器常给不出成员列表，但这和历史消息无关。以前这里抛异常就把整次备份标成
`failed`，一条消息都没存下来——备份的主体反而被附件元数据拖垮了。现在群信息/成员各自
try，失败就沿用上次快照并在 `error` 里说明；只有历史消息拉不到才算失败。

**`message_count` 到底数什么**
数「这一轮实际写入库的消息数」，不是「新增了几行」。增量没跑到新消息是 0，全量重跑也是
0/翻一遍，两种情况在 `list` 里都显示真实覆盖量，同时 `is_incremental` 会说明这是增量还是
全量——库里已经有消息时不会再谎称「首次全量」。

**权限为什么还要看角色字段**
适配器的 `is_admin()` 常常没实现、直接抛异常，抛异常就当「没权限」会把所有人挡在门外。
所以 `is_admin` 不可用（或返回 False）时继续看 `sender_role` / `role` / `sender.role`，
都认不出才拒绝。

**通知钩子为什么必须有 `on_notice`**
`_on_notice_impl` 写好了却没人调，等于「大量退群触发备份」「群解散预警」「机器人被踢前
最后一次备份」三个功能从来没上线过——README 写着，代码不通。现在 `on_notice` 用
`filter.event_message_type(ALL)` 注册（与 `on_message` 同一入口），内部先判
`post_type == message` 直接返回：消息重投已经在 `on_message` 归档过了，再走一遍退群聚合
等于凭空造出 N 个退群者，一次普通聊天就能把阈值顶满。钩子整体 try/except，通知出问题也
不能把机器人的消息链路带崩。

**为什么 `sub_type` 要先于 `notice_type` 读**
OneBot v11 把类型放 `notice_type`（`group_decrease` / `group_disband`），原因放 `sub_type`
（`leave` / `kick` / `kick_me`）。原来的顺序是先读 `notice_type`，于是所有退群通知都被归类
成 `group_decrease`，既不在 `DECREASE_SUBTYPES` 里、也不在解散集合里，聚合永不触发。

**为什么 `leave` / `kick` 不算「机器人被踢」**
`leave` 是「有成员退群」，`kick` 是「有成员被踢」——被踢的可以是别人。把它们放进
`KICK_SUBTYPES` 的后果是：群里任意一人退群都会立刻做一次「离群前最后备份」，而真正的
`kick_me`（机器人自己被踢）不在集合里，永远不触发。三个集合现在互不相交，
`handle_notice` 的 if/else 才不会把一次退群判成两种事。

**解析产物为什么要保证稳定**
`@张三` + `" 看这个"` 用 `" ".join()` 拼出来是 `@张三  看这个`，中英混排的段之间又会粘成
`helloworld`。这不只是好看不好看：校验和是按 `content` 算的，同一段话两次解析结果不同，
跨来源比对就会假阳性。现在按「相邻两段都是 ASCII 词才补空格」来拼，段内原有空白不动。

**当时以为「只能实机确认的三处」，其实源码就能确认**
前面几轮反复说「`message_obj` 字段名、退群/被踢的 `sub_type` 取值、宿主自定义路由的挂载
方式只能实机确认」。第 20 轮把 AstrBot 4.14.6 的源码拉下来逐处对照，发现那不是「猜得准不
准」的问题，是**形状完全对不上**——而且「自洽的假设」让 350 个用例全绿却一条真实路径都
走不通。详见 [第 20 轮](#第二十轮a-strbot-真实形状)。

### 第十轮：状态快照与包完整性

**成员名单为什么改成整群替换**
`save_members` 只做 `INSERT OR REPLACE`，于是名单是「累加」的：退群的人永远留在库里，
`/backup status` 的成员数只增不减。成员列表本来就是「当前状态」而不是流水，`replace_members`
按本次快照整体替换，多出来的人一次性删掉。拉回来是空表（权限不足的适配器常见）就当没
拉到，不能拿空表把快照清空。

**翻页为什么不能按「本页不足 PAGE_SIZE 就收工」**
适配器不认 `count`、每页固定给 30 条时，第一页 30 < 50，循环当场退出，剩下 3 倍的消息一条
没备，状态还是 `completed`。现在低于半页才算到头，偏移量也改成按「平台真正吐回来的条数」
推进（而不是页码 × 页大小）——平台把 `offset` 当序号而非计数时，按页码算会整段跳过。
「翻过的消息集合」仍是收工兜底：平台完全不认 offset 时不会死循环。

**`total_size` 为什么按记录覆盖的消息算**
原来是「这个群所有媒体加起来」，于是每条记录的体积都等于全群总量，`/backup list` 里 10 条
记录的体积完全相同、加起来是实际的十倍。

**`/backup status` 为什么写「这次覆盖」**
记录里的 `message_count` 是「这次备份覆盖了多少」，不是「新增了几行」。一轮全是已在库消息
的增量备份 `message_count` 是 0，却实实在在覆盖了整批——写「这次新增：0 条」，用户看到的
就是「备份了个寂寞」。另外「其中实时归档 N 条（还没被任何一次备份接手）」原来按归属数，
而实时归档的归属故意不改，于是全量备份跑完这句永远不消失；改成「没有被任何来源清单登记」才对。

**导出为什么再按群筛一次**
来源清单记的是「这次备份覆盖过哪些消息」，一次全量备份可能连带把库里别的群的旧数据也收了
进来。导出某群的包却夹着别群的聊天记录是隐私问题，不是完整性问题。

**包里为什么要有 `manifest_checksum`**
消息校验和只覆盖 messages。包里的群名、群信息、成员表是自由文本，改完清单依然「自洽」，
恢复时就把伪造的群名和成员写进目标群。新增的 `manifest_checksum` 把 `group_id` / `group_name`
/ `group_info` / `members` 一起哈希；老包没有这个字段就跳过（不因为少个新字段把旧包全判死）。

**`parse_message` 的群号为什么要先看消息自己**
原来的写法是 `group_id or raw.group_id`——传进来的兜底群号优先。这让导出包里的 `group_id`
形同虚设：包里的消息被改成哪个群都能算成原群，「恢复时不许夹带别群消息」这条校验直接被
绕过去。现在消息自带的群号优先，参数只作兜底。

**恢复的校验为什么必须在按群过滤之前**
`restore` 先把消息按 `source_group` 过滤，再 `_check_content`。包里被改成别群的消息在过滤
那一步被静默丢掉，`_check_content` 拿到空列表一路放过——「恢复 0 条」看着像成功，实际什么
也没恢复，也没告诉任何人包被动过。现在校验在前，过滤在后，过滤空了直接报错。

**换机器恢复为什么要把快照写进状态表**
包里的群信息和成员原来只写进 `backup_bundle`（这份备份自己的证据）。刚换机器恢复完，
`/backup status` 报「成员数 0」、导出的 HTML 里没有群名和成员——数据都在包里，只是没人搬进
`group_info` / `group_members`。现在本机没有这个群的任何状态时才用包补一份，本机有更新的
实时状态时不覆盖。

## 第 11 轮：三处「表面正常、实际不对」

**`message_count` 到底回答哪个问题**
它原来答的是「这次新存进库几条」，于是增量备份跑一轮全是已在库消息时报 0 条——
`/backup list` 里明晃晃写着「覆盖 0 条」，像是备份了个寂寞，而这轮实实在在覆盖了 50 条。
现在它答「这份备份保住了多少条消息」（来源清单口径），新增了几条由新的 `new_count` 字段
单独交代。两个数都得能看见：只改 `message_count` 的话，「这次又存进来几条」这个信息就没了。
老库打开时自动补 `new_count` 列（`_migrate_record_columns`）——`CREATE TABLE IF NOT EXISTS`
不会给已有的表加列，不补的话升级后第一次读记录就崩。

**`/backup diff` 为什么不能再按归属取数**
消息按 `(message_id, group_id)` 唯一，`backup_id` 只有一个值，记的是「最后一次归档它的备份」。
一次全量重跑就把整群消息划到新备份名下，老备份按归属取出来是空的——`/backup diff` 于是
永远报「A 0 条 / 相同 0 条 / 仅在 B N 条」，比什么都长这样。改成按来源清单比（老库没有清单
才退回按归属）。`count_backup_source` 同步改成数来源清单，否则「这份备份保住了多少条」
全被归属口径带偏。

**`export latest` 的「历史是不是空的」为什么换了判法**
原来判断「别的备份名下还有没有消息」用的是 `count_backup_source`。来源清单不会被后续备份
擦掉，于是每个老备份都「覆盖了满满一批」，这个条件恒为 False——「上次备份接手了整群、
老备份名下一个字都不剩」这种情况永远认不出来，导出 latest 就只剩最近那几条新增。
现在直接看别的备份名下还剩哪些消息行。

**Range 探测为什么不能吃掉 token**
token 一次性，目的是「链接转发出去不能被重复领走」。但浏览器下载大文件一定先发
`Range: bytes=0-` 探测再续传，原来每次兑换都删 token，探测成了唯一一次，真正取数据的那次
请求拿到 410——用户看到的是「下了一半断了」。现在区间请求按「同一次下载的后续片段」处理，
只有整份取走才作废。

**包里的群名和成员凭什么信得过**
`manifest_checksum` 只能证明「清单没被单独改过」：谁都能用同一套算法算完填回去。
包里的 `manifest['trusted']` 也一样——它和被比对的内容在同一个 ZIP 里，改完顺手重算
指纹，包就自洽了（实测：把 trusted 的成员表换成一个人，同机恢复照样通过）。

真正拦得住的是**本机库里另存的那份**：导出时把快照和它的 SHA-256 落进
`export_evidence` 表，恢复时只认这一份，包作者改不了他没写过的记录。
证据行本身被动过（快照与指纹对不上）同样拒绝恢复。

比对用「一致」而不是「包里有的都得对上」：包在群名或成员表上**单方面添加**内容
（删掉真群名换成自造的、多塞一个 owner）也自洽，只有双向相等才算没被改过。

换台机器时本机没有任何证据，此时只能信包——但会在日志里明确写「无法校验」，
不假装验过，也不因此把迁移这条路堵死（拦下去等于「换机器恢复」彻底不能用）。

## overwrite 为什么必须连来源登记一起清

`/backup restore --strategy overwrite` 原来的写法是「删掉目标群里那条消息，让它重新插入」。
可 `save_message` 是 upsert：行还在库里时走 UPDATE 分支、返回 `False`，于是后面那句
`assign_message_backup`（只为「新插入」准备）不会执行。

结果：消息内容换成了备份里的，来源清单却还挂着「这条属于别的备份」的记录。按来源导出
或再次恢复时，取出来的仍是覆盖前的旧内容——`--strategy overwrite` 变成一次看起来成功、
实际没换到位的操作。

现在删消息时一并撤掉**本群**的来源登记（`Database.delete_sources`），别的群同 ID 的
消息不受影响。skip 策略不写任何东西，登记也就原样保留。

## 库里被清理过之后，恢复按「剩下的那部分」走

`/backup clean` 只淘汰记录、不动消息，但媒体淘汰、手工删行、迁移只搬了一截，
都会让一条记录名下的消息少几条。原来的校验一律判「消息校验和不一致 → 备份可能已损坏」，
于是备份被打扫过一次之后就再也恢复不了。

现在的判定分三步：

1. 消息集和记录的校验和对得上 → 放行。
2. 对不上，但这条记录名下**确实少了**消息（来源清单登记过、现在库里查不到）→
   这是清理的结果，按现存部分恢复，日志写清少了几条。
3. 对不上，而且一条都没少 → 库被改过，拒绝。

第 2 步的「少了多少」必须直接问 `backup_sources` 表（`Database.source_message_ids`），
不能走 `get_source_messages`：后者要 JOIN messages，被删掉的行两边同时消失，
差集恒为空，「少了」永远认不出来。

## 恢复三次、解析嵌套段、媒体重启后去重（第 15 轮）

**同一个包恢复两次被自己拦下。** 换机器恢复完，再执行一次同样的命令是常规动作
（第一次可能没看清回执）。第二次的消息 ID 在库里认得，归属却是记录里的原群号
——「包里有不属于这条记录的消息」这条夹带判据把它判成了篡改。第一次恢复时
`_ensure_record` 没给重建的记录登记来源清单，于是「记录名下一条消息都不在包里」
这个换机判据也成立。修法：重建记录时按**源群**登记清单（不能按目标群，否则
按源群取数一条都取不到），夹带判据接受「已在来源清单里登记过」的消息。

**认不出的消息段解析成空内容。** 消息链里嵌套的段（`{'type':'text','data':
[{'text':...}]}`、平台自带的富文本）原来一层都进不去，最终落到 `UNKNOWN`，
`content` 是空串。备份插件存下一条空消息，等于这条消息丢了。现在从**消息链**
往下抠文字（不是整条 raw——`message_id`、昵称会被当成正文），进 `data` 袋后
只认 `text`/`content`，那里的 `name` 是文件名。

**媒体去重只在内存里。** `MediaDownloader._url_index` 重启即空，同一条消息的
同一张图会被再下一遍、按同样的文件名落回同一个路径——看着是「没重复」，其实
每次重启都把整批媒体重下了一遍，流量按群大小算。现在落盘前先问库
（`find_media_by_url`），文件还在就直接复用并补登记库里那行。

**`--strategy merge` 是空头支票。** 文档写着 skip/overwrite/merge 三种，代码里
merge 分支只写了句注释、行为等同 skip。现在真补：只填库里缺的字段（内容、媒体
路径、引用、发送者名字），本地已有的值一个不动，回执单列「补齐 N 条」。

顺带把 `save_message` 的边界写清：它的 UPDATE 分支**只动归属和媒体路径**，
不碰内容——日常备份不该把库里已有的消息内容改掉。要真覆盖走
`overwrite_message`，要补缺走 `apply_merge`。

## 分页假到头、自己导的包恢复不了、公告没进包（第 16 轮）

**第一页整页都翻过，不代表分页到头了。** 翻页的退出条件之一是「这一页全是翻过
的消息 → 平台不认 offset，收工」。但「每次都从最新往回翻」是很多适配器的真实
形态：`offset` 是从最新一条往回的偏移。上一轮备份已经把最新那段写进库，于是
第二次备份的第一页必然整页都是「翻过的消息」——循环当场收工，后面更旧的一大段
一条不备，状态还是 `completed`。

复现：120 条、每页 50 条、库里已有最旧的 20 条。修之前只调了 **1 次**
`get_group_history`，只看到 50 条。修法：连续**两页**都翻不出新东西才判定到头；
真·不认 offset 的平台第二页依然是同一批，所以死循环兜底仍然成立（用例
`test_ignored_offset_does_not_loop_forever` 的调用次数断言从 2 改成 3）。

**自己导出的包，恢复时被自己的篡改拦截拒掉。** `messages.backup_id` 只有一个值，
记「最后归档这条消息的备份」。本机还留着更老的一份备份、它也覆盖过同一条消息时，
归属就落在老备份名下——而这条消息本来就在这份记录的包里。于是
`_messages_outside_record` 判成夹带：

```
备份包含 2 条不属于备份 bk999 的消息，拒绝恢复
```

修法不是把这条判据放宽，而是加一个时间维度：看「接管那条消息的备份是什么时候
开始的」。这份记录动手**之后**才被接管的，那次接管本身就带来了同一条消息，包里
有它合情合理；记录动手**之前**就归别人的，说明它早就不是这份备份的东西，照样拦。

**群公告 / 群文件跟着包走。** 这两样备份时是采了的（`scope.group_announcements` /
`scope.group_files`），zip 里也写了 `announcements.json` / `group_files.json`，
但恢复路径从来没读过它们。换台机器恢复完，公告和文件清单全留在原机上。现在它们
进 `manifest`、进指纹（`_MANIFEST_FIELDS`），`restore` 完写回目标群；本机已有
更新快照时不覆盖。

顺带修了写回时的类型问题：包里的时间是 JSON 里的字符串，`to_db` 只认
`datetime`，`strftime` 直接抛 `AttributeError`——恢复在最后一步崩掉，前面的写入
已经落库。现在 `to_db` 对字符串原样放过。

**媒体淘汰会误删别群还在用的文件。** 媒体按 `{message_id}_{urlhash}.{ext}` 落盘，
两个群共用同一个 message_id 时落的是同一个路径。`prune_media` 只按「这条消息过期了」
就 `unlink`，于是另一个群那条新消息的图被一起删掉——那个群从此导不出图，
`media_files` 里那行还指着不存在的路径，HTML 里就是一堆断链。

修法不是不淘汰，而是多问一句「还有没有未过期的消息占着这个 ID」。淘汰的条件从
「这条旧消息的媒体」变成「没有任何新消息需要这份文件」。

**`group_id: 0` 被当成真群号。** 解析器写的是 `int(_get(raw, "group_id", fallback) or fallback)`，
看着没问题——但 `_get` 返回的是 **0**，而 `0 or fallback` 只在前者是假值时才兜底，
写成 `int(_get(...) or fallback)` 才会兜底。去掉 `int()` 之后 `0 or fallback` 是对的，
包一层 `int()` 就变成「0 是真值」。于是平台给出 `group_id: 0` 的成员/公告/文件条目
全落进群 0：备份报成功，导出包里这几份清单是空的，目标群查不到。

写成一个 `_group_or(raw, fallback)` 明确「0 也算不知道」，四个解析函数共用。

**快照条数按输入数报，不按入库数报。** `save_group_files` 返回 `len(rows)`，而写入
是 `INSERT OR REPLACE` + `(群, file_id)` 唯一键：平台给出两条重复 file_id 时只留一条，
返回值却多一个。现在写完再 `COUNT(*)`——报的数就是库里能查到的数。

**淘汰记录后，来源清单还在。** `delete_record` 删记录删消息，但 `backup_sources`
留着。那份登记指向一个不存在的备份：恢复路径会先在清单里找到消息、再拿一条不
存在的记录去比对，报出来的是一句看不懂的话。现在清单跟着记录一起删，别的备份对
同一条消息的登记不动。

## 实时归档不能把已有媒体洗掉

实时归档（消息一进群就写库）刻意不下载媒体：群活跃时同步下载会拖住每条消息的处理。
它和完整备份写的是同一行 `(message_id, group_id)`，按「后写覆盖」处理时，那份空的
`media_paths` 会把上一次完整备份存下的图片/语音路径洗成 `[]`——从此那条消息导出不带
媒体，`media_files` 里还留着文件，但没人再引用它。

`save_message` 的更新分支改成对 `media_paths` 取**并集**（发送者名字同理，空串不覆盖），
只增不减。代价是「同一份完整备份重跑时解析少了一两个附件」也不会把它们抹掉——比起
悄悄丢媒体，留着多一条路径是更小的错。

## 过期 token 为什么一条都清不掉

`clean_exports` 里写的是：

```python
removed = self.db.purge_expired_tokens(now - max_age_days * 86400)
```

本意是复用一个 `now`，实际是把「导出文件的 mtime 截止刻」当成了「当前时间」——比真实
时间小整整一个保留期。`expires < cutoff` 于是恒不成立，`download_tokens` 只增不减。
现在传 `time.time()`，过期即删。

## 记录删了，导出证据也得跟着走

`delete_record` 以前只删 `backup_records` / `backup_sources` / `messages`，`export_evidence`
留着。证据是「包里的群信息对不对」的判据，留着它，下次导出复用到同一个 `backup_id`
（恢复时会重建记录）时，恢复端会拿上一世代的证据比对，正常包被判成「被改过」。
`prune_records` 同理。

## `count_realtime_only` 的判据写成死条件

这个数回答「这个群里有多少条消息只走了实时归档」。判据原本写成「清单里没有它」，
而实时归档自己现在也登记清单——`backup_sources.backup_id = 'realtime'` 和
`m.backup_id = 'realtime'` 直接相消，恒为 0。改成「清单里没有**别的**备份」。

## 下载路由只认完整前缀

宿主注册路由时一般会把 `/backup/download/` 吃掉，`handle_download` 收到的只是
`/download/<token>`。只认完整前缀的话，这种接法一律 404——而 404 和「链接过期」在
用户眼里是同一个意思：都下不了。现在两种前缀都接住，其余路径照旧 404，免得把无关
请求误报成「链接已失效」。

## 公告 / 文件清单是「当时的」，不是「现在的」

`group_announcements` / `group_files` 两张表是当前状态，每次备份整群覆盖。导出包却一直
从这两张表取数——导一份上个月的备份，带出去的却是这个群今天的公告。那不是备份，是现状；
换台机器恢复，公告和文件清单也全留在原机上。

现在 `backup_bundle`（每份备份自己的快照）一起存公告与文件清单，导出优先取它，
当前状态表只在老库没有快照时兜底。时间字段两种形态都要吃得下：快照里是 `datetime`，
从包里读回来是 ISO 串，直接往 sqlite3 递 `datetime` 会抛 InterfaceError，备份在最后
一步崩掉（消息却已经全落库了）。

## 适配器接口：取到名字不等于能调

`PlatformAdapter._call` 原来「找到第一个可调用对象就调」。适配器上同名不同签名的方法
很常见（`get_group_notice(group)`），一调就 TypeError，而这个 TypeError 被上层当成
「公告拉取失败，沿用上次快照」吞掉——功能看着在，实际一次都没成功过。
现在签名不匹配就换下一个名字，全都不行才返回 None。

## 永远不会触发的 cron

`0 0 31 2 *`（2 月 31 日）每段都在界内，校验放行，APScheduler 也收下，然后建出一个
永远算不出下次触发时间的任务：`schedule set` 回「✅ 已设置」，之后一辈子不跑，`list`
里还挂着它。判据用 APScheduler 自己的 `get_next_fire_time(...) is None`，比手写日历
规则可靠；缺依赖时退回「固定月日是否超界」的兜底。

顺带：周字段 `7` = 周日是合法写法，而 `CronTrigger.from_crontab` 只认 0-6，直接递 7
会在挂任务时抛错。挂之前把 7 规整成 0，库里仍存用户写的原值。

## 恢复回执里的条数

导出包装的是「这份备份 + 只在实时归档里的消息」，条数天然不等于记录里的
`message_count`。以前回执写「写入 1 条」，用户数得出 3 条——包没坏，是那句话在撒谎。
现在导出时把包内条数写回记录（`package_count`），`RestoreResult` 也多带一个 `total`。

## `/backup restore <备份包路径>` 真的能用了

包里自带 `record` / `group_id`，但命令层只把第一个参数当备份 ID，包路径没有入口——
「从 ZIP 恢复」这条代码路径除了单测之外从来没有被用户走到，因为走不到。
现在第一个参数可以是一个包路径（按扩展名/路径分隔符识别），包里没有记录也能定目标群。

### 第二十轮：AstrBot 真实形状

前面十九轮的用例全绿（350 个），但它们和代码共用同一套**臆想出来的** AstrBot API：
消息段是 `{'type': 'image', 'data': {...}}` 这样的 dict、事件上有 `group_id` 属性、
`is_admin` 是布尔字段、历史消息接口认 `offset`。把 AstrBot 4.14.6 的源码拉下来逐处对照，
发现真实形状一处都不一样。这不是「猜得准不准」，是**每条主路径都走不通**。

**`ComponentType` 是 str-Enum，`str()` 出来是 `ComponentType.Plain`**
Python 3.11 上 `str(ComponentType.Plain)` 给 `"ComponentType.Plain"`（3.12 才改成返回值）。
原代码 `str(type).lower()` 得到 `componenttype.plain`，查 `_TYPE_KEYS` 恒为 None，直接进
else 分支——图片、语音、视频、文件全被当成文本，`media_paths` 永远是空的，`@` 和引用一起丢。
备份"成功"了，导出的 HTML 里一张图都没有。
现在 `_segment_type()` 先取枚举的 `.value`，再退回 `str()`，并剥掉 `ComponentType.` 前缀和
`Poke:poke` 这类子类型尾巴。

**消息段自己就是数据袋，没有 `data` 层**
AstrBot 的 `Plain.text` / `Image.file` / `At.qq` 都直接挂在对象上。原来的代码一律
`_get(seg, "data", seg)`，取到的就是段对象本身，靠 `_get` 的 getattr 兜底——能用，但
`File.file` 是个**会触发同步下载的 property**：异步上下文里返回空串并打警告，非异步
上下文里 `run_until_complete` 直接卡住事件循环。`_safe_get()` 用
`inspect.getattr_static` 判断是不是 property，是就不碰。

**适配器不提供 `get_group_history`，拉数据要走 OneBot `call_action`**
`aiocqhttp` 适配器只做「收消息、转事件」，历史消息得走 OneBot。而 `CQHttp.__getattr__`
把**任何**属性名变成 `functools.partial(call_action, 名字)`——探测式代码问「有没有
`get_group_history`」会拿到一个可调用对象，调下去真的发请求，看起来万事大吉。
更糟的是这类"方法"签名永远是 `(*args, **kwargs)`，所以 `PlatformAdapter._supports("offset")`
不能拿 `**kwargs` 当证据，只能看有没有显式声明过的参数名。

**OneBot 的历史消息只认 `message_seq`，不认 offset**
传 `offset` 进去会被静默忽略，每次返回同一批最新消息。引擎的去重兜底（连续两页都是翻过
的消息就判定到头）立刻收工——**每个群只备份最新一页，状态写 `completed`**。现在适配器自己
维护「上一页最旧那条」当锚点，并在平台一次给不满时反复补齐。

补齐还有个坑，是我改完第一版自己撞上的：补到 60 条、按 `count` 截成 50 条返回，被截掉的
10 条**锚点已经推过去了**，之后再也翻不回来——那一小段消息永久丢失，而备份报
`completed`。现在宁可一页多给几条，也不扔掉已经拿到的消息。

**`is_admin()` / `is_private_chat()` / `get_group_id()` 都是方法**
原代码 `getattr(event, "is_admin")` 拿到的是 bound method，`isinstance(value, bool)` 恒为
False——每一项探测都判「没有这个标记」。后果是管理员发 `/backup` 被「仅群主或管理员」挡
回去，`allow_private` 配置对真实私聊完全无效。

**OneBot notice 事件被包成普通消息事件**
适配器把 notice 也走 `handle_msg`，`type` 是 `OTHER_MESSAGE`、`message_str` 空，原始事件
在 `message_obj.raw_message` 里。原来的 `_notice_payload` 只读 `event.post_type` /
`event.message_obj.post_type`，两个位置都是 None——每条通知都摊成 `{'group_id': '100'}`，
`sub_type` 恒空。退群聚合、群解散、机器人被踢**一次都没触发过**。
现在 `notice_payload()` 直接钻 `raw_message`，并先判 `post_type == message` 直接返回，
免得一次普通聊天把「大量退群」的阈值顶满。

**`Context.get_platform` 也是方法**
`getattr(self.context, "get_platform", lambda: None)()` 拿到的是 bound method 当平台用，
后面每个 `_call` 都打在方法对象上全返回 None——`/backup now` 只会回一句「当前没有可用的
平台适配器」，定时备份同样拿不到平台。现在按「事件平台 id → 名字 → 唯一那个实例」找，
多开同类适配器时 id 才是唯一的。

**带发布时间的群公告能让整轮备份判 failed**
`published_at` 是 `datetime`，`json.dumps` 直接抛 `TypeError: Object of type datetime is
not JSON serializable`，而 `save_bundle` 在主流程里——消息全落库了，备份却写 `failed`。
这处是第 19 轮我自己引入的（那轮把公告塞进 bundle 时没考虑时间字段）。新增
`_jsonable_row()` 统一转换。

**`PARTIAL` 记录不算「最近一次备份」**
`latest_completed_record` 只认 `COMPLETED`。一次媒体下载失败降级成 `partial` 之后，
`/backup status` 报「还没有完成过备份」、`export latest` 导不出任何东西——数据明明都在库里。
`partial` 的语义是「消息都备上了，只是有几个媒体没下下来」，它就该算数。

**`/backup verify <包>` 拿记录的校验和比包清单**
记录的 `checksum` 覆盖「这次备份覆盖的消息」，`export latest` 的包还含只在实时归档里的
消息，两者天然不等。拿它当期望值，自己刚导出的包会被自己的校验判成「备份可能被修改」。
`verify_zip` 里早写了这个道理，命令层却一直在传。

**`json` / `markdown` 导出的是目录，下载链接下不动**
目录交给下载路由：`Content-Type` 退成 `octet-stream`，读流时 `open(dir)` 抛
`IsADirectoryError`。用户点出来的是一条打不开的链接。现在目录型产物在给链接前打成一个
zip，目录本身留着（本地翻着方便）。

**平铺消息从包里读回来变了样**
导出器把消息写成 `{message_type, content, media_paths, ...}` 平铺 dict。原来的解析要求
`content` 非空才认：纯图片、纯语音、纯文件消息的 `content` 本来就是空的，于是整条消息退化
成 `unknown`、媒体和引用全丢——**自己导出的包自己恢复不了**，报的还是一句「备份可能已
损坏」。另外平铺 dict 会被当成一个消息段遍历，`content` 被当正文捡走，平铺分支根本进不去。
新增 `_is_flat()` 先识别形态。

还有一处：链解析出「文本 + 引用」会归成 `REPLY`，但摊平消息自带权威的 `message_type`，
不能再改——改了就成了「库里 `text`、包里 `reply`」，校验和必然对不上。

**下载链接：连着两轮都读错了，这轮把宿主源码跑了一遍**

这条线上我错过两次，方向相反：

1. 第一轮：「4.14 上群里点链接做不到，`/api/plug/*` 一律要 JWT」；
2. 第二轮：「白名单里有 `/api/backup/download`，说明这条路通」，据此写了「上一轮
   结论下错了」，还把理由写进代码注释。

两次都是**只核一半就下结论**。把 AstrBot 4.14.6 装进来逐行对着
`dashboard/server.py` 核完之后，实际是两条约束同时成立：

- `srv_plug_route` 里是 `if route == f"/{subpath}"`，**字符串精确比较**。
  `"/backup/download/<token>"` 是我们的**字面量**，`subpath` 是真实尾巴——尖括号
  不参与比较，所以带 token 的路径**一条都命中不了**。第二轮我把「注册时能写路径参数」
  当成了「匹配时也认路径参数」，这是把两件事混作一件。
- `auth_middleware` 的白名单确实是前缀放行，但 `/api/backup/download` **属于宿主**：
  `routes/backup.py` 的 `BackupRoute` 已经把 `/backup/download` 注册成自己的
  `download_backup`（收 `filename` + JWT `token` 两个 query）。插件插不进去。而插件
  能挂的 `/api/plug/*` 不在白名单里，一律要面板 JWT，群里点链接的人没有 JWT。

所以在这个版本的宿主上，「把链接挂到面板上、群成员点开就下」**做不到**。这轮起了一个
内置网关（`core/gateway.py`）：`ThreadingHTTPServer`，只监听回环，默认开启，端口可配。
`download_link` 优先用它——它能真正兑换 token。

`make_view` 和面板路由登记都留着，因为这两条约束都是**宿主版本相关**的：哪天匹配改成
`startswith`、或者白名单对插件开放，登记的那条就会生效。但不再拿它当「能用」来宣传。

三条教训，记在这里免得再犯：

- **注册成功 ≠ 能命中 ≠ 能下载。** 这三件事要分别验证。`register_web_api` 返回没报错
  只说明前两件里第一件成立。
- **白名单里有一条前缀，不等于那条前缀归你。** 得看谁先注册、注册的是什么。
- **读源码得出的结论也要跑一遍。** 第二轮那条错误注释在代码里躺了一轮多，把后来每次
  排查都往「这条路是通的」上引，比没有注释更糟。

顺带两处接线 bug 一并修了（都是跑真框架才看得见的）：

1. **视图函数的参数对不上**。`srv_plug_route` 是 `await view_handler(*args, **kwargs)`，
   `subpath` 已经被它自己吃掉当路由匹配用，视图拿到的是**零个**位置参数。视图签名里
   写 `request` 就是 `TypeError`。现在视图自己去找当前请求。
2. **回元组是空文件**。`DownloadResponse.body` 在正常下载里是空的（文件在 `file` 上），
   回 `(body, status, headers)` 会让 Flask 认为响应体就是 0 字节：状态 200、
   `Content-Length: 0`、下下来的 ZIP 打不开。

**域名只有一个来源是不够的**

**域名只有一个来源是不够的**

`_public_base_url` 原来只认 `export.base_url` 和 `config["dashboard"]`，而后者在
`_conf_schema.json` 和 `config.yaml` 里**根本没有**——面板上填不了，文案却写着「留空
则用面板公网地址自动拼」。结果默认配置下永远拼不出链接，用户看到的是裸路径。
现在多了第三条也是最后一条路：**这条消息是从哪个域名进来的**，读 `Host` /
`X-Forwarded-Host`（逗号列表取第一跳）+ `X-Forwarded-Proto`。反代和内网穿透部署下，
配置里往往一个公网地址都没有，但请求本身就是从群成员点得到的域名过来的——这是唯一
现成的真相。内网与本机地址（`localhost` / `127.` / `192.168.` / `10.` / `172.16-31.`）
不认，宁可让用户看见「没配公网地址」，也不要给一条点不动的链接。

顺带补上依赖：`_as_flask` 里对 `werkzeug` 的导入是 try/except 兜底，而
`requirements.txt` 没写它。缺了不报错，只是每次下载都退化成自定义对象——假 Request 的
用例把 `body` 当字节读，永远看不见这一层；只有真 Werkzeug 会说话。现在它进了依赖，
并且用例里用 `importorskip` 钉住「装了就必须是 Werkzeug Response」。

**配置从来没读过面板那份**

`star_manager` 见到 `_conf_schema.json` 就建一个 `AstrBotConfig`（落在
`data/config/<插件名>_config.json`），再以 `config=` 关键字传给插件类。插件不接这个
参数，于是用户在面板里改的每一项都是写给自己的——包括 `retention.keep_days`、
`export.base_url`。容器里没有 AstrBot，这个关键字从来不会被调用，`config.yaml`
那条路看起来一直「好使」，二十轮都没人发现。

现在 `BackupPlugin.__init__(context, config=None)`，优先级：宿主注入 > `config.yaml` >
`schema` 默认值。`/backup config set` 也写回注入的那份（它会 `save_config()`），
没有注入才退回写 `config.yaml`。

**被踢为什么要单独一个冷却**

AstrBot 把同一条 notice 投给 `on_message` 和 `on_notice` 两个 handler，而
`on_message` 那条路径认不出「这是通知」也会走到触发判定。原来它跟退群聚合共用 30
分钟冷却：第一次跑完备份，半小时后同一个事件再来一遍——机器人都已经不在群里了，
`get_group_msg_history` 只会失败，备份列表里多一条 `failed`，看起来像备份坏了。
被踢是一辈子一次的事，冷却放到一天；退群聚合仍然是 30 分钟（那本来就是「一批人同时
退」）。

**插件市场的两个缺口**
AstrBot 只认 `logo.png` 当插件图标（`logo_fname = "logo.png"`），只有 SVG 就是空白方块；
配置面板靠 `_conf_schema.json` 渲染，只有 `config.yaml` 的话面板里什么都看不到。都补上了，
并加了一个用例保证 schema 与 yaml 的 26 个默认值逐项一致——漂移了用户就会看到「面板写着
90 天、实际按 30 天清理」。

**这一轮的教训**
十九轮里我一直在「代码内部找矛盾」，用例和代码共用同一套假设，所以永远绿。真正该问的是
**「这套假设和外面那个东西对得上吗」**——而这件事不需要实机，源码就能回答，我早该去翻。
前面几轮反复说「只能实机确认」，是把「我懒得去读依赖的源码」包装成了「环境限制」。

**实时归档的归属不能被备份接手**

`adopt_orphans` 原来把 `backup_id = 'realtime'` 也算成「无主」，理由是「实时归档那批
本来就该被本次备份覆盖」。覆盖是对的，但连归属一起改成本次备份就过了：`count_realtime_only`
的注释写的是「实时归档的归属故意不改，那是群被炸时的兜底数据」，两处口径互相打架。
真按注释那样用（按 `backup_id='realtime'` 找最后那几条）就一条也找不到，
`/backup status` 的「其中实时归档 N 条」也永远归零。

现在：覆盖靠**来源清单**表达（登记到本次备份），归属保持不变。
「归属的备份已经消失」的行照旧收编——那是另一件事，不能一起放过。

**备份失败时，已入库的消息要补登记来源清单**

备份在第 N 页超时时，前 N-1 页的消息已经 `save_message` 进库了，但 `add_sources`
要等整轮跑完才执行。失败的记录是在 `except` 里写进 `backup_records` 的，所以这些行的
`backup_id` 看起来完全正常，`adopt_orphans` 也不会来认领它们（它只收「归属的备份
已消失」的行）。结果：**库里 50 条、来源清单 0 条**，`/backup list` 说覆盖 0 条，
`/backup export` 和 `/backup restore` 都取不到——聊天记录明明就在那儿。

现在失败分支里补一次 `add_sources`，并把 `collected` 提到 `try` 之前。补登记自己出错
不能盖掉原本的失败原因。

**群信息：群号不一致不采信平台那份，群名缺失不清空**

`group_info` 按群号做主键，`INSERT OR REPLACE`。适配器回 `{'group_id': 999, ...}`
而我们在备群 100 时，记录会被写到**另一个群**名下：`/backup status 100` 查不到群名，
导出包里群号还对不上。`parse_group_info(strict_group=True)` 以请求的群号为准。

另一头是群名：OneBot 的 `get_group_info` 有时只回 `group_id` / `member_count`，
没有 `group_name`。直接存会把已有群名洗成空串，导出包的 `<title>` 和 HTML 头栏跟着
变空。新值是空的就沿用上次的。

**权限：`role == "admin"` 是「机器人主人」，不是「群管」**

AstrBot 4.14 的 `AstrMessageEvent.role` 只有 `member` / `admin` 两个值，而 `admin`
是拿 `admins_id` 配置比出来的（`pipeline/waking_check/stage.py`）。也就是说：群主在群里
发 `/backup now`，`is_admin()` 返回 `False`——一个「仅群主或管理员可执行」的开关，
把真正的群主挡在门外。

真正的群管身份在各平台适配器的 `message_obj.group` 上：`group_owner`（群主 QQ）和
`group_admins`（管理员 QQ 列表）。现在拿发送者 ID 去比对——比猜 `role` 字符串可靠，
因为它不依赖适配器怎么填。`role == "admin"` 那条仍然保留（机器人主人也是人）。

## 第 24 轮：把下载链路放进真 Quart 跑了一遍

前几轮的下载用例全是拿假的 Request 对象喂 `make_view`，断言的是我们自己造出来的
形状——用例和代码共用同一套假设，互相证明，谁也碰不到真实世界。这一轮装上真 Quart
（AstrBot 面板跑的就是它）、照 `dashboard/server.py` 的 `srv_plug_route` 写转发，
一次请求就撞出三个 500 / 404。

**宿主前缀削错了长度**

`srv_plug_route` 把插件路由挂在 `/api/plug/<path:subpath>` 上，于是 `request.path`
是 `/api/plug/backup/download/<token>`。原来写死只削 `path[4:]`（等于只削 `/api`），
削完是 `/plug/backup/download/<token>`——`handle_download` 两种前缀都不认，回 404。
而 404 和「链接已失效」在用户眼里是同一句话：下不了。

现在按宿主可能挂的几层前缀逐个匹配（`/api/plug`、`/plug`、`/api`、`/web`），
按最长的那层削。

**请求对象找错了框架**

`_current_request` 先试 Flask 再试 Quart。AstrBot 面板跑在 Quart 上，但
`astrbot/dashboard/server.py` 为了 JSON 序列化 `import flask` 了东西——于是
`from flask import request` 成功，拿到的是一个「当前没有活动请求」的代理对象，
一读 `request.path` 就 `RuntimeError: Working outside of request context`，视图 500。

现在先判 Quart（并且用 `has_request_context()` 判，不是判 import 成功），再判 Flask；
读请求对象的每一步都单独兜异常，`view` 自己也兜一层——视图是直接对着群成员的浏览器
的，抛出去就是一个 500 错误页，比「链接已失效」更让人摸不着头脑。

**群管身份：发送者在 `message_obj.sender` 上，不在事件上**

23 轮修好了「拿发送者 ID 比 `group_owner` / `group_admins`」，但**发送者本身**是从
`getattr(event, "sender")` 拿的——`AstrMessageEvent` 根本没有 `sender` 属性，拿到
None 就直接放弃。所以那个修法一行都没生效：群主发 `/backup now` 依旧被挡。

同时 `sender.role` 写成 dict（`event.sender = {"role": "admin"}`）时也读不到。
现在两级都走 `_call_or_value`（`getattr` + 可调用就调），dict 形态单独认。

**同一条通知被数两遍退群**

AstrBot 的处理器分发是广播式的：`on_message` 和 `on_notice` 都挂在
`EventMessageType.ALL` 上，同一条 notice 会依次进两个函数。解散/被踢有冷却窗口挡着，
退群聚合却是**按次数累加**的——一次退群被数两遍，阈值 10 的群 5 个人就触发备份。

现在按事件指纹（`message_id` + 群号 + 类型，**不含正文**）在 120 秒窗口内去重，
先到的那个 handler 处理，后到的直接跳过。

## 第 25 轮：去找「谁在送事件、平台从哪来、地址在哪一层」

前 24 轮盯着 `message_obj` 的字段名和 `sub_type` 的取值——这两处这次核完是对的
（`Group.group_id` 是字符串、`group_owner` / `group_admins` 在 `Group` 上、
OneBot 的 `group_decrease` + `leave` / `kick_me` 与代码里的集合一致）。但我漏了
三个更上游的问题：**谁把事件送进钩子**、**平台实例从哪来**、**媒体地址放在哪一层**。

**`platform_insts[0]` 永远是 webchat**

`PlatformManager.load_webchat_platform` 在加载真实 IM 适配器**之前**就把
`WebChatAdapter` 塞进 `platform_insts`。而 `_platform_by` 在「没给 key」时是
`return insts[0]`——定时备份、以及任何走全局找平台的路径，拿到的是 webchat。
webchat 没有群历史、没有成员列表，`get_user_info` 一类动作直接抛，备份一条也拉不到，
日志里只有一句「当前没有可用的平台适配器」。

现在没给 key 时按「有没有客户端」挑：`aiocqhttp` 的 `CQHttp`、telegram 的 `client`
都有，webchat 没有；都有或有都没时，再按平台类型（`aiocqhttp` / `telegram` /
`discord` / `slack` / `wecom`）挑。纯 webchat 部署退回第一个，不抛异常。

**`Image.url` 才是地址，`data` 只是 OneBot 那一层**

AstrBot 的 `Image` / `Record` / `File` 把地址放在**段上**：`url`、`file`、`path`。
`data` 是 OneBot 原始 dict 才有的一层。解析器先 `data = _safe_get(seg, "data", seg)`，
段上没有 `data` 时退化成段自己没问题；但段上要是也带了个 `data`（有些适配器把原始
payload 塞在里头），`url` 就只在段上、`data` 里没有——`_media_url` 从 `data` 里找，
返回空串。

后果是图片/语音的 `media_paths` 一条都没有，导出里全是断图；纯图消息还会因为
「没有内容也没有媒体」被判定成空消息直接丢弃。现在三个来源依次试：`seg` 优先，
`data` 兜底。

**通知事件不该进消息库**

`aiocqhttp` 的 `_convert_handle_notice_event` 把 notice 包成 `OTHER_MESSAGE` 的普通
消息事件投进管道（`message` 空链、`message_str` 空串、原始事件在 `raw_message`）。
`on_message` 收到的就是这样一条「消息」。

实测这一轮的形状下它解析不出内容，`archive_message` 本来就会放弃。但那是巧合：
解析器对未知段的兜底是「记 TEXT、内容能抠就抠」，任何一次退化都会凭空多出一条
「消息」，内容还是发送者 QQ 号。备份库里的消息必须是真人说的话，所以在
`archive_message` 里显式拦一层：`raw_message.post_type` 是 `notice` / `request` /
`meta_event` 就不进库（`parser.is_event_payload`）。

**`route == f"/{subpath}"` 只认固定路径**

宿主比的是字符串相等：`/api/plug/backup/download/<token>` 进来时 `subpath` 是
`backup/download/<token>`，等于 `/<token>` 的 route 永远比不中。真正会命中的是
`"/backup/download"` 那条（视图自己从 request 里取 token）。所以登记顺序改成
固定前缀在前——顺序错了不影响功能，但会影响「哪条先被找到」的直觉。

## 第二十六轮：时间戳不许凭空造，overwrite 的判据

**缺时间戳不能拿「现在」顶上**

`_to_datetime` 原来在「值为 None / 解析失败」时回落 `datetime.now()`，`timestamp: 0`
则落成 1970-01-01。三件事叠在一起：

- 同一批消息两次解析出两个时间戳。校验和按 `(timestamp, message_id)` 排序算，
  同一份数据就有两个指纹——数据一个字节没坏，比对却假阳性；
- `/backup export` 按日期分片，半年前的消息被归进今天那个文件；
- `timestamp: 0` 让消息排到整库最前，日期分组里多出一个 1970-01-01 的分区。

现在缺失、空串、0、负数一律返回 `None`，`parse_message` 落到 `datetime.min`，
意思是「这一条不知道什么时候发的」。`datetime.min` 上 `timestamp()` 会抛
「year 0 is out of range」，生成兜底 message_id 时单独兜了 0。

成员 `join_time`、公告 `published_at`、文件 `upload_time` 同一条路径，都是可空列，
传 `None` 没问题——只是不再有「这个人今天入群」这种假事实。

**overwrite 的判据用「归谁」，不是「在不在」**

`message_exists` 按唯一键 (message_id, group_id) 查，而 `overwrite_message` 的 WHERE
也是这两个字段——两者口径本来就一致，`existed` 换成 `message_backup_id` 是为了让
下面的 `elif` 能看见「行在、但 `backup_id` 是空串」的情况（老库升级、
`assign_message_backup` 摘过归属）。那种行按存在性算是「有」，按归属算是「没有」，
分支走错就会既不覆盖也不补归属。

写这两条时我一度以为旧代码「先写后校验」「覆盖不换归属」，去改了 `restore()` 的
顺序和 overwrite 分支——实测都不是：`_check_content` 早就在第一次写入之前，
`overwrite_message` 带 `backup_id` 时也会连归属一起改。这两处改动回滚了，
只留下用例把语义钉住（`test_round26_restore_order_and_timestamp.py`）。

## 红线对照

| 红线 | 落地位置 |
| --- | --- |
| 日志不打印消息内容 | 全仓日志只打 message_id / 群号 / 异常类型 |
| 媒体失败不阻塞 | `BackupEngine._download_media` 内 try/except，计失败数 |
| 备份不在 Web 目录 | `storage/exports`，靠 token 转发 |
| 链接带 token 且过期 | `ExportManager._issue_token` / `resolve_token`；兑换入口是 `core/gateway.py`（宿主面板挂不通，见上文） |
| 恢复二次确认 + 校验 | `RestoreEngine.restore(confirm=True)` + `verify_backup` + 本机 `export_evidence` 快照比对 |
| 大群分页 | `PAGE_SIZE = 50` 循环拉取 |
| 超时兜底 | aiohttp `ClientTimeout`、APscheduler `misfire_grace_time` |
| pathlib 路径 | 全部用 `Path`；`safe_filename` 剥离目录部分 |
| SVG 素材自产 | `assets/*.svg`、`templates/chat.html` 内联 SVG |
| 作者信息 | `metadata.yaml`、`README.md`、HTML 导出页页脚 |

**探查日志不许带值**

`describe_event` / `describe_notice` 是「第一次收到真实事件时打一行字段清单」，用来
对齐适配器字段名。`_get` 是 `getattr`，读 `message_obj` / `message` / `group` 会把整个
消息对象捞出来——日志里出现对象 repr，就等于把正文写进了日志（红线 1）。
现在只判断「有没有」，绝不打印值。

## round27：`_call` 只认一种参数名，整条拉取路径静默失败

`PlatformAdapter._call` 原来的策略是「按名字逐个调，TypeError 就换下一个名字」。
它默认适配器的参数名也叫 `group_id`——而 OneBot 那套（以及本仓库测试替身之外的
绝大多数写法）叫 `gid`。于是：

```
引擎送:  get_group_history(group_id=100, count=50, offset=0)
适配器:  async def get_group_history(self, gid, count=50, offset=0)
         → TypeError: unexpected keyword argument 'group_id'
         → 换下一个名字 → 名字根本不存在 → return None
         → 引擎当作「空页」→ 备份报 completed、0 条
```

**为什么用例一直是绿的**：替身的签名和引擎对齐，测的是「平台返回什么」，
从来没验证过「参数怎么送进去」。这是本仓库第七次撞到同一类问题——
**用一套自己的假设去验证自己的代码**。

修法分三层：

1. `_adapt_kwargs` 先按签名重映射参数名（`group_id` ↔ `gid` / `group` / `chat_id`），
   对不上就不硬送。这条让「参数名不同」不再等于「调不通」。
2. `_fetch_page` 在「两条路都拿不到东西」时置 `adapter.history_broken`，
   引擎收尾时把状态从 `completed` 降成 `failed` 并写清原因。
   **空群和「接口根本用不了」在返回值上都是空列表**，不区分就是骗人。
3. 参数名对不上的调用会建出一个永不 await 的 coroutine（`coroutine was never
   awaited`）。探测失败要 `close()` 掉，别把半成品协程堆在事件循环上。

## round28：真 AstrBot 跑一遍接线，四个钩子全是哑的

前二十七轮用的都是自己写的替身：假事件、假注册器、假 `filter`。这一轮把 AstrBot
4.14.6 的源码装进来（`pip install -e <源码>`），用真 `Star` / `Context` /
`star_handlers_registry` / `WakingCheckStage` / `StarRequestSubStage` 跑了一遍。
只保留一件假事：OneBot 客户端——它就是平台本身。

**1. `on_notice` 少了 `event`，插件在真实加载器下装不上**

```python
@filter.command("backup")               # 这一行把 handler 塞进注册表
async def backup(self, event): ...

@filter.event_message_type(ALL)         # 这一行只加 filter，不注册
async def on_notice(self):              # 少一个参数
```

`filter.command` 不是「修饰命令」，它是**注册入口**：AstrBot 只认
`star_handlers_registry`，而往里面塞东西的只有 register 系列装饰器。它顺带
`inspect.signature(handler)` 并把前两个参数当 `self` / `event` 跳过——所以
两个钩子的签名必须够长。

**2. 两个钩子只挂 `event_message_type`，从来没被调用过**

`WakingCheckStage` 对「`event_filters` 为空」的 handler 直接 `continue`。
只有 `EventMessageTypeFilter` 的钩子照样进注册表、照样在插件详情里显示，
`is_wake` 也不会因为 `ALL` 变成无条件——实测普通群消息一个 handler 都不激活。

修法：两个钩子都加一个 `@filter.command("__backup_archive")` /
`@filter.command("__backup_notice")` 当**注册位**。命令名用双下划线开头，
用户敲不出来，只负责把函数登记进注册表。

**3. 真 pipeline 会把同一条 notice 送进两个钩子，退群被数两遍**

`star_request` 遍历 `activated_handlers`，两个钩子都挂着 `ALL`，所以一条退群
通知必然进两次。旧代码靠 `EventHooks.seen_notice(event)` 挡——而它用的是**事件**
指纹，认不出群号时会自己放开（`if not key or key == "group=None|type=": return False`）。
认不出群，闸就整道失效。

修法：退群聚合的锁挪到接线层（`BackupPlugin._notice_seen_in_group`），用**已知
群号**拼指纹。`EventHooks.handle_notice` 的返回值从三个加到四个，第四个是
「这次数过一个人了」——接线层据此判断要不要再往下走。

**4. 面板注入的配置形状和 `config.yaml` 不一样**

`AstrBotConfig` 按 `_conf_schema.json` 摊平，`storage_dir` 在顶层、`scope` 是
一层 dict；`config.yaml` 外面还包了一层 `backup:`。`_load_config` 的优先顺序
（注入 > yaml > schema 默认值）已经对，但缺一条断言：**注入的每一层都得是
dict，且 `config.yaml` 里的每个键在注入那份里都有**。两边形状漂移时，面板改了
不生效，而日志里一句提示都没有。

**这一轮的教训**

「核了源码」不等于「跑过」。前面第 20 轮我核过 `ComponentType`、第 24 轮核过
Quart，结论都对；但「`filter.command` 到底会不会注册 handler」「`CommandFilter`
会不会 inspect 签名」这两条，只读源码能看出疑点，只有**真装一遍**才能确定是
`TypeError` 还是「静默不调用」。区别很大：

- `TypeError` → 插件加载失败，用户看得见（但只看得见「插件加载出错」）
- 静默不调用 → 功能写着、文档写着、单测绿着，**一次都没运行过**

所以 `test_round28_real_astrbot_loader.py` 是这批用例里唯一不打桩的那个文件，
没装 AstrBot 时整组 skip——不是「不用验」，是「这个环境验不了」。

---

## 面板路由：新旧两套形状，都不能写死

AstrBot 换过一次面板框架，插件路由的挂载方式和请求对象的来源全变了。这段记下
差异，免得下次又拿一套假设去验代码。

**旧面板（Quart）**

- 挂载：`app.add_url_rule("/api/plug/<path:subpath>")`，匹配时 `route == f"/{subpath}"`，
  所以 route 里**不能**有插件名；
- 请求对象：Flask/Quart 的 `request` 代理，`request.args` 取 query；
- 返回：Flask 认 `Response`（`werkzeug.wrappers.Response`）；
- 鉴权：`auth_middleware` 有一批前缀白名单，`/api/backup/download` 在里头，群里点开
  不需要登录。

**新面板（FastAPI）**

- 挂载：`/api/v1/plugins/extensions/{plugin_path:path}`，`_match_registered_web_api`
  把 `<插件名>/` 那层**剥掉**再拿尾巴比 route——所以 route **必须**以插件名开头；
- 请求对象：不在 Quart/Flask 上下文里，自己绑一个 `astrbot.api.web.request` 的
  contextvar 代理（`PluginRequest`），query 在 `request.query` 而不是 `.args`；
- 返回：`_coerce_view_result` 只认 `Response` / tuple / dict。**自定义对象原样往下
  传**，FastAPI 再把它当响应体序列化——状态码、`Content-Type` 全丢。
- 鉴权：没有那条白名单，`/api/v1/plugins/extensions/*` 要面板 JWT 或 API Key。

**踩过的三个坑**（都是「HTTP 200，但下不到东西」这种最难查的形状）

1. route 只写 `/backup/download`：新面板上 `_match_registered_web_api` 永远命中不了，
   回 `{"status":"error","message":"未找到该路由"}` —— 状态码 200。
2. 视图返回 Werkzeug `Response`：`_coerce_view_result` 不认，包成 JSON，下载下来的
   文件内容是 `{"headers":{...},"_status":"200 OK",...}`。
3. `request.args` 取 token：新面板只有 `.query`，只有「路径里带 token」那条能活，
   固定前缀 + query token 那条每次都 404。

**所以现在怎么接**

- 两套形状**都登记**（`DOWNLOAD_ROUTES`），宿主各自取认得的那条；
- 视图按「当前请求是谁给的」挑返回形态：AstrBot 代理 → `StreamingResponse`；
  否则 Flask `Response`；再否则 Quart `Response`；
- 链接前缀**不猜**，问宿主：`_astrbot_extensions_mount()` 看它有没有
  `plugins/extensions` 那条路由。猜错一边，另一边点开就是 404。

验证方式也换了：`test_round29_fastapi_dashboard.py` 装真 FastAPI，用面板自己的
`_call_plugin_extension` + `PluginRequest` 上下文跑真请求（`httpx.ASGITransport`），
断言的是 content-type 和字节数。修之前同一组请求全部是 `200 application/json`。

---

## 第 31 轮：翻页链路只有真 HTTP 才算数

前面所有的分页用例，喂进去的都是我自己写的假客户端：签名、返回形状、消息朝向、
分页语义，全是我按「应该长这样」定的。这一轮起了个真 aiohttp 服务，
客户端走真 HTTP + 真 JSON，只把平台当黑盒。三处叠在一起，表现是同一个：
**备份报 `completed`，120 条的群只存了几十条。**

### 1. `{"messages": [...]}` 这层外壳没拆开

`_fetch_page` 里写着「`result` 是 dict 就取 `.get("messages")`」——可那条分支
**永远走不到**：`_call` 在 return 之前只判 `asyncio.iscoroutine`，dict 不是协程，
直接原样交回。调用方 `list(result or [])` 于是拿到**键名** `["messages"]`：
一个 dict 被当成「一页 1 条消息」。

后果比「少一条」严重得多。`_raw_id("messages")` 退化成同一个指纹，翻页判定的
「这一页是不是又全是翻过的」永远为真——引擎两次调用之后就认为到底了。

现在 `_unwrap_messages` 认三种外壳（`messages` / `data` / `data.messages`），
拆不出来（比如平台回 `{"error": ...}`）返回 `None` 并置 `history_broken`，
收尾降 `failed`。**`{}` 例外**：那是「这个群没有文件/公告」的正常回执，等于空列表。

### 2. 锚点取成了「这一页最新那条」

`_oldest_id` 默认「第一个元素就是最旧的」。OneBot 的 `get_group_msg_history`
返回的是**新→旧**，于是锚点拿到这一页最新那条，而 `message_seq` 的语义是
「从这条往回数 count 条」——下一页原地再取一遍同样的消息。

现在先按时间戳 / message_id 比出朝向（`_detect_newest_first`），有锚点之后
`_fetch_page` 不再同时发 `offset`：OneBot 里 `count` + `message_seq` 和
`offset` 是两种互斥语义，一起发出去行为随实现而变。

顺带发现补齐循环的一个死结：`offset += len(fresh)` 里的 `fresh` 是「本页里还
没见过的」，第二页之后它常常等于整页，但锚点没推进时它不动——于是 `offset`
原地踏步。改成按 `len(page)` 推进。

### 3. 每页给得比 `PAGE_SIZE` 少就收工

引擎用 `len(batch) < PAGE_SIZE` 判到底。可适配器不认 `count`、每页固定给 20 条
时，第一页 20 < 50 就退出了——**补齐逻辑就是为了对付这种平台，判据用错等于没补**。

现在分两种：有锚点时「给得比 `want` 少」才算到底，没锚点的 offset 翻页仍按
半页判（平台可能只是不认 count）。收工兜底始终是「翻过的消息集合」，平台既不认
count 也不认 offset 时靠它停下来。

### 验证

`test_round31_http_paging.py`（7 条）：真 aiohttp 服务、真 JSON、真 HTTP。
其中 `test_full_backup_over_real_http_reads_every_message` 断言 120 条一条不少，
且锚点序列无重复；`test_newest_first_and_oldest_first_both_read_everything`
把同一批数据正着反着各跑一遍。旧代码上 `{"error"}` 那条断言失败——它报的是
`completed`。

一条方法论上的事：这轮的三处都不在「我写的逻辑」里，而在「我以为平台会怎么回」
里。假客户端替我省掉的从来不是网络，是**平台实际怎么说话**。

---

## 第 32 轮：装上真 AstrBot，用真 pipeline 跑一遍

前 31 轮的宿主全是假的：假 `Context`、假注册器、假事件、假请求。这一轮把
`astrbot==4.14.6` 装进来，在 `data/plugins/backup` 布局下用**真的**
`PluginManager.load()` 加载，再用**真的** `WakingCheckStage` +
`StarRequestSubStage` 发命令、fire 事件。

四条，全部是「功能写着、用户永远走不到」。

### 1. `download_link` 收了 `request`，一次都没用过

```python
def download_link(self, token: str, request=None) -> str:   # request 在函数体里出现 1 次
    ...
    if self.gateway.running:
        return self.gateway.url_for(token, explicit)         # 回环地址
```

`_public_base_url` 里那套「反代后面真实域名在 `X-Forwarded-Host` 里」的推断写得
很完整，**却没有任何调用点**（全仓 grep 只命中它自己的定义和它自己的用例）。

于是反代 / 内网穿透部署下（配置里通常没有任何公网地址），发进群的永远是
`http://127.0.0.1:<端口>/...`——群成员点的是**他自己电脑**的 127.0.0.1，一辈子
点不开，而回执上看起来像条正常链接。

现在顺序是：`export.base_url` → **这条命令从哪个域名进来** → 网关回环地址。
请求头从两个地方取：真请求的 `.headers`，以及事件上的
`request_headers` / `message_obj.raw_message["headers"]`（`/backup export` 手里
是事件，不是 request）。内网与本机地址（`localhost` / `127.` / `192.168.` /
`10.` / `172.16-31.`）一律不认。

`test_round22` 里原有那条断言把错的行为钉死了——它要求带 `Host: bot.example.com`
时链接必须是 `http://127.0.0.1:<端口>`。改成按新语义验证，并补了反代头、多跳
列表、内网地址、显式 `base_url` 优先级四条。

### 2. `_adapt_kwargs` 见 `**kwargs` 就原样透传

真适配器最常见的写法是：

```python
async def get_group_history(self, gid, count=50, offset=0, **kw): ...
```

`group_id=` 落进 `**kw`，显式参数 `gid` 永远缺参 → 每次调用 `TypeError` →
`_call` 换下一个名字 → 下一个名字根本不存在 → 返回 `None` → 备份报
`failed`、0 条。

现在别名映射照做，`**kwargs` 只负责兜「映射不到、但对方也不明着拒绝」的额外
参数（比如 `message_seq`）。不收 `**kwargs` 的实现仍然拒绝多余参数。

### 3. 锚点把 `message_id` 当 `message_seq` 回传

`_raw_id` 优先取 `message_id`（OneBot 里常是 `-1234567890` 或带前缀的串），
`_oldest_id` 直接把它当锚点喂回平台的 `message_seq` 参数——平台侧
`int('m99')` 抛 `ValueError`，异常一路冒出引擎，整次备份判 `failed`：

```
❌ 备份 增量结束 | 消息 0 条 ... 备注：ValueError: invalid literal for int() with base 10: 'm81'
```

现在 `_seq_of` 依次取 `message_seq` → `message_id` → `id`：消息上同时有两个
字段时必须取 `message_seq`；只有 `message_id` 时才用它（部分私有实现只有这一个
ID，而它确实能当锚点）。

### 4. `/backup restore <包> <群号>` 报「备份不存在」

```python
if target is None and _looks_like_archive(backup_id):   # ← 两头落空
```

- `target` 有值（`987654`），第一个条件不成立；
- `len(tokens) > 2` 也不成立（只有两个参数）。

于是 `archive` 留在 `None`，**包路径被当成备份 ID 去查库**，报
「备份 …zip 不存在（本机没有这条记录，也没有给备份包）」——而那个包就躺在
回执给的那个路径上。这是换机器恢复时**最常写的那条**命令。

现在只看第一个参数的形状。

### 验证

- `test_round32_kwargs_alias.py`（5 条）：`gid` + `**kwargs` 的签名映射，
  以及端到端「备份真的拉到消息而不是报 failed」
- `test_round32_host_pipeline.py`（13 条）：真 AstrBot 实例、真网关

两文件 18 条里，**旧代码上 8 条失败**。

外加一个不写成用例的端到端：真 `PluginManager` 加载 → 真 pipeline 发
`/backup now`（10 条）→ `/backup export 100 zip`（回执链接是 `https://bot.example.com/...`）
→ 真 HTTP 200 下到 13499 字节、与盘上文件 SHA-256 一致 → 同一链接再点 410 →
`/backup restore <包> 987654 --confirm` 写入 100 条。

### 一条方法论

前 31 轮我反复说「只能实机确认」。这一轮证明那句话是借口的一半：**依赖是
开源的，装一下就有**。`pip install astrbot` 之后，`PluginManager` / `WakingCheckStage`
/ `StarRequestSubStage` 全是可执行的真相，比读源码强、比想象强。

但这轮四条里，**三条不在「字段名对不对」上，而在「谁调用谁」上**：
`request` 传了没用、`archive` 赋值条件写错、`_public_base_url` 没有调用点。
这些读源码看不出来——源码每一段都对自己的，缝在拼接处。

---

## 第 33 轮：三个钩子功能其实一次都没跑过

这一轮没加功能，只问了一个问题：**那两个事件钩子，到底有没有被调用过？**
用真 AstrBot 4.14.6 的 `WakingCheckStage` + `StarRequestSubStage` 跑下来，
答案是「没有」，而且不止一处原因。

### 1. 装饰器顺序把钩子堵死了

`main.py` 里两个钩子挂了三层装饰器，最初是这样写的：

```python
@filter.command("__backup_archive")        # 登记位
@filter.custom_filter(_hook_filter(...))   # 认领过滤器
@filter.event_message_type(ALL)
async def on_message(self, event): ...
```

装饰器是**自下向上**应用的，所以 `custom_filter` 拿到的是 `command` 刚刚建出来的
`handler_md`；而 `command` 在 `init_handler_md` 里 `inspect.signature` 了被包装的
函数——那时它还是裸函数，参数是 `(self, event)`，跳过前两个后 `handler_params`
为空。看着没问题。

问题在 `CommandFilter` 本身：它留在 `event_filters` 里，`filter()` 是

```python
message_str.startswith("__backup_archive")   # 真实聊天里永远不成立
```

`WakingCheckStage` 对 `event_filters` 是 AND 关系，一条不通过整条事件就被判掉。
于是「登记位」直接变成了「否决票」：实时归档、退群聚合、解散/被踢备份三个功能
代码在、文档在、用例绿，**一次都没执行过**。

现在的写法：

- 钩子上**不挂** `CommandFilter`（只挂 `event_message_type` + 认领过滤器）；
- 钩子函数自己置 `event.is_at_or_wake_command = True` / `event.is_wake = True`：
  `WakingCheckStage` 只把这两个标记为真的 handler 交给 `StarRequestSubStage`，
  而钩子是后台任务，本来就不该靠「有人 @ 机器人」来唤醒；
- 注册表那边仍要能查到 handler——`event_filters` 非空即可（空 filter 会被
  `WakingCheckStage` 直接 `continue`）。

### 2. 认领过滤器不能带注解，也不能传实例

`@filter.custom_filter` 对**裸指令**做的事是 `custom_filter(raise_error)`——
它**调用**传进去的对象。所以：

- 传类 → 得到实例，正常；
- 传实例 → `TypeError: object is not callable`，插件加载失败；
- 传工厂函数 → 得到工厂函数自己的返回值（一个普通函数），而调用处写的是
  `filter.filter(event, cfg)`，每条事件 `AttributeError`。

另外，`CommandFilter.init_handler_md` 会在 `@filter.command` 那一步
`inspect.signature` 并把**第三个参数之后的**都当「要注入的参数」。过滤器类上写
`def filter(self, event, _config=None) -> bool:` 时，`event_filters` 里前两个
filter 是事件类型和认领过滤器，第三个正好是 `CommandFilter`——它 inspect 的是
被包装的函数，不受影响；但如果 `custom_filter` 排在 `command` **上面**，
inspect 到的是过滤器自己的 `filter` 方法，`-> bool` 就成了「必填参数」，
`validate_and_convert_params` 抛 `ValueError: 必要参数缺失`，整条事件被终止。

结论：**过滤器的 `filter` 方法不写注解**，`custom_filter` 永远排在 `command`
下面。

### 3. `raw_message` 被当成消息本体

`archive_message` → `parse_event_message` 会拿
`event.message_obj or event.raw_message` 去 `parse_message`。

AstrBot 的 `message_obj` 是 `AstrBotMessage`，`raw_message` 属性是 **OneBot 原始
事件 dict**——`{"post_type": "message", "message_id": "xxx", "raw_message": "真正的那句话", ...}`。
它有自己的 `message_id`、`group_id`、`timestamp`、`sender`，看起来就是一条消息；
直接丢进 `parse_message` 会走到 `_segments` 的兜底分支 `return [raw]`，整份事件
被当成「一段」，`content` 扣出来的是一串既不像消息 ID、也不像正文的东西。

后果不是「多一条脏数据」：**真正文、媒体、引用全丢**，而实时归档正是「群被炸时
最后那几条」的唯一来源。

现在 `parse_event_message` 先用 `is_event_payload` 判一层：事件 → 不当消息用，
回退到 `message_str`。

### 4. 验证方式

`test_round32_hooks_never_ran.py`（5 条）+ `test_round28_real_astrbot_loader.py`
里改写的 3 条，都跑真 stage：

- 普通群消息 → `on_message` 被激活 → 库里真有这条；
- 一条退群通知 → 只有 `on_notice` 被激活 → 窗口人数 +1（不是 +2）；
- 9 个人退群不触发、第 10 个才触发一次备份；
- 通知事件不被当成消息入库；
- `raw_message` 是事件时不顶替消息本体。

旧代码上这组里 3 条是红的。

### 一句总结

前几轮我一直在验「钩子里的逻辑对不对」，没人验「钩子有没有被调用」。
**注册表里有名字 ≠ 会被调用；装饰器写上 ≠ 生效；用例绿 ≠ 跑过。**

### 补记：为什么不再用 `@filter.command` 当登记位

前几轮的做法是给钩子挂 `@filter.command("__backup_archive")`，理由是
「`star_manager` 只认注册表，而往注册表里塞东西的只有 register 系列装饰器」。
前半句对，后半句是我自己推出来的：`@filter.event_message_type` 也会
`get_handler_or_create` 并塞进注册表。两个装饰器都能让 handler 出现在注册表里，
区别只在 `event_filters` 里多不多一条 `CommandFilter`——而那条 `CommandFilter`
恰恰是钩子的死刑判决书。

所以现在钩子上只有 `event_message_type` + 认领过滤器，不带任何命令语义。

## 第 34 轮：一条没人点得动的链接，和一句不成立的承诺

这轮的起点不是读代码，是**在干净环境里跑一遍 `pytest`**：先装 `pytest` 再跑，
发现整轮在**收集阶段**就中断了——一条用例都没执行。顺着往下，又撞出两处
「回执上写着、代码里不成立」的东西。

### 1. 没有 AstrBot 的环境里，`pytest` 直接退出

```
ERROR backup/tests/test_round28_real_astrbot_loader.py
ERROR backup/tests/test_round32_hooks_never_ran.py
!!! Interrupted: 2 errors during collection !!!
```

两个文件在**模块顶层** `import astrbot.api.event`，而模块级 `skipif` 是在那之后
才生效的——import 先炸，skip 没机会说话。

这件事真正难看的不是这两个文件，是**判据本身**：`real_astrbot_available()` 当时
只判 `find_spec("astrbot") is not None`，那是「包在不在」，不是「能不能用」。

- 装了 astrbot 但它的依赖不全 → `find_spec` 为真、import 抛；
- 前面跑过 `install_fake_astrbot`（往 `sys.modules` 塞替身）→ `find_spec` 又为真。

三种不一致叠在一起，就有三种炸法：

```
没有 astrbot              → 收集阶段 ModuleNotFoundError，整轮中断
装了但依赖不全            → 同样中断
替身先装、真货后来        → astrbot 是真的、astrbot.core 是替身，
                            `cannot import name 'astrbot_config'`
```

现在：

- 判据改成「**import 得动吗**」：`astrbot.api.event` + `astrbot.core.star.star_handler`
  都能拉起来，且模块上没打替身标记，才算这个环境验得了；
- 两个文件把那句顶层 import 挪进 `if real_astrbot_available():` 里；
- FastAPI 面板那份（`test_round29`）的判据也从 `find_spec("astrbot.api.web")`
  改成真 import——父包 import 失败时 `find_spec` 会把子模块的异常抛出来；
- `conftest` 里补 `preload_host_for_lineage()`：走「替身契约」的那几个文件
  （round20–25）现在统一走它，有真货用真货、没有才装替身；装之前先把
  `sys.modules` 里的替身拆干净。替身里也补了 `astrbot.core.star.filter.custom_filter`
  ——`main.py` 那句 `from astrbot.core...` 原本在「装了 astrbot 但依赖不全」的
  机器上必炸，因为替身只铺了 `astrbot.api` 那一层。

### 2. 回执里的链接永远是 `127.0.0.1`

```python
if gateway.running:
    return gateway.url_for(token, "")     # → http://127.0.0.1:<端口>/...
```

第 32 轮修过一次「`request` 收了没用」，但只修了**没有网关**那条分支。网关一起
（默认就起），链接又变回回环地址。而回执文案写的是「临时链接（24:00 过期，仅可
使用一次）」，读的人以为能直接转进群。

群成员点开的是什么？**他自己电脑的 127.0.0.1**。一辈子点不开，而回执看起来一切正常。

更难看的是：第 30 轮那两条用例**亲手把这个 bug 锁成了正确答案**——
`assert link.startswith("http://127.0.0.1:{gateway.bound_port}/")`。用例和代码共用
一个错误假设，于是全绿。

现在：`export.base_url` → 这条命令进来的域名（`Host` / `X-Forwarded-Host`）→
**裸路径 + 明说点不动**。回环地址只留在 `gateway.url_for()`（本机自检用），不再
进回执。用例改成「回执里不许出现 `http://127.0.0.1`」。

### 3. 「仅可使用一次」是一次空话

token 的作废判定原来是「整份取走就删」：

```python
partial = bool(range_header)
target = exports.resolve_token(token, shared=partial)   # 片段请求 → 不删
```

浏览器下载大文件一定先发 `Range: bytes=0-` 探测、再发续传，**两次都是片段请求，
两次都不删 token**。于是链接转发进另一个群，第二个人照样能整份下载。上一版注释
把这件事写成「只有整份取走才作废」，读起来像设计，实际是「谁都没删」。

「片段请求不能删 token」这个理由是**对的**（删了探测就成了唯一一次，用户看到
「下了一半断了」），但解法错了：判据不该是「这个请求是整份还是片段」，而该是
「**是不是第一个人在开头**」。

现在 `download_tokens` 多一列 `opened_at`：第一次兑换打时间戳，之后的请求必须在
`GRACE_SECONDS`（15 分钟）窗口内才算同一次下载的后续片段，超窗即是第二个人，410。
老库有 `_migrate_token_columns` 补列——这张表的 token 有效期 24 小时，升级那一刻
必然还有活的，没这列就是每次下载 500。

### 4. 验证方式

`test_round33_link_and_once.py`（8 条）：

- Range 探测不烧 token、续传 206、整份一次就 410；
- **第二个人拿着转发链接来领 → 410**（旧代码上这条是红的）；
- 慢链接：探测 + 续传同窗共享，跨窗即第二个人；
- 老库 `download_tokens` 无 `opened_at` 列时自动迁移；
- 真 HTTP 端到端：探测 → 续传 → 整份 → 再点 410；
- 回执里不出现回环地址、且点不动时说出来。

三处旧断言按新语义改写（`round22` / `round30` / `round32`），每条都写了「原来那条
断言本身才是 bug」的理由，不是为了让用例变绿而改。

### 一句总结

这轮三处都不是「逻辑写错」，是**承诺与实现相反**：README 说「点得动」，回执说
「仅一次」，代码两样都没做到。而且两处都有一条用例把错误答案写成断言——**用例绿
不等于承诺成立，它只等于「代码和用例的假设一致」**。

---

## 第 35 轮：钩子把机器人变成了话痨

验证方式还是第 32 轮那套：装真 `astrbot==4.14.6`，跑真 `WakingCheckStage` /
`StarRequestSubStage` / `ProcessStage` / `RespondStage`，只把平台出口换成一个
记账用的替身。这轮撞出来的东西有一个共同点：**它不是新功能的 bug，是上一轮
修法的副作用**。

### 1. 每一条群消息都换来一句 LLM 回复

第 32 轮为了让钩子被调用，在 `on_message` / `on_notice` 里写了：

```python
event.is_at_or_wake_command = True
```

这个标记的语义是宿主的、不是插件的。`ProcessStage` 的逻辑是：

```python
if activated_handlers:
    async for resp in self.star_request_sub_stage.process(event):
        yield
# 跑完所有 handler 之后，再看这个
if (
    not event._has_send_oper
    and event.is_at_or_wake_command     # ← 钩子刚置的
    and not event.call_llm
):
    async for _ in self.agent_sub_stage.process(event):
        yield                            # ← 默认 LLM 链路
```

于是：群里每来一句闲聊 → 钩子归档 → `ProcessStage` 收尾时判定「被唤醒了，回一句」
→ `AgentRequestSubStage` 真的去问模型 → `RespondStage` 把答案发进群。
**备份插件成了全群最话痨的成员。**

退群、撤回、戳一戳这些通知更难看：宿主把 notice 也包成事件丢进管道，置真之后
机器人会对着一条**退群通知**现编一段话发进群。

为什么本地试不出来：`provider_settings.enable` 关掉时这条链路不跑。真部署里它
默认是开的。

修法：钩子自己把默认 LLM 链路关掉。

```python
event.should_call_llm(False)
```

这是宿主为「置真了唤醒标记但不想被 AI 接管」留的口子，语义很死：只挡默认 LLM
请求，不挡插件自己的调用。

**不能回滚 `is_at_or_wake_command`**：回滚了钩子就进不了 `ProcessStage`，第 32 轮
那个「钩子从来没被调用过」原样还回来。两个标记必须一真一关。

### 2. 「只写了归档，不回复任何内容」这句 docstring 一直是假的

`on_message` 的注释从第一版就写着「只写库不下载媒体，也不回复任何内容——群聊里
静默干活」。第 32 轮之前它确实不回复（因为压根没被调用）；第 32 轮之后它被调用了，
于是每句话都回。**注释是对的，代码不是**，中间隔了两轮没人发现。

这也是这轮真正该记住的一条：第 32 轮把「钩子没被调用」修成「钩子被调用」，
验收标准只有「`activated` 里有没有 `on_message`」。**「调用成功」不等于「行为正确」**
——钩子跑起来了，副作用没人看。

### 3. 验证方式

`test_round34_default_llm.py`（7 条，旧代码上 4 条红）：

- 一条群消息跑完真 pipeline：`event.call_llm` 必须是 `False`（钩子关掉了默认
  LLM），且消息仍然在库里（归档照旧）；
- 一条 `group_recall` 通知：同上；
- 反方向：`/backup help` 这种命令仍然被唤醒（钩子只是把标记又置了一遍，不该
  把命令的唤醒也弄丢）；
- 契约：`on_message` / `on_notice` 的源码里必须出现 `_silence_default_llm`，
  且那个 helper 调的是 `should_call_llm(False)`；
- 老宿主兼容：事件对象上没有 `should_call_llm` 时不抛（「禁不掉」不是「出错」）。

第一版测试写成「`agent_sub_stage` 那个替身有没有被调用」。这个断言**骗人**：
`ProcessStage` 是洋葱模型，钩子把 `call_llm` 关掉之后，`StarRequestSubStage`
收尾时还会动一次事件状态，中间的调用顺序和最终落在 event 上的值不是同一件事。
后来改成直接断言 `event.call_llm` —— 那是钩子唯一真正改动的、也是宿主唯一真正
读取的那个位。**断言要落在「宿主的判据」上，不是「我搭的替身被调了几次」。**

### 4. 一句总结

第 32 轮修「钩子没被调用」，第 33 轮修「链接点不动」，第 34 轮修「一次性不成立」，
这一轮的起点是「**上一轮的修法有没有留下副作用**」。三条修法本身都对，但每一条都
只验了「目标行为出现了」，没验「别的东西有没有跟着变」。宿主那一层是别人写的，
它的判断条件不会因为插件「只置了一个标记」就放过——**改动一个共享状态位之前，
先看这个位还被谁读**。

---

## 第 36 轮：网关的失败原因被吞掉了

这轮的起点不是读代码，是**把插件真加载两遍**——宿主在「面板点了重载」「改了
`gateway.port`」「进程重启」时就是这么做。然后把 `download_link` 的每条分支真跑
一遍，看发进群的东西对不对。

### 1. 绑定失败只剩一个 `OSError`

```python
except OSError as e:
    logger.warning("下载网关启动失败 %s:%s：%s", self.host, self.port, type(e).__name__)
```

一个 `except` 把三件事吞成同一句话：

- 端口被占（`EADDRINUSE`）——多半是宿主重载插件时旧实例还活着，它的
  `terminate()` 没被调到；
- 主机名解析不了、端口越界、没权限绑低端口；
- `self.port` 写 0 时那是「让系统挑」，打进这句话**毫无意义**：用户看到
  `127.0.0.1:0` 会以为自己配错了端口（实际想绑的是系统挑出来的那个）。

更要紧的是**上层拿不到原因**。`gateway.running` 是 False，回执于是照着「网关
没起来」的模板说「把 `gateway.enabled` 打开」——可配置里那一项就是 `true`。
**提示指向一项本来就对的东西，等于没提示**，用户照改一遍还是同一句话。

现在：`DownloadGateway.last_error` 记下原因，`_explain()` 按 `errno` 分开说，
端口写 0 时不打端口。回执里 `enabled=true` 却起不来时直接搬这句话。

### 2. 重载这条路没人走过

`start()` 的路径依赖是「宿主一定会调 `terminate()`」。实测：

- `terminate()` 调了 → 固定端口能原样绑回来（这条本来是好的，只是没测过）；
- `terminate()` 没调（旧实例还在占着）→ 新实例静默失败。

第二条的第 32 轮教训原样适用：**「构造成功」不等于「能服务」**。所以
`last_error` 不只是日志，它是可断言的——`test_second_instance_cannot_bind_and_knows_why`
就是按这个写的。

### 3. `_public_base_url`：第二个「写全了、零调用点」

上一轮修的是它**内部**的 `_host_from_request`（那处确实补上了调用点）。函数本身
依然全仓只有定义和用例。它拼的是面板那条路的链接前缀
（`<host>/api/plug/backup/download`），而那条路在 4.14 上已被证伪：
`/api/plug/*` 既比不中带 token 的路径，又整条要面板 JWT（推导见
`core/download_route.py` 末尾）。

一处连锁反应得说清：**`round21` 里有一条用例把这条死路锁成了正确答案**
（`assert base == "https://bot.example.com" + instance._route_path()`）。删函数
时它是红的——那条断言本身才是 bug。改成断言「面板地址不再参与链接拼装」，并
在 docstring 里写明为什么改。面板路由登记（`register_web_api`）留着：
宿主版本会变，那部分是「尽力」，不再假装是链接来源。

### 4. 验证方式

`test_round35_gateway_restart.py`（6 条，旧代码上 4 条红）：

- 端口占着 → 日志里出现「端口」和**真正的端口号**，不出现 `127.0.0.1:0`；
- 主机名写错 → 日志里**不出现**「占用」（不把两件事混成一句）；
- 旧实例没 terminate → 新实例 `running=False` 且 `last_error` 说得出原因；
- 正常 terminate → 固定端口原样绑回，`last_error` 为空；
- 契约：`_public_base_url` 不存在，链接前缀唯一来源是
  `download_link` + `GATEWAY_PREFIX`；
- 端到端：端口被占时 `/backup export` 的回执写「端口已被占用」+ 端口号，且
  **不出现**「把 `gateway.enabled` 打开」。

第一个用例第一版把断言写成「出现 `127.0.0.1:<port>`」。它挂了，而挂的理由不
对——旧代码那行打的是 `self.port`，配成 0 时是 0。**留着这条断言，就等于把
「凑巧对的日志格式」测成契约。** 现在断言的是内容（端口号在、`:0` 不在）。

### 5. 一句总结

第 35 轮的教训是「改了共享状态位要先看谁在读它」。这一轮补另一半：**报错也是
共享的**——`except OSError` 这种兜底把「原因」和「结果」压成同一件事，上层就
只能照着自己那点信息编一句提示，编出来的多半是叫人去查一个对的东西。

---

## 第 37 轮：链接被"像真的一样"用过一次之后

前 36 轮验下载链路，用的都是同一个动作：`urlopen(link)` 一次，比字节。
可现实里没人这么下文件——先抓卡片、再探一刀、断线续传、下载器开三个连接、
链接被转发到另一个群。照着这个顺序打一遍，四处全挂，而且都在**已经修过三轮的
那一段代码**里。

### 1. HEAD 把一次性 token 烧掉了

```python
def do_HEAD(self):
    self._respond(body=False)      # 只影响「发不发 body」
```

`_respond` 里该做的都做了：`handle_download` 兑换 token、删 token。`body=False`
只是最后不多写那几个字节。于是：

- 微信/QQ/Telegram 抓一次链接预览 → token 没了；
- 下载器/浏览器"另存为"先探大小 → token 没了；
- 群成员真正点开 → `链接已失效或已被使用`。

**这条链接从头到尾只有一个人点过**，回执上那句"仅可使用一次"却在第一次
预览时就兑现了。一次性是给"把文件领走"这件事的，不是给"问一句这文件多大"的。

现在 `handle_download(..., head=True)`：只查 `lookup_token`（不占名额），
回 `200` + 真实 `Content-Length`。

### 2. HEAD 的响应里有两个 `Content-Length`

```python
for name, value in response.headers.items():
    self.send_header(name, value)          # Content-Length: 102404
self.send_header("Content-Length", str(len(response.body) or 0))   # Content-Length: 0
```

`handle_download` 填了文件大小，HEAD 这条分支又补了 `0`（因为不发 body）。
`http.client` 把两个同名头读成 `"102404, 0"`；宽松点的客户端按第二个理解，
存下来是 0 字节 ZIP，报"文件损坏"。现在 `_write_head` 只在缺的时候补。

### 3. 并发一来就是 500

网关是 `ThreadingHTTPServer`，每个请求一个线程。而 `Database` 只有**一条**
sqlite 连接，`check_same_thread=False`——那只是"不拦你"，不是"允许多线程同时
用"。`execute` / `query` / `executemany` 全都直接摸 `self._conn`：

```
下载网关处理失败：OperationalError('cannot start a transaction within a transaction')
下载网关处理失败：OperationalError('cannot commit - no transaction is active')
下载网关处理失败：SystemError('error return without exception set')
```

8 个线程各下 3 条，24 个请求里 3 个 500。真实现场更难看：备份主流程在往库里
写，群成员在点链接，两边共用同一个 `Database`。

现在 `Database` 有一把 `RLock` 护住连接（可重入——`save_message` 里会嵌套调
`query_one`），`executemany` 收成方法并支持 `commit=False`，让"一次事务写两张
表"真的一次提交。

### 4. Range 的两种合法写法被判成"要不到"

- `bytes=-500`（后缀）：注释写"对 ZIP 没意义，忽略"，代码 `return None`，
  而 `None` 在调用处等于"不可满足"→ **416**。断点续传工具（IDM、aria2）探
  文件大小发的就是这个，用户看到"下载失败"，文件其实好好躺着。
- `bytes=0-9,20-29`（多段）：取第一段、回 **206** 带 10 个字节，客户端以为
  这是它要的全部——拼出来是坏的 ZIP。

现在按 RFC 7233 分三档：语法不认 → 忽略 Range 发 200 整份；后缀区间 → 认，
取末尾；多段 → 服务端可无视，发 200 整份。416 时补 `Content-Range: bytes */<总长>`，
客户端据此重新对齐，不然它只会说"不支持断点续传"从 0 重来。

### 5. "仅可使用一次"在并发下是假的

判定原来是三步各自加锁：`has_been_opened` 查、`open_token` 写、
`resolve_token` 删。两个线程同时点同一条链接能交错着全通过——各自 `DELETE`
一次，一个删到 1 行、一个删到 0 行，**没人看 `rowcount`**，两个人一起把文件
下走。修好之后 30 轮并发测试里 30 轮都是单人得手。

现在收进 `Database.redeem_token`，一把锁里做完：

- **`full=True`（不带 Range 的请求）：`DELETE` 就是抢占**。`rowcount == 1` 的
  那个赢，链接转发出去第二个人点开当场 410，不用等窗口过期；
- **`full=False`（带 Range 的片段）：只刷新会话窗口**，`GRACE_SECONDS` 之内算
  同一次下载。

判据为什么是"有没有 Range"而不是"取到的字节数够不够整个文件"：`Range:
bytes=0-` 交付的字节数和整份一样多，但它是**分段下载的第一个动作**，后面还跟
续传。按字节数判，正当的续传链条会被掐断——这一版我先写成那样，被 `round30`
和 `round33` 的用例拦下来了，而那些用例拦得对。

**已知妥协，写在这里而不是藏着**：`GRACE_SECONDS`（默认 15 分钟）之内，别人
拿同一条转发链接发**片段**请求仍会被放行。HTTP 层面没有可靠判据能区分它和
"同一台机器在断点续传"——`Range: bytes=0-`、多连接下载、用户取消后重试三者
和"第二个人"长得一模一样。凭 IP/UA 去猜是另一类误伤。所以这里只认"一次会话"
这个 HTTP 真给得出的边界：**整份请求（点开链接最常走的那条路）只能一次**，
片段续传在窗口内共享会话。

一度想做成"整份交付**写完**才作废"（`finish_delivery` + socket 异常判断），
放弃了：插件观测不到交付终点。网关能靠写 socket 的异常判断，但同一个
`handle_download` 还被 Flask/Quart/aiohttp 三种视图复用，字节由那三个框架自己
吐。拿观测不到的东西当判据，等于在有的宿主上永远不失效——那比妥协更糟。

### 6. 验证方式

`test_round37_head_and_concurrency.py`（13 条，旧代码上 9–10 条红，跑三次都红）：

- HEAD 之后还能真下（旧代码 410）；
- HEAD 的 `Content-Length` 只有一个且等于文件大小（旧代码 `['102404', '0']`）；
- 8 线程 × 3 条并发下载、0 个 500（旧代码 24 个请求里稳定 1–3 个 500）；
- 备份在写库的同时下载，一个失败都没有（旧代码同样 500 起，写入侧也会炸）；
- 两个并发整份请求只有一个人得手，30 轮全中（旧代码 30 轮里 41 次双人得手，
  即多数轮次两个人都下到了）；
- 后缀区间回 206 且内容是末尾 N 字节（旧代码 416）；
- 多段区间回 200 整份（旧代码 206 + 10 字节）；
- 416 带 `bytes */<总长>`（旧代码没有这个头）；
- `bytes=abc-def` 回 200 而不是 416（旧代码 416）。

并发类用例故意做成"多跑几轮"：单轮可能碰不上，写成一次就断言等于把一条
概率性 bug 测成"偶尔绿"。

### 7. 一句总结

这四处不是"逻辑写错"，是**没人真正用过它**。前 36 轮的下载用例都是
`urlopen` 一次比字节——那个动作不会发 HEAD、不会并发、不会发后缀区间，
于是这一段代码在真实浏览器和下载器面前的每一道坎都没被踩到。
"用例绿"证明的是"我用过的那条路是通的"，不是"这条路能用"。

---

## 第 38 轮：把导出包当用户那样点一遍

前 37 轮验导出，验的都是「文件生成了、里面有这句话」。有没有人**点**过它？
没有。这轮装真 Chromium（153）、把真导出包解压，按群成员的动作走一遍：
翻记录、点图片、点附件、在搜索框里敲昵称。四处全挂，全在 HTML 导出这条
最显眼的产出上。

### 1. 包里的附件链接指着导出机的绝对路径

```python
return f'<a class="file" href="{html.escape(media)}" ...>📎 {name}</a>'
#                                          ↑ media_paths 的原值
#                                          = /root/.astrbot-backup/media/20240501/m4_ghi.pdf
```

`_export_zip` 明明把媒体打进了 `media/20240501/m4_m4_ghi.pdf`，可 `chat.html`
里写的还是**导出机上的绝对路径**。解压到任何别的机器，点开就是
「无法访问此文件」——文件就在旁边那个目录里躺着。

这个包的卖点正是「解开就能看」。图片因为内联成 `data:` 侥幸没事，于是
只有附件坏掉——**恰好是唯一带不走的那类证据**。

现在 `ExportContext` 多了一个 `media_links`（媒体原值 → **可点得动的地址**）。
`_export_zip` 先算包内成员名，再渲染 `chat.html`，页面里的 href 就是
`media/20240501/m4_m4_ghi.pdf`。单文件导出没有这个映射，于是：

- 图片 → 内联；内联不下（超预算）→ 明说「体积过大，未内联」；
- 非图片附件 → 明说「未随本页导出，需要附件请用 zip」。

**不要给一条别人打不开的链接**，比给一条 404 诚实。

### 2. 「点击放大」用 `window.open(data:...)`，被浏览器拦成静默失败

```js
if(e.target.classList.contains('media')){ window.open(e.target.getAttribute('src'),'_blank'); }
```

`src` 是 `data:image/png;base64,...`。主流浏览器把「打开 data: URL 的新标签」
一律当弹窗拦掉，而且**没有任何提示**——点了完全没反应，用户以为图片坏了，
而缩略图本身又小得看不清。真机上是这样：

```
节点上绑了 click，点击后 window.open 返回 null，tabs 数量不变
```

现在改成自建覆盖层（`#lightbox`）：点击缩略图把 `src` 赋给覆盖层里的
`<img>`，点任意处或 Esc 关闭。没有新标签、没有弹窗、没有 `window.open`。

### 3. 搜索框写着「搜索昵称或内容」，`data-text` 里只有内容

```python
text_for_search = html.escape((m.content or "").lower())
```

搜「阿七」搜不到任何东西——因为昵称只在 `<span>` 里显示，不在 `data-text` 里。
附件名同理：只发了个 PDF、正文空白的消息，在搜索里是**隐形**的。

比这更难查的是第二层：`el.hidden = true` 写了，消息一条都没消失。原因是
`.msg{display:flex}` 的优先级高于 UA 样式表里的 `[hidden]{display:none}`。
表现是「输入关键词后整页消息还在，中间冒出一句『没有匹配的消息』」——JS 和
CSS 各自都对，凑一起才错。现在模板里有一行 `[hidden]{display:none!important}`。

`data-text` 现在收：昵称 + 正文 + 媒体文件名，仍然整串走 `html.escape`——
昵称是用户可控的自由文本，加字段不能顺手把转义丢了。

### 4. `[下载失败] <url>` 被当成文件路径塞进 `href`

下载失败的媒体在 `media_paths` 里留的是 `"[下载失败] http://..."`（红线 2 的
兜底）。它**不是路径**，是一个带标记的字符串。原来直接 `html.escape` 之后当
href 用，点开是 `[下载失败]%20http://...`，一串谁也看不懂的东西。

现在这种值单独识别：原始 URL 才是唯一还能用的线索，做成链接（`nofollow`，
不把外站的权重带走），显示的是文件名而不是带签名参数的整条 URL。

### 5. 顺手补的：内联没有总预算

单张 6MB 的上限挡不住「一天两百张合格的图」。一张 3MB 的 PNG 内联后 base64
还要再涨 1/3，两百张就是 **800MB 的单个 HTML**，浏览器直接卡死，而每张图
单看都合格。

加了 `MAX_INLINE_TOTAL_BYTES`（96MB）按原始字节累计，超预算的图退化成包内
链接。预算在每次 `render()` 开头重置——同一个 exporter 连着渲染两次
（导出一次、打包时再写一份 `chat.html`）时，第二份不能因为上一份用掉了而
突然全部不内联。

### 6. 验证方式

`test_round38_export_page.py`（12 条，旧代码上 **10 条红**）：

- ZIP 里 `chat.html` 的每个 href 都是包内成员、且导出机路径一个字节不留（旧代码
  直接给出 `/tmp/.../m2.amr`）；
- 单文件导出页里没有任何本机绝对路径（旧代码整页都是）；
- 模板里没有 `window.open`、有 `#lightbox`（旧代码两样都没有）；
- 模板有 `[hidden]{display:none}`（旧代码没有，搜索等于没生效）；
- `data-text` 含昵称 / 含附件名（旧代码搜不到）；
- 昵称带 `<img onerror>` 时仍是转义文本（加字段不能丢转义）；
- `[下载失败] <url>` 的 href 是原始 URL、显示的是文件名（旧代码 href 是那个标记串）；
- 只有标记没有 URL 时不可点（旧代码照样生成 `<a>`）；
- 内联预算：6 张 4KB 的图、预算 12KB → 只内联 2 张，其余走包内链接
  （旧代码全内联）；
- 同一个 exporter 渲染两次，内联数量一致；
- `MAX_INLINE_TOTAL_BYTES` 是个正数（旧代码没有这个常量）。

视觉复查：真 Chromium 截图看过亮色/暗色、单文件/ZIP 解压两套、灯箱打开态、
下载失败态、以及 1280px 与移动宽度。第一次改完的截图里发现两个自己造的毛病
（`[图片]` 占位和图片同时出现；提示文案把气泡撑得很难看），一并修了。

### 7. 一句总结

第 37 轮的教训是「没人真正用过它」——下载链接。这轮是同一件事的另一半：
**导出的东西也没人真正打开过**。用例一直在断言"这个字符串在不在 HTML 里"，
而那串字符串恰好是别人机器上不存在的路径。

更进一步：**产物是给人看的，验证就得走人的路径**。`href` 里有什么字都对，
点下去能不能开才是契约。

---

## 第 39 轮：把「换台机器」这件事真做一遍

第 38 轮学会了「产物要当用户那样点一遍」，于是只点了 `chat.html`。
这轮把场景往前推一步——**原机器没了，手里只有一个 zip**：

    解压 → 看 chat.html → 用 txt/csv/markdown 归档 → 恢复回新机器 → 再导一次

三处全挂。两处是第 38 轮漏掉的同一件事，第三处是整个插件存在的理由。

### 1. 同一个包，一份能点、四份是死链

第 38 轮给 `chat.html` 加了 `ctx.media_links`（媒体原值 → 包内地址），
但同一份包里另外四份产物照旧写着导出机的绝对路径：

```
messages.txt     <附件> /root/.astrbot-backup/media/20240501/m4_ghi.pdf
messages.csv     ...,/root/.astrbot-backup/media/20240501/m4_ghi.pdf
markdown/x.md    ![图片](/root/.astrbot-backup/media/20240501/m4_ghi.png)
json/daily/x.json "media_paths": ["/root/.astrbot-backup/media/20240501/m4_ghi.png"]
```

而 `media_links` 是在这四份导出**之后**才算出来的，只有 `chat.html` 享受到了。
一份包五个文件，一个能点、四个废掉——偏偏 txt 和 markdown 是 README 里
「归档进 Git / 存档阅读」的推荐格式，json 是「适合程序处理」的那份。

现在是**先**算包内成员名、**再**跑所有导出器。每个导出器用同一件事：
有包内地址就写包内地址，没有就只写文件名并说明为什么点不动。
`[下载失败] <url>` 单独识别：原始 URL 做成链接，显示的是文件名而不是
带签名参数的整条 URL。（这条第 38 轮在 HTML 里修过，其余四份没有。）

**markdown 那层 `../` 是最容易漏的。** 把绝对路径换成 `media/...` 还不够：
这份文件在 `<包根>/markdown/` 下，地址会被解析成 `markdown/media/...`，
一样打不开。所以 `media_href(..., prefix="../")`，并有一条用例真的去
「解压后按相对路径找文件」。

json 的 `media_paths` 直接换成包内地址。它是给程序读的，读的人不会知道
要先自己拼一层 `media/`；只留原值，任何照着它读文件的程序都只能拿到 ENOENT。

### 2. 从 ZIP 恢复，媒体一个字节都不落盘

```python
self.restore_engine = RestoreEngine(self.db)   # 没有媒体目录这回事
```

`RestoreEngine` 只搬消息行，`media_paths` 原样保留导出机路径。恢复回执说
「写入 3 条」，打开聊天记录每张图都是「本机找不到这个文件」——而文件就在
他刚解压的那个包里。

现在恢复引擎收一个 `media_dir`，写库之前先把包里的 `media/...` 落回本机、
登记进 `media_files`、把 `media_paths` 改成本机真实路径。回执多一句
「媒体 N 个已落回本机」：只说「写入 3 条」，用户打开 HTML 发现全是占位，
不知道该怪谁。

几个不能省的细节：

- **落盘在写库之前。** 写库之后才发现「这条的图在哪」就晚了。
- **路径写本机真实位置，不写包内相对路径。** 库里的 `media_paths` 给下载、
  去重、导出三处共用，留一个只在包内成立的相对路径，等于把「文件在哪」
  绑死在某一次导出上。
- **包内成员名逐段取 `Path.name`。** 它来自包，等于不可信输入，`../` 要挡掉。
- **登记 `media_files`。** 没有这行，第二次导出时 `_media_index` 找不到它，
  包里的图会凭空消失，`/backup clean` 也认不出这个文件归谁。
- **包里没有那份媒体时保留原值。** 洗成空列表等于把「附件丢了」这件事抹掉，
  之后再导出，页面上一句提示都没有，看起来一切正常。
- **`media_dir` 不给时降级为旧行为。** 这是增强，缺席时不该让调用方直接报错。

### 3. 这次恢复会把媒体永久弄丢

第 2 条的直接后果，也是这轮最要紧的一条。恢复完之后本机没有文件、
`media_files` 是空的，**第二次导出 `media_files: []`、一张图都没有**。
原包一删，群里的图片和文件就真的没了——而「群被炸了靠它找回来」正是
这个插件存在的理由。

用例 `test_restore_media_survives_a_re_export` 走完「恢复 → 再导」，断言新包里
两个媒体都还在。这条比第 2 条更值得写：第 2 条的失败是「看着不对」，
这条的失败是「东西没了，而且没人知道」。

### 4. 导出/恢复对「包里叫什么」两处各写一遍

```python
member = str(Path("media") / path.parent.name / f"{m.message_id}_{path.name}")  # 导出
member = media_member_name(m.message_id, media)                                 # 恢复（新写）
```

这是这类 bug 的老家：导出按 A 命名、恢复按 B 去找，找不到就静默跳过——
包里有文件、库里没路径，看着像「恢复成功」。现在只有一个
`exporters/base.py::media_member_name`，两边共用，并有一条用例断言
「导出端塞进包里的名字集合 == 恢复端按同样规则算出来的名字集合」。

顺带修了两处自己造的毛病：

- **名字会滚雪球。** 恢复出来的文件本来就叫 `<message_id>_<原名>`，导出时
  再拼一次前缀就成了 `m2_m2_m2_photo.png`，恢复→导出每转一圈长一截。
  改成幂等：已经带过前缀就不再拼。
- **两条消息共用一份媒体时，第二条丢图。** 导出端按媒体原值去重（同一份
  文件不该在包里存两份），包里只有 `<第一条的 id>_<文件名>`；恢复端按各自
  message_id 算名字，第二条算出来的在包里不存在，于是显示「本机找不到」。
  「同一条消息被实时归档和定时备份各记一次」正好长这样：两个 message_id、
  一份文件。现在按 id 算不到时，退回「同目录同文件名、只是前缀不同」的
  唯一命中；多个候选就保留原值让它显形——张冠李戴比缺失更难查。

这个 bug 是截图看出来的：我造的真 PNG 只有 8×8，两条图消息一条显示、
一条显示「本机找不到这个文件」，颜色和大小都对，肉眼一看就知道不对。
**用例当时是绿的**，因为它只断言「文件在不在磁盘上」，没断言两个
message_id 共用一份媒体。

### 5. 验证方式

`test_round39_package_media.py`（19 条，旧代码上 **15 条红**，失败全落在断言上）：

- 包里 txt / csv / markdown / json 的媒体地址都是包内相对路径（旧代码四份全红）；
- 包内所有 `.txt/.csv/.md/.json/.html` 里一个字节都不许出现导出机目录（旧代码 red）；
- markdown 真的解压、按相对路径找文件，`../` 必须成立（旧代码 red）；
- 恢复后 `media/` 下真有那两个文件、`media_paths` 指向本机（旧代码 `media_restored=0`）；
- 恢复 → 再导，新包里媒体还在（旧代码 `media_files: []`）；
- 恢复 → 导出 → 恢复转三圈，包内文件名不增长；
- 两条消息共用一份媒体，恢复后两条都找得到（旧代码只落 1 份）；
- 包里没有的媒体保留原值，不被洗成空列表；
- 单文件 txt/markdown 导出不编假链接、只写文件名加说明。

真浏览器复查：解压后的 `chat.html` 与 `markdown/` 页面都过一遍 Chromium，
图片 `naturalWidth > 0`、附件相对链接 `resolve().is_file()`，亮色暗色都截图看过。

### 6. 一句总结

第 38 轮的教训是「导出的东西没人真正打开过」，修的是**打开**这一步。
这轮是同一件事的下一格：**打开之后、以及把它搬到另一台机器上之后**。

一处判断失误值得记下来：我第一版的验证方式是「人眼看截图」，看到图显示正常
就以为过了。真正救回这个 bug 的是「换个视角走一遍」——把包解压到另一个目录、
用另一台机器的库去恢复，而不是在原来的上下文里再断言一次字符串。

**产物要跟着用户走完全程，才算验过。** 停在「导出了、里面有这句话」，
和第 38 轮停在「文件生成了」是同一个坑。

---

## 第 40 轮：`config set` 的每一句「已设置」都是假的

这轮的起点不是读代码，是**把 schema 里每一项都改一遍，再回头看组件变没变**。

```python
p = BackupPlugin(Context())                             # 组件在这个时刻抄走了配置
replies = await _cmd_config(ev, ["set", "media.timeout", "5"])
p.engine.media.timeout                                  # → 30
```

15 项，15 项全没变。回执写的是「✅ `media.timeout` = `5`」——它说的是配置字典变了，
一个字没提「这次运行仍然按 30 秒超时」。用户按回执以为改好了，实际得重载插件。

### 1. 值被抄进属性里，改字典对属性没有任何作用

每个组件都在 `__init__` 里把需要的值取出来存成自己的字段：`MediaDownloader.timeout`、
`ExportManager.expire_seconds`、`PermissionChecker.require_admin`、`AutoTrigger.threshold`、
`EventHooks.archive`、`BackupScheduler.enabled`。这是对的（组件不该每次用到都回头翻配置），
错的是**只有一条写入路径**：`config set` 只改字典。

现在每个组件多一个 `reconfigure()`，`__init__` 和 `config set` 共用它。构造路径和
改配置路径走同一段代码，才不会一边改一边忘。

两处只能重建、改不了属性：

- `media.max_concurrent` 落在 `asyncio.Semaphore` 里。额度构造时定死，改属性不改信号量
  等于没改——必须新建一个。
- 网关的开关是 bind/unbind socket。关掉要 `shutdown()`，打开要真 `listen()`，换端口要先关再开。

### 2. `storage_dir` 是唯一诚实的例外

换目录牵扯正在打开的 SQLite 连接、已经落盘的媒体、已经发出去的 token 里的路径。
热切等于把库和文件劈成两半。所以这一项**改不了**——但回执必须说出来：

```
✅ `storage_dir` = `/new/path`
⚠️ `storage_dir` 改成 /new/path 要重启插件才生效（当前仍在使用 /old/path，库和媒体文件不会自动搬家）
```

「做不到」和「装作做到了」的区别，就在这句话上。

### 3. `scope.group_members` 是一张从没兑现的支票

schema 里写着、README 里写着「成员列表」，代码里**一次都没读过**。关掉它照样每轮拉
一次全员名单：2000 人的群，一次几千条。用户关它的理由（省时间、省配额、平台不给权限）
一条都没兑现。

### 4. 私聊归档只进不出

`scope.private_messages` 打开后，私聊消息落在虚拟群号 `-1`。可 `parse_group_arg` 的
下限是 5 位数字：

```python
parse_group_arg("-1")   # → None
```

于是 `/backup status -1`、`/backup export -1 zip` 一律回「请指定群号」。数据在库里，
**没有任何命令能把它们导出来**——而「私聊也备份」这个开关唯一的价值就是导出来看。

现在 `-1` 可寻址，`render_status` 把它显示成「私聊归档」而不是「群 -1（-1）」：
内部约定的负数群号不该甩到用户脸上。

### 5. 第二个「写全了、零调用点」

`dashboard.public_url` 没人读，`_dashboard_host()`（去翻宿主 `cmd_config.json` 的那个）
自己也没有任何调用点。第 33 轮刚删过同形状的 `_public_base_url`，这是第二个。

它比上一个更隐蔽：它有**两条用例**。一条断言「它得出现在 schema 里」，另一条把它
`monkeypatch` 掉——两条都在把它按住，没有一条在验它有用。用例数量和它是否被调用
毫无关系。

一并删掉。链接来源现在只有三层：`export.base_url` → 这条命令进来的域名 → 裸路径。

### 6. 顺手撞出来的：`ExportResult.link` 少了路径段

```python
f"{self.base_url.rstrip('/')}/{self.token}"        # → https://host/<token>
```

网关只认 `/backup/download/<token>` 开头。这条拼法在网关上一定 404，而
`download_link()`（`/backup export` 回执走的那条）拼得是对的——同一个配置项、两条产出、
两种拼法。之所以一直没被发现：`ExportResult.link` 全仓零引用，用例也只断言了
`base_url` 前缀、没断言路径。

写这轮的用例时我才注意到：第一版断言写的是 `download_link()` 的返回值，而那个函数
每次都直接读 `self.config`——**旧代码上它也是对的**，这条用例在旧代码上蒙过了。
改成断言 `ExportManager.base_url` 和 `result.link` 之后才真的红。

### 7. 验证

`test_round40_config_takes_effect.py` 27 条，旧代码上 **26 条红**（1 条是「顺手撞出来
的少了路径段」那条的初版，见上）。留着没红的那条说明我第一版写弱了，不是说明旧代码对。

- 15 个配置项逐个走 `/backup config set` 命令路径，断言**运行中组件**的值变了
  （不是断言 `self.config` 变了——那正是旧代码唯一的动作）；
- `max_concurrent` 断言信号量额度真的重建（`_sem._value`）；
- `schedule.enabled=false` 断言已挂的任务从 `get_jobs()` 消失，且库里的 cron 记录留着
  （再打开就回来，不能连配置一起删）；
- `gateway.enabled` 断言 socket 真的关/真的开；
- `storage_dir` 断言运行目录一个字节没动、且回执里说了「重启才生效」；
- `group_members` 关掉时断言 `get_group_members` 调用次数为 0（真的没去要）；
- 私聊归档：`-1` 可解析、`status` 显示「私聊归档」、`export -1 zip` 出的包里有那条消息；
- 一条「schema 里每一项都得有代码读它」的静态检查——`dashboard.public_url` 和
  `scope.group_members` 都会被它抓出来。

全量 **620 passed / 1 skipped**，覆盖率 92%。

### 8. 一句总结

前几轮的 bug 大多是「这条路径本身写错了」。这轮不一样：**代码每一段都对，
只是它们之间没有连线**。配置键在 schema 里、在 README 里、在回执里、在字典里，
唯独不在干活的组件里。回执说「已设置」不算撒谎——它只是回答了另一个问题。

**「配置项存在」和「配置项生效」之间隔着一次连线，而 schema / 文档 / 回执都证明不了
那条线存在。** 唯一能证明的是：改一下，然后去问那个干活的组件。
## 第 41 轮：按真群规模跑一遍

前 39 轮验的东西，绝大多数是在「3 条消息、1 个成员」这种规模上验的。红线 6
写的是「大群（2000 人以上）必须分页拉取」——分页是做了，但这个插件从没按
2000 人的群的量级真跑过一次。照那个规模跑，五处露出来。

### 1. 每页封顶 20 条，120 条的群备到 20 条

```python
if len(batch) < PAGE_SIZE // 2:
    break
```

平台把单页封顶在 20 条（NapCat/Lagrange 一类实现的常见行为），第一页 20 < 25，
当场收工，状态 `completed`。

第 31 轮修过同一条判据：原来写 `len(batch) < PAGE_SIZE`，把「每页 30 条」修好了；
这次是「每页 20 条」又漏。**「半页」这类判据永远只是把阈值挪一挪，而平台的封顶值
可以比任何固定阈值都小。** 现在收工的判据只剩两条：平台回空页，或者连续两页
翻不出新东西。多问一次空页的代价是一次请求，少问一次的代价是一整段历史永久
缺失、而且没人知道。

顺手修了一个硬伤：补齐路径的第一页会无条件带 `offset`，而只认 `message_seq`
的实现直接判「无匹配签名」——补齐一步都没走出去，整群报 `failed`、0 条。
`PlatformAdapter` 那套补齐本来就是为了对付「一次给不满 count」的平台，
而它自己的第一页在这些平台上从来没成功过。

### 2. 写库一行一次 commit，2 万条要 175 秒

```
3000 条   21.8s   138 条/秒
20000 条 175.4s   114 条/秒
```

`execute()` 每次都 `self._conn.commit()`，而 SQLite 默认 `journal_mode=DELETE` +
`synchronous=FULL`：每一次提交都要 fsync 日志文件 + 主库文件。备份是「一条消息一次
`save_message`」，于是每个群按「条数 × 2 次 fsync」付账。

两件事一起改：

- **`batch()`**：一页一次事务，`isolation_level=None` 显式 BEGIN / COMMIT。
- **WAL + `synchronous=NORMAL`**：WAL 把随机写变顺序追加，NORMAL 在 WAL 下不会
  损坏数据库，最坏是断电丢最近若干个已提交事务（这里就是「最近一页」）。

```
3000 条   0.54s   5500 条/秒
20000 条  3.78s   5300 条/秒
```

`batch()` 有两条约束写进了注释，因为它们是「一条连接」逼出来的，不写下来下一个人
一定踩：**块里不能有 `await`**（事件循环切走后别的协程会顺着可重入锁进到同一个
事务里），**整块持锁**（网关线程此刻 commit 一次就把这页提前提交了）。所以媒体
下载放在事务外——先下完，再整页入库。

顺带得到一个额外的好处：断在一页中间就整页回滚，库里永远是「整页的整数倍」，
下一轮从断点接着翻。有一条用例专门验它（第 70 条写入抛错 → 库里正好 50 条）。
这条用例第一版是红的：`isolation_level=None` 是**自动提交**模式，不显式 `BEGIN`
的话每条语句各自落盘，「一页一次事务」变成一句空话，写错也不回滚。

### 3. 一页失败，整轮作废

500 条的群、第 5 页网络抖一下：整次备份判 `failed`。已经备上的 200 条在库里，
但回执只有一句 `RuntimeError`——用户既不知道丢了多少，也不知道该不该再跑一遍。

现在单页退避重试 3 次（1/3/5 秒）。重试也失败就降成 `partial`，回执写明
「第 4 页拉取失败，已保住前 3 页」，下一轮从断点接着补（增量只看「库里有没有」，
不认水位）。第一页就失败是另一回事：没有「保住的部分」可言，仍按 `failed` 处理，
错误原话一并留着。

`round23` 里那条把「一页失败 = 整轮 failed」钉成正确答案的用例，按新语义改写了 ——
原文写清了为什么改：那条断言保护的是「失败原因不被吞掉」，而不是「必须报 failed」。

### 4. 跑到一半重启，记录永远停在 `running`

记录的生命周期是「开跑先写一条 running → 跑完改状态」。宿主重启、插件重载、容器
被 kill 都会让进程死在这两步中间，于是库里留下一条永远 `running` 的记录：

- `/backup list` 里它一直挂着，看着像「还在跑」；
- `/backup status` 按 `latest_completed_record` 找实绩，找不到它；
- 恢复被「状态为 running，不可恢复」挡回去——而它其实已经把上千条消息写进库了。

现在启动时 `reconcile_stale_records(cutoff)` 把 `started_at < cutoff` 的那些收成
`partial`，并按归属补登来源清单（没有清单，导出和恢复都按来源取数，一条也取不出来，
降级等于白降）。宽限两分钟，不能写成「所有 running 都降级」——正在跑的那一轮会被
踩掉，有专门一条用例盯着。

### 5. 超限的媒体把半截 `.part` 留在盘里

`_fetch` 发现文件过大直接 raise，临时文件不清；`download` 的清理只在「大小为 0」
时触发。20 万条消息里哪怕 1% 超限，媒体目录里就是两千个没人认领的残渣，
`/backup clean` 也认不出它们。

临时文件名还固定是 `<正式名>.part`，于是**同一个 URL 被两处同时下**（实时补下撞上
定时备份、用户连点两次）时，先完成的那次把它换走了，另一次的 `replace` 找不到源
文件，抛 `FileNotFoundError`，媒体被记成「下载失败」——而盘上明明有一份完整的。
现在临时名带进程内唯一后缀，谁的文件谁收拾，`finally` 里删自己的那一份。

### 6. 顺手：私聊那条路补上端到端

第 40 轮把 `parse_group_arg` 从「至少 5 位数字」放宽成 `-?\d{1,20}`，`-1` 和
Telegram 的 `-100…` 都能解析了。这轮补的是「解析出来之后真的能跑完」：
私聊消息入库 → `/backup status -1` → `/backup export -1` → 包里真有那条消息。
顺手把「`999` 这种三位群号」和「`0` 不算群号」也钉进用例——第 40 轮那条放宽
没有下界保护，`0` 会解析成 0，而 0 不是任何一个群的号。

### 验证

`test_round41_scale.py`（19 条）。分页、大批量、单页失败、僵尸记录、残渣、
私聊群号六类（全量 639 passed / 1 skipped，覆盖率 90%）。把 `backup/core` 与 `backup/main.py` 回退到本轮之前跑一遍：
**13 条红、6 条绿**。绿的那 6 条也是这轮加的，只是旧代码恰好在那个角度上没露馅
（比如「只认 message_seq 且每页 20」：旧代码的补齐路径确实会卡在 `offset` 上，
但那个替身的签名宽松，`offset` 被 `**kwargs` 吞了，所以它绿——真实 OneBot 客户端
不吞，`offset` 会被当未知参数打回。这条用例留着，是给真实现兜底的）。红的那批：

- 每页 20：旧代码 `只备到 20 条`；
- 翻到底判据：旧代码 70 条的群只备 20；
- 3000 条时限：旧代码 `21.0s`；
- 批量提交可见性：旧代码只到 20 条（同一个分页 bug 的连带）；
- 单页抖动：重试在旧代码上不存在，`只备到 150 条`；
- 重试用尽：旧代码 `failed`（库里明明有 150 条）；
- 超限残渣：旧代码留 `<名字>.part`；
- 并发同一个 URL：旧代码 `FileNotFoundError`；
- 整页回滚：旧代码一页写到一半抛错，前 19 条留在库里（69 条而不是 50 条）；
- 僵尸记录三条：旧代码没有 `reconcile_stale_records`；
- 两万条僵尸记录：旧代码没有 `batch()`；
- 私聊 `-1`：旧代码 `None`（这轮跟着第 40 轮的放宽一起验到底）。

真浏览器复查：2 万条 / 2000 人的 HTML 导出（7.1MB），Chromium 里实测 20000 个
消息节点、搜索「群友12」命中 737 条、耗时 37ms —— 首屏那段截图就是它。

### 一句总结

第 38 轮是「没人真正打开过它」，第 39 轮是「没人把它搬到另一台机器上」，第 40 轮是
「没人问过配置到底生效没」。这轮是**没人用真数据量试过它**。四条都指向同一件事：
用例跑在 3 条消息上，绿得很干净，而真实世界给的是 2 万条、每页只回 20 条、
网络偶尔断一下、进程偶尔被杀。

**「跑得通」和「跑得动」是两个命题。** 前 40 轮一直在证前者。

## 第 42 轮：插件从来没被装上过

这一轮的起点不是读代码，是按 README 的安装段装一遍：

```bash
cd AstrBot/data/plugins
git clone <本仓库> astrbot-plugin-Backup
```

装完，插件列表里没有它。日志只有一行：

```
插件 astrbot-plugin-Backup 未找到 main.py 或者 astrbot-plugin-Backup.py，跳过。
```

`PluginManager._get_modules` 的判据就这一条：

```python
if os.path.exists(os.path.join(path, d, "main.py")):      module_str = "main"
elif os.path.exists(os.path.join(path, d, d + ".py")):    module_str = d
else:                                                     continue   # 跳过
```

而仓库把 `main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt`、
`config.yaml`、`logo.png`、`templates/`、`assets/` 全收在了 `backup/` 子目录里。

### 为什么 639 条用例一条都没发现

因为它们**都从「文件在哪」出发**（`Path(__file__).resolve().parent.parent`），
没有一条从「宿主怎么找插件」出发。更关键的是 `load_plugin_for_real` 自己把加载器
绕开了：

```python
spec = importlib.util.spec_from_file_location(name, plugin_dir / "__init__.py", ...)
```

自己算目录、自己造 spec，等于把 `_get_modules` 里那条 `os.path.exists` 判据
整个跳过。它验的是「这段代码 import 得动吗」，不是「宿主会加载它吗」。这两个问题
在这份代码里曾经是两个答案。

### 表现，以及为什么它比崩溃更难发现

- 插件列表里**没有这个插件**，不是「装坏了报错」。用户看到的是「我装了但没生效」，
  第一反应是回去查安装步骤，而安装步骤正是错的。
- 同一个 `backup/` 目录还会让 `import backup.core...` 在进程里命中**磁盘上那个
  子目录**，而宿主加载的是 `data.plugins.<目录名>.main`。同一份代码两份模块对象，
  改配置、改状态、注册表全对不上——这一层只有在把文件挪到根上之后才会暴露。

### 改了什么

文件全部搬到插件根，`backup/` 目录消失。仓库根 = 插件根，和任何 AstrBot 插件一样。

测试侧不能再靠目录位置，所以 `tests/conftest.py` 开头按**固定名 `backup`** 把根目录
注册进 `sys.modules`（`__path__` 指向根），`backup.core.database` 这类导入原样不动。
不去 `sys.path` 里找 `backup`：安装后的目录名是 `astrbot-plugin-Backup`（带连字符，
import 语句写不出来），本地测试时又是任意目录名，两者都不该成为用例的前提。

`tests/test_round42_plugin_root_layout.py`（12 条）：

- 8 条「宿主直接读的文件必须在插件根」（`main.py` / `metadata.yaml` /
  `_conf_schema.json` / `requirements.txt` / `config.yaml` / `logo.png` /
  `templates/chat.html` / `assets/logo.svg`）
- 1 条「`backup/` 子目录不许回来」（防两份模块对象）
- 1 条**调宿主自己的 `_get_modules`**（不经过任何测试侧计算，旧布局上返回 `[]`）
- 1 条 `metadata.yaml` 字段完整性（宿主缺 name/version/author 会直接抛）
- 1 条 schema 与 `config.yaml` 的键不漂移

### 端到端

按真装法放到 `data/plugins/astrbot-plugin-Backup`，用 `data.plugins.<目录名>.main`
这条真实路径 import，然后：

- `_get_modules` 认出了它，`star_map` 里有 `backup`，三个 handler 全在
- 实例化成功，`storage` 落在 `~/.astrbot-backup`，网关真绑上了端口
- `/backup help` / `list` / `export` / `status` 都对答
- 导出 → 从网关 HTTP 下载（7.8KB）→ 同一个 token 再点一次回 410
- 用另一个库从包里恢复：3 条消息 + 1 个媒体落回本机，路径指向本机

### 一句总结

前 41 轮验的是「代码写得对不对」，这轮是**「它到用户手里到底存不存在」**。
用例可以绕开加载器验逻辑，但用户不能——他手里只有 README 和那个目录。

## 第 43 轮：`/backup verify <备份ID>` 从来没校验过任何东西

这一条的起点是发版前把 README 的「命令速查」表逐行跑一遍。跑到 verify 那行：

```
/backup verify bk1
✅ 本机记录校验和：1a992468c9be26dc
```

那个 ✅ 是个装饰。它只是把 `backup_records.checksum` 里存着的字符串截了前 16 位
打出来——库里那批消息被改成什么样，这一句都是同一个形状、同一个 ✅。

更隐蔽的是同一个函数的另一条分支（给了备份包）**是真的在校验**。用户见过它报
「✅ 包与自身清单一致」，就以为这个命令整体可信。

### 为什么它一直没被发现

因为它的输出**看起来就是校验的样子**：有个 ✅、有个十六进制串、还有个技术感十足
的「校验和」词。前 42 轮里有三处 `verify_zip` 的用例（CRC、manifest 校验和、
缺失媒体），全都在测「给了包」那条分支。**没给包那条分支一条用例都没有**——
它是个只打印不判断的分支，而打印看起来像判断。

### 改了什么

重算：来源清单里的消息取出来、按 `compute_checksum` 同一套规则哈希、和记录比。
这个函数早就写好了（确定性哈希，与顺序无关），`RestoreEngine` 一直在用它，
只有这条命令没用。

四种情形分清楚，不再用一个 ✅ 盖住：

| 情形 | 回执 |
| --- | --- |
| 重算 == 记录 | ✅ 校验通过：N 条消息与备份时的校验和一致 |
| 重算 != 记录 | ❌ 校验不一致：现在重算得 X，记录里是 Y |
| 清单有、库空了 | ❌ 消息可能已被清理，指向 `/backup verify <ID> <包路径>` |
| 老记录（checksum 为空） | ⚠️ 没有校验和，无法比对——不假装验过，也不谎称损坏 |

最后一行是刻意的：早期版本建的记录 `checksum` 是空串，把它混进「校验不一致」
等于告诉用户他的数据坏了；混进 ✅ 则是假装验过了。

### 验证

`tests/test_round43_verify_actually_verifies.py`（3 条）。把 `main.py` 回退到本轮
之前跑：**3 条全红**（都红在 `'✅ 本机记录校验和：…'` 这个返回上）。

### 一句总结

这是这一轮唯一一个「功能一直在，只是没干活」的缺陷。前两轮修的是「装不上」和
「没人按回执点过链接」，这条是**没人看过回执里那个 ✅ 到底代表什么**。
