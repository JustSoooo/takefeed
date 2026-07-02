"""Shared rendering helpers: Jinja environment, design-token asset path, and
formatting utilities reused across V1/V2/... dashboard renderers."""
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"


def get_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=True)


def write_page(out_dir: str, filename: str, html: str) -> None:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / filename).write_text(html, encoding="utf-8")
    shutil.copy(STATIC_DIR / "tokens.css", out_path / "tokens.css")


def fmt_pct(v: float) -> str:
    return f"{v * 100:+.2f}%"


def pct_class(v: float) -> str:
    return "pct-positive" if v >= 0 else "pct-negative"
