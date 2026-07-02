# 监控 & 选股平台

个人量化决策支持系统。当前已实现 **V1 · 宏观/市场情绪仪表盘**（六维度打分 + Claude 生成的
中文简报 + 静态 HTML 仪表盘），其余模块（V2 行业轮动 / V3 个股评分 / V4 股池监控 / V5 期权分析）
按 [guidebook](./docs) 中定义的顺序逐步开发。这是决策支持系统，不接交易接口、不自动下单。

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...   # 叙事层需要；未设置时打分和图表仍会生成

python run_daily.py
```

运行后：

- `output/site/index.html` — 静态仪表盘，可直接用浏览器打开，或部署到 GitHub Pages / Vercel
- `output/reports/daily_YYYYMMDD.md` — 当日报告存档
- `db/market.sqlite` — 全部历史指标落库，用于后续校准评分权重

## 网络依赖

数据抓取需要能访问 Yahoo Finance（`yfinance`）、AAII 和 CNN 的页面。部分网络环境（企业代理、
部分 CI/沙箱）会拦截这些域名，此时请在有出站访问权限的机器上运行（本机或自建服务器均可）。
定时任务配置见 [docs/cron.md](docs/cron.md)。

## 项目结构

```
config.yaml          # 全部权重/阈值/标的清单，不硬编码在代码里
core/fetchers/        # 数据抓取层（yfinance 封装、广度计算、情绪抓取）
core/scoring/          # 六维度打分 + 综合评分（纯计算，不依赖 LLM）
core/narrative/        # Claude API 叙事层，只做文字转述，不做数字计算
core/render/            # Jinja2 渲染仪表盘 + markdown 报告
templates/, static/    # 前端模板与设计 token（详见 guidebook 第 7 节）
run_daily.py            # 编排入口：fetch -> score -> narrate -> render
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
