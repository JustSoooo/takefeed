"""V1 六维度打分：波动率 / 趋势 / 广度 / 信用 / 板块轮动 / 情绪。
每个维度：原始值 -> 历史百分位(近1年) -> 维度分(-20..+20)。
综合分 = 50 + Σ(维度分 x 权重)，clamp 到 0-100（guidebook 2.3）。

数字计算全部在本文件完成，narrative 层只负责把结果翻译成文字，不参与计算
（guidebook 0 总原则：数据与判断分层）。
"""
from dataclasses import dataclass, field
from typing import Optional

from core.fetchers.us_market import USMarketFetcher, above_ma_ratio, pct_change_over, percentile_of_last
from core.fetchers.us_breadth import compute_breadth_series, load_constituents
from core.fetchers import sentiment_scrape
from core import db as dbmod


@dataclass
class DimensionResult:
    dimension: str
    status: str  # ok | missing
    raw: dict = field(default_factory=dict)
    percentile: Optional[float] = None
    score: Optional[float] = None
    note: Optional[str] = None


def _clip(x: float, lo: float = -20.0, hi: float = 20.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- volatility

def compute_volatility(fetcher: USMarketFetcher, cfg: dict) -> DimensionResult:
    tickers = cfg["tickers"]
    vix_res = fetcher.get_quote_history(tickers["vix"], period=cfg.get("history_period", "1y"))
    if not vix_res.ok:
        return DimensionResult("volatility", "missing", note=vix_res.error)
    vix9d_res = fetcher.get_quote_history(tickers["vix9d"], period="1mo")
    if not vix9d_res.ok:
        return DimensionResult("volatility", "missing", note=vix9d_res.error)

    vix_close = vix_res.data["Close"]
    vix_pctile = percentile_of_last(vix_close)
    vix_last = float(vix_close.iloc[-1])
    vix9d_last = float(vix9d_res.data["Close"].iloc[-1])
    ratio = vix9d_last / vix_last if vix_last else None

    score = -_clip((vix_pctile - 50) / 50 * 20)
    ratio_adj = 0.0
    if ratio is not None:
        if ratio > 1.0:
            ratio_adj = -5.0  # VIX9D > VIX：近端恐慌高于远端，短期承压信号
        elif ratio < 0.9:
            ratio_adj = 3.0  # 明显 contango：市场情绪偏平静
    score = _clip(score + ratio_adj)

    return DimensionResult(
        "volatility", "ok",
        raw={"vix": vix_last, "vix9d": vix9d_last, "vix9d_vix_ratio": round(ratio, 3) if ratio else None},
        percentile=round(vix_pctile, 2), score=round(score, 2),
    )


# --------------------------------------------------------------------- trend

def compute_trend(fetcher: USMarketFetcher, cfg: dict) -> DimensionResult:
    symbols = cfg["tickers"]["trend"]
    windows = [20, 50, 200]
    per_symbol = {}
    ratios = []
    for sym in symbols:
        res = fetcher.get_quote_history(sym, period=cfg.get("history_period", "1y"))
        if not res.ok:
            return DimensionResult("trend", "missing", note=res.error)
        close = res.data["Close"]
        ratio = above_ma_ratio(close, windows)
        ma_values = {f"ma{w}": round(float(close.rolling(w).mean().iloc[-1]), 2) for w in windows if len(close) >= w}
        per_symbol[sym] = {"close": round(float(close.iloc[-1]), 2), **ma_values, "above_ratio": round(ratio, 3)}
        ratios.append(ratio)

    overall_ratio = sum(ratios) / len(ratios) if ratios else 0.5
    score = _clip((overall_ratio - 0.5) * 40)
    return DimensionResult(
        "trend", "ok",
        raw={"symbols": per_symbol, "overall_above_ratio": round(overall_ratio, 3)},
        score=round(score, 2),
    )


# ------------------------------------------------------------------- breadth

def compute_breadth(fetcher: USMarketFetcher, cfg: dict) -> DimensionResult:
    universe = cfg.get("breadth_universe", "dow30")
    try:
        symbols = load_constituents(universe)
    except FileNotFoundError as exc:
        return DimensionResult("breadth", "missing", note=str(exc))

    res = compute_breadth_series(fetcher, symbols)
    if not res.ok:
        return DimensionResult("breadth", "missing", note=res.error)

    series = res.data["series"]
    pctile = percentile_of_last(series)
    score = _clip((pctile - 50) / 50 * 20)
    return DimensionResult(
        "breadth", "ok",
        raw={"today": round(res.data["today"], 2), "universe": universe,
             "constituents_used": res.data["constituents_used"], "constituents_total": res.data["constituents_total"]},
        percentile=round(pctile, 2), score=round(score, 2),
    )


# -------------------------------------------------------------------- credit

def compute_credit(fetcher: USMarketFetcher, cfg: dict) -> DimensionResult:
    risk_on = cfg["tickers"]["credit_risk_on"]
    risk_off = cfg["tickers"]["credit_risk_off"]
    hyg_res = fetcher.get_quote_history(risk_on, period=cfg.get("history_period", "1y"))
    ief_res = fetcher.get_quote_history(risk_off, period=cfg.get("history_period", "1y"))
    if not hyg_res.ok:
        return DimensionResult("credit", "missing", note=hyg_res.error)
    if not ief_res.ok:
        return DimensionResult("credit", "missing", note=ief_res.error)

    hyg = hyg_res.data["Close"]
    ief = ief_res.data["Close"]
    joined = hyg.to_frame("hyg").join(ief.to_frame("ief"), how="inner").dropna()
    if len(joined) < 25:
        return DimensionResult("credit", "missing", note="credit: insufficient overlapping history")

    ratio = joined["hyg"] / joined["ief"]
    pctile = percentile_of_last(ratio)
    chg_5d = pct_change_over(ratio, 5)
    chg_20d = pct_change_over(ratio, 20)

    score = _clip((pctile - 50) / 50 * 20)
    if chg_5d > 0 and chg_20d > 0:
        score = _clip(score + 3)
    elif chg_5d < 0 and chg_20d < 0:
        score = _clip(score - 3)

    return DimensionResult(
        "credit", "ok",
        raw={"hyg": round(float(hyg.iloc[-1]), 2), "ief": round(float(ief.iloc[-1]), 2),
             "ratio": round(float(ratio.iloc[-1]), 4), "chg_5d": round(chg_5d, 4), "chg_20d": round(chg_20d, 4)},
        percentile=round(pctile, 2), score=round(score, 2),
    )


# ------------------------------------------------------------------ rotation

def compute_rotation(fetcher: USMarketFetcher, cfg: dict) -> DimensionResult:
    sector_cfg = cfg["sector_etfs"]
    rows = []
    for s in sector_cfg:
        res = fetcher.get_quote_history(s["symbol"], period="3mo")
        if not res.ok:
            return DimensionResult("rotation", "missing", note=f"{s['symbol']}: {res.error}")
        close = res.data["Close"]
        try:
            chg_1w = pct_change_over(close, 5)
            chg_1m = pct_change_over(close, 21)
        except ValueError as exc:
            return DimensionResult("rotation", "missing", note=f"{s['symbol']}: {exc}")
        rows.append({"symbol": s["symbol"], "name": s["name"], "style": s["style"],
                      "chg_1w": round(chg_1w, 4), "chg_1m": round(chg_1m, 4)})

    rows.sort(key=lambda r: r["chg_1m"], reverse=True)
    cyclical = [r["chg_1m"] for r in rows if r["style"] == "cyclical"]
    defensive = [r["chg_1m"] for r in rows if r["style"] == "defensive"]
    cyc_avg = sum(cyclical) / len(cyclical) if cyclical else 0.0
    def_avg = sum(defensive) / len(defensive) if defensive else 0.0
    pct_positive = sum(1 for r in rows if r["chg_1m"] > 0) / len(rows) * 100 if rows else 50.0

    score_leadership = _clip((cyc_avg - def_avg) * 300)
    score_breadth = _clip((pct_positive - 50) / 50 * 20)
    score = round((score_leadership + score_breadth) / 2, 2)

    return DimensionResult(
        "rotation", "ok",
        raw={"sectors": rows, "cyclical_avg_1m": round(cyc_avg, 4), "defensive_avg_1m": round(def_avg, 4),
             "pct_positive_1m": round(pct_positive, 1)},
        score=score,
    )


# ----------------------------------------------------------------- sentiment

def compute_sentiment(conn, date: str, cfg: dict) -> DimensionResult:
    sent_cfg = cfg["sentiment"]
    min_history = sent_cfg.get("min_history_for_percentile", 20)

    fg_res = sentiment_scrape.fetch_fear_greed(sent_cfg["fear_greed_url"])
    aaii_res = sentiment_scrape.fetch_aaii_sentiment(sent_cfg["aaii_url"])

    if not fg_res.ok and not aaii_res.ok:
        return DimensionResult("sentiment", "missing", note=f"AAII: {aaii_res.error} | F&G: {fg_res.error}")

    scores = []
    raw = {}
    notes = []

    if fg_res.ok:
        fg = fg_res.data
        fg_score = _clip((fg["percentile_1y"] - 50) / 50 * 20)
        scores.append(fg_score)
        raw["fear_greed"] = fg
    else:
        notes.append(f"Fear&Greed 缺失: {fg_res.error}")

    if aaii_res.ok:
        aaii = aaii_res.data
        raw["aaii"] = aaii
        history = dbmod.get_metric_history(conn, "v1", "sentiment", before_date=date, limit=200)
        spreads = [h["aaii"]["spread"] for _, h in history if isinstance(h.get("aaii"), dict) and "spread" in h["aaii"]]
        if len(spreads) >= min_history:
            pctile = sum(1 for v in spreads if v <= aaii["spread"]) / len(spreads) * 100
            aaii_score = _clip((pctile - 50) / 50 * 20)
            raw["aaii"]["percentile_bootstrap"] = True
            raw["aaii"]["percentile"] = round(pctile, 2)
        else:
            # 样本不足，退化为固定阈值线性映射（非百分位法），见 config sentiment.min_history_for_percentile
            aaii_score = _clip(aaii["spread"] / 50 * 20)
            raw["aaii"]["percentile_bootstrap"] = False
            notes.append(f"AAII 历史样本不足({len(spreads)}/{min_history})，本次用阈值法而非百分位法")
        scores.append(aaii_score)
    else:
        notes.append(f"AAII 缺失: {aaii_res.error}")

    if not scores:
        return DimensionResult("sentiment", "missing", note="; ".join(notes))

    score = round(sum(scores) / len(scores), 2)
    return DimensionResult("sentiment", "ok", raw=raw, score=score, note="; ".join(notes) or None)


# ---------------------------------------------------------------- composite

DIMENSION_FUNCS_NO_ARG_NEEDED = ["volatility", "trend", "breadth", "credit", "rotation"]


def compute_composite(dim_results: dict[str, DimensionResult], weights: dict, state_thresholds: dict):
    ok_dims = {k: v for k, v in dim_results.items() if v.status == "ok"}
    if not ok_dims:
        raise ValueError("all six dimensions missing today; cannot compute composite score")

    ok_weight_sum = sum(weights[k] for k in ok_dims)
    normalized_weights = {k: weights[k] / ok_weight_sum for k in ok_dims}

    weighted_sum = sum(ok_dims[k].score * normalized_weights[k] for k in ok_dims)
    composite = max(0.0, min(100.0, 50 + weighted_sum))

    if composite < state_thresholds["defensive_below"]:
        state = "防守"
    elif composite > state_thresholds["aggressive_above"]:
        state = "积极"
    else:
        state = "谨慎观望"

    return round(composite, 2), state, normalized_weights
