"""Render the V2 sector-rotation dashboard (rotation.html) and its daily
markdown report section."""
import json

from core.render.common import fmt_pct, get_env, pct_class, write_page

WINDOW_LABELS = {"short": "近1周", "mid": "近1月", "long": "近3月"}


def _build_sector_rows(matrix_raw: dict) -> list[dict]:
    rows = []
    for s in matrix_raw["sectors"]:
        row = {
            "symbol": s["symbol"], "name": s["name"],
            "style_label": "顺周期" if s["style"] == "cyclical" else "防御性",
            "rank_mid": s["rank_mid"],
        }
        for w in ("short", "mid", "long"):
            row[f"excess_{w}_str"] = fmt_pct(s[f"excess_{w}"])
            row[f"excess_{w}_class"] = pct_class(s[f"excess_{w}"])
            row[f"rank_{w}"] = s[f"rank_{w}"]
        row["persistent_strong"] = s.get("persistent_strong", False)
        if s.get("persistence_hit_ratio") is not None:
            row["persistence_str"] = f"{s['persistence_hit_ratio'] * 100:.0f}% ({s.get('persistence_observed_days', 0)}个交易日样本)"
        else:
            row["persistence_str"] = s.get("persistence_note") or "-"
        rows.append(row)
    return rows


def _build_health_rows(health_results: dict, matrix_raw: dict) -> list[dict]:
    name_map = {s["symbol"]: s["name"] for s in matrix_raw["sectors"]}
    rows = []
    for symbol, res in health_results.items():
        row = {"symbol": symbol, "name": name_map.get(symbol, symbol), "status": res["status"]}
        if res["status"] == "ok":
            raw = res["raw"]
            row["pct_holdings_up_str"] = f"{raw['pct_holdings_up_1m']:.0f}%"
            row["top3_weight_str"] = f"{raw['top3_weight_pct']:.1f}%"
            row["concentrated"] = raw["concentrated"]
            row["holdings_counted"] = raw["holdings_counted"]
        else:
            row["note"] = res.get("note")
        rows.append(row)
    return rows


def render_v2_dashboard(matrix_raw: dict, health_results: dict, as_of_date: str, generated_at: str,
                         output_dir: str, is_sample_data: bool = False):
    template = get_env().get_template("rotation.html")
    sector_rows = _build_sector_rows(matrix_raw)
    health_rows = _build_health_rows(health_results, matrix_raw)

    chart_json = json.dumps({
        "labels": [s["name"] for s in matrix_raw["sectors"]],
        "excess_mid": [round(s["excess_mid"] * 100, 2) for s in matrix_raw["sectors"]],
    }, ensure_ascii=False)

    context = {
        "as_of_date": as_of_date,
        "generated_at": generated_at,
        "active_module": "v2",
        "benchmark": matrix_raw["benchmark"],
        "benchmark_returns": {w: fmt_pct(v) for w, v in matrix_raw["benchmark_returns"].items()},
        "sector_rows": sector_rows,
        "health_rows": health_rows,
        "chart_json": chart_json,
        "is_sample_data": is_sample_data,
    }
    write_page(output_dir, "rotation.html", template.render(**context))


def build_v2_report_section(matrix_raw: dict, health_results: dict) -> str:
    lines = ["## V2 · 板块轮动", "", f"基准: {matrix_raw['benchmark']}", ""]
    lines.append("### 相对强弱矩阵（按近1月排名）")
    for s in sorted(matrix_raw["sectors"], key=lambda r: r["rank_mid"]):
        persistent = "　【持续强势】" if s.get("persistent_strong") else ""
        lines.append(
            f"- {s['name']}({s['symbol']}, {s['style']})："
            f"近1周超额 {fmt_pct(s['excess_short'])}(#{s['rank_short']})　"
            f"近1月超额 {fmt_pct(s['excess_mid'])}(#{s['rank_mid']})　"
            f"近3月超额 {fmt_pct(s['excess_long'])}(#{s['rank_long']}){persistent}"
        )
    lines.append("")

    if health_results:
        lines.append("### 强势板块内部健康度")
        for symbol, res in health_results.items():
            if res["status"] == "ok":
                raw = res["raw"]
                warn = "（集中度偏高，普涨判断需谨慎）" if raw["concentrated"] else ""
                lines.append(
                    f"- {symbol}: 前{raw['holdings_counted']}大持仓中 {raw['pct_holdings_up_1m']:.0f}% 近1月上涨，"
                    f"前3大持仓权重占比 {raw['top3_weight_pct']:.1f}%{warn}"
                )
            else:
                lines.append(f"- {symbol}: 内部健康度数据缺失（{res.get('note')}）")
        lines.append("")

    return "\n".join(lines)
