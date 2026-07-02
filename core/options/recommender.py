"""3×3 腿策略推荐引擎（guidebook 5.6 功能三）：到期分桶（末日/中期/LEAPS）×
风险偏好（保守/中性/激进），每格 1-2 个具体组合。

硬规则（不可妥协）：
- 保守×末日格子硬编码"不建议"——末日期权与保守偏好本质矛盾，不提供伪保守方案
- 推荐的每条腿必须通过功能一的流动性阈值，凑不齐合格合约时该格诚实输出"无可用合约"
- 每个推荐必须带：具体腿、最大亏损（渲染层置于最显著层级）、最大盈利、盈亏平衡、
  中间价成本、保证金估算（量级参考）、引用具体数据的选择理由
- 与 V3 评分卡方向不一致时照常输出但标注分歧
"""
from datetime import date, datetime

from core.fetchers.options_base import OptionChainData, OptionQuote, passes_liquidity
from core.options.scenarios import OPTION_MULTIPLIER, entry_cost, risk_metrics
from core.options.sentiment import atm_iv_for_expiry

BUCKET_LABELS = {"zero_dte": "末日 (0-3 DTE)", "mid": "中期 (2周-3月)", "leaps": "长期 (6月+/LEAPS)"}
RISK_LABELS = {"conservative": "保守", "neutral": "中性", "aggressive": "激进"}


def _dte(expiry: str) -> int:
    return (datetime.strptime(expiry, "%Y-%m-%d").date() - date.today()).days


def pick_bucket_expiry(chain: OptionChainData, bucket_cfg: dict, bucket_key: str) -> str | None:
    candidates = [(e, _dte(e)) for e in chain.expiries if bucket_cfg["min"] <= _dte(e) <= bucket_cfg["max"]]
    if not candidates:
        return None
    if bucket_key == "mid":
        return min(candidates, key=lambda c: abs(c[1] - 40))[0]  # 偏好 30-45 DTE 一带
    return min(candidates, key=lambda c: c[1])[0]


def nearest_liquid(chain: OptionChainData, expiry: str, kind: str, target_strike: float,
                   liq_cfg: dict, exclude_strikes: set | None = None) -> OptionQuote | None:
    pool = [q for q in chain.for_expiry(expiry)
            if q.kind == kind and passes_liquidity(q, liq_cfg) and q.mid and q.mid > 0
            and (exclude_strikes is None or q.strike not in exclude_strikes)]
    if not pool:
        return None
    return min(pool, key=lambda q: abs(q.strike - target_strike))


def _leg(quote: OptionQuote, direction: int, qty: int = 1) -> dict:
    return {"kind": quote.kind, "strike": quote.strike, "expiry": quote.expiry,
            "direction": direction, "qty": qty, "entry_price": quote.mid, "iv": quote.iv,
            "contract_symbol": quote.contract_symbol,
            "desc": f"{'买入' if direction > 0 else '卖出'} {quote.expiry} {quote.strike:g} {'Call' if quote.kind == 'call' else 'Put'} @ {quote.mid:.2f}"}


def _build_rec(name: str, legs: list[dict], reason: str, margin_estimate: float,
               ref_price: float, rate: float, direction_fit: str | None,
               extra_note: str | None = None) -> dict:
    metrics = risk_metrics(legs, ref_price, rate)
    return {
        "name": name, "legs": legs, "reason": reason,
        "cost": entry_cost(legs), "margin_estimate": round(margin_estimate, 2),
        "direction_fit": direction_fit, "extra_note": extra_note, **metrics,
    }


def _term_note(chain: OptionChainData, expiry: str) -> str:
    iv = atm_iv_for_expiry(chain, expiry)
    return f"该到期日 ATM IV {iv * 100:.1f}%" if iv is not None else "该到期日 ATM IV 不可得"


