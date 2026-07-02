"""Unit tests for V2 (sector rotation) pure logic: persistence tracking off
SQLite history and rendering. Live yfinance/SPDR-holdings fetchers are
exercised manually against real network access -- see docs/cron.md.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod
from core.render.render_v2 import build_v2_report_section, render_v2_dashboard
from core.render.report import write_daily_report
from core.scoring.v2_rotation import attach_persistence

V2_CFG = {
    "persistence": {"top_n": 3, "lookback_days": 20, "hit_ratio_threshold": 0.7},
}


def _matrix_raw(xlk_rank_mid=1, xlp_rank_mid=9):
    return {
        "benchmark": "SPY",
        "benchmark_returns": {"short": 0.01, "mid": 0.03, "long": 0.06},
        "sectors": [
            {"symbol": "XLK", "name": "科技", "style": "cyclical",
             "ret_short": 0.02, "ret_mid": 0.08, "ret_long": 0.15,
             "excess_short": 0.01, "excess_mid": 0.05, "excess_long": 0.09,
             "rank_short": 1, "rank_mid": xlk_rank_mid, "rank_long": 1},
            {"symbol": "XLP", "name": "必需消费", "style": "defensive",
             "ret_short": 0.00, "ret_mid": 0.01, "ret_long": 0.02,
             "excess_short": -0.01, "excess_mid": -0.02, "excess_long": -0.04,
             "rank_short": 9, "rank_mid": xlp_rank_mid, "rank_long": 9},
        ],
    }


def test_persistence_no_history_marks_insufficient_sample():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        db_path = f"{d}/test.sqlite"
        with dbmod.connect(db_path) as conn:
            matrix = _matrix_raw()
            attach_persistence(conn, "2026-07-02", V2_CFG, matrix)
            xlk = next(s for s in matrix["sectors"] if s["symbol"] == "XLK")
            assert xlk["persistence_hit_ratio"] is None
            assert xlk["persistent_strong"] is False


def test_persistence_detects_consistent_top_rank():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        db_path = f"{d}/test.sqlite"
        with dbmod.connect(db_path) as conn:
            # simulate 10 prior days where XLK held rank_mid <= 3
            for i in range(10):
                past_date = f"2026-06-{10 + i:02d}"
                dbmod.upsert_metric(conn, past_date, "v2", "sector_matrix", _matrix_raw(xlk_rank_mid=2),
                                     None, None, "ok", None, f"{past_date}T20:30:00Z")

            matrix = _matrix_raw(xlk_rank_mid=1)
            attach_persistence(conn, "2026-07-02", V2_CFG, matrix)
            xlk = next(s for s in matrix["sectors"] if s["symbol"] == "XLK")
            xlp = next(s for s in matrix["sectors"] if s["symbol"] == "XLP")

            assert xlk["persistence_hit_ratio"] == 1.0
            assert xlk["persistent_strong"] is True
            assert xlp["persistent_strong"] is False  # never in top-3


def test_render_v2_dashboard_and_report(tmp_path):
    matrix = _matrix_raw(xlk_rank_mid=1)
    matrix["sectors"][0]["persistent_strong"] = True
    matrix["sectors"][0]["persistence_hit_ratio"] = 0.85
    matrix["sectors"][0]["persistence_observed_days"] = 20
    matrix["sectors"][1]["persistent_strong"] = False
    matrix["sectors"][1]["persistence_hit_ratio"] = 0.0
    matrix["sectors"][1]["persistence_observed_days"] = 20

    health_results = {
        "XLK": {"status": "ok", "raw": {"holdings_counted": 15, "pct_holdings_up_1m": 80.0,
                                          "top3_weight_pct": 45.0, "concentrated": True}},
    }

    site_dir = tmp_path / "site"
    reports_dir = tmp_path / "reports"
    render_v2_dashboard(matrix, health_results, as_of_date="2026-07-02", generated_at="2026-07-02T20:30:00Z",
                         output_dir=str(site_dir), is_sample_data=True)

    html = (site_dir / "rotation.html").read_text(encoding="utf-8")
    assert "科技" in html
    assert "持续强势" in html
    assert "集中度偏高" in html

    section = build_v2_report_section(matrix, health_results)
    report_path = write_daily_report("2026-07-02", [section], str(reports_dir))
    md = report_path.read_text(encoding="utf-8")
    assert "V2 · 板块轮动" in md
    assert "持续强势" in md
