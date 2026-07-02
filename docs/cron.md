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
2. 配置环境变量 `ZHIPU_API_KEY`（叙事层需要，接入智谱 GLM；未配置时仍会生成打分和图表，仅叙事段落降级为提示文字）
3. 首次运行前确认能访问 Yahoo Finance（yfinance 数据源）与 AAII / CNN 页面（情绪抓取），
   部分网络环境（如企业代理、部分云 CI）会拦截这些域名，需要在允许出站访问的机器上运行
4. `output/site/` 是可直接部署到 GitHub Pages / Vercel 的静态站点目录，无需后端服务

## 托管方案：GitHub Actions + GitHub Pages（推荐，不依赖本机常开）

`.github/workflows/daily.yml` 已经实现了这条路径：定时（工作日 UTC 20:30，约等于美东
收盘后 30 分钟，冬令时会提早一小时，见文件内注释）在 GitHub 的 runner 上跑
`run_daily.py`，把 `db/market.sqlite`（历史指标，评分体系依赖它做百分位/持续性判断）
提交回本分支，再把 `output/site/` 部署到 GitHub Pages。也可以在 Actions 页面手动
`workflow_dispatch` 触发一次，不用等定时。

**仓库所有者需要手动做的一次性设置（Actions 权限不够改这两处）：**

1. Settings → Secrets and variables → Actions → New repository secret，
   添加 `ZHIPU_API_KEY`
2. Settings → Pages → Build and deployment → Source，选择 **GitHub Actions**

**注意**：仓库当前是 public，GitHub Pages 免费版部署出来的页面对所有人公开可访问
（watchlist、评分、期权分析都会暴露）。如果介意，需要转成私有仓库并换用支持访问控制
的托管方案（如 Vercel + 密码保护），GitHub Pages 免费版不支持私有仓库的受限访问。
