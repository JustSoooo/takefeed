"""Sentiment data via web scraping: AAII weekly bull/bear survey and CNN's
Fear & Greed index. Scraping is inherently more fragile than yfinance (no
API contract at all) -- both functions return FetchResult(ok=False) on any
parse failure rather than guessing, so the sentiment dimension is marked
'missing' for the day instead of showing a stale or wrong number
(guidebook 6, rule 1; see also guidebook 9.1 on non-official data sources)."""
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from core.fetchers.base import FetchResult

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; market-console/1.0)"}


def fetch_aaii_sentiment(url: str, timeout: int = 10) -> FetchResult:
    """Scrape the latest published AAII bull/bear/neutral percentages.
    The public page only exposes the most recent week (full history is a
    paid CSV); long-run percentile therefore falls back to our own
    accumulated SQLite history (see scoring layer, min_history_for_percentile)."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table")
        if table is None:
            return FetchResult(ok=False, error="AAII: no table found on page (markup may have changed)")

        header_cells = [c.get_text(strip=True).lower() for c in table.find_all(["th"])]
        first_data_row = table.find("tbody").find("tr") if table.find("tbody") else table.find_all("tr")[1]
        cells = [c.get_text(strip=True) for c in first_data_row.find_all("td")]

        row = dict(zip(header_cells, cells)) if header_cells else {}
        bull = _parse_pct(row.get("bullish") or (cells[1] if len(cells) > 1 else None))
        neutral = _parse_pct(row.get("neutral") or (cells[2] if len(cells) > 2 else None))
        bear = _parse_pct(row.get("bearish") or (cells[3] if len(cells) > 3 else None))

        if bull is None or bear is None:
            return FetchResult(ok=False, error="AAII: could not parse bull/bear percentages from table")

        return FetchResult(ok=True, data={
            "bull_pct": bull,
            "neutral_pct": neutral,
            "bear_pct": bear,
            "spread": round(bull - bear, 2),
        })
    except Exception as exc:
        return FetchResult(ok=False, error=f"AAII scrape failed: {exc}")


def fetch_fear_greed(base_url: str, timeout: int = 10) -> FetchResult:
    """CNN Fear & Greed index. The dataviz endpoint accepts a start-date path
    segment and returns ~1y of daily history plus the current score, which
    lets us compute a real percentile immediately instead of bootstrapping."""
    start_date = (datetime.utcnow() - timedelta(days=380)).strftime("%Y-%m-%d")
    url = f"{base_url.rstrip('/')}/{start_date}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()

        current = payload.get("fear_and_greed") or {}
        current_score = current.get("score")
        current_rating = current.get("rating")

        hist_points = (payload.get("fear_and_greed_historical") or {}).get("data", [])
        history = [p["y"] for p in hist_points if "y" in p]

        if current_score is None or not history:
            return FetchResult(ok=False, error="Fear&Greed: unexpected response schema")

        percentile = round(sum(1 for v in history if v <= current_score) / len(history) * 100, 2)

        return FetchResult(ok=True, data={
            "score": float(current_score),
            "rating": current_rating,
            "percentile_1y": percentile,
            "history_points": len(history),
        })
    except Exception as exc:
        return FetchResult(ok=False, error=f"Fear&Greed fetch failed: {exc}")


def _parse_pct(raw) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace("%", "").strip())
    except ValueError:
        return None
