"""Watchlist loading: config.yaml list (default) or a manually-maintained CSV
(guidebook 4.1 / M4: "watchlist 管理 CSV 导入"). Either source, the loader
normalizes to a list of {"ticker": str, "sector_etf": str | None}."""
import csv
from pathlib import Path


def load_watchlist(cfg: dict) -> list[dict]:
    source = cfg.get("source", "config")
    if source == "csv":
        return _load_from_csv(cfg["csv_path"])
    if source == "config":
        return [dict(entry) for entry in cfg["tickers"]]
    raise ValueError(f"unknown watchlist source: {source}")


def _load_from_csv(path: str) -> list[dict]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"watchlist CSV not found: {path}")
    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [
            {"ticker": row["ticker"].strip().upper(), "sector_etf": (row.get("sector_etf") or "").strip().upper() or None}
            for row in reader if row.get("ticker", "").strip()
        ]
