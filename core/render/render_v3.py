"""Render the V3 stock-scorecard dashboard (stocks.html) and its daily
markdown report section."""
from core.render.common import fmt_pct, get_env, pct_class, write_page

LABEL_CLASS = {"利多": "pct-positive", "利空": "pct-negative", "中性": "pct-neutral", "未分类": "pct-neutral"}


def _fmt_signed(v, digits=2):
    return f"{v:+.{digits}f}" if v is not None else "-"


def _build_card(card: dict) -> dict:
    symbol = card["symbol"]
    out = {"symbol": symbol, "current_price": f"{card['current_price']:.2f}" if card["current_price"] else "-"}

    m = card["financial_momentum"]
    out["momentum_status"] = m.status
    if m.status == "ok":
        out["momentum"] = {
            "revenue_qoq": fmt_pct(m.raw["revenue_qoq"]),
            "revenue_yoy": fmt_pct(m.raw["revenue_yoy"]) if m.raw.get("revenue_yoy") is not None else "-",
            "eps_beat": m.raw.get("eps_beat_latest"),
            "eps_surprise": f"{m.raw['eps_surprise_pct']:+.1f}%" if m.raw.get("eps_surprise_pct") is not None else "-",
        }
    else:
        out["momentum_note"] = m.note

    inst = card["institutional"]
    out["institutional_status"] = inst.status
    if inst.status == "ok":
        out["institutional"] = {
            "median_target": f"{inst.raw['median_target']:.2f}",
            "upside": fmt_pct(inst.raw["upside_pct"]) if inst.raw.get("upside_pct") is not None else "-",
            "upside_class": pct_class(inst.raw["upside_pct"]) if inst.raw.get("upside_pct") is not None else "zero",
            "rating_changes": inst.raw.get("rating_changes_recent", []),
        }
    else:
        out["institutional_note"] = inst.note

    rs = card["relative_strength"]
    out["relative_strength_status"] = rs.status
    if rs.status == "ok":
        out["relative_strength"] = {
            "sector_etf": rs.raw["sector_etf"],
            "excess_mid": fmt_pct(rs.raw["excess_mid"]), "excess_mid_class": pct_class(rs.raw["excess_mid"]),
            "excess_long": fmt_pct(rs.raw["excess_long"]), "excess_long_class": pct_class(rs.raw["excess_long"]),
        }
    else:
        out["relative_strength_note"] = rs.note

    cal = card["event_calendar"]
    out["calendar_status"] = cal.status
    if cal.status == "ok":
        out["calendar"] = cal.raw
    else:
        out["calendar_note"] = cal.note

    news = card["news"]
    out["news_status"] = news.status
    out["news_note"] = news.note if news.status == "missing" else None

    sentiment_items = card.get("sentiment_items", [])
    out["sentiment_items"] = [
        {**item, "label_class": LABEL_CLASS.get(item["label"], "zero")} for item in sentiment_items
    ]

    return out


def render_v3_dashboard(cards: list[dict], as_of_date: str, generated_at: str, output_dir: str,
                         is_sample_data: bool = False):
    template = get_env().get_template("stocks.html")
    context = {
        "as_of_date": as_of_date,
        "generated_at": generated_at,
        "active_module": "v3",
        "cards": [_build_card(c) for c in cards],
        "is_sample_data": is_sample_data,
    }
    write_page(output_dir, "stocks.html", template.render(**context))


def build_v3_report_section(cards: list[dict]) -> str:
    lines = ["## V3 · 个股评分卡", ""]
    for card in cards:
        symbol = card["symbol"]
        header = f"### {symbol}（现价 {card['current_price']:.2f}）" if card["current_price"] else f"### {symbol}"
        lines.append(header)

        m = card["financial_momentum"]
        if m.status == "ok":
            lines.append(f"- 财务动量: 营收环比 {fmt_pct(m.raw['revenue_qoq'])}，"
                          f"同比 {fmt_pct(m.raw['revenue_yoy']) if m.raw.get('revenue_yoy') is not None else '-'}，"
                          f"最近一期{'超预期' if m.raw.get('eps_beat_latest') else '未超预期' if m.raw.get('eps_beat_latest') is not None else '未知'}")
        else:
            lines.append(f"- 财务动量: 数据缺失（{m.note}）")

        inst = card["institutional"]
        if inst.status == "ok":
            lines.append(f"- 机构态度: 目标价中位数 {inst.raw['median_target']:.2f}，"
                          f"较现价{'上行' if (inst.raw.get('upside_pct') or 0) >= 0 else '下行'} "
                          f"{fmt_pct(abs(inst.raw['upside_pct'])) if inst.raw.get('upside_pct') is not None else '-'}，"
                          f"近30日评级变动 {len(inst.raw.get('rating_changes_recent', []))} 条")
        else:
            lines.append(f"- 机构态度: 数据缺失（{inst.note}）")

        rs = card["relative_strength"]
        if rs.status == "ok":
            lines.append(f"- 相对强度(vs {rs.raw['sector_etf']}): 近1月超额 {fmt_pct(rs.raw['excess_mid'])}，"
                          f"近3月超额 {fmt_pct(rs.raw['excess_long'])}")
        else:
            lines.append(f"- 相对强度: 数据缺失（{rs.note}）")

        cal = card["event_calendar"]
        if cal.status == "ok":
            warn = "（临近财报，注意仓位）" if cal.raw["earnings_soon"] else ""
            lines.append(f"- 事件日历: 下次财报 {cal.raw['next_earnings_date']}，距今 {cal.raw['days_until']} 个交易日{warn}")
        else:
            lines.append(f"- 事件日历: 数据缺失（{cal.note}）")

        sentiment_items = card.get("sentiment_items", [])
        if sentiment_items:
            lines.append("- 舆情摘要（近7日新闻，附来源溯源）:")
            for item in sentiment_items:
                lines.append(f"  - [{item['label']}] {item['title']}（{item.get('publisher') or '未知来源'}）：{item['reason']}")
        lines.append("")

    return "\n".join(lines)
