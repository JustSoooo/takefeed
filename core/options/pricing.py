"""Black-Scholes 定价、希腊值、中途估值（guidebook 5.6 功能二的数学层）。
纯确定性计算，标准库实现（erf 累积正态），无任何网络或 LLM 依赖，可独立单测。

模型局限（页面免责区同步声明）：欧式假设，不处理美式提前行权、股息除权、盘后跳空。
"""
import math
from datetime import date


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)


def intrinsic(kind: str, spot: float, strike: float) -> float:
    if kind == "call":
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def bs_price(kind: str, spot: float, strike: float, t_years: float, rate: float, iv: float) -> float:
    """欧式期权理论价。t_years <= 0 或 iv <= 0 时退化为内在价值。"""
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return intrinsic(kind, spot, strike)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + iv * iv / 2.0) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    discount = math.exp(-rate * t_years)
    if kind == "call":
        return spot * norm_cdf(d1) - strike * discount * norm_cdf(d2)
    return strike * discount * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_greeks(kind: str, spot: float, strike: float, t_years: float, rate: float, iv: float) -> dict:
    """Delta / Gamma / Theta(每日) / Vega(每1%波动率)。到期或退化情形返回边界值。"""
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        itm = intrinsic(kind, spot, strike) > 0
        delta = (1.0 if itm else 0.0) if kind == "call" else (-1.0 if itm else 0.0)
        return {"delta": delta, "gamma": 0.0, "theta_per_day": 0.0, "vega_per_pct": 0.0}
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + iv * iv / 2.0) * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    discount = math.exp(-rate * t_years)
    pdf_d1 = norm_pdf(d1)

    delta = norm_cdf(d1) if kind == "call" else norm_cdf(d1) - 1.0
    gamma = pdf_d1 / (spot * iv * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0
    theta_annual = -spot * pdf_d1 * iv / (2.0 * sqrt_t)
    if kind == "call":
        theta_annual -= rate * strike * discount * norm_cdf(d2)
    else:
        theta_annual += rate * strike * discount * norm_cdf(-d2)
    return {
        "delta": round(delta, 4), "gamma": round(gamma, 6),
        "theta_per_day": round(theta_annual / 365.0, 4), "vega_per_pct": round(vega, 4),
    }


def year_fraction(from_date: date, to_date: date) -> float:
    return max(0.0, (to_date - from_date).days / 365.0)
