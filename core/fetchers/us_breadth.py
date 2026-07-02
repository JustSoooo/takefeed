"""Market-breadth indicator: what fraction of an index's constituents trade
above their 50/200-day moving average. Starts on Dow 30 per guidebook 2.2
(cheap to fetch, good enough to validate the logic) before scaling to the
S&P 500 full constituent list."""
from pathlib import Path

import pandas as pd

from core.fetchers.base import FetchResult
from core.fetchers.us_market import USMarketFetcher

CONSTITUENTS_DIR = Path(__file__).parent


def load_constituents(universe: str = "dow30") -> list[str]:
    path = CONSTITUENTS_DIR / f"constituents_{universe}.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _extract_close(multi_df: pd.DataFrame, symbol: str) -> pd.Series | None:
    try:
        if isinstance(multi_df.columns, pd.MultiIndex):
            return multi_df[symbol]["Close"].dropna()
        return multi_df["Close"].dropna()
    except (KeyError, TypeError):
        return None


def compute_breadth_series(fetcher: USMarketFetcher, symbols: list[str], lookback_period: str = "2y") -> FetchResult:
    """Return a daily breadth series (% of constituents above the 50/200MA,
    averaged) covering roughly the trailing year, with enough lookback ahead
    of it for the 200-day MA to be valid at the series' start."""
    res = fetcher.get_multi_history(symbols, period=lookback_period)
    if not res.ok:
        return res

    closes = {}
    for sym in symbols:
        s = _extract_close(res.data, sym)
        if s is not None and len(s) > 200:
            closes[sym] = s
    if len(closes) < max(5, len(symbols) // 2):
        return FetchResult(ok=False, error=f"too many constituents missing data ({len(closes)}/{len(symbols)} usable)")

    frame = pd.DataFrame(closes)
    above50 = frame.gt(frame.rolling(50).mean())
    above200 = frame.gt(frame.rolling(200).mean())
    daily_breadth = ((above50.mean(axis=1) + above200.mean(axis=1)) / 2 * 100).dropna()
    if daily_breadth.empty:
        return FetchResult(ok=False, error="breadth series empty after rolling-window warmup")

    trailing_1y = daily_breadth.tail(252)
    return FetchResult(ok=True, data={
        "series": trailing_1y,
        "today": float(trailing_1y.iloc[-1]),
        "constituents_used": len(closes),
        "constituents_total": len(symbols),
    })
