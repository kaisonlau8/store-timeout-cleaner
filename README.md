# store-timeout-cleaner

门店超时提醒机器人统计控制台。

## 功能

- 上传全量群清单与已拉超时提醒机器人的筛选群清单
- 自动统计全国门店群数量、各店群数量
- 支持按年月筛选，全量建群与拉机器人群同页比对
- 支持导出各维度 CSV 数据

## 部署

部署在远端 mac-studio，通过 Cloudflare Tunnel 暴露至：
http://127.0.0.1:5001

## 使用

1. 访问 http://127.0.0.1:5001/parse
2. 上传全量群清单（ZIP 或 XLSX）
3. 上传筛选群清单（ZIP 或 XLSX）
4. 点击解析，主页自动更新统计结果

## 技术栈

- Python 3 + Flask
- Pandas + OpenPyXL（Excel 解析）
- Jinja2 模板
- Cloudflare Tunnel