def recommend_matrix(chain: OptionChainData, view: str, rate: float, liq_cfg: dict,
                     buckets_cfg: dict, v3_direction: str | None = None) -> dict:
    """view: bull | bear。返回 cells[bucket][risk] = {"status": ..., "recs"/"note"}。"""
    spot = chain.spot
    bull = view == "bull"

    fit = None
    if v3_direction in ("看多", "看空"):
        matched = (v3_direction == "看多") == bull
        fit = "与 V3 评分卡方向一致" if matched else f"与 V3 评分卡方向分歧（评分卡{v3_direction}，本推荐按{'看多' if bull else '看空'}生成）"

    cells: dict[str, dict] = {}
    for bucket_key, bucket_cfg in buckets_cfg.items():
        cells[bucket_key] = {}
        expiry = pick_bucket_expiry(chain, bucket_cfg, bucket_key)
        for risk in ("conservative", "neutral", "aggressive"):
            if bucket_key == "zero_dte" and risk == "conservative":
                cells[bucket_key][risk] = {"status": "refused",
                    "note": "不建议：末日期权 gamma/theta 剧烈、容错为零，与保守偏好本质矛盾，"
                            "本系统不提供伪保守的末日方案（硬编码规则）"}
                continue
            if expiry is None:
                cells[bucket_key][risk] = {"status": "no_contract",
                    "note": f"当前链上无 {BUCKET_LABELS[bucket_key]} 区间内的到期日"}
                continue
            recs = _build_cell(chain, expiry, bucket_key, risk, bull, spot, rate, liq_cfg, buckets_cfg, fit)
            if recs:
                cells[bucket_key][risk] = {"status": "ok", "recs": recs}
            else:
                cells[bucket_key][risk] = {"status": "no_contract",
                    "note": f"{expiry} 到期链上没有通过流动性阈值（OI≥{liq_cfg['min_open_interest']}、"
                            f"量≥{liq_cfg['min_volume']}、价差≤{liq_cfg['max_spread_over_mid']:.0%}）的合适合约"}
    return cells


