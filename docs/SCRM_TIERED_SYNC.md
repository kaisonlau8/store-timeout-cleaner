# SCRM 分层同步（tiered sync）

本方案把原先每天 00:00 / 12:00 的一次性全量拉群拆成三层，降低对 SCRM OpenAPI 的瞬时压力。
原有的 `scripts/sync_scrm.py` / `app.run_sync()`（拉群 + 超时计算）保持原样，可继续运行。

## 调度

| tier | 时间 | 作用 |
|------|------|------|
| `groups` | 每小时 `HH:20` | 增量同步群名、群主/门店/战区/人数变化，新增机器人群和员工姓名；不删除 |
| `full` | 每天 `02:00` | 全量对账；解散群从 `chat_store` / `robot_chat` 消失，并清理缓存中的旧群成员 |
| `members` | `02:30, 06:30, 10:30, 14:30, 18:30, 22:30` | 更新群主/人数，刷新群成员与客户缓存 |

`blackout_ok(hm)` 在 `00:00`、`08:30`、`09:00`、`12:00` 前后各 5 分钟内返回 `False`，避免与既有任务抢资源。
`simulate_day()` 可按 1440 分钟模拟一天，正常为 24 次 groups + 1 次 full + 6 次 members。

## 运行

```bash
python scripts/sync_scrm_tier.py groups
python scripts/sync_scrm_tier.py members
python scripts/sync_scrm_tier.py full
python scripts/sync_scrm_tier.py groups --dry-run   # 只打印计划，不请求 SCRM
```

tier 自己的 flock（`ROOT/.sync-tier-groups.lock` 或 `ROOT/.sync-tier-members.lock`）被占用时，
日志输出 `skip: previous run still going`，进程以 0 退出。`full` 与 `groups` 共用 groups 锁。

## 限流与重试

`ThrottledScrmClient` 默认串行请求：

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `SCRM_SYNC_CONCURRENCY` | `1` | 全局并发，硬上限 `2` |
| `SCRM_SYNC_MIN_INTERVAL` | `0.2` | 相邻请求起始最小间隔（秒），另加 0~50% 随机抖动，约 300 请求/分钟 |
| `SCRM_TIER_LIMIT` | 空 | 仅用于开发/测试，限制 members 层群数和员工数 |
| `SCRM_CACHE_DB` | `~/m-hero-run/data/scrm_cache.sqlite3` | 成员/客户缓存库，自动 `chmod 600` |
| `SCRM_TIER_METRICS` | `~/m-hero-run/cron-logs/scrm-tier-metrics.jsonl` | 每次运行的 JSONL 指标 |

- HTTP `429`、`5xx`、`URLError`、超时、JSON 解析失败会重试，最多 5 次尝试。
- 退避为指数退避 + full jitter：基数 2 秒、上限 60 秒。
- 单次请求耗时超过 2 秒后，下一次请求前额外等待 `min(30, 本次耗时)` 秒。
- access_token 在单次运行内只获取一次；日志和指标均不记录 token/secret。

## 缓存库

members 层写独立的 SQLite：

- `scrm_group_member(chat_id, userid, type, join_time, join_scene, nick, name, invitor)`，主键 `chat_id + userid`
- `scrm_follow(external_userid, userid, name, type, gender, unionid, corp_name, remark, remark_mobiles, add_way, add_time, is_del, tags)`，主键 `external_userid + userid`

每轮先写 `*_staging`，再在事务中整表切换，读方不会看到半份数据。群成员/客户拉取错误率超过 5% 时
整轮失败，旧缓存保持不变；CLI 以非 0 退出。

## 锁与写入边界

- 所有网络请求都在 `.sync.lock` 之外完成。
- 对 `timeout.db` 的写入只发生在短暂临界区：普通 `fcntl.flock` 锁 `ROOT/.sync.lock`，最多等待 120 秒；每次写入是一个事务。
- 不使用 `app._SYNC_LOCK`，与 00:00 / 12:00 原任务的进程内锁隔离，但共用同一个文件锁。
- `full` 只调用 `sync_from_scrm(client=...)` + `timeout_store.replace_context`，不计算超时，也不改写 `data.json` 统计；`data.json` 仍由原 00:00 / 12:00 任务维护。

## 指标

每次运行（含 skip/error）向 `SCRM_TIER_METRICS` 追加一行 JSON，包含
`tier/start/end/status/counts/requests/ok/errors/retries/p50_ms/p95_ms/seconds`。
成功运行还会把同一份 JSON 写入 `timeout.db` 的 `sync_meta`，键为 `tier_<name>_last`。

## 回滚

1. 停用新增的三个 tier 定时任务即可恢复只跑原 `scripts/sync_scrm.py`；原任务完全未改。
2. 如需回退代码：`git revert <tiered-sync-commit>`，再运行一次 `scripts/sync_scrm.py` 重新全量覆盖 `chat_store`。
3. 缓存库可安全删除（`SCRM_CACHE_DB`），不影响 `timeout.db`、页面和超时计算；下次 members 会自动重建。
4. `full` 曾删除的解散群不会由缓存恢复，回滚后以原全量同步结果为准。
5. tier 锁文件 `.sync-tier-*.lock` 可删除；`.sync.lock` 由新旧任务共用，不要手工删除或替换。
