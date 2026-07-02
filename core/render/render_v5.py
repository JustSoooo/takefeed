"""Render the V5 options-analysis page (options.html). V5 是按需分析工具而非每日巡检：
由 run_options.py 针对单一标的生成，页面顶部固定展示模型局限声明（guidebook 5.6，不可折叠）。"""
import json
from datetime import date, datetime

from core.fetchers.options_base import OptionChainData, passes_liquidity
from core.options.pricing import bs_greeks, year_fraction
from core.options.recommender import BUCKET_LABELS, RISK_LABELS
from core.options.sentiment import atm_iv_for_expiry
from core.render.common import get_env, write_page

DISCLAIMERS = [
    "所有盈亏为理论模型估算：BS 模型不处理美式提前行权、股息除权、盘后跳空；实际成交价受价差和滑点影响。",
    "预估收益强依赖 IV 假设，IV 情景切换可导致结论反转。",
    "推荐矩阵是候选清单而非投资建议，最终决策和风险自担；系统不追踪实际持仓、不提供追加保证金测算。",
    "卖方策略（卖put/卖call）的保证金和被指派风险需在券商端自行确认，本系统的保证金估算仅为量级参考。",
]


def _fmt_money(v: float | None) -> str:
    return f"{v:+,.0f}" if v is not None else "-"


