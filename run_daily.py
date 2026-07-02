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
from core.narrative.generate import generate_v1_narrative
from core.render.render_v1 import build_v1_report_section, render_v1_dashboard
from core.render.render_v2 import build_v2_report_section, render_v2_dashboard
from core.render.report import write_daily_report
from core.scoring import v1_composite as v1
from core.scoring import v2_rotation as v2

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

    narrative_data = generate_v1_narrative(composite_score, state, dim_results, weights_used, cfg["anthropic"])
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


def main():
    parser = argparse.ArgumentParser(description="Run the daily monitoring pipeline (V1 + V2).")
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

    report_path = write_daily_report(run_date, sections, cfg["paths"]["reports_dir"])
    logger.info("report written to %s", report_path)


if __name__ == "__main__":
    main()
