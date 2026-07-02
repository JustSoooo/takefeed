"""期权链的确定性分析指标：ATM IV 期限结构（功能一）与每日期权情绪快照
（5.4 回填：P/C 成交量比、ATM IV、总持仓量、单一行权价集中度）。
IV Rank 需要近1年 IV 历史——免费源拿不到现成历史，本系统按日把 ATM IV 落 SQLite
自行累积，样本不足时显式标记，绝不编造百分位。"""
from core.fetchers.options_base import OptionChainData


def atm_iv_for_expiry(chain: OptionChainData, expiry: str) -> float | None:
    """现价最近行权价的 call/put IV 均值。"""
    quotes = [q for q in chain.for_expiry(expiry) if q.iv is not None and q.iv > 0]
    if not quotes:
        return None
    nearest_strike = min({q.strike for q in quotes}, key=lambda s: abs(s - chain.spot))
    ivs = [q.iv for q in quotes if q.strike == nearest_strike]
    return round(sum(ivs) / len(ivs), 4) if ivs else None


def compute_term_structure(chain: OptionChainData) -> list[dict]:
    """各到期日的 ATM IV 连线，用于判断近月是否因事件（财报）被抬高（guidebook 5.6 功能一）。"""
    out = []
    for expiry in chain.expiries:
        iv = atm_iv_for_expiry(chain, expiry)
        if iv is not None:
            out.append({"expiry": expiry, "atm_iv": iv})
    return out


def compute_options_snapshot(chain: OptionChainData) -> dict:
    """当日期权情绪快照（确定性计算，供 V3 展示与 V4 预警规则使用）。"""
    total_call_volume = sum(q.volume for q in chain.quotes if q.kind == "call")
    total_put_volume = sum(q.volume for q in chain.quotes if q.kind == "put")
    total_volume = total_call_volume + total_put_volume
    total_oi = sum(q.open_interest for q in chain.quotes)

    volume_by_strike: dict[float, int] = {}
    for q in chain.quotes:
        volume_by_strike[q.strike] = volume_by_strike.get(q.strike, 0) + q.volume
    max_strike_share = (max(volume_by_strike.values()) / total_volume) if total_volume > 0 else 0.0
    max_strike = max(volume_by_strike, key=volume_by_strike.get) if volume_by_strike else None

    return {
        "spot": chain.spot,
        "nearest_expiry": chain.expiries[0] if chain.expiries else None,
        "atm_iv": atm_iv_for_expiry(chain, chain.expiries[0]) if chain.expiries else None,
        "pc_volume_ratio": round(total_put_volume / total_call_volume, 3) if total_call_volume > 0 else None,
        "total_volume": total_volume,
        "total_oi": total_oi,
        "max_strike_volume_share": round(max_strike_share, 3),
        "max_volume_strike": max_strike,
    }


def iv_rank_from_history(today_iv: float, historical_ivs: list[float], min_samples: int) -> dict:
    """今日 ATM IV 在自行累积的历史中的百分位。样本不足时 status=insufficient。"""
    if len(historical_ivs) < min_samples:
        return {"status": "insufficient", "samples": len(historical_ivs), "required": min_samples}
    rank = sum(1 for v in historical_ivs if v <= today_iv) / len(historical_ivs) * 100
    return {"status": "ok", "rank": round(rank, 1), "samples": len(historical_ivs)}


def iv_median_from_history(historical_ivs: list[float], min_samples: int) -> float | None:
    """近1年 IV 中位数（情景B用）。样本不足返回 None，调用方显式标记缺失。"""
    if len(historical_ivs) < min_samples:
        return None
    ordered = sorted(historical_ivs)
    n = len(ordered)
    mid = n // 2
    return round(ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2, 4)
