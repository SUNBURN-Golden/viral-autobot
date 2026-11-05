"""Additive SQLite tables for the isolated operations pipeline."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_runs (
 id INTEGER PRIMARY KEY, command TEXT NOT NULL, project TEXT NOT NULL, mode TEXT NOT NULL,
 model TEXT, prompt_path TEXT, status TEXT NOT NULL, output TEXT, error TEXT, created TEXT NOT NULL, finished TEXT
);
CREATE TABLE IF NOT EXISTS ops_drafts (
 id INTEGER PRIMARY KEY, project TEXT NOT NULL, draft_id INTEGER NOT NULL REFERENCES drafts(id),
 question_id INTEGER NOT NULL, channel TEXT NOT NULL, copy TEXT NOT NULL, card_copy TEXT, card_path TEXT,
 copy_hash TEXT NOT NULL, evidence_ids TEXT NOT NULL, landing_url TEXT NOT NULL, utm_url TEXT NOT NULL,
 hook_type TEXT, campaign_id TEXT, status TEXT NOT NULL DEFAULT 'draft'
 CHECK(status IN ('draft','blocked','compliant','scheduled','guide_ready','archived')),
 blocked_reasons TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_compliance (
 id INTEGER PRIMARY KEY, ops_draft_id INTEGER NOT NULL REFERENCES ops_drafts(id), copy_hash TEXT NOT NULL,
 verdict TEXT NOT NULL CHECK(verdict IN ('PASS','FAIL')), reasons TEXT NOT NULL, checks TEXT NOT NULL,
 version TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_schedule (
 id INTEGER PRIMARY KEY, ops_draft_id INTEGER NOT NULL UNIQUE REFERENCES ops_drafts(id),
 post_id TEXT NOT NULL UNIQUE, copy_hash TEXT NOT NULL, utm_url TEXT NOT NULL, account_alias TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('scheduled','cancelled')), created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_publish_guides (
 id INTEGER PRIMARY KEY, ops_schedule_id INTEGER NOT NULL UNIQUE REFERENCES ops_schedule(id),
 json_path TEXT NOT NULL, md_path TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_measure_notes (
 id INTEGER PRIMARY KEY, project TEXT NOT NULL, post_id TEXT NOT NULL, check_kind TEXT NOT NULL,
 category TEXT, verdict TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
 UNIQUE(post_id,check_kind)
);
"""


def ensure_ops_schema(conn):
    conn.executescript(SCHEMA)
