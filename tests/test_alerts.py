"""Unit tests for V4 alert rules using synthetic price series (a fake fetcher
that returns hand-built DataFrames instead of hitting yfinance) plus SQLite
history for the news-spike baseline. Live yfinance fetchers for the price
data are exercised manually against real network access -- see docs/cron.md.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod
from core.fetchers.base import FetchResult
from core.render.render_v4 import build_v4_report_section, render_v4_dashboard
from core.scoring.v3_stock_card import Block
from core.scoring.v4_alerts import (
    check_earnings_proximity,
    check_ma_cross,
    check_news_spike,
    check_rating_change_today,
    check_return_outlier,
    check_volume_spike,
    generate_alerts_for_symbol,
)

V4_CFG = {
    "price_history_period": "6mo",
    "ma_windows": [20, 50],
    "volume_spike_multiplier": 2.0,
    "volume_lookback_days": 30,
    "return_std_window": 90,
    "return_std_multiplier": 2.0,
    "news_spike_lookback_days": 30,
    "news_spike_multiplier": 3.0,
    "news_spike_min_history": 10,
}


class FakeFetcher:
    def __init__(self, df: pd.DataFrame):
        self.df = df

    def get_quote_history(self, symbol, period="1y"):
        return FetchResult(ok=True, data=self.df)


def _flat_price_series(n=150, base=100.0, volume=1_000_000):
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 0.1, n)
    close = pd.Series(base + noise, index=dates)
    volume_series = pd.Series([volume] * n, index=dates, dtype=float)
    return pd.DataFrame({"Close": close, "Volume": volume_series})


def test_check_ma_cross_up_detected():
    df = _flat_price_series(n=60, base=100.0)
    # push the last close well above the 20-day MA (which is still ~100) to force a cross-up
    df.iloc[-1, df.columns.get_loc("Close")] = 120.0
    df.iloc[-2, df.columns.get_loc("Close")] = 100.0
    fetcher = FakeFetcher(df)
    alerts = check_ma_cross(fetcher, "TEST", [20], period="6mo")
    assert any(a.rule == "ma_cross_up_20" for a in alerts)


def test_check_volume_spike_detected():
    df = _flat_price_series(n=40, volume=1_000_000)
    df.iloc[-1, df.columns.get_loc("Volume")] = 5_000_000
    fetcher = FakeFetcher(df)
    alert = check_volume_spike(fetcher, "TEST", multiplier=2.0, lookback_days=30, period="6mo")
    assert alert is not None
    assert alert.rule == "volume_spike"


def test_check_volume_spike_not_triggered_below_threshold():
    df = _flat_price_series(n=40, volume=1_000_000)
    fetcher = FakeFetcher(df)
    alert = check_volume_spike(fetcher, "TEST", multiplier=2.0, lookback_days=30, period="6mo")
    assert alert is None


def test_check_return_outlier_detected():
    df = _flat_price_series(n=120, base=100.0)
    df.iloc[-1, df.columns.get_loc("Close")] = df.iloc[-2]["Close"] * 1.15  # +15% single-day jump
    fetcher = FakeFetcher(df)
    alert = check_return_outlier(fetcher, "TEST", std_window=90, std_multiplier=2.0, period="6mo")
    assert alert is not None
    assert alert.rule == "return_outlier"


def test_check_earnings_proximity():
    soon = Block("ok", raw={"next_earnings_date": "2026-07-05", "days_until": 3, "earnings_soon": True})
    far = Block("ok", raw={"next_earnings_date": "2026-09-01", "days_until": 40, "earnings_soon": False})
    missing = Block("missing", note="no calendar")

    assert check_earnings_proximity("TEST", soon) is not None
    assert check_earnings_proximity("TEST", far) is None
    assert check_earnings_proximity("TEST", missing) is None


def test_check_rating_change_today():
    inst = Block("ok", raw={"rating_changes_recent": [
        {"date": "2026-07-02", "firm": "Example Capital", "action": "up", "from_grade": "Hold", "to_grade": "Buy"},
        {"date": "2026-06-15", "firm": "Old Firm", "action": "down", "from_grade": "Buy", "to_grade": "Hold"},
    ]})
    alerts = check_rating_change_today("TEST", inst, run_date="2026-07-02")
    assert len(alerts) == 1
    assert "Example Capital" in alerts[0].message


def test_check_news_spike_insufficient_history_returns_none():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with dbmod.connect(f"{d}/test.sqlite") as conn:
            alert = check_news_spike(conn, "2026-07-02", "TEST", today_count=20,
                                      lookback_days=30, multiplier=3.0, min_history=10)
            assert alert is None


def test_check_news_spike_detects_spike_with_history():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with dbmod.connect(f"{d}/test.sqlite") as conn:
            for i in range(15):
                past_date = f"2026-06-{10 + i:02d}"
                dbmod.upsert_metric(conn, past_date, "v4", "TEST::news_count", {"count": 2},
                                     None, None, "ok", None, f"{past_date}T20:30:00Z")
            alert = check_news_spike(conn, "2026-07-02", "TEST", today_count=10,
                                      lookback_days=30, multiplier=3.0, min_history=10)
            assert alert is not None
            assert alert.rule == "news_spike"


def test_generate_alerts_for_symbol_integration():
    import tempfile
    df = _flat_price_series(n=150, base=100.0)
    df.iloc[-1, df.columns.get_loc("Close")] = 130.0  # trend + return outlier
    df.iloc[-1, df.columns.get_loc("Volume")] = 10_000_000  # volume spike
    fetcher = FakeFetcher(df)
    card = {
        "symbol": "TEST",
        "event_calendar": Block("ok", raw={"next_earnings_date": "2026-07-05", "days_until": 3, "earnings_soon": True}),
        "institutional": Block("ok", raw={"rating_changes_recent": []}),
        "news": Block("ok", raw=[{"title": "x"} for _ in range(3)]),
    }
    with tempfile.TemporaryDirectory() as d:
        with dbmod.connect(f"{d}/test.sqlite") as conn:
            alerts = generate_alerts_for_symbol(fetcher, "TEST", card, conn, "2026-07-02", V4_CFG)
            rules = {a.rule for a in alerts}
            assert "earnings_proximity" in rules
            assert "volume_spike" in rules
            assert all(a.symbol == "TEST" for a in alerts)


def test_check_iv_jump_and_option_volume_anomaly():
    import tempfile
    from core.scoring.v4_alerts import check_iv_jump, check_option_volume_anomaly, generate_options_alerts

    with tempfile.TemporaryDirectory() as d:
        with dbmod.connect(f"{d}/test.sqlite") as conn:
            # 无历史：两条规则都应跳过而非报假警
            assert check_iv_jump(conn, "2026-07-02", "TEST", 0.50, threshold=0.20) is None
            assert check_option_volume_anomaly(conn, "2026-07-02", "TEST", 90000, 0.5, 100.0,
                                                lookback_days=30, multiplier=3.0,
                                                concentration_threshold=0.4, min_history=10) is None

            for i in range(15):
                past_date = f"2026-06-{10 + i:02d}"
                dbmod.upsert_metric(conn, past_date, "v5", "TEST::options_snapshot",
                                     {"atm_iv": 0.30, "total_volume": 10000, "total_oi": 50000},
                                     None, None, "ok", None, f"{past_date}T20:30:00Z")

            # IV 0.30 -> 0.40 = +33% > 20% 阈值
            iv_alert = check_iv_jump(conn, "2026-07-02", "TEST", 0.40, threshold=0.20)
            assert iv_alert is not None and iv_alert.rule == "iv_jump"
            assert check_iv_jump(conn, "2026-07-02", "TEST", 0.33, threshold=0.20) is None

            # 量 9 倍于均量且 50% 集中单一行权价
            vol_alert = check_option_volume_anomaly(conn, "2026-07-02", "TEST", 90000, 0.5, 100.0,
                                                     lookback_days=30, multiplier=3.0,
                                                     concentration_threshold=0.4, min_history=10)
            assert vol_alert is not None and vol_alert.rule == "option_volume_anomaly"
            # 量够大但不集中：不触发
            assert check_option_volume_anomaly(conn, "2026-07-02", "TEST", 90000, 0.2, 100.0,
                                                lookback_days=30, multiplier=3.0,
                                                concentration_threshold=0.4, min_history=10) is None

            # 组合入口：Block missing 时静默跳过
            assert generate_options_alerts(conn, "2026-07-02", "TEST",
                                            Block("missing", note="x"), {
                                                "iv_jump_threshold": 0.20, "volume_multiplier": 3.0,
                                                "volume_lookback_days": 30, "volume_min_history": 10,
                                                "strike_concentration_threshold": 0.40}) == []
            alerts = generate_options_alerts(conn, "2026-07-02", "TEST",
                                              Block("ok", raw={"atm_iv": 0.40, "total_volume": 90000,
                                                               "max_strike_volume_share": 0.5,
                                                               "max_volume_strike": 100.0}), {
                                                  "iv_jump_threshold": 0.20, "volume_multiplier": 3.0,
                                                  "volume_lookback_days": 30, "volume_min_history": 10,
                                                  "strike_concentration_threshold": 0.40})
            assert {a.rule for a in alerts} == {"iv_jump", "option_volume_anomaly"}


def test_render_v4_dashboard_and_report_with_alerts(tmp_path):
    from core.scoring.v4_alerts import Alert
    alerts = [
        Alert("AAPL", "ma_cross_up_20", "warning", "收盘价站上20日均线"),
        Alert("AAPL", "earnings_proximity", "info", "距离财报日仅剩3个交易日，注意仓位"),
    ]
    site_dir = tmp_path / "site"
    render_v4_dashboard(alerts, as_of_date="2026-07-02", generated_at="2026-07-02T20:30:00Z",
                         output_dir=str(site_dir), is_sample_data=True)
    html = (site_dir / "alerts.html").read_text(encoding="utf-8")
    assert "AAPL" in html
    assert "20日均线" in html

    section = build_v4_report_section(alerts)
    assert "AAPL" in section
    assert "预警" in section


def test_render_v4_dashboard_empty_state(tmp_path):
    site_dir = tmp_path / "site"
    render_v4_dashboard([], as_of_date="2026-07-02", generated_at="2026-07-02T20:30:00Z",
                         output_dir=str(site_dir))
    html = (site_dir / "alerts.html").read_text(encoding="utf-8")
    assert "无触发规则的异动" in html

    section = build_v4_report_section([])
    assert "无触发规则的异动" in section
