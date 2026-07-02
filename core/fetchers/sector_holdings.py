"""SPDR Select Sector ETF holdings, used for V2's internal-health check (is a
strong sector's move broad-based, or driven by one or two mega-cap names).
The SSGA holdings file is an unofficial download endpoint with a format that
can change without notice, so this degrades to FetchResult(ok=False) on any
parse failure rather than guessing (same discipline as sentiment_scrape.py)."""
from io import BytesIO

import pandas as pd
import requests

from core.fetchers.base import FetchResult

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-console/1.0)"}
_HOLDINGS_URL = "https://www.ssga.com/us/en/individual/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{symbol}.xlsx"


def fetch_sector_holdings(symbol: str, top_n: int = 15, timeout: int = 15) -> FetchResult:
    """Return the top-N holdings by weight for a Select Sector SPDR ETF."""
    url = _HOLDINGS_URL.format(symbol=symbol.lower())
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        df = pd.read_excel(BytesIO(resp.content), skiprows=4)
        df.columns = [str(c).strip().lower() for c in df.columns]

        ticker_col = next((c for c in df.columns if c in ("ticker", "identifier")), None)
        weight_col = next((c for c in df.columns if "weight" in c), None)
        if ticker_col is None or weight_col is None:
            return FetchResult(ok=False, error=f"{symbol}: unexpected holdings columns {list(df.columns)}")

        df = df[[ticker_col, weight_col]].dropna()
        df[weight_col] = pd.to_numeric(df[weight_col], errors="coerce")
        df = df.dropna().sort_values(weight_col, ascending=False)
        df = df[~df[ticker_col].astype(str).str.contains("CASH|USD", case=False, na=False)]

        holdings = [
            {"ticker": str(r[ticker_col]).strip(), "weight": float(r[weight_col])}
            for _, r in df.head(top_n).iterrows()
        ]
        if not holdings:
            return FetchResult(ok=False, error=f"{symbol}: no holdings parsed from file")
        return FetchResult(ok=True, data=holdings)
    except Exception as exc:
        return FetchResult(ok=False, error=f"{symbol} holdings fetch failed: {exc}")
