#!/usr/bin/env python3
"""编排入口：fetch -> score -> narrate -> render（guidebook 6）。
建议通过 cron 在美东收盘后 30 分钟触发，见 docs/cron.md。
"""
import argparse
import logging
from datetime import datetime, timezone

import yaml

from core import db as dbmod
from core.fetchers.us_market import USMarketFetcher
from core.narrative.generate import classify_news_sentiment, generate_v1_narrative
from core.render.render_v1 import build_v1_report_section, render_v1_dashboard
from core.render.render_v2 import build_v2_report_section, render_v2_dashboard
from core.render.render_v3 import build_v3_report_section, render_v3_dashboard
from core.render.render_v4 import build_v4_report_section, render_v4_dashboard
from core.render.report import write_daily_report
from core.scoring import v1_composite as v1
from core.scoring import v2_rotation as v2
from core.scoring import v4_alerts as v4
from core.scoring.v3_stock_card import build_stock_card, serialize_card
from core.watchlist import load_watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_daily")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_v1(cfg: dict, fetcher: USMarketFetcher, conn, run_date: str, now_iso: str) -> tuple[str, dict]:
    v1_cfg = {**cfg["v1"], "history_period": cfg["fetcher"]["history_period"], "sector_etfs": cfg["sector_etfs"]}

    dim_results = {
        "volatility": v1.compute_volatility(fetcher, v1_cfg),
        "trend": v1.compute_trend(fetcher, v1_cfg),
        "breadth": v1.compute_breadth(fetcher, v1_cfg),
        "credit": v1.compute_credit(fetcher, v1_cfg),
        "rotation": v1.compute_rotation(fetcher, v1_cfg),
        "sentiment": v1.compute_sentiment(conn, run_date, v1_cfg),
    }

    for name, d in dim_results.items():
        if d.status == "missing":
            logger.warning("V1 dimension '%s' missing today: %s", name, d.note)
        dbmod.upsert_metric(conn, run_date, "v1", name, d.raw, d.percentile, d.score, d.status, d.note, now_iso)

    composite_score, state, weights_used = v1.compute_composite(dim_results, v1_cfg["weights"], v1_cfg["state_thresholds"])
    logger.info("V1 composite score = %.1f (%s)", composite_score, state)

    narrative_data = generate_v1_narrative(composite_score, state, dim_results, weights_used, cfg["llm"])
    dbmod.upsert_composite(conn, run_date, "v1", composite_score, state, weights_used,
                            narrative_data.get("narrative"), narrative_data.get("suggestions"), now_iso)

    render_v1_dashboard(composite_score, state, dim_results, weights_used, narrative_data,
                         as_of_date=run_date, generated_at=now_iso, output_dir=cfg["paths"]["site_dir"])
    section = build_v1_report_section(composite_score, state, dim_results, weights_used, narrative_data)
    return section, {"composite_score": composite_score, "state": state, "dim_results": dim_results}


def run_v2(cfg: dict, fetcher: USMarketFetcher, conn, run_date: str, now_iso: str) -> tuple[str | None, dict]:
    v2_cfg = {**cfg["v2"], "sector_etfs": cfg["sector_etfs"]}

    matrix_result = v2.compute_relative_strength_matrix(fetcher, v2_cfg)
    if matrix_result.status == "missing":
        logger.warning("V2 relative-strength matrix missing today: %s", matrix_result.note)
        dbmod.upsert_metric(conn, run_date, "v2", "sector_matrix", {}, None, None, "missing", matrix_result.note, now_iso)
        return None, {}

    v2.attach_persistence(conn, run_date, v2_cfg, matrix_result.raw)
    dbmod.upsert_metric(conn, run_date, "v2", "sector_matrix", matrix_result.raw, None, None, "ok", None, now_iso)

    health_results = v2.compute_internal_health(fetcher, v2_cfg, matrix_result.raw)

    render_v2_dashboard(matrix_result.raw, health_results, as_of_date=run_date, generated_at=now_iso,
                         output_dir=cfg["paths"]["site_dir"])
    section = build_v2_report_section(matrix_result.raw, health_results)
    return section, {"matrix": matrix_result.raw, "health": health_results}


def _attach_options_sentiment(card: dict, options_fetcher, conn, run_date: str, now_iso: str, cfg: dict) -> None:
    """5.4 回填：为评分卡补充期权市场情绪（P/C 比、ATM IV、IV Rank、持仓量变化）。
    快照按日落库，供 IV Rank 自行累积历史和 V4 的 IV/成交量异动预警使用。"""
    from core.options.sentiment import compute_options_snapshot, iv_rank_from_history
    from core.scoring.v3_stock_card import Block

    symbol = card["symbol"]
    chain_res = options_fetcher.get_chain(symbol, max_expiries=2)
    if not chain_res.ok:
        card["options_sentiment"] = Block("missing", note=chain_res.error)
        return

    snapshot = compute_options_snapshot(chain_res.data)
    history = dbmod.get_metric_history(conn, "v5", f"{symbol}::options_snapshot", before_date=run_date, limit=252)
    historical_ivs = [raw["atm_iv"] for _, raw in history if raw.get("atm_iv") is not None]

    min_samples = cfg["v5"]["scenarios"]["iv_history_min_samples"]
    iv_rank = (iv_rank_from_history(snapshot["atm_iv"], historical_ivs, min_samples)
               if snapshot["atm_iv"] is not None else None)
    prev_oi = history[0][1].get("total_oi") if history else None
    oi_change_pct = (round(snapshot["total_oi"] / prev_oi - 1, 4)
                     if prev_oi else None)

    dbmod.upsert_metric(conn, run_date, "v5", f"{symbol}::options_snapshot", snapshot,
                        None, None, "ok", None, now_iso)
    card["options_sentiment"] = Block("ok", raw={**snapshot, "iv_rank": iv_rank, "oi_change_pct": oi_change_pct})


