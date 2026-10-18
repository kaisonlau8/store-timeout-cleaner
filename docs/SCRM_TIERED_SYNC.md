# SCRM 分层同步（tiered sync）

本方案把原先每天 00:00 / 12:00 的一次性全量拉群拆成三层，降低对 SCRM OpenAPI 的瞬时压力。
原有的 `scripts/sync_scrm.py` / `app.run_sync()`（拉群 + 超时计算）保持原样，可继续运行。

## 调度

| tier | 时间 | 作用 |
|------|------|------|
| `groups` | 每小时 `HH:20` | 增量同步群名、群主/门店/战区/人数变化，新增机器人群和员工姓名；不删除 |
| `full` | 每天 `02:00` | 全量对账；解散群从 `chat_store` / `robot_chat` 消失，并清理缓存中的旧群成员 |
| `members` | `02:30, 06:30, 10:30, 14:30, 18:30, 22:30` | 更新群主/人数，刷新群成员与客户缓存 |

启动黑名单为 `00:00`、`08:30`、`09:00`、`12:00` 前后各 5 分钟（含边界）。
`blackout_ok(hm)` 判断某分钟是否允许启动，`_in_start_blackout()` 判断当前时刻是否命中启动黑名单；`simulate_day()` 在 1440 分钟内模拟调度，
正常为 24 次 groups + 1 次 full + 6 次 members。

## 运行

```bash
python scripts/sync_scrm_tier.py groups
python scripts/sync_scrm_tier.py members
python scripts/sync_scrm_tier.py full
python scripts/sync_scrm_tier.py groups --dry-run   # 只打印计划，不请求 SCRM
python scripts/sync_scrm_tier.py groups --force     # 手动忽略「启动」黑名单
```

tier 自己的 flock（`ROOT/.sync-tier-groups.lock` 或 `ROOT/.sync-tier-members.lock`）被占用时，
日志输出 `skip: previous run still going`，进程以 0 退出。`full` 与 `groups` 共用 groups 锁。
启动黑名单命中时日志输出 `skip: ... 处于启动黑名单`，同样以 0 退出；`--force` 只跳过启动检查。

### 启动黑名单与运行中避让

- **启动**：`run_tier()` / CLI 在 `00:00`、`08:30`、`09:00`、`12:00` ±5 分钟内拒绝启动，记录 skip 并退出 0；`--force` 可人工覆盖。
- **运行中**：所有 HTTP 请求在真正发出前检查 `00:00` / `12:00` ±5 分钟，命中时先睡到窗口结束，绝不抢占原任务的资源。
- **写库**：`_timeout_transaction()` 在取 `ROOT/.sync.lock` **之前**等待同一个运行中黑名单；等待期间不持有 `.sync.lock`。
  取到锁后还会再检查一次，若刚好进入黑名单则立刻释放锁、等完窗口后重试，原任务在 00:00 / 12:00 的非阻塞取锁不会看到 busy。
  timeout.db 连接的 busy timeout 限制为 5 秒；单个写事务超过 30 秒会回滚并中止，避免长期占用文件锁。
- **最大运行时间**：`SCRM_TIER_MAX_SECONDS` 对当轮生效，未设置时 groups/full 为 900 秒、members 为 10800 秒。
  超时在发布前抛出 `ScrmTierError`，不写 `chat_store` / `robot_chat` / 成员缓存（指标记为 error，CLI 非 0）。

## 限流与重试

`ThrottledScrmClient` 默认串行请求，并同时使用进程内信号量与跨进程文件锁：

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `SCRM_SYNC_CONCURRENCY` | `1` | 全局并发，硬上限 `2`；2 时使用两个跨进程槽位 |
| `SCRM_SYNC_MIN_INTERVAL` | `0.2` | 相邻请求起始最小间隔（秒），另加 0~50% 随机抖动，约 300 请求/分钟 |
| `SCRM_TIER_LIMIT` | 空 | 仅用于开发/测试，限制 members 层群数和员工数，并改为按 unit 合并 |
| `SCRM_TIER_MAX_SECONDS` | 见上 | 单 tier 硬超时 |
| `SCRM_FULL_MIN_RATIO` | `0.95` | full 层允许的最小群数量比例 |
| `SCRM_CACHE_DB` | `~/m-hero-run/data/scrm_cache.sqlite3` | 成员/客户缓存库，自动 `chmod 600` |
| `SCRM_TIER_METRICS` | `~/m-hero-run/cron-logs/scrm-tier-metrics.jsonl` | 每次运行的 JSONL 指标 |

### 跨进程全局节流

每个 HTTP 请求（含重试）都必须拿到一个网络槽位 flock：

- 并发 1：只使用 `ROOT/.scrm-net.lock`（slot0）；
- 并发 2：使用 `ROOT/.scrm-net.lock`（slot0）+ `ROOT/.scrm-net.1.lock`（slot1）。

槽位名与进程的并发设置无关。并发 1 与并发 2 的进程混跑时也共享同一组 slot0/slot1，因此全局最多 2 个请求。

槽位文件内保存最近一次请求开始时间戳（wall clock）。请求方持有 flock 等待 `min_interval + jitter`，
写入本次开始时间，然后**继续持有 flock 完成 HTTP 请求**才释放。因此多个 tier 进程合计最多 1 个请求
（并发 2 时最多 2 个），进程内 `threading.BoundedSemaphore` 仍保留作为第二道限制。

### 重试

