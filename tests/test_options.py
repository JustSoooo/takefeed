"""Unit tests for V5 (options): Black-Scholes pricing against known reference
values, put-call parity, liquidity filtering, scenario engine (dual IV, IV crush
flag, sensitivity matrix), recommender hard rules, and options.html rendering --
all on synthetic chains, no network. Live yfinance option_chain is exercised
manually against real network access, see docs/cron.md.
"""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.fetchers.options_base import OptionChainData, OptionQuote, passes_liquidity
from core.options.pricing import bs_greeks, bs_price
from core.options.recommender import recommend_matrix
from core.options.scenarios import build_scenario_report, position_pnl
from core.options.sentiment import (compute_options_snapshot, compute_term_structure,
                                    iv_median_from_history, iv_rank_from_history)
from core.render.render_v5 import render_options_page

LIQ_CFG = {"min_open_interest": 100, "min_volume": 10, "max_spread_over_mid": 0.10}
SCEN_CFG = {"price_step_pct": 0.05, "date_step_bdays": 5, "iv_history_min_samples": 20}
BUCKETS_CFG = {
    "zero_dte": {"min": 0, "max": 3},
    "mid": {"min": 14, "max": 92},
    "leaps": {"min": 180, "max": 1100},
}


# ------------------------------------------------------------------- pricing

def test_bs_price_matches_reference_values():
    # 经典教科书参照值：S=100, K=100, r=5%, sigma=20%, T=1y
    call = bs_price("call", 100, 100, 1.0, 0.05, 0.20)
    put = bs_price("put", 100, 100, 1.0, 0.05, 0.20)
    assert abs(call - 10.4506) < 1e-3
    assert abs(put - 5.5735) < 1e-3


def test_put_call_parity():
    s, k, t, r, iv = 105.0, 95.0, 0.5, 0.04, 0.35
    call = bs_price("call", s, k, t, r, iv)
    put = bs_price("put", s, k, t, r, iv)
    assert abs((call - put) - (s - k * math.exp(-r * t))) < 1e-9


def test_bs_price_at_expiry_is_intrinsic():
    assert bs_price("call", 110, 100, 0.0, 0.05, 0.2) == 10.0
    assert bs_price("put", 90, 100, 0.0, 0.05, 0.2) == 10.0


def test_greeks_sane():
    g = bs_greeks("call", 100, 100, 0.25, 0.05, 0.3)
    assert 0.4 < g["delta"] < 0.7
    assert g["gamma"] > 0
    assert g["theta_per_day"] < 0
    assert g["vega_per_pct"] > 0


# --------------------------------------------------------- liquidity filter

def _quote(kind="call", strike=100.0, expiry="2026-09-18", bid=4.9, ask=5.1,
           volume=50, oi=500, iv=0.30):
    return OptionQuote(contract_symbol=f"TEST{strike}{kind}", kind=kind, expiry=expiry,
                       strike=strike, bid=bid, ask=ask, last=(bid + ask) / 2,
                       volume=volume, open_interest=oi, iv=iv)


def test_liquidity_filter_rules():
    assert passes_liquidity(_quote(), LIQ_CFG)
    assert not passes_liquidity(_quote(oi=50), LIQ_CFG)          # OI < 100
    assert not passes_liquidity(_quote(volume=5), LIQ_CFG)       # 量 < 10
    assert not passes_liquidity(_quote(bid=4.0, ask=6.0), LIQ_CFG)  # 价差 40% > 10%


# --------------------------------------------------------------- synthetic chain

