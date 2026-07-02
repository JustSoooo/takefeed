"""Unit tests for the financial-service snapshot-file adapter: schema validation,
freshness enforcement, symbol mismatch, factory fallback behavior."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.fetchers.options_base import FinancialServiceUnavailable, get_options_fetcher
from core.fetchers.options_financial_service import FinancialServiceOptionsFetcher

FETCH_CFG = {"max_retries": 1, "backoff_base_seconds": 0}


def _fs_cfg(tmp_path, max_age_hours=24):
    return {"snapshot_dir": str(tmp_path), "max_age_hours": max_age_hours}


def _write_snapshot(tmp_path, symbol="NVDA", age_hours=1.0, **overrides):
    payload = {
        "symbol": symbol,
        "spot": 187.32,
        "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
        "risk_free_rate": 0.042,
        "quotes": [
            {"kind": "call", "expiry": "2026-08-21", "strike": 190.0, "bid": 5.1, "ask": 5.3,
             "last": 5.22, "volume": 1234, "open_interest": 5678, "iv": 0.42},
            {"kind": "put", "expiry": "2026-08-21", "strike": 185.0, "bid": 4.0, "ask": 4.2,
             "last": 4.1, "volume": 900, "open_interest": 4000, "iv": 0.44},
        ],
    }
    payload.update(overrides)
    (tmp_path / f"{symbol}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_valid_snapshot_loads(tmp_path):
    _write_snapshot(tmp_path)
    fetcher = FinancialServiceOptionsFetcher(_fs_cfg(tmp_path), FETCH_CFG)
    res = fetcher.get_chain("NVDA")
    assert res.ok
    assert res.data.spot == 187.32
    assert len(res.data.quotes) == 2
    assert res.data.expiries == ["2026-08-21"]

    rate = fetcher.get_risk_free_rate()
    assert rate.ok and rate.data == 0.042


def test_stale_snapshot_rejected(tmp_path):
    _write_snapshot(tmp_path, age_hours=30)
    fetcher = FinancialServiceOptionsFetcher(_fs_cfg(tmp_path, max_age_hours=24), FETCH_CFG)
    res = fetcher.get_chain("NVDA")
    assert not res.ok
    assert "过期" in res.error


def test_missing_snapshot_gives_instructions(tmp_path):
    fetcher = FinancialServiceOptionsFetcher(_fs_cfg(tmp_path), FETCH_CFG)
    res = fetcher.get_chain("AAPL")
    assert not res.ok
    assert "financial_service_snapshot.md" in res.error


def test_symbol_mismatch_rejected(tmp_path):
    _write_snapshot(tmp_path, symbol="NVDA")
    (tmp_path / "MSFT.json").write_text((tmp_path / "NVDA.json").read_text(), encoding="utf-8")
    fetcher = FinancialServiceOptionsFetcher(_fs_cfg(tmp_path), FETCH_CFG)
    res = fetcher.get_chain("MSFT")
    assert not res.ok and "不匹配" in res.error


def test_bad_quote_field_reports_position(tmp_path):
    _write_snapshot(tmp_path, quotes=[{"kind": "call", "expiry": "2026-08-21", "strike": 190.0}])
    fetcher = FinancialServiceOptionsFetcher(_fs_cfg(tmp_path), FETCH_CFG)
    res = fetcher.get_chain("NVDA")
    assert not res.ok and "quotes[0]" in res.error


def test_nonexistent_dir_raises_unavailable(tmp_path):
    with pytest.raises(FinancialServiceUnavailable):
        FinancialServiceOptionsFetcher({"snapshot_dir": str(tmp_path / "nope"), "max_age_hours": 24}, FETCH_CFG)


def test_factory_fallback_and_strict_mode(tmp_path):
    v5_cfg = {"provider": "financial_service", "allow_fallback": True,
              "financial_service": {"snapshot_dir": str(tmp_path / "nope"), "max_age_hours": 24},
              "risk_free_ticker": "^IRX"}
    fetcher = get_options_fetcher(v5_cfg, FETCH_CFG)
    assert type(fetcher).__name__ == "YFinanceOptionsFetcher"

    v5_cfg["allow_fallback"] = False
    with pytest.raises(FinancialServiceUnavailable):
        get_options_fetcher(v5_cfg, FETCH_CFG)

    # 目录存在时 financial_service 正常启用
    v5_cfg["financial_service"]["snapshot_dir"] = str(tmp_path)
    fetcher = get_options_fetcher(v5_cfg, FETCH_CFG)
    assert type(fetcher).__name__ == "FinancialServiceOptionsFetcher"