def _build_cell(chain, expiry, bucket_key, risk, bull, spot, rate, liq_cfg, buckets_cfg, fit) -> list[dict]:
    recs = []
    kind = "call" if bull else "put"
    dte = _dte(expiry)
    term = _term_note(chain, expiry)

    if bucket_key == "zero_dte":
        if risk == "neutral":
            q = nearest_liquid(chain, expiry, kind, spot * (0.85 if bull else 1.15), liq_cfg)
            if q and abs(q.strike / spot - 1) > 0.05:
                recs.append(_build_rec(
                    f"深度价内单腿 {'Call' if bull else 'Put'}", [_leg(q, 1)],
                    f"深度价内（行权价 {q.strike:g} vs 现价 {spot:g}）时间价值占比低，末日 theta 损耗相对可控；"
                    f"{term}，DTE {dte} 天。", entry_cost([_leg(q, 1)]), spot, rate, fit,
                    extra_note="末日仓位：整体高风险，仅适合已明确接受当日归零可能的资金"))
        elif risk == "aggressive":
            q = nearest_liquid(chain, expiry, kind, spot * (1.02 if bull else 0.98), liq_cfg)
            if q:
                recs.append(_build_rec(
                    f"价外单腿 {'Call' if bull else 'Put'}", [_leg(q, 1)],
                    f"价外行权价 {q.strike:g}，权利金 {q.mid:.2f} 即为最大亏损上限；{term}，DTE {dte} 天。",
                    entry_cost([_leg(q, 1)]), spot, rate, fit,
                    extra_note="末日仓位：大概率归零，博高赔率，切勿重仓"))
            long_q = nearest_liquid(chain, expiry, kind, spot, liq_cfg)
            short_q = nearest_liquid(chain, expiry, kind, spot * (1.02 if bull else 0.98), liq_cfg,
                                     exclude_strikes={long_q.strike} if long_q else None)
            if long_q and short_q and short_q.strike != long_q.strike:
                legs = [_leg(long_q, 1), _leg(short_q, -1)]
                width = abs(short_q.strike - long_q.strike) * OPTION_MULTIPLIER
                recs.append(_build_rec(
                    "末日垂直价差", legs,
                    f"借方价差把最大亏损锁定在净权利金，放弃 {short_q.strike:g} 以外的收益换取成本降低；"
                    f"价差宽度 {width / OPTION_MULTIPLIER:g} 点，{term}。",
                    max(entry_cost(legs), 0), spot, rate, fit,
                    extra_note="末日仓位：整体高风险提示同上"))

    elif bucket_key == "mid":
        if risk == "conservative":
            put_q = nearest_liquid(chain, expiry, "put", spot * 0.95, liq_cfg)
            if put_q:
                legs = [_leg(put_q, -1)]
                margin = put_q.strike * OPTION_MULTIPLIER - put_q.mid * OPTION_MULTIPLIER
                csp_fit = fit if bull else ((fit + "；" if fit else "") + "注意：现金担保卖put 本质中性偏多，与看空观点不匹配")
                recs.append(_build_rec(
                    "现金担保卖 Put", legs,
                    f"在 {put_q.strike:g}（现价约 {put_q.strike / spot:.0%}）卖出，权利金 {put_q.mid:.2f}/股；"
                    f"被指派则以折价接货，{term}，DTE {dte} 天。", margin, spot, rate, csp_fit,
                    extra_note=f"需备足 {margin:,.0f} 现金担保；被指派风险需在券商端确认"))
            call_q = nearest_liquid(chain, expiry, "call", spot * 1.05, liq_cfg)
            if call_q:
                stock_leg = {"kind": "stock", "strike": None, "expiry": None, "direction": 1,
                             "qty": OPTION_MULTIPLIER, "entry_price": spot, "iv": None,
                             "contract_symbol": chain.symbol, "desc": f"持有 {chain.symbol} 100 股 @ {spot:g}"}
                legs = [stock_leg, _leg(call_q, -1)]
                cc_fit = fit if bull else ((fit + "；" if fit else "") + "注意：备兑开仓为持股增强策略，与看空观点不匹配")
                recs.append(_build_rec(
                    "备兑开仓（Covered Call）", legs,
                    f"以 {call_q.mid:.2f}/股 卖出 {call_q.strike:g} Call 增强持股收益，"
                    f"上方收益封顶于 {call_q.strike:g}；{term}。",
                    spot * OPTION_MULTIPLIER - call_q.mid * OPTION_MULTIPLIER, spot, rate, cc_fit,
                    extra_note="前提是持有（或同时买入）100 股正股"))
        elif risk == "neutral":
            long_q = nearest_liquid(chain, expiry, kind, spot, liq_cfg)
            short_q = nearest_liquid(chain, expiry, kind, spot * (1.05 if bull else 0.95), liq_cfg,
                                     exclude_strikes={long_q.strike} if long_q else None)
            if long_q and short_q and short_q.strike != long_q.strike:
                legs = [_leg(long_q, 1), _leg(short_q, -1)]
                recs.append(_build_rec(
                    "牛市看涨价差 (Bull Call Spread)" if bull else "熊市看跌价差 (Bear Put Spread)", legs,
                    f"买 {long_q.strike:g} 卖 {short_q.strike:g}，最大亏损锁定为净权利金 {entry_cost(legs):,.0f}；"
                    f"{term}，DTE {dte} 天在 theta 损耗和方向表达之间平衡。",
                    max(entry_cost(legs), 0), spot, rate, fit))
        else:  # aggressive
            q = nearest_liquid(chain, expiry, kind, spot * (1.02 if bull else 0.98), liq_cfg)
            if q:
                recs.append(_build_rec(
                    f"单腿买入 {'Call' if bull else 'Put'}", [_leg(q, 1)],
                    f"行权价 {q.strike:g}（{'轻度价外' if abs(q.strike / spot - 1) < 0.05 else '价外'}），"
                    f"最大亏损即权利金 {q.mid * OPTION_MULTIPLIER:,.0f}；{term}，DTE {dte} 天。",
                    entry_cost([_leg(q, 1)]), spot, rate, fit))

    elif bucket_key == "leaps":
        if risk == "conservative":
            q = nearest_liquid(chain, expiry, kind, spot * (0.80 if bull else 1.20), liq_cfg)
            if q:
                recs.append(_build_rec(
                    f"深度价内 LEAPS {'Call' if bull else 'Put'}（替代{'持股' if bull else '做空'}）", [_leg(q, 1)],
                    f"深度价内（{q.strike:g} vs 现价 {spot:g}）近似 {'正股替代' if bull else '空头替代'}，"
                    f"占用资金远小于{'持股' if bull else '融券'}；{term}，DTE {dte} 天时间价值损耗平缓。",
                    entry_cost([_leg(q, 1)]), spot, rate, fit))
        elif risk == "neutral":
            long_q = nearest_liquid(chain, expiry, kind, spot * (0.80 if bull else 1.20), liq_cfg)
            mid_expiry = pick_bucket_expiry(chain, buckets_cfg["mid"], "mid")
            short_q = nearest_liquid(chain, mid_expiry, kind, spot * (1.05 if bull else 0.95), liq_cfg) if mid_expiry else None
            if long_q and short_q:
                legs = [_leg(long_q, 1), _leg(short_q, -1)]
                recs.append(_build_rec(
                    "PMCC（LEAPS + 卖近月 Call）" if bull else "对角价差（LEAPS Put + 卖近月 Put）", legs,
                    f"长腿 {long_q.expiry} {long_q.strike:g} 深度价内，短腿 {short_q.expiry} {short_q.strike:g} "
                    f"持续收权利金摊薄成本；两腿分属不同到期日，payoff 图以短腿到期日为评估时点。",
                    max(entry_cost(legs), 0), spot, rate, fit,
                    extra_note="短腿被指派时需用长腿对冲或平仓，操作复杂度高于单腿"))
        else:  # aggressive
            q = nearest_liquid(chain, expiry, kind, spot * (1.15 if bull else 0.85), liq_cfg)
            if q:
                recs.append(_build_rec(
                    f"长期价外 {'Call' if bull else 'Put'}", [_leg(q, 1)],
                    f"价外行权价 {q.strike:g}（现价的 {q.strike / spot:.0%}），赌长周期大幅波动；"
                    f"{term}，DTE {dte} 天，IV 假设变化对估值影响显著。",
                    entry_cost([_leg(q, 1)]), spot, rate, fit))

    return recs[:2]
