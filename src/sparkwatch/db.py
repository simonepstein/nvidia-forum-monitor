from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, Settings, get_settings
from .util import utcnow_iso

MIGRATION_1 = """
CREATE TABLE forums (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL,
  name TEXT NOT NULL,
  last_synced_at TEXT,
  backfill_floor TEXT
);

CREATE TABLE topics (
  id INTEGER PRIMARY KEY,
  forum_id INTEGER NOT NULL REFERENCES forums(id),
  slug TEXT,
  title TEXT NOT NULL,
  tags TEXT,
  excerpt TEXT,
  created_at TEXT,
  bumped_at TEXT,
  last_posted_at TEXT,
  posts_count INTEGER,
  highest_post_number INTEGER,
  views INTEGER,
  like_count INTEGER,
  pinned INTEGER DEFAULT 0,
  has_accepted_answer INTEGER DEFAULT 0,
  relevance_score REAL DEFAULT 0,
  relevance_override INTEGER,
  first_fetched_at TEXT,
  fetched_at TEXT
);

CREATE TABLE posts (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL REFERENCES topics(id),
  post_number INTEGER NOT NULL,
  post_type INTEGER NOT NULL,
  username TEXT,
  trust_level INTEGER,
  staff INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT,
  version INTEGER,
  reply_to_post_number INTEGER,
  like_count INTEGER DEFAULT 0,
  accepted_answer INTEGER DEFAULT 0,
  cooked TEXT,
  raw TEXT,
  content_hash TEXT NOT NULL,
  first_fetched_at TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  fetched_at TEXT,
  UNIQUE(topic_id, post_number)
);

CREATE INDEX posts_changed ON posts(changed_at);
CREATE INDEX posts_topic ON posts(topic_id, post_number);

CREATE TABLE missing_posts (
  topic_id INTEGER NOT NULL REFERENCES topics(id),
  post_id INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK (reason IN ('absent_from_batch','deleted','hidden','forbidden')),
  first_seen_at TEXT NOT NULL,
  last_checked_at TEXT NOT NULL,
  PRIMARY KEY (topic_id, post_id)
);

CREATE VIRTUAL TABLE posts_fts USING fts5(title, body, tokenize='porter unicode61');

CREATE TRIGGER posts_fts_insert AFTER INSERT ON posts BEGIN
  INSERT INTO posts_fts(rowid, title, body)
  VALUES (new.id, COALESCE((SELECT title FROM topics WHERE id = new.topic_id), ''), COALESCE(new.raw, ''));
END;

CREATE TRIGGER posts_fts_delete AFTER DELETE ON posts BEGIN
  DELETE FROM posts_fts WHERE rowid = old.id;
END;

CREATE TRIGGER posts_fts_update AFTER UPDATE ON posts BEGIN
  DELETE FROM posts_fts WHERE rowid = old.id;
  INSERT INTO posts_fts(rowid, title, body)
  VALUES (new.id, COALESCE((SELECT title FROM topics WHERE id = new.topic_id), ''), COALESCE(new.raw, ''));
END;

CREATE TRIGGER topics_fts_title AFTER UPDATE OF title ON topics BEGIN
  UPDATE posts_fts SET title = new.title WHERE rowid IN (SELECT id FROM posts WHERE topic_id = new.id);
END;

CREATE TABLE sync_runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT,
  finished_at TEXT,
  "trigger" TEXT,
  requests INTEGER,
  new_topic_ids TEXT,
  updated_topic_ids TEXT,
  posts_new INTEGER,
  posts_changed INTEGER,
  partial INTEGER,
  errors TEXT
);

CREATE TABLE watchlist (
  id INTEGER PRIMARY KEY,
  label TEXT NOT NULL UNIQUE,
  canonical TEXT,
  match_regex TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('tracking','anticipated')),
  first_seen_post_id INTEGER,
  first_seen_at TEXT,
  sighting_acked_at TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE watch_hits (
  watch_id INTEGER REFERENCES watchlist(id),
  post_id INTEGER REFERENCES posts(id),
  topic_id INTEGER,
  matched_text TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (watch_id, post_id)
);

CREATE TABLE digest_cursors (
  name TEXT PRIMARY KEY,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE repos (
  id INTEGER PRIMARY KEY,
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  description TEXT,
  default_branch TEXT,
  archived INTEGER,
  pushed_at TEXT,
  origin TEXT NOT NULL DEFAULT 'forum' CHECK (origin IN ('forum','agent')),
  watch INTEGER NOT NULL DEFAULT 0,
  branch TEXT,
  head_sha TEXT,
  head_seen_at TEXT,
  latest_release_tag TEXT,
  latest_tag TEXT,
  note TEXT,
  first_seen_at TEXT NOT NULL,
  watched_at TEXT,
  last_checked_at TEXT,
  etags TEXT
);

CREATE TABLE repo_mentions (
  repo_id INTEGER REFERENCES repos(id),
  post_id INTEGER REFERENCES posts(id),
  topic_id INTEGER,
  url TEXT,
  context_snippet TEXT,
  mentioned_at TEXT,
  PRIMARY KEY (repo_id, post_id)
);

CREATE TABLE repo_installs (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id),
  label TEXT NOT NULL DEFAULT 'default',
  sha TEXT NOT NULL,
  ref_given TEXT,
  branch TEXT,
  installed_at TEXT NOT NULL,
  note TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE (repo_id, label)
);

CREATE TABLE repo_events (
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL REFERENCES repos(id),
  kind TEXT NOT NULL CHECK (kind IN ('head_moved','release','tag','archived','branch_gone')),
  ref TEXT,
  prev_ref TEXT,
  commits_ahead INTEGER,
  title TEXT,
  body_excerpt TEXT,
  url TEXT,
  occurred_at TEXT,
  detected_at TEXT NOT NULL,
  UNIQUE (repo_id, kind, ref)
);

CREATE TABLE compare_cache (
  repo_id INTEGER NOT NULL,
  base_sha TEXT NOT NULL,
  head_sha TEXT NOT NULL,
  response TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, base_sha, head_sha)
);

CREATE TABLE hf_models (
  id INTEGER PRIMARY KEY,
  repo_id TEXT NOT NULL UNIQUE,
  first_seen_at TEXT NOT NULL
);

CREATE TABLE hf_mentions (
  hf_model_id INTEGER REFERENCES hf_models(id),
  post_id INTEGER REFERENCES posts(id),
  topic_id INTEGER,
  url TEXT,
  mentioned_at TEXT,
  PRIMARY KEY (hf_model_id, post_id)
);

CREATE TABLE models (
  canonical TEXT PRIMARY KEY,
  family TEXT,
  params_total_b REAL,
  params_active_b REAL,
  released_at TEXT,
  hf_repo_id TEXT,
  note TEXT,
  updated_at TEXT
);

CREATE TABLE model_aliases (
  alias TEXT PRIMARY KEY,
  canonical TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('seed','agent','triage')),
  reviewed INTEGER DEFAULT 0
);

CREATE TABLE recipes (
  id INTEGER PRIMARY KEY,
  model_canonical TEXT NOT NULL,
  model_raw TEXT,
  quant_format TEXT,
  quant_raw TEXT,
  quant_repo TEXT,
  engine TEXT,
  engine_version TEXT,
  container_image TEXT,
  topology TEXT NOT NULL CHECK (topology IN ('single-spark','dual-spark','other','unknown')),
  tensor_parallel INTEGER,
  pipeline_parallel INTEGER,
  context_len INTEGER,
  tok_s_decode REAL,
  tok_s_prefill REAL,
  concurrency INTEGER,
  memory_gb REAL,
  status TEXT NOT NULL CHECK (status IN ('works','partial','fails','unknown')),
  setup TEXT,
  gotchas TEXT,
  fit_estimate_gb REAL,
  fit_ok INTEGER,
  reported_at TEXT NOT NULL,
  review TEXT NOT NULL DEFAULT 'accepted' CHECK (review IN ('proposed','accepted','rejected')),
  created_by TEXT NOT NULL CHECK (created_by IN ('agent','triage','seed')),
  created_at TEXT NOT NULL,
  superseded_by INTEGER REFERENCES recipes(id)
);

CREATE TABLE evidence (
  id INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL CHECK (target_type IN ('recipe','benchmark','note')),
  target_id INTEGER NOT NULL,
  post_id INTEGER NOT NULL REFERENCES posts(id),
  quote TEXT,
  quote_verified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE benchmark_results (
  id INTEGER PRIMARY KEY,
  model_canonical TEXT NOT NULL,
  quant_format TEXT,
  benchmark TEXT NOT NULL,
  score REAL NOT NULL,
  score_unit TEXT,
  higher_is_better INTEGER NOT NULL DEFAULT 1,
  source_type TEXT NOT NULL CHECK (source_type IN ('forum','model_card','leaderboard','paper','manual')),
  source_url TEXT,
  measured_on TEXT CHECK (measured_on IN ('spark','other','vendor','unknown')),
  engine TEXT,
  measured_at TEXT,
  review TEXT NOT NULL DEFAULT 'accepted' CHECK (review IN ('proposed','accepted','rejected')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE research_notes (
  id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  model_canonical TEXT,
  body TEXT NOT NULL,
  as_of TEXT NOT NULL,
  created_at TEXT NOT NULL,
  superseded_by INTEGER REFERENCES research_notes(id)
);

CREATE TABLE flags (
  id INTEGER PRIMARY KEY,
  target_type TEXT NOT NULL CHECK (target_type IN ('topic','post','repo','hf_model','recipe','model')),
  target_id TEXT NOT NULL,
  label TEXT NOT NULL,
  reason TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 3),
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution TEXT
);

CREATE TABLE web_cache (
  query TEXT PRIMARY KEY,
  response TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
"""

