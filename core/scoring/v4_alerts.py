"""V4 目标股池监控 + 异常预警：对 watchlist 做每日巡检，触发规则时生成告警条目
（guidebook 5.2）。复用 V3 已经拉到的机构评级 / 事件日历 / 新闻数据，避免重复取数；
均线突破、成交量异动、涨跌幅异常这几条价格类规则需要独立取带 Volume 的历史数据。
"""
from dataclasses import dataclass

from core import db as dbmod
from core.fetchers.us_market import USMarketFetcher


@dataclass
class Alert:
    symbol: str
    rule: str
    severity: str  # info | warning
    message: str


def check_ma_cross(fetcher: USMarketFetcher, symbol: str, windows: list[int], period: str) -> list[Alert]:
    res = fetcher.get_quote_history(symbol, period=period)
    if not res.ok:
        return []
    close = res.data["Close"].dropna()
    alerts = []
    for w in windows:
        if len(close) < w + 2:
            continue
        ma = close.rolling(w).mean()
        today_close, yesterday_close = close.iloc[-1], close.iloc[-2]
        today_ma, yesterday_ma = ma.iloc[-1], ma.iloc[-2]
        if yesterday_close <= yesterday_ma and today_close > today_ma:
            alerts.append(Alert(symbol, f"ma_cross_up_{w}", "warning", f"收盘价站上{w}日均线"))
        elif yesterday_close >= yesterday_ma and today_close < today_ma:
            alerts.append(Alert(symbol, f"ma_cross_down_{w}", "warning", f"收盘价跌破{w}日均线"))
    return alerts


def check_volume_spike(fetcher: USMarketFetcher, symbol: str, multiplier: float, lookback_days: int, period: str) -> Alert | None:
    res = fetcher.get_quote_history(symbol, period=period)
    if not res.ok or "Volume" not in res.data.columns:
        return None
    volume = res.data["Volume"].dropna()
    if len(volume) < lookback_days + 1:
        return None
    today_volume = volume.iloc[-1]
    baseline = volume.iloc[-1 - lookback_days:-1].mean()
    if baseline > 0 and today_volume > baseline * multiplier:
        return Alert(symbol, "volume_spike", "warning",
                     f"成交量 {today_volume:,.0f} 为近{lookback_days}日均量的 {today_volume / baseline:.1f} 倍")
    return None


def check_return_outlier(fetcher: USMarketFetcher, symbol: str, std_window: int, std_multiplier: float, period: str) -> Alert | None:
    res = fetcher.get_quote_history(symbol, period=period)
    if not res.ok:
        return None
    close = res.data["Close"].dropna()
    returns = close.pct_change().dropna()
    if len(returns) < std_window + 1:
        return None
    today_return = returns.iloc[-1]
    baseline_std = returns.iloc[-1 - std_window:-1].std()
    if baseline_std and abs(today_return) > std_multiplier * baseline_std:
        direction = "上涨" if today_return > 0 else "下跌"
        return Alert(symbol, "return_outlier", "warning",
                     f"单日{direction} {abs(today_return) * 100:.2f}%，超过近{std_window}日波动率的 {std_multiplier:.0f} 倍标准差")
    return None


def check_earnings_proximity(symbol: str, event_calendar_block) -> Alert | None:
    if event_calendar_block.status != "ok" or not event_calendar_block.raw.get("earnings_soon"):
        return None
    raw = event_calendar_block.raw
    return Alert(symbol, "earnings_proximity", "info",
                 f"距离财报日 {raw['next_earnings_date']} 仅剩 {raw['days_until']} 个交易日，注意仓位")


def check_rating_change_today(symbol: str, institutional_block, run_date: str) -> list[Alert]:
    if institutional_block.status != "ok":
        return []
    alerts = []
    for change in institutional_block.raw.get("rating_changes_recent", []):
        if change.get("date") == run_date:
            alerts.append(Alert(symbol, "rating_change", "info",
                                 f"{change.get('firm')} 今日{change.get('action')}评级（{change.get('from_grade')} → {change.get('to_grade')}）"))
    return alerts


def check_news_spike(conn, run_date: str, symbol: str, today_count: int, lookback_days: int,
                      multiplier: float, min_history: int) -> Alert | None:
    history = dbmod.get_metric_history(conn, "v4", f"{symbol}::news_count", before_date=run_date, limit=lookback_days)
    counts = [raw["count"] for _, raw in history]
    if len(counts) < min_history:
        return None
    avg = sum(counts) / len(counts)
    if avg > 0 and today_count > avg * multiplier:
        return Alert(symbol, "news_spike", "warning",
                     f"今日新闻 {today_count} 条，为近{lookback_days}日日均（{avg:.1f} 条）的 {today_count / avg:.1f} 倍")
    return None