def run_v3(cfg: dict, fetcher: USMarketFetcher, conn, run_date: str, now_iso: str) -> tuple[str | None, dict]:
    watchlist = load_watchlist(cfg["watchlist"])
    if not watchlist:
        return None, {}

    options_fetcher = None
    if cfg["v5"].get("daily_sentiment_enabled"):
        from core.fetchers.options_base import get_options_fetcher
        options_fetcher = get_options_fetcher(cfg["v5"], cfg["fetcher"])

    cards = []
    for entry in watchlist:
        card = build_stock_card(fetcher, entry, cfg["v3"], cfg["fetcher"])

        if card["news"].status == "ok" and card["news"].raw:
            sentiment = classify_news_sentiment(card["symbol"], card["news"].raw, cfg["llm"])
            card["sentiment_items"] = sentiment["items"]
        else:
            card["sentiment_items"] = []

        if options_fetcher is not None:
            _attach_options_sentiment(card, options_fetcher, conn, run_date, now_iso, cfg)
        cards.append(card)

        for field_name in ("financial_momentum", "institutional", "relative_strength", "event_calendar", "news"):
            block = card[field_name]
            if block.status == "missing":
                logger.warning("V3 %s.%s missing today: %s", card["symbol"], field_name, block.note)

        dbmod.upsert_metric(conn, run_date, "v3", card["symbol"], serialize_card(card), None, None, "ok", None, now_iso)

    render_v3_dashboard(cards, as_of_date=run_date, generated_at=now_iso, output_dir=cfg["paths"]["site_dir"])
    section = build_v3_report_section(cards)
    return section, {"cards": cards}


def run_v4(cfg: dict, fetcher: USMarketFetcher, conn, run_date: str, now_iso: str, cards: list[dict]) -> tuple[str | None, dict]:
    if not cards:
        return None, {}

    all_alerts = []
    for card in cards:
        symbol = card["symbol"]
        alerts = v4.generate_alerts_for_symbol(fetcher, symbol, card, conn, run_date, cfg["v4"])
        alerts += v4.generate_options_alerts(conn, run_date, symbol, card.get("options_sentiment"),
                                              cfg["v5"]["alerts"])
        all_alerts.extend(alerts)
        if alerts:
            logger.info("V4 %s: %d alert(s) triggered", symbol, len(alerts))

        # always persist today's news count, even with zero alerts, so future runs have
        # a baseline to compare against (guidebook 5.2's news-spike rule needs this history)
        news_count_today = len(card["news"].raw) if card["news"].status == "ok" else 0
        dbmod.upsert_metric(conn, run_date, "v4", f"{symbol}::news_count", {"count": news_count_today},
                             None, None, "ok", None, now_iso)

    render_v4_dashboard(all_alerts, as_of_date=run_date, generated_at=now_iso, output_dir=cfg["paths"]["site_dir"])
    section = build_v4_report_section(all_alerts)
    return section, {"alerts": all_alerts}


def main():
    parser = argparse.ArgumentParser(description="Run the daily monitoring pipeline (V1 + V2 + V3 + V4).")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--date", default=None, help="Override run date (YYYY-MM-DD), defaults to today.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run_date = args.date or datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()
    fetcher = USMarketFetcher(max_retries=cfg["fetcher"]["max_retries"], backoff_base=cfg["fetcher"]["backoff_base_seconds"])

    sections = []
    with dbmod.connect(cfg["paths"]["db"]) as conn:
        v1_section, _ = run_v1(cfg, fetcher, conn, run_date, now_iso)
        sections.append(v1_section)

        v2_section, _ = run_v2(cfg, fetcher, conn, run_date, now_iso)
        if v2_section:
            sections.append(v2_section)

        v3_section, v3_info = run_v3(cfg, fetcher, conn, run_date, now_iso)
        if v3_section:
            sections.append(v3_section)

        v4_section, _ = run_v4(cfg, fetcher, conn, run_date, now_iso, v3_info.get("cards", []))
        if v4_section:
            sections.append(v4_section)

    report_path = write_daily_report(run_date, sections, cfg["paths"]["reports_dir"])
    logger.info("report written to %s", report_path)


if __name__ == "__main__":
    main()
