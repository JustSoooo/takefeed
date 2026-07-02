"""V2 板块/行业轮动追踪：11个 SPDR 板块 ETF 相对 SPY 的超额收益矩阵（1周/1月/3月三窗口），
排名持续性判断，以及强势板块的内部健康度（普涨还是靠权重股拉动）。

V2 不产出单一综合分——它回答的是"资金去哪、这个趋势还在不在"，输出是一张矩阵而不是一个数字
（guidebook 3.1）。持续性和内部健康度都可能因数据缺失而部分降级，不影响矩阵主体的展示。
"""
from dataclasses import dataclass, field
from typing import Optional

from core import db as dbmod
from core.fetchers.sector_holdings import fetch_sector_holdings
from core.fetchers.us_market import USMarketFetcher, extract_symbol_close, pct_change_over


@dataclass
class ModuleResult:
    module: str
    status: str  # ok | missing
    raw: dict = field(default_factory=dict)
    note: Optional[str] = None


def compute_relative_strength_matrix(fetcher: USMarketFetcher, cfg: dict) -> ModuleResult:
    windows = cfg["windows"]  # {short, mid, long} in trading days
    benchmark = cfg["benchmark"]

    spy_res = fetcher.get_quote_history(benchmark, period="6mo")
    if not spy_res.ok:
        return ModuleResult("v2", "missing", note=spy_res.error)
    spy_close = spy_res.data["Close"]
    try:
        spy_returns = {w: pct_change_over(spy_close, n) for w, n in windows.items()}
    except ValueError as exc:
        return ModuleResult("v2", "missing", note=f"benchmark history too short: {exc}")

    rows = []
    for s in cfg["sector_etfs"]:
        res = fetcher.get_quote_history(s["symbol"], period="6mo")
        if not res.ok:
            return ModuleResult("v2", "missing", note=f"{s['symbol']}: {res.error}")
        close = res.data["Close"]
        try:
            sector_returns = {w: pct_change_over(close, n) for w, n in windows.items()}
        except ValueError as exc:
            return ModuleResult("v2", "missing", note=f"{s['symbol']}: {exc}")

        row = {"symbol": s["symbol"], "name": s["name"], "style": s["style"]}
        for w in windows:
            row[f"ret_{w}"] = round(sector_returns[w], 4)
            row[f"excess_{w}"] = round(sector_returns[w] - spy_returns[w], 4)
        rows.append(row)

    for w in windows:
        ranked = sorted(rows, key=lambda r: r[f"excess_{w}"], reverse=True)
        for rank, row in enumerate(ranked, start=1):
            row[f"rank_{w}"] = rank

    rows.sort(key=lambda r: r["rank_mid"])
    return ModuleResult("v2", "ok", raw={
        "benchmark": benchmark,
        "benchmark_returns": {w: round(v, 4) for w, v in spy_returns.items()},
        "sectors": rows,
    })


def attach_persistence(conn, date: str, cfg: dict, matrix_raw: dict) -> None:
    """Mutates matrix_raw['sectors'] in place, adding persistence fields based on
    how often each sector has held a top-N rank on the mid (1-month) window across
    the trailing lookback window of daily snapshots already stored in SQLite."""
    p_cfg = cfg["persistence"]
    history = dbmod.get_metric_history(conn, "v2", "sector_matrix", before_date=date, limit=p_cfg["lookback_days"])

    for row in matrix_raw["sectors"]:
        symbol = row["symbol"]
        hits = 0
        observed = 0
        for _, past_raw in history:
            past_row = next((r for r in past_raw.get("sectors", []) if r["symbol"] == symbol), None)
            if past_row is None:
                continue
            observed += 1
            if past_row.get("rank_mid", 999) <= p_cfg["top_n"]:
                hits += 1

        if observed == 0:
            row["persistence_hit_ratio"] = None
            row["persistent_strong"] = False
            row["persistence_note"] = "历史样本不足，尚无法判断持续性"
        else:
            hit_ratio = hits / observed
            row["persistence_hit_ratio"] = round(hit_ratio, 3)
            row["persistence_observed_days"] = observed
            row["persistent_strong"] = hit_ratio >= p_cfg["hit_ratio_threshold"] and row["rank_mid"] <= p_cfg["top_n"]
            row["persistence_note"] = None


def compute_internal_health(fetcher: USMarketFetcher, cfg: dict, matrix_raw: dict) -> dict:
    """For the current top-N sectors (by 1-month rank), check whether the move is
    broad-based across the ETF's top holdings or concentrated in a few names."""
    h_cfg = cfg["internal_health"]
    strong_sectors = sorted(matrix_raw["sectors"], key=lambda r: r["rank_mid"])[: h_cfg["strong_sector_count"]]

    results = {}
    for row in strong_sectors:
        symbol = row["symbol"]
        holdings_res = fetch_sector_holdings(symbol, top_n=h_cfg["holdings_top_n"])
        if not holdings_res.ok:
            results[symbol] = {"status": "missing", "note": holdings_res.error}
            continue

        holdings = holdings_res.data
        tickers = [h["ticker"] for h in holdings]
        hist_res = fetcher.get_multi_history(tickers, period="2mo")
        if not hist_res.ok:
            results[symbol] = {"status": "missing", "note": hist_res.error}
            continue

        up_count = 0
        counted = 0
        for h in holdings:
            close = extract_symbol_close(hist_res.data, h["ticker"])
            if close is None or len(close) < 22:
                continue
            try:
                chg = pct_change_over(close, 21)
            except ValueError:
                continue
            counted += 1
            if chg > 0:
                up_count += 1

        if counted == 0:
            results[symbol] = {"status": "missing", "note": f"{symbol}: no usable holding price history"}
            continue

        top3_weight = round(sum(h["weight"] for h in holdings[:3]), 2)
        results[symbol] = {
            "status": "ok",
            "raw": {
                "holdings_counted": counted,
                "pct_holdings_up_1m": round(up_count / counted * 100, 1),
                "top3_weight_pct": top3_weight,
                "concentrated": top3_weight >= 40.0,  # 前3大持仓占比过高，普涨判断需谨慎
            },
        }
    return results
