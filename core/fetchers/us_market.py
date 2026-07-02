"""US market data via yfinance. All network calls funnel through here: retried
with exponential backoff, cached per process run, and returned as FetchResult
so a Yahoo outage degrades to a 'missing dimension' instead of a silent stale
number (guidebook 6, rule 1; yfinance is an unofficial API, see guidebook 9.1)."""
import logging
import time

import pandas as pd
import yfinance as yf

from core.fetchers.base import FetchResult, MarketFetcher

logger = logging.getLogger(__name__)


def retry_call(fn, max_retries=3, backoff_base=2, what=""):
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:  # yfinance raises assorted exception types
            last_err = exc
            wait = backoff_base * (2 ** attempt)
            logger.warning("fetch failed (%s), attempt %d/%d: %s", what, attempt + 1, max_retries, exc)
            if attempt < max_retries - 1:
                time.sleep(wait)
    raise last_err


class USMarketFetcher(MarketFetcher):
    def __init__(self, max_retries=3, backoff_base=2):
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._cache: dict[str, pd.DataFrame] = {}

    def get_quote_history(self, symbol: str, period: str = "1y") -> FetchResult:
        cache_key = f"{symbol}:{period}"
        if cache_key in self._cache:
            return FetchResult(ok=True, data=self._cache[cache_key])
        try:
            df = retry_call(
                lambda: yf.Ticker(symbol).history(period=period, auto_adjust=False),
                self.max_retries, self.backoff_base, what=f"history({symbol})",
            )
            if df is None or df.empty:
                return FetchResult(ok=False, error=f"{symbol}: empty history returned")
            self._cache[cache_key] = df
            return FetchResult(ok=True, data=df)
        except Exception as exc:
            return FetchResult(ok=False, error=f"{symbol}: {exc}")

    def get_latest_close(self, symbol: str) -> FetchResult:
        res = self.get_quote_history(symbol, period="5d")
        if not res.ok:
            return res
        return FetchResult(ok=True, data=float(res.data["Close"].iloc[-1]))

    def get_multi_history(self, symbols: list[str], period: str = "1y") -> FetchResult:
        """Batch download for breadth-style calculations over many tickers at once."""
        cache_key = f"multi:{','.join(sorted(symbols))}:{period}"
        if cache_key in self._cache:
            return FetchResult(ok=True, data=self._cache[cache_key])
        try:
            df = retry_call(
                lambda: yf.download(symbols, period=period, auto_adjust=False, group_by="ticker", progress=False),
                self.max_retries, self.backoff_base, what="multi-download",
            )
            if df is None or df.empty:
                return FetchResult(ok=False, error="multi-download: empty result")
            self._cache[cache_key] = df
            return FetchResult(ok=True, data=df)
        except Exception as exc:
            return FetchResult(ok=False, error=f"multi-download: {exc}")


def pct_change_over(series: pd.Series, trading_days: int) -> float:
    """Percent change of the last value vs. `trading_days` sessions ago."""
    if len(series) <= trading_days:
        raise ValueError("not enough history for requested window")
    return float(series.iloc[-1] / series.iloc[-1 - trading_days] - 1.0)


def percentile_of_last(series: pd.Series) -> float:
    """Percentile rank (0-100) of the series' last value within itself."""
    clean = series.dropna()
    if clean.empty:
        raise ValueError("empty series")
    return float((clean <= clean.iloc[-1]).mean() * 100)


def extract_symbol_close(multi_df: pd.DataFrame, symbol: str) -> pd.Series | None:
    """Pull one ticker's Close series out of a yf.download(..., group_by='ticker') frame."""
    try:
        if isinstance(multi_df.columns, pd.MultiIndex):
            return multi_df[symbol]["Close"].dropna()
        return multi_df["Close"].dropna()
    except (KeyError, TypeError):
        return None


def above_ma_ratio(close: pd.Series, windows: list[int]) -> float:
    """Fraction of the given moving-average windows the last close sits above."""
    hits = 0
    for w in windows:
        if len(close) < w:
            continue
        ma = close.rolling(w).mean().iloc[-1]
        if close.iloc[-1] > ma:
            hits += 1
    return hits / len(windows) if windows else 0.0
