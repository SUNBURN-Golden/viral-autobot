#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Soulbound viral autobot -- read-only growth scout for the soulbound sites.

Subcommands
    run      crawl -> discover -> draft -> metrics -> status
    status   status only (parses w1/rounds.log, runs due metric checks, writes status files)

Design / hard rules (this tool has no posting capability by construction):
  * no posting, commenting, DM, email, follow, login or account use anywhere
  * no paid APIs, no auth, no scraping behind a login
  * no usernames or other personal data stored (Reddit candidates keep thread
    metadata only: url, title, subreddit, created, score, num_comments, keyword)
  * robots.txt honoured on every host, 2-5 s random sleep between requests,
    >= 6 s between Reddit requests, URL/page-fetch caps per site per run
  * drafts are templates only (no LLM) and always carry the header
    "DRAFT - VIRAL compliance check required before posting"
  * metrics are only what a public, unauthenticated request can see;
    anything unknown is reported as "미확인" and never guessed

Everything degrades to a blocker row in the DB + a log line; nothing raises out
of a single project's failure.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

try:  # Python 3.9+
    ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover - defensive
    ZoneInfo = None  # type: ignore[assignment]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None
TZLABEL = "KST"

UA_DEFAULT = "SoulboundViralAutobot/1.0 (+https://hanset.soulbound.co.kr)"
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_XML = "application/xml,text/xml,application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.8"
ACCEPT_JSON = "application/json;q=0.9,*/*;q=0.8"

UNKNOWN = "미확인"

# file extensions never treated as crawlable content pages
SKIP_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".pdf",
    ".zip", ".gz", ".tar", ".mp4", ".mp3", ".mov", ".avi", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".eot", ".json", ".xml",
}

log = logging.getLogger("autobot")


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def now_kst() -> datetime:
    if KST is not None:
        return datetime.now(KST)
    return datetime.now()  # box TZ is Asia/Seoul; fall back to local time


def iso(dt: datetime) -> str:
    """Stable ISO-8601 string (with offset when available) for DB storage."""
    return dt.isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def kst(dt: datetime) -> datetime:
    """Force a datetime onto KST so comparisons are apples-to-apples."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST) if KST is not None else dt
    return dt.astimezone(KST) if KST is not None else dt


def fmt_kst(dt) -> str:
    return kst(dt).strftime("%Y-%m-%d %H:%M") + " " + TZLABEL


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

class Project:
    def __init__(self, raw: dict):
        self.raw = raw
        self.name = str(raw.get("name") or "").strip()
        self.url = (raw.get("url") or None) if raw.get("url") not in ("", "null", None) else None
        self.keywords = [str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()]
        self.hashtags = [str(h).strip() for h in (raw.get("hashtags") or []) if str(h).strip()]
        self.rss_feeds = [str(f).strip() for f in (raw.get("rss_feeds") or []) if str(f).strip()]
        self.tagline_en = raw.get("tagline_en") or f"{self.name} updates"
        self.tagline_ko = raw.get("tagline_ko") or ""
        aliases = raw.get("aliases") or {}
        self.domains = [str(d).lower() for d in (aliases.get("domains") or [])]
        self.names = [str(n).lower() for n in (aliases.get("names") or [])]
        self.terms = [str(t).lower() for t in (aliases.get("terms") or [])]

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.url).netloc.lower() if self.url else ""

    def __repr__(self):  # pragma: no cover
        return f"<Project {self.name} {self.url}>"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("user_agent", UA_DEFAULT)
    cfg.setdefault("campaign", "viral_w1")
    cfg.setdefault("channels", ["x", "threads", "instagram", "reddit"])
    limits = cfg.setdefault("limits", {})
    limits.setdefault("max_urls_per_site", 500)
    limits.setdefault("max_page_fetches_per_site", 30)
    limits.setdefault("max_sitemap_fetches_per_site", 8)
    limits.setdefault("max_sitemap_depth", 2)
    limits.setdefault("first_run_draft_pages", 5)
    limits.setdefault("http_timeout", 20)
    limits.setdefault("max_bytes", 2_000_000)
    limits.setdefault("request_sleep", [2.0, 5.0])
    limits.setdefault("reddit_min_sleep", 6.0)
    limits.setdefault("max_reddit_requests_per_run", 40)
    limits.setdefault("max_metric_checks_per_run", 40)
    limits.setdefault("metric_max_attempts", 3)
    limits.setdefault("metric_retry_minutes", 60)
    limits.setdefault("max_reverify_per_site", 10)
    paths = cfg.setdefault("paths", {})
    paths.setdefault("db", "data/autobot.db")
    paths.setdefault("logs", "logs")
    paths.setdefault("drafts", "out/drafts")
    paths.setdefault("status_slack", "out/status_slack.txt")
    paths.setdefault("rounds_log", "/workspace/viral/w1/rounds.log")
    paths.setdefault("status_md", "/workspace/viral/w1/status.md")
    cfg["_projects"] = [Project(p) for p in (cfg.get("projects") or [])]
    names = [p.name for p in cfg["_projects"]]
    if len(names) != len(set(names)):
        raise SystemExit("config error: duplicate project names")
    return cfg


def abs_path(cfg: dict, key: str) -> str:
    value = cfg["paths"][key]
    return value if os.path.isabs(value) else os.path.join(BASE_DIR, value)


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def setup_logging(cfg: dict, verbose: bool = False) -> None:
    log_dir = abs_path(cfg, "logs")
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s " + TZLABEL + " | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    sh = logging.FileHandler(os.path.join(log_dir, "autobot.log"), encoding="utf-8")
    sh.setFormatter(fmt)
    log.handlers.clear()
    log.addHandler(stream)
    log.addHandler(sh)


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  url TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  lastmod TEXT,
  content_hash TEXT,
  title TEXT,
  description TEXT,
  last_status TEXT NOT NULL DEFAULT 'new',
  last_changed TEXT,
  fetch_count INTEGER NOT NULL DEFAULT 0,
  http_status INTEGER,
  last_verified TEXT,
  valid_until TEXT,
  UNIQUE (project, url)
);
CREATE INDEX IF NOT EXISTS idx_pages_proj_status ON pages (project, last_status);
CREATE TABLE IF NOT EXISTS crawl_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started TEXT NOT NULL,
  finished TEXT,
  project TEXT NOT NULL,
  source TEXT,
  first_run INTEGER NOT NULL DEFAULT 0,
  urls_seen INTEGER NOT NULL DEFAULT 0,
  new_count INTEGER NOT NULL DEFAULT 0,
  changed_count INTEGER NOT NULL DEFAULT 0,
  unchanged_count INTEGER NOT NULL DEFAULT 0,
  fetches INTEGER NOT NULL DEFAULT 0,
  robots_blocked INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  keyword TEXT,
  source TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  subreddit TEXT,
  created_utc TEXT,
  score INTEGER,
  num_comments INTEGER,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  UNIQUE (project, source, url)
);
CREATE INDEX IF NOT EXISTS idx_cand_proj ON candidates (project);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  page_url TEXT NOT NULL,
  path TEXT NOT NULL,
  channels TEXT NOT NULL,
  page_status TEXT,
  created TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'candidate',
  hold_reason TEXT,
  UNIQUE (project, page_url)
);
CREATE TABLE IF NOT EXISTS published (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT,
  url TEXT,
  label TEXT,
  kind TEXT,
  published_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'rounds.log',
  line_no INTEGER,
  raw_line TEXT,
  mapping TEXT,
  due_hint_48h TEXT,
  due_hint_7d TEXT,
  dedupe_key TEXT UNIQUE,
  first_seen TEXT NOT NULL,
  post_id TEXT,
  channel TEXT,
  target_url TEXT,
  state TEXT NOT NULL DEFAULT 'published_verified',
  verified_at TEXT,
  hold_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_pub_proj ON published (project);
CREATE TABLE IF NOT EXISTS metric_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  published_id INTEGER NOT NULL REFERENCES published(id),
  check_kind TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  checked_at TEXT,
  result TEXT,
  retry_at TEXT,
  failure_reason TEXT,
  UNIQUE (published_id, check_kind)
);
CREATE INDEX IF NOT EXISTS idx_checks_due ON metric_checks (status, due_at);
CREATE TABLE IF NOT EXISTS post_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id TEXT,
  entity TEXT NOT NULL CHECK (entity IN ('draft', 'post')),
  entity_id INTEGER NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  at TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS blockers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL,
  code TEXT NOT NULL,
  detail TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE (project, code)
);
"""


def db_connect(cfg: dict) -> sqlite3.Connection:
    path = abs_path(cfg, "db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade existing ledgers in place; safe to repeat on every connection."""
    additions = {
        "pages": {"http_status": "INTEGER", "last_verified": "TEXT", "valid_until": "TEXT"},
        "drafts": {"state": "TEXT NOT NULL DEFAULT 'candidate'", "hold_reason": "TEXT"},
        "published": {"post_id": "TEXT", "channel": "TEXT", "target_url": "TEXT",
                      "state": "TEXT NOT NULL DEFAULT 'published_verified'",
                      "verified_at": "TEXT", "hold_reason": "TEXT"},
        "metric_checks": {"retry_at": "TEXT", "failure_reason": "TEXT"},
    }
    with conn:
        for table, columns in additions.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        conn.execute("UPDATE published SET channel=kind WHERE channel IS NULL")
        conn.execute("UPDATE published SET verified_at=published_at WHERE verified_at IS NULL "
                     "AND state IN ('published_verified','measured','evaluated')")
        used = {r["post_id"] for r in conn.execute(
            "SELECT post_id FROM published WHERE post_id IS NOT NULL")}
        for row in conn.execute("SELECT * FROM published WHERE post_id IS NULL ORDER BY id").fetchall():
            post_id = make_post_id(row["project"], row["channel"],
                                   row["url"] or row["target_url"], row["label"])
            if post_id in used:
                log.warning("[migration] post_id collision for published row %s; retained without id",
                            row["id"])
                continue
            conn.execute("UPDATE published SET post_id=? WHERE id=?", (post_id, row["id"]))
            used.add(post_id)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_post_id ON published(post_id) "
                     "WHERE post_id IS NOT NULL")
        conn.execute("UPDATE metric_checks SET status='pending' WHERE status='error'")
        for row in conn.execute("SELECT id, result FROM metric_checks WHERE status='done'").fetchall():
            try:
                result = json.loads(row["result"] or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict) and result.get("checked") is False:
                conn.execute("UPDATE metric_checks SET status='failed', retry_at=NULL, "
                             "failure_reason=? WHERE id=?",
                             (result.get("reason") or "legacy unchecked result", row["id"]))


