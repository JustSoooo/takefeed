"""Compose the single daily markdown report from whichever module sections
ran that day. Modules are independently deliverable (guidebook 1), so this
just concatenates whatever sections it is given."""
from pathlib import Path


def write_daily_report(as_of_date: str, sections: list[str], output_dir: str) -> Path:
    lines = [f"# 决策支持日报 · {as_of_date}", ""]
    lines.extend(sections)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"daily_{as_of_date.replace('-', '')}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path