MIGRATION_2 = """
ALTER TABLE topics ADD COLUMN posts_complete INTEGER NOT NULL DEFAULT 0;
"""

MIGRATION_3 = """
ALTER TABLE forums ADD COLUMN listing_floor TEXT;
UPDATE forums SET listing_floor = backfill_floor;

ALTER TABLE watchlist ADD COLUMN release_regex TEXT;
ALTER TABLE watchlist ADD COLUMN sighting_post_id INTEGER;
ALTER TABLE watchlist ADD COLUMN sighting_at TEXT;

CREATE TABLE missing_posts_v3 (
  topic_id INTEGER NOT NULL REFERENCES topics(id),
  post_id INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK (
    reason IN ('absent_from_batch','deleted','hidden','forbidden','not_content')
  ),
  first_seen_at TEXT NOT NULL,
  last_checked_at TEXT NOT NULL,
  PRIMARY KEY (topic_id, post_id)
);

INSERT INTO missing_posts_v3 (topic_id, post_id, reason, first_seen_at, last_checked_at)
  SELECT topic_id, post_id, reason, first_seen_at, last_checked_at FROM missing_posts;

DROP TABLE missing_posts;
ALTER TABLE missing_posts_v3 RENAME TO missing_posts;

UPDATE missing_posts SET reason='not_content'
  WHERE reason='hidden'
    AND post_id IN (SELECT id FROM posts WHERE post_type IN (3, 4));
"""

