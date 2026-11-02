<p align="center">
  <img src="backup/assets/banner.svg" alt="Backup 高级备份" width="100%">
</p>

<p align="center">
  <img src="backup/assets/logo.svg" alt="logo" width="72">
</p>

# Backup 高级备份

> AstrBot 群聊数据备份插件。把群里的消息、文件、图片、成员、群信息全备份下来，支持定时自动备份、增量备份、多格式导出、一键恢复。群被炸了、机器人迁移、数据审计，都靠它。

<p align="center">
  <img src="backup/assets/icon.svg" width="18" alt="icon"> 消息 / 媒体 / 成员全量备份 ·
  <img src="backup/assets/icon.svg" width="18" alt="icon"> 增量备份 ·
  <img src="backup/assets/icon.svg" width="18" alt="icon"> 7 种导出格式 ·
  <img src="backup/assets/icon.svg" width="18" alt="icon"> HTML 聊天记录
</p>

## 功能

**消息备份**
- 群聊消息：文本 / 图片 / 语音 / 视频 / 文件 / 表情 / 合并转发 / 引用回复 / @
- 群公告、群文件列表、群成员（QQ号 / 昵称 / 群名片 / 入群时间 / 角色）、群信息
- 每条消息保留平台原始 JSON（`raw_content`）做保底
- 媒体文件自动下载，文件名 `{message_id}_{url哈希}{ext}`，记录大小 / MIME / SHA-256

**备份方式**
- 手动：`/backup now [群号]`
- 定时：`/backup schedule set <群号> "0 3 * * *"`
- 增量：以 `(timestamp, message_id)` 复合排序记录增量点，首次全量、之后增量
- 实时归档：消息进群即写库（只写库不下媒体），群被炸/被踢时仍有底
- 触发式：10 分钟内退群数超阈值、群解散、机器人被踢，自动补一次备份

**导出格式**

| 格式 | 产物 | 适合场景 |
| --- | --- | --- |
| `sqlite` | 单文件 `.sqlite` | 二次查询 / 数据分析 |
| `json` | 目录 + 按天分片 | 程序处理 |
| `csv` | 单文件（带 BOM） | Excel 直接打开 |
| `txt` | 单文件 | 纯文本归档 |
| `markdown` | 目录 + 按天分文件 | 阅读 / 存档进 Git |
| `html` | 自包含单文件 | 分享给群友看，仿聊天界面 |
| `zip` | 以上全部 + 媒体 | 完整打包迁移 |

## 安装

```bash
cd AstrBot/data/plugins
git clone <本仓库地址> astrbot-plugin-Backup
pip install -r astrbot-plugin-Backup/backup/requirements.txt
```

重启 AstrBot 后，插件会出现在插件列表中。

## 命令速查

| 命令 | 说明 |
| --- | --- |
| `/backup help` | 帮助 |
| `/backup now [群号]` | 立即备份（不填则当前群） |
| `/backup schedule set <群号> "<cron>"` | 设置定时备份 |
| `/backup schedule list` | 查看定时任务 |
| `/backup schedule remove <群号>` | 移除定时任务 |
| `/backup list [群号]` | 备份列表（含大小 / 时间 / 消息数） |
| `/backup export [群号] <格式>` | 导出，返回 24 小时有效的临时链接 |
| `/backup restore <备份ID> [目标群号] --confirm` | 恢复（危险操作，需二次确认） |
| `/backup status [群号]` | 当前群备份状态 |
| `/backup verify <备份ID> [备份包]` | 校验备份完整性 |
| `/backup diff <备份ID1> <备份ID2>` | 对比两个备份 |
| `/backup clean [天数]` | 清理旧备份 |

恢复的冲突策略：`--strategy skip`（默认，跳过已存在）/ `overwrite`（覆盖）/ `merge`（补齐缺失）。

## 配置

配置文件：`backup/config.yaml`

```yaml
backup:
  storage_dir: "storage"
  scope:
    group_messages: true      # 群消息
    private_messages: false   # 私聊（默认关）
    group_members: true       # 成员列表
    download_media: true      # 下载图片/语音/视频/文件
  media:
    max_file_size: "100MB"    # 单文件上限，超过跳过
    max_concurrent: 3         # 并发下载数
    retry_count: 3
    timeout: 30
  schedule:
    default_cron: "0 3 * * *"
  retention:
    keep_days: 90
    keep_count: 30
    auto_clean: true
  export:
    download_link_expire: "24h"
  permission:
    require_admin: true       # 仅群主/管理员可用
  incremental:
    enabled: true
  auto_trigger:
    mass_leave_threshold: 10  # 10 分钟内退群超过该数触发备份
    on_group_disband: true
```

## 数据存放

```
backup/storage/
├── db/backup.sqlite   # 消息、成员、群信息、备份记录、增量点、定时任务
├── media/             # 媒体文件（按日期分目录）
└── exports/           # 导出产物（不对外暴露，靠带 token 的临时链接访问）
```

## 常见问题

**Q：备份会占多少空间？**
A：文本很少，主要是媒体。不想存媒体就把 `scope.download_media` 关掉，只留文本。

**Q：增量备份会漏消息吗？**
A：不会。增量点用 `(timestamp, message_id)` 复合比较，同一毫秒内的多条消息也能区分。

**Q：媒体下载失败会中断备份吗？**
A：不会。失败的文件标记为 `[下载失败]` 并计入记录，备份状态变 `partial`，消息本身照常入库。

**Q：HTML 能直接发给别人看吗？**
A：能。CSS/JS/logo 全部内联，图片 base64 内嵌，单个 `.html` 文件就是完整备份。

**Q：恢复会覆盖现有数据吗？**
A：默认 `skip` 策略不动已有数据。恢复前会二次确认 + 校验完整性，损坏的备份直接拒绝。

**Q：群号换了还能恢复吗？**
A：可以。`/backup restore <备份ID> <新群号> --confirm`。

## 开发与测试

```bash
pip install pytest pytest-asyncio coverage
python -m pytest -q
python -m coverage run --source=backup/core,backup/exporters -m pytest -q && python -m coverage report
```

覆盖率要求 ≥ 80%。`backup/main.py` 是 AstrBot 接线层，不进覆盖率统计口径。

项目结构：

```
backup/
├── metadata.yaml          # 插件元数据
├── main.py                # 命令注册
├── config.yaml            # 默认配置
├── assets/                # SVG 品牌素材（logo / icon / banner）
├── core/                  # 备份、恢复、导出调度、解析、校验、调度器
├── exporters/             # 6 种格式导出器 + ZIP
├── templates/chat.html    # HTML 导出模板（内联样式与脚本）
├── storage/               # 运行时数据（git 忽略）
└── tests/                 # pytest
```

## 贡献

1. Fork 本仓库，切出功能分支
2. 保持 `python -m pytest -q` 全绿，覆盖率不掉
3. 不打印消息内容到日志（隐私红线）
4. 提交 PR，描述里写清改了什么、为什么

## 作者

**科技酱** — 同一个世界，同一个互联网！

| 渠道 | 地址 |
| --- | --- |
| 博客 | https://www.asoe.cn |
| 技术文档 | https://docs.asoe.cn |
| GitHub | https://github.com/techjiang/ |
| 哔哩哔哩 | https://space.bilibili.com/1768832152 |
| 邮箱 | official@tech.asoe.cn |
| 玲珑社区 | https://forums.asoe.cn/ |

QQ 群：291974598 ｜ 474819022 ｜ 631167288

## License

MIT © 科技酱
