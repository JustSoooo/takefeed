# 监控 & 选股平台

个人量化决策支持系统。当前已实现：

- **V1 · 宏观/市场情绪仪表盘**：六维度打分（波动率/趋势/广度/信用/板块轮动/情绪）+ Claude
  生成的中文简报 + 静态仪表盘（`index.html`）
- **V2 · 行业轮动追踪**：11个 SPDR 板块 ETF 相对 SPY 的 1周/1月/3月超额收益矩阵、排名持续性
  判断、强势板块内部健康度（普涨还是靠权重股拉动）+ 静态仪表盘（`rotation.html`）
- **V3 · 个股综合评分**：watchlist（`config.yaml` 手工维护或 CSV 导入）里每只票的评分卡
  ——财务动量、机构态度（分析师目标价/评级变动）、相对板块的强度、事件日历（下次财报），
  加上近7日新闻的 Claude 归纳（仅三档标签 利多/利空/中性 + 一句话理由，原始标题永远由代码
  本地拼回，不依赖模型复述）+ 静态仪表盘（`stocks.html`）
- **V4 · 股池监控 + 异常预警**：对 watchlist 每日巡检六条规则——收盘价穿越20/50日均线、
  成交量突破近30日均量2倍、单日涨跌幅超近90日波动率2倍标准差、财报临近5个交易日内、
  当日分析师评级变动、新闻条数突破近30日日均3倍（复用 V3 已抓的数据，不重复取数）
  + 静态仪表盘（`alerts.html`）

其余模块（V5 期权分析）按 [guidebook](./docs) 中定义的顺序逐步开发。
这是决策支持系统，不接交易接口、不自动下单。推送通道（邮件/Telegram）是 guidebook 标注的
二期可选项，暂未实现——目前告警只进每日报告和网页仪表盘。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...   # 叙事层需要；未设置时打分和图表仍会生成

python run_daily.py
```

运行后：

- `output/site/index.html` / `rotation.html` / `stocks.html` / `alerts.html` — 静态仪表盘，
  可直接用浏览器打开，或部署到 GitHub Pages / Vercel
- `output/reports/daily_YYYYMMDD.md` — 当日报告存档（各模块共用一份文件）
- `db/market.sqlite` — 全部历史指标落库，用于后续校准评分权重

## 网络依赖

数据抓取需要能访问 Yahoo Finance（`yfinance`）、AAII、CNN 的页面，以及 SPDR/SSGA 的板块 ETF
持仓下载（V2 内部健康度用）。部分网络环境（企业代理、部分 CI/沙箱）会拦截这些域名，此时请在
有出站访问权限的机器上运行（本机或自建服务器均可）。定时任务配置见 [docs/cron.md](docs/cron.md)。

## 项目结构

```
config.yaml          # 全部权重/阈值/标的清单/watchlist，不硬编码在代码里
core/fetchers/        # 数据抓取层（yfinance 封装、广度计算、情绪抓取、板块持仓、个股基本面/新闻）
core/scoring/          # v1_composite.py + v2_rotation.py + v3_stock_card.py + v4_alerts.py
core/narrative/        # Claude API 层：V1 叙事转述 + V3 新闻三档分类，都集中在这一个文件
core/render/            # Jinja2 渲染仪表盘 + 每日 markdown 报告（按模块拆分 render_v1/v2/v3/v4）
core/watchlist.py      # V3/V4 共用的 watchlist 加载（config.yaml 列表 或 CSV 导入）
templates/, static/    # 前端模板与设计 token（详见 guidebook 第 7 节）
run_daily.py            # 编排入口：fetch -> score -> narrate -> render，V1/V2/V3/V4 依次执行
```

## 测试

```bash
python -m pytest tests/ -v
```

测试覆盖打分逻辑（含维度缺失时的权重重归一化）、SQLite 读写、以及渲染管线（用合成数据，
不依赖网络）。真实数据抓取需要在有网络访问的环境中手动验证。

## 校准期说明

评分体系的权重和阈值全部在 `config.yaml` 里，初始为等权重。按 guidebook 的 M2 里程碑，
建议连续跑 2-4 周后人工对照报告判断与实际走势，再调整权重。这套框架未经历史回测验证，
校准完成前不要把它当作决策的唯一依据。