MIGRATIONS: tuple[str, ...] = (MIGRATION_1, MIGRATION_2, MIGRATION_3)

# One stored timestamp format: YYYY-MM-DDTHH:MM:SS+00:00. COALESCE keeps values that
# SQLite cannot parse (a hand-written `measured_at`, say) instead of nulling them.
_TS = "COALESCE(strftime('%Y-%m-%dT%H:%M:%S+00:00', {column}), {column})"


def _normalise_sql(table: str, columns: tuple[str, ...]) -> str:
    assignments = ",\n  ".join(f"{column} = {_TS.format(column=column)}" for column in columns)
    return f"UPDATE {table} SET\n  {assignments};"


MIGRATION_4 = """
ALTER TABLE sync_runs ADD COLUMN forum_ids TEXT;
ALTER TABLE recipes ADD COLUMN engine_raw TEXT;

-- Legacy tombstones were labelled 'hidden' before post_type 3/4 became 'not_content'.
-- Mark them due for recheck so the next sync reclassifies them.
UPDATE missing_posts SET last_checked_at='1970-01-01T00:00:00+00:00' WHERE reason='hidden';
""" + "\n".join(
    _normalise_sql(table, columns)
    for table, columns in (
        ("forums", ("last_synced_at", "backfill_floor", "listing_floor")),
        ("topics", ("created_at", "bumped_at", "last_posted_at", "first_fetched_at", "fetched_at")),
        (
            "posts",
            ("created_at", "updated_at", "first_fetched_at", "changed_at", "fetched_at"),
        ),
        ("missing_posts", ("first_seen_at", "last_checked_at")),
        ("sync_runs", ("started_at", "finished_at")),
        (
            "watchlist",
            ("first_seen_at", "sighting_at", "sighting_acked_at", "created_at"),
        ),
        ("watch_hits", ("created_at",)),
        ("digest_cursors", ("last_seen_at",)),
        (
            "repos",
            ("pushed_at", "head_seen_at", "first_seen_at", "watched_at", "last_checked_at"),
        ),
        ("repo_mentions", ("mentioned_at",)),
        ("repo_installs", ("installed_at", "updated_at")),
        ("repo_events", ("occurred_at", "detected_at")),
        ("compare_cache", ("fetched_at",)),
        ("hf_models", ("first_seen_at",)),
        ("hf_mentions", ("mentioned_at",)),
        ("models", ("released_at", "updated_at")),
        ("recipes", ("reported_at", "created_at")),
        ("benchmark_results", ("measured_at", "created_at")),
        ("research_notes", ("as_of", "created_at")),
        ("flags", ("created_at", "resolved_at")),
    )
)

