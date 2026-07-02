# 定时任务配置

美股常规收盘时间为美东 16:00。建议在收盘后 30 分钟触发，给数据源留出结算缓冲。

## crontab 示例（服务器/本机时区已设为 America/New_York）

```cron
30 16 * * 1-5 cd /path/to/takefeed && /path/to/venv/bin/python run_daily.py >> logs/cron.log 2>&1
```

## 服务器时区不是美东时间时

用 `TZ` 前缀，避免因夏令时切换导致触发时间偏移：

```cron
30 16 * * 1-5 TZ=America/New_York cd /path/to/takefeed && /path/to/venv/bin/python run_daily.py >> logs/cron.log 2>&1
```

## 运行前准备

1. `pip install -r requirements.txt`
2. 配置环境变量 `ANTHROPIC_API_KEY`（叙事层需要；未配置时仍会生成打分和图表，仅叙事段落降级为提示文字）
3. 首次运行前确认能访问 Yahoo Finance（yfinance 数据源）与 AAII / CNN 页面（情绪抓取），
   部分网络环境（如企业代理、部分云 CI）会拦截这些域名，需要在允许出站访问的机器上运行
4. `output/site/` 是可直接部署到 GitHub Pages / Vercel 的静态站点目录，无需后端服务
