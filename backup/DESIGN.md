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

**下载链接：上一轮的结论是错的**

上一轮我写在这里的结论是「4.14 上群里点链接做不到，因为 `/api/plug/*` 一律要 JWT」。
再核一遍 `dashboard/server.py`，两处都读错了：

- `auth_middleware` 的白名单是**按前缀**放行的，里面明明白白有一条
  `/api/backup/download`，注释写着「备份下载使用 URL 参数传递 token」；
- 插件登记的 `route` 在前缀匹配里就是**前缀**：`/api/plug/<path:subpath>` 命中后
  用 `route == f"/{subpath}"` 去比，`/backup/download/<token>` 这条能匹配上
  `backup/download/xxx` 这个 subpath。

所以「把链接发给群成员、他点开就能下」本来是通的，是我上一轮下错了结论，还把错的
理由写进了代码注释，免得「下次又当成 bug 去查」——反过来把正确判断挡住了。

真正卡住的是另外两处，都是接线细节，一起修了：

1. **视图函数的参数对不上**。`srv_plug_route` 是 `await view_handler(*args, **kwargs)`，
   而 `subpath` 已经被它自己吃掉当路由匹配用，视图拿到的是**零个**位置参数。视图
   签名里写 `request` 就是 `TypeError`。现在视图自己去找当前请求（Flask/Quart 的
   请求上下文、aiohttp 的形参），并且所有形参可选。
2. **回元组是空文件**。`DownloadResponse.body` 在正常下载里是空的（文件在 `file` 上），
   回 `(body, status, headers)` 会让 Flask 认为响应体就是 0 字节：状态 200、
   `Content-Length: 0`、下下来的 ZIP 打不开。现在回一个 WSGI 响应对象，把
   `iter_chunks()` 接上去。这个 bug 只有跑真 Flask 才看得见，假 Request 的用例永远绿。

顺带两件：`/api/backup/download/<token>` 前面那层 `/api` 要在 `handle_download` 之前
削掉（否则每次真实点击都是 404）；`export.base_url` 空着时从面板配置里找公网地址拼成
`<面板>/api/backup/download`——原来只给裸路径，发到群里没人点得动。

三条路由都登记（宿主侧精确匹配，只能一条一条来）：`/backup/download/<token>` 是群里
点的，`/backup/download` 是面板按钮 + query token，`/download/<token>` 兜住宿主自己
吃掉前缀的情况。

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

## 红线对照

| 红线 | 落地位置 |
| --- | --- |
| 日志不打印消息内容 | 全仓日志只打 message_id / 群号 / 异常类型 |
| 媒体失败不阻塞 | `BackupEngine._download_media` 内 try/except，计失败数 |
| 备份不在 Web 目录 | `storage/exports`，靠 token 转发 |
| 链接带 token 且过期 | `ExportManager._issue_token` / `resolve_token` |
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
