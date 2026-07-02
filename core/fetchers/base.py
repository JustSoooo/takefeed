"""Market-agnostic fetcher interface. US implementation lives in us_market.py /
us_breadth.py; a CN (akshare) implementation can be dropped in behind the same
interface in a later phase without touching scoring or narrative code."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class FetchResult:
    """Uniform envelope for every fetcher call.

    ok=False must never be papered over with stale data: callers propagate the
    failure so the affected scoring dimension is marked 'missing' for the day
    instead of silently reusing yesterday's numbers (guidebook 6, rule 1)."""
    ok: bool
    data: Optional[Any] = None
    error: Optional[str] = None


class MarketFetcher(ABC):
    """Abstract interface a market implementation (us_market, cn_market...) fulfills."""

    @abstractmethod
    def get_quote_history(self, symbol: str, period: str = "1y") -> FetchResult:
        """Return a DataFrame with at least a 'Close' column, indexed by date."""
        raise NotImplementedError

    @abstractmethod
    def get_latest_close(self, symbol: str) -> FetchResult:
        raise NotImplementedError
