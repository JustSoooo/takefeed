#!/usr/bin/env python3
"""V5 期权分析入口（按需运行，非每日巡检）。

用法示例：
  # 只看链 + 推荐矩阵（默认按现价方向 view=bull）
  python run_options.py --symbol NVDA

  # 完整情景分析：预期 2026-08-21 到达 210，默认持仓为一张 ATM 单腿
  python run_options.py --symbol NVDA --expected-price 210 --expected-date 2026-08-21

  # 自定义 1-4 腿持仓（JSON：kind/strike/expiry/direction/qty，价格与IV自动取链上中间价）
  python run_options.py --symbol NVDA --expected-price 210 --expected-date 2026-08-21 \\
      --position '[{"kind":"call","strike":200,"expiry":"2026-09-18","direction":1,"qty":1}]'
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from run_daily import load_config
from core import db as dbmod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_options")


def main():
    parser = argparse.ArgumentParser(description="V5 期权链浏览 / 情景收益预估 / 腿策略推荐")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--expected-price", type=float, default=None)
    parser.add_argument("--expected-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--view", choices=["bull", "bear"], default=None,
                        help="不传时按 expected-price 相对现价推断，无预期价则默认 bull")
    parser.add_argument("--position", default=None, help="自定义持仓 JSON（1-4 腿）")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    from core.fetchers.options_base import get_options_fetcher
    from core.fetchers.us_stock import fetch_event_calendar
    from core.options.recommender import nearest_liquid, recommend_matrix
    from core.options.scenarios import build_scenario_report, _parse_date
    from core.options.sentiment import (compute_options_snapshot, compute_term_structure,
                                        iv_median_from_history, iv_rank_from_history)
    from core.render.render_v5 import render_options_page

    cfg = load_config(args.config)
    v5_cfg = cfg["v5"]
    fetch_cfg = cfg["fetcher"]
    run_date = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()

    fetcher = get_options_fetcher(v5_cfg, fetch_cfg)

    chain_res = fetcher.get_chain(args.symbol, max_expiries=v5_cfg["max_expiries"])
    if not chain_res.ok:
        logger.error("期权链获取失败：%s", chain_res.error)
        sys.exit(1)
    chain = chain_res.data

    rate_res = fetcher.get_risk_free_rate()
    if rate_res.ok:
        rate, rate_note = rate_res.data, None
    else:
        rate = v5_cfg["default_risk_free"]
        rate_note = f"无风险利率取数失败（{rate_res.error}），使用配置默认值 {rate:.2%}"
        logger.warning(rate_note)

    term_structure = compute_term_structure(chain)
    snapshot = compute_options_snapshot(chain)

    # 快照落库 + 从自行累积的历史里取 IV rank / IV 中位数（样本不足则显式缺失）
    min_samples = v5_cfg["scenarios"]["iv_history_min_samples"]
    with dbmod.connect(cfg["paths"]["db"]) as conn:
        history = dbmod.get_metric_history(conn, "v5", f"{args.symbol}::options_snapshot",
                                           before_date=run_date, limit=252)
        historical_ivs = [raw["atm_iv"] for _, raw in history if raw.get("atm_iv") is not None]
        dbmod.upsert_metric(conn, run_date, "v5", f"{args.symbol}::options_snapshot",
                            snapshot, None, None, "ok", None, now_iso)

    iv_rank = iv_rank_from_history(snapshot["atm_iv"], historical_ivs, min_samples) if snapshot["atm_iv"] else None
    iv_median = iv_median_from_history(historical_ivs, min_samples)

    view = args.view or ("bull" if args.expected_price is None or args.expected_price >= chain.spot else "bear")

    calendar_res = fetch_event_calendar(args.symbol, cfg["v3"]["earnings_soon_threshold_days"],
                                        max_retries=fetch_cfg["max_retries"],
                                        backoff_base=fetch_cfg["backoff_base_seconds"])
    next_earnings = calendar_res.data["next_earnings_date"] if calendar_res.ok else None
    if not calendar_res.ok:
        logger.warning("财报日期获取失败（跨财报检测降级跳过）：%s", calendar_res.error)

    scenario_report, scenario_inputs = None, None
    if args.expected_price and args.expected_date:
        position = _build_position(args, chain, v5_cfg)
        if position:
            scenario_report = build_scenario_report(
                position, args.expected_price, _parse_date(args.expected_date), rate,
                iv_median, v5_cfg["scenarios"], next_earnings)
            scenario_inputs = {
                "expected_price": args.expected_price, "expected_date": args.expected_date,
                "position_desc": "；".join(leg["desc"] for leg in position),
            }
    elif args.expected_price or args.expected_date:
        logger.warning("--expected-price 与 --expected-date 需同时提供，已跳过情景分析")

    matrix = recommend_matrix(chain, view, rate, v5_cfg["liquidity"], v5_cfg["buckets"])

    render_options_page(chain, v5_cfg["liquidity"], rate, rate_note, term_structure, iv_rank,
                        scenario_report, scenario_inputs, matrix, view,
                        as_of_date=run_date, generated_at=now_iso,
                        output_dir=cfg["paths"]["site_dir"])
    logger.info("options.html 已生成于 %s", cfg["paths"]["site_dir"])


def _build_position(args, chain, v5_cfg) -> list[dict] | None:
    from core.options.recommender import nearest_liquid, _leg

    if args.position:
        specs = json.loads(args.position)
        if not 1 <= len(specs) <= 4:
            logger.error("自定义持仓需 1-4 腿，收到 %d 腿", len(specs))
            return None
        position = []
        for spec in specs:
            q = nearest_liquid(chain, spec["expiry"], spec["kind"], spec["strike"], v5_cfg["liquidity"])
            if q is None:
                logger.error("找不到通过流动性过滤的合约：%s %s %s（换个行权价/到期日重试）",
                             spec["expiry"], spec["strike"], spec["kind"])
                return None
            position.append(_leg(q, spec.get("direction", 1), spec.get("qty", 1)))
        return position

    # 默认持仓：中期桶最近到期日的 ATM 单腿（bull=call / bear=put）
    from core.options.recommender import pick_bucket_expiry
    view = args.view or ("bull" if args.expected_price >= chain.spot else "bear")
    expiry = pick_bucket_expiry(chain, v5_cfg["buckets"]["mid"], "mid") or (chain.expiries[-1] if chain.expiries else None)
    if expiry is None:
        return None
    q = nearest_liquid(chain, expiry, "call" if view == "bull" else "put", chain.spot, v5_cfg["liquidity"])
    if q is None:
        logger.error("链上没有通过流动性过滤的 ATM 合约，无法构建默认持仓")
        return None
    logger.info("未指定 --position，使用默认持仓：%s", q.contract_symbol)
    return [_leg(q, 1)]


if __name__ == "__main__":
    main()
