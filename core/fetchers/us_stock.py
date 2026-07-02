"""Per-stock fundamentals/analyst/news/calendar data for V3 scorecards, via
yfinance's Ticker object. These endpoints are notably less stable across
yfinance versions than plain price history, so every function here parses
defensively and returns FetchResult(ok=False) rather than guessing at a
schema that may have shifted (same discipline as the rest of core/fetchers)."""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from core.fetchers.base import FetchResult
from core.fetchers.us_market import retry_call

logger = logging.getLogger(__name__)


def _get_ticker(symbol: str, max_retries=3, backoff_base=2) -> yf.Ticker:
    return retry_call(lambda: yf.Ticker(symbol), max_retries, backoff_base, what=f"Ticker({symbol})")


def fetch_financial_momentum(symbol: str, max_retries=3, backoff_base=2) -> FetchResult:
    """Revenue/EPS YoY and QoQ trend, and whether the latest quarter beat estimates."""
    try:
        t = _get_ticker(symbol, max_retries, backoff_base)
        income = retry_call(lambda: t.quarterly_income_stmt, max_retries, backoff_base, what=f"income_stmt({symbol})")
        if income is None or income.empty or "Total Revenue" not in income.index:
            return FetchResult(ok=False, error=f"{symbol}: quarterly income statement unavailable")

        revenue = income.loc["Total Revenue"].dropna()
        if len(revenue) < 2:
            return FetchResult(ok=False, error=f"{symbol}: insufficient quarterly revenue history")
        revenue_qoq = float(revenue.iloc[0] / revenue.iloc[1] - 1.0)
        revenue_yoy = float(revenue.iloc[0] / revenue.iloc[4] - 1.0) if len(revenue) >= 5 else None

        eps_beat = None
        eps_surprise_pct = None
        try:
            eps_hist = retry_call(lambda: t.earnings_history, max_retries, backoff_base, what=f"earnings_history({symbol})")
            if eps_hist is not None and not eps_hist.empty:
                latest = eps_hist.iloc[-1]
                eps_beat = bool(latest.get("epsActual", 0) > latest.get("epsEstimate", 0))
                eps_surprise_pct = float(latest.get("surprisePercent")) if latest.get("surprisePercent") is not None else None
        except Exception as exc:
            logger.info("%s: earnings_history unavailable (%s)", symbol, exc)

        return FetchResult(ok=True, data={
            "revenue_qoq": round(revenue_qoq, 4),
            "revenue_yoy": round(revenue_yoy, 4) if revenue_yoy is not None else None,
            "eps_beat_latest": eps_beat,
            "eps_surprise_pct": eps_surprise_pct,
        })
    except Exception as exc:
        return FetchResult(ok=False, error=f"{symbol} financial momentum fetch failed: {exc}")


def fetch_institutional_view(symbol: str, current_price: float, lookback_days: int = 30,
                              max_retries=3, backoff_base=2) -> FetchResult:
    """Analyst price-target consensus vs. current price, and rating changes in the
    trailing `lookback_days`."""
    try:
        t = _get_ticker(symbol, max_retries, backoff_base)

        targets = retry_call(lambda: t.analyst_price_targets, max_retries, backoff_base, what=f"price_targets({symbol})")
        median_target = None
        if isinstance(targets, dict):
            median_target = targets.get("median") or targets.get("mean")
        elif isinstance(targets, pd.DataFrame) and not targets.empty:
            median_target = float(targets["Median"].iloc[-1]) if "Median" in targets.columns else None
        if median_target is None:
            return FetchResult(ok=False, error=f"{symbol}: analyst price targets unavailable")
        upside_pct = float(median_target / current_price - 1.0) if current_price else None

        recent_changes = []
        try:
            ud = retry_call(lambda: t.upgrades_downgrades, max_retries, backoff_base, what=f"upgrades_downgrades({symbol})")
            if ud is not None and not ud.empty:
                cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
                ud = ud.reset_index()
                date_col = "GradeDate" if "GradeDate" in ud.columns else ud.columns[0]
                ud[date_col] = pd.to_datetime(ud[date_col], utc=True, errors="coerce")
                recent = ud[ud[date_col] >= cutoff]
                for _, row in recent.iterrows():
                    recent_changes.append({
                        "date": str(row[date_col].date()) if pd.notna(row[date_col]) else None,
                        "firm": row.get("Firm"), "action": row.get("Action"),
                        "from_grade": row.get("FromGrade"), "to_grade": row.get("ToGrade"),
                    })
        except Exception as exc:
            logger.info("%s: upgrades_downgrades unavailable (%s)", symbol, exc)

        return FetchResult(ok=True, data={
            "median_target": round(float(median_target), 2),
            "current_price": round(current_price, 2),
            "upside_pct": round(upside_pct, 4) if upside_pct is not None else None,
            "rating_changes_recent": recent_changes,
        })
    except Exception as exc:
        return FetchResult(ok=False, error=f"{symbol} institutional view fetch failed: {exc}")


def fetch_event_calendar(symbol: str, earnings_soon_threshold_days: int = 5,
                          max_retries=3, backoff_base=2) -> FetchResult:
    try:
        t = _get_ticker(symbol, max_retries, backoff_base)
        cal = retry_call(lambda: t.calendar, max_retries, backoff_base, what=f"calendar({symbol})")
        earnings_dates = (cal or {}).get("Earnings Date")
        if not earnings_dates:
            return FetchResult(ok=False, error=f"{symbol}: no earnings date in calendar")
        next_date = min(d for d in earnings_dates if d is not None)
        days_until = (next_date - datetime.now().date()).days
        return FetchResult(ok=True, data={
            "next_earnings_date": str(next_date),
            "days_until": days_until,
            "earnings_soon": 0 <= days_until <= earnings_soon_threshold_days,
        })
    except Exception as exc:
        return FetchResult(ok=False, error=f"{symbol} event calendar fetch failed: {exc}")


def fetch_news_headlines(symbol: str, lookback_days: int = 7, max_headlines: int = 15,
                          max_retries=3, backoff_base=2) -> FetchResult:
    try:
        t = _get_ticker(symbol, max_retries, backoff_base)
        raw_news = retry_call(lambda: t.news, max_retries, backoff_base, what=f"news({symbol})")
        if not raw_news:
            return FetchResult(ok=True, data=[])

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        items = []
        for entry in raw_news:
            # yfinance has used a couple of shapes for this payload across versions;
            # handle both the flat and the nested-under-"content" forms.
            content = entry.get("content", entry)
            title = content.get("title")
            publisher = (content.get("provider") or {}).get("displayName") if isinstance(content.get("provider"), dict) else content.get("publisher")
            link = ((content.get("canonicalUrl") or {}).get("url")) if isinstance(content.get("canonicalUrl"), dict) else content.get("link")
            pub_time = content.get("pubDate") or entry.get("providerPublishTime")

            published_at = _parse_publish_time(pub_time)
            if title is None or (published_at and published_at < cutoff):
                continue
            items.append({
                "title": title, "publisher": publisher, "link": link,
                "published_at": published_at.isoformat() if published_at else None,
            })

        items.sort(key=lambda i: i["published_at"] or "", reverse=True)
        return FetchResult(ok=True, data=items[:max_headlines])
    except Exception as exc:
        return FetchResult(ok=False, error=f"{symbol} news fetch failed: {exc}")


def _parse_publish_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        parsed = pd.to_datetime(value, utc=True)
        return parsed.to_pydatetime()
    except Exception:
        return None