def _synthetic_chain(spot=100.0):
    """构造覆盖三个到期分桶、行权价 70-130 的合成链，IV 由 BS 反推口径直接指定，
    价格 = BS 理论价 ± 微小价差，保证过流动性过滤。近月 IV 抬高模拟事件定价。"""
    today = date.today()
    expiries = [str(today + timedelta(days=d)) for d in (2, 45, 400)]
    ivs = {expiries[0]: 0.55, expiries[1]: 0.32, expiries[2]: 0.30}  # 近月 IV 抬高
    rate = 0.04
    quotes = []
    for expiry in expiries:
        t = (date.fromisoformat(expiry) - today).days / 365.0
        for strike in range(70, 131, 5):
            for kind in ("call", "put"):
                theo = bs_price(kind, spot, float(strike), t, rate, ivs[expiry])
                mid = max(theo, 0.05)
                half_spread = min(mid * 0.04, 0.5)
                quotes.append(OptionQuote(
                    contract_symbol=f"SYN{expiry}{strike}{kind[0].upper()}",
                    kind=kind, expiry=expiry, strike=float(strike),
                    bid=round(mid - half_spread, 2), ask=round(mid + half_spread, 2),
                    last=round(mid, 2), volume=200, open_interest=1500, iv=ivs[expiry]))
    return OptionChainData(symbol="SYN", spot=spot, fetched_at="2026-07-02T20:30:00Z",
                           expiries=expiries, quotes=quotes)


def test_term_structure_shows_front_month_elevation():
    chain = _synthetic_chain()
    ts = compute_term_structure(chain)
    assert len(ts) == 3
    assert ts[0]["atm_iv"] > ts[1]["atm_iv"] > ts[2]["atm_iv"]


def test_snapshot_and_iv_rank():
    chain = _synthetic_chain()
    snap = compute_options_snapshot(chain)
    assert snap["pc_volume_ratio"] == 1.0  # 合成链 call/put 成交量对称
    assert snap["atm_iv"] == 0.55
    assert snap["total_oi"] > 0

    assert iv_rank_from_history(0.55, [0.3] * 5, min_samples=20)["status"] == "insufficient"
    # 历史为 0.30-0.59 共30个样本，0.55 应落在 26/30 = 86.7 百分位
    rank = iv_rank_from_history(0.55, [0.30 + i * 0.01 for i in range(30)], min_samples=20)
    assert rank["status"] == "ok" and abs(rank["rank"] - 86.7) < 0.1

    assert iv_median_from_history([0.3] * 5, min_samples=20) is None
    assert iv_median_from_history([0.2, 0.3, 0.4] * 10, min_samples=20) == 0.3


# ------------------------------------------------------------------ scenarios

def _single_call_position(chain):
    q = next(q for q in chain.quotes if q.kind == "call" and q.strike == 100.0 and q.expiry == chain.expiries[1])
    return [{"kind": "call", "strike": 100.0, "expiry": q.expiry, "direction": 1, "qty": 1,
             "entry_price": q.mid, "iv": q.iv, "contract_symbol": q.contract_symbol,
             "desc": f"买入 {q.expiry} 100 Call"}]


def test_scenario_report_dual_iv_and_earnings_flag():
    chain = _synthetic_chain()
    position = _single_call_position(chain)
    expected_date = date.today() + timedelta(days=30)
    earnings_inside = str(date.today() + timedelta(days=10))

    report = build_scenario_report(position, expected_price=112.0, expected_date=expected_date,
                                   rate=0.04, iv_median_1y=0.25, scen_cfg=SCEN_CFG,
                                   next_earnings_date=earnings_inside)
    # 涨到 112：情景① 盈利；情景② IV 从 0.32 砍到 0.25，盈亏必须更差（IV crush 方向正确）
    assert report["scenario_a"]["pnl"] > 0
    assert report["scenario_b"]["status"] == "ok"
    assert report["scenario_b"]["pnl"] < report["scenario_a"]["pnl"]
    assert report["crosses_earnings"] is True
    # 单腿买 call：最大亏损 = 权利金，有界；上方无界
    assert not report["risk"]["max_loss_unbounded"]
    assert report["risk"]["max_gain_unbounded"]
    assert abs(report["risk"]["max_loss"] + position[0]["entry_price"] * 100) < 1.0
    # 敏感性矩阵 3×3
    assert len(report["sensitivity"]["prices"]) == 3
    assert len(report["sensitivity"]["rows"]) == 3


def test_scenario_b_missing_without_history():
    chain = _synthetic_chain()
    position = _single_call_position(chain)
    report = build_scenario_report(position, 110.0, date.today() + timedelta(days=30),
                                   0.04, iv_median_1y=None, scen_cfg=SCEN_CFG, next_earnings_date=None)
    assert report["scenario_b"]["status"] == "missing"
    assert report["crosses_earnings"] is False


