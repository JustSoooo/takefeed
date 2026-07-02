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
from core.render.render_site import render_v1_dashboard, write_markdown_report
from core.scoring import v1_composite as v1

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_daily")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_v1(cfg: dict, run_date: str) -> dict:
    fetch_cfg = cfg["fetcher"]
    v1_cfg = cfg["v1"]
    fetcher = USMarketFetcher(max_retries=fetch_cfg["max_retries"], backoff_base=fetch_cfg["backoff_base_seconds"])
    v1_cfg = {**v1_cfg, "history_period": fetch_cfg["history_period"]}

    now_iso = datetime.now(timezone.utc).isoformat()

    with dbmod.connect(cfg["paths"]["db"]) as conn:
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
                logger.warning("dimension '%s' missing today: %s", name, d.note)
            dbmod.upsert_metric(
                conn, run_date, "v1", name, d.raw, d.percentile, d.score, d.status, d.note, now_iso,
            )

        composite_score, state, weights_used = v1.compute_composite(
            dim_results, v1_cfg["weights"], v1_cfg["state_thresholds"],
        )
        logger.info("composite score = %.1f (%s)", composite_score, state)

        narrative_data = generate_v1_narrative(composite_score, state, dim_results, weights_used, cfg["anthropic"])

        dbmod.upsert_composite(
            conn, run_date, "v1", composite_score, state, weights_used,
            narrative_data.get("narrative"), narrative_data.get("suggestions"), now_iso,
        )

    render_v1_dashboard(
        composite_score, state, dim_results, weights_used, narrative_data,
        as_of_date=run_date, generated_at=now_iso, output_dir=cfg["paths"]["site_dir"],
    )
    report_path = write_markdown_report(
        composite_score, state, dim_results, weights_used, narrative_data,
        as_of_date=run_date, output_dir=cfg["paths"]["reports_dir"],
    )
    logger.info("report written to %s", report_path)

    return {"composite_score": composite_score, "state": state, "dim_results": dim_results}


def main():
    parser = argparse.ArgumentParser(description="Run the daily V1 macro-sentiment pipeline.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--date", default=None, help="Override run date (YYYY-MM-DD), defaults to today.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    run_date = args.date or datetime.now().strftime("%Y-%m-%d")
    run_v1(cfg, run_date)


if __name__ == "__main__":
    main()
