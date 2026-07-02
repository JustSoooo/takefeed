"""预期情景收益预估（guidebook 5.6 功能二）：
- 中途估值：预期日那天（未到期时点）用 BS 计算各腿理论价值，汇总组合盈亏
- 双 IV 情景强制并排：① IV 维持当前 ② IV 回落至近1年中位数（历史样本不足时
  该情景显式标记缺失，绝不用编造的中位数——见 config v5.scenarios.iv_history_min_samples）
- 敏感性矩阵：预期价格 ±5% × 预期日期 ±5 个交易日
- 跨财报检测：预期持有区间覆盖财报日时输出 IV crush 警示

腿(leg)结构：{"kind": "call"|"put"|"stock", "strike": float|None, "expiry": "YYYY-MM-DD"|None,
             "direction": 1|-1, "qty": int, "entry_price": float, "iv": float|None}
期权腿乘数 100；stock 腿 qty 按股数计、乘数 1。
"""
from datetime import date, datetime, timedelta

from core.options.pricing import bs_price, intrinsic, year_fraction

OPTION_MULTIPLIER = 100


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def leg_unit_value(leg: dict, spot: float, at_date: date, rate: float, iv_override: float | None = None) -> float:
    if leg["kind"] == "stock":
        return spot
    expiry = _parse_date(leg["expiry"])
    t = year_fraction(at_date, expiry)
    iv = iv_override if iv_override is not None else (leg.get("iv") or 0.0)
    if t <= 0:
        return intrinsic(leg["kind"], spot, leg["strike"])
    return bs_price(leg["kind"], spot, leg["strike"], t, rate, iv)


def position_pnl(position: list[dict], spot: float, at_date: date, rate: float,
                 iv_override: float | None = None) -> float:
    total = 0.0
    for leg in position:
        mult = 1 if leg["kind"] == "stock" else OPTION_MULTIPLIER
        override = None if leg["kind"] == "stock" else iv_override
        value = leg_unit_value(leg, spot, at_date, rate, override)
        total += leg["direction"] * leg["qty"] * mult * (value - leg["entry_price"])
    return round(total, 2)


def entry_cost(position: list[dict]) -> float:
    """建仓净成本（正 = 净付出/借方，负 = 净收入/贷方）。"""
    total = 0.0
    for leg in position:
        mult = 1 if leg["kind"] == "stock" else OPTION_MULTIPLIER
        total += leg["direction"] * leg["qty"] * mult * leg["entry_price"]
    return round(total, 2)


def earliest_option_expiry(position: list[dict]) -> date | None:
    expiries = [_parse_date(leg["expiry"]) for leg in position if leg["kind"] != "stock"]
    return min(expiries) if expiries else None


def payoff_curve(position: list[dict], ref_price: float, rate: float,
                 low_mult: float = 0.7, high_mult: float = 1.3, points: int = 61) -> dict:
    """到期 payoff（多到期日组合以最早到期日为评估时点，长腿按 BS 剩余价值计，
    页面会注明这一约定）+ 用于图表的价格网格。"""
    eval_date = earliest_option_expiry(position) or date.today()
    prices = [ref_price * (low_mult + (high_mult - low_mult) * i / (points - 1)) for i in range(points)]
    pnl = [position_pnl(position, p, eval_date, rate) for p in prices]
    return {"prices": [round(p, 2) for p in prices], "pnl": pnl, "eval_date": str(eval_date)}


def risk_metrics(position: list[dict], ref_price: float, rate: float) -> dict:
    """从到期盈亏网格提取最大亏损/最大盈利/盈亏平衡点。网格从 0 起步（股价下界，
    卖put类策略的最大亏损必须覆盖归零情形）到 2.5x；上界边缘仍在恶化/改善则
    标记为无界（如裸卖 call 的理论无限亏损）。"""
    eval_date = earliest_option_expiry(position) or date.today()
    prices = [ref_price * (2.5 * i / 400) for i in range(401)]
    pnl = [position_pnl(position, p, eval_date, rate) for p in prices]

    min_pnl, max_pnl = min(pnl), max(pnl)
    loss_unbounded = pnl[-1] == min_pnl and pnl[-1] < pnl[-2]
    gain_unbounded = pnl[-1] == max_pnl and pnl[-1] > pnl[-2]

    breakevens = []
    for i in range(1, len(prices)):
        a, b = pnl[i - 1], pnl[i]
        if (a < 0 <= b) or (a >= 0 > b):
            if b != a:
                x = prices[i - 1] + (0 - a) * (prices[i] - prices[i - 1]) / (b - a)
                breakevens.append(round(x, 2))

    return {
        "max_loss": round(min_pnl, 2), "max_loss_unbounded": loss_unbounded,
        "max_gain": round(max_pnl, 2), "max_gain_unbounded": gain_unbounded,
        "breakevens": breakevens[:4],
    }


def business_day_offset(d: date, offset: int) -> date:
    step = 1 if offset >= 0 else -1
    remaining = abs(offset)
    current = d
    while remaining > 0:
        current += timedelta(days=step)
        if current.weekday() < 5:
            remaining -= 1
    return current


def sensitivity_matrix(position: list[dict], expected_price: float, expected_date: date,
                       rate: float, price_step_pct: float, date_step_bdays: int) -> dict:
    """3×3 盈亏矩阵：价格 {-step, 0, +step} × 日期 {-N, 0, +N 个交易日}（IV 维持当前）。"""
    today = date.today()
    price_points = [round(expected_price * (1 + k * price_step_pct), 2) for k in (-1, 0, 1)]
    date_points = [max(today, business_day_offset(expected_date, k * date_step_bdays)) for k in (-1, 0, 1)]
    rows = []
    for d in date_points:
        rows.append({
            "date": str(d),
            "pnl": [position_pnl(position, p, d, rate) for p in price_points],
        })
    return {"prices": price_points, "rows": rows}


def build_scenario_report(position: list[dict], expected_price: float, expected_date: date,
                          rate: float, iv_median_1y: float | None, scen_cfg: dict,
                          next_earnings_date: str | None) -> dict:
    """汇总功能二的全部产出。iv_median_1y 为 None 表示历史样本不足，情景B显式缺失。"""
    today = date.today()
    scenario_a_pnl = position_pnl(position, expected_price, expected_date, rate)

    if iv_median_1y is not None:
        scenario_b = {"status": "ok", "iv": iv_median_1y,
                      "pnl": position_pnl(position, expected_price, expected_date, rate, iv_override=iv_median_1y)}
    else:
        scenario_b = {"status": "missing",
                      "note": "近1年 IV 中位数历史样本不足（本系统按日累积 ATM IV，样本达标后自动启用），"
                              "该情景暂不输出，避免用编造的中位数误导判断"}

    crosses_earnings = False
    if next_earnings_date:
        try:
            earnings = _parse_date(next_earnings_date)
            crosses_earnings = today <= earnings <= expected_date
        except ValueError:
            pass

    return {
        "entry_cost": entry_cost(position),
        "scenario_a": {"pnl": scenario_a_pnl},
        "scenario_b": scenario_b,
        "risk": risk_metrics(position, expected_price, rate),
        "payoff": payoff_curve(position, expected_price, rate),
        "sensitivity": sensitivity_matrix(position, expected_price, expected_date, rate,
                                          scen_cfg["price_step_pct"], scen_cfg["date_step_bdays"]),
        "crosses_earnings": crosses_earnings,
        "next_earnings_date": next_earnings_date,
    }
