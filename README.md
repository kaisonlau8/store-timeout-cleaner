# 门店超时提醒机器人统计控制台

从企微 SCRM 生产 OpenAPI 拉取客户群，统计「已拉超时提醒机器人」覆盖；超时提醒次数来自管家后台导出的「回复超时记录」Excel。

> 归属猛士服务运营工具集。总文档地图与依赖关系见 [m-hero](https://github.com/kaisonlau8/m-hero)。接口说明见 [scrm-api](https://github.com/kaisonlau8/scrm-api)。

## 入口

| 项 | 值 |
|----|-----|
| 拉群覆盖 | `http://127.0.0.1:5001/` |
| 超时提醒 | `http://127.0.0.1:5001/timeout` |
| 群名统计 | `http://127.0.0.1:5001/groups` |
| 超时评分 | `http://127.0.0.1:5001/score` |
| Tunnel | `store-timeout-cleaner` → `:5001`（hostname 仅写本机 cloudflared） |

本服务**不爬取 DMS**，也不使用共享 Chromium 会话。

## 快速开始

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填入与 scrm-api 相同的生产凭证
python app.py
```

浏览器打开 http://127.0.0.1:5001 。拉群覆盖由 launchd 每天 **00:00 / 12:00**（北京时间）从 SCRM 同步。超时提醒把管家导出放到 `input_file/` 或在控制台上传。

未单独配置 `.env` 时，会回读同级 `scrm-api/.env`。

## 数据口径

| 集合 | 来源 |
|------|------|
| 全量客户群 | `GET /openapi/chat/group/list` |
| 已拉机器人 | 同上，群成员含刘明轩（`user_type=1`） |
| 门店 | 群主主部门路径中以「猛士」开头的一段 |
| 战区 | 部门路径里「门店」的下一段（如武汉特区、华北战区） |
| 超时提醒 | 管家「回复超时记录」Excel |
| 群名关键字统计 | `timeout.db` 的 `chat_store` 表（SCRM 每次同步写入的全量客户群） |

机器人账号只认刘明轩：`.env` 的 `SCRM_ROBOT_USERIDS=25738`（或 `SCRM_ROBOT_USERNAME=刘明轩`）。

### 超时提醒统计

OpenAPI 没有超时记录查询接口。使用管家导出的 Excel：

1. 文件名必须带时间范围，例如 `导出时间范围20260701-20260731回复超时记录.xlsx`
2. 导入时按该区间**整段覆盖**并登记（文件、起止日、行数、未归店数）
3. 用「所在会话」对上 SCRM 群名，归到门店 / 战区；对不上的记入未归店（含「未知群聊」）
4. 消息数按触发时间 + 群名 + 消息内容去重；规则次数按「触发超时规则」各计一次

曲线图可按全国 / 战区 / 门店筛选，粒度支持日、月、季。

### 门店超时评分

打开 http://127.0.0.1:5001/score 。评分、超时统计及门店超时区间默认本月 1 日至今天（北京时间）。日期范围控件支持本月、上月、本季度、当年 Q1–Q4、近 30 天；尚未结束的季度截至今天，未来季度不可选。旧链接的 start/end 仍兼容。日期必须在 2026-01-01 至今天之间、开始不晚于结束、含首尾不超过 366 天；非法页面请求返回带提示的 400，API 返回 400 JSON。列表给出店名、战区、机器人群数、原始超时、规则过滤后的有效超时、超时率、分档和得分，可导出 CSV / Excel。点店名进入明细，分成「计入」和「剔除」（带剔除原因）两段。

分档只在有机器人群、且不是下面两种特殊店的门店之间进行。超时率从低到高排名，同率并列取较高档：

| 名次 | 得分 |
|------|------|
| ≤ 店数的 20% | 100 |
| 20% 以上、≤ 50% | 80 |
| 其余 | 60 |

超时率 = (有效超时 + 20×μ) / (机器人群数 + 20) × 100。μ 是这些参与排名门店的有效超时合计 / 机器人群合计。

另外三种情况不进这套排名：

- 有客户群、但机器人群数为 0：0 分（未接入机器人）
- 徐州泉山、六安金安、杭州西溪、上海闵行这四家交付店自己没有机器人群时，并入同城零售中心的机器人群数和超时后再排名
- SCRM 里对不上的店：取已排名门店得分的平均分

有效超时从已导入的超时事件出发，按顺序剔除：重复触发、疑似员工/营销话术、寒暄/结束语/纯表情、仅 @ 人、同会话 30 分钟内连发。话术类规则需要能从 `input_file/` 的 Excel 对上消息正文。

规则留下的消息会自动交给 DeepSeek（`deepseek-chat`）做增量复核。密钥放在环境变量 `DEEPSEEK_API_KEY`（写入本目录 `.env`，不要提交）。没配密钥时评分照常计算，页面标明「LLM复核关闭」，不会请求接口。配了密钥后，超时 Excel 导入完成，以及打开评分页（或门店明细）时如果当前区间还有未缓存正文，都会在后台只补齐这些正文。已经按正文哈希加提示词版本写入 `timeout.db` 的结果不会再次发送。页面默认把缓存里「不计入」的消息从有效超时里去掉，并标明「LLM复核已自动计入」；缓存还没齐时先按已有结果出分，补齐后页面自动刷新。评分页没有复核按钮，也不能切换是否计入 LLM 剔除。需要整段重跑时，管理接口 `POST /api/score/llm-review?start=YYYY-MM-DD&end=YYYY-MM-DD&force=1` 会在后台强制刷新。剔除原因形如 `LLM复核剔除: 闲聊`。导出表多一列「LLM剔除」。

`scripts/sync_timeout.py` 在扫描导入之后会等这批后台复核结束再退出，避免一次性进程把任务丢掉。首次打开无任务的评分区间可启动后台增量复核；已完成或失败的任务不会被 GET 访问重复启动，后续数据更新由导入/同步或显式复核接口处理。只有页面加载时正在运行的任务完成后，页面才保留完整当前 URL 刷新一次；轮询最多 120 次、连续失败 3 次后停止。

客户消息正文会在自动复核时发送到 DeepSeek，不写入仓库。

```bash
# 扫描 input_file/ 并导入
venv/bin/python scripts/sync_timeout.py
```

导出文件含客户消息，**不要提交** `input_file/`。

### 群管理

`/groups` 的「群列表」Tab 在查询按钮旁提供「导出 Excel」，按当前群名 / 群 ID、战区、门店和机器人状态筛选导出全部匹配行（`GET /api/groups/list/export?q=&theater=&store=&robot=all`），不受分页限制。

「关键字统计」Tab 按客户群名称做关键字子串统计（默认关键字 `X700-VIP`，大小写不敏感）。

1. 数据来自 `timeout.db` 的 `chat_store` 表（SCRM 每次同步写入的全量客户群），只读查询，不新增外部数据源
2. 支持战区筛选（作用于所有统计）与「仅已拉机器人群」过滤；关键字写入 URL query，便于分享
3. 展示匹配群总数 / 已拉机器人群数 / 涉及门店数 / 未归店群数四张卡片，以及按战区、按门店两张明细表；`store` 或 `theater` 为空的群单列「未归店群」
4. 点击按战区表行、或按门店表的战区单元格，可按该战区重新筛选
5. `GET /api/groups/keyword/export` 导出 Excel（`群名统计_<关键字>.xlsx`，含汇总 / 按战区 / 按门店 / 未归店群 / 群明细五个 sheet）

开发调试可用环境变量 `CLEANER_DB_PATH`、`CLEANER_DATA_FILE` 指向真实数据副本，不影响默认路径。

## 功能概要

- 每天 00:00 / 12:00 从 SCRM 同步客户群覆盖（供超时记录归店）
- 控制台：拉群覆盖 + 超时 Excel 导入 / 曲线 / 门店明细 + 群名关键字统计
- CSV 导出（覆盖总表 / 超时门店表）；群名统计导出 Excel

数据文件（本地，默认不提交）：

| 路径 | 说明 |
|------|------|
| `data.json` | 最近一次拉群覆盖统计 |
| `timeout.db` | 超时事件与导出登记 |
| `input_file/` | 管家导出的 Excel |

## 周报（默认关闭、dry-run）

周报只读本地 `timeout.db` / JSON 快照和分层同步 JSONL，不调用 SCRM。
`weekly_report.json` 默认 `enabled=false`、`dry_run=true`，不会启动后台线程或自动发送。
手工 CLI 不受 enabled 限制；只有明确 `--send` 或将配置 dry_run 改为 false 才会发送。
本次开发验证不发飞书、不部署；上线前需另行确认权限及配置。

```bash
python scripts/run_weekly_report.py --week 2026-10-19 --dry-run \
  --chat-store-json /path/to/chat_store_snap.json \
  --metrics-jsonl /path/to/scrm-tier-metrics.jsonl \
  --timeout-summary-json /path/to/week_timeout_summary.json \
  --out /path/to/output
# 默认读取 CLEANER_DB_PATH（未配置为 timeout.db），只读打开；上一完整周
python scripts/run_weekly_report.py --dry-run
```

产物：`YYYY-MM-DD_weekly.xlsx`、`brief.txt`、`YYYY-MM-DD_brief.txt`。
默认输出 `data/weekly_reports/`（已忽略，不提交群名/群主等信息）。
显式 `--chat-store-json` 时不会偷偷回读默认 DB；如需同读超时数据，显式传 `--db-path`。
`--timeout-summary-json` 接受 `events`、`valid`、`by_theater`、`valid_by_theater`，后两者为战区/数字对的列表或映射；总数须与分战区合计一致。
该汇总无日期元数据时由操作者确保与 `--week` 一致；缺少超时数据会在简报注明。

口径：北京时间周一 00:00 至下周一 00:00（不含）；本周新建以 `create_time` 判定，
无时区按北京时间、带时区先转北京时间，缺失/非法时间不纳入新建并注明。
期末覆盖是运行时 chat_store 快照，**不是历史周日重建**；门店按非空店名去重。
“新建且已拉机器人”取当前 `in_robot=1`；`robot_rooms_added` 为 groups 同步增量（包含历史群），不可混用。
metrics 仅选 start 在周内的 groups 记录，非 success 均计失败；updated_groups/groups_updated 只取一个，不重复加。
首末快照严格取周内首末记录 counts，字段缺失不推算。超时按事件逐条计数，exclude_reason 为空视为有效，不额外按消息去重或套评分过滤。
JSONL 损坏/缺失则拒绝生成，不把缺失数据当作零。

配置支持 `--config /path/to/local.json` 覆盖默认 JSON。常用字段：

- `weekday/hour/minute`：默认周一 09:40；拒绝 00:00、08:30、09:00、09:30、12:00 前后 5 分钟（含跨午夜）。
- `metrics_path`：优先读 `STC_METRICS_PATH`，其次配置，再回落 `~/m-hero-run/cron-logs/scrm-tier-metrics.jsonl`。
- `link_base`：优先读 `STC_LINK_BASE`；公网地址仅在环境/未入库配置中设置，默认生成相对下载链接。
- `chat_id`：默认建议“售后服务运营”；只影响显式发送。
- `state_path`：默认 `data/weekly_reports/state.json`，相对路径以仓库根目录为基准；`--state-path` 可覆盖。
- `sending_timeout_seconds`：默认 21600（6 小时）。

`/weekly-reports` 列表及 `/weekly-reports/<file>` 下载走现有 m-hero SSO，未将它们加入公开路径；
即使 auth 为 off/audit，没有 `g.mhero_session` 也返回 401。上线需启用 `MHERO_AUTH_CLEANER=on`，
沿用现有 SSO 配置，不能用静态目录公开文件。仅允许规范日期 XLSX 文件名，禁止路径穿越和符号链接下载。

去重在跨进程非阻塞 fcntl 锁内完成，生成文件期间也持锁。
sent 阻止重复发送；sending 在 6 小时内阻止重发，无效/未来时间戳保守阻止；
dry_run/failed 不阻止真实发送。预览不覆盖 sent/sending。
`--send` 与 `--dry-run` 互斥；`--force` 可跳过去重，但不能跳过锁或损坏状态校验。
状态用 fsync + 同目录原子替换；先落 sending 再发消息，发送成功后落 sent 失败会保留 sending。
强制重发失败不抹掉旧 sent/sending。调用超时存在“远端已收到但本地未知”的风险，重试前人工核实。

飞书凭证仅来自环境 `FEISHU_APP_ID/FEISHU_APP_SECRET`（兼容 `APP_ID/APP_SECRET`），不回读其他仓库凭证，
不在状态或日志中记录 token/secret。需由运行器注入环境变量。

### 上线 cron 示例（仅说明，不修改生产）

本仓库不增加应用内调度。审核上线时，在外部 `~/m-hero-run/launchers/cron.py` 按其现有命令格式增加：

```python
"cleaner-weekly-report": (["09:40"], [PYTHON, str(CLEANER_ROOT / "scripts/run_weekly_report.py"), "--scheduled"]),
```

此为条目示意，PYTHON/CLEANER_ROOT 需对照运行器实际定义。每天 09:40 调用，
`--scheduled` 在北京时间校验 enabled/weekday/hour/minute，不到点直接跳过。
先保持 dry_run=true，确认本地输出、SSO 下载及数据口径；再经批准配置 enabled=true、dry_run=false，
核实群 ID、凭证、metrics 路径和下载基址。不要与其他发送任务共用独立状态路径以绕过去重。

测试（出站网络被阻断，发送用 mock，后台评分线程在每个测试结束前收尾）：

```bash
/tmp/opencode/stc-weekly-venv/bin/python -m pytest -q
```

## 部署

launchd：`com.store-timeout-cleaner.web`（控制台）、`com.store-timeout-cleaner.sync`（每天 00:00 / 12:00 同步拉群覆盖）  
plist：`deploy/com.store-timeout-cleaner.sync.plist`  
WorkingDirectory：本仓库根目录；Cloudflare Tunnel：`com.cloudflare.cloudflared.store-timeout-cleaner`。

黄页本地入口：`http://127.0.0.1:9004`
