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

- **V5 · 期权分析**：按需运行的分析工具（`run_options.py`，不进每日巡检）——期权链浏览
  （流动性硬过滤 + IV 期限结构）、情景收益预估（BS 中途估值、双 IV 情景、±5%价格 ×
  ±5交易日敏感性矩阵、payoff 图、跨财报 IV crush 警示）、3×3 腿策略推荐矩阵（到期分桶 ×
  风险偏好，最大亏损置于最显著层级，保守×末日硬编码拒绝）。同时回填 guidebook 5.4：
  V3 评分卡新增"期权市场情绪"字段，V4 新增 IV 单日跳升与期权成交量异常两条预警

五个模块全部落地。这是决策支持系统，不接交易接口、不自动下单。推送通道（邮件/Telegram）
是 guidebook 标注的二期可选项，暂未实现——目前告警只进每日报告和网页仪表盘。

## V5 期权数据源说明

guidebook 指定 financial-service 为首选数据源、yfinance 为降级源。financial-service 是
本地 Claude Code 里的 Anthropic 金融分析 skill（非 HTTP 服务），因此采用**快照文件契约**
对接：在本地 Claude Code 会话中让 Claude 用该 skill 按
[docs/financial_service_snapshot.md](docs/financial_service_snapshot.md) 的 schema 生成
`data/options_snapshots/<SYMBOL>.json`（文档里有可直接粘贴的提示词），管道读文件并强制
校验 schema 与新鲜度（默认超过 24 小时拒用）。快照目录不存在且 `v5.allow_fallback: true`
时显式告警后降级 yfinance，设为 `false` 则直接报错，绝不静默换源。

```bash
# 期权链 + 推荐矩阵
python run_options.py --symbol NVDA

# 完整情景分析（预期价格 + 预期日期，可选 --position 自定义 1-4 腿）
python run_options.py --symbol NVDA --expected-price 210 --expected-date 2026-08-21
```

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
core/fetchers/        # 数据抓取层（yfinance 封装、广度/情绪/板块持仓/个股/期权链，含 options 抽象接口）
core/scoring/          # v1_composite.py + v2_rotation.py + v3_stock_card.py + v4_alerts.py
core/options/           # V5 数学层：BS 定价/希腊值、情景引擎、3×3 推荐引擎、期权情绪快照
core/narrative/        # Claude API 层：V1 叙事转述 + V3 新闻三档分类，都集中在这一个文件
core/render/            # Jinja2 渲染仪表盘 + 每日 markdown 报告（render_v1/v2/v3/v4/v5）
core/watchlist.py      # V3/V4 共用的 watchlist 加载（config.yaml 列表 或 CSV 导入）
templates/, static/    # 前端模板与设计 token（详见 guidebook 第 7 节）
run_daily.py            # 每日编排入口：V1/V2/V3(含期权情绪快照)/V4 依次执行
run_options.py          # V5 按需分析入口（期权链/情景预估/推荐矩阵 -> options.html）
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