def check_iv_jump(conn, run_date: str, symbol: str, today_atm_iv: float | None, threshold: float) -> Alert | None:
    """IV 单日跳升 > threshold：市场可能在为该股定价某个事件（guidebook 5.4）。
    对比对象是最近一个已落库的期权快照，无历史则跳过。"""
    if today_atm_iv is None:
        return None
    history = dbmod.get_metric_history(conn, "v5", f"{symbol}::options_snapshot", before_date=run_date, limit=1)
    if not history:
        return None
    prev_iv = history[0][1].get("atm_iv")
    if not prev_iv:
        return None
    jump = today_atm_iv / prev_iv - 1
    if jump > threshold:
        return Alert(symbol, "iv_jump", "warning",
                     f"ATM IV 单日跳升 {jump * 100:.0f}%（{prev_iv * 100:.1f}% → {today_atm_iv * 100:.1f}%），"
                     f"市场可能在为某个事件定价")
    return None


def check_option_volume_anomaly(conn, run_date: str, symbol: str, today_volume: int, max_strike_share: float,
                                 max_volume_strike, lookback_days: int, multiplier: float,
                                 concentration_threshold: float, min_history: int) -> Alert | None:
    """单日期权成交量 > N日均量 M 倍 且集中于单一行权价：异常大单活动（guidebook 5.4）。"""
    history = dbmod.get_metric_history(conn, "v5", f"{symbol}::options_snapshot", before_date=run_date, limit=lookback_days)
    volumes = [raw["total_volume"] for _, raw in history if raw.get("total_volume")]
    if len(volumes) < min_history:
        return None
    avg = sum(volumes) / len(volumes)
    if avg > 0 and today_volume > avg * multiplier and max_strike_share >= concentration_threshold:
        strike_str = f"{max_volume_strike:g}" if max_volume_strike is not None else "未知"
        return Alert(symbol, "option_volume_anomaly", "warning",
                     f"期权成交量 {today_volume:,} 为近{lookback_days}日均量的 {today_volume / avg:.1f} 倍，"
                     f"且 {max_strike_share:.0%} 集中于行权价 {strike_str}（异常大单活动）")
    return None


def generate_options_alerts(conn, run_date: str, symbol: str, options_block, v5_alerts_cfg: dict) -> list[Alert]:
    """5.4 回填的两条期权预警。options_block 为 V3 卡上的期权情绪 Block，缺失时静默跳过。"""
    if options_block is None or options_block.status != "ok":
        return []
    raw = options_block.raw
    alerts = []
    iv_alert = check_iv_jump(conn, run_date, symbol, raw.get("atm_iv"), v5_alerts_cfg["iv_jump_threshold"])
    if iv_alert:
        alerts.append(iv_alert)
    vol_alert = check_option_volume_anomaly(
        conn, run_date, symbol, raw.get("total_volume", 0), raw.get("max_strike_volume_share", 0.0),
        raw.get("max_volume_strike"), v5_alerts_cfg["volume_lookback_days"], v5_alerts_cfg["volume_multiplier"],
        v5_alerts_cfg["strike_concentration_threshold"], v5_alerts_cfg["volume_min_history"])
    if vol_alert:
        alerts.append(vol_alert)
    return alerts


def generate_alerts_for_symbol(fetcher: USMarketFetcher, symbol: str, v3_card: dict, conn, run_date: str, cfg: dict) -> list[Alert]:
    period = cfg["price_history_period"]
    alerts = []
    alerts += check_ma_cross(fetcher, symbol, cfg["ma_windows"], period)

    vol_alert = check_volume_spike(fetcher, symbol, cfg["volume_spike_multiplier"], cfg["volume_lookback_days"], period)
    if vol_alert:
        alerts.append(vol_alert)

    ret_alert = check_return_outlier(fetcher, symbol, cfg["return_std_window"], cfg["return_std_multiplier"], period)
    if ret_alert:
        alerts.append(ret_alert)

    earnings_alert = check_earnings_proximity(symbol, v3_card["event_calendar"])
    if earnings_alert:
        alerts.append(earnings_alert)

    alerts += check_rating_change_today(symbol, v3_card["institutional"], run_date)

    news_count_today = len(v3_card["news"].raw) if v3_card["news"].status == "ok" else 0
    news_alert = check_news_spike(conn, run_date, symbol, news_count_today, cfg["news_spike_lookback_days"],
                                   cfg["news_spike_multiplier"], cfg["news_spike_min_history"])
    if news_alert:
        alerts.append(news_alert)

    return alerts
