"""期权数据抽象接口（guidebook 5.6）：financial-service 首选实现 + yfinance 降级实现
共用同一份数据结构和接口契约，上层（pricing/scenarios/recommender/render）只依赖本文件，
换数据源不改任何业务代码。"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from core.fetchers.base import FetchResult

logger = logging.getLogger(__name__)


class FinancialServiceUnavailable(RuntimeError):
    """financial-service 数据源无法初始化（未配置/快照目录不存在）。"""


@dataclass
class OptionQuote:
    contract_symbol: str
    kind: str            # call | put
    expiry: str          # YYYY-MM-DD
    strike: float
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    volume: int
    open_interest: int
    iv: Optional[float]  # 年化隐含波动率，小数（0.35 = 35%）

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.last

    @property
    def spread_over_mid(self) -> Optional[float]:
        m = self.mid
        if m is None or m <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / m


@dataclass
class OptionChainData:
    symbol: str
    spot: float
    fetched_at: str
    expiries: list[str] = field(default_factory=list)
    quotes: list[OptionQuote] = field(default_factory=list)

    def for_expiry(self, expiry: str) -> list[OptionQuote]:
        return [q for q in self.quotes if q.expiry == expiry]


def passes_liquidity(q: OptionQuote, liq_cfg: dict) -> bool:
    """流动性硬过滤（guidebook 5.6 功能一）：OI、成交量、买卖价差三条同时满足才通过。
    无流动性合约的理论盈亏没有实盘意义，推荐引擎的每条腿也必须过这个门槛。"""
    if q.open_interest < liq_cfg["min_open_interest"]:
        return False
    if q.volume < liq_cfg["min_volume"]:
        return False
    spread = q.spread_over_mid
    if spread is None or spread > liq_cfg["max_spread_over_mid"]:
        return False
    return True


class OptionsFetcher(ABC):
    @abstractmethod
    def get_chain(self, symbol: str, max_expiries: int = 8) -> FetchResult:
        """返回 FetchResult[OptionChainData]，含现价和 max_expiries 个最近到期日的全部合约。"""
        raise NotImplementedError

    @abstractmethod
    def get_risk_free_rate(self) -> FetchResult:
        """返回 FetchResult[float]，年化无风险利率（小数）。"""
        raise NotImplementedError


def get_options_fetcher(v5_cfg: dict, fetch_cfg: dict) -> OptionsFetcher:
    """按 config 选择数据源。financial_service 未配置完成时的行为由 allow_fallback 控制：
    true = 显式告警后降级 yfinance；false = 抛错停止。绝不静默换源。"""
    provider = v5_cfg.get("provider", "yfinance")
    if provider == "financial_service":
        from core.fetchers.options_financial_service import FinancialServiceOptionsFetcher
        try:
            return FinancialServiceOptionsFetcher(v5_cfg["financial_service"], fetch_cfg)
        except FinancialServiceUnavailable as exc:
            if not v5_cfg.get("allow_fallback", False):
                raise
            logger.warning("financial-service 不可用（%s），按 allow_fallback=true 降级为 yfinance", exc)

    from core.fetchers.options_yfinance import YFinanceOptionsFetcher
    return YFinanceOptionsFetcher(v5_cfg, fetch_cfg)