def meta_get(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


# --------------------------------------------------------------------------
# state machine (ledger only; no publishing capability)
# --------------------------------------------------------------------------

POST_FLOW = ("candidate", "fact_checked", "approved", "scheduled",
             "published_verified", "measured", "evaluated")
HOLD_STATES = ("needs_research", "no_demand_hold", "expired_hold")
PRE_PUBLISH = POST_FLOW[:4]
TRANSITIONS = {state: set() for state in POST_FLOW + HOLD_STATES}
for _src, _dst in zip(POST_FLOW, POST_FLOW[1:]):
    TRANSITIONS[_src].add(_dst)
for _src in PRE_PUBLISH:
    TRANSITIONS[_src].update(HOLD_STATES)
for _src in HOLD_STATES:
    TRANSITIONS[_src].add("candidate")


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, ())


def transition(conn, entity, entity_id, dst, now, reason="") -> str:
    table = {"draft": "drafts", "post": "published"}.get(entity)
    if table is None:
        raise ValueError(f"unknown entity: {entity}")
    # A savepoint also keeps state + audit atomic inside a caller's transaction.
    conn.execute("SAVEPOINT post_transition")
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown {entity}: {entity_id}")
        src = row["state"]
        if src != dst:
            if not can_transition(src, dst):
                raise ValueError(f"invalid transition: {src} -> {dst}")
            conn.execute(f"UPDATE {table} SET state=?, hold_reason=? WHERE id=?",
                         (dst, reason if dst in HOLD_STATES else None, entity_id))
            conn.execute(
                "INSERT INTO post_events (post_id, entity, entity_id, from_state, to_state, at, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["post_id"] if entity == "post" else None,
                 entity, entity_id, src, dst, iso(now), reason))
        conn.execute("RELEASE post_transition")
    except Exception:
        conn.execute("ROLLBACK TO post_transition")
        conn.execute("RELEASE post_transition")
        raise
    return dst


def make_post_id(project, channel, url=None, label=None) -> str:
    identity = normalize_url(url) or (label or "").strip().lower()
    value = f"{project or ''}|{channel or ''}|{identity}"
    return "p_" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def record_post(conn, project, channel, target_url, label, now, state="scheduled") -> str:
    """Record planned work, without asserting that anything has been published."""
    if state not in PRE_PUBLISH + HOLD_STATES:
        raise ValueError("record_post requires a pre-publication or hold state")
    post_id = make_post_id(project, channel, target_url, label)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO published "
            "(post_id, project, channel, kind, target_url, label, state, published_at, source, first_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ledger', ?)",
            (post_id, project, channel, channel, normalize_url(target_url) or None,
             label, state, iso(now), iso(now)))
    return post_id


