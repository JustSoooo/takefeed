# financial-service 期权快照契约

financial-service 是安装在本地 Claude Code 里的 Anthropic 金融分析 skill。它服务于
Claude 会话，不是可被 Python 直接调用的网络服务，因此本平台与它之间用**快照文件**桥接：

```
本地 Claude Code（financial-service skill 取数）
  → 写 data/options_snapshots/<SYMBOL>.json（本文档定义的 schema）
  → run_options.py / run_daily.py 读文件（校验 schema + 新鲜度后使用）
```

## 生成快照：在本地 Claude Code 里说

在仓库根目录打开 Claude Code，直接粘贴（换掉标的代码即可）：

> 用 financial-service skill 获取 NVDA 的期权链数据：标的现价、最近 8 个到期日的全部
> call 和 put 合约（每个合约要行权价、买价、卖价、最新价、当日成交量、持仓量、年化隐含
> 波动率的小数形式），如果 skill 能提供无风险利率也带上。然后严格按照
> docs/financial_service_snapshot.md 里的 JSON schema 写入
> data/options_snapshots/NVDA.json，fetched_at 用当前 UTC 时间。

之后正常运行 `python run_options.py --symbol NVDA` 即可，管道会自动读取快照。

## JSON Schema

```json
{
  "symbol": "NVDA",
  "spot": 187.32,
  "fetched_at": "2026-07-02T20:30:00Z",
  "risk_free_rate": 0.042,
  "quotes": [
    {
      "contract_symbol": "NVDA260821C00190000",
      "kind": "call",
      "expiry": "2026-08-21",
      "strike": 190.0,
      "bid": 5.10,
      "ask": 5.30,
      "last": 5.22,
      "volume": 1234,
      "open_interest": 5678,
      "iv": 0.42
    }
  ]
}
```

字段说明：

| 字段 | 必需 | 说明 |
|---|---|---|
| symbol | 是 | 与文件名一致的标的代码 |
| spot | 是 | 标的现价 |
| fetched_at | 是 | ISO 8601 UTC 时间戳，用于新鲜度校验 |
| risk_free_rate | 否 | 年化无风险利率（小数）；缺省时管道回退 ^IRX 或 config 默认值 |
| quotes[].kind | 是 | `call` 或 `put` |
| quotes[].expiry | 是 | `YYYY-MM-DD` |
| quotes[].strike / volume / open_interest | 是 | 数值 |
| quotes[].bid / ask / last | 否 | 缺 bid/ask 时该合约会因价差无法计算而被流动性过滤拦下 |
| quotes[].iv | 否 | 年化隐含波动率，小数（0.42 = 42%），缺失时该合约不参与 IV 相关计算 |
| quotes[].contract_symbol | 否 | 缺省时自动生成 |

## 硬性校验规则（fetcher 端强制执行）

1. **新鲜度**：`fetched_at` 距当前时间超过 `config v5.financial_service.max_age_hours`
   （默认 24 小时）直接拒用并报错——过期期权数据比没有更危险，绝不静默用旧快照
2. **schema 完整性**：缺必需字段、kind 非法、symbol 与文件名不匹配都会明确报错并指出位置
3. **单标的缺快照不降级**：报错并提示生成方法；只有快照目录整体不存在且
   `allow_fallback: true` 时才降级 yfinance（带显式告警）

## 每日巡检（run_daily.py 的期权情绪快照）的配合方式

V3/V4 的期权情绪与预警每天需要 watchlist 内每只票的链数据。两种做法：

- 在本地 Claude Code 里让 skill 一次性生成 watchlist 全部标的的快照文件，再跑 run_daily
- 或者把 `v5.provider` 留 `financial_service` 但接受 watchlist 巡检部分标的因快照缺失
  而标记"数据缺失"（按需分析的标的单独生成快照即可）

不想维护快照文件时，把 `v5.provider` 改为 `yfinance` 即为全自动（数据质量稍逊）。
