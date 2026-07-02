"""SQLite persistence layer. All daily indicator values land here, unconditionally,
so the scoring framework can be validated against real outcomes later (guidebook 0."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date        TEXT NOT NULL,
    module      TEXT NOT NULL,           -- v1 | v2 | v3 | v4
    dimension   TEXT NOT NULL,           -- volatility | trend | breadth | credit | rotation | sentiment ...
    raw_json    TEXT NOT NULL,           -- raw indicator values, as fetched
    percentile  REAL,                    -- historical percentile, NULL if not applicable
    score       REAL,                    -- dimension score, NULL if status != ok
    status      TEXT NOT NULL,           -- ok | missing
    status_note TEXT,                    -- human-readable reason when status = missing
    created_at  TEXT NOT NULL,
    PRIMARY KEY (date, module, dimension)
);

CREATE TABLE IF NOT EXISTS daily_composite (
    date            TEXT NOT NULL,
    module          TEXT NOT NULL,       -- v1 | v2 ...
    composite_score REAL NOT NULL,
    state           TEXT NOT NULL,
    weights_json    TEXT NOT NULL,
    narrative       TEXT,
    suggestions_json TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (date, module)
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(db_path: str):
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_metric(conn, date, module, dimension, raw, percentile, score, status, status_note, created_at):
    conn.execute(
        """INSERT INTO daily_metrics
           (date, module, dimension, raw_json, percentile, score, status, status_note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, module, dimension) DO UPDATE SET
             raw_json=excluded.raw_json, percentile=excluded.percentile, score=excluded.score,
             status=excluded.status, status_note=excluded.status_note, created_at=excluded.created_at""",
        (date, module, dimension, json.dumps(raw, ensure_ascii=False), percentile, score, status, status_note, created_at),
    )


def get_metric_history(conn, module, dimension, before_date=None, limit=400):
    """Return past raw_json values for a dimension, most recent first.
    Used to bootstrap percentiles for indicators with no long external history
    (e.g. breadth, sentiment) from our own accumulated daily runs."""
    query = "SELECT date, raw_json FROM daily_metrics WHERE module=? AND dimension=? AND status='ok'"
    params = [module, dimension]
    if before_date:
        query += " AND date < ?"
        params.append(before_date)
    query += " ORDER BY date DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [(r["date"], json.loads(r["raw_json"])) for r in rows]


def upsert_composite(conn, date, module, composite_score, state, weights, narrative, suggestions, created_at):
    conn.execute(
        """INSERT INTO daily_composite
           (date, module, composite_score, state, weights_json, narrative, suggestions_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, module) DO UPDATE SET
             composite_score=excluded.composite_score, state=excluded.state,
             weights_json=excluded.weights_json, narrative=excluded.narrative,
             suggestions_json=excluded.suggestions_json, created_at=excluded.created_at""",
        (date, module, composite_score, state, json.dumps(weights, ensure_ascii=False),
         narrative, json.dumps(suggestions, ensure_ascii=False), created_at),
    )


def get_composite_history(conn, module, limit=400):
    rows = conn.execute(
        "SELECT * FROM daily_composite WHERE module=? ORDER BY date DESC LIMIT ?",
        (module, limit),
    ).fetchall()
    return [dict(r) for r in rows]