- HTTP `429` / `503` 若带 `Retry-After`（秒），优先按该值等待，最长 120 秒；没有或非法时使用指数退避。
- 其余 `5xx`、`URLError`、超时、JSON 解析失败会重试，最多 5 次尝试。
- 退避为指数退避 + full jitter：基数 2 秒、上限 60 秒。
- 单次请求耗时超过 2 秒后，下一次请求前额外等待 `min(30, 本次耗时)` 秒。
- access_token 在单次运行内只获取一次；日志和指标均不记录 token/secret。`_redact()` 同时覆盖
  `key=value`、URL query 和 JSON 形式，掩码 `token` / `secret` 相关键、`appid`、`app_secret`、`password`。

## 完整性保护

- **full 层**：发布前检查（1）群列表非空；（2）`len(rows) >= SCRM_FULL_MIN_RATIO * 当前 chat_store 行数`；
  （3）`robot_chat_ids` 非空，除非当前 `robot_chat` 也是空的。任一不满足即中止，不写任何表、不清理缓存。
- **groups 层**：`robot_chat_ids` 为空时只跳过机器人相关更新（`in_robot` / `robot_chat`），仍更新普通群字段。

## groups 层的更新范围

- 新群插入 `chat_store`。
- 已存在群的 `group_name`、`owner_id`、`owner_name`、`owner_dept`、`store`、`theater`、`member_count`
  有任一变化才 UPDATE，并报告 `updated_groups` / `groups_updated`（名称变化另计 `renamed_groups`）。
- 当前机器人群统一置 `in_robot=1` 并补 `robot_chat` 行；机器人列表非空时，已不在列表中的旧行置 `in_robot=0`。
  groups 层从不删除 `chat_store` / `robot_chat` 行。

## 缓存库

members 层写独立的 SQLite：

- `scrm_group_member(chat_id, userid, type, join_time, join_scene, nick, name, invitor)`，主键 `chat_id + userid`
- `scrm_follow(external_userid, userid, name, type, gender, unionid, corp_name, remark, remark_mobiles, add_way, add_time, is_del, tags)`，主键 `external_userid + userid`

每轮先写 `*_staging`，再在事务中切换，读方不会看到半份数据。

- 单个 `chat_id` / `userid` 拉取失败时，从既有主表复制该 unit 的旧行进入本轮 staging，失败不会丢上一轮数据。
- 错误率同时按 unit 数（`errors / units`）和旧行数加权（失败 unit 的旧行数 / 旧行总数，并分别看成员/客户旧行）计算，任一超过 5% 整轮失败，旧缓存保持不变。
- 设置 `SCRM_TIER_LIMIT` 时只替换本轮实际处理的 unit，其他 unit 的成员/客户行原样保留；未设置时整表替换。
- 发布成员前阻塞获取 groups tier 锁（`.sync-tier-groups.lock`，最多等待 20 分钟，超时则中止发布且不改动旧数据）；
  在锁内重新短读 `chat_store`，丢弃 `chat_id` 已不在其中的成员行，并在同一临界区完成 `chat_store` 更新与缓存发布，
  避免 members 与 full/groups 竞态把已删除群写回来。
- CLI 失败时以非 0 退出。

## 锁与写入边界

- 所有网络请求都在 `.sync.lock` 之外完成；HTTP 只持有 `.scrm-net*.lock` 槽位。
- 对 `timeout.db` 的写入只发生在短暂临界区：普通 `fcntl.flock` 锁 `ROOT/.sync.lock`，最多等待 120 秒；每次写入是一个事务。
- `_timeout_transaction()` 在 00:00 / 12:00 ±5 分钟先等待，且等待过程中不持有 `.sync.lock`；取锁后再次检查，
  若命中黑名单则释放锁等待后重试。事务 busy timeout 为 5 秒，超过 30 秒即回滚中止。
- 不使用 `app._SYNC_LOCK`，与 00:00 / 12:00 原任务的进程内锁隔离，但共用同一个文件锁。
- `full` 只调用 `sync_from_scrm(client=...)` + `timeout_store.replace_context`，不计算超时，也不改写 `data.json` 统计；`data.json` 仍由原 00:00 / 12:00 任务维护。

## 指标

每次运行（含 skip/error）向 `SCRM_TIER_METRICS` 追加一行 JSON，包含
`tier/start/end/status/counts/requests/ok/errors/retries/p50_ms/p95_ms/seconds`。
成功运行还会把同一份 JSON 写入 `timeout.db` 的 `sync_meta`，键为 `tier_<name>_last`。
groups 的 `counts` 额外包含 `updated_groups`、`in_robot_updated`、`robot_rooms_added`；members 额外包含
`row_error_rate`、`failed_old_rows`、`old_rows_total`。

## 回滚

1. 停用新增的三个 tier 定时任务即可恢复只跑原 `scripts/sync_scrm.py`；原任务完全未改。
2. 如需回退代码：`git revert <tiered-sync-commit>`，再运行一次 `scripts/sync_scrm.py` 重新全量覆盖 `chat_store`。
3. 缓存库可安全删除（`SCRM_CACHE_DB`），不影响 `timeout.db`、页面和超时计算；下次 members 会自动重建。
4. `full` 曾删除的解散群不会由缓存恢复，回滚后以原全量同步结果为准。
5. tier 锁文件 `.sync-tier-*.lock`、网络槽位 `.scrm-net*.lock` 可删除；`.sync.lock` 由新旧任务共用，不要手工删除或替换。
