"""V3 个股综合评分卡：财务动量 / 机构态度 / 相对强度 / 事件日历都是确定性取数，
不产出单一分数——这是一张结构化卡片，供人工判断用（guidebook 4.1）。
舆情摘要的新闻抓取在这里完成（确定性），但利多/利空/中性分类是 LLM 的工作
（core/narrative/generate.py），因为guidebook 4.2 明确把"归纳分类"交给 LLM。
"""
from dataclasses import dataclass, field
from typing import Optional

from core.fetchers import us_stock
from core.fetchers.us_market import USMarketFetcher, pct_change_over


@dataclass
class Block:
    status: str  # ok | missing
    raw: dict | list = field(default_factory=dict)
    note: Optional[str] = None


def compute_relative_strength(fetcher: USMarketFetcher, symbol: str, sector_etf: Optional[str], cfg: dict) -> Block:
    if not sector_etf:
        return Block("missing", note=f"{symbol}: 未在 watchlist 中配置 sector_etf，跳过相对强度计算")

    windows = cfg["windows"]  # {mid, long}
    stock_res = fetcher.get_quote_history(symbol, period="6mo")
    if not stock_res.ok:
        return Block("missing", note=stock_res.error)
    sector_res = fetcher.get_quote_history(sector_etf, period="6mo")
    if not sector_res.ok:
        return Block("missing", note=sector_res.error)

    try:
        stock_close = stock_res.data["Close"]
        sector_close = sector_res.data["Close"]
        raw = {"sector_etf": sector_etf}
        for w, n in windows.items():
            stock_ret = pct_change_over(stock_close, n)
            sector_ret = pct_change_over(sector_close, n)
            raw[f"ret_{w}"] = round(stock_ret, 4)
            raw[f"sector_ret_{w}"] = round(sector_ret, 4)
            raw[f"excess_{w}"] = round(stock_ret - sector_ret, 4)
        return Block("ok", raw=raw)
    except ValueError as exc:
        return Block("missing", note=f"{symbol}: {exc}")


def build_stock_card(fetcher: USMarketFetcher, ticker_entry: dict, cfg: dict, fetch_cfg: dict) -> dict:
    symbol = ticker_entry["ticker"]
    sector_etf = ticker_entry.get("sector_etf")
    retry_kwargs = {"max_retries": fetch_cfg["max_retries"], "backoff_base": fetch_cfg["backoff_base_seconds"]}

    price_res = fetcher.get_latest_close(symbol)
    current_price = price_res.data if price_res.ok else None

    momentum_res = us_stock.fetch_financial_momentum(symbol, **retry_kwargs)
    momentum = Block("ok", raw=momentum_res.data) if momentum_res.ok else Block("missing", note=momentum_res.error)

    if current_price is not None:
        inst_res = us_stock.fetch_institutional_view(
            symbol, current_price, lookback_days=cfg["upgrades_downgrades_lookback_days"], **retry_kwargs,
        )
        institutional = Block("ok", raw=inst_res.data) if inst_res.ok else Block("missing", note=inst_res.error)
    else:
        institutional = Block("missing", note=price_res.error)

    relative_strength = compute_relative_strength(fetcher, symbol, sector_etf, cfg)

    calendar_res = us_stock.fetch_event_calendar(symbol, cfg["earnings_soon_threshold_days"], **retry_kwargs)
    event_calendar = Block("ok", raw=calendar_res.data) if calendar_res.ok else Block("missing", note=calendar_res.error)

    news_res = us_stock.fetch_news_headlines(
        symbol, lookback_days=cfg["news_lookback_days"], max_headlines=cfg["news_max_headlines"], **retry_kwargs,
    )
    news = Block("ok", raw=news_res.data) if news_res.ok else Block("missing", note=news_res.error)

    return {
        "symbol": symbol,
        "current_price": current_price,
        "financial_momentum": momentum,
        "institutional": institutional,
        "relative_strength": relative_strength,
        "event_calendar": event_calendar,
        "news": news,
    }


_BLOCK_FIELDS = ["financial_momentum", "institutional", "relative_strength", "event_calendar", "news"]


def serialize_card(card: dict) -> dict:
    """JSON-safe snapshot of a card for SQLite persistence (Block -> plain dict)."""
    out = {"current_price": card["current_price"], "sentiment_items": card.get("sentiment_items", [])}
    for field_name in _BLOCK_FIELDS:
        b = card[field_name]
        out[field_name] = {"status": b.status, "raw": b.raw, "note": b.note}
    return out