MIGRATIONS: tuple[str, ...] = (MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4)


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index in range(version, len(MIGRATIONS)):
        conn.executescript(MIGRATIONS[index])
        conn.execute(f"PRAGMA user_version={index + 1}")
        version = index + 1
    return version


def ensure_forums(conn: sqlite3.Connection, categories: list[Any]) -> None:
    for category in categories:
        existing = conn.execute(
            "SELECT path, name FROM forums WHERE id=?", (category.id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO forums (id, path, name) VALUES (?, ?, ?)",
                (category.id, category.path, category.name),
            )
        elif existing["path"] != category.path or existing["name"] != category.name:
            conn.execute(
                "UPDATE forums SET path=?, name=? WHERE id=?",
                (category.path, category.name, category.id),
            )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_seeds(conn: sqlite3.Connection, seed_dir: str | Path | None = None) -> None:
    """Load seed/*.toml. Reads before it writes, so a steady-state run issues no writes.

    Seed rows never overwrite what the agent wrote, and never reset
    watchlist.first_seen_* or sighting_acked_at.
    """
    directory = Path(seed_dir) if seed_dir else PROJECT_ROOT / "seed"
    now = utcnow_iso()

    for entry in _read_toml(directory / "watchlist.toml").get("watch", []):
        existing = conn.execute(
            "SELECT * FROM watchlist WHERE label=?", (entry["label"],)
        ).fetchone()
        canonical = entry.get("canonical")
        kind = entry.get("kind", "tracking")
        note = entry.get("note")
        if existing is None:
            conn.execute(
                "INSERT INTO watchlist (label, canonical, match_regex, kind, active, note, created_at)"
                " VALUES (?, ?, ?, ?, 1, ?, ?)",
                (entry["label"], canonical, entry["match_regex"], kind, note, now),
            )
            continue
        new_canonical = canonical if canonical is not None else existing["canonical"]
        new_note = note if note is not None else existing["note"]
        if (
            existing["match_regex"] != entry["match_regex"]
            or existing["kind"] != kind
            or existing["canonical"] != new_canonical
            or existing["note"] != new_note
        ):
            conn.execute(
                "UPDATE watchlist SET match_regex=?, kind=?, canonical=?, note=? WHERE id=?",
                (entry["match_regex"], kind, new_canonical, new_note, existing["id"]),
            )

    for entry in _read_toml(directory / "models.toml").get("model", []):
        if conn.execute("SELECT 1 FROM models WHERE canonical=?", (entry["canonical"],)).fetchone():
            continue
        conn.execute(
            "INSERT INTO models (canonical, family, params_total_b, params_active_b, released_at,"
            " hf_repo_id, note, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry["canonical"],
                entry.get("family"),
                entry.get("params_total_b"),
                entry.get("params_active_b"),
                entry.get("released_at"),
                entry.get("hf_repo_id"),
                entry.get("note"),
                now,
            ),
        )

    for entry in _read_toml(directory / "model_aliases.toml").get("alias", []):
        existing = conn.execute(
            "SELECT * FROM model_aliases WHERE alias=?", (entry["alias"],)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO model_aliases (alias, canonical, source, reviewed)"
                " VALUES (?, ?, 'seed', 0)",
                (entry["alias"], entry["canonical"]),
            )
        elif existing["source"] == "seed" and existing["canonical"] != entry["canonical"]:
            conn.execute(
                "UPDATE model_aliases SET canonical=? WHERE alias=?",
                (entry["canonical"], entry["alias"]),
            )


def open_db(settings: Settings | None = None, load_seed: bool = True) -> sqlite3.Connection:
    settings = settings or get_settings()
    conn = connect(settings.db_path)
    ensure_forums(conn, settings.forum.categories)
    if load_seed:
        load_seeds(conn)
    return conn
