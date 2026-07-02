"""yfinance 期权数据实现（降级源，guidebook 5.6：免费；IV 和希腊值质量一般，价差数据可用）。
IV 由 yfinance 直接提供（Yahoo 计算值），本系统不重算 IV，只在页面免责区声明其质量限制。"""
import logging
import math
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from core.fetchers.base import FetchResult
from core.fetchers.options_base import OptionChainData, OptionQuote, OptionsFetcher
from core.fetchers.us_market import retry_call

logger = logging.getLogger(__name__)


def _f(value) -> float | None:
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _i(value) -> int:
    v = _f(value)
    return int(v) if v is not None else 0


class YFinanceOptionsFetcher(OptionsFetcher):
    def __init__(self, v5_cfg: dict, fetch_cfg: dict):
        self.v5_cfg = v5_cfg
        self.max_retries = fetch_cfg["max_retries"]
        self.backoff_base = fetch_cfg["backoff_base_seconds"]

    def get_chain(self, symbol: str, max_expiries: int = 8) -> FetchResult:
        try:
            ticker = retry_call(lambda: yf.Ticker(symbol), self.max_retries, self.backoff_base, what=f"Ticker({symbol})")

            hist = retry_call(lambda: ticker.history(period="5d"), self.max_retries, self.backoff_base, what=f"spot({symbol})")
            if hist is None or hist.empty:
                return FetchResult(ok=False, error=f"{symbol}: 现价获取失败")
            spot = float(hist["Close"].iloc[-1])

            expiries = list(ticker.options or [])[:max_expiries]
            if not expiries:
                return FetchResult(ok=False, error=f"{symbol}: 无可用期权到期日（可能无期权或接口变动）")

            quotes: list[OptionQuote] = []
            for expiry in expiries:
                chain = retry_call(lambda e=expiry: ticker.option_chain(e), self.max_retries, self.backoff_base,
                                   what=f"option_chain({symbol},{expiry})")
                for kind, frame in (("call", chain.calls), ("put", chain.puts)):
                    if frame is None or frame.empty:
                        continue
                    for _, row in frame.iterrows():
                        quotes.append(OptionQuote(
                            contract_symbol=str(row.get("contractSymbol", "")),
                            kind=kind, expiry=expiry,
                            strike=float(row["strike"]),
                            bid=_f(row.get("bid")), ask=_f(row.get("ask")), last=_f(row.get("lastPrice")),
                            volume=_i(row.get("volume")), open_interest=_i(row.get("openInterest")),
                            iv=_f(row.get("impliedVolatility")),
                        ))

            if not quotes:
                return FetchResult(ok=False, error=f"{symbol}: 期权链为空")
            return FetchResult(ok=True, data=OptionChainData(
                symbol=symbol, spot=round(spot, 4),
                fetched_at=datetime.now(timezone.utc).isoformat(),
                expiries=expiries, quotes=quotes,
            ))
        except Exception as exc:
            return FetchResult(ok=False, error=f"{symbol} 期权链拉取失败: {exc}")

    def get_risk_free_rate(self) -> FetchResult:
        ticker = self.v5_cfg.get("risk_free_ticker", "^IRX")
        try:
            hist = retry_call(lambda: yf.Ticker(ticker).history(period="5d"),
                              self.max_retries, self.backoff_base, what=f"risk_free({ticker})")
            if hist is None or hist.empty:
                return FetchResult(ok=False, error=f"{ticker}: 无风险利率取数失败")
            return FetchResult(ok=True, data=round(float(hist["Close"].iloc[-1]) / 100.0, 6))
        except Exception as exc:
            return FetchResult(ok=False, error=f"{ticker} 无风险利率取数失败: {exc}")
