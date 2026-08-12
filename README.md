# 门店超时提醒机器人统计控制台

解析门店超时提醒机器人相关导出数据，统计「已拉超时提醒机器人」等指标，提供 Web 控制台查看与 CSV 导出。

> 归属猛士服务运营工具集。总文档地图与依赖关系见 [m-hero](https://github.com/kaisonlau8/m-hero)。

## 入口

| 项 | 值 |
|----|-----|
| 本地 | `http://127.0.0.1:5001` |
| Tunnel | `store-timeout-cleaner` → `:5001`（hostname 仅写本机 cloudflared） |

本服务**不爬取 DMS**，也不使用共享 Chromium 会话。

## 快速开始

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

浏览器打开 http://127.0.0.1:5001 。

## 功能概要

- 上传 / 解析超时提醒相关 Excel
- 控制台展示汇总与明细（含 2026-05-01 后新建等口径）
- CSV 导出（单表 / 总表）

数据文件：

| 路径 | 说明 |
|------|------|
| `data.json` | 最近一次统计结果（本地，默认不提交） |
| `uploads/` | 上传原始文件（本地，默认不提交） |

## 部署

launchd：`com.store-timeout-cleaner.web`  
WorkingDirectory：本仓库根目录；Cloudflare Tunnel：`com.cloudflare.cloudflared.store-timeout-cleaner`。

黄页本地入口：`http://127.0.0.1:9004`
