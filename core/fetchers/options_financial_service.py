"""financial-service 期权数据源（首选实现，guidebook 5.6）。

financial-service 是安装在使用者本地 Claude Code 里的 Anthropic 金融分析 skill——
它不是 HTTP 服务，无法被本 Python 管道直接调用。因此本实现采用**快照文件契约**：

  1. 在本地 Claude Code 会话中，让 Claude 用 financial-service skill 拉取期权链，
     按 docs/financial_service_snapshot.md 定义的 JSON schema 写入
     data/options_snapshots/<SYMBOL>.json
  2. 本 fetcher 读取该文件并做两道硬校验：schema 完整性 + 新鲜度
     （fetched_at 距今超过 max_age_hours 直接拒绝——过期期权数据比没有更危险，
     绝不静默用旧快照冒充实时数据）

生成快照的提示词示例见 docs/financial_service_snapshot.md。
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.fetchers.base import FetchResult
from core.fetchers.options_base import (FinancialServiceUnavailable, OptionChainData,
                                        OptionQuote, OptionsFetcher)

logger = logging.getLogger(__name__)

_REQUIRED_QUOTE_FIELDS = {"kind", "expiry", "strike", "volume", "open_interest"}


class FinancialServiceOptionsFetcher(OptionsFetcher):
    def __init__(self, fs_cfg: dict, fetch_cfg: dict):
        self.snapshot_dir = Path(fs_cfg.get("snapshot_dir") or "")
        self.max_age_hours = fs_cfg.get("max_age_hours", 24)
        self._last_rate: float | None = None
        if not str(self.snapshot_dir):
            raise FinancialServiceUnavailable(
                "financial-service 快照目录未配置（config v5.financial_service.snapshot_dir）")
        if not self.snapshot_dir.is_dir():
            raise FinancialServiceUnavailable(
                f"financial-service 快照目录不存在：{self.snapshot_dir}。"
                f"请在本地 Claude Code 中用 financial-service skill 生成快照"
                f"（方法见 docs/financial_service_snapshot.md），或改用 provider: yfinance")

    def get_chain(self, symbol: str, max_expiries: int = 8) -> FetchResult:
        path = self.snapshot_dir / f"{symbol.upper()}.json"
        if not path.exists():
            return FetchResult(ok=False, error=(
                f"{symbol}: 快照文件不存在（{path}）。请在本地 Claude Code 会话中用 "
                f"financial-service skill 生成，提示词模板见 docs/financial_service_snapshot.md"))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return FetchResult(ok=False, error=f"{symbol}: 快照文件解析失败（{exc}）")

        for key in ("symbol", "spot", "fetched_at", "quotes"):
            if key not in payload:
                return FetchResult(ok=False, error=f"{symbol}: 快照缺少必需字段 '{key}'，schema 见文档")
        if payload["symbol"].upper() != symbol.upper():
            return FetchResult(ok=False, error=f"{symbol}: 快照文件 symbol 不匹配（{payload['symbol']}）")

        try:
            fetched_at = datetime.fromisoformat(str(payload["fetched_at"]).replace("Z", "+00:00"))
        except ValueError:
            return FetchResult(ok=False, error=f"{symbol}: fetched_at 不是合法的 ISO 时间戳")
        age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600
        if age_hours > self.max_age_hours:
            return FetchResult(ok=False, error=(
                f"{symbol}: 快照已过期（{age_hours:.1f} 小时前生成，上限 {self.max_age_hours} 小时）。"
                f"过期期权数据不可用于估值，请重新生成快照"))

        quotes = []
        for i, q in enumerate(payload["quotes"]):
            missing = _REQUIRED_QUOTE_FIELDS - set(q)
            if missing:
                return FetchResult(ok=False, error=f"{symbol}: quotes[{i}] 缺少字段 {sorted(missing)}")
            if q["kind"] not in ("call", "put"):
                return FetchResult(ok=False, error=f"{symbol}: quotes[{i}].kind 必须是 call|put")
            quotes.append(OptionQuote(
                contract_symbol=q.get("contract_symbol", f"{symbol}-{q['expiry']}-{q['strike']}-{q['kind']}"),
                kind=q["kind"], expiry=q["expiry"], strike=float(q["strike"]),
                bid=q.get("bid"), ask=q.get("ask"), last=q.get("last"),
                volume=int(q["volume"]), open_interest=int(q["open_interest"]),
                iv=q.get("iv"),
            ))
        if not quotes:
            return FetchResult(ok=False, error=f"{symbol}: 快照 quotes 为空")

        expiries = sorted({q.expiry for q in quotes})[:max_expiries]
        quotes = [q for q in quotes if q.expiry in set(expiries)]
        self._last_rate = payload.get("risk_free_rate")

        return FetchResult(ok=True, data=OptionChainData(
            symbol=symbol.upper(), spot=float(payload["spot"]),
            fetched_at=payload["fetched_at"], expiries=expiries, quotes=quotes))

    def get_risk_free_rate(self) -> FetchResult:
        if self._last_rate is not None:
            return FetchResult(ok=True, data=float(self._last_rate))
        return FetchResult(ok=False, error="快照未提供 risk_free_rate 字段")
