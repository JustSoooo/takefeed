"""Unit tests for the pure/no-network parts of the pipeline: composite scoring,
SQLite roundtrip, and HTML/markdown rendering. Live yfinance/scrape fetchers
are exercised manually against real network access (not mocked here) --
see docs/cron.md.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod
from core.render.render_v1 import build_v1_report_section, render_v1_dashboard
from core.render.report import write_daily_report
from core.scoring.v1_composite import DimensionResult, compute_composite

WEIGHTS = {
    "volatility": 0.1667, "trend": 0.1667, "breadth": 0.1667,
    "credit": 0.1667, "rotation": 0.1667, "sentiment": 0.1665,
}
THRESHOLDS = {"defensive_below": 35, "aggressive_above": 65}


def _sample_dim_results():
    return {
        "volatility": DimensionResult("volatility", "ok", raw={"vix": 14.2, "vix9d": 13.1, "vix9d_vix_ratio": 0.922}, percentile=22.0, score=12.5),
        "trend": DimensionResult("trend", "ok", raw={"symbols": {
            "SPY": {"close": 560.1, "ma20": 555.0, "ma50": 548.2, "ma200": 520.4, "above_ratio": 1.0},
            "QQQ": {"close": 480.3, "ma20": 475.0, "ma50": 468.1, "ma200": 440.2, "above_ratio": 1.0},
        }, "overall_above_ratio": 1.0}, score=18.0),
        "breadth": DimensionResult("breadth", "ok", raw={"today": 68.3, "universe": "dow30", "constituents_used": 30, "constituents_total": 30}, percentile=71.0, score=8.4),
        "credit": DimensionResult("credit", "missing", note="HYG: simulated network failure"),
        "rotation": DimensionResult("rotation", "ok", raw={
            "sectors": [
                {"symbol": "XLK", "name": "科技", "style": "cyclical", "chg_1w": 0.021, "chg_1m": 0.055},
                {"symbol": "XLP", "name": "必需消费", "style": "defensive", "chg_1w": -0.004, "chg_1m": 0.008},
            ],
            "cyclical_avg_1m": 0.055, "defensive_avg_1m": 0.008, "pct_positive_1m": 100.0,
        }, score=14.1),
        "sentiment": DimensionResult("sentiment", "ok", raw={
            "fear_greed": {"score": 62.0, "rating": "greed", "percentile_1y": 58.0, "history_points": 250},
            "aaii": {"bull_pct": 42.1, "neutral_pct": 30.5, "bear_pct": 27.4, "spread": 14.7, "percentile_bootstrap": False},
        }, score=6.2),
    }


def test_compute_composite_renormalizes_missing_dimension():
    dims = _sample_dim_results()
    score, state, weights_used = compute_composite(dims, WEIGHTS, THRESHOLDS)

    assert 0 <= score <= 100
    assert "credit" not in weights_used
    assert abs(sum(weights_used.values()) - 1.0) < 1e-9
    assert state in {"防守", "谨慎观望", "积极"}


def test_compute_composite_all_missing_raises():
    dims = {k: DimensionResult(k, "missing", note="x") for k in WEIGHTS}
    try:
        compute_composite(dims, WEIGHTS, THRESHOLDS)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_db_roundtrip(tmp_path):
    db_path = str(tmp_path / "test.sqlite")
    with dbmod.connect(db_path) as conn:
        dbmod.upsert_metric(conn, "2026-07-01", "v1", "sentiment", {"spread": 5.0}, 50.0, 2.0, "ok", None, "2026-07-01T20:30:00Z")
        dbmod.upsert_composite(conn, "2026-07-01", "v1", 55.5, "谨慎观望", WEIGHTS, "narrative text", ["do X"], "2026-07-01T20:30:00Z")

    with dbmod.connect(db_path) as conn:
        history = dbmod.get_metric_history(conn, "v1", "sentiment", before_date="2026-07-02")
        assert len(history) == 1
        assert history[0][1]["spread"] == 5.0

        composite_history = dbmod.get_composite_history(conn, "v1")
        assert len(composite_history) == 1
        assert composite_history[0]["composite_score"] == 55.5


def test_render_dashboard_and_report(tmp_path):
    dims = _sample_dim_results()
    score, state, weights_used = compute_composite(dims, WEIGHTS, THRESHOLDS)
    narrative_data = {
        "narrative": "示例叙事：VIX 处于近一年 22 百分位，波动率偏低；SPY/QQQ 全部站上 20/50/200 日均线，趋势健康；广度 68.3% 处于近1年71百分位，参与面较宽。综合来看当前状态为谨慎偏积极，信用维度数据缺失，需关注补数后的变化。",
        "suggestions": ["若 HYG/IEF 数据恢复后信用维度转负，则下调仓位基调", "关注科技板块近1月是否维持相对强势"],
    }

    site_dir = tmp_path / "site"
    reports_dir = tmp_path / "reports"
    render_v1_dashboard(score, state, dims, weights_used, narrative_data,
                         as_of_date="2026-07-02", generated_at="2026-07-02T20:30:00Z",
                         output_dir=str(site_dir), is_sample_data=True)
    section = build_v1_report_section(score, state, dims, weights_used, narrative_data)
    report_path = write_daily_report("2026-07-02", [section], str(reports_dir))

    html = (site_dir / "index.html").read_text(encoding="utf-8")
    assert "示例数据" in html
    assert state in html
    assert (site_dir / "tokens.css").exists()

    md = report_path.read_text(encoding="utf-8")
    assert "综合评分" in md
    assert "数据缺失" in md  # credit dimension
