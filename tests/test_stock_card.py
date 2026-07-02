"""Unit tests for V3 (stock scorecards): rendering and report-section building
with synthetic Block data, plus the news-classification fallback path when no
ZHIPU_API_KEY is configured. Live yfinance fetchers are exercised manually
against real network access -- see docs/cron.md.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.narrative.generate import classify_news_sentiment
from core.render.render_v3 import build_v3_report_section, render_v3_dashboard
from core.render.report import write_daily_report
from core.scoring.v3_stock_card import Block, serialize_card

LLM_CFG = {"model": "glm-4.6", "max_tokens": 512}


def _sample_card():
    return {
        "symbol": "AAPL",
        "current_price": 210.5,
        "financial_momentum": Block("ok", raw={
            "revenue_qoq": 0.032, "revenue_yoy": 0.081, "eps_beat_latest": True, "eps_surprise_pct": 4.2,
        }),
        "institutional": Block("ok", raw={
            "median_target": 235.0, "current_price": 210.5, "upside_pct": 0.1164,
            "rating_changes_recent": [{"date": "2026-06-20", "firm": "Example Capital", "action": "up", "from_grade": "Hold", "to_grade": "Buy"}],
        }),
        "relative_strength": Block("ok", raw={
            "sector_etf": "XLK", "ret_mid": 0.06, "sector_ret_mid": 0.04, "excess_mid": 0.02,
            "ret_long": 0.12, "sector_ret_long": 0.09, "excess_long": 0.03,
        }),
        "event_calendar": Block("missing", note="AAPL: no earnings date in calendar"),
        "news": Block("ok", raw=[
            {"title": "Apple reports record services revenue", "publisher": "Example Wire", "link": "https://example.com/1", "published_at": "2026-06-30T12:00:00+00:00"},
        ]),
        "sentiment_items": [
            {"title": "Apple reports record services revenue", "publisher": "Example Wire", "link": "https://example.com/1",
             "published_at": "2026-06-30T12:00:00+00:00", "label": "利多", "reason": "标题提及服务营收创纪录"},
        ],
    }


def test_serialize_card_is_json_safe():
    card = _sample_card()
    serialized = serialize_card(card)
    assert serialized["financial_momentum"]["status"] == "ok"
    assert serialized["financial_momentum"]["raw"]["revenue_qoq"] == 0.032
    assert serialized["event_calendar"]["status"] == "missing"
    import json
    json.dumps(serialized, ensure_ascii=False)  # must not raise


def test_classify_news_sentiment_without_api_key_degrades_gracefully(monkeypatch):
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    headlines = [{"title": "Example headline", "publisher": "Wire", "link": None, "published_at": None}]
    result = classify_news_sentiment("AAPL", headlines, LLM_CFG)
    assert len(result["items"]) == 1
    assert result["items"][0]["title"] == "Example headline"  # original title preserved verbatim
    assert result["items"][0]["label"] == "未分类"


def test_classify_news_sentiment_empty_headlines():
    result = classify_news_sentiment("AAPL", [], LLM_CFG)
    assert result["items"] == []


def test_render_v3_dashboard_and_report(tmp_path):
    cards = [_sample_card()]
    site_dir = tmp_path / "site"
    reports_dir = tmp_path / "reports"

    render_v3_dashboard(cards, as_of_date="2026-07-02", generated_at="2026-07-02T20:30:00Z",
                         output_dir=str(site_dir), is_sample_data=True)
    html = (site_dir / "stocks.html").read_text(encoding="utf-8")
    assert "AAPL" in html
    assert "利多" in html
    assert "数据缺失" in html  # event_calendar is missing

    section = build_v3_report_section(cards)
    report_path = write_daily_report("2026-07-02", [section], str(reports_dir))
    md = report_path.read_text(encoding="utf-8")
    assert "V3 · 个股评分卡" in md
    assert "AAPL" in md
