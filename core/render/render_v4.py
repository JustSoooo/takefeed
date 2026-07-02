"""Render the V4 watchlist-alerts dashboard (alerts.html) and its daily
markdown report section."""
from core.render.common import get_env, write_page

SEVERITY_NOTE_CLASS = {"warning": "error-note", "info": "info-note"}
SEVERITY_LABEL = {"warning": "预警", "info": "提示"}


def _group_by_symbol(alerts: list) -> list[dict]:
    by_symbol: dict[str, list] = {}
    for a in alerts:
        by_symbol.setdefault(a.symbol, []).append(a)
    return [
        {"symbol": symbol, "alerts": [
            {"rule": a.rule, "severity": a.severity, "message": a.message,
             "note_class": SEVERITY_NOTE_CLASS.get(a.severity, "info-note"),
             "severity_label": SEVERITY_LABEL.get(a.severity, a.severity)}
            for a in symbol_alerts
        ]}
        for symbol, symbol_alerts in by_symbol.items()
    ]


def render_v4_dashboard(alerts: list, as_of_date: str, generated_at: str, output_dir: str,
                         is_sample_data: bool = False):
    template = get_env().get_template("alerts.html")
    context = {
        "as_of_date": as_of_date,
        "generated_at": generated_at,
        "active_module": "v4",
        "groups": _group_by_symbol(alerts),
        "total_count": len(alerts),
        "is_sample_data": is_sample_data,
    }
    write_page(output_dir, "alerts.html", template.render(**context))


def build_v4_report_section(alerts: list) -> str:
    lines = ["## V4 · 股池监控告警", ""]
    if not alerts:
        lines.append("今日 watchlist 内无触发规则的异动。")
        lines.append("")
        return "\n".join(lines)

    groups = _group_by_symbol(alerts)
    for group in groups:
        lines.append(f"### {group['symbol']}")
        for a in group["alerts"]:
            lines.append(f"- [{a['severity_label']}] {a['message']}")
        lines.append("")
    return "\n".join(lines)
