"""Render the V1 macro-sentiment dashboard (index.html) and its daily
markdown report section."""
import json

from core.render.common import fmt_pct, get_env, pct_class, write_page

DIM_ORDER = ["volatility", "trend", "breadth", "credit", "rotation", "sentiment"]
DIM_LABELS = {
    "volatility": "波动率", "trend": "趋势", "breadth": "广度",
    "credit": "信用", "rotation": "板块轮动", "sentiment": "情绪",
}
STATE_CLASS = {"防守": "defensive", "谨慎观望": "neutral", "积极": "aggressive"}


def _detail_rows(dimension: str, raw: dict) -> list[dict]:
    rows = []
    if dimension == "volatility":
        rows.append({"label": "VIX", "value": f"{raw['vix']:.2f}"})
        rows.append({"label": "VIX9D", "value": f"{raw['vix9d']:.2f}"})
        if raw.get("vix9d_vix_ratio") is not None:
            rows.append({"label": "VIX9D/VIX", "value": f"{raw['vix9d_vix_ratio']:.3f}"})
    elif dimension == "trend":
        for sym, vals in raw["symbols"].items():
            rows.append({"label": f"{sym} 收盘", "value": f"{vals['close']:.2f}"})
            rows.append({"label": f"{sym} 站上均线比例", "value": f"{vals['above_ratio'] * 100:.0f}%"})
    elif dimension == "breadth":
        rows.append({"label": "今日广度", "value": f"{raw['today']:.1f}%"})
        rows.append({"label": "覆盖样本", "value": f"{raw['constituents_used']}/{raw['constituents_total']} ({raw['universe']})"})
    elif dimension == "credit":
        rows.append({"label": "HYG/IEF", "value": f"{raw['ratio']:.4f}"})
        rows.append({"label": "5日变动", "value": fmt_pct(raw["chg_5d"])})
        rows.append({"label": "20日变动", "value": fmt_pct(raw["chg_20d"])})
    elif dimension == "rotation":
        rows.append({"label": "顺周期板块近1月均值", "value": fmt_pct(raw["cyclical_avg_1m"])})
        rows.append({"label": "防御板块近1月均值", "value": fmt_pct(raw["defensive_avg_1m"])})
        rows.append({"label": "近1月上涨板块占比", "value": f"{raw['pct_positive_1m']:.0f}%"})
    elif dimension == "sentiment":
        fg = raw.get("fear_greed")
        aaii = raw.get("aaii")
        if fg:
            rows.append({"label": "Fear & Greed", "value": f"{fg['score']:.0f} ({fg.get('rating', '-')})"})
        if aaii:
            rows.append({"label": "AAII 看多-看跌差", "value": f"{aaii['spread']:+.1f}"})
    return rows


def _build_dim_context(dim_results: dict) -> list[dict]:
    out = []
    for key in DIM_ORDER:
        d = dim_results.get(key)
        if d is None:
            continue
        entry = {"key": key, "label": DIM_LABELS[key], "status": d.status, "note": d.note}
        if d.status == "ok":
            entry["score_str"] = f"{d.score:+.1f}"
            entry["score_class"] = "positive" if d.score > 0 else ("negative" if d.score < 0 else "zero")
            entry["percentile_str"] = f"{d.percentile:.0f}%" if d.percentile is not None else None
            entry["detail_rows"] = _detail_rows(key, d.raw)
        out.append(entry)
    return out


def _build_sector_table(rotation_result):
    if rotation_result is None or rotation_result.status != "ok":
        return [], json.dumps({"labels": [], "chg_1m": []})
    sectors = rotation_result.raw["sectors"]
    table = []
    for s in sectors:
        table.append({
            "name": s["name"], "symbol": s["symbol"],
            "style_label": "顺周期" if s["style"] == "cyclical" else "防御性",
            "chg_1w_str": fmt_pct(s["chg_1w"]), "chg_1w_class": pct_class(s["chg_1w"]),
            "chg_1m_str": fmt_pct(s["chg_1m"]), "chg_1m_class": pct_class(s["chg_1m"]),
        })
    chart_json = json.dumps({
        "labels": [s["name"] for s in sectors],
        "chg_1m": [round(s["chg_1m"] * 100, 2) for s in sectors],
    }, ensure_ascii=False)
    return table, chart_json


def render_v1_dashboard(composite_score, state, dim_results, weights_used, narrative_data,
                         as_of_date, generated_at, output_dir: str, is_sample_data: bool = False):
    template = get_env().get_template("index.html")
    sector_table, sector_chart_json = _build_sector_table(dim_results.get("rotation"))

    context = {
        "as_of_date": as_of_date,
        "generated_at": generated_at,
        "active_module": "v1",
        "composite_score": f"{composite_score:.1f}",
        "state": state,
        "state_class": STATE_CLASS.get(state, "neutral"),
        "gauge_position_pct": max(0, min(100, composite_score)),
        "narrative": narrative_data.get("narrative", ""),
        "suggestions": narrative_data.get("suggestions", []),
        "dimensions": _build_dim_context(dim_results),
        "sector_table": sector_table,
        "sector_chart_json": sector_chart_json,
        "is_sample_data": is_sample_data,
    }
    write_page(output_dir, "index.html", template.render(**context))


def build_v1_report_section(composite_score, state, dim_results, weights_used, narrative_data) -> str:
    lines = [
        "## V1 · 宏观情绪",
        "",
        f"**综合评分: {composite_score:.1f} / 100　状态: {state}**",
        "",
        narrative_data.get("narrative", ""),
        "",
    ]
    if narrative_data.get("suggestions"):
        lines.append("### 建议")
        for s in narrative_data["suggestions"]:
            lines.append(f"- {s}")
        lines.append("")

    lines.append("### 六维度明细")
    for key in DIM_ORDER:
        d = dim_results.get(key)
        if d is None:
            continue
        lines.append(f"#### {DIM_LABELS[key]}")
        if d.status == "ok":
            w = weights_used.get(key)
            w_str = f"{w:.3f}" if w is not None else "-"
            lines.append(f"- 维度分: {d.score:+.1f}　百分位: {d.percentile if d.percentile is not None else '-'}　当日权重: {w_str}")
            for row in _detail_rows(key, d.raw):
                lines.append(f"  - {row['label']}: {row['value']}")
        else:
            lines.append(f"- 数据缺失: {d.note}")
        lines.append("")

    return "\n".join(lines)
