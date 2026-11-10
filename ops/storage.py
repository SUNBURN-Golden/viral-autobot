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
    conn.executescript(SCHEMA + LIVE_SCHEMA)
    if 'snapshot_id' not in {r[1] for r in conn.execute('PRAGMA table_info(ops_measure_notes)')}:
        conn.execute('ALTER TABLE ops_measure_notes ADD COLUMN snapshot_id INTEGER REFERENCES ops_metric_snapshots(id)')
    # compliance_verdict distinguishes a business block from a compliance FAIL.
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ops_packages'").fetchone()
    if row and "'SKIPPED'" not in (row[0] or ''):
        conn.executescript("""
        ALTER TABLE ops_packages RENAME TO ops_packages_legacy;
        CREATE TABLE ops_packages (
         id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES ops_runs(id), project TEXT NOT NULL,
         package_date TEXT NOT NULL, checked_date TEXT NOT NULL, packet_hash TEXT NOT NULL,
         package_hash TEXT, output_dir TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ready','blocked')),
         blocked_reasons TEXT NOT NULL, compliance_verdict TEXT NOT NULL CHECK(compliance_verdict IN ('PASS','FAIL','SKIPPED')),
         compliance_checks TEXT NOT NULL, created TEXT NOT NULL
        );
        INSERT INTO ops_packages SELECT * FROM ops_packages_legacy;
        DROP TABLE ops_packages_legacy;
        """)
    conn.commit()


LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_packages (
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES ops_runs(id), project TEXT NOT NULL,
 package_date TEXT NOT NULL, checked_date TEXT NOT NULL, packet_hash TEXT NOT NULL,
 package_hash TEXT, output_dir TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ready','blocked')),
 blocked_reasons TEXT NOT NULL, compliance_verdict TEXT NOT NULL CHECK(compliance_verdict IN ('PASS','FAIL','SKIPPED')),
 compliance_checks TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_metric_snapshots (
 id INTEGER PRIMARY KEY, project TEXT NOT NULL, post_id TEXT NOT NULL,
 check_kind TEXT NOT NULL CHECK(check_kind IN ('48h','7d')), public_url TEXT NOT NULL,
 channel TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('manual','x_public_api','unverified')),
 observed_at TEXT, collected_at TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 UNIQUE(project,post_id,check_kind,payload_hash)
);
"""