def test_position_pnl_short_leg_sign():
    chain = _synthetic_chain()
    q = next(q for q in chain.quotes if q.kind == "call" and q.strike == 100.0 and q.expiry == chain.expiries[1])
    short_leg = [{"kind": "call", "strike": 100.0, "expiry": q.expiry, "direction": -1, "qty": 1,
                  "entry_price": q.mid, "iv": q.iv, "contract_symbol": "x", "desc": "x"}]
    expiry_date = date.fromisoformat(q.expiry)
    # 到期归零（价格远低于行权价）：卖方赚足权利金
    assert abs(position_pnl(short_leg, 80.0, expiry_date, 0.04) - q.mid * 100) < 1e-6
    # 到期深度价内：卖方亏损
    assert position_pnl(short_leg, 130.0, expiry_date, 0.04) < 0


# ----------------------------------------------------------------- recommender

def test_recommend_matrix_hard_rules():
    chain = _synthetic_chain()
    cells = recommend_matrix(chain, view="bull", rate=0.04, liq_cfg=LIQ_CFG, buckets_cfg=BUCKETS_CFG)

    # 硬规则：保守×末日永远拒绝
    assert cells["zero_dte"]["conservative"]["status"] == "refused"
    # 其余格子在合成链上应产出推荐
    assert cells["mid"]["neutral"]["status"] == "ok"
    assert cells["leaps"]["conservative"]["status"] == "ok"

    # 每个推荐都必须有最大亏损/盈亏平衡/保证金/理由
    for bucket in cells.values():
        for cell in bucket.values():
            if cell["status"] != "ok":
                continue
            for rec in cell["recs"]:
                assert "max_loss" in rec and "breakevens" in rec
                assert rec["margin_estimate"] >= 0
                assert rec["reason"]
                assert 1 <= len(rec["legs"]) <= 4


def test_recommend_matrix_direction_mismatch_flagged():
    chain = _synthetic_chain()
    cells = recommend_matrix(chain, view="bear", rate=0.04, liq_cfg=LIQ_CFG,
                             buckets_cfg=BUCKETS_CFG, v3_direction="看多")
    ok_recs = [rec for bucket in cells.values() for cell in bucket.values()
               if cell["status"] == "ok" for rec in cell["recs"]]
    assert ok_recs
    assert any(rec["direction_fit"] and "分歧" in rec["direction_fit"] for rec in ok_recs)


def test_recommend_matrix_illiquid_chain_refuses_honestly():
    chain = _synthetic_chain()
    for q in chain.quotes:
        q.open_interest = 5  # 全链流动性不达标
    cells = recommend_matrix(chain, view="bull", rate=0.04, liq_cfg=LIQ_CFG, buckets_cfg=BUCKETS_CFG)
    assert cells["mid"]["neutral"]["status"] == "no_contract"


# -------------------------------------------------------------------- render

def test_render_options_page(tmp_path):
    chain = _synthetic_chain()
    position = _single_call_position(chain)
    report = build_scenario_report(position, 112.0, date.today() + timedelta(days=30), 0.04,
                                   iv_median_1y=None, scen_cfg=SCEN_CFG,
                                   next_earnings_date=str(date.today() + timedelta(days=10)))
    cells = recommend_matrix(chain, view="bull", rate=0.04, liq_cfg=LIQ_CFG, buckets_cfg=BUCKETS_CFG)

    site_dir = tmp_path / "site"
    render_options_page(chain, LIQ_CFG, 0.04, None, compute_term_structure(chain),
                        {"status": "insufficient", "samples": 0, "required": 20},
                        report, {"expected_price": 112.0, "expected_date": "2026-08-01",
                                 "position_desc": position[0]["desc"]},
                        cells, "bull", as_of_date="2026-07-02", generated_at="2026-07-02T20:30:00Z",
                        output_dir=str(site_dir), is_sample_data=True)

    html = (site_dir / "options.html").read_text(encoding="utf-8")
    assert "模型局限与免责" in html
    assert "IV crush" in html          # 跨财报警示条
    assert "不建议" in html             # 保守×末日拒绝
    assert "最大亏损" in html
    assert "历史样本不足" in html        # 情景B与IV Rank的诚实缺失