def _build_expiry_summary(chain: OptionChainData, liq_cfg: dict) -> list[dict]:
    today = date.today()
    rows = []
    for expiry in chain.expiries:
        quotes = chain.for_expiry(expiry)
        liquid = [q for q in quotes if passes_liquidity(q, liq_cfg)]
        iv = atm_iv_for_expiry(chain, expiry)
        rows.append({
            "expiry": expiry,
            "dte": (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days,
            "atm_iv_str": f"{iv * 100:.1f}%" if iv is not None else "-",
            "liquid_count": len(liquid), "total_count": len(quotes),
        })
    return rows


def _build_chain_rows(chain: OptionChainData, liq_cfg: dict, rate: float,
                      expiries_shown: int = 3, moneyness_band: float = 0.15) -> tuple[list[dict], int]:
    """近月若干个到期日、现价 ±15% 行权价区间内、通过流动性过滤的合约明细。
    返回 (rows, 被过滤隐藏的合约数)。"""
    today = date.today()
    rows, hidden = [], 0
    for expiry in chain.expiries[:expiries_shown]:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        t = year_fraction(today, expiry_date)
        in_band = [q for q in chain.for_expiry(expiry)
                   if abs(q.strike / chain.spot - 1) <= moneyness_band]
        for q in sorted(in_band, key=lambda x: (x.expiry, x.strike, x.kind)):
            if not passes_liquidity(q, liq_cfg):
                hidden += 1
                continue
            delta = bs_greeks(q.kind, chain.spot, q.strike, t, rate, q.iv or 0.0)["delta"]
            rows.append({
                "expiry": expiry, "kind": "Call" if q.kind == "call" else "Put",
                "strike": f"{q.strike:g}",
                "bid": f"{q.bid:.2f}" if q.bid is not None else "-",
                "ask": f"{q.ask:.2f}" if q.ask is not None else "-",
                "mid": f"{q.mid:.2f}" if q.mid is not None else "-",
                "volume": q.volume, "oi": q.open_interest,
                "iv_str": f"{q.iv * 100:.1f}%" if q.iv is not None else "-",
                "delta_str": f"{delta:+.2f}",
            })
    return rows, hidden


def _build_scenario_ctx(report: dict) -> dict:
    risk = report["risk"]
    max_loss_str = "理论无上限" if risk["max_loss_unbounded"] else _fmt_money(risk["max_loss"])
    max_gain_str = "理论无上限" if risk["max_gain_unbounded"] else _fmt_money(risk["max_gain"])
    scenario_b = report["scenario_b"]
    return {
        "entry_cost_str": _fmt_money(report["entry_cost"]),
        "scenario_a_pnl_str": _fmt_money(report["scenario_a"]["pnl"]),
        "scenario_a_class": "pct-positive" if report["scenario_a"]["pnl"] >= 0 else "pct-negative",
        "scenario_b": scenario_b,
        "scenario_b_pnl_str": _fmt_money(scenario_b.get("pnl")) if scenario_b["status"] == "ok" else None,
        "scenario_b_class": ("pct-positive" if scenario_b.get("pnl", 0) >= 0 else "pct-negative") if scenario_b["status"] == "ok" else "",
        "scenario_b_iv_str": f"{scenario_b['iv'] * 100:.1f}%" if scenario_b["status"] == "ok" else None,
        "max_loss_str": max_loss_str, "max_gain_str": max_gain_str,
        "breakevens_str": " / ".join(f"{b:g}" for b in risk["breakevens"]) or "-",
        "payoff_json": json.dumps(report["payoff"], ensure_ascii=False),
        "payoff_eval_date": report["payoff"]["eval_date"],
        "sensitivity": {
            "prices": report["sensitivity"]["prices"],
            "rows": [{"date": r["date"],
                      "cells": [{"pnl_str": _fmt_money(v),
                                 "cls": "pct-positive" if v >= 0 else "pct-negative"} for v in r["pnl"]]}
                     for r in report["sensitivity"]["rows"]],
        },
        "crosses_earnings": report["crosses_earnings"],
        "next_earnings_date": report["next_earnings_date"],
    }


def _build_matrix_ctx(cells: dict) -> list[dict]:
    rows = []
    for bucket_key in ("zero_dte", "mid", "leaps"):
        if bucket_key not in cells:
            continue
        row = {"bucket_label": BUCKET_LABELS[bucket_key], "is_zero_dte": bucket_key == "zero_dte", "cells": []}
        for risk in ("conservative", "neutral", "aggressive"):
            cell = cells[bucket_key].get(risk, {"status": "no_contract", "note": "-"})
            ctx = {"risk_label": RISK_LABELS[risk], "status": cell["status"], "note": cell.get("note")}
            if cell["status"] == "ok":
                ctx["recs"] = [{
                    "name": r["name"],
                    "legs": [leg["desc"] for leg in r["legs"]],
                    "max_loss_str": "理论无上限" if r["max_loss_unbounded"] else _fmt_money(r["max_loss"]),
                    "max_gain_str": "理论无上限" if r["max_gain_unbounded"] else _fmt_money(r["max_gain"]),
                    "breakevens_str": " / ".join(f"{b:g}" for b in r["breakevens"]) or "-",
                    "cost_str": _fmt_money(r["cost"]),
                    "margin_str": f"{r['margin_estimate']:,.0f}",
                    "reason": r["reason"], "direction_fit": r["direction_fit"], "extra_note": r["extra_note"],
                } for r in cell["recs"]]
            row["cells"].append(ctx)
        rows.append(row)
    return rows


def render_options_page(chain: OptionChainData, liq_cfg: dict, rate: float, rate_note: str | None,
                        term_structure: list[dict], iv_rank: dict | None,
                        scenario_report: dict | None, scenario_inputs: dict | None,
                        matrix_cells: dict | None, view: str,
                        as_of_date: str, generated_at: str, output_dir: str,
                        is_sample_data: bool = False):
    template = get_env().get_template("options.html")
    chain_rows, hidden_count = _build_chain_rows(chain, liq_cfg, rate)
    context = {
        "as_of_date": as_of_date, "generated_at": generated_at, "active_module": "v5",
        "is_sample_data": is_sample_data,
        "disclaimers": DISCLAIMERS,
        "symbol": chain.symbol, "spot": f"{chain.spot:g}",
        "rate_str": f"{rate * 100:.2f}%", "rate_note": rate_note,
        "view_label": "看多" if view == "bull" else "看空",
        "expiry_summary": _build_expiry_summary(chain, liq_cfg),
        "term_structure_json": json.dumps(term_structure, ensure_ascii=False),
        "iv_rank": iv_rank,
        "chain_rows": chain_rows, "hidden_count": hidden_count,
        "liq_cfg": liq_cfg,
        "scenario": _build_scenario_ctx(scenario_report) if scenario_report else None,
        "scenario_inputs": scenario_inputs,
        "matrix_rows": _build_matrix_ctx(matrix_cells) if matrix_cells else None,
    }
    write_page(output_dir, "options.html", template.render(**context))