def mark_published_verified(conn, post_id, post_url, verified_at, now):
    """Record a confirmed public URL; measurement starts at verification time."""
    url = normalize_url(post_url)
    if not url:
        raise ValueError("a public HTTP(S) post URL is required")
    with conn:
        row = conn.execute("SELECT * FROM published WHERE post_id=?", (post_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown post_id: {post_id}")
        if row["state"] == "scheduled":
            conn.execute("UPDATE published SET url=?, verified_at=?, published_at=? WHERE id=?",
                         (url, iso(verified_at), iso(verified_at), row["id"]))
            transition(conn, "post", row["id"], "published_verified", now)
        elif row["state"] not in POST_FLOW[4:] or row["url"] != url:
            raise ValueError(f"cannot verify post in state {row['state']}")
        # Repeated verification preserves the original timestamp and due dates.
    _queue_metric_checks(conn, now)


# --------------------------------------------------------------------------
# blockers
# --------------------------------------------------------------------------

def blocker_upsert(conn: sqlite3.Connection, project: str, code: str, detail: str,
                   now_iso: str, hits: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO blockers (project, code, detail, first_seen, last_seen, hits, active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(project, code) DO UPDATE SET
          detail=excluded.detail,
          last_seen=excluded.last_seen,
          hits=blockers.hits + excluded.hits,
          active=1
        """,
        (project, code, detail, now_iso, now_iso, hits),
    )
    conn.commit()
    log.warning("[blocker] %s %s: %s", project, code, detail)


def blockers_deactivate(conn: sqlite3.Connection, project: str, codes_keep=()) -> None:
    """Turn off project blockers that did not re-fire in the latest run."""
    rows = conn.execute(
        "SELECT code FROM blockers WHERE project=? AND active=1", (project,)
    ).fetchall()
    for row in rows:
        if row["code"] not in codes_keep:
            conn.execute(
                "UPDATE blockers SET active=0 WHERE project=? AND code=?",
                (project, row["code"]),
            )
    conn.commit()


def ensure_config_blockers(conn: sqlite3.Connection, cfg: dict, now=None) -> None:
    """Blockers that come straight from config, so `status` shows them too.

    e.g. agamajung has url=null -> "공개 URL 없음" even before any crawl ran.
    """
    now_s = iso(now or now_kst())
    for proj in cfg["_projects"]:
        if not proj.url:
            blocker_upsert(conn, proj.name, "NO_PUBLIC_URL",
                           "공개 URL 없음 (no public site yet) - crawl/discover 대상 없음",
                           now_s)


# --------------------------------------------------------------------------
# polite HTTP fetcher (robots-aware, throttled, byte-capped)
# --------------------------------------------------------------------------

class FetchResult:
    __slots__ = ("url", "status", "body", "headers", "error", "elapsed")

    def __init__(self, url, status, body, headers, error, elapsed):
        self.url = url
        self.status = status
        self.body = body
        self.headers = headers
        self.error = error
        self.elapsed = elapsed

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def charset(self):
        try:
            return self.headers.get_content_charset()
        except Exception:
            return None


class RobotsInfo:
    __slots__ = ("host", "rp", "rules", "sitemaps", "state")

    def __init__(self, host, rp, rules, sitemaps, state):
        self.host = host
        self.rp = rp
        self.rules = rules  # RobotsRules, longest-match view of the same file
        self.sitemaps = sitemaps
        self.state = state  # 'ok' | 'missing' | 'error' | 'server-error'


_ROBOTS_UA_RE = re.compile(r"^\s*user-?agent\s*:\s*(.*?)\s*$", re.I)
_ROBOTS_RULE_RE = re.compile(r"^\s*(allow|disallow)\s*:\s*(.*?)\s*$", re.I)


class RobotsRules:
    """robots.txt groups with REP longest-match semantics.

    urllib.robotparser is kept as the primary check, but this stdlib version
    returns the *first* matching rule, so a broad ``Allow: /`` line placed
    above a more specific ``Disallow: /private/`` would hide the disallow.
    The crawler therefore requires *both* verdicts to allow a URL.
    """

    def __init__(self, text: str = ""):
        self.groups = []  # [([ua tokens], [(allow: bool, path: str)])]
        self._compiled = {}
        tokens: list = []
        rules: list = []
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
            if not line.strip():
                continue
            ua = _ROBOTS_UA_RE.match(line)
            if ua:
                if rules:
                    self.groups.append((tokens, rules))
                    tokens, rules = [], []
                tokens.append(ua.group(1).lower())
                continue
            rule = _ROBOTS_RULE_RE.match(line)
            if rule:
                allow = rule.group(1).lower() == "allow"
                path = rule.group(2)
                if path == "" and not allow:
                    continue  # bare "Disallow:" == allow everything
                rules.append((allow, path))
        if tokens or rules:
            self.groups.append((tokens, rules))

    def _matcher(self, path_rule: str):
        cached = self._compiled.get(path_rule)
        if cached is not None:
            return cached
        end = path_rule.endswith("$")
        body = path_rule[:-1] if end else path_rule
        precedence = len(body.replace("*", "")) + (1 if end else 0)
        pattern = ".*".join(re.escape(part) for part in body.split("*"))
        cached = (re.compile(pattern + ("$" if end else "")).match, precedence)
        self._compiled[path_rule] = cached
        return cached

    def allows(self, useragent: str, url: str) -> bool:
        try:
            path = urllib.parse.unquote(urllib.parse.urlsplit(url).path or "/")
        except Exception:
            path = urllib.parse.urlsplit(url).path or "/"
        ua = (useragent or "*").lower()
        specific = [g for g in self.groups
                    if g[0] and "*" not in g[0] and any(tok and tok in ua for tok in g[0])]
        groups = specific or [g for g in self.groups if "*" in g[0]]
        if not groups:
            return True
        best = None  # (precedence, allow)
        for _, rules in groups:
            for allow, path_rule in rules:
                matcher, precedence = self._matcher(path_rule)
                if not matcher(path):
                    continue
                if best is None or precedence > best[0]:
                    best = (precedence, allow)
        return True if best is None else bool(best[1])


class Fetcher:
    def __init__(self, cfg: dict):
        limits = cfg["limits"]
        self.ua = cfg["user_agent"]
        self.sleep_range = tuple(limits.get("request_sleep", [2.0, 5.0]))
        self.timeout = float(limits.get("http_timeout", 20))
        self.max_bytes = int(limits.get("max_bytes", 2_000_000))
        self.requests = 0
        self.errors = 0
        self._robots: dict = {}

    # -- throttling -------------------------------------------------------
    def throttle(self, seconds=None) -> None:
        if seconds is None:
            lo, hi = self.sleep_range
            seconds = random.uniform(float(lo), float(hi))
        if seconds and seconds > 0:
            time.sleep(seconds)

    # -- robots -----------------------------------------------------------
    def robots(self, url: str) -> RobotsInfo:
        parts = urllib.parse.urlsplit(url)
        origin = (parts.scheme + "://" + parts.netloc) if parts.scheme else parts.netloc
        origin = (origin or "").lower()
        if origin in self._robots:
            return self._robots[origin]
        robots_url = origin.rstrip("/") + "/robots.txt"
        res = self.get(robots_url, accept="text/plain,*/*;q=0.8")
        sitemaps: list = []
        rules = RobotsRules()
        if res.ok and res.body:
            text = res.body.decode("utf-8", "replace")
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(text.splitlines())
            rules = RobotsRules(text)
            for line in text.splitlines():
                m = re.match(r"\s*sitemap\s*:\s*(\S+)", line, re.I)
                if m and m.group(1).lower().startswith(("http://", "https://")):
                    sitemaps.append(m.group(1).strip())
            info = RobotsInfo(origin, rp, rules, sitemaps, "ok")
        elif res.status in (401, 403, 404, 410) or (res.ok and not res.body):
            info = RobotsInfo(origin, None, rules, sitemaps, "missing")  # no robots -> allow
        elif res.status is not None and res.status >= 500:
            info = RobotsInfo(origin, None, rules, sitemaps, "server-error")
        else:
            info = RobotsInfo(origin, None, rules, sitemaps, "error")
        self._robots[origin] = info
        return info

    def can_fetch(self, url: str) -> bool:
        info = self.robots(url)
        if info.state == "server-error":
            return False  # unreachable robots: be conservative
        if info.rp is None:
            return True
        stdlib = bool(info.rp.can_fetch(self.ua, url) or info.rp.can_fetch("*", url))
        longest_match = info.rules.allows(self.ua, url)
        return stdlib and longest_match

    # -- requests ---------------------------------------------------------
    def get(self, url: str, accept: str = ACCEPT_HTML, sleep=None, max_bytes=None,
            method: str = "GET") -> FetchResult:
        self.throttle(sleep)
        limit = max_bytes or self.max_bytes
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": self.ua,
            "Accept": accept,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        })
        started = time.time()
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read(limit + 1)
                return FetchResult(resp.geturl() or url, resp.status, body,
                                   resp.headers, None, time.time() - started)
        except urllib.error.HTTPError as exc:
            try:
                exc.read(512)
            except Exception:
                pass
            self.errors += 1
            return FetchResult(url, exc.code, b"", exc.headers, "HTTPError",
                               time.time() - started)
        except Exception as exc:  # URLError, timeout, ssl, ...
            self.errors += 1
            return FetchResult(url, None, b"", None, type(exc).__name__,
                               time.time() - started)


# --------------------------------------------------------------------------
# small text / url helpers
# --------------------------------------------------------------------------

def clean_ws(text: str) -> str:
    return " ".join((text or "").split())


def clip(text: str, limit: int) -> str:
    text = clean_ws(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def slugify(text: str, maxlen: int = 48) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "-", text).strip("-").lower()
    return text[:maxlen].strip("-")


def url_hash(url: str, n: int = 6) -> str:
    return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:n]


def normalize_url(url: str, base: str = "") -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if base and not url.lower().startswith(("http://", "https://")):
        url = urllib.parse.urljoin(base, url)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    host = parts.netloc.lower().rstrip(".")
    path = parts.path or "/"
    query = ("?" + parts.query) if parts.query else ""
    return f"{parts.scheme}://{host}{path}{query}"  # fragment dropped on purpose


def add_utm(url: str, channel: str, campaign: str) -> str:
    """Merge utm_* params into an existing query string without clobbering it."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    wanted = {"utm_source": channel, "utm_medium": "social", "utm_campaign": campaign}
    kept, seen = [], set()
    for key, value in params:
        if key in wanted:
            if key not in seen:
                kept.append((key, wanted[key]))
                seen.add(key)
            continue
        kept.append((key, value))
    for key, value in wanted.items():
        if key not in seen:
            kept.append((key, value))
    query = urllib.parse.urlencode(kept, doseq=True, quote_via=urllib.parse.quote)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    query, parts.fragment))


def is_reddit_url(url: str) -> bool:
    host = urllib.parse.urlsplit(url or "").netloc.lower()
    return host == "reddit.com" or host.endswith(".reddit.com")


def looks_skippable(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    ext = os.path.splitext(path)[1]
    return ext in SKIP_EXT


# --------------------------------------------------------------------------
# HTML / XML / RSS parsing
# --------------------------------------------------------------------------

class _MetaExtractor(HTMLParser):
    """Collect <title> and meta description/og:description (first wins)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.description = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title" and self.title is None:
            self._in_title = True
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").strip().lower()
            content = (attrs.get("content") or "").strip()
            if not content:
                return
            if name == "description" and self.description is None:
                self.description = content
            elif name in ("og:description", "twitter:description") and self.description is None:
                self.description = content

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and self.title is None:
            self.title = ""  # title may arrive in several data chunks
        if self._in_title:
            self.title = (self.title or "") + data


def sniff_charset(body: bytes, header_charset) -> str:
    if header_charset:
        try:
            "".encode(header_charset)
            return header_charset
        except Exception:
            pass
    head = body[:4096].decode("ascii", "ignore")
    m = re.search(r'<meta[^>]+charset=["\']?([\w\-]+)', head, re.I)
    if m:
        return m.group(1)
    return "utf-8"


def extract_meta(body: bytes, header_charset=None):
    """Return (title, description) for an HTML document."""
    try:
        parser = _MetaExtractor()
        parser.feed(body.decode(sniff_charset(body, header_charset), "replace"))
        parser.close()
        title = clean_ws(parser.title)[:300] if parser.title else None
        desc = clean_ws(parser.description)[:500] if parser.description else None
        return title, desc
    except Exception:
        return None, None


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap_entry(body: bytes):
    """Parse sitemap / sitemap-index bytes -> (child_sitemaps, [(url, lastmod)])."""
    children, entries = [], []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        # some feeds are served as sitemaps; try RSS/Atom before giving up
        rss = parse_feed_body(body)
        if rss:
            return [], rss
        return [], []
    if _local_tag(root.tag) == "sitemapindex":
        for node in root.iter():
            if _local_tag(node.tag) != "sitemap":
                continue
            loc = ""
            for child in node:
                if _local_tag(child.tag) == "loc" and (child.text or "").strip():
                    loc = child.text.strip()
                    break
            if loc:
                children.append(loc)
        return children, []
    if _local_tag(root.tag) == "urlset":
        for node in root.iter():
            if _local_tag(node.tag) != "url":
                continue
            loc, lastmod = "", None
            for child in node:
                tag = _local_tag(child.tag)
                if tag == "loc" and (child.text or "").strip():
                    loc = child.text.strip()
                elif tag == "lastmod" and (child.text or "").strip():
                    lastmod = child.text.strip()
            if loc:
                entries.append((loc, lastmod))
        return [], entries
    # unknown root: maybe an RSS feed
    rss = parse_feed_body(body)
    if rss:
        return [], rss
    return [], []


def parse_feed_body(body: bytes):
    """Parse RSS 2.0 / Atom bytes -> [(url, lastmod)]."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    entries = []
    for node in root.iter():
        tag = _local_tag(node.tag)
        if tag not in ("item", "entry"):
            continue
        link, updated = "", None
        for child in node:
            ctag = _local_tag(child.tag)
            text = (child.text or "").strip()
            if ctag in ("link",) and not child.get("href"):
                link = text
            elif ctag == "link" and child.get("href"):
                link = child.get("href")
            elif ctag in ("pubdate", "published", "updated", "date"):
                updated = text
            if link and updated:
                break
        if link:
            entries.append((link, normalize_date(updated)))
    return entries


def normalize_date(value):
    """Best-effort normalisation of sitemap/feed dates to ISO (date part kept)."""
    if not value:
        return None
    value = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", value)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4):
            return f"{y:04d}-{mo:02d}-{d:02d}T{int(m.group(4)):02d}:{int(m.group(5)):02d}:00"
        return f"{y:04d}-{mo:02d}-{d:02d}"
    try:
        return parsedate_to_datetime(value).isoformat(timespec="seconds")
    except Exception:
        return None


def content_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# --------------------------------------------------------------------------
# 1. crawl
# --------------------------------------------------------------------------

PAGE_DEAD = ("gone", "expired")


def hold_for_page(conn, project, page_url, now, reason):
    counts = []
    for entity, table, column in (("draft", "drafts", "page_url"),
                                  ("post", "published", "target_url")):
        rows = conn.execute(f"SELECT id, state FROM {table} WHERE project=? AND {column}=?",
                            (project, page_url)).fetchall()
        held = 0
        for row in rows:
            if row["state"] in PRE_PUBLISH:
                transition(conn, entity, row["id"], "expired_hold", now, reason)
                held += 1
        counts.append(held)
    return tuple(counts)


def _mark_page_dead(conn, project, url, status, http_status, now):
    if status not in PAGE_DEAD:
        raise ValueError(f"invalid dead page status: {status}")
    with conn:
        conn.execute("UPDATE pages SET last_status=?, http_status=COALESCE(?, http_status), "
                     "last_verified=? WHERE project=? AND url=?",
                     (status, http_status, iso(now), project, url))
        counts = hold_for_page(conn, project, url, now,
                               f"page {status}: {url}")
    return sum(counts)


def reverify_pages(conn, fetcher, proj, cfg, now, stale_urls=None) -> dict:
    """Expire dated evidence locally, then check a bounded set of stored URLs."""
    result = {"checked": 0, "gone": 0, "expired": 0, "held": 0}
    pages = conn.execute("SELECT * FROM pages WHERE project=? "
                         "ORDER BY COALESCE(last_verified, ''), id", (proj.name,)).fetchall()
    stale = set(stale_urls or ())
    linked = set()
    for table, column in (("drafts", "page_url"), ("published", "target_url")):
        linked.update(r["url"] for r in conn.execute(
            f"SELECT {column} AS url FROM {table} WHERE project=? "
            "AND state IN ('candidate','fact_checked','approved','scheduled')", (proj.name,)))
    candidates = []
    for page in pages:
        if page["last_status"] in PAGE_DEAD:
            continue
        deadline = parse_iso(page["valid_until"])
        if deadline is not None and kst(deadline) < kst(now):
            result["held"] += _mark_page_dead(conn, proj.name, page["url"], "expired", None, now)
            result["expired"] += 1
        elif page["url"] in stale or page["url"] in linked:
            candidates.append(page)
    cap = max(0, int(cfg["limits"].get("max_reverify_per_site", 10)))
    for page in candidates:
        if result["checked"] >= cap:
            break
        url = page["url"]
        if not fetcher.can_fetch(url):
            continue
        res = fetcher.get(url, max_bytes=4096)
        result["checked"] += 1
        if res.status is None:
            continue  # a connection failure establishes neither life nor death
        if res.status in (404, 410):
            result["held"] += _mark_page_dead(conn, proj.name, url, "gone", res.status, now)
            result["gone"] += 1
        else:
            conn.execute("UPDATE pages SET http_status=?, last_verified=? WHERE project=? AND url=?",
                         (res.status, iso(now), proj.name, url))
    conn.commit()
    dead = conn.execute("SELECT COUNT(*) AS n FROM pages WHERE project=? "
                        "AND last_status IN ('gone','expired')", (proj.name,)).fetchone()["n"]
    if dead:
        blocker_upsert(conn, proj.name, "PAGES_GONE",
                       f"{dead} stored page(s) gone or expired; related work held", iso(now))
    return result


def crawl_project(conn: sqlite3.Connection, fetcher: Fetcher, proj: Project,
                  cfg: dict, now: datetime, max_urls=None, max_fetches=None) -> None:
    limits = cfg["limits"]
    started = iso(now)
    had_prior = conn.execute(
        "SELECT COUNT(*) AS n FROM pages WHERE project=?", (proj.name,)
    ).fetchone()["n"]
    run_values = {
        "started": started, "project": proj.name, "first_run": 0,
        "source": None, "urls_seen": 0, "new_count": 0, "changed_count": 0,
        "unchanged_count": 0, "fetches": 0, "robots_blocked": 0, "notes": [],
    }
    keep_blockers = {"PAGES_GONE"}

    def note(msg):
        run_values["notes"].append(msg)
        log.info("[crawl:%s] %s", proj.name, msg)

    if not proj.url:
        ensure_config_blockers(conn, cfg, now)
        blockers_deactivate(conn, proj.name, {"NO_PUBLIC_URL", "PAGES_GONE"})
        return

    base = proj.url.rstrip("/")
    cap_urls = int(max_urls or limits["max_urls_per_site"])
    cap_fetches = int(max_fetches or limits["max_page_fetches_per_site"])
    cap_sitemaps = int(limits["max_sitemap_fetches_per_site"])
    max_depth = int(limits["max_sitemap_depth"])

    # ---- robots ---------------------------------------------------------
    robots = fetcher.robots(base + "/")
    if robots.state == "ok" and robots.rp is not None:
        if not fetcher.can_fetch(base + "/"):
            blocker_upsert(conn, proj.name, "ROBOTS_DISALLOWED",
                           f"robots.txt disallows crawling {base}", started)
            keep_blockers.add("ROBOTS_DISALLOWED")
            _finish_crawl_run(conn, run_values)
            blockers_deactivate(conn, proj.name, keep_blockers)
            return
    if robots.state == "server-error":
        blocker_upsert(conn, proj.name, "ROBOTS_UNREACHABLE",
                       f"robots.txt server error ({robots.host}) - site skipped this run",
                       started)
        keep_blockers.add("ROBOTS_UNREACHABLE")
        _finish_crawl_run(conn, run_values)
        blockers_deactivate(conn, proj.name, keep_blockers)
        return
    if robots.sitemaps:
        note(f"robots.txt sitemaps: {', '.join(robots.sitemaps[:cap_sitemaps])}")

    # ---- url discovery: sitemap(s), then RSS/feed fallback --------------
    entries: list = []          # [(url, lastmod)]
    sitemap_urls = list(robots.sitemaps[:cap_sitemaps])
    sitemap_fetches = 0
    seen_sitemaps = set()

    def fetch_sitemap(url, depth):
        nonlocal sitemap_fetches
        if sitemap_fetches >= cap_sitemaps or depth > max_depth or url in seen_sitemaps:
            return
        seen_sitemaps.add(url)
        sitemap_fetches += 1
        res = fetcher.get(url, accept=ACCEPT_XML)
        body = res.body or b""
        if url.endswith(".gz") and body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body[:fetcher.max_bytes])
            except Exception:
                note(f"sitemap gunzip failed: {url}")
                return
        if not res.ok or not body:
            note(f"sitemap not readable ({res.status or res.error}): {url}")
            return
        children, found = parse_sitemap_entry(body)
        entries.extend(found)
        if children:
            note(f"sitemap index ({len(children)} children): {url}")
            for child in children[: cap_sitemaps - sitemap_fetches]:
                fetch_sitemap(normalize_url(child) or child, depth + 1)

    for sitemap_url in sitemap_urls:
        fetch_sitemap(sitemap_url, 0)

    if not entries:  # try the well-known locations before falling back to feeds
        for guess in ("sitemap.xml", "sitemap_index.xml", "wp-sitemap.xml",
                      "sitemap.xml.gz", "atom.xml"):
            if sitemap_fetches >= cap_sitemaps:
                break
            fetch_sitemap(f"{base}/{guess}", 0)
            if entries:
                break

    source = "sitemap" if entries else None
    feeds = list(proj.rss_feeds)
    if not entries and feeds:
        for feed in feeds:
            res = fetcher.get(normalize_url(feed) or feed, accept=ACCEPT_XML)
            if res.ok and res.body:
                found = parse_feed_body(res.body)
                entries.extend(found)
                if found:
                    note(f"feed entries: {len(found)} from {feed}")
        if entries:
            source = "rss (config)"

    if not entries:  # last resort: RSS/feed auto-discovery on common paths
        for guess in ("feed", "rss", "rss.xml", "feed.xml", "index.xml", "blog/feed"):
            res = fetcher.get(f"{base}/{guess}", accept=ACCEPT_XML)
            if res.ok and res.body:
                found = parse_feed_body(res.body)
                if found:
                    entries.extend(found)
                    source = f"rss (/{guess})"
                    note(f"feed fallback /{guess}: {len(found)} entries")
                    break

    if not entries:
        blocker_upsert(conn, proj.name, "NO_SITEMAP_OR_FEED",
                       f"no sitemap.xml / sitemap index / RSS feed discovered under {base}",
                       started)
        keep_blockers.add("NO_SITEMAP_OR_FEED")
        entries = [(base + "/", None)]  # still record the homepage
        note("no sitemap or feed found; recording homepage only")
        source = "homepage-only"

    # ---- normalise + cap ------------------------------------------------
    unique: dict = {}
    for loc, lastmod in entries:
        url = normalize_url(loc, base)
        if not url or looks_skippable(url):
            continue
        prev = unique.get(url)
        if prev is None or (lastmod and not prev):
            unique[url] = lastmod
    ordered = sorted(unique.items(), key=lambda kv: (kv[1] or "", kv[0]), reverse=True)
    url_list = ordered[:cap_urls]
    if len(unique) > cap_urls:
        note(f"capped at {cap_urls} urls ({len(unique)} discovered)")
    run_values["urls_seen"] = len(url_list)
    run_values["source"] = source

    # ---- per-url processing --------------------------------------------
    # statuses describe the *latest* run: anything not rediscovered/re-fetched
    # below falls back to 'unchanged', so counts mean "new content this run".
    conn.execute("UPDATE pages SET last_status='unchanged' WHERE project=? "
                 "AND last_status NOT IN ('gone','expired')",
                 (proj.name,))
    conn.commit()
    fetches = 0
    fetch_errors = 0
    needed_but_capped = 0
    rows = {r["url"]: r for r in conn.execute(
        "SELECT * FROM pages WHERE project=?", (proj.name,)).fetchall()}

    for url, lastmod in url_list:
        lastmod = normalize_date(lastmod)
        prev = rows.get(url)
        if prev is not None and prev["last_status"] in PAGE_DEAD:
            rows.pop(url, None)
            continue
        needs_fetch = prev is None or prev["content_hash"] is None or (
            lastmod is not None and lastmod != prev["lastmod"])

        if needs_fetch and fetches >= cap_fetches:
            needed_but_capped += 1
            status = "new" if prev is None else "unchanged"
        elif needs_fetch:
            if not fetcher.can_fetch(url):
                run_values["robots_blocked"] += 1
                keep_blockers.add("ROBOTS_BLOCKED_URL")
                status = "new" if prev is None else "unchanged"
            else:
                res = fetcher.get(url, accept=ACCEPT_HTML)
                fetches += 1
                if res.ok and res.body:
                    title, desc = extract_meta(res.body, res.charset)
                    digest = content_hash(res.body)
                    if prev is None:
                        status = "new"
                    elif digest != prev["content_hash"]:
                        status = "changed"
                    else:
                        status = "unchanged"
                    _upsert_page(conn, proj.name, url, now, lastmod, digest,
                                 title, desc, status, fetched=True, prev=prev)
                    conn.execute("UPDATE pages SET http_status=?, last_verified=? "
                                 "WHERE project=? AND url=?", (res.status, started, proj.name, url))
                    conn.commit()
                    rows.pop(url, None)
                    continue
                if res.status in (404, 410):
                    _upsert_page(conn, proj.name, url, now, lastmod,
                                 prev["content_hash"] if prev else None,
                                 prev["title"] if prev else None,
                                 prev["description"] if prev else None,
                                 "gone", fetched=True, prev=prev)
                    _mark_page_dead(conn, proj.name, url, "gone", res.status, now)
                    rows.pop(url, None)
                    continue
                fetch_errors += 1
                status = "new" if prev is None else "unchanged"
                note(f"fetch failed ({res.status or res.error}): {url}")
        else:
            status = "unchanged"

        _upsert_page(conn, proj.name, url, now, lastmod,
                     prev["content_hash"] if prev else None,
                     prev["title"] if prev else None,
                     prev["description"] if prev else None,
                     status, fetched=False, prev=prev)
        rows.pop(url, None)

    run_values["fetches"] = fetches
    if run_values["robots_blocked"]:
        blocker_upsert(conn, proj.name, "ROBOTS_BLOCKED_URL",
                       f"{run_values['robots_blocked']} sitemap url(s) disallowed by "
                       f"robots.txt were stored but not fetched", started)
        keep_blockers.add("ROBOTS_BLOCKED_URL")
    if needed_but_capped:
        blocker_upsert(conn, proj.name, "FETCH_CAP",
                       f"{needed_but_capped} url(s) needed a fetch but the "
                       f"{cap_fetches}/run page-fetch cap was reached", started)
        keep_blockers.add("FETCH_CAP")
    if fetch_errors:
        blocker_upsert(conn, proj.name, "FETCH_ERRORS",
                       f"{fetch_errors} page fetch(es) failed on this run", started)
        keep_blockers.add("FETCH_ERRORS")

    reverify_pages(conn, fetcher, proj, cfg, now, stale_urls=list(rows))
    counts = conn.execute(
        "SELECT last_status, COUNT(*) AS n FROM pages WHERE project=? "
        "GROUP BY last_status", (proj.name,)).fetchall()
    by_status = {r["last_status"]: r["n"] for r in counts}
    run_values["new_count"] = by_status.get("new", 0)
    run_values["changed_count"] = by_status.get("changed", 0)
    run_values["unchanged_count"] = by_status.get("unchanged", 0)
    run_values["first_run"] = 0 if had_prior else 1
    note(f"done: {len(url_list)} urls, new {run_values['new_count']}, "
         f"changed {run_values['changed_count']}, fetches {fetches} ({source})")
    _finish_crawl_run(conn, run_values)
    blockers_deactivate(conn, proj.name, keep_blockers)


def _upsert_page(conn, project, url, now, lastmod, digest, title, desc, status,
                 fetched, prev) -> None:
    now_s = iso(now)
    changed_at = now_s if status in ("new", "changed") else (prev["last_changed"] if prev else None)
    conn.execute(
        """
        INSERT INTO pages (project, url, first_seen, last_seen, lastmod, content_hash,
                           title, description, last_status, last_changed, fetch_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project, url) DO UPDATE SET
          last_seen=excluded.last_seen,
          lastmod=COALESCE(excluded.lastmod, pages.lastmod),
          content_hash=COALESCE(excluded.content_hash, pages.content_hash),
          title=COALESCE(excluded.title, pages.title),
          description=COALESCE(excluded.description, pages.description),
          last_status=excluded.last_status,
          last_changed=excluded.last_changed,
          fetch_count=pages.fetch_count + excluded.fetch_count
        """,
        (project, url, now_s, now_s, lastmod, digest, title, desc, status,
         changed_at, 1 if fetched else 0),
    )
    conn.commit()


def _finish_crawl_run(conn, run_values) -> None:
    conn.execute(
        """
        INSERT INTO crawl_runs (started, finished, project, source, first_run, urls_seen,
                                new_count, changed_count, unchanged_count, fetches,
                                robots_blocked, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_values["started"], iso(now_kst()), run_values["project"],
         run_values["source"], run_values["first_run"], run_values["urls_seen"],
         run_values["new_count"], run_values["changed_count"],
         run_values["unchanged_count"], run_values["fetches"],
         run_values["robots_blocked"], "; ".join(run_values["notes"])[:2000]),
    )
    conn.commit()


# --------------------------------------------------------------------------
# 2. discover (Reddit public JSON + optional per-project RSS feeds)
# --------------------------------------------------------------------------

REDDIT_SEARCH = "https://www.reddit.com/search.json"


def _reddit_blocked(keep_global) -> bool:
    """True when robots or an HTTP 403/429 already stopped Reddit this run."""
    return bool({"REDDIT_ROBOTS", "REDDIT_HTTP_403", "REDDIT_HTTP_429"} & set(keep_global))


def run_discovery(conn: sqlite3.Connection, fetcher: Fetcher, projects, cfg: dict,
                  now: datetime) -> None:
    limits = cfg["limits"]
    now_s = iso(now)
    reddit_sleep = float(limits.get("reddit_min_sleep", 6.0))
    cap_requests = int(limits.get("max_reddit_requests_per_run", 40))
    sample_url = REDDIT_SEARCH + "?" + urllib.parse.urlencode({"q": "test"})
    robots_ok = fetcher.can_fetch(sample_url)
    reddit_requests = 0
    keep_global = set()

    if not robots_ok:
        blocker_upsert(conn, "*", "REDDIT_ROBOTS",
                       "www.reddit.com/robots.txt disallows this agent "
                       "(User-agent: * Disallow: /) - public search.json skipped",
                       now_s)
        keep_global.add("REDDIT_ROBOTS")
    else:
        conn.execute("UPDATE blockers SET active=0 WHERE project='*' AND code='REDDIT_ROBOTS'")
        conn.commit()

    for proj in projects:
        found_total = 0
        if robots_ok and proj.keywords and not _reddit_blocked(keep_global):
            for keyword in proj.keywords:
                if reddit_requests >= cap_requests:
                    blocker_upsert(conn, "*", "REDDIT_CAP",
                                   f"per-run Reddit request cap reached ({cap_requests})",
                                   now_s)
                    keep_global.add("REDDIT_CAP")
                    break
                query = urllib.parse.urlencode(
                    {"q": keyword, "sort": "new", "t": "week", "limit": 25})
                url = f"{REDDIT_SEARCH}?{query}"
                if not fetcher.can_fetch(url):
                    blocker_upsert(conn, "*", "REDDIT_ROBOTS",
                                   "robots.txt disallows reddit search.json", now_s)
                    keep_global.add("REDDIT_ROBOTS")
                    break
                res = fetcher.get(url, accept=ACCEPT_JSON,
                                  sleep=random.uniform(reddit_sleep, reddit_sleep + 3.0))
                reddit_requests += 1
                if res.status in (403, 429):
                    blocker_upsert(conn, "*", f"REDDIT_HTTP_{res.status}",
                                   f"reddit search.json returned HTTP {res.status} for "
                                   f"'{keyword}' - no auth retry (public access only)",
                                   now_s)
                    keep_global.add(f"REDDIT_HTTP_{res.status}")
                    log.warning("[discover] reddit HTTP %s for %r - stopping all reddit "
                                "requests this run", res.status, keyword)
                    break
                if not res.ok or not res.body:
                    log.info("[discover:%s] reddit unavailable (%s) for %r",
                             proj.name, res.status or res.error, keyword)
                    blocker_upsert(conn, "*", "REDDIT_UNAVAILABLE",
                                   f"reddit search.json error ({res.status or res.error})",
                                   now_s)
                    keep_global.add("REDDIT_UNAVAILABLE")
                    continue
                found = _store_reddit_candidates(conn, proj.name, keyword, res.body, now_s)
                found_total += found
                log.info("[discover:%s] %-28s -> %2d thread(s)",
                         proj.name, keyword, found)

        for feed in proj.rss_feeds:
            feed_url = normalize_url(feed)
            if not feed_url:
                continue
            res = fetcher.get(feed_url, accept=ACCEPT_XML)
            if res.ok and res.body:
                entries = parse_feed_body(res.body)
                stored = 0
                for link, lastmod in entries[:100]:
                    if _store_candidate(conn, proj.name, "", "rss", link, None, None,
                                        None, None, None, now_s):
                        stored += 1
                found_total += stored
                log.info("[discover:%s] rss %s -> %d new", proj.name, feed, stored)

        log.info("[discover:%s] candidates this run: %d (reddit requests so far: %d)",
                 proj.name, found_total, reddit_requests)

    if "REDDIT_CAP" not in keep_global:
        conn.execute("UPDATE blockers SET active=0 WHERE project='*' AND code='REDDIT_CAP'")
    if "REDDIT_UNAVAILABLE" not in keep_global:
        conn.execute("UPDATE blockers SET active=0 WHERE project='*' AND code='REDDIT_UNAVAILABLE'")
    for code in ("REDDIT_HTTP_403", "REDDIT_HTTP_429"):
        if code not in keep_global:
            conn.execute("UPDATE blockers SET active=0 WHERE project='*' AND code=?",
                         (code,))
    conn.commit()


def _store_reddit_candidates(conn, project, keyword, body, now_s) -> int:
    """Store thread metadata only -- never authors or any personal data."""
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        log.warning("[discover] reddit returned non-JSON body for %r", keyword)
        return 0
    children = ((payload or {}).get("data") or {}).get("children") or []
    stored = 0
    for child in children:
        data = (child or {}).get("data") or {}
        permalink = data.get("permalink")
        if not permalink:
            continue
        url = "https://www.reddit.com" + permalink
        stored += int(_store_candidate(
            conn, project, keyword, "reddit", url,
            data.get("title"), data.get("subreddit"),
            data.get("created_utc"), data.get("score"), data.get("num_comments"),
            now_s, stickied=bool(data.get("stickied"))))
    return stored


def _store_candidate(conn, project, keyword, source, url, title, subreddit,
                     created_utc, score, num_comments, now_s, stickied=False) -> bool:
    url = normalize_url(url)
    if not url or stickied:
        return False
    created = None
    if created_utc:
        try:
            created = datetime.fromtimestamp(float(created_utc), KST).isoformat(timespec="seconds") \
                if KST else datetime.fromtimestamp(float(created_utc)).isoformat(timespec="seconds")
        except Exception:
            created = None
    cur = conn.execute(
        """
        INSERT INTO candidates (project, keyword, source, url, title, subreddit,
                                created_utc, score, num_comments, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project, source, url) DO UPDATE SET
          score=excluded.score,
          num_comments=excluded.num_comments,
          last_seen=excluded.last_seen,
          title=COALESCE(excluded.title, candidates.title)
        """,
        (project, keyword, source, url, clean_ws(title)[:300] if title else None,
         subreddit, created, _int_or_none(score), _int_or_none(num_comments),
         now_s, now_s),
    )
    conn.commit()
    return cur.rowcount == 1


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 3. drafts (template only -- no LLM, nothing is ever posted)
# --------------------------------------------------------------------------

DRAFT_HEADER = "# DRAFT - VIRAL compliance check required before posting"


def generate_drafts(conn: sqlite3.Connection, cfg: dict, projects, now: datetime,
                    run_started_iso: str) -> None:
    drafts_dir = abs_path(cfg, "drafts")
    campaign = cfg.get("campaign", "viral_w1")
    first_run_pages = int(cfg["limits"].get("first_run_draft_pages", 5))

    for proj in projects:
        run = conn.execute(
            "SELECT * FROM crawl_runs WHERE project=? ORDER BY id DESC LIMIT 1",
            (proj.name,)).fetchone()
        if run is None:
            continue
        pages = conn.execute(
            """
            SELECT * FROM pages
            WHERE project=? AND last_status IN ('new','changed')
              AND last_changed >= ?
            ORDER BY COALESCE(lastmod, last_changed) DESC
            """,
            (proj.name, run_started_iso)).fetchall()
        if not pages:
            log.info("[draft:%s] no new/changed pages this run", proj.name)
            continue
        if run["first_run"]:
            pages = pages[:first_run_pages]
            log.info("[draft:%s] first run -> limited to %d newest page(s)",
                     proj.name, len(pages))
        os.makedirs(os.path.join(drafts_dir, proj.name), exist_ok=True)
        for page in pages:
            packet = render_packet(proj, page, cfg, campaign, now)
            date_s = kst(now).strftime("%Y%m%d")
            slug = slugify(page["title"] or page["url"]) or "page"
            fname = f"{date_s}_{slug}-{url_hash(page['url'])}.md"
            path = os.path.join(drafts_dir, proj.name, fname)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(packet)
            conn.execute(
                """
                INSERT INTO drafts (project, page_url, path, channels, page_status, created)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project, page_url) DO UPDATE SET
                  path=excluded.path, channels=excluded.channels,
                  page_status=excluded.page_status, created=excluded.created
                """,
                (proj.name, page["url"], path, ",".join(cfg.get("channels", [])),
                 page["last_status"], iso(now)))
            conn.commit()
            log.info("[draft:%s] wrote %s (%s)", proj.name, os.path.basename(path),
                     page["last_status"])


def render_packet(proj: Project, page, cfg: dict, campaign: str, now: datetime) -> str:
    url = page["url"]
    title = clean_ws(page["title"]) or _title_from_url(url)
    desc = clean_ws(page["description"]) or proj.tagline_en
    tags = " ".join(proj.hashtags[:5]) if proj.hashtags else ""
    links = {ch: add_utm(url, ch, campaign) for ch in cfg.get("channels", [])}
    gen = fmt_kst(now)
    status_line = page["last_status"]

    # --- x (<= 280 chars) -------------------------------------------------
    x_head = f"{title}"
    x_link = links.get("x", url)
    x_tail = f"\n{x_link}\n{tags}".rstrip()
    x_desc = clip(desc, max(0, 280 - len(x_head) - len(x_tail) - 1))
    x_text = "\n".join(part for part in (x_head, x_desc, x_link, tags) if part)

    # --- threads ----------------------------------------------------------
    threads_text = (
        f"{title}\n\n"
        f"{clip(desc, 220)}\n\n"
        f"{proj.tagline_ko + chr(10) + chr(10) if proj.tagline_ko else ''}"
        f"full notes: {links.get('threads', url)}\n{tags}"
    ).strip()

    # --- instagram --------------------------------------------------------
    insta_text = (
        f"{title}\n\n"
        f"{clip(desc, 400)}\n\n"
        f"{proj.tagline_ko or proj.tagline_en}\n\n"
        f"link in bio -> {links.get('instagram', url)}\n\n"
        f"{tags}"
    ).strip()

    # --- reddit (informational, NO LINK) ----------------------------------
    reddit_text = (
        f"informational draft - no link, no self-promotion\n\n"
        f"Put together what I could verify on this: {title}. {clip(desc, 240)}\n\n"
        f"(Only mention the source page if someone asks for it in the thread; "
        f"do not drop links unprompted.)"
    ).strip()

    lines = [
        DRAFT_HEADER,
        "",
        "> Generated from templates by autobot.py (no LLM). Nothing here has been posted,",
        "> submitted, messaged or scheduled by the tool. A human MUST review for platform",
        "> rules, factual accuracy and self-promotion policy before anything is published.",
        "",
        f"- project: {proj.name} ({proj.url or 'no public url'})",
        f"- page: {url}",
        f"- page title: {title}",
        f"- page description: {desc}",
        f"- page status: {status_line} (first seen {page['first_seen']}, lastmod {page['lastmod'] or 'n/a'})",
        f"- generated: {gen} | utm campaign: {campaign}",
        f"- keywords: {', '.join(proj.keywords)}",
        "",
        "## x",
        "",
        f"post text ({len(x_text)} chars):",
        "",
        "```text",
        x_text,
        "```",
        "",
        f"link (utm): {links.get('x', url)}",
        "",
        "## threads",
        "",
        "post text:",
        "",
        "```text",
        threads_text,
        "```",
        "",
        f"link (utm): {links.get('threads', url)}",
        "",
        "## instagram",
        "",
        "caption:",
        "",
        "```text",
        insta_text,
        "```",
        "",
        f"link for bio / link-in-bio tool (utm): {links.get('instagram', url)}",
        "",
        "## reddit",
        "",
        "NO LINK draft (informational comment/post, Reddit self-promo rules first):",
        "",
        "```text",
        reddit_text,
        "```",
        "",
        "---",
        "compliance checklist before posting:",
        "- [ ] facts in the page still correct (price, dates, rules)",
        "- [ ] platform rules checked (Reddit self-promo / X / Threads / IG)",
        "- [ ] Korean + English tone matches the project account",
        "- [ ] link + UTM correct, no tracking on third-party sites",
        "- [ ] no personal data, no promises, no paid placement",
    ]
    return "\n".join(lines) + "\n"


def _title_from_url(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1] if path else urllib.parse.urlsplit(url).netloc
    return slugify(tail).replace("-", " ").title() or url


# --------------------------------------------------------------------------
# 4. metrics: rounds.log -> published entries -> 48h / 7d checks
# --------------------------------------------------------------------------

ABS_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
BARE_DOMAIN_RE = re.compile(
    r"(?<![\w@.\-/])((?:[a-z][a-z0-9-]*\.)+[a-z]{2,6})(/[a-z0-9\-._~%/?=&#]*)?",
    re.IGNORECASE,
)
REDDIT_SHORT_RE = re.compile(r"\br/([A-Za-z0-9_]{2,30})\s+(\d[a-z0-9]{4,7})\b")
FILE_EXT_TLD = {
    "py", "md", "json", "xml", "html", "htm", "txt", "log", "png", "jpg",
    "jpeg", "gif", "svg", "env", "db", "sh", "csv", "yml", "yaml", "js",
    "css", "pdf", "zip", "gz", "tar", "sql", "conf", "toml", "ini",
}
ROUND_HEADER_RE = re.compile(
    r"===\s*Round\s+(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", re.I)
FULL_DT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?")
MD_HM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\b")
MD_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
HM_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
HINT_48H_RE = re.compile(r"\b48\s*h\b[^0-9\n]{0,24}(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", re.I)
HINT_7D_RE = re.compile(r"\b7\s*d\b[^0-9\n]{0,24}(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", re.I)

ACTION_RE = re.compile(
    r"\b(submitted|submitting|posted|publish(ed)?|approved|listed|live\b|launched|"
    r"등록|제출|게시|승인)\b", re.IGNORECASE)
NEGATIVE_RE = re.compile(
    r"no\s+new|not\s+(?:posted|submitted|published|approved|ours)|already\s+"
    r"(?:answered|posted|submitted|listed)|\bskip\b|\bskipped\b|excluded|stale|"
    r"dropped|do\s+not\s+(?:submit|post)|future\s+repl|possible\s+(?:future|outlet)|"
    r"next\s+round|needs?\s+(?:approval|account|sign-?up|confirmation)|unverified|"
    r"not\s+verified|research\s+only|nothing\s+submitted|will\s+(?:submit|post)|"
    r"to\s+check\s*:|check\s+before|candidate", re.IGNORECASE)
CITATION_RE = re.compile(
    r"confirmed\s+on|fact\s+source|cf\.|source\s*:", re.IGNORECASE)


def _strip_trailing_punct(url: str) -> str:
    return url.rstrip(".,;:!?)}]>\"'")


def extract_urls(text: str):
    """Absolute URLs, bare domains-with-path, and r/<sub> <id> Reddit shorthands."""
    spans, found = [], []
    for match in ABS_URL_RE.finditer(text):
        url = _strip_trailing_punct(match.group(0))
        if url.lower().startswith(("http://", "https://")):
            found.append(url)
            spans.append(match.span())
    for match in BARE_DOMAIN_RE.finditer(text):
        start = match.start()
        if any(s <= start < e for s, e in spans):
            continue
        domain, path = match.group(1), match.group(2) or ""
        if domain.rsplit(".", 1)[-1].lower() in FILE_EXT_TLD:
            continue
        if all(part.isdigit() for part in domain.split(".")):
            continue
        found.append(f"https://{domain.lower()}{path}")
    for match in REDDIT_SHORT_RE.finditer(text):
        sub, thread_id = match.group(1), match.group(2)
        found.append(f"https://www.reddit.com/r/{sub}/comments/{thread_id}/")
    # de-duplicate, keep order
    seen, ordered = set(), []
    for url in found:
        norm = normalize_url(url)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        ordered.append(norm)
    return ordered


def extract_line_datetime(text: str, round_dt):
    """Return the most action-relevant timestamp on a line (KST datetime)."""
    candidates = []  # (quality, position, datetime)
    for m in FULL_DT_RE.finditer(text):
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                          int(m.group(4)), int(m.group(5)),
                          int(m.group(6) or 0))
        except ValueError:
            continue
        candidates.append((4, m.start(), dt))
    year = round_dt.year if round_dt else now_kst().year
    for m in MD_HM_RE.finditer(text):
        try:
            dt = datetime(year, int(m.group(1)), int(m.group(2)),
                          int(m.group(3)), int(m.group(4)))
        except ValueError:
            continue
        candidates.append((3, m.start(), dt))
    if not any(q >= 3 for q, _, _ in candidates):
        for m in HM_RE.finditer(text):
            try:
                base = round_dt or now_kst().replace(minute=0, second=0, microsecond=0)
                dt = base.replace(hour=int(m.group(1)), minute=int(m.group(2)))
            except ValueError:
                continue
            candidates.append((2, m.start(), dt))
    if not candidates:
        return None
    action = ACTION_RE.search(text)
    anchor = action.start() if action else 0
    best = max(candidates, key=lambda c: (c[0], -abs(c[1] - anchor)))
    return kst(best[2]) if KST else best[2]


def extract_due_hints(text: str, round_dt, published_dt):
    """Pick up explicit '48h check 10/23' / '7d 10/28' notes when present."""
    year = (round_dt or published_dt or now_kst()).year
    hints = {}
    for key, regex in (("48h", HINT_48H_RE), ("7d", HINT_7D_RE)):
        m = regex.search(text)
        if not m:
            continue
        hour, minute = 9, 0
        if m.group(3) and m.group(4):
            hour, minute = int(m.group(3)), int(m.group(4))
        elif published_dt is not None:
            hour, minute = published_dt.hour, published_dt.minute
        try:
            hints[key] = kst(datetime(year, int(m.group(1)), int(m.group(2)), hour, minute))
        except ValueError:
            continue
    return hints


def map_project(text: str, url, projects):
    """Attribute a rounds.log line to a project by domain / name / keyword terms."""
    low = (text or "").lower()
    url_host = urllib.parse.urlsplit(url).netloc.lower() if url else ""
    url_path = (urllib.parse.urlsplit(url).path or "").lower() if url else ""
    scores = {}
    for proj in projects:
        score, basis = 0, []
        for domain in proj.domains + ([proj.host] if proj.host else []):
            if not domain:
                continue
            if url_host == domain or url_host.endswith("." + domain):
                score += 100
                basis.append(f"domain:{domain}")
                break
        for name in proj.names + [proj.name]:
            if name and name.lower() in low:
                score += 50
                basis.append(f"name:{name}")
                break
        for term in proj.terms:
            if not term:
                continue
            if re.search(r"(?<![0-9a-z])" + re.escape(term) + r"(?![0-9a-z])", low):
                score += 10
                basis.append(f"term:{term}")
        for keyword in proj.keywords:
            if keyword.lower() in low:
                score += 8
                basis.append(f"kw:{keyword}")
        if url_path and any(re.search(re.escape(t), url_path) for t in proj.terms):
            score += 4
        scores[proj.name] = (score, basis)
    ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
    if not ranked or ranked[0][1][0] < 10:
        return None, "no-match"
    if len(ranked) > 1 and ranked[0][1][0] == ranked[1][1][0]:
        return None, "ambiguous"
    return ranked[0][0], ";".join(ranked[0][1][1][:4])


def parse_rounds_log(text: str, projects):
    """Free-text parser: (line_no, url|None, label, published_at, hints, raw_line)."""
    entries = []
    round_dt = None
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        header = ROUND_HEADER_RE.search(line)
        if header:
            try:
                round_dt = datetime(int(header.group(1)), int(header.group(2)),
                                    int(header.group(3)), int(header.group(4)),
                                    int(header.group(5)))
                round_dt = kst(round_dt) if KST else round_dt
            except ValueError:
                round_dt = None
            continue
        if not ACTION_RE.search(line) or NEGATIVE_RE.search(line) or CITATION_RE.search(line):
            continue
        published_dt = extract_line_datetime(line, round_dt) or round_dt or now_kst()
        hints = extract_due_hints(line, round_dt, published_dt)
        urls = extract_urls(line)
        if urls:
            for url in urls:
                entries.append((line_no, url, None, published_dt, hints, line))
        else:
            label = _line_label(line)
            if label:
                entries.append((line_no, None, label, published_dt, hints, line))
    return entries


def _line_label(line: str) -> str:
    quoted = re.search(r"\"([^\"]{3,80})\"", line)
    if quoted:
        return clean_ws(quoted.group(1))
    prefix = re.sub(r"^[-*]\s*", "", line).split(":", 1)[0]
    prefix = clean_ws(prefix)
    if 2 < len(prefix) <= 60 and not prefix.endswith("."):
        return prefix
    return ""


def process_rounds_log(conn: sqlite3.Connection, cfg: dict, now: datetime) -> int:
    path = cfg["paths"]["rounds_log"]
    now_s = iso(now)
    if not os.path.exists(path):
        blocker_upsert(conn, "*", "ROUNDS_LOG_MISSING",
                       f"rounds log not found: {path}", now_s)
        return 0
    conn.execute("UPDATE blockers SET active=0 WHERE project='*' AND code='ROUNDS_LOG_MISSING'")
    conn.commit()
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    projects = cfg["_projects"]
    entries = parse_rounds_log(text, projects)
    added = 0
    for line_no, url, label, published_dt, hints, line in entries:
        project, basis = map_project(line, url, projects)
        dedupe = hashlib.sha1(
            f"{line_no}|{url or label}".encode("utf-8", "replace")).hexdigest()
        channel = "reddit" if is_reddit_url(url or "") else "web"
        post_id = make_post_id(project, channel, url, label)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO published
              (project, url, label, kind, published_at, source, line_no, raw_line,
               mapping, due_hint_48h, due_hint_7d, dedupe_key, first_seen,
               post_id, channel, state, verified_at)
            VALUES (?, ?, ?, ?, ?, 'rounds.log', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published_verified', ?)
            """,
            (project, url, label, channel,
             iso(published_dt), line_no, line[:1000], basis or "no-match",
             iso(hints["48h"]) if hints.get("48h") else None,
             iso(hints["7d"]) if hints.get("7d") else None,
             dedupe, now_s, post_id, channel, iso(published_dt)))
        if cur.rowcount:
            added += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS n FROM published").fetchone()["n"]
    unmapped = conn.execute(
        "SELECT COUNT(*) AS n FROM published WHERE project IS NULL").fetchone()["n"]
    log.info("[metrics] rounds.log: %d published action line(s) parsed, %d new, "
             "%d total entries (%d unmapped/ambiguous)", len(entries), added, total, unmapped)
    _queue_metric_checks(conn, now)
    return added


def _queue_metric_checks(conn: sqlite3.Connection, now: datetime) -> None:
    """Make sure every published entry has a pending 48h and 7d check.

    Due dates default to verified_at + 48h / +7d; an explicit
    "48h check 10/23" note in rounds.log wins when present.
    """
    rows = conn.execute(
        "SELECT id, verified_at, due_hint_48h, due_hint_7d FROM published "
        "WHERE state IN ('published_verified','measured','evaluated') "
        "AND verified_at IS NOT NULL").fetchall()
    queued = 0
    for row in rows:
        published_dt = parse_iso(row["verified_at"])
        if published_dt is None:
            continue
        published_dt = kst(published_dt)
        for kind, due in (("48h", published_dt + timedelta(hours=48)),
                          ("7d", published_dt + timedelta(days=7))):
            hint = row["due_hint_48h"] if kind == "48h" else row["due_hint_7d"]
            hint_dt = parse_iso(hint) if hint else None
            if hint_dt is not None:
                due = kst(hint_dt)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO metric_checks (published_id, check_kind, due_at)
                VALUES (?, ?, ?)
                """, (row["id"], kind, iso(due)))
            queued += cur.rowcount
    if queued:
        conn.commit()
        log.info("[metrics] queued %d new 48h/7d check(s)", queued)


def run_due_checks(conn: sqlite3.Connection, fetcher: Fetcher, cfg: dict,
                   now: datetime) -> None:
    cap = int(cfg["limits"].get("max_metric_checks_per_run", 40))
    max_attempts = max(1, int(cfg["limits"].get("metric_max_attempts", 3)))
    retry_minutes = max(0, float(cfg["limits"].get("metric_retry_minutes", 60)))
    rows = conn.execute(
        """
        SELECT mc.id AS check_id, mc.check_kind, mc.due_at, mc.attempts,
               pub.id AS pub_id, pub.url, pub.label, pub.project
        FROM metric_checks mc
        JOIN published pub ON pub.id = mc.published_id
        WHERE mc.status = 'pending' AND COALESCE(mc.retry_at, mc.due_at) <= ?
        ORDER BY COALESCE(mc.retry_at, mc.due_at)
        LIMIT ?
        """, (iso(now), cap)).fetchall()
    if not rows:
        due_next = conn.execute(
            "SELECT MIN(COALESCE(retry_at, due_at)) AS d FROM metric_checks WHERE status='pending'").fetchone()["d"]
        log.info("[metrics] no checks due (next pending: %s)", due_next or "none")
        return
    log.info("[metrics] %d check(s) due now", len(rows))
    for row in rows:
        result = _check_published(row, fetcher)
        attempts = row["attempts"] + 1
        checked = bool(result.get("checked"))
        status = "done" if checked else ("failed" if attempts >= max_attempts else "pending")
        retry_at = iso(now + timedelta(minutes=retry_minutes * attempts)) if status == "pending" else None
        failure_reason = None if checked else (result.get("reason") or "metric check unconfirmed")
        conn.execute(
            """
            UPDATE metric_checks
            SET status=?,
                attempts = attempts + 1,
                checked_at = ?,
                result = ?, retry_at = ?, failure_reason = ?
            WHERE id = ?
            """, (status, iso(now), json.dumps(result, ensure_ascii=False), retry_at, failure_reason,
                  row["check_id"]))
        conn.commit()
        log.info("[metrics] %s %s %s -> %s", row["project"] or "unmapped",
                 row["check_kind"], row["url"] or row["label"],
                 _short_result(result))


def _check_published(row, fetcher: Fetcher) -> dict:
    url = row["url"]
    base = {"kind": row["check_kind"], "url": url, "label": row["label"]}
    if not url:
        base.update({"checked": True, "result": UNKNOWN,
                     "reason": "no public url in rounds.log"})
        return base
    if is_reddit_url(url):
        if not fetcher.can_fetch(url + ".json"):
            base.update({"checked": True, "result": UNKNOWN,
                         "reason": "robots.txt disallows reddit json"})
            return base
        json_url = url.rstrip("/") + ".json"
        res = fetcher.get(json_url, accept=ACCEPT_JSON,
                          sleep=random.uniform(6.0, 9.0))
        if res.status in (403, 429):
            base.update({"checked": True, "result": UNKNOWN,
                         "reason": f"reddit HTTP {res.status} (public access only)"})
            return base
        if not res.ok:
            base.update({"checked": False, "result": UNKNOWN,
                         "reason": f"fetch error {res.status or res.error}"})
            return base
        try:
            payload = json.loads(res.body.decode("utf-8", "replace"))
            data = payload[0]["data"]["children"][0]["data"]
            base.update({"checked": True, "score": _int_or_none(data.get("score")),
                         "num_comments": _int_or_none(data.get("num_comments"))})
        except Exception:
            base.update({"checked": False, "result": UNKNOWN,
                         "reason": "unexpected reddit json shape"})
        return base
    # non-reddit: HTTP status only
    res = fetcher.get(url, accept=ACCEPT_HTML, max_bytes=4096)
    if res.status is not None:
        base.update({"checked": True, "http_status": res.status})
    else:
        base.update({"checked": False, "result": UNKNOWN,
                     "reason": f"fetch error {res.error}"})
    return base


def _short_result(result: dict) -> str:
    if result.get("score") is not None or result.get("num_comments") is not None:
        return f"score {result.get('score', UNKNOWN)} / comments {result.get('num_comments', UNKNOWN)}"
    if "http_status" in result:
        return f"HTTP {result['http_status']}"
    return f"{result.get('result', UNKNOWN)} ({result.get('reason', '')})"


# --------------------------------------------------------------------------
# 5. status rendering
# --------------------------------------------------------------------------

def collect_status(conn: sqlite3.Connection, cfg: dict, now: datetime) -> dict:
    ensure_config_blockers(conn, cfg, now)  # config blockers always belong in the table
    since_7d = iso(now - timedelta(days=7))
    per_project = {}
    for proj in cfg["_projects"]:
        run = conn.execute(
            "SELECT * FROM crawl_runs WHERE project=? ORDER BY id DESC LIMIT 1",
            (proj.name,)).fetchone()
        new_content = "0"
        if run is not None:
            fresh = (run["new_count"] or 0) + (run["changed_count"] or 0)
            new_content = (f"{fresh} (new {run['new_count']}, changed "
                           f"{run['changed_count']})") if fresh else "0"
        cand_total = conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE project=?",
            (proj.name,)).fetchone()["n"]
        cand_7d = conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE project=? AND last_seen >= ?",
            (proj.name, since_7d)).fetchone()["n"]
        drafts_n = conn.execute(
            "SELECT COUNT(*) AS n FROM drafts WHERE project=?",
            (proj.name,)).fetchone()["n"]
        pub_n = conn.execute(
            "SELECT COUNT(*) AS n FROM published WHERE project=?",
            (proj.name,)).fetchone()["n"]
        blockers = [f"{r['code']}: {r['detail']}" for r in conn.execute(
            "SELECT code, detail FROM blockers WHERE project=? AND active=1 "
            "ORDER BY code", (proj.name,)).fetchall()]
        per_project[proj.name] = {
            "run": run, "new_content": new_content,
            "candidates": cand_total, "candidates_7d": cand_7d,
            "drafts": drafts_n, "published": pub_n,
            "metrics": _metrics_cell(conn, proj.name),
            "blockers": blockers,
            "blocker_codes": [b.split(":", 1)[0] for b in blockers],
        }
    global_blockers = [f"{r['code']}: {r['detail']}" for r in conn.execute(
        "SELECT code, detail FROM blockers WHERE project='*' AND active=1 "
        "ORDER BY code").fetchall()]
    unmapped = conn.execute(
        "SELECT COUNT(*) AS n FROM published WHERE project IS NULL").fetchone()["n"]
    next_due = conn.execute(
        "SELECT MIN(COALESCE(retry_at, due_at)) AS d FROM metric_checks WHERE status='pending'").fetchone()["d"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM metric_checks WHERE status='pending'").fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM metric_checks WHERE status='failed'").fetchone()["n"]
    return {"projects": per_project, "global_blockers": global_blockers,
            "unmapped_published": unmapped, "next_due": next_due,
            "pending_checks": pending, "failed_checks": failed}


def _metrics_cell(conn: sqlite3.Connection, project: str) -> str:
    rows = conn.execute(
        """
        SELECT mc.check_kind, mc.status, mc.result
        FROM metric_checks mc JOIN published pub ON pub.id = mc.published_id
        WHERE pub.project = ?
        """, (project,)).fetchall()
    if not rows:
        return "-"  # nothing published for this project yet -> nothing to check
    parts, unknown = [], 0
    for kind in ("48h", "7d"):
        subset = [r for r in rows if r["check_kind"] == kind]
        if not subset:
            continue
        done = sum(1 for r in subset if r["status"] == "done")
        parts.append(f"{kind} {done}/{len(subset)}")
    for row in rows:
        try:
            payload = json.loads(row["result"]) if row["result"] else {}
        except Exception:
            payload = {}
        if payload.get("result") == UNKNOWN or payload.get("reason"):
            unknown += 1
    text = " · ".join(parts)
    if unknown:
        text += f" · {UNKNOWN} {unknown}"
    failed = sum(1 for r in rows if r["status"] == "failed")
    if failed:
        text += f" · failed {failed}"
    return text or UNKNOWN


def render_status_md(status: dict, cfg: dict, now: datetime, mode: str) -> str:
    gen = fmt_kst(now)
    lines = [
        "# Soulbound viral autobot — status",
        "",
        f"generated: {gen} | mode: `{mode}` | db: `{cfg['paths']['db']}`",
        "",
        "| project | new content | candidates found | drafts ready | published (rounds.log) | metrics | blockers |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, row in status["projects"].items():
        cand = f"{row['candidates']}" if not row["candidates_7d"] else \
            f"{row['candidates']} (7d {row['candidates_7d']})"
        lines.append(
            f"| {name} | {row['new_content']} | {cand} | {row['drafts']} | "
            f"{row['published']} | {row['metrics']} | "
            f"{'; '.join(row['blockers']) if row['blockers'] else '없음'} |")
    lines += [
        "",
        "## notes",
        "",
        f"- new content: latest crawl run per project (sitemap/RSS, capped at "
        f"{cfg['limits']['max_urls_per_site']} urls and "
        f"{cfg['limits']['max_page_fetches_per_site']} page fetches per site per run)",
        f"- published (rounds.log): parsed from `{cfg['paths']['rounds_log']}` "
        f"(free text; URLs + timestamps, mapped to projects by domain/keywords)",
        f"- metric checks pending: {status['pending_checks']}"
        + (f", next due {fmt_kst(parse_iso(status['next_due']))}" if status["next_due"] else ""),
        f"- published entries not attributable to a project: {status['unmapped_published']}",
        f"- global blockers: {'; '.join(status['global_blockers']) if status['global_blockers'] else '없음'}",
        "",
        "rules honoured by this bot: robots.txt, 2-5 s random request sleep, "
        "≥6 s on Reddit, no login/auth, no posting, no personal data, "
        f"unknown metrics reported as {UNKNOWN}.",
        "",
    ]
    return "\n".join(lines)


def render_status_slack(status: dict, cfg: dict, now: datetime) -> str:
    gen = fmt_kst(now)
    lines = [f"*viral autobot status* — {gen}", ""]
    for name, row in status["projects"].items():
        lines.append(
            f"• *{name}* — new {row['new_content']} · candidates {row['candidates']}"
            f" (7d {row['candidates_7d']}) · drafts {row['drafts']} · "
            f"published {row['published']} · metrics {row['metrics']} · "
            f"blockers {'; '.join(row['blocker_codes']) if row['blocker_codes'] else '없음'}")
    lines += [
        "",
        f"pending metric checks: {status['pending_checks']}"
        + (f" (next {fmt_kst(parse_iso(status['next_due']))})" if status["next_due"] else ""),
    ]
    if status["global_blockers"]:
        lines.append(f"global blockers: {'; '.join(status['global_blockers'])}")
    lines += [
        f"unmapped published entries: {status['unmapped_published']}",
        "_drafts only — no posting, no login, no personal data; unknown = 미확인_",
    ]
    return "\n".join(lines) + "\n"


def write_status(conn: sqlite3.Connection, cfg: dict, now: datetime, mode: str) -> dict:
    status = collect_status(conn, cfg, now)  # also applies config-level blockers
    md_path = cfg["paths"]["status_md"]
    slack_path = abs_path(cfg, "status_slack")
    os.makedirs(os.path.dirname(slack_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(md_path) or ".", exist_ok=True)
    md = render_status_md(status, cfg, now, mode)
    slack = render_status_slack(status, cfg, now)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(slack_path, "w", encoding="utf-8") as fh:
        fh.write(slack)
    log.info("[status] wrote %s and %s", md_path, slack_path)
    print("\n" + md)
    return status


# --------------------------------------------------------------------------
# 6. slack (optional, only with --post / status subcommand)
# --------------------------------------------------------------------------

def load_env_file(path: str) -> dict:
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def post_slack(cfg: dict, text: str) -> bool:
    env_path = os.path.expanduser("~/.secrets/viral_autobot.env")
    env = load_env_file(env_path)
    webhook = env.get("SLACK_WEBHOOK_URL", "").strip()
    secrets = [v for v in env.values() if len(v) > 8]
    if not webhook:
        log.info("webhook missing, skipped (no SLACK_WEBHOOK_URL in %s)", env_path)
        return False
    payload = json.dumps({"text": text[:3900]}).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info("[slack] posted status (%d chars, HTTP %s, %.1fs)",
                     len(text), resp.status, time.time() - started)
            return True
    except Exception as exc:
        # never log the webhook or anything derived from it
        message = type(exc).__name__
        log.error("[slack] post failed: %s", _scrub(message, secrets))
        return False


def _scrub(message: str, secrets) -> str:
    for secret in secrets or []:
        if secret and secret in message:
            message = message.replace(secret, "***")
    return message


# --------------------------------------------------------------------------
# CLI / pipeline
# --------------------------------------------------------------------------

def pick_projects(cfg: dict, names):
    if not names:
        return list(cfg["_projects"])
    known = {p.name: p for p in cfg["_projects"]}
    picked = []
    for name in names:
        if name not in known:
            raise SystemExit(f"unknown project: {name} (known: {', '.join(known)})")
        picked.append(known[name])
    return picked


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg, args.verbose)
    projects = pick_projects(cfg, args.project)
    conn = db_connect(cfg)
    fetcher = Fetcher(cfg)
    started = now_kst()
    log.info("run start %s | projects: %s | ua: %s", fmt_kst(started),
             ", ".join(p.name for p in projects), cfg["user_agent"])
    run_started_iso = iso(started)

    if args.no_crawl:
        log.info("crawl skipped (--no-crawl)")
    else:
        for proj in projects:
            try:
                crawl_project(conn, fetcher, proj, cfg, now_kst(),
                              max_urls=args.max_urls, max_fetches=args.max_fetches)
            except Exception as exc:
                log.exception("[crawl:%s] failed: %s", proj.name, type(exc).__name__)
                blocker_upsert(conn, proj.name, "CRAWL_ERROR",
                               f"{type(exc).__name__}: {exc}"[:500], iso(now_kst()))

    if args.no_discover:
        log.info("discovery skipped (--no-discover)")
    else:
        try:
            run_discovery(conn, fetcher, projects, cfg, now_kst())
        except Exception as exc:
            log.exception("[discover] failed: %s", type(exc).__name__)

    if args.no_drafts:
        log.info("drafts skipped (--no-drafts)")
    else:
        try:
            generate_drafts(conn, cfg, projects, now_kst(), run_started_iso)
        except Exception as exc:
            log.exception("[draft] failed: %s", type(exc).__name__)

    if args.no_metrics:
        log.info("metrics skipped (--no-metrics)")
    else:
        try:
            process_rounds_log(conn, cfg, now_kst())
            run_due_checks(conn, fetcher, cfg, now_kst())
        except Exception as exc:
            log.exception("[metrics] failed: %s", type(exc).__name__)

    write_status(conn, cfg, now_kst(), mode="run")
    if args.post:
        slack_text = open(abs_path(cfg, "status_slack"), encoding="utf-8").read()
        post_slack(cfg, slack_text)
    else:
        log.info("slack not requested (use --post to send status_slack.txt)")
    meta_set(conn, "last_run_at", iso(now_kst()))
    log.info("run finished in %.1fs | %d http request(s), %d error(s)",
             (now_kst() - started).total_seconds(), fetcher.requests, fetcher.errors)
    conn.close()
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    setup_logging(cfg, args.verbose)
    conn = db_connect(cfg)
    fetcher = Fetcher(cfg)
    started = now_kst()
    log.info("status start %s (crawl/discover/draft not run)", fmt_kst(started))
    process_rounds_log(conn, cfg, now_kst())
    run_due_checks(conn, fetcher, cfg, now_kst())
    write_status(conn, cfg, now_kst(), mode="status")
    if args.post:
        slack_text = open(abs_path(cfg, "status_slack"), encoding="utf-8").read()
        post_slack(cfg, slack_text)
    else:
        log.info("slack not requested (use --post to send status_slack.txt)")
    meta_set(conn, "last_status_at", iso(now_kst()))
    conn.close()
    return 0


def cmd_daemon(args) -> int:
    import daemon as daemon_mod
    return daemon_mod.main(["--config", args.config, args.action])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autobot.py",
        description="Soulbound viral autobot - read-only crawl/discover/draft/metrics "
                    "scout. Never posts, logs in, or stores personal data.",
        epilog="examples: python3 autobot.py run --project hanset --max-fetches 5 ; "
               "python3 autobot.py status --post")
    parser.add_argument("--config", default=CONFIG_PATH,
                        help="path to config.json (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="crawl + discover + draft + metrics + status")
    run.add_argument("--project", action="append", default=None,
                     help="restrict to a project (repeatable)")
    run.add_argument("--post", action="store_true",
                     help="post out/status_slack.txt to Slack if a webhook is configured")
    run.add_argument("--no-crawl", action="store_true", help="skip the crawl step")
    run.add_argument("--no-discover", action="store_true", help="skip discovery")
    run.add_argument("--no-drafts", action="store_true", help="skip draft generation")
    run.add_argument("--no-metrics", action="store_true", help="skip rounds.log metrics")
    run.add_argument("--max-urls", type=int, default=None,
                     help="override the per-site URL cap for this run")
    run.add_argument("--max-fetches", type=int, default=None,
                     help="override the per-site page-fetch cap for this run")
    run.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="status only (rounds.log metrics + status files)")
    st.add_argument("--post", action="store_true",
                    help="post out/status_slack.txt to Slack if a webhook is configured")
    st.set_defaults(func=cmd_status)

    dm = sub.add_parser("daemon", help="7-stage dev & blueprint-audit daemon (start/stop/restart/status/stages)")
    dm.add_argument("action", nargs="?", default="status",
                    choices=["start", "stop", "restart", "status", "stages", "seed", "loop"],
                    help="daemon action (default: status)")
    dm.set_defaults(func=cmd_daemon)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
