#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for autobot.py.

No production site is contacted: the crawl path is exercised against a local
http.server that serves robots.txt, a sitemap index + sitemap, an RSS feed and
two HTML pages. Everything else (url/utm handling, rounds.log parsing, project
mapping, drafts, status rendering) is pure function testing.

Run:  python3 -I tests/test_offline.py
"""

import json
import contextlib
import io
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import autobot as ab  # noqa: E402

FAILURES = []
PASSES = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


# ---------------------------------------------------------------- helpers
def make_cfg(tmp_dir, base_url=None, rounds_text=None, no_url=(), **over):
    os.makedirs(tmp_dir, exist_ok=True)
    cfg = ab.load_config(os.path.join(ROOT, "config.json"))
    cfg["paths"]["db"] = os.path.join(tmp_dir, "autobot.db")
    cfg["paths"]["rules"] = os.path.join(tmp_dir, "rules.json")
    cfg["paths"]["logs"] = os.path.join(tmp_dir, "logs")
    cfg["paths"]["drafts"] = os.path.join(tmp_dir, "out", "drafts")
    cfg["paths"]["status_slack"] = os.path.join(tmp_dir, "out", "status_slack.txt")
    cfg["paths"]["status_md"] = os.path.join(tmp_dir, "status.md")
    cfg["paths"]["rounds_log"] = os.path.join(tmp_dir, "rounds.log")
    cfg["limits"]["request_sleep"] = [0.0, 0.0]
    cfg["limits"]["reddit_min_sleep"] = 0.0
    cfg["limits"]["http_timeout"] = 5
    for key, value in over.items():
        cfg["limits"][key] = value
    if rounds_text is not None:
        with open(cfg["paths"]["rounds_log"], "w", encoding="utf-8") as fh:
            fh.write(rounds_text)
    if base_url:
        for proj in cfg["_projects"]:
            if proj.name == "hanset":
                proj.url = base_url
                proj.domains = [urllib.parse.urlsplit(base_url).netloc]
    for proj in cfg["_projects"]:
        if proj.name in no_url:
            proj.url = None
            proj.domains = []
    return cfg


# ---------------------------------------------------------------- helpers
def code_block(packet: str, channel: str) -> str:
    """Return the fenced text block of one channel section of a draft packet."""
    section = packet.split(f"## {channel}", 1)[1]
    return section.split("```text", 1)[1].split("```", 1)[0].strip()


# ---------------------------------------------------------------- pure tests
def test_utm():
    base = "https://hanset.soulbound.co.kr/guides/a/?utm_source=x&utm_content=keep&x=1"
    out = ab.add_utm(base, "threads", "viral_w1")
    parts = urllib.parse.urlsplit(out)
    q = dict(urllib.parse.parse_qsl(parts.query))
    check("utm merge keeps existing params", q.get("x") == "1" and q.get("utm_content") == "keep", str(q))
    check("utm merge overrides source", q.get("utm_source") == "threads", str(q))
    check("utm medium/campaign", q.get("utm_medium") == "social" and q.get("utm_campaign") == "viral_w1", str(q))
    check("utm once only", out.count("utm_source") == 1, out)
    plain = ab.add_utm("https://example.com/a", "x", "viral_w1")
    check("utm added without query", "utm_source=x" in plain and "utm_medium=social" in plain, plain)


def test_urls():
    check("normalize drops fragment", ab.normalize_url("https://a.b/c#z") == "https://a.b/c")
    check("normalize keeps query", ab.normalize_url("https://a.b/c?x=1") == "https://a.b/c?x=1")
    check("normalize relative", ab.normalize_url("/p", "https://a.b/x/") == "https://a.b/p")
    check("normalize rejects junk", ab.normalize_url("mailto:x@y.z") == "")
    urls = ab.extract_urls(
        "known: r/crossfit 1wu9e15 (old), see https://Example.com/A/?q=1 and "
        "cfhannam.com/book-online plus newsletter.place/add, file design/ph/logo-512.png")
    check("extract absolute", any(u.lower().startswith("https://example.com/a") for u in urls), str(urls))
    check("extract bare domain w/ path", "https://cfhannam.com/book-online" in urls, str(urls))
    check("extract bare domain no path", "https://newsletter.place/add" in urls, str(urls))
    check("extract reddit shorthand",
          "https://www.reddit.com/r/crossfit/comments/1wu9e15/" in urls, str(urls))
    check("extract skips file-ish", not any("logo-512" in u for u in urls), str(urls))
    check("extract skips plain words", not any("design" in u for u in urls), str(urls))


def test_dates():
    line = "[x] SUBMITTED 12:03 KST, pending review. 48h check 10/23, 7d 10/28."
    dt = ab.extract_line_datetime(line, datetime(2026, 10, 21, 12, 0))
    check("line time", dt is not None and (dt.hour, dt.minute) == (12, 3), str(dt))
    dt2 = ab.extract_line_datetime("approved (email 2026-10-21 06:23 KST). submitted 10/20 16:44",
                                   datetime(2026, 10, 21, 12, 0))
    check("full datetime preferred", dt2 is not None and (dt2.month, dt2.day, dt2.hour) == (10, 21, 6), str(dt2))
    hints = ab.extract_due_hints("48h check due 10/22 16:44, 7d due 10/27.",
                                 datetime(2026, 10, 21, 12, 0), datetime(2026, 10, 20, 16, 44))
    check("hint 48h", hints.get("48h") is not None and hints["48h"].strftime("%m-%d %H:%M") == "10-22 16:44", str(hints))
    check("hint 7d", hints.get("7d") is not None and hints["7d"].strftime("%m-%d") == "10-27", str(hints))


ROUNDS = """=== Round 2026-10-21 12:00 KST (routine test) ===
[discovery]
- WebSearch HANSET: no new threads. Known: r/crossfit 1wu9e15 (already answered, excluded), r/hyrox 1q1fvnu (skip).
- WebSearch pitchboard: no new fresh Reddit threads (r/WritingHub 1re9jx4 is 2026-02, stale). New fact source for future replies: CrossFit Hannam booking page (cfhannam.com/book-online).
[results check]
- InboxReads: "Open Calls by pitchboard" APPROVED and listed (email 2026-10-21 06:23 KST). Submitted 10/20 16:44. 48h check due 10/22 16:44, 7d due 10/27.
[reddit seeding]
- R3-H1/H2/H3 not posted this round: no HANSET confirmation of round3.md wording on record.
[listings]
- GoLetter (directory.goletter.co): SUBMITTED 12:03 KST, pending review. cat Writing & Literature, logo design/ph/logo-512.png. 48h check 10/23, 7d 10/28.
- The Newsletter Place (newsletter.place/add): SUBMITTED 12:03 KST, in review queue. Category Books.
- Next round: BlogHub/SaaSHub/AlternativeTo/Uneed need account sign-up -> approval.
2026-10-21 12:26 KST round3: corrections applied & approved (12:25), confirmed on hyrox.com/event/hyrox-seoul. No links.
"""


def test_rounds_parsing(tmp_dir):
    cfg = make_cfg(tmp_dir, rounds_text=ROUNDS)
    entries = ab.parse_rounds_log(ROUNDS, cfg["_projects"])
    by_url = {(e[1] or f"label:{e[2]}"): e for e in entries}
    check("rounds: goletter captured", "https://directory.goletter.co/" in by_url, str(list(by_url)))
    check("rounds: newsletter place captured", "https://newsletter.place/add" in by_url, str(list(by_url)))
    check("rounds: label entry captured", "label:Open Calls by pitchboard" in by_url, str(list(by_url)))
    check("rounds: negated reddit lines dropped",
          "https://www.reddit.com/r/crossfit/comments/1wu9e15/" not in by_url, str(list(by_url)))
    check("rounds: citation line dropped", not any("hyrox.com" in (k or "") for k in by_url), str(list(by_url)))
    check("rounds: not-posted line dropped", len(entries) == 3, f"{len(entries)} entries: {[e[1] or e[2] for e in entries]}")
    goletter = by_url.get("https://directory.goletter.co/")
    check("rounds: goletter time", goletter and (goletter[3].hour, goletter[3].minute) == (12, 3), str(goletter))

    proj, basis = ab.map_project("- GoLetter (directory.goletter.co): SUBMITTED 12:03 KST, cat Writing & Literature.",
                                 "https://directory.goletter.co/", cfg["_projects"])
    check("mapping: outlet domain -> pitchboard", proj == "pitchboard", f"{proj} {basis}")
    proj2, _ = ab.map_project('InboxReads: "Open Calls by pitchboard" APPROVED and listed.', None, cfg["_projects"])
    check("mapping: name -> pitchboard", proj2 == "pitchboard", str(proj2))
    proj3, _ = ab.map_project("- random site https://example.com/page SUBMITTED today.", "https://example.com/page",
                              cfg["_projects"])
    check("mapping: no match -> None", proj3 is None, str(proj3))

    conn = ab.db_connect(cfg)
    ab.process_rounds_log(conn, cfg, ab.now_kst())
    rows = conn.execute("SELECT project, url, label, published_at, due_hint_48h FROM published").fetchall()
    check("published: 3 rows", len(rows) == 3, str([dict(r) for r in rows]))
    check("published: all pitchboard", all(r["project"] == "pitchboard" for r in rows), str([r["project"] for r in rows]))
    checks = conn.execute("SELECT check_kind, due_at FROM metric_checks ORDER BY check_kind").fetchall()
    check("checks queued 48h+7d per entry", len(checks) == 6, str([dict(c) for c in checks]))
    hint_row = conn.execute(
        "SELECT due_at FROM metric_checks mc JOIN published p ON p.id=mc.published_id "
        "WHERE p.url='https://directory.goletter.co/' AND mc.check_kind='48h'").fetchone()
    check("check uses rounds.log hint", hint_row and hint_row["due_at"].startswith("2026-10-23"),
          str(dict(hint_row) if hint_row else None))
    ab.process_rounds_log(conn, cfg, ab.now_kst())
    again = conn.execute("SELECT COUNT(*) AS n FROM published").fetchone()["n"]
    check("published: idempotent re-parse", again == 3, str(again))
    conn.close()


def test_drafts(tmp_dir):
    cfg = make_cfg(tmp_dir)
    proj = [p for p in cfg["_projects"] if p.name == "hanset"][0]
    page = {"url": "https://hanset.soulbound.co.kr/guides/tattoo-friendly-saunas-korea/?utm_source=x",
            "title": "Tattoo-Friendly Saunas in Korea",
            "description": "Sauna and jjimjilbang tattoo rules, each checked on the venue's official page.",
            "last_status": "new", "first_seen": "2026-10-21T14:00:00+09:00", "lastmod": "2026-10-20"}
    packet = ab.render_packet(proj, page, cfg, "viral_w1", ab.now_kst())
    check("draft header", packet.startswith(ab.DRAFT_HEADER), packet[:80])
    check("draft has all channels",
          all(f"## {ch}" in packet for ch in cfg["channels"]), "")
    check("draft reddit block has no site link",
          "soulbound.co.kr" not in code_block(packet, "reddit"), code_block(packet, "reddit"))
    check("draft utm x", "utm_source=x&utm_medium=social" in code_block(packet, "x"),
          code_block(packet, "x"))
    check("draft utm threads in threads block",
          code_block(packet, "threads").count("utm_source=threads") == 1,
          code_block(packet, "threads"))
    check("draft utm instagram", "utm_source=instagram" in code_block(packet, "instagram"), "")
    check("draft compliance note", "compliance" in packet.lower(), "")
    x_block = code_block(packet, "x")
    check("x text <= 280", len(x_block) <= 280, f"{len(x_block)} chars: {x_block!r}")


# ---------------------------------------------------------------- local crawl
PAGES = {
    "/robots.txt": (
        "text/plain",
        "User-agent: *\nAllow: /\nDisallow: /private/\n"
        "Sitemap: {base}/sitemap_index.xml\nSitemap: {base}/missing-sitemap.xml\n",
    ),
    "/sitemap_index.xml": (
        "application/xml",
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"
        "<sitemap><loc>{base}/posts-sitemap.xml</loc><lastmod>2026-10-21</lastmod></sitemap>"
        "</sitemapindex>",
    ),
    "/posts-sitemap.xml": (
        "application/xml",
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"
        "<url><loc>{base}/guides/saunas</loc><lastmod>2026-10-20T09:00:00+09:00</lastmod></url>"
        "<url><loc>{base}/guides/hyrox</loc><lastmod>2026-10-18</lastmod></url>"
        "<url><loc>{base}/private/hidden</loc><lastmod>2026-10-19</lastmod></url>"
        "<url><loc>{base}/img/logo.png</loc></url>"
        "</urlset>",
    ),
    "/guides/saunas": (
        "text/html; charset=utf-8",
        "<html><head><title>사우나 문신 가이드</title>"
        "<meta name='description' content='Korea sauna tattoo rules, checked per venue.'></head>"
        "<body>content A</body></html>",
    ),
    "/guides/hyrox": (
        "text/html; charset=utf-8",
        "<html><head><title>HYROX Seoul drop-in</title>"
        "<meta property='og:description' content='Drop-in slots and prices for HYROX Seoul.'></head>"
        "<body>content B</body></html>",
    ),
    "/private/hidden": ("text/html", "<html><head><title>hidden</title></head></html>"),
    "/feed": (
        "application/rss+xml",
        '<?xml version="1.0"?><rss version="2.0"><channel><title>fallback</title>'
        "<item><link>{base}/feed-item-1</link><pubDate>Mon, 19 Oct 2026 10:00:00 +0900</pubDate></item>"
        "</channel></rss>",
    ),
}


class Handler(BaseHTTPRequestHandler):
    base = "http://127.0.0.1:1"

    def log_message(self, *args):  # keep test output quiet
        pass

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/slow-robots.txt":
            time.sleep(0.2)
        if path == "/gone-410":
            self.send_response(410)
            self.end_headers()
            return
        entry = PAGES.get(path)
        if entry is None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        ctype, body_tpl = entry
        body = body_tpl.replace("{base}", self.base).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FeedHandler(Handler):
    """A site with no sitemap at all, only /feed (RSS fallback path)."""

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/feed":
            body = (PAGES["/feed"][1].replace("{base}", self.base)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", PAGES["/feed"][0])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"not found")


def start_server(handler_class=Handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_crawl(tmp_dir, server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    Handler.base = base
    cfg = make_cfg(tmp_dir, base_url=base, max_urls_per_site=500)
    conn = ab.db_connect(cfg)
    fetcher = ab.Fetcher(cfg)
    proj = [p for p in cfg["_projects"] if p.name == "hanset"][0]
    run_started = ab.iso(ab.now_kst())
    ab.crawl_project(conn, fetcher, proj, cfg, ab.now_kst())
    pages = {r["url"]: r for r in conn.execute("SELECT * FROM pages").fetchall()}
    check("crawl: sitemap index + child parsed", f"{base}/guides/saunas" in pages, str(list(pages)))
    check("crawl: asset url skipped", f"{base}/img/logo.png" not in pages, str(list(pages)))
    check("crawl: robots-disallowed url stored but not fetched",
          f"{base}/private/hidden" in pages and pages[f"{base}/private/hidden"]["content_hash"] is None,
          str(pages.get(f"{base}/private/hidden")))
    saunas = pages.get(f"{base}/guides/saunas")
    check("crawl: title extracted", saunas and saunas["title"] == "사우나 문신 가이드", str(dict(saunas) if saunas else None))
    check("crawl: meta description extracted",
          saunas and saunas["description"] == "Korea sauna tattoo rules, checked per venue.", str(saunas["description"] if saunas else None))
    check("crawl: content hash stored", bool(saunas and saunas["content_hash"]), "")
    run = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    check("crawl: first run flag", run["first_run"] == 1, str(dict(run)))
    check("crawl: source=sitemap", run["source"] == "sitemap", str(run["source"]))
    check("crawl: new count", run["new_count"] == len(pages), f"{run['new_count']} vs {len(pages)}")
    check("crawl: robots blocked counted", run["robots_blocked"] == 1, str(dict(run)))
    check("crawl: fetches under cap", run["fetches"] <= 2, str(run["fetches"]))
    check("crawl: blocker recorded for robots-blocked url",
          conn.execute("SELECT COUNT(*) AS n FROM blockers WHERE code='ROBOTS_BLOCKED_URL' AND active=1"
                       ).fetchone()["n"] == 1, "")

    # ---- drafts on first run limited to 5 newest --------------------------
    ab.generate_drafts(conn, cfg, [proj], ab.now_kst(), run_started)
    draft_rows = conn.execute("SELECT * FROM drafts").fetchall()
    check("drafts: written for new pages", len(draft_rows) == len(pages), str(len(draft_rows)))
    for row in draft_rows:
        check(f"drafts: file exists {os.path.basename(row['path'])}", os.path.exists(row["path"]), row["path"])

    # ---- second run: nothing changed -> no fetches, statuses reset ---------
    fetcher2 = ab.Fetcher(cfg)
    ab.crawl_project(conn, fetcher2, proj, cfg, ab.now_kst())
    run2 = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    check("crawl#2: no page fetches (lastmod unchanged)", run2["fetches"] == 0, str(dict(run2)))
    check("crawl#2: nothing new", run2["new_count"] == 0, str(dict(run2)))
    pages2 = {r["url"]: r for r in conn.execute("SELECT * FROM pages").fetchall()}
    check("crawl#2: statuses unchanged", all(p["last_status"] == "unchanged" for p in pages2.values()),
          str({u: p['last_status'] for u, p in pages2.items()}))
    check("crawl#2: drafts not duplicated", conn.execute("SELECT COUNT(*) AS n FROM drafts"
                                                         ).fetchone()["n"] == len(draft_rows), "")
    conn.close()


def test_feed_fallback_config(tmp_dir):
    """No sitemap anywhere -> per-project rss_feeds list in config is used."""
    server = start_server(FeedHandler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    FeedHandler.base = base
    cfg = make_cfg(tmp_dir, base_url=base)
    cfg["_projects"][0].rss_feeds = [f"{base}/feed"]
    conn = ab.db_connect(cfg)
    ab.crawl_project(conn, ab.Fetcher(cfg), cfg["_projects"][0], cfg, ab.now_kst())
    pages = {r["url"] for r in conn.execute("SELECT url FROM pages").fetchall()}
    check("feed(config): item discovered", any("feed-item-1" in u for u in pages), str(pages))
    run = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    check("feed(config): source recorded", run["source"] == "rss (config)", str(run["source"]))
    check("feed(config): no NO_SITEMAP blocker", not conn.execute(
        "SELECT 1 FROM blockers WHERE code='NO_SITEMAP_OR_FEED' AND active=1").fetchall(), "")
    conn.close()
    server.shutdown()


def test_feed_fallback_autodiscover(tmp_dir):
    """No sitemap, no configured feed -> common feed paths are tried."""
    server = start_server(FeedHandler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    FeedHandler.base = base
    cfg = make_cfg(tmp_dir, base_url=base)
    conn = ab.db_connect(cfg)
    ab.crawl_project(conn, ab.Fetcher(cfg), cfg["_projects"][0], cfg, ab.now_kst())
    pages = {r["url"] for r in conn.execute("SELECT url FROM pages").fetchall()}
    check("feed(autodiscover): item discovered", any("feed-item-1" in u for u in pages), str(pages))
    run = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
    check("feed(autodiscover): source recorded", run["source"] == "rss (/feed)", str(run["source"]))
    conn.close()
    server.shutdown()


def test_discovery_robots_blocked(tmp_dir):
    """Reddit robots disallow must become a blocker, not a request."""
    cfg = make_cfg(tmp_dir)

    class StubFetcher(ab.Fetcher):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.requested = []

        def robots(self, url):
            return ab.RobotsInfo("https://www.reddit.com", None, ab.RobotsRules(),
                                 [], "ok")

        def can_fetch(self, url):
            return False if "reddit.com" in url else True

        def get(self, url, **kwargs):
            self.requested.append(url)
            return ab.FetchResult(url, 200, b"{}", None, None, 0.0)

    conn = ab.db_connect(cfg)
    fetcher = StubFetcher(cfg)
    ab.run_discovery(conn, fetcher, cfg["_projects"], cfg, ab.now_kst())
    check("discovery: no reddit request when robots forbids", fetcher.requested == [], str(fetcher.requested[:2]))
    row = conn.execute("SELECT code, active FROM blockers WHERE project='*' AND code='REDDIT_ROBOTS'").fetchone()
    check("discovery: REDDIT_ROBOTS blocker active", row is not None and row["active"] == 1, str(dict(row) if row else None))
    conn.close()


def test_discovery_reddit_store(tmp_dir):
    payload = {"data": {"children": [
        {"data": {"permalink": "/r/CrossFit/comments/1abcde/title_x/", "title": "drop in seoul",
                  "subreddit": "CrossFit", "created_utc": 1762900000.0, "score": 4,
                  "num_comments": 1, "stickied": False, "author": "someone"}},
        {"data": {"permalink": "/r/spam/comments/1zzzzz/y/", "title": "sticky", "subreddit": "spam",
                  "created_utc": 1762900000.0, "score": 0, "num_comments": 0,
                  "stickied": True, "author": "someone"}},
    ]}}
    cfg = make_cfg(tmp_dir)
    conn = ab.db_connect(cfg)
    stored = ab._store_reddit_candidates(conn, "hanset", "crossfit drop-in korea",
                                         json.dumps(payload).encode(), ab.iso(ab.now_kst()))
    rows = conn.execute("SELECT * FROM candidates").fetchall()
    check("candidates: stickied dropped, real kept", stored == 1 and len(rows) == 1, f"{stored} {len(rows)}")
    cols = dict(rows[0])
    check("candidates: no personal data columns", "author" not in rows[0].keys(), str(rows[0].keys()))
    check("candidates: metadata stored",
          cols["subreddit"] == "CrossFit" and cols["score"] == 4 and cols["num_comments"] == 1
          and cols["keyword"] == "crossfit drop-in korea", str(cols))
    conn.close()


def test_status(tmp_dir, server):
    cfg = make_cfg(tmp_dir, rounds_text=ROUNDS, no_url=("agamajung",))
    conn = ab.db_connect(cfg)
    ab.process_rounds_log(conn, cfg, ab.now_kst())
    # make every due check local: one 404, one dead port, one url-less entry
    base = f"http://127.0.0.1:{server.server_address[1]}"
    conn.execute("UPDATE published SET url=? WHERE label IS NOT NULL", (None,))
    conn.execute("UPDATE published SET url=? WHERE url LIKE '%goletter%'", (base + "/gone",))
    conn.execute("UPDATE published SET url=? WHERE url LIKE '%newsletter.place%'", ("http://127.0.0.1:1/dead",))
    conn.commit()
    ab._queue_metric_checks(conn, ab.now_kst())
    conn.execute("UPDATE metric_checks SET due_at='2026-01-01T00:00:00+09:00'")
    conn.commit()
    ab.run_due_checks(conn, ab.Fetcher(cfg), cfg, ab.now_kst())
    results = {(r["check_kind"], r["url"]): r for r in conn.execute(
        "SELECT mc.check_kind, mc.status, mc.result, pub.url FROM metric_checks mc "
        "JOIN published pub ON pub.id=mc.published_id").fetchall()}
    check("metrics: url-less entry -> 미확인",
          any(ab.UNKNOWN in (r["result"] or "") for r in results.values() if r["url"] is None),
          str([r["result"] for r in results.values() if r["url"] is None]))
    check("metrics: 404 page -> http_status 404 (honest, not fabricated)",
          any('"http_status": 404' in (r["result"] or "") for r in results.values()), str(list(results.values())))
    check("metrics: unreachable host -> 미확인",
          any(ab.UNKNOWN in (r["result"] or "") for r in results.values() if r["url"] and ":1/" in r["url"]),
          str([r["result"] for r in results.values() if r["url"] and ":1/" in r["url"]]))
    status = ab.collect_status(conn, cfg, ab.now_kst())
    check("status: pitchboard published=3", status["projects"]["pitchboard"]["published"] == 3,
          str(status["projects"]["pitchboard"]))
    check("status: agamajung blocker", any("NO_PUBLIC_URL" in b for b in status["projects"]["agamajung"]["blockers"]),
          str(status["projects"]["agamajung"]["blockers"]))
    md = ab.render_status_md(status, cfg, ab.now_kst(), "status")
    header = "| project | new content | candidates found | drafts ready | published (rounds.log) | metrics | blockers |"
    check("status.md: table header exact", header in md, md[:400])
    check("status.md: kst timestamp", "KST" in md, "")
    for name in ("hanset", "goallit", "kitchengenome", "pitchboard", "agamajung"):
        check(f"status.md: row for {name}", f"| {name} |" in md, "")
    slack = ab.render_status_slack(status, cfg, ab.now_kst())
    check("slack: no markdown table", "--- |" not in slack and "| project" not in slack, slack[:200])
    check("slack: bold project", "*pitchboard*" in slack, "")
    conn.close()


def test_status_write(tmp_dir):
    cfg = make_cfg(tmp_dir, rounds_text=ROUNDS)
    conn = ab.db_connect(cfg)
    ab.process_rounds_log(conn, cfg, ab.now_kst())
    ab.write_status(conn, cfg, ab.now_kst(), "status")
    check("status files written", os.path.exists(cfg["paths"]["status_md"])
          and os.path.exists(cfg["paths"]["status_slack"]), "")
    conn.close()


def test_env_and_slack(tmp_dir):
    cfg = make_cfg(tmp_dir)
    env_path = os.path.join(tmp_dir, "env")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write('# comment\nSLACK_WEBHOOK_URL="https://hooks.example.com/T000/B000/XXX"\nOTHER=x\n')
    env = ab.load_env_file(env_path)
    check("env: parsed + quotes stripped",
          env.get("SLACK_WEBHOOK_URL") == "https://hooks.example.com/T000/B000/XXX", str(env))
    check("env: comment ignored", "comment" not in env, str(env))
    check("env: missing file -> empty", ab.load_env_file(os.path.join(tmp_dir, "nope")) == {}, "")

    real_expand, real_urlopen = os.path.expanduser, urllib.request.urlopen
    os.path.expanduser = lambda p: env_path

    class StubResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        return StubResponse()

    try:
        # Exercise the missing-webhook branch without making an external request.
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("OTHER=x\n")
        check("slack: missing webhook -> skipped",
              ab.post_slack(cfg, "hello") is False, "")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("SLACK_WEBHOOK_URL=https://hooks.example.com/T000/B000/XXX\n")
        urllib.request.urlopen = fake_urlopen
        check("slack: posts json text payload",
              ab.post_slack(cfg, "status text") is True and len(sent) == 1, str(len(sent)))
        body = json.loads(sent[0].data.decode("utf-8"))
        check("slack: payload is the status text", body == {"text": "status text"}, str(body))

        def exploding_urlopen(req, timeout=None):
            raise OSError("connection refused to https://hooks.example.com/T000/B000/XXX")

        urllib.request.urlopen = exploding_urlopen
        check("slack: failure reported, returns False", ab.post_slack(cfg, "x") is False, "")
    finally:
        os.path.expanduser = real_expand
        urllib.request.urlopen = real_urlopen


def test_robots_rules():
    ua = "SoulboundViralAutobot/1.0 (+https://hanset.soulbound.co.kr)"
    wp = ab.RobotsRules("User-agent: *\nAllow: /\nDisallow: /private/\n")
    check("robots: broad allow does not shadow longer disallow",
          not wp.allows(ua, "https://x.test/private/hidden"), "")
    check("robots: normal path allowed", wp.allows(ua, "https://x.test/guides/a"), "")
    drupal = ab.RobotsRules("User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n")
    check("robots: allow beats disallow when longer",
          drupal.allows(ua, "https://x.test/wp-admin/admin-ajax.php"), "")
    check("robots: rest of admin disallowed",
          not drupal.allows(ua, "https://x.test/wp-admin/other"), "")
    bare = ab.RobotsRules("User-agent: *\nDisallow: /private/\n")
    check("robots: plain disallow", not bare.allows(ua, "https://x.test/private/x"), "")
    check("robots: empty robots allows", ab.RobotsRules("").allows(ua, "https://x.test/a"), "")
    check("robots: bare Disallow allows all",
          ab.RobotsRules("User-agent: *\nDisallow:\n").allows(ua, "https://x.test/a"), "")
    wild = ab.RobotsRules("User-agent: *\nDisallow: /*/print$\n")
    check("robots: wildcard + $ anchor", not wild.allows(ua, "https://x.test/x/print"), "")
    check("robots: wildcard not over-matched", wild.allows(ua, "https://x.test/x/print/more"), "")
    specific = ab.RobotsRules(
        "User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    check("robots: other-agent group ignored", specific.allows(ua, "https://x.test/a"), "")
    import urllib.robotparser as _rp
    std = _rp.RobotFileParser()
    std.parse(["User-agent: *", "Allow: /", "Disallow: /private/"])
    check("robots: stdlib parser is indeed first-match (guards the need for RobotsRules)",
          std.can_fetch(ua, "https://x.test/private/hidden") is True, "")


def test_daemon_stages_and_fallback(tmp_dir):
    import daemon as dm
    cfg_path = os.path.join(ROOT, "config.json")
    dcfg = dm.load_daemon_config(cfg_path)
    check("daemon: audit_every_prs == 10", dcfg.get("audit_every_prs") == 10, str(dcfg.get("audit_every_prs")))

    expected = {
        "design": [("astra6", "codex", "gpt-6-astra", "high"),
                   ("dsflash", "deepseek", "deepseek-flash", None),
                   ("glm", "glm", None, None),
                   ("swe2", "devin", "swe-2", None)],
        "design_review": [("dsflash", "deepseek", "deepseek-flash", None),
                          ("sol61", "codex", "gpt-6.1-sol", "high"),
                          ("glm", "glm", None, None),
                          ("swe2", "devin", "swe-2", None)],
        "build": [("sol61", "codex", "gpt-6.1-sol", "high"),
                  ("dsflash", "deepseek", "deepseek-flash", None),
                  ("glm", "glm", None, None),
                  ("swe2", "devin", "swe-2", None)],
        "build_review": [("glm", "glm", None, None),
                         ("dsflash", "deepseek", "deepseek-flash", None),
                         ("sol61", "codex", "gpt-6.1-sol", "high"),
                         ("swe2", "devin", "swe-2", None)],
        "review_fix": [("dsflash", "deepseek", "deepseek-flash", None),
                       ("sol61", "codex", "gpt-6.1-sol", "high"),
                       ("glm", "glm", None, None),
                       ("swe2", "devin", "swe-2", None)],
        "re_review": [("devin_auto", "devin", "automatic", None),
                      ("dsflash", "deepseek", "deepseek-flash", None),
                      ("sol61", "codex", "gpt-6.1-sol", "high"),
                      ("swe2", "devin", "swe-2", None)],
        "audit": [("astra6", "codex", "gpt-6-astra", "high"),
                  ("glm", "glm", None, None),
                  ("swe2", "devin", "swe-2", None)],
    }

    for stage, exp_chain in expected.items():
        got = [
            (c.get("model"), c.get("provider"), c.get("id"), c.get("effort"))
            for c in dm.get_stage_chain(stage, cfg_path)
        ]
        check(f"daemon stage chain {stage}", got == exp_chain, f"got={got} expected={exp_chain}")

    check("daemon readonly stages",
          all(dm.is_stage_readonly(s) for s in ("design", "design_review", "build_review", "re_review", "audit")), "")
    check("daemon write stages",
          all(not dm.is_stage_readonly(s) for s in ("build", "review_fix")), "")

    # Command construction checks
    c_cmd, c_env = dm.build_lane_command(
        dm.get_stage_chain("design", cfg_path)[0], "/tmp/p.md", "/tmp", "/tmp/o.out", readonly=True
    )
    check("daemon codex astra6 high readonly cmd",
          "gpt-6-astra" in c_cmd and 'model_reasoning_effort="high"' in c_cmd and "read-only" in c_cmd,
          str(c_cmd))

    x_cmd, _ = dm.build_lane_command(
        dm.get_stage_chain("build", cfg_path)[0], "/tmp/p.md", "/tmp", "/tmp/o.out", readonly=False
    )
    check("daemon codex sol61 high write cmd",
          "gpt-6.1-sol" in x_cmd and 'model_reasoning_effort="high"' in x_cmd and "workspace-write" in x_cmd,
          str(x_cmd))

    g_cmd, g_env = dm.build_lane_command(
        dm.get_stage_chain("build_review", cfg_path)[0], "/tmp/p.md", "/tmp", "/tmp/o.out", readonly=True,
        base_env={"CLAUDE_CONFIG_DIR": "/home/box/.claude-fable", "PATH": "/usr/bin"}
    )
    check("daemon glm unsets CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR" not in g_env, str(g_env))
    check("daemon glm readonly flags", "--allowedTools" in g_cmd and "Read" in g_cmd, str(g_cmd))

    d_cmd, _ = dm.build_lane_command(
        dm.get_stage_chain("audit", cfg_path)[2], "/tmp/p.md", "/tmp", "/tmp/o.out", readonly=True
    )
    check("daemon devin swe-2 cmd", "--model" in d_cmd and "swe-2" in d_cmd and "auto" in d_cmd, str(d_cmd))

    # Fallback execution check: primary fails with 429, 1st backup fails with quota, 2nd backup (GLM) succeeds
    calls = []
    outcomes = [
        (1, "", "HTTP 429 rate limit reached"),
        (0, "You've hit your usage limit. Resets in 2 hours", ""),
        (0, "VERDICT: APPROVE\nAll checks passed.", ""),
    ]

    def fake_runner(choice, prompt, cwd, log_prefix, ro):
        idx = len(calls)
        calls.append(choice["model"])
        return outcomes[idx]

    res = dm.run_stage_chain("design", "design task", tmp_dir, tmp_dir, runner=fake_runner, config_path=cfg_path)
    check("daemon fallback stops at 2nd backup (glm)",
          calls == ["astra6", "dsflash", "glm"] and res["choice"]["model"] == "glm" and len(res["skipped"]) == 2,
          f"calls={calls} res={res}")
    check("daemon verdict parse", dm.parse_verdict(res["text"]) == "APPROVE", str(res["text"]))

    # Blueprint & 4-batch roadmap seeding
    check("daemon blueprint is BLUEPRINT.md",
          dcfg.get("blueprint") == "BLUEPRINT.md" and os.path.exists(os.path.join(ROOT, "BLUEPRINT.md")),
          str(dcfg.get("blueprint")))
    qdir = os.path.join(tmp_dir, "dev-queue")
    seeded = dm.seed_roadmap_batches(qdir)
    check("daemon seeds 4 roadmap batches", len(seeded) == 4, str(seeded))
    seeded_again = dm.seed_roadmap_batches(qdir)
    check("daemon seed idempotent", seeded_again == [], str(seeded_again))

    # 10-PR audit trigger
    check("daemon audit every 10 PRs",
          dm.should_run_audit(10, 0) and dm.should_run_audit(20, 10)
          and not dm.should_run_audit(9, 0) and not dm.should_run_audit(10, 10), "")


def test_is_limit_or_unavailable_content():
    import daemon as dm
    long = ("Design notes covering a weekly rate limit and quota policy for later stages. " * 12)
    check("limit: long rc=0 text with rate limit/quota is available",
          len(long) >= 600 and not dm.is_limit_or_unavailable(0, long), str(len(long)))
    check("limit: short 429 limit text is unavailable",
          dm.is_limit_or_unavailable(0, "HTTP 429 rate limit reached"), "")
    check("limit: rc!=0 is unavailable",
          dm.is_limit_or_unavailable(1, "ok"), "")


def test_config_batch1(tmp_dir):
    cfg = make_cfg(tmp_dir)
    projects = {p.name: p for p in cfg["_projects"]}
    baby, lit = projects["agamajung"], projects["goallit"]
    check("config: agamajung public URL", baby.url == "https://agamajoong.soulbound.co.kr")
    check("config: agamajoong domain alias", "agamajoong.soulbound.co.kr" in baby.domains)
    check("config: no stale blocker note", "blocker_note" not in baby.raw)
    check("config: both baby romanizations", {"agamajung", "agamajoong"} <= set(baby.names))
    check("config: primary Korean spelling", lit.tagline_ko.startswith("꼴릿") and "골릿" not in lit.tagline_ko)
    check("config: old Korean alias retained", lit.names.index("꼴릿") < lit.names.index("골릿"))
    check("config: reliability limits", all(cfg["limits"][key] == value for key, value in
          (("metric_max_attempts", 3), ("metric_retry_minutes", 60), ("max_reverify_per_site", 10))))
    check("config: domain maps to stable project key", ab.map_project(
        "see https://agamajoong.soulbound.co.kr/x", "https://agamajoong.soulbound.co.kr/x",
        cfg["_projects"])[0] == "agamajung")
    # Defaults also apply to configurations written before Batch 1.
    raw = json.loads(json.dumps({k: v for k, v in cfg.items() if k != "_projects"}))
    for key in ("metric_max_attempts", "metric_retry_minutes", "max_reverify_per_site"):
        raw["limits"].pop(key)
    path = os.path.join(tmp_dir, "old-config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    loaded = ab.load_config(path)
    check("config: missing limits get defaults", all(loaded["limits"][key] == cfg["limits"][key]
          for key in ("metric_max_attempts", "metric_retry_minutes", "max_reverify_per_site")))
    conn = ab.db_connect(cfg)
    ab.ensure_config_blockers(conn, cfg)
    check("config: actual URLs have no missing-URL blocker", not conn.execute(
        "SELECT 1 FROM blockers WHERE code='NO_PUBLIC_URL' AND active=1").fetchall())
    ab.ensure_config_blockers(conn, make_cfg(tmp_dir, no_url=("agamajung",)))
    check("config: explicit null URL still blocked", conn.execute(
        "SELECT active FROM blockers WHERE project='agamajung' AND code='NO_PUBLIC_URL'"
    ).fetchone()["active"] == 1)
    conn.close()


def test_metric_retry(tmp_dir):
    cfg = make_cfg(tmp_dir)
    conn = ab.db_connect(cfg)
    now = ab.kst(datetime(2026, 10, 23, 12))
    post_id = ab.record_post(conn, "hanset", "threads", "https://example.test/landing", "retry", now)
    ab.mark_published_verified(conn, post_id, "https://example.test/post", now - timedelta(days=8), now)

    class StubFetcher:
        success = False
        requests = 0

        def get(self, url, **kwargs):
            self.requests += 1
            return ab.FetchResult(url, 200 if self.success else None, b"ok", None,
                                  None if self.success else "timeout", 0.0)

    fetcher = StubFetcher()
    ab.run_due_checks(conn, fetcher, cfg, now)
    rows = conn.execute("SELECT * FROM metric_checks").fetchall()
    check("retry: first failure remains pending", all(r["status"] == "pending" and r["attempts"] == 1 for r in rows))
    check("retry: first backoff is 60 minutes", all(r["retry_at"] == ab.iso(now + timedelta(hours=1)) for r in rows))
    check("retry: checked_at uses supplied clock", all(r["checked_at"] == ab.iso(now) for r in rows))
    ab.run_due_checks(conn, fetcher, cfg, now + timedelta(minutes=59))
    check("retry: no request before retry_at", fetcher.requests == 2)
    ab.run_due_checks(conn, fetcher, cfg, now + timedelta(hours=1))
    rows = conn.execute("SELECT * FROM metric_checks").fetchall()
    check("retry: second failure still pending", all(r["status"] == "pending" and r["attempts"] == 2 for r in rows))
    check("retry: second backoff is 120 minutes", all(r["retry_at"] == ab.iso(now + timedelta(hours=3)) for r in rows))
    ab.run_due_checks(conn, fetcher, cfg, now + timedelta(hours=3))
    rows = conn.execute("SELECT * FROM metric_checks").fetchall()
    check("retry: third failure is failed, never done", all(
        r["status"] == "failed" and r["attempts"] == 3 and r["failure_reason"] and r["retry_at"] is None for r in rows))
    ab.run_due_checks(conn, fetcher, cfg, now + timedelta(days=10))
    check("retry: failed checks are terminal", fetcher.requests == 6)
    check("retry: status distinguishes failed checks", ab.collect_status(conn, cfg, now)["failed_checks"] == 2
          and "failed 2" in ab._metrics_cell(conn, "hanset") and "48h 0/1" in ab._metrics_cell(conn, "hanset"))

    recovered = ab.record_post(conn, "hanset", "x", "https://example.test/recover", "recover", now)
    ab.mark_published_verified(conn, recovered, "https://example.test/recovered", now - timedelta(days=3), now)
    ab.run_due_checks(conn, fetcher, cfg, now)
    fetcher.success = True
    ab.run_due_checks(conn, fetcher, cfg, now + timedelta(hours=1))
    row = conn.execute("SELECT mc.* FROM metric_checks mc JOIN published p ON p.id=mc.published_id "
                       "WHERE p.post_id=? AND check_kind='48h'", (recovered,)).fetchone()
    check("retry: success after failure clears retry and reason", row["status"] == "done" and row["attempts"] == 2
          and row["retry_at"] is None and row["failure_reason"] is None)
    conn.execute("UPDATE metric_checks SET status='error' WHERE id=?", (rows[0]["id"],))
    conn.execute("UPDATE metric_checks SET status='done', result=? WHERE id=?",
                 (json.dumps({"checked": False, "reason": "old timeout"}), rows[1]["id"]))
    conn.commit()
    conn.close()
    conn = ab.db_connect(cfg)
    migrated = {r["id"]: r for r in conn.execute("SELECT * FROM metric_checks")}
    check("migration: old error is retryable", migrated[rows[0]["id"]]["status"] == "pending")
    check("migration: false done becomes failed", migrated[rows[1]["id"]]["status"] == "failed"
          and migrated[rows[1]["id"]]["failure_reason"] == "old timeout")
    check("migration: true done remains done", conn.execute(
        "SELECT status FROM metric_checks WHERE id=?", (row["id"],)).fetchone()["status"] == "done")
    conn.close()


def seed_page(conn, url, now, state="candidate", valid_until=None, digest="stored"):
    """A stored page with a linked draft; all URLs are local or stubbed."""
    conn.execute("INSERT INTO pages (project, url, first_seen, last_seen, content_hash, valid_until) "
                 "VALUES ('hanset', ?, ?, ?, ?, ?)", (url, ab.iso(now), ab.iso(now), digest, valid_until))
    cur = conn.execute("INSERT INTO drafts (project, page_url, path, channels, created, state) "
                       "VALUES ('hanset', ?, 'unused.md', 'threads', ?, ?)", (url, ab.iso(now), state))
    conn.commit()
    return cur.lastrowid


def test_page_reverify(tmp_dir, server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    Handler.base = base
    cfg = make_cfg(tmp_dir, base_url=base)
    conn = ab.db_connect(cfg)
    proj = cfg["_projects"][0]
    now = ab.kst(datetime(2026, 10, 23, 12))
    for path in ("/gone", "/gone-410"):
        url = base + path
        seed_page(conn, url, now)
        ab.record_post(conn, "hanset", "threads", url, path, now)
    verified = ab.record_post(conn, "hanset", "x", base + "/gone", "already live", now)
    ab.mark_published_verified(conn, verified, base + "/external-post", now, now)
    result = ab.reverify_pages(conn, ab.Fetcher(cfg), proj, cfg, now)
    check("reverify: 404 and 410 detected", result == {"checked": 2, "gone": 2, "expired": 0, "held": 4}, str(result))
    pages = {r["url"]: r for r in conn.execute("SELECT * FROM pages")}
    check("reverify: HTTP status and verification time recorded", all(
        pages[base + path]["http_status"] == code and pages[base + path]["last_status"] == "gone"
        and pages[base + path]["last_verified"] == ab.iso(now) for path, code in (("/gone", 404), ("/gone-410", 410))))
    check("reverify: drafts held with reasons", all(r["state"] == "expired_hold" and r["hold_reason"]
          for r in conn.execute("SELECT * FROM drafts")))
    check("reverify: scheduled posts held", conn.execute(
        "SELECT COUNT(*) AS n FROM published WHERE state='expired_hold'").fetchone()["n"] == 2)
    check("reverify: live post preserved", conn.execute("SELECT state FROM published WHERE post_id=?",
          (verified,)).fetchone()["state"] == "published_verified")
    check("reverify: state changes audited", conn.execute("SELECT COUNT(*) AS n FROM post_events "
          "WHERE to_state='expired_hold'").fetchone()["n"] == 4)
    check("reverify: blocker active", conn.execute("SELECT active FROM blockers WHERE code='PAGES_GONE'").fetchone()["active"] == 1)
    again = ab.reverify_pages(conn, ab.Fetcher(cfg), proj, cfg, now)
    check("reverify: repeated check makes no changes", again == {"checked": 0, "gone": 0, "expired": 0, "held": 0})
    check("reverify: repeated hold adds no events", conn.execute("SELECT COUNT(*) AS n FROM post_events "
          "WHERE to_state='expired_hold'").fetchone()["n"] == 4)

    url = base + "/deadline"
    seed_page(conn, url, now, valid_until=ab.iso(now - timedelta(seconds=1)))
    ab.record_post(conn, "hanset", "threads", url, "deadline", now)
    fetcher = ab.Fetcher(cfg)
    result = ab.reverify_pages(conn, fetcher, proj, cfg, now)
    check("reverify: expiry needs no HTTP requests", result == {"checked": 0, "gone": 0, "expired": 1, "held": 2}
          and fetcher.requests == 0, str(result))
    check("reverify: expired status saved", conn.execute("SELECT last_status FROM pages WHERE url=?", (url,)).fetchone()["last_status"] == "expired")
    # A cached URL still in the sitemap stays dead after the crawl's status reset.
    seed_page(conn, base + "/guides/saunas", now)
    ab._mark_page_dead(conn, "hanset", base + "/guides/saunas", "gone", 404, now)
    ab.crawl_project(conn, ab.Fetcher(cfg), proj, cfg, now)
    check("crawl: preserves gone and expired", all(conn.execute(
        "SELECT last_status FROM pages WHERE url=?", (u,)).fetchone()["last_status"] == status
        for u, status in ((base + "/gone", "gone"), (url, "expired"), (base + "/guides/saunas", "gone"))))
    check("crawl: PAGES_GONE survives blocker cleanup", conn.execute(
        "SELECT active FROM blockers WHERE code='PAGES_GONE'").fetchone()["active"] == 1)
    # Force a first content fetch from a sitemap URL to hit 404.
    sitemap = PAGES["/posts-sitemap.xml"]
    try:
        PAGES["/posts-sitemap.xml"] = ("application/xml", "<urlset><url><loc>{base}/fresh-gone</loc></url></urlset>")
        ab.crawl_project(conn, ab.Fetcher(cfg), proj, cfg, now)
    finally:
        PAGES["/posts-sitemap.xml"] = sitemap
    check("crawl: new 404 page saved as gone", conn.execute(
        "SELECT last_status, http_status FROM pages WHERE url=?", (base + "/fresh-gone",)).fetchone()["last_status"] == "gone")
    check("crawl: permanent removal is not FETCH_ERRORS", not conn.execute(
        "SELECT 1 FROM blockers WHERE code='FETCH_ERRORS' AND active=1").fetchall())
    conn.close()

    # Cap, order, robots and transient errors without even local HTTP requests.
    cfg = make_cfg(os.path.join(tmp_dir, "bounded"), base_url=base, max_reverify_per_site=1)
    conn = ab.db_connect(cfg)
    for path in ("/private/old", "/old", "/recent"):
        seed_page(conn, base + path, now)
    conn.execute("UPDATE pages SET last_verified=? WHERE url=?", (ab.iso(now), base + "/recent"))
    conn.commit()

    class StubFetcher:
        requested = []
        success = False

        def can_fetch(self, url):
            return "/private/" not in url

        def get(self, url, **kwargs):
            self.requested.append((url, kwargs.get("max_bytes")))
            return ab.FetchResult(url, 200 if self.success else None, b"ok", None, "timeout", 0)

    fetcher = StubFetcher()
    result = ab.reverify_pages(conn, fetcher, cfg["_projects"][0], cfg, now)
    check("reverify: oldest first, cap and robots respected", fetcher.requested == [(base + "/old", 4096)])
    row = conn.execute("SELECT * FROM pages WHERE url=?", (base + "/old",)).fetchone()
    check("reverify: timeout leaves verification unknown", row["last_status"] == "new" and row["last_verified"] is None
          and row["http_status"] is None and result["gone"] == 0)
    fetcher.success = True
    ab.reverify_pages(conn, fetcher, cfg["_projects"][0], cfg, now)
    row = conn.execute("SELECT * FROM pages WHERE url=?", (base + "/old",)).fetchone()
    check("reverify: success updates only HTTP metadata", row["last_status"] == "new" and row["http_status"] == 200
          and row["last_verified"] == ab.iso(now))
    conn.close()


def test_post_id_state_machine(tmp_dir):
    cfg = make_cfg(tmp_dir, rounds_text=ROUNDS)
    conn = ab.db_connect(cfg)
    now = ab.kst(datetime(2026, 10, 23, 12))
    url = "https://example.test/landing"
    first_id = ab.make_post_id("hanset", "threads", url)
    check("post_id: deterministic and fragment-independent", first_id == ab.make_post_id("hanset", "threads", url + "#frag")
          and first_id.startswith("p_") and len(first_id) == 18)
    check("post_id: channel and project isolated", first_id != ab.make_post_id("hanset", "x", url)
          and first_id != ab.make_post_id("goallit", "threads", url))
    check("post_id: normalized label fallback", ab.make_post_id(None, None, label=" Hello ") == ab.make_post_id("", "", label="hello"))
    post_id = ab.record_post(conn, "hanset", "threads", url, "test", now)
    repeated = ab.record_post(conn, "hanset", "threads", url + "#frag", "changed label", now + timedelta(hours=1))
    check("post_id: repeated record inserts one row", post_id == repeated and conn.execute(
        "SELECT COUNT(*) AS n FROM published").fetchone()["n"] == 1)
    ab._queue_metric_checks(conn, now)
    check("metrics: scheduled post has no checks", not conn.execute("SELECT * FROM metric_checks").fetchall())
    conn.close()
    conn = ab.db_connect(cfg)
    row = conn.execute("SELECT * FROM published WHERE post_id=?", (post_id,)).fetchone()
    check("migration: scheduled post stays unverified on reopen", row["verified_at"] is None and row["url"] is None)
    check("state: all forward steps allowed", all(ab.can_transition(a, b) for a, b in zip(ab.POST_FLOW, ab.POST_FLOW[1:])))
    check("state: holds allowed only before publication", all(ab.can_transition(a, b) for a in ab.PRE_PUBLISH for b in ab.HOLD_STATES)
          and all(ab.can_transition(a, "candidate") for a in ab.HOLD_STATES)
          and not ab.can_transition("published_verified", "expired_hold"))
    check("state: skips, backwards and terminal moves denied", not ab.can_transition("candidate", "published_verified")
          and not ab.can_transition("scheduled", "approved") and not any(ab.can_transition("evaluated", b) for b in ab.POST_FLOW + ab.HOLD_STATES)
          and not ab.can_transition("invalid", "candidate"))
    try:
        ab.transition(conn, "post", row["id"], "evaluated", now)
        rejected = False
    except ValueError:
        rejected = True
    check("state: invalid transition raises and preserves row", rejected and conn.execute(
        "SELECT state FROM published WHERE post_id=?", (post_id,)).fetchone()["state"] == "scheduled")
    verified_at = now + timedelta(days=2)
    ab.mark_published_verified(conn, post_id, "https://example.test/public-post", verified_at, verified_at)
    checks = {r["check_kind"]: r["due_at"] for r in conn.execute("SELECT * FROM metric_checks")}
    check("metrics: due dates start at verification", checks == {"48h": ab.iso(verified_at + timedelta(hours=48)),
          "7d": ab.iso(verified_at + timedelta(days=7))}, str(checks))
    ab.mark_published_verified(conn, post_id, "https://example.test/public-post#frag", verified_at + timedelta(days=1), verified_at)
    check("metrics: repeated verification preserves clock", conn.execute("SELECT verified_at FROM published WHERE post_id=?",
          (post_id,)).fetchone()["verified_at"] == ab.iso(verified_at) and conn.execute(
          "SELECT COUNT(*) AS n FROM metric_checks").fetchone()["n"] == 2)
    ab.transition(conn, "post", row["id"], "measured", now)
    ab.transition(conn, "post", row["id"], "evaluated", now)
    before = conn.execute("SELECT COUNT(*) AS n FROM post_events").fetchone()["n"]
    ab.transition(conn, "post", row["id"], "evaluated", now)
    check("state: same state creates no duplicate event", before == 3 and conn.execute(
        "SELECT COUNT(*) AS n FROM post_events").fetchone()["n"] == before)
    draft_id = seed_page(conn, url, now)
    ab.transition(conn, "draft", draft_id, "needs_research", now, "missing evidence")
    ab.transition(conn, "draft", draft_id, "candidate", now)
    check("state: hold can restart at candidate", conn.execute("SELECT hold_reason FROM drafts WHERE id=?",
          (draft_id,)).fetchone()["hold_reason"] is None)
    # Failure inserting the event must roll the state update back too.
    conn.execute("CREATE TEMP TRIGGER reject_event BEFORE INSERT ON post_events "
                 "BEGIN SELECT RAISE(ABORT, 'event rejected'); END")
    try:
        ab.transition(conn, "draft", draft_id, "fact_checked", now)
        rejected = False
    except sqlite3.IntegrityError:
        rejected = True
    conn.execute("DROP TRIGGER reject_event")
    check("state: update and event are atomic", rejected and conn.execute(
        "SELECT state FROM drafts WHERE id=?", (draft_id,)).fetchone()["state"] == "candidate")
    conn.close()

    cfg = make_cfg(os.path.join(tmp_dir, "rounds"), rounds_text=ROUNDS)
    os.makedirs(os.path.dirname(cfg["paths"]["rounds_log"]), exist_ok=True)
    conn = ab.db_connect(cfg)
    ab.process_rounds_log(conn, cfg, now)
    with open(cfg["paths"]["rounds_log"], "w", encoding="utf-8") as fh:
        fh.write("an inserted heading\n" + ROUNDS)
    check("post_id: line insertion adds no duplicates", ab.process_rounds_log(conn, cfg, now) == 0
          and conn.execute("SELECT COUNT(*) AS n FROM published").fetchone()["n"] == 3)
    # One historical duplicate and one unfilled id that collides with an existing id.
    old = conn.execute("SELECT * FROM published WHERE url IS NOT NULL ORDER BY id LIMIT 1").fetchone()
    conn.execute("INSERT INTO published (project, url, kind, published_at, first_seen) VALUES (?, ?, ?, ?, ?)",
                 (old["project"], old["url"], old["kind"], old["published_at"], old["first_seen"]))
    conn.commit()
    conn.close()
    conn = ab.db_connect(cfg)
    check("migration: collision preserves legacy row without id", conn.execute(
        "SELECT COUNT(*) AS n FROM published WHERE post_id IS NULL").fetchone()["n"] == 1)
    conn.close()

    # Upgrade real pre-Batch-1 tables, rather than only exercising a fresh schema.
    cfg = make_cfg(os.path.join(tmp_dir, "legacy"))
    os.makedirs(os.path.dirname(cfg["paths"]["db"]), exist_ok=True)
    conn = sqlite3.connect(cfg["paths"]["db"])
    conn.executescript("""
        CREATE TABLE pages (id INTEGER PRIMARY KEY, project TEXT, url TEXT,
            first_seen TEXT, last_seen TEXT, lastmod TEXT, content_hash TEXT, title TEXT,
            description TEXT, last_status TEXT DEFAULT 'new', last_changed TEXT,
            fetch_count INTEGER DEFAULT 0, UNIQUE(project,url));
        CREATE TABLE drafts (id INTEGER PRIMARY KEY, project TEXT, page_url TEXT, path TEXT,
            channels TEXT, page_status TEXT, created TEXT, UNIQUE(project,page_url));
        CREATE TABLE published (id INTEGER PRIMARY KEY, project TEXT, url TEXT, label TEXT,
            kind TEXT, published_at TEXT, source TEXT DEFAULT 'rounds.log', line_no INTEGER,
            raw_line TEXT, mapping TEXT, due_hint_48h TEXT, due_hint_7d TEXT,
            dedupe_key TEXT UNIQUE, first_seen TEXT);
        CREATE TABLE metric_checks (id INTEGER PRIMARY KEY, published_id INTEGER,
            check_kind TEXT, due_at TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
            checked_at TEXT, result TEXT, UNIQUE(published_id,check_kind));
        INSERT INTO pages(project,url,first_seen,last_seen) VALUES ('hanset','https://example.test/a','old','old');
        INSERT INTO drafts(project,page_url,path,channels,created) VALUES ('hanset','https://example.test/a','a.md','x','old');
        INSERT INTO published(project,url,kind,published_at,first_seen)
            VALUES ('hanset','https://example.test/old-post','web','2026-10-01T12:00:00+09:00','old');
        INSERT INTO published(project,url,kind,published_at,first_seen)
            VALUES ('hanset','https://example.test/old-post','web','2026-10-01T12:00:00+09:00','old');
    """)
    conn.close()
    conn = ab.db_connect(cfg)
    check("migration: old columns added", {"http_status", "last_verified", "valid_until"} <=
          {r["name"] for r in conn.execute("PRAGMA table_info(pages)")} and conn.execute(
          "SELECT state FROM drafts").fetchone()["state"] == "candidate")
    old = conn.execute("SELECT * FROM published ORDER BY id LIMIT 1").fetchone()
    check("migration: legacy post clock and identity backfilled", old["verified_at"] == old["published_at"]
          and old["channel"] == "web" and old["state"] == "published_verified" and old["post_id"])
    check("migration: duplicate historical identity handled", conn.execute(
        "SELECT COUNT(*) AS n FROM published WHERE post_id IS NULL").fetchone()["n"] == 1)
    conn.close()
    conn = ab.db_connect(cfg)
    check("migration: repeated upgrade preserves data", conn.execute("SELECT COUNT(*) AS n FROM published").fetchone()["n"] == 2)
    conn.close()


# ---------------------------------------------------------------- Batch 2

def batch2_fixture(tmp_dir):
    cfg = make_cfg(tmp_dir)
    cfg["evidence"]["dir"] = os.path.join(tmp_dir, "evidence")
    cfg["questions"]["dir"] = os.path.join(tmp_dir, "questions")
    now = ab.kst(datetime(2026, 10, 23, 12))
    proj = cfg["_projects"][0]
    conn = ab.db_connect(cfg)
    landing = proj.url + "/guides/sauna-day-pass"
    conn.execute("INSERT INTO pages (project,url,first_seen,last_seen,last_changed,title) VALUES (?,?,?,?,?,?)",
                 (proj.name, landing, ab.iso(now), ab.iso(now), ab.iso(now), "Fixture sauna guide"))
    raw_q = {"project": proj.name, "question": "Which sauna day pass permits entry?",
             "keyword_cluster": "sauna-day-pass", "source": "search_console", "source_refs": [],
             "persona_decision": "A visitor decides which day pass to choose",
             "why_now": "Fixture search query observed today", "unique_evidence": "Fixture verified entry condition",
             "natural_channel": "reddit", "target_landing_url": landing}
    raw_e = {"id": "fixture-entry", "project": proj.name, "keyword_cluster": "sauna-day-pass",
             "keywords": ["sauna", "day pass"], "kind": "entry_condition",
             "fact": "Fixture fact for offline tests; not a real venue claim",
             "source_url": "https://official.example.test/conditions",
             "verified_at": ab.iso(now - timedelta(days=1)), "valid_until": ab.iso(now + timedelta(days=10)),
             "allowed_claims": ["Fixture entry requires checking the booking conditions"],
             "forbidden_claims": ["Fixture unrestricted entry"]}
    return cfg, conn, proj, now, raw_q, raw_e


def batch2_write(cfg, section, project, rows):
    os.makedirs(cfg[section]["dir"], exist_ok=True)
    with open(os.path.join(cfg[section]["dir"], project + ".json"), "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False)


def batch2_question(conn, cfg, proj, raw, now):
    rec, errors = ab.normalize_question(raw, proj, cfg["_projects"], cfg)
    assert not errors, errors
    return ab.upsert_question(conn, rec, now)


def batch2_evidence(conn, proj, raw, now):
    rec, errors = ab.validate_evidence(raw, proj, now)
    assert not errors, errors
    return ab.upsert_evidence(conn, rec, now)


def test_question_candidates(tmp_dir):
    cfg, conn, proj, now, raw, _ = batch2_fixture(tmp_dir)
    for field in ("persona_decision", "why_now", "unique_evidence", "natural_channel", "target_landing_url"):
        missing = dict(raw)
        missing.pop(field)
        _, errors = ab.normalize_question(missing, proj, cfg["_projects"], cfg)
        check("question: required " + field, "missing:" + field in errors, str(errors))
    for url, reason in ((proj.url, "main_page"), (proj.url + "/index.html?x=1", "main_page"),
                        (proj.url + "/a/../", "main_page"),
                        ("https://pitchboard.soulbound.co.kr/calls/a", "other_brand"),
                        ("https://external.test/guides/a", "external_domain"),
                        ("javascript:alert(1)", "invalid_url")):
        _, errors = ab.normalize_question(dict(raw, target_landing_url=url), proj, cfg["_projects"], cfg)
        check("question: landing rejects " + reason + url, "landing:" + reason in errors, str(errors))
    for value, source, expected in ((None, None, "검색량 미확인"), ("1200", None, "검색량 미확인"),
                                    ("1200", "Fixture search console export", "1200")):
        rec, errors = ab.normalize_question(dict(raw, search_volume_status=value, search_volume_source=source),
                                             proj, cfg["_projects"], cfg)
        check("question: volume provenance " + str(source) + str(value), rec["search_volume_status"] == expected)
    rec, _ = ab.normalize_question(dict(raw, author="private-id", username="secret", email="secret@example.test",
                                       question="Which sauna for u/someone @private contact@example.test?",
                                       source_refs=[{"author": "private-id"}, "https://reddit.com/user/private-id", 1,
                                                    "https://www.reddit.com/r/travel/comments/abc/?author=secret"]),
                                   proj, cfg["_projects"], cfg)
    stored = json.dumps(rec)
    check("question: personal fields and inline identifiers removed",
          not any(v in stored for v in ("private-id", "someone", "contact@", "@private", "secret")), stored)
    qid = ab.upsert_question(conn, rec, now)
    same = ab.upsert_question(conn, dict(rec, source_refs=[2, 1]), now + timedelta(hours=1))
    row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    check("question: stable upsert merges refs and preserves first_seen", same == qid and
          row["first_seen"] == ab.iso(now) and row["last_seen"] == ab.iso(now + timedelta(hours=1)) and
          set(v for v in json.loads(row["source_refs"]) if type(v) is int) == {1, 2})
    check("question: no personal columns", not ab.PERSONAL_KEYS.intersection(row.keys()))
    scored = ab.score_question(rec, proj, cfg, 9, {"count": 4, "latest": ab.iso(now)}, now)
    keys = ("demand", "intent", "brand_fit", "evidence", "timeliness")
    check("question: score range and weighted priority", all(0 <= scored["score_" + k] <= 3 for k in keys) and
          abs(scored["priority"] - sum(scored["score_" + k] * cfg["demand"]["priority_weights"][k] for k in keys)) < .000001)
    for count, score in ((0, 0), (1, 1), (2, 2), (3, 2), (4, 3)):
        result = ab.score_question(dict(rec, source="reddit"), proj, cfg, 0, {"count": count}, now)
        check("question: demand distinct-thread scale " + str(count), result["score_demand"] == score)
    confirmed = ab.score_question(dict(rec, source="manual", search_volume_status="1200",
                                       search_volume_source="Fixture export"), proj, cfg, 0, {}, now)
    unknown = ab.score_question(dict(rec, source="manual", search_volume_status="1200",
                                    search_volume_source=None), proj, cfg, 0, {}, now)
    check("question: only confirmed positive volume indicates demand", confirmed["score_demand"] == 1 and unknown["score_demand"] == 0)
    payload = {"data": {"children": [
        {"data": {"permalink": f"/r/travel/comments/fixture{i}/", "title": "Which sauna day pass? u/private-id",
                  "author": "private-id", "created_utc": (now - timedelta(days=1)).timestamp(), "subreddit": "travel"}}
        for i in range(3)]}}
    ab._store_reddit_candidates(conn, proj.name, "sauna day pass", json.dumps(payload).encode(), ab.iso(now))
    result = ab.questions_from_candidates(conn, cfg, [proj], now)
    rows = conn.execute("SELECT * FROM questions WHERE source='reddit'").fetchall()
    check("question: reddit metadata clustered with actual counts", result["created"] == 1 and len(rows) == 1 and
          "3 public questions" in rows[0]["why_now"] and len(json.loads(rows[0]["source_refs"])) == 6)
    check("question: source titles scrubbed before persistence", "private-id" not in str([
        dict(r) for r in conn.execute("SELECT * FROM candidates")]))
    check("question: cluster upsert idempotent", ab.questions_from_candidates(conn, cfg, [proj], now)["created"] == 0)
    check("question: author profile URLs never stored", not ab._store_candidate(
        conn, proj.name, "sauna", "reddit", "https://reddit.com/user/private-id", "sauna", None,
        now.timestamp(), 0, 0, ab.iso(now)))
    # A fresh observation of an old thread is not fresh demand.
    conn.execute("UPDATE candidates SET created_utc=?", (ab.iso(now - timedelta(days=30)),))
    stats = ab._question_thread_stats(conn, rows[0], cfg, now)
    check("question: old threads excluded from demand", stats["count"] == 0)
    conn.close()


def test_evidence_validation(tmp_dir):
    cfg, conn, proj, now, _, raw = batch2_fixture(tmp_dir)
    for field in ("fact", "source_url", "verified_at", "valid_until", "allowed_claims", "forbidden_claims"):
        missing = dict(raw)
        missing.pop(field)
        _, errors = ab.validate_evidence(missing, proj, now)
        check("evidence: required " + field, "missing:" + field in errors, str(errors))
    cases = [(dict(raw, valid_until=raw["verified_at"]), "invalid:valid_until"),
             (dict(raw, verified_at=ab.iso(now + timedelta(days=1))), "invalid:verified_at"),
             (dict(raw, verified_at="not-a-date"), "invalid:verified_at"),
             (dict(raw, valid_until=42), "invalid:valid_until"),
             (dict(raw, source_url="file:///tmp/fact"), "invalid:source_url"),
             (dict(raw, source_url={"url": "https://example.test"}), "invalid:source_url"),
             (dict(raw, source_url="https://private:secret@example.test/fact"), "invalid:source_url"),
             (dict(raw, allowed_claims=[]), "missing:allowed_claims"),
             (dict(raw, forbidden_claims=None), "invalid:forbidden_claims"),
             (dict(raw, allowed_claims="untyped string"), "invalid:allowed_claims"),
             (dict(raw, forbidden_claims=raw["allowed_claims"]), "conflict:claims"),
             (dict(raw, allowed_claims=["무조건 확인"]), "banned:allowed_claims"),
             (dict(raw, project="pitchboard"), "project:mismatch")]
    for i, (case, expected) in enumerate(cases):
        _, errors = ab.validate_evidence(case, proj, now)
        check("evidence: invalid case " + str(i), expected in errors, str(errors))
    eid = batch2_evidence(conn, proj, raw, now)
    check("evidence: stable upsert", eid == batch2_evidence(conn, proj, raw, now + timedelta(hours=1)) and
          conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1)
    batch2_write(cfg, "evidence", proj.name, {"project": "pitchboard", "evidence": [raw]})
    check("evidence: mismatched file rejected", ab.import_evidence_dir(conn, cfg, [proj], now) == {"imported": 0, "rejected": 1})
    batch2_write(cfg, "evidence", proj.name, [dict(raw, project="pitchboard"), raw])
    check("evidence: bad item does not prevent valid import", ab.import_evidence_dir(conn, cfg, [proj], now) == {"imported": 1, "rejected": 1})
    with open(os.path.join(cfg["evidence"]["dir"], proj.name + ".json"), "w") as fh:
        fh.write("{not json")
    check("evidence: malformed input rejected", ab.import_evidence_dir(conn, cfg, [proj], now)["rejected"] == 1)
    conn.close()


def test_evidence_expiry(tmp_dir):
    cfg, conn, proj, now, qraw, eraw = batch2_fixture(tmp_dir)
    qid = batch2_question(conn, cfg, proj, qraw, now)
    eid = batch2_evidence(conn, proj, eraw, now)
    ab.link_evidence(conn, eid, "question", qid)
    conn.execute("UPDATE questions SET state='drafted' WHERE id=?", (qid,))
    draft = conn.execute("INSERT INTO drafts (project,page_url,path,channels,created,question_id) VALUES (?,?,?,?,?,?)",
                         (proj.name, "question:fixture", "fixture.md", "threads", ab.iso(now), qid)).lastrowid
    ab.link_evidence(conn, eid, "draft", draft)
    planned = ab.record_post(conn, proj.name, "threads", qraw["target_landing_url"], "fixture", now, evidence_ids=[eid])
    verified = ab.record_post(conn, proj.name, "x", qraw["target_landing_url"], "fixture", now, evidence_ids=[eid])
    ab.mark_published_verified(conn, verified, "https://example.test/public-fixture", now, now)
    before = conn.execute("SELECT COUNT(*) FROM post_events").fetchone()[0]
    expired_now = now + timedelta(days=11)
    # Every date comparison uses instants, not lexicographic offsets.
    conn.execute("UPDATE evidence SET valid_until=? WHERE id=?", (ab.iso(expired_now - timedelta(seconds=1)), eid))
    conn.commit()
    result = ab.expire_evidence(conn, expired_now)
    check("expiry: draft and scheduled post held; question reopened", result == {"expired": 1, "held": 2, "questions_reopened": 1}, str(result))
    check("expiry: evidence status expired", conn.execute("SELECT status FROM evidence WHERE id=?", (eid,)).fetchone()[0] == "expired")
    draft_row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft,)).fetchone()
    post = conn.execute("SELECT * FROM published WHERE post_id=?", (planned,)).fetchone()
    check("expiry: reasons include evidence key", all(r["state"] == "expired_hold" and eraw["id"] in r["hold_reason"]
          for r in (draft_row, post)))
    check("expiry: verified post preserved", conn.execute("SELECT state FROM published WHERE post_id=?", (verified,)).fetchone()[0] == "published_verified")
    check("expiry: exactly two audited holds", conn.execute("SELECT COUNT(*) FROM post_events").fetchone()[0] == before + 2)
    check("expiry: question needs research", conn.execute("SELECT state FROM questions WHERE id=?", (qid,)).fetchone()[0] == "needs_research")
    check("expiry: repeat makes no changes", ab.expire_evidence(conn, expired_now) == {"expired": 0, "held": 0, "questions_reopened": 0})
    before_posts = conn.execute("SELECT COUNT(*) FROM published").fetchone()[0]
    try:
        ab.record_post(conn, proj.name, "instagram", qraw["target_landing_url"], "expired", expired_now, evidence_ids=[eid])
        rejected = False
    except ValueError:
        rejected = True
    check("expiry: expired evidence cannot schedule a new post", rejected and
          conn.execute("SELECT COUNT(*) FROM published").fetchone()[0] == before_posts)
    # Reimporting the same expired JSON must not reactivate it.
    batch2_write(cfg, "evidence", proj.name, [dict(eraw, valid_until=ab.iso(expired_now - timedelta(seconds=1)))])
    ab.import_evidence_dir(conn, cfg, [proj], expired_now)
    check("expiry: expired reimport stays expired", ab.expire_evidence(conn, expired_now)["expired"] == 0)
    # The whole expiry pass, including its audit event, rolls back on failure.
    fresh = batch2_evidence(conn, proj, dict(eraw, id="atomic", valid_until=ab.iso(expired_now + timedelta(days=1))), now)
    d2 = conn.execute("INSERT INTO drafts(project,page_url,path,channels,created) VALUES(?,?,?,?,?)",
                      (proj.name, "question:atomic", "fixture.md", "threads", ab.iso(now))).lastrowid
    ab.link_evidence(conn, fresh, "draft", d2)
    conn.execute("CREATE TEMP TRIGGER reject_expiry BEFORE INSERT ON post_events BEGIN SELECT RAISE(ABORT,'fixture'); END")
    conn.commit()
    try:
        ab.expire_evidence(conn, expired_now + timedelta(days=2))
        rejected = False
    except sqlite3.IntegrityError:
        rejected = True
    check("expiry: transaction rolls back evidence and entity", rejected and conn.execute(
        "SELECT status FROM evidence WHERE id=?", (fresh,)).fetchone()[0] == "active" and conn.execute(
        "SELECT state FROM drafts WHERE id=?", (d2,)).fetchone()[0] == "candidate")
    conn.execute("DROP TRIGGER reject_expiry")
    conn.close()


def test_needs_research_and_no_demand(tmp_dir):
    cfg, conn, proj, now, qraw, eraw = batch2_fixture(tmp_dir)
    qid = batch2_question(conn, cfg, proj, qraw, now)
    ab.run_demand_pipeline(conn, cfg, [proj], now)
    q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    check("research: missing evidence holds question", q["state"] == "needs_research" and q["hold_reason"] == "no_verified_evidence")
    check("research: no draft rows or files", ab.generate_question_drafts(conn, cfg, [proj], now) == 0 and
          conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0 and not os.path.exists(cfg["paths"]["drafts"]))
    batch2_write(cfg, "evidence", proj.name, [eraw])
    ab.run_demand_pipeline(conn, cfg, [proj], now)
    check("research: supplied evidence makes ready", conn.execute("SELECT state FROM questions WHERE id=?", (qid,)).fetchone()[0] == "ready")
    conn.execute("UPDATE questions SET target_landing_url=NULL WHERE id=?", (qid,))
    ab.classify_questions(conn, cfg, [proj], now)
    q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    check("research: missing landing holds question", q["state"] == "needs_research" and q["hold_reason"] == "landing_missing")
    # Recovery resolves only actual crawled pages.
    ab.run_demand_pipeline(conn, cfg, [proj], now)
    check("research: live deep landing resolved", conn.execute("SELECT target_landing_url FROM questions WHERE id=?", (qid,)).fetchone()[0] == qraw["target_landing_url"])
    # Evidence with no demand remains on hold, then recovers when observed demand arrives.
    q2raw = dict(qraw, source="manual", question="Where is the fixture second sauna?", keyword_cluster="fixture-second")
    q2id = batch2_question(conn, cfg, proj, q2raw, now)
    e2raw = dict(eraw, id="fixture-second", keyword_cluster="fixture-second", keywords=["fixture second"])
    eid = batch2_evidence(conn, proj, e2raw, now)
    # Prevent broad first-cluster keyword matches so this evidence has no other demand.
    conn.execute("UPDATE questions SET question='Fixture second option' WHERE id=?", (q2id,))
    ab.run_demand_pipeline(conn, cfg, [proj], now)
    check("demand: question without observations held", conn.execute("SELECT state FROM questions WHERE id=?", (q2id,)).fetchone()[0] == "no_demand_hold")
    check("demand: evidence without demand held", conn.execute("SELECT status FROM evidence WHERE id=?", (eid,)).fetchone()[0] == "no_demand_hold")
    conn.execute("UPDATE questions SET source='search_console' WHERE id=?", (q2id,))
    ab.run_demand_pipeline(conn, cfg, [proj], now)
    check("demand: evidence and question recover", conn.execute("SELECT status FROM evidence WHERE id=?", (eid,)).fetchone()[0] == "active" and
          conn.execute("SELECT state FROM questions WHERE id=?", (q2id,)).fetchone()[0] == "ready")
    e3 = batch2_evidence(conn, proj, dict(eraw, id="unmatched", keywords=[], keyword_cluster="unmatched"), now)
    ab.hold_evidence_without_demand(conn, now)
    check("demand: unlinked evidence held", conn.execute("SELECT status FROM evidence WHERE id=?", (e3,)).fetchone()[0] == "no_demand_hold")
    conn.execute("INSERT INTO crawl_runs(started,project,first_run) VALUES(?,?,0)", (ab.iso(now), proj.name))
    unused = proj.url + "/guides/no-demand"
    conn.execute("INSERT INTO pages(project,url,first_seen,last_seen,last_changed,title) VALUES(?,?,?,?,?,?)",
                 (proj.name, unused, ab.iso(now), ab.iso(now), ab.iso(now), "No demand fixture"))
    ab.generate_drafts(conn, cfg, [proj], now, ab.iso(now))
    row = conn.execute("SELECT * FROM drafts WHERE page_url=?", (unused,)).fetchone()
    check("demand: page draft held but packet retained", row["state"] == "no_demand_hold" and os.path.exists(row["path"]))
    before = conn.execute("SELECT COUNT(*) FROM post_events").fetchone()[0]
    ab.generate_drafts(conn, cfg, [proj], now, ab.iso(now))
    check("demand: page hold is idempotent", conn.execute("SELECT COUNT(*) FROM post_events").fetchone()[0] == before)
    conn.close()


def test_hooks(tmp_dir):
    cfg, conn, proj, now, qraw, eraw = batch2_fixture(tmp_dir)
    qid = batch2_question(conn, cfg, proj, qraw, now)
    eid = batch2_evidence(conn, proj, eraw, now)
    q = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    evidence = conn.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()
    hooks = ab.generate_hooks(q, [evidence], proj, cfg)
    ab.store_hooks(conn, qid, hooks, now)
    ab.store_hooks(conn, qid, hooks, now)
    check("hooks: exact three types with idempotent storage", set(hooks) == set(ab.HOOK_TYPES) and
          conn.execute("SELECT COUNT(*) FROM hooks WHERE question_id=?", (qid,)).fetchone()[0] == 3)
    check("hooks: all ok use approved claims only", all(h["status"] == "ok" and eraw["allowed_claims"][0] in h["text"] and
          eraw["fact"] not in h["text"] and not ab.find_banned_terms(h["text"]) for h in hooks.values()))
    check("hooks: three angles differ", len({h["text"] for h in hooks.values()}) == 3)
    check("hooks: whitespace bait variants detected", set(ab.find_banned_terms("충격! 아무도모르는 무조건")) == {"충격", "아무도 모르는", "무조건"})
    check("hooks: english casefold bait detected", "shocking" in ab.find_banned_terms("SHOCKING"))
    baitq = dict(q, question="충격! 아무도모르는 무조건 sauna", language="ko")
    blocked = ab.generate_hooks(baitq, [evidence], proj, cfg)
    check("hooks: bait blocks all angles with reasons", all(h["status"] == "blocked" and "충격" in h["blocked_terms"] for h in blocked.values()))
    forbidden_q = dict(q, question="Fixture unrestricted entry", language="en")
    forbidden_e = dict(evidence, forbidden_claims=json.dumps(["unrestricted"]))
    check("hooks: evidence forbidden claims blocked", all(h["status"] == "blocked" for h in ab.generate_hooks(forbidden_q, [forbidden_e], proj, cfg).values()))
    check("hooks: English template chosen", hooks["loss_prevention"]["text"].startswith("Before deciding"))
    ko = ab.generate_hooks(dict(q, question="사우나 당일 준비물", language="ko"), [evidence], proj, cfg)
    check("hooks: Korean template chosen", "확인해요" in ko["loss_prevention"]["text"])
    for name, term in (("agamajung", "최저가"), ("kitchengenome", "제자")):
        brand = next(p for p in cfg["_projects"] if p.name == name)
        own = dict(evidence, project=name)
        out = ab.generate_hooks(dict(q, project=name, question=term + " 조건", language="ko"), [own], brand, cfg)
        check("hooks: brand term blocked " + name, all(h["status"] == "blocked" and term in h["blocked_terms"] for h in out.values()))
    kitchen = next(p for p in cfg["_projects"] if p.name == "kitchengenome")
    permitted = dict(evidence, project=kitchen.name, allowed_claims=json.dumps(["Fixture 제자 관계 확인"]))
    check("hooks: sourced mentorship exception", all(h["status"] == "ok" for h in ab.generate_hooks(
        dict(q, project=kitchen.name, question="셰프 경력", language="ko"), [permitted], kitchen, cfg).values()))
    foreign = dict(evidence, project="pitchboard")
    check("hooks: another brand cannot supply claim", all(h["status"] == "blocked" and not h["evidence_ids"] for h in ab.generate_hooks(q, [foreign], proj, cfg).values()))
    other = next(p for p in cfg["_projects"] if p.name == "pitchboard")
    other_eid = batch2_evidence(conn, other, dict(eraw, project=other.name), now)
    check("hooks: evidence matching isolates brand", not any(e["id"] == other_eid for e in ab.match_evidence(conn, q)))
    try:
        ab.link_evidence(conn, other_eid, "question", qid)
        rejected = False
    except ValueError:
        rejected = True
    check("hooks: direct cross-brand link rejected", rejected)
    ab.link_evidence(conn, eid, "question", qid)
    conn.execute("UPDATE questions SET state='ready',question=? WHERE id=?", (baitq["question"], qid))
    check("hooks: no files or rows for all-blocked question", ab.generate_question_drafts(conn, cfg, [proj], now) == 0 and
          conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0 and not os.path.exists(cfg["paths"]["drafts"]))
    conn.close()


def test_question_draft_packet(tmp_dir):
    cfg, conn, proj, now, qraw, eraw = batch2_fixture(tmp_dir)
    batch2_write(cfg, "questions", proj.name, [qraw])
    batch2_write(cfg, "evidence", proj.name, [eraw])
    report = ab.run_demand_pipeline(conn, cfg, [proj], now)
    check("pipeline: imports and classifies offline", report["states"] == {"ready": 1}, str(report))
    check("packet: creates one question draft", ab.generate_question_drafts(conn, cfg, [proj], now) == 1)
    row = conn.execute("SELECT * FROM drafts WHERE question_id IS NOT NULL").fetchone()
    with open(row["path"], encoding="utf-8") as fh:
        packet = fh.read()
    check("packet: header and required metadata", packet.startswith(ab.DRAFT_HEADER) and all(
        k in packet for k in ("evidence_ids", "why_this_channel", "why_now", "검색량 미확인", "target_landing_url")))
    check("packet: all hook comparisons", "## hooks (compare)" in packet and all(t in packet for t in ab.HOOK_TYPES))
    check("packet: evidence dates and source", all(eraw[k] in packet for k in ("fact", "source_url", "verified_at", "valid_until")))
    check("packet: deep URL and tracked hook", urllib.parse.urlsplit(row["landing_url"]).netloc == proj.host and
          urllib.parse.urlsplit(row["landing_url"]).path != "/" and "utm_content=loss_prevention" in packet)
    check("packet: reddit no site link", "soulbound.co.kr" not in code_block(packet, "reddit") and
          "https://" not in code_block(packet, "reddit"))
    check("packet: X limit and channel variety", len(code_block(packet, "x")) <= 280 and
          len({code_block(packet, ch) for ch in cfg["channels"]}) == 4)
    check("packet: database metadata and evidence links", row["page_url"].startswith("question:") and row["hook_type"] == "loss_prevention" and
          row["why_this_channel"] and row["why_now"] and len(ab.linked_evidence(conn, "draft", row["id"])) == 1)
    ab.run_demand_pipeline(conn, cfg, [proj], now)
    check("pipeline: repeat creates no duplicate drafts/hooks", ab.generate_question_drafts(conn, cfg, [proj], now) == 0 and
          conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1 and conn.execute("SELECT COUNT(*) FROM hooks").fetchone()[0] == 3)
    # Question generation has its own per-project cap, applied after blocked items.
    for i in range(3):
        batch2_question(conn, cfg, proj, dict(qraw, question=f"Which sauna fixture {i}?"), now)
    cfg["limits"]["max_question_drafts_per_run"] = 2
    ab.run_demand_pipeline(conn, cfg, [proj], now)
    check("packet: per-project run cap", ab.generate_question_drafts(conn, cfg, [proj], now) == 2)
    status = ab.collect_status(conn, cfg, now)
    check("status: demand counters exposed", status["projects"][proj.name]["questions"]["drafted"] == 3 and
          status["projects"][proj.name]["evidence"]["active"] == 1 and
          "demand: hanset" in ab.render_status_md(status, cfg, now, "test"))
    conn.close()


def test_config_batch2(tmp_dir):
    cfg = make_cfg(tmp_dir)
    check("config: five brand rule keys match projects", set(ab.BRAND_RULES) == {p.name for p in cfg["_projects"]})
    check("config: every brand has demand clusters and tone", all(p.demand["clusters"] and p.demand["tone"] for p in cfg["_projects"]))
    check("config: core bait terms", {"충격", "아무도 모르는", "무조건"} <= set(cfg["hooks"]["banned_terms"]))
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        legacy = json.load(fh)
    for key in ("demand", "evidence", "questions", "hooks"):
        legacy.pop(key, None)
    for p in legacy["projects"]:
        p.pop("demand", None)
    path = os.path.join(tmp_dir, "legacy.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)
    old_cfg = ab.load_config(path)
    check("config: legacy config fills Batch 2 defaults", old_cfg["demand"]["search_volume_default"] == "검색량 미확인" and
          old_cfg["hooks"]["default"] == "loss_prevention" and all(p.demand["clusters"] for p in old_cfg["_projects"]))
    legacy["projects"][0]["demand"] = {"tone": "custom brand tone"}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)
    check("config: brand override retained", ab.load_config(path)["_projects"][0].demand["tone"] == "custom brand tone")
    conn = ab.db_connect(cfg)
    # Rebuild only old-format tables; the migration must retain their data.
    conn.executescript("DROP TABLE drafts; CREATE TABLE drafts(id INTEGER PRIMARY KEY,project TEXT,page_url TEXT,"
                       "path TEXT,channels TEXT,created TEXT,UNIQUE(project,page_url));"
                       "INSERT INTO drafts VALUES(1,'hanset','old','old.md','x','old');")
    conn.close()
    conn = ab.db_connect(cfg)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(drafts)")}
    check("config: legacy draft migration adds all fields", {"question_id", "landing_url", "hook_type", "evidence_ids",
          "why_this_channel", "why_now"} <= cols and conn.execute("SELECT path FROM drafts WHERE id=1").fetchone()[0] == "old.md")
    check("config: published migration fields", {"question_id", "hook_type"} <=
          {r["name"] for r in conn.execute("PRAGMA table_info(published)")})
    check("cli: no-demand flag", ab.build_parser().parse_args(["run", "--no-demand"]).no_demand)
    conn.close()


def test_batch2_cli_lifecycle(tmp_dir):
    from contextlib import redirect_stdout
    from io import StringIO
    from unittest.mock import patch

    cfg, conn, proj, now, qraw, eraw = batch2_fixture(tmp_dir)
    batch2_write(cfg, "questions", proj.name, [qraw])
    batch2_write(cfg, "evidence", proj.name, [eraw])
    path = os.path.join(tmp_dir, "cli.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in cfg.items() if not k.startswith("_")}, fh, ensure_ascii=False)
    conn.commit()
    conn.close()
    args = ["--config", path, "run", "--project", proj.name, "--no-crawl", "--no-discover", "--no-metrics"]
    with patch.object(ab, "now_kst", return_value=now), patch.object(ab, "setup_logging"), redirect_stdout(StringIO()):
        check("cli: local run can skip demand", ab.main(args + ["--no-demand"]) == 0)
        conn = ab.db_connect(cfg)
        check("cli: no-demand skips imports and question drafts", conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0 and
              conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0)
        conn.close()
        check("cli: local demand run succeeds", ab.main(args) == 0)
    conn = ab.db_connect(cfg)
    draft = conn.execute("SELECT * FROM drafts").fetchone()
    check("cli: demand pipeline runs before question draft generation", draft is not None and draft["question_id"] is not None)
    eid = conn.execute("SELECT id FROM evidence").fetchone()[0]
    post_id = ab.record_post(conn, proj.name, "threads", qraw["target_landing_url"], "cli fixture", now, evidence_ids=[eid])
    conn.close()
    late = now + timedelta(days=11)
    with patch.object(ab, "now_kst", return_value=late), patch.object(ab, "setup_logging"), redirect_stdout(StringIO()):
        check("cli: status applies local expiry", ab.main(["--config", path, "status"]) == 0)
    conn = ab.db_connect(cfg)
    check("cli: status held both question draft and reservation", conn.execute("SELECT state FROM drafts WHERE id=?", (draft["id"],)).fetchone()[0] == "expired_hold" and
          conn.execute("SELECT state FROM published WHERE post_id=?", (post_id,)).fetchone()[0] == "expired_hold")
    # A renewed human input reopens the question draft, preserving its identity and requiring review again.
    batch2_write(cfg, "evidence", proj.name, [dict(eraw, verified_at=ab.iso(late), valid_until=ab.iso(late + timedelta(days=10)))])
    ab.run_demand_pipeline(conn, cfg, [proj], late)
    check("lifecycle: renewed evidence restores a ready question", conn.execute("SELECT state FROM questions").fetchone()[0] == "ready")
    check("lifecycle: refreshed draft reuses the existing row", ab.generate_question_drafts(conn, cfg, [proj], late) == 1 and
          conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1 and
          conn.execute("SELECT state FROM drafts WHERE id=?", (draft["id"],)).fetchone()[0] == "candidate")
    check("lifecycle: refreshed evidence does not resume a held reservation", conn.execute(
        "SELECT state FROM published WHERE post_id=?", (post_id,)).fetchone()[0] == "expired_hold")
    # Expiring one item still stops drafts that used it, even if the question has another active answer.
    qid = draft["question_id"]
    second = batch2_evidence(conn, proj, dict(eraw, id="second-answer", valid_until=ab.iso(late + timedelta(days=30))), late)
    ab.link_evidence(conn, second, "question", qid)
    result = ab.expire_evidence(conn, late + timedelta(days=11))
    check("lifecycle: remaining evidence preserves question while old draft is held", result["held"] == 1 and result["questions_reopened"] == 0 and
          conn.execute("SELECT state FROM questions WHERE id=?", (qid,)).fetchone()[0] == "drafted")
    conn.close()


# ---------------------------------------------------------------- Batch 3
def batch3_fixture(tmp_dir):
    cfg, conn, proj, now, qraw, eraw = batch2_fixture(tmp_dir)
    cfg["metrics"]["dir"] = os.path.join(tmp_dir, "metrics")
    batch2_write(cfg, "questions", proj.name, [qraw])
    batch2_write(cfg, "evidence", proj.name, [eraw])
    ab.run_demand_pipeline(conn, cfg, [proj], now)
    ab.generate_question_drafts(conn, cfg, [proj], now)
    draft = conn.execute("SELECT * FROM drafts WHERE question_id IS NOT NULL").fetchone()
    return cfg, conn, proj, now, draft


def batch3_rejects(name, action):
    try:
        action()
    except ValueError:
        check(name, True)
    else:
        check(name, False, "expected ValueError")


def batch3_cli(cfg, now, *args):
    path = os.path.join(os.path.dirname(cfg["paths"]["db"]), "cli-config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({k: v for k, v in cfg.items() if not k.startswith("_")}, fh)
    stdout, stderr = io.StringIO(), io.StringIO()
    clock = ab.now_kst
    try:
        ab.now_kst = lambda: now
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = ab.main(["--config", path, *args])
    finally:
        ab.now_kst = clock
    return rc, stdout.getvalue(), stderr.getvalue()


def test_channel_formats_batch3(tmp_dir):
    cfg, conn, proj, now, draft = batch3_fixture(tmp_dir)
    with open(draft["path"], encoding="utf-8") as fh:
        packet = fh.read()
    channels = ("search_page", "instagram", "threads", "reddit")
    blocks = {ch: code_block(packet, ch) for ch in channels}
    check("batch3 formats: four distinct bodies", len(set(blocks.values())) == 4)
    for ch in channels:
        section = packet.split(f"\n## {ch}\n", 1)[1].split("\n## ", 1)[0]
        check(f"batch3 formats: {ch} metadata", all(f"- {key}: " in section for key in
              ("why_this_channel", "why_now", "evidence_ids", "channel")))
        check(f"batch3 formats: {ch} reason specific", ab._why_this_channel(
            {"language": "en", "natural_channel": "reddit"}, ch) in section)
    check("batch3 formats: durable search answer", all(x in blocks["search_page"] for x in
          ("slug:", "canonical:", "질문", "답", "근거", "source_url:", "verified_at:", "valid_until:")))
    check("batch3 formats: card checklist", all(x in blocks["instagram"] for x in
          ("[CARD 1/3]", "[CARD 2/3]", "[CARD 3/3]", "☐", "저장·공유")))
    check("batch3 formats: three threads blocks", len(blocks["threads"].split("\n\n")) == 3 and
          all(x in blocks["threads"] for x in ("의견 / Opinion:", "근거 / Evidence:", "질문 / Question:")))
    check("batch3 formats: reddit answer and review gates", all(x in blocks["reddit"] for x in
          ("Direct answer:", "홍보 허용", "관계 표시")) and "http" not in blocks["reddit"])
    check("batch3 formats: draft metadata", all(draft[k] for k in
          ("why_this_channel", "why_now", "evidence_ids", "asset_id", "keyword_cluster", "campaign_id")) and
          set(draft["channels"].split(",")) == set(ab.QUESTION_PACKET_CHANNELS))
    for ch in ("search_page", "threads", "instagram"):
        link = next(url.rstrip() for url in ab.ABS_URL_RE.findall(blocks[ch]) if "utm_source=" in url)
        utm = ab.parse_utm_attribution(link)
        check(f"batch3 attribution: {ch} complete UTM", utm["utm_source"] == ch and
              utm["utm_asset"] == draft["asset_id"] and utm["utm_brand"] == proj.name and
              utm["utm_term"] == draft["keyword_cluster"] and utm["utm_campaign"] == draft["campaign_id"] and
              utm["utm_content"] == draft["hook_type"])
    q = conn.execute("SELECT * FROM questions WHERE id=?", (draft["question_id"],)).fetchone()
    evidence = dict(ab.linked_evidence(conn, "draft", draft["id"])[0])
    hooks = ab.generate_hooks(q, [evidence], proj, cfg)
    for kind, label in (("career", "경력 지도"), ("comparison", "비교 카드")):
        evidence["kind"] = kind
        variant = ab.render_question_packet(proj, q, [evidence], hooks, cfg, now)
        check(f"batch3 formats: instagram {kind}", label in code_block(variant, "instagram"))
    evidence["allowed_claims"] = json.dumps(["Verified condition https://example.test/a and example.test/b"])
    variant = ab.render_question_packet(proj, q, [evidence], hooks, cfg, now)
    check("batch3 formats: reddit removes URLs from claims", "example.test" not in code_block(variant, "reddit"))
    # Legacy page candidates also carry attribution and queue-required fields.
    conn.execute("INSERT INTO crawl_runs(started,project,first_run) VALUES (?, ?, 0)", (ab.iso(now), proj.name))
    ab.generate_drafts(conn, cfg, [proj], now, ab.iso(now))
    page = conn.execute("SELECT * FROM drafts WHERE question_id IS NULL").fetchone()
    check("batch3 formats: page candidate metadata", page is not None and all(page[k] for k in
          ("asset_id", "keyword_cluster", "campaign_id", "hook_type", "why_this_channel", "why_now", "evidence_ids")))
    with open(page["path"], encoding="utf-8") as fh:
        page_packet = fh.read()
    check("batch3 formats: page packet metadata", all(k in page_packet for k in
          ("brand:", "asset_id:", "keyword_cluster:", "campaign_id:", "why_this_channel:", "evidence_ids:")))
    conn.close()


def test_approval_queue(tmp_dir):
    cfg, conn, proj, now, draft = batch3_fixture(tmp_dir)
    post_id = ab.record_post(conn, proj.name, "threads", draft["landing_url"], "queue fixture", now,
                             state="candidate", draft_id=draft["id"])
    post = conn.execute("SELECT * FROM published WHERE post_id=?", (post_id,)).fetchone()
    check("batch3 queue: linked post inherits metadata", all(post[k] == draft[k] for k in
          ("asset_id", "keyword_cluster", "campaign_id", "hook_type", "question_id", "evidence_ids")) and
          "threads" in post["why_this_channel"] and post["why_now"] == draft["why_now"])
    check("batch3 queue: filters", len(ab.queue_list(conn, proj.name, "candidate")) == 2 and
          len(ab.queue_list(conn, entity="draft")) == 1 and len(ab.queue_list(conn, entity="post")) == 1 and
          ab.queue_list(conn, project="goallit") == [] and ab.queue_list(conn, state="evaluated") == [])
    batch3_rejects("batch3 queue: rejects unknown entity", lambda: ab.queue_list(conn, entity="evidence"))
    batch3_rejects("batch3 queue: rejects unknown state", lambda: ab.queue_list(conn, state="made_up"))
    batch3_rejects("batch3 queue: rejects state skip", lambda: ab.transition(conn, "post", post["id"], "scheduled", now))
    batch3_rejects("batch3 queue: brand isolation", lambda: ab.record_post(
        conn, "goallit", "threads", draft["landing_url"], "wrong brand", now, draft_id=draft["id"]))
    check("batch3 queue: invalid work not inserted", conn.execute("SELECT COUNT(*) FROM published").fetchone()[0] == 1)
    # Drafts expose the entire workflow, with each advance audited exactly once.
    for state in ab.POST_FLOW[1:]:
        ab.transition(conn, "draft", draft["id"], state, now)
    check("batch3 queue: complete draft chain", ab.queue_list(conn, state="evaluated", entity="draft")[0]["id"] == draft["id"] and
          conn.execute("SELECT COUNT(*) FROM post_events WHERE entity='draft'").fetchone()[0] == 6)
    conn.commit()
    rc, out, _ = batch3_cli(cfg, now, "queue", "list", "--project", proj.name, "--state", "candidate")
    check("batch3 queue CLI: list", rc == 0 and post_id in out and "evaluated" not in out)
    for state in ("fact_checked", "approved", "scheduled"):
        rc, _, _ = batch3_cli(cfg, now, "queue", "move", "--entity", "post", "--id", str(post["id"]), "--to", state)
        check(f"batch3 queue CLI: {state}", rc == 0)
    rc, _, err = batch3_cli(cfg, now, "queue", "move", "--entity", "post", "--id", str(post["id"]), "--to", "published_verified")
    check("batch3 queue CLI: cannot assert publication without URL", rc == 2 and "queue verify" in err and
          conn.execute("SELECT COUNT(*) FROM metric_checks").fetchone()[0] == 0)
    rc, _, _ = batch3_cli(cfg, now, "queue", "verify", "--post-id", post_id, "--url", "https://example.test/post",
                          "--verified-at", "invalid")
    check("batch3 queue CLI: invalid verification date rejected", rc == 2)
    rc, _, _ = batch3_cli(cfg, now, "queue", "verify", "--post-id", post_id, "--url", "https://example.test/post",
                          "--verified-at", ab.iso(now + timedelta(hours=1)))
    check("batch3 queue CLI: future verification rejected", rc == 2)
    for attempt in range(2):
        rc, _, _ = batch3_cli(cfg, now, "queue", "verify", "--post-id", post_id, "--url", "https://example.test/post",
                              "--verified-at", ab.iso(now))
        check(f"batch3 queue CLI: verify attempt {attempt}", rc == 0)
    checks = {r["check_kind"]: r["due_at"] for r in conn.execute("SELECT * FROM metric_checks")}
    check("batch3 queue: verification queues two checks once", checks == {
        "48h": ab.iso(now + timedelta(hours=48)), "7d": ab.iso(now + timedelta(days=7))})
    for state in ("measured", "evaluated"):
        rc, _, _ = batch3_cli(cfg, now, "queue", "move", "--entity", "post", "--id", str(post["id"]), "--to", state)
        check(f"batch3 queue CLI: {state}", rc == 0)
    check("batch3 queue: complete post chain audited", conn.execute(
        "SELECT COUNT(*) FROM post_events WHERE entity='post'").fetchone()[0] == 6)
    rc, _, _ = batch3_cli(cfg, now, "queue", "move", "--entity", "post", "--id", str(post["id"]), "--to", "approved")
    check("batch3 queue CLI: backwards transition rejected", rc == 2)
    expiring = ab.record_post(conn, proj.name, "reddit", draft["landing_url"], "expiry fixture", now, draft_id=draft["id"])
    late = now + timedelta(days=11)
    rc, _, _ = batch3_cli(cfg, late, "queue", "verify", "--post-id", expiring, "--url", "https://example.test/expired")
    held = conn.execute("SELECT * FROM published WHERE post_id=?", (expiring,)).fetchone()
    check("batch3 queue CLI: expired evidence blocks verification", rc == 2 and held["state"] == "expired_hold" and
          held["verified_at"] is None and held["hold_reason"].startswith("evidence expired:"))
    conn.close()


def test_metrics_three_tiers(tmp_dir):
    cfg, conn, proj, now, draft = batch3_fixture(tmp_dir)
    post_id = ab.record_post(conn, proj.name, "threads", draft["landing_url"], "metrics fixture", now,
                             draft_id=draft["id"])
    for tier, metric in ((1, "views"), (2, "saves"), (3, "confirmed_purchases")):
        mid = ab.record_metric(conn, tier, metric, 12, now, post_id=post_id, attribution="threads")
        row = conn.execute("SELECT * FROM metric_observations WHERE id=?", (mid,)).fetchone()
        check(f"batch3 metrics: tier {tier} inherits content", row["brand"] == proj.name and
              row["post_id"] == post_id and row["channel"] == "threads" and all(row[k] == draft[k] for k in
              ("asset_id", "keyword_cluster", "hook_type", "campaign_id")))
    unknown_id = ab.record_metric(conn, 3, "resource_views", None, now, note="미확인")
    unknown = conn.execute("SELECT * FROM metric_observations WHERE id=?", (unknown_id,)).fetchone()
    check("batch3 metrics: honest unknown traffic/value", unknown["attribution"] == ab.DIRECT_UNKNOWN and
          unknown["brand"] is None and unknown["value"] is None and unknown["note"] == "미확인")
    check("batch3 metrics: dedupe manual observations", ab.record_metric(
        conn, 3, "resource_views", None, now, note="미확인") is None)
    for tier, metrics in ab.TIER_METRICS.items():
        for metric in metrics:
            check(f"batch3 metrics: supports {tier}/{metric}", ab.record_metric(
                conn, tier, metric, 0, now, brand="goallit", window="48h") is not None)
    bad_calls = [dict(tier=2, metric="views", value=1), dict(tier=4, metric="views", value=1),
                 dict(tier=True, metric="views", value=1), dict(tier=1, metric="views", value=True),
                 dict(tier=1, metric="views", value="9"), dict(tier=1, metric="views", value=float("nan")),
                 dict(tier=1, metric="views", value=float("inf")), dict(tier=1, metric="views", value=-1),
                 dict(tier=1, metric="views", value=1, observed_at="bad"),
                 dict(tier=1, metric="views", value=1, observed_at=ab.iso(now + timedelta(days=1))),
                 dict(tier=1, metric="views", value=1, entity="other"),
                 dict(tier=1, metric="views", value=1, entity="post", entity_id=99999),
                 dict(tier=1, metric="views", value=1, post_id=post_id, brand="goallit"),
                 dict(tier=1, metric="views", value=1, post_id=post_id, campaign_id="other"),
                 dict(tier=1, metric="views", value=1, source="invented")]
    for i, args in enumerate(bad_calls):
        batch3_rejects(f"batch3 metrics: rejects invalid input {i}", lambda args=args: ab.record_metric(conn, now=now, **args))
    check("batch3 metrics: negative reddit score is valid", ab.record_metric(conn, 1, "score", -2, now) is not None)
    conn.commit()
    tracked = ab.add_utm(draft["landing_url"], "instagram", cfg["campaign"], draft["hook_type"],
                         draft["asset_id"], draft["keyword_cluster"], proj.name)
    entries = [dict(tier=2, metric="reach", value=30, post_id=post_id, observed_at=ab.iso(now), window="7d"),
               dict(tier=3, metric="signups", value=2, landing_url=tracked, observed_at=ab.iso(now)),
               dict(tier=3, metric="revisits", value=1, observed_at=ab.iso(now)),
               dict(tier=3, metric="resource_views", value=None, landing_url=draft["landing_url"], observed_at=ab.iso(now)),
               dict(tier=2, metric="saves", value=4, entity="draft", entity_id=draft["id"], observed_at=ab.iso(now)),
               dict(tier=1, metric="likes", value=1),
               dict(tier=3, metric="likes", value=1, observed_at=ab.iso(now)),
               dict(tier=1, metric="likes", value=1, observed_at=ab.iso(now), post_id="missing"),
               dict(tier=1, metric="likes", value=1, observed_at=ab.iso(now), brand="goallit"),
               dict(tier=1, metric="likes", value=1, observed_at=ab.iso(now), landing_url=cfg["_projects"][1].url + "/review"),
               None]
    batch2_write(cfg, "metrics", proj.name, {"project": proj.name, "metrics": entries})
    report = ab.import_metrics_dir(conn, cfg, [proj], now)
    check("batch3 metrics: JSON envelope and rejection counts", report == {"imported": 5, "rejected": 6}, str(report))
    check("batch3 metrics: JSON repeat idempotent across runs", ab.import_metrics_dir(
        conn, cfg, [proj], now + timedelta(hours=1)) == {"imported": 0, "rejected": 6})
    imported = conn.execute("SELECT * FROM metric_observations WHERE metric='signups' AND source='json_import'").fetchone()
    check("batch3 metrics: UTM attribution", imported["attribution"] == "instagram" and imported["brand"] == proj.name and
          imported["asset_id"] == draft["asset_id"] and imported["channel"] == "instagram")
    direct = conn.execute("SELECT * FROM metric_observations WHERE metric='revisits' AND source='json_import'").fetchone()
    check("batch3 metrics: JSON unknown does not inherit file brand", direct["brand"] is None and direct["attribution"] == ab.DIRECT_UNKNOWN)
    landing_only = conn.execute("SELECT * FROM metric_observations WHERE metric='resource_views' AND source='json_import'").fetchone()
    check("batch3 metrics: landing domain does not invent acquisition", landing_only["brand"] == proj.name and
          landing_only["channel"] is None and landing_only["attribution"] == ab.DIRECT_UNKNOWN and landing_only["value"] is None)
    batch2_write(cfg, "metrics", proj.name, {"project": "goallit", "metrics": entries})
    check("batch3 metrics: wrong envelope rejected", ab.import_metrics_dir(conn, cfg, [proj], now) == {"imported": 0, "rejected": 1})
    other_id = ab.record_post(conn, "goallit", "threads", "https://example.test/other", "other", now)
    batch2_write(cfg, "metrics", proj.name, [dict(tier=1, metric="likes", value=1, post_id=other_id, observed_at=ab.iso(now))])
    check("batch3 metrics: foreign post rejected", ab.import_metrics_dir(conn, cfg, [proj], now) == {"imported": 0, "rejected": 1})
    batch2_write(cfg, "metrics", proj.name, entries[:1])
    rc, out, _ = batch3_cli(cfg, now, "metrics", "import", "--project", proj.name)
    check("batch3 metrics CLI: import local list", rc == 0 and json.loads(out)["imported"] == 0)
    rc, out, _ = batch3_cli(cfg, now, "metrics", "record", "--tier", "2", "--metric", "shares", "--value", "5", "--post-id", post_id)
    check("batch3 metrics CLI: manual input", rc == 0 and json.loads(out)["imported"] == 1)
    rc, out, _ = batch3_cli(cfg, now, "metrics", "record", "--tier", "3", "--metric", "saves", "--value", "null")
    check("batch3 metrics CLI: null input", rc == 0 and json.loads(out)["id"] is not None)
    rc, _, _ = batch3_cli(cfg, now, "metrics", "record", "--tier", "2", "--metric", "likes", "--value", "3")
    check("batch3 metrics CLI: wrong tier rejected", rc == 2)
    rc, out, _ = batch3_cli(cfg, now, "metrics", "summary", "--project", proj.name)
    check("batch3 metrics CLI: summary", rc == 0 and set(json.loads(out)["brands"]) == {proj.name})
    malformed = [dict(tier=1, metric="likes", value=1, observed_at=ab.iso(now), post_id={"id": post_id}),
                 dict(tier=1, metric="likes", value=1, observed_at=ab.iso(now), entity="draft", entity_id=float(draft["id"])),
                 dict(tier=1, metric="likes", value=1, observed_at=ab.iso(now), entity_id=[]),
                 dict(tier=1, metric="likes", value=1, observed_at=ab.iso(now), entity=[]),
                 dict(tier=1, metric=["likes"], value=1, observed_at=ab.iso(now)),
                 dict(tier=3, metric="signups", value=1, observed_at=ab.iso(now),
                      landing_url=tracked.replace("utm_brand=hanset", "utm_brand=goallit"))]
    batch2_write(cfg, "metrics", proj.name, malformed + [dict(tier=3, metric="revisits", value=8, observed_at=ab.iso(now))])
    check("batch3 metrics: malformed identifiers isolated per entry", ab.import_metrics_dir(conn, cfg, [proj], now) ==
          {"imported": 1, "rejected": len(malformed)})
    # Both operational commands import locally, even with every network phase skipped.
    for command, value in (("run", 9), ("status", 10)):
        batch2_write(cfg, "metrics", proj.name, [dict(tier=3, metric="resource_views", value=value, observed_at=ab.iso(now))])
        args = [command]
        if command == "run":
            args += ["--no-crawl", "--no-discover", "--no-demand", "--no-drafts", "--project", proj.name]
        rc, _, _ = batch3_cli(cfg, now, *args)
        check(f"batch3 metrics: {command} imports local insights", rc == 0 and conn.execute(
            "SELECT id FROM metric_observations WHERE source='json_import' AND metric='resource_views' AND value=?",
            (value,)).fetchone() is not None)
    # Only genuine Reddit counters become tier-1 insights; HTTP status is liveness.
    reddit = ab.record_post(conn, proj.name, "reddit", draft["landing_url"], "reddit fixture", now, draft_id=draft["id"])
    ab.mark_published_verified(conn, reddit, "https://www.reddit.com/r/test/comments/fixture/", now - timedelta(days=8), now)
    ab.mark_published_verified(conn, post_id, "https://example.test/post", now - timedelta(days=8), now)

    class StubFetcher:
        def can_fetch(self, url):
            return True

        def get(self, url, **kwargs):
            payload = [{"data": {"children": [{"data": {"score": -3, "num_comments": 4}}]}}]
            return ab.FetchResult(url, 200, json.dumps(payload).encode(), None, None, 0)

    ab.run_due_checks(conn, StubFetcher(), cfg, now)
    rows = conn.execute("SELECT * FROM metric_observations WHERE source='reddit_json'").fetchall()
    check("batch3 metrics: reddit automatic tier 1", len(rows) == 4 and {r["window"] for r in rows} == {"48h", "7d"} and
          {r["metric"] for r in rows} == {"score", "comments"} and all(r["tier"] == 1 and r["post_id"] == reddit for r in rows))
    ab.run_due_checks(conn, StubFetcher(), cfg, now + timedelta(hours=1))
    check("batch3 metrics: checks do not duplicate insights", conn.execute(
        "SELECT COUNT(*) FROM metric_observations WHERE source='reddit_json'").fetchone()[0] == 4)
    summary = ab.metrics_summary(conn, proj.name)
    stats = summary["brands"][proj.name]
    check("batch3 metrics: summary all tiers", all(stats["counts"][tier] > 0 for tier in (1, 2, 3)) and
          stats["tiers"][1]["score"]["sum"] == -6 and stats["tiers"][3]["resource_views"]["sum"] is None)
    status = ab.collect_status(conn, cfg, now)
    check("batch3 metrics: status counts and unknown", status["projects"][proj.name]["tier_metrics"] == stats["counts"] and
          status["unattributed"] > 0 and "metrics: hanset — t1 n=" in ab.render_status_md(status, cfg, now, "test") and
          ab.DIRECT_UNKNOWN in ab.render_status_md(status, cfg, now, "test"))
    conn.close()


def test_config_batch3(tmp_dir):
    cfg = make_cfg(tmp_dir)
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        raw = json.load(fh)
    raw.pop("metrics", None)
    path = os.path.join(tmp_dir, "legacy-config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    check("batch3 config: legacy metrics defaults", ab.load_config(path)["metrics"]["dir"] == "data/metrics")
    conn = ab.db_connect(cfg)
    conn.executescript("DROP TABLE drafts; DROP TABLE metric_observations; "
                       "CREATE TABLE drafts(id INTEGER PRIMARY KEY,project TEXT,page_url TEXT,path TEXT,channels TEXT,"
                       "created TEXT,UNIQUE(project,page_url));"
                       "INSERT INTO drafts VALUES(1,'hanset','https://example.test/a','old.md','x','old');")
    conn.close()
    conn = ab.db_connect(cfg)
    migrated = conn.execute("SELECT * FROM drafts").fetchone()
    check("batch3 migration: preserved data and backfilled metadata", migrated["path"] == "old.md" and
          migrated["asset_id"] and migrated["campaign_id"] == cfg["campaign"] and migrated["keyword_cluster"] == ab.UNKNOWN)
    check("batch3 migration: new published columns", {"asset_id", "campaign_id", "keyword_cluster", "evidence_ids", "why_now", "why_this_channel"} <=
          {r["name"] for r in conn.execute("PRAGMA table_info(published)")})
    mid = ab.record_metric(conn, 3, "signups", 1, ab.now_kst())
    conn.commit()
    conn.close()
    conn = ab.db_connect(cfg)
    check("batch3 migration: repeated upgrade retains ledger", conn.execute("SELECT * FROM metric_observations WHERE id=?", (mid,)).fetchone() is not None and
          conn.execute("SELECT asset_id FROM drafts").fetchone()[0] == migrated["asset_id"])
    check("batch3 UTM: no source stays unknown", ab.parse_utm_attribution(None) is None and
          ab.parse_utm_attribution("https://example.test/a?utm_campaign=fixture") is None and
          ab.parse_utm_attribution("https://example.test/a?utm_source=") is None)
    check("batch3 UTM: parse explicit source", ab.parse_utm_attribution("https://example.test/a?utm_source=threads&utm_campaign=fixture")["utm_source"] == "threads")
    check("batch3 asset: deterministic and brand isolated", ab.make_asset_id("draft", "hanset", "a") == ab.make_asset_id("draft", "hanset", "a") and
          ab.make_asset_id("draft", "hanset", "a") != ab.make_asset_id("draft", "goallit", "a"))
    conn.close()


# ---- Batch 4

def batch4_fixture(tmp_dir):
    cfg = make_cfg(tmp_dir, rounds_text="")
    cfg["paths"]["rules"] = os.path.join(tmp_dir, "rules.json")
    cfg["metrics"]["dir"] = os.path.join(tmp_dir, "metrics")
    now = datetime(2026, 1, 1, 10, tzinfo=ab.KST)
    return cfg, ab.db_connect(cfg), now


def batch4_post(conn, now, label, project="hanset", hook="loss_prevention", channel="threads",
                target="https://hanset.soulbound.co.kr/sauna"):
    # The ledger's identity is the landing URL, so distinct publications get IDs
    # before assigning their common, controlled destination.
    post_id = ab.record_post(conn, project, channel, target + "?publication=" + label, label, now, hook_type=hook)
    ab.mark_published_verified(conn, post_id, "https://example.test/posts/" + label, now, now)
    with conn:
        conn.execute("UPDATE published SET target_url=? WHERE post_id=?", (target, post_id))
    return post_id


def batch4_metrics(conn, post_id, now, window="7d", **values):
    mapping = {"reach": (2, "reach"), "clicks": (2, "link_clicks"), "engaged": (3, "resource_views"),
               "signups": (3, "signups"), "revisits": (3, "revisits"), "views": (1, "views")}
    with conn:
        for name, value in values.items():
            tier, metric = mapping[name]
            ab.record_metric(conn, tier, metric, value, now, post_id=post_id, window=window)


def batch4_experiment(conn, cfg, now, key, project="hanset", nums=(10, 5), early=(10, 5)):
    units = [{"unit_key": "a", "variable_value": "loss_prevention"},
             {"unit_key": "b", "variable_value": "discovery"}]
    eid = ab.create_experiment(conn, cfg, project, key, "hook_type", "훅의 가입률 비교", units, now)
    for i, unit in enumerate(units):
        pid = batch4_post(conn, now + timedelta(days=1), key + unit["unit_key"], project, unit["variable_value"])
        ab.attach_experiment_post(conn, cfg, eid, unit["unit_key"], pid, now + timedelta(days=1))
        for window, counts in (("7d", nums), ("48h", early)):
            batch4_metrics(conn, pid, now + timedelta(days=10), window,
                           reach=1000, clicks=200, engaged=100, signups=counts[i], revisits=5)
    return eid, ab.evaluate_experiment(conn, cfg, eid, now + timedelta(days=14))


def test_experiment_create_batch4(tmp_dir):
    cfg, conn, now = batch4_fixture(tmp_dir)
    six = [{"unit_key": "u" + str(i), "variable_value": "channel" + str(i)} for i in range(6)]
    eid = ab.create_experiment(conn, cfg, "hanset", "six", "channel", "채널 비교", six, now)
    exp = ab.list_experiments(conn)[0]
    check("batch4 experiment: six units over two weeks", len(ab.experiment_units_summary(conn, eid)) == 6 and
          ab.parse_iso(exp["window_end"]) == now + timedelta(days=14))
    for title, project, key, variable, hypothesis, units in [
        ("seven", "goallit", "seven", "channel", "test", six + [{"unit_key": "u6", "variable_value": "ch6"}]),
        ("duplicate value", "goallit", "dup", "channel", "test", [six[0], dict(six[0], unit_key="other")]),
        ("duplicate key", "goallit", "dupkey", "channel", "test", [six[0], dict(six[1], unit_key="u0")]),
        ("bad hook", "goallit", "hook", "hook_type", "test", [{"unit_key": "a", "variable_value": "fake"}]),
        ("cta", "hanset", "cta", "cta", "test", six[:1]),
        ("unknown brand", "fake", "brand", "channel", "test", six[:1]),
        ("empty hypothesis", "goallit", "empty", "channel", " ", six[:1]),
        ("same running lever", "hanset", "again", "channel", "test", six[:1]),
    ]:
        batch3_rejects("batch4 experiment rejects " + title, lambda p=project, k=key, v=variable, h=hypothesis, u=units:
                       ab.create_experiment(conn, cfg, p, k, v, h, u, now))
    hooks = [{"unit_key": str(i), "variable_value": value} for i, value in enumerate(ab.HOOK_TYPES)]
    hook_id = ab.create_experiment(conn, cfg, "hanset", "hooks", "hook_type", "훅 비교", hooks, now)
    check("batch4 experiment: different lever accepted", hook_id != eid)
    first = batch4_post(conn, now, "first")
    ab.attach_experiment_post(conn, cfg, hook_id, "0", first, now)
    check("batch4 attach: valid", ab.experiment_units_summary(conn, hook_id)[0]["post_id"] == first)
    bad_posts = {
        "other brand": batch4_post(conn, now, "foreign", project="goallit", hook="unexpected_diff"),
        "unknown post": "missing",
        "outside window": batch4_post(conn, now - timedelta(days=1), "old", hook="unexpected_diff"),
        "mismatch tested lever": batch4_post(conn, now, "mismatch", hook="discovery"),
        "second variable": batch4_post(conn, now, "channel", hook="unexpected_diff", channel="reddit"),
        "unknown control": batch4_post(conn, now, "unknown", hook="unexpected_diff", channel=ab.UNKNOWN),
    }
    for name, pid in bad_posts.items():
        batch3_rejects("batch4 attach rejects " + name, lambda p=pid:
                       ab.attach_experiment_post(conn, cfg, hook_id, "1", p, now))
    valid = batch4_post(conn, now, "valid", hook="unexpected_diff")
    ab.attach_experiment_post(conn, cfg, hook_id, "1", valid, now)
    batch3_rejects("batch4 attach rejects occupied unit", lambda:
                   ab.attach_experiment_post(conn, cfg, hook_id, "1", valid, now))
    check("batch4 attach: normalized URL and KST controls", ab._post_levers(conn.execute(
        "SELECT * FROM published WHERE post_id=?", (valid,)).fetchone())["posting_window"] == "morning")
    rc, out, _ = batch3_cli(cfg, now, "experiments", "list", "--project", "hanset", "--status", "running")
    check("batch4 CLI: experiment list", rc == 0 and len(json.loads(out)) == 2)
    rc, out, _ = batch3_cli(cfg, now, "experiments", "evaluate", "--id", str(hook_id))
    check("batch4 CLI: early evaluation", rc == 0 and json.loads(out)["verdict"] is None)
    rc, _, _ = batch3_cli(cfg, now, "experiments", "attach", "--id", str(hook_id), "--unit", "missing", "--post-id", first)
    check("batch4 CLI: attach error rc2", rc == 2)
    for unit, expected in (("a=threads", 0), ("malformed", 2)):
        rc, _, _ = batch3_cli(cfg, now, "experiments", "create", "--project", "goallit", "--key", unit,
                              "--variable", "channel", "--hypothesis", "채널 비교", "--unit", unit)
        check("batch4 CLI: create " + unit, rc == expected)
    conn.close()


def test_funnel_snapshots_batch4(tmp_dir):
    cfg, conn, now = batch4_fixture(tmp_dir)
    pid = batch4_post(conn, now, "snapshots")
    batch4_metrics(conn, pid, now, reach=80)
    batch4_metrics(conn, pid, now + timedelta(hours=1), reach=1200)
    batch4_metrics(conn, pid, now + timedelta(hours=2), reach=None)
    batch4_metrics(conn, pid, now, None, reach=9999)
    batch4_metrics(conn, pid, now, "week", reach=8888)
    batch4_metrics(conn, pid, now, "48h", views=500)
    f = ab._funnel(conn, pid, "7d")
    check("batch4 funnel: latest non-null not sum", f["impressions"] == 1200)
    check("batch4 funnel: ignores unsupported windows", f["ignored_observations"] == 2)
    check("batch4 funnel: missing remains null", f["clicks"] is None)
    check("batch4 funnel: views fallback isolated by window", ab._funnel(conn, pid, "48h")["impressions"] == 500)
    batch4_metrics(conn, pid, now + timedelta(hours=3), "48h", reach=0)
    check("batch4 funnel: zero reach does not fall back", ab._funnel(conn, pid, "48h")["impressions"] == 0)
    batch3_rejects("batch4 funnel: rejects invalid window", lambda: ab._funnel(conn, pid, "week"))
    conn.close()


def test_diagnosis_five_class_batch4(tmp_dir):
    cfg, conn, now = batch4_fixture(tmp_dir)
    base = dict(reach=1000, clicks=200, engaged=100, signups=40, revisits=10)
    cases = [dict(reach=60), dict(clicks=10), dict(engaged=20), dict(engaged=60, signups=2), dict(revisits=2), {}]
    for i, overrides in enumerate(cases):
        pid = batch4_post(conn, now, "diag" + str(i))
        data = dict(base, **overrides)
        batch4_metrics(conn, pid, now, "48h", **data)
        batch4_metrics(conn, pid, now, "7d", **data)
        report = ab.diagnose_post(conn, cfg, pid, "48h", now)
        check(f"batch4 diagnosis {i}: mandatory format", all(report[k] for k in
              ("hypothesis", "actual_with_denominator", "next_single_change")) and "alternative_causes" in report)
        expected = ab.FAILURE_CLASSES[i] if i < 5 else None
        check(f"batch4 diagnosis {i}: class and alternatives", report["category"] == expected and
              (bool(report["alternative_causes"]) if expected else report["verdict"] == "pass"))
        if i == 0:
            check("batch4 diagnosis: threshold denominator", "60 / 기준 100" in report["actual_with_denominator"] and
                  report["denominator_kind"] == "threshold")
        if i == 1:
            check("batch4 diagnosis: CTR denominator", "10 / reach 1000" in report["actual_with_denominator"])
        check(f"batch4 diagnosis {i}: measured state", conn.execute(
            "SELECT state FROM published WHERE post_id=?", (pid,)).fetchone()[0] == "measured")
        row_id = conn.execute("SELECT id FROM diagnoses WHERE post_id=?", (pid,)).fetchone()[0]
        ab.diagnose_post(conn, cfg, pid, "48h", now)
        check(f"batch4 diagnosis {i}: stable upsert ID", conn.execute(
            "SELECT id FROM diagnoses WHERE post_id=?", (pid,)).fetchone()[0] == row_id)
        ab.diagnose_post(conn, cfg, pid, "7d", now)
        check(f"batch4 diagnosis {i}: evaluated state", conn.execute(
            "SELECT state FROM published WHERE post_id=?", (pid,)).fetchone()[0] == "evaluated")
    check("batch4 diagnosis: persistent reports", conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0] == 12)
    conn.close()


def test_judgment_hold_batch4(tmp_dir):
    cfg, conn, now = batch4_fixture(tmp_dir)
    cases = [(dict(reach=20), "low_impressions"), (dict(reach=200, clicks=10), "high_bounce"),
             (dict(reach=1000, clicks=None), "low_ctr"),
             (dict(reach=1000, clicks=200, engaged=100, signups=None), "low_signup"),
             (dict(reach=1000, clicks=200, engaged=100, signups=40, revisits=None), "low_retention")]
    for i, (data, category) in enumerate(cases):
        pid = batch4_post(conn, now, "hold" + str(i))
        batch4_metrics(conn, pid, now, **data)
        report = ab.diagnose_post(conn, cfg, pid, "7d", now)
        check(f"batch4 hold {i}: report and measured state", report["verdict"] == ab.JUDGMENT_HOLD and
              report["category"] == category and "변경 없음" in report["next_single_change"] and
              all(report[k] for k in ("hypothesis", "actual_with_denominator", "alternative_causes", "next_single_change")) and
              conn.execute("SELECT state FROM published WHERE post_id=?", (pid,)).fetchone()[0] == "measured")
    units = [{"unit_key": "a", "variable_value": "discovery"}]
    eid = ab.create_experiment(conn, cfg, "hanset", "no-post", "hook_type", "test", units, now)
    check("batch4 experiment hold: early writes nothing", ab.evaluate_experiment(conn, cfg, eid, now)["verdict"] is None and
          conn.execute("SELECT result FROM experiments WHERE id=?", (eid,)).fetchone()[0] is None)
    check("batch4 experiment hold: unattached counts", ab.evaluate_experiment(conn, cfg, eid, now + timedelta(days=14))["verdict"] == ab.JUDGMENT_HOLD)
    eid = ab.create_experiment(conn, cfg, "hanset", "starved", "hook_type", "test", units, now)
    pid = batch4_post(conn, now, "starved", hook="discovery")
    ab.attach_experiment_post(conn, cfg, eid, "a", pid, now)
    batch4_metrics(conn, pid, now, engaged=10, signups=2)
    result = ab.evaluate_experiment(conn, cfg, eid, now + timedelta(days=14))
    check("batch4 experiment hold: insufficient denominator listed", result["verdict"] == ab.JUDGMENT_HOLD and result["result"]["7d"]["a"]["den"] == 10)
    batch4_metrics(conn, pid, now + timedelta(days=15), engaged=100, signups=10)
    check("batch4 experiment: conclusion immutable", ab.evaluate_experiment(conn, cfg, eid, now + timedelta(days=16)) == result)
    check("batch4 hold: no premature rules", conn.execute("SELECT COUNT(*) FROM brand_rules").fetchone()[0] == 0)
    conn.close()


def test_rules_promote_rollback_batch4(tmp_dir):
    cfg, conn, now = batch4_fixture(tmp_dir)
    late = now + timedelta(days=16)
    held, _ = batch4_experiment(conn, cfg, now, "tied", nums=(10, 10))
    mismatch, _ = batch4_experiment(conn, cfg, now, "mismatch", early=(5, 10))
    winner, result = batch4_experiment(conn, cfg, now, "winner")
    for name, project, value, source in [
        ("held", "hanset", "loss_prevention", {"experiment_id": held}),
        ("unconfirmed", "hanset", "loss_prevention", {"experiment_id": mismatch}),
        ("other brand", "goallit", "loss_prevention", {"experiment_id": winner}),
        ("wrong value", "hanset", "discovery", {"experiment_id": winner}),
        ("fake evidence", "hanset", "loss_prevention", {"experiment_id": 9999}),
        ("same confirmation", "hanset", "loss_prevention", {"experiment_id": mismatch, "confirm_experiment_id": mismatch}),
    ]:
        batch3_rejects("batch4 promotion rejects " + name, lambda p=project, v=value, s=source:
                       ab.promote_rule(conn, cfg, p, "hook_type", v, late, s))
    first = ab.promote_rule(conn, cfg, "hanset", "hook_type", "loss_prevention", late, {"experiment_id": winner})
    check("batch4 promotion: window agreement v1", ab.active_rules(conn, "hanset")["hook_type"] == {"value": "loss_prevention", "version": 1})
    with open(cfg["paths"]["rules"], encoding="utf-8") as fh:
        check("batch4 promotion: project JSON", json.load(fh)["projects"]["hanset"]["hook_type"]["version"] == 1)
    second = ab.promote_rule(conn, cfg, "hanset", "hook_type", "loss_prevention", late + timedelta(days=1),
                             {"experiment_id": mismatch, "confirm_experiment_id": winner})
    check("batch4 promotion: confirm experiment v2", ab.active_rules(conn, "hanset")["hook_type"]["version"] == 2 and
          conn.execute("SELECT status FROM brand_rules WHERE id=?", (first,)).fetchone()[0] == "superseded")
    foreign, _ = batch4_experiment(conn, cfg, now, "foreign", project="goallit")
    ab.promote_rule(conn, cfg, "goallit", "hook_type", "loss_prevention", late, {"experiment_id": foreign})
    check("batch4 rules: brand isolation", ab.active_rules(conn, "hanset")["hook_type"]["version"] == 2 and
          ab.active_rules(conn, "goallit")["hook_type"]["version"] == 1)
    rc, out, _ = batch3_cli(cfg, late, "rules", "list", "--project", "hanset")
    check("batch4 CLI: rules list", rc == 0 and set(json.loads(out)) == {"hanset"})
    rc, _, _ = batch3_cli(cfg, late, "rules", "promote", "--project", "hanset", "--key", "channel",
                          "--value", "threads", "--experiment", str(winner))
    check("batch4 CLI: invalid rule rc2", rc == 2)
    rc, out, _ = batch3_cli(cfg, late, "rules", "rollback", "--project", "hanset", "--key", "hook_type", "--reason", "manual")
    check("batch4 rollback: restores v1", rc == 0 and json.loads(out) == {"rolled_back": 2, "restored": 1} and
          ab.active_rules(conn, "hanset")["hook_type"]["version"] == 1 and
          conn.execute("SELECT status FROM brand_rules WHERE id=?", (second,)).fetchone()[0] == "rolled_back")
    ab.rollback_rule(conn, cfg, "hanset", "hook_type", late, "last rule")
    check("batch4 rollback: no previous version", ab.active_rules(conn, "hanset") == {})
    with open(cfg["paths"]["rules"], encoding="utf-8") as fh:
        payload = json.load(fh)
    check("batch4 rollback: JSON updated and isolated", payload["projects"]["hanset"] == {} and payload["projects"]["goallit"]["hook_type"]["version"] == 1)
    batch3_rejects("batch4 rollback: no active rule error", lambda: ab.rollback_rule(conn, cfg, "hanset", "hook_type", late))
    conn.close()


def test_rule_regression_batch4(tmp_dir):
    cfg, conn, now = batch4_fixture(tmp_dir)
    eid, _ = batch4_experiment(conn, cfg, now, "baseline")
    promoted = now + timedelta(days=16)
    for i in range(2):
        ab.promote_rule(conn, cfg, "hanset", "hook_type", "loss_prevention", promoted + timedelta(days=i), {"experiment_id": eid})
    post_time = promoted + timedelta(days=2)
    pid = batch4_post(conn, post_time, "followup")
    batch4_metrics(conn, pid, post_time, engaged=10, signups=1)
    hold = ab.check_rule_regression(conn, cfg, "hanset", "hook_type", post_time)
    check("batch4 regression: sample hold persisted", hold["verdict"] == ab.JUDGMENT_HOLD and
          conn.execute("SELECT verdict FROM rule_checks ORDER BY id DESC LIMIT 1").fetchone()[0] == ab.JUDGMENT_HOLD and
          ab.active_rules(conn, "hanset")["hook_type"]["version"] == 2)
    batch4_metrics(conn, pid, post_time + timedelta(hours=1), engaged=100, signups=1)
    regressed = ab.check_rule_regression(conn, cfg, "hanset", "hook_type", post_time + timedelta(hours=1))
    check("batch4 regression: auto rollback", regressed["verdict"] == "regressed" and regressed["rolled_back"] ==
          {"rolled_back": 2, "restored": 1} and ab.active_rules(conn, "hanset")["hook_type"]["version"] == 1)
    # Latest snapshots replace the earlier low measurements.
    batch4_metrics(conn, pid, post_time + timedelta(hours=2), engaged=100, signups=10)
    healthy = ab.check_rule_regression(conn, cfg, "hanset", "hook_type", post_time + timedelta(hours=2))
    check("batch4 regression: healthy rate", healthy["verdict"] == "ok" and healthy["actual_rate"] == .1)
    foreign = batch4_post(conn, post_time, "foreignfollowup", project="goallit")
    unknown = batch4_post(conn, post_time, "unknownfollowup")
    different = batch4_post(conn, post_time, "differentfollowup", hook="discovery")
    for p in (foreign, different):
        batch4_metrics(conn, p, post_time, engaged=1000, signups=0)
    batch4_metrics(conn, unknown, post_time, engaged=1000, signups=None)
    rc, out, _ = batch3_cli(cfg, post_time + timedelta(hours=2), "rules", "check", "--project", "hanset", "--key", "hook_type")
    check("batch4 regression: excludes foreign, different, unknown and pre-promotion posts", rc == 0 and
          json.loads(out)["denominator"] == 100 and json.loads(out)["numerator"] == 10)
    check("batch4 regression: no active rule", ab.check_rule_regression(conn, cfg, "goallit", "hook_type", post_time) == {"verdict": None})
    conn.close()


def test_config_batch4(tmp_dir):
    cfg, conn, now = batch4_fixture(tmp_dir)
    check("batch4 config: legacy defaults", cfg["experiments"]["max_units"] == 6 and
          cfg["experiments"]["window_days"] == 14 and cfg["diagnosis"]["min_sample"] == 30 and
          cfg["rules"]["regression_tolerance"] == .2 and cfg["paths"]["rules"].endswith("rules.json"))
    pid = batch4_post(conn, now, "run")
    for window in ab.METRIC_WINDOWS:
        batch4_metrics(conn, pid, now, window, reach=1000, clicks=200, engaged=100, signups=40, revisits=10)
    with conn:
        conn.execute("UPDATE metric_checks SET status='done'")
    flags = ("--no-crawl", "--no-discover", "--no-demand", "--no-drafts")
    rc, _, _ = batch3_cli(cfg, now, "run", *flags, "--no-metrics")
    check("batch4 integration: no-metrics gates diagnosis", rc == 0 and conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0] == 0)
    rc, _, _ = batch3_cli(cfg, now, "run", *flags)
    check("batch4 integration: run diagnoses both windows", rc == 0 and conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0] == 2)
    check("batch4 integration: repeat diagnostics idempotent", ab.run_diagnostics(conn, cfg, cfg["_projects"], now) ==
          dict(diagnosed=0, held=0, passed=0, evaluated=0, rule_checks=0, rolled_back=0))
    pid2 = batch4_post(conn, now, "statushold")
    batch4_metrics(conn, pid2, now, "48h", reach=20)
    with conn:
        conn.execute("UPDATE metric_checks SET status='done'")
    rc, _, _ = batch3_cli(cfg, now, "status")
    check("batch4 integration: status diagnoses holds", rc == 0 and conn.execute(
          "SELECT verdict FROM diagnoses WHERE post_id=? AND check_kind='48h'", (pid2,)).fetchone()[0] == ab.JUDGMENT_HOLD)
    status = ab.collect_status(conn, cfg, now)
    check("batch4 status: diagnosis summaries", status["projects"]["hanset"]["diagnoses"]["pass"] == 2 and
          "diagnosis: hanset" in ab.render_status_md(status, cfg, now, "test"))
    conn.close()


def test_evaluation_edges_batch4(tmp_dir):
    cfg, conn, now = batch4_fixture(tmp_dir)
    units = [{"unit_key": "a", "variable_value": "loss_prevention"},
             {"unit_key": "b", "variable_value": "discovery"}]
    eid = ab.create_experiment(conn, cfg, "hanset", "ctr-tie", "hook_type", "같은 가입률의 CTR 비교", units, now)
    posts = []
    for i, unit in enumerate(units):
        pid = batch4_post(conn, now, "tie" + str(i), hook=unit["variable_value"])
        posts.append(pid)
        rc, _, _ = batch3_cli(cfg, now, "experiments", "attach", "--id", str(eid), "--unit", unit["unit_key"], "--post-id", pid)
        check(f"batch4 CLI: successful attach {i}", rc == 0)
        batch4_metrics(conn, pid, now, "7d", engaged=30, signups=3, reach=1000, clicks=100 + i * 100)
        batch4_metrics(conn, pid, now, "48h", engaged=29 if i else 30, signups=3, reach=1000, clicks=100 + i * 100)
    later = now + timedelta(days=14)
    result = ab.evaluate_experiment(conn, cfg, eid, later)
    check("batch4 evaluation: CTR tiebreak and sample boundary", result["verdict"] == "winner:b" and
          result["result"]["winner_rate"] == .1 and result["result"]["48h"]["leader"] is None)
    batch3_rejects("batch4 promotion: 48h sample shortage blocks revalidation", lambda:
                   ab.promote_rule(conn, cfg, "hanset", "hook_type", "discovery", later, {"experiment_id": eid}))
    batch3_rejects("batch4 attach: concluded experiment rejected", lambda:
                   ab.attach_experiment_post(conn, cfg, eid, "a", posts[0], later))
    # Latest tie at the exact same observed time uses the row ID as tiebreak.
    batch4_metrics(conn, posts[0], now, "7d", clicks=300)
    check("batch4 funnel: identical time ordered by ID", ab._funnel(conn, posts[0], "7d")["clicks"] == 300)
    confirm, _ = batch4_experiment(conn, cfg, now, "confirm", nums=(5, 10))
    rc, out, _ = batch3_cli(cfg, later, "rules", "promote", "--project", "hanset", "--key", "hook_type",
                          "--value", "discovery", "--experiment", str(eid), "--confirm-experiment", str(confirm))
    check("batch4 CLI: promotion succeeds with confirmation", rc == 0 and json.loads(out)["id"] > 0)
    # Diagnostics only process selected brands and completed check rows, and also
    # conclude due experiments and check active rules during the same run.
    pending = batch4_post(conn, now, "pending")
    foreign = batch4_post(conn, now, "foreigncheck", project="goallit")
    with conn:
        conn.execute("UPDATE metric_checks SET status='done' WHERE published_id IN "
                     "(SELECT id FROM published WHERE post_id=?)", (foreign,))
    due_id = ab.create_experiment(conn, cfg, "hanset", "due", "channel", "test",
                                  [{"unit_key": "a", "variable_value": "threads"}], now)
    selected = [p for p in cfg["_projects"] if p.name == "hanset"]
    report = ab.run_diagnostics(conn, cfg, selected, later)
    check("batch4 diagnostics: due evaluation and rule check", report["evaluated"] == 1 and report["rule_checks"] == 1 and
          conn.execute("SELECT verdict FROM experiments WHERE id=?", (due_id,)).fetchone()[0] == ab.JUDGMENT_HOLD)
    check("batch4 diagnostics: pending and foreign excluded", conn.execute(
          "SELECT COUNT(*) FROM diagnoses WHERE post_id IN (?,?)", (pending, foreign)).fetchone()[0] == 0)
    # A completed diagnosis links back to its experiment without recreating IDs.
    diagnosis = ab.diagnose_post(conn, cfg, posts[0], "7d", later)
    check("batch4 diagnosis: experiment linked", conn.execute(
          "SELECT experiment_id FROM diagnoses WHERE post_id=?", (posts[0],)).fetchone()[0] == eid)
    # Reopening an existing ledger preserves all five new tables and their rows.
    conn.close()
    conn = ab.db_connect(cfg)
    check("batch4 schema: repeated initialization preserves rules", ab.active_rules(conn, "hanset")["hook_type"]["value"] == "discovery")
    try:
        with conn:
            conn.execute("INSERT INTO experiment_units (experiment_id,unit_key,variable_value,created) VALUES (?,?,?,?)",
                         (eid, "duplicate", "discovery", ab.iso(now)))
    except sqlite3.IntegrityError:
        check("batch4 schema: duplicate value constrained", True)
    else:
        check("batch4 schema: duplicate value constrained", False)
    conn.close()


# Batch 5: offline operations tests. Existing Batch 1–4 cases remain unchanged.
import ops.config as ops_config
import ops.guards as ops_guards
import ops.storage as ops_storage
import ops.pipeline as ops_pipeline
import ops.runner as ops_runner
import ops.mocks as ops_mocks
from unittest.mock import patch


def ops_fixture(tmp_dir):
    cfg, conn, proj, now, q, e = batch2_fixture(tmp_dir)
    cfg['ops']['paths'].update(out=os.path.join(tmp_dir, 'out', 'ops'),
                               dryrun_log=os.path.join(tmp_dir, 'logs', 'ops_dryrun.md'),
                               prompts=os.path.join(ROOT, 'ops', 'prompts'),
                               cost_log=os.path.join(tmp_dir, 'logs', 'ops_cost.jsonl'))
    cfg['ops']['accounts']['hanset']['x']['confirmed'] = True
    cfg['metrics']['dir'] = os.path.join(tmp_dir, 'metrics')
    qid = batch2_question(conn, cfg, proj, q, now)
    eid = batch2_evidence(conn, proj, e, now)
    ab.link_evidence(conn, eid, 'question', qid)
    conn.execute("UPDATE questions SET state='ready' WHERE id=?", (qid,))
    conn.commit()
    ops_storage.ensure_ops_schema(conn)
    return cfg, conn, proj, now, qid, eid


def ops_mock(command, context):
    return ops_mocks.response(command, context)


def ops_cli(cfg, now, command, *args):
    return batch3_cli(cfg, now, 'ops', command, *args)


def ops_make_drafts(cfg, conn, proj, now):
    result = ops_pipeline.cmd_draft(conn, cfg, proj, now, mock=ops_mock)
    check('ops draft fixture succeeds', result['status'] == 'ok', result)
    return result['drafts']


def ops_approve(cfg, conn, proj, now, drafts):
    ids = [d['id'] for d in drafts]
    result = ops_pipeline.cmd_compliance(conn, cfg, proj, now, mock=ops_mock, ops_draft_ids=ids)
    check('ops full checklist passes', result['verdict'] == 'PASS', result)
    conn.execute("UPDATE drafts SET state='approved' WHERE id=?", (drafts[0]['draft_id'],))
    conn.commit()


def ops_expect_block(name, action, reason):
    try:
        action()
    except ops_guards.OpsError as exc:
        check(name, reason in str(exc), str(exc))
    else:
        check(name, False, 'expected OpsError')


def ops_counts(conn):
    return {t: conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in (
        'drafts', 'published', 'questions', 'evidence', 'evidence_links', 'ops_drafts', 'ops_compliance',
        'ops_schedule', 'ops_publish_guides', 'ops_measure_notes')}


def test_ops_config_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    legacy = {'campaign': 'legacy'}
    ops_config.apply_ops_defaults(legacy)
    check('ops legacy config default', legacy['ops']['model'] == 'deepseek-flash')
    legacy['ops']['model'] = 'custom-model'
    legacy['ops']['utm']['campaign'] = ''
    ops_config.apply_ops_defaults(legacy)
    check('ops deep merge preserves overrides', legacy['ops']['model'] == 'custom-model' and legacy['ops']['utm']['campaign'] == 'legacy')
    check('ops defaults independent', cfg['ops']['accounts']['hanset']['x']['confirmed'] is True and legacy['ops']['accounts']['hanset']['x']['confirmed'] is False)
    ops_storage.ensure_ops_schema(conn)
    check('ops schema idempotent', {'ops_runs', 'ops_drafts', 'ops_compliance', 'ops_schedule', 'ops_publish_guides', 'ops_measure_notes', 'ops_packages', 'ops_metric_snapshots'} <= {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
    check('ops paths expanduser', ops_config.resolve_path('~/profile') == os.path.expanduser('~/profile'))
    for name in ('pitchboard', 'goallit', 'agamajung'):
        cfg['ops']['brands'][name]['enabled'] = True
        ops_expect_block('ops unsupported brand ' + name, lambda: ops_guards.check_brand(cfg, name), 'brand_not_supported')
    cfg['ops']['brands']['kitchengenome']['enabled'] = True
    ops_expect_block('ops kitchen hold hard block', lambda: ops_guards.check_brand(cfg, 'kitchengenome'), 'brand_hold')
    cfg['ops']['cost']['allow_paid_actions'] = True
    ops_expect_block('ops paid config hard block', lambda: ops_guards.check_cost_config(cfg), 'paid_actions_forbidden')
    cfg['ops']['cost']['allow_paid_actions'] = False
    cfg['ops']['publish']['browser_automation'] = True
    ops_expect_block('ops browser config hard block', lambda: ops_guards.check_cost_config(cfg), 'unsafe_publish_config')
    conn.close()


def test_ops_guards_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    g = ops_guards
    check('ops X URL and wide weighting', g.x_weighted_length('한₩🙂a https://example.test/a123') == 31)
    check('ops wide char weight default wired', g.x_weighted_length('한', cfg) == 2)
    cfg['ops']['limits']['wide_char_weight'] = 1
    check('ops wide char weight override wired', g.x_weighted_length('한', cfg) == 1)
    cfg['ops']['limits']['wide_char_weight'] = 2
    check('ops utm defaults wired', ops_pipeline.utm_content(cfg, hook='hookA') == 'hookA' and ops_pipeline.utm_medium(cfg) == 'social')
    check('ops X boundary', not g.validate_copy('x', 'a' * 280, None, proj, [], cfg) and 'x_length_exceeded' in g.validate_copy('x', 'a' * 281, None, proj, [], cfg))
    check('ops Threads boundary', 'threads_length_exceeded' in g.validate_copy('threads', 'a' * 501, None, proj, [], cfg))
    evidence = [{'fact': '9,000원', 'allowed_claims': '[]'}]
    check('ops currency normalization', not g.unsupported_numbers('₩9,000 / 9,000원 / KRW 9000', evidence, cfg))
    check('ops unitless and currency number guard', g.unsupported_numbers('3가지 이유, 5 tips, 12,000원', evidence, cfg) == ['unsupported_number:12000원', 'unsupported_number:3가지', 'unsupported_number:5tips'])
    check('ops numeric labels excluded', g.copy_numbers('[CARD 1/3] 2026-11-05\n1. 안내 https://example.test/50') == [])
    check('ops card hash included', g.content_hash('a', 'b') != g.content_hash('a', 'c'))
    check('ops card number validated', 'unsupported_number:20%' in g.validate_copy('instagram', '안내', '[CARD 1/3] 20%', proj, evidence, cfg))
    check('ops missing card rejected', 'card_copy_format' in g.validate_copy('instagram', '안내', None, proj, evidence, cfg))
    check('ops forbidden terms all covered', len(g.find_prohibited('가짜 리뷰 부계정 댓글 알바 경쟁사 비방 체험단 미표기 저작권 이미지 무단 사용')) == 6)
    text = g.redact('sk-secret Bearer abc password=pw otp=123456 https://example.test/?key=hidden', ['sk-secret'])
    check('ops generic secret redaction', all(s not in text for s in ('sk-secret', 'Bearer abc', 'password=pw', 'otp=123456', 'hidden')))
    check('ops quoted passwords redacted', 'with spaces' not in g.redact('password="with spaces"'))
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'opaque-test-key'}):
        check('ops dynamic secret blocked in copy', 'personal_or_secret_data' in g.validate_copy('x', 'opaque-test-key', None, proj, [], cfg))
    check('ops card personal handles blocked', 'personal_or_secret_data' in g.validate_copy('instagram', '안내', '[CARD 1/3] @private_user', proj, [], cfg))
    check('ops fabricated ISO date blocked', 'unsupported_date:2099-01-01' in g.unsupported_numbers('2099-01-01', evidence, cfg))
    check('ops brand exclusions retained', any('brand_term' in r for r in g.validate_copy('x', '직접 가봤', None, proj, [], cfg)))
    check('ops nested secret redaction', g.redact_obj({'api_key': 'opaque-value'})['api_key'] == '[REDACTED]')
    account = cfg['ops']['accounts']['hanset']['x']
    for alias in ('Crom_kr', 'crom_KR', '@AGAMAJUNG'):
        account.update(alias=alias, confirmed=False)
        check('ops forbidden account first ' + alias, g.check_account(cfg, 'hanset', 'x')[1] == 'forbidden_account')
    account.update(alias='wrong', confirmed=True)
    check('ops wrong X alias blocked', g.check_account(cfg, 'hanset', 'x')[1] == 'x_account_not_allowed')
    account.update(alias='hanset_mag', confirmed=False)
    cfg['ops']['account_rules']['confirmation_required'] = False
    check('ops X confirmation cannot be disabled', g.check_account(cfg, 'hanset', 'x')[1] == 'x_account_confirmation_required')
    for ch in ('threads', 'instagram'):
        check('ops X-only confirmation ' + ch, g.check_account(cfg, 'hanset', ch)[1] is None)
    g.validate_single_change('다음 발행 시 제목을 질문형으로')
    for text in ('', 'a' * 121, '제목 교체 + 시간 변경', '제목 그리고 시간', 'A; B', '1) 제목', 'A\nB', 'A. B.'):
        ops_expect_block('ops single change rejects ' + repr(text[:20]), lambda: g.validate_single_change(text), 'measure_multiple_changes')
    import logging
    logger = logging.getLogger('ops-secret-test')
    streams = [io.StringIO(), io.StringIO()]
    logger.handlers = [logging.StreamHandler(s) for s in streams]
    logger.setLevel(logging.INFO)
    g.install_redaction(logger, ['opaque-value'])
    try:
        raise ValueError('password=hidden opaque-value')
    except ValueError:
        logger.exception('Bearer abc %s', 'opaque-value')
    check('ops all handlers and traceback redacted', all('hidden' not in s.getvalue() and 'opaque-value' not in s.getvalue() and 'Bearer abc' not in s.getvalue() for s in streams))
    conn.close()


def test_ops_plan_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    with open(cfg['paths']['rounds_log'], 'w') as stream:
        stream.write('password=plan-secret sk-test contact@example.test\nA verified local observation\n')
    with open(cfg['paths']['status_md'], 'w') as stream:
        stream.write('otp=123456\nContact contact@example.test\n')
    before = ops_counts(conn)
    rc, stdout, stderr = ops_cli(cfg, now, 'plan', '--dry-run')
    result = json.loads(stdout)
    check('ops plan dryrun CLI', rc == 0 and result['status'] == 'ok', stderr)
    check('ops plan report exists', os.path.isfile(result['path']))
    with open(cfg['ops']['paths']['dryrun_log']) as stream:
        audit = stream.read()
    run = conn.execute('SELECT * FROM ops_runs ORDER BY id DESC LIMIT 1').fetchone()
    check('ops plan audit mode and template', run['mode'] == 'dry-run' and run['prompt_path'].endswith('/ops/prompts/plan.md'))
    check('ops plan no business writes', before == ops_counts(conn))
    from ops.context import plan_context
    ctx = json.dumps(plan_context(conn, cfg, proj, now))
    check('ops plan privacy across sinks', all(secret not in ctx + stdout + audit + run['output'] for secret in ('plan-secret', 'sk-test', 'contact@example.test', '123456')))
    check('ops plan audit payload equals output', json.loads(run['output']) == result)
    conn.close()


def test_ops_draft_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    before = ops_counts(conn)
    result = ops_pipeline.cmd_draft(conn, cfg, proj, now, dry_run=True)
    check('ops draft dryrun previews three channels', len(result['drafts']) == 3 and before == ops_counts(conn))
    check('ops dryrun draft writes no files', not os.path.exists(os.path.join(cfg['ops']['paths']['out'], 'drafts')))
    def partial(command, ctx):
        response = ops_mock(command, ctx)
        response['drafts'] = response['drafts'][:2]
        return response
    ops_expect_block('ops draft partial channel set rejected',
                     lambda: ops_pipeline.cmd_draft(conn, cfg, proj, now, mock=partial), 'llm_output_invalid')
    drafts = ops_make_drafts(cfg, conn, proj, now)
    anchor = conn.execute('SELECT * FROM drafts').fetchone()
    check('ops anchor required path and namespace', bool(anchor['path']) and os.path.isfile(anchor['path']) and anchor['page_url'].startswith('question:'))
    check('ops channel draft persistence', conn.execute('SELECT COUNT(*) FROM ops_drafts').fetchone()[0] == 3 and len({d['channel'] for d in drafts}) == 3)
    conn.execute("UPDATE drafts SET state='approved',page_url='https://example.test/existing' WHERE id=?", (anchor['id'],))
    question = conn.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    evidence = conn.execute('SELECT * FROM evidence WHERE id=?', (eid,)).fetchall()
    a = ops_pipeline.ensure_anchor_draft(conn, proj, question, evidence, cfg, now, question['target_landing_url'])
    b = ops_pipeline.ensure_anchor_draft(conn, proj, question, evidence, cfg, now, question['target_landing_url'])
    row = conn.execute('SELECT * FROM drafts WHERE id=?', (a,)).fetchone()
    check('ops anchor reuses question preserving approval', a == b == anchor['id'] and row['state'] == 'approved' and row['page_url'] == 'https://example.test/existing' and conn.execute('SELECT COUNT(*) FROM drafts').fetchone()[0] == 1)
    conn.execute("UPDATE evidence SET valid_until='2000-01-01' WHERE id=?", (eid,))
    result = ops_pipeline.cmd_draft(conn, cfg, proj, now, dry_run=True)
    check('ops draft expired evidence skipped', result == {'status': 'skipped', 'reason': 'no_verified_evidence'})
    conn.execute("UPDATE questions SET state='needs_research'")
    check('ops draft empty question skipped', ops_pipeline.cmd_draft(conn, cfg, proj, now, dry_run=True)['reason'] == 'no_questions')
    conn.close()


def test_ops_compliance_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    drafts = ops_make_drafts(cfg, conn, proj, now)
    ids = [d['id'] for d in drafts]
    before = ops_counts(conn)
    result = ops_pipeline.cmd_compliance(conn, cfg, proj, now, dry_run=True, ops_draft_ids=ids)
    check('ops compliance dryrun no business writes', result['verdict'] == 'PASS' and before == ops_counts(conn))
    ops_approve(cfg, conn, proj, now, drafts)
    def missing(command, ctx):
        response = ops_mock(command, ctx)
        response['checks'].pop('C9')
        return response
    result = ops_pipeline.cmd_compliance(conn, cfg, proj, now, mock=missing, ops_draft_ids=[ids[0]])
    check('ops checklist missing fails closed', result['verdict'] == 'FAIL' and 'llm_check_missing:C9' in result['results'][0]['reasons'])
    for checks in ({}, {'C1': {'pass': 1}}, {'C1': {'pass': 'true'}}):
        result = ops_pipeline.cmd_compliance(conn, cfg, proj, now, mock={'compliance': {'verdict': 'PASS', 'reasons': [], 'checks': checks}}, ops_draft_ids=[ids[0]])
        check('ops incomplete or nonboolean checklist fails', result['verdict'] == 'FAIL')
    copy = '가짜 리뷰로 12,000원'
    conn.execute('UPDATE ops_drafts SET copy=?,copy_hash=? WHERE id=?', (copy, ops_guards.content_hash(copy), ids[0]))
    def forbidden_llm(*args, **kwargs):
        raise AssertionError('code failure must not call LLM')
    result = ops_pipeline.cmd_compliance(conn, cfg, proj, now, mock=forbidden_llm, ops_draft_ids=[ids[0]])
    reasons = result['results'][0]['reasons']
    check('ops compliance prohibited and invented number', result['verdict'] == 'FAIL' and 'prohibited:fake_review' in reasons and 'unsupported_number:12000원' in reasons)
    ig = ids[2]
    conn.execute("UPDATE ops_drafts SET card_copy='[CARD 1/3] 20%' WHERE id=?", (ig,))
    result = ops_pipeline.cmd_compliance(conn, cfg, proj, now, mock=forbidden_llm, ops_draft_ids=[ig])
    check('ops card edit invalidates PASS', 'stale_copy_hash' in result['results'][0]['reasons'])
    draft = conn.execute('SELECT * FROM ops_drafts WHERE id=?', (ig,)).fetchone()
    ops_expect_block('ops stale hash requires recheck', lambda: ops_pipeline.require_compliance_pass(conn, draft), 'stale_copy_hash')
    with patch.dict(os.environ, {}, clear=True):
        result = ops_pipeline.cmd_compliance(conn, cfg, proj, now, ops_draft_ids=[ids[1]])
    check('ops missing key records compliance FAIL', 'llm_error:deepseek_key_missing' in result['results'][0]['reasons'])
    conn.close()


def test_ops_schedule_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    drafts = ops_make_drafts(cfg, conn, proj, now)
    did = drafts[0]['id']
    before = ops_counts(conn)
    result = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[did])
    check('ops automatic PASS still requires human approval', result['status'] == 'blocked' and result['results'][0]['reason'] == 'draft_not_approved')
    check('ops blocked scheduling leaves queue unchanged', ops_counts(conn) == before)
    ops_pipeline.cmd_compliance(conn, cfg, proj, now, mock=ops_mock, ops_draft_ids=[did])
    result = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[did])
    check('ops schedule requires human approval', result['results'][0]['reason'] == 'draft_not_approved')
    conn.execute("UPDATE drafts SET state='approved'")
    cfg['ops']['accounts']['hanset']['x']['confirmed'] = False
    result = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[did])
    check('ops schedule X confirmation', result['results'][0]['reason'] == 'x_account_confirmation_required')
    cfg['ops']['accounts']['hanset']['x']['confirmed'] = True
    before = ops_counts(conn)
    result = ops_pipeline.cmd_schedule(conn, cfg, proj, now, dry_run=True, ops_draft_ids=[did])
    check('ops schedule preview computes post identity', result['status'] == 'ok' and result['results'][0]['post_id'].startswith('p_') and before == ops_counts(conn))
    result = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[did])
    check('ops approved schedule succeeds', result['status'] == 'ok', result)
    pid = result['results'][0]['post_id']
    cfg['ops']['utm']['campaign'] = 'changed_campaign'
    result2 = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[did])
    post = conn.execute('SELECT * FROM published WHERE post_id=?', (pid,)).fetchone()
    check('ops UTM changes keep stable identity', result2['status'] == 'ok' and result2['results'][0]['post_id'] == pid and conn.execute('SELECT COUNT(*) FROM published').fetchone()[0] == 1 and conn.execute('SELECT COUNT(*) FROM ops_schedule').fetchone()[0] == 1)
    check('ops scheduled state and attribution', post['state'] == 'scheduled' and 'changed_campaign' in post['target_url'] and conn.execute('SELECT status FROM ops_drafts WHERE id=?', (did,)).fetchone()[0] == 'scheduled')
    cfg['ops']['utm']['medium'] = 'newsletter'
    result3 = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[did])
    post = conn.execute('SELECT * FROM published WHERE post_id=?', (pid,)).fetchone()
    check('ops UTM medium config wired', result3['status'] == 'ok' and 'utm_medium=newsletter' in post['target_url'])
    cfg['ops']['utm']['medium'] = 'social'
    conn.execute("UPDATE evidence SET valid_until='2000-01-01' WHERE id=?", (eid,))
    result = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[did])
    check('ops schedule rechecks expired evidence', result['status'] == 'blocked' and result['results'][0]['reason'] == 'evidence_expired')
    check('ops expiration block records reason', 'evidence_expired' in conn.execute('SELECT blocked_reasons FROM ops_drafts WHERE id=?', (did,)).fetchone()[0])
    conn.close()


def test_ops_publish_guide_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    drafts = ops_make_drafts(cfg, conn, proj, now)
    ops_approve(cfg, conn, proj, now, drafts)
    ids = [d['id'] for d in drafts]
    ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=ids)
    before = ops_counts(conn)
    result = ops_pipeline.cmd_publish_guide(conn, cfg, proj, now, dry_run=True)
    check('ops guide dryrun no writes', ops_counts(conn) == before)
    check('ops missing IG asset blocks', result['status'] == 'blocked' and any(r.get('reason') == 'missing_card_asset' for r in result['results']))
    result = ops_pipeline.cmd_publish_guide(conn, cfg, proj, now, ops_schedule_ids=[1, 2])
    check('ops X and Threads guides written', result['status'] == 'ok' and conn.execute('SELECT COUNT(*) FROM ops_publish_guides').fetchone()[0] == 2)
    guide = result['results'][0]['guide']
    check('ops guide account safety payload', guide['account']['logout_allowed'] is False and guide['forbidden_accounts'] == ['Crom_kr', 'agamajung'] and guide['no_browser_automation'] is True)
    check('ops guide measurement precondition', any('queue verify' in s for s in guide['checklist']))
    check('ops guide_ready transition', conn.execute('SELECT status FROM ops_drafts WHERE id=?', (ids[0],)).fetchone()[0] == 'guide_ready')
    row = conn.execute('SELECT * FROM ops_publish_guides LIMIT 1').fetchone()
    check('ops guide JSON and MD exist', os.path.isfile(row['json_path']) and os.path.isfile(row['md_path']))
    card = os.path.join(tmp_dir, 'card.png')
    with open(card, 'wb') as stream:
        stream.write(b'offline asset placeholder')
    conn.execute('UPDATE ops_drafts SET card_path=? WHERE id=?', (card, ids[2]))
    check('ops IG guide with local card', ops_pipeline.cmd_publish_guide(conn, cfg, proj, now, ops_schedule_ids=[3])['status'] == 'ok')
    # Latest FAIL invalidates even a previously generated guide.
    fail = ops_mock('compliance', {})
    fail['checks']['C1']['pass'] = False
    ops_pipeline.cmd_compliance(conn, cfg, proj, now, mock={'compliance': fail}, ops_draft_ids=[ids[0]])
    result = ops_pipeline.cmd_publish_guide(conn, cfg, proj, now, ops_schedule_ids=[1])
    check('ops FAIL refuses publish guide', result['status'] == 'blocked' and result['results'][0]['reason'] == 'compliance_pass_required')
    before = ops_counts(conn)
    result = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[ids[0]])
    check('ops FAIL refuses schedule after former PASS', result['status'] == 'blocked' and ops_counts(conn) == before)
    conn.close()


def test_ops_measure_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    pid = batch4_post(conn, now, 'ops-measure')
    diagnosis = ab.diagnose_post(conn, cfg, pid, '48h', now)
    before = ops_counts(conn)
    with patch.object(ab, 'run_due_checks', side_effect=AssertionError('no network collection')) as due:
        with patch.object(ab, 'process_rounds_log', wraps=ab.process_rounds_log) as rounds:
            result = ops_pipeline.cmd_measure(conn, cfg, proj, now, dry_run=True)
            check('ops measure dryrun read only', rounds.call_count == 0 and ops_counts(conn) == before)
            result = ops_pipeline.cmd_measure(conn, cfg, proj, now, mock=ops_mock)
            check('ops measure local pipeline reused', rounds.call_count == 1 and due.call_count == 0)
    check('ops measure note persisted', result['status'] == 'ok' and conn.execute('SELECT COUNT(*) FROM ops_measure_notes').fetchone()[0] == 1)
    note = conn.execute('SELECT payload FROM ops_measure_notes').fetchone()
    check('ops measure alternative causes stored as list', isinstance(json.loads(note['payload'])['alternative_causes'], list))
    check('ops measure holds canonical verdict', result['notes'][0]['verdict'] == diagnosis['verdict'] == '판정 보류' and result['notes'][0]['category'] == diagnosis['category'])
    for label, value in (('missing', None), ('empty', []), ('nonstring', [1]), ('blank', ['  '])):
        def broken_alternatives(command, ctx, value=value):
            response = ops_mock(command, ctx)
            if value is None:
                response.pop('alternative_causes', None)
            else:
                response['alternative_causes'] = value
            return response
        ops_expect_block('ops measure alternative causes ' + label,
                         lambda broken=broken_alternatives: ops_pipeline.cmd_measure(conn, cfg, proj, now, mock=broken),
                         'llm_output_invalid')
    def bad_numbers(command, ctx):
        response = ops_mock(command, ctx)
        response['actual_with_denominator'] = '999999 / 123456'
        return response
    ops_expect_block('ops measure fabricated number blocked', lambda: ops_pipeline.cmd_measure(conn, cfg, proj, now, mock=bad_numbers), 'measure_fabricated_number')
    def multiple(command, ctx):
        response = ops_mock(command, ctx)
        response['next_single_change'] = '제목 교체 + 발행 시간 변경'
        return response
    ops_expect_block('ops measure multiple changes blocked', lambda: ops_pipeline.cmd_measure(conn, cfg, proj, now, mock=multiple), 'measure_multiple_changes')
    def reclassify(command, ctx):
        response = ops_mock(command, ctx)
        response.update(category='low_signup', verdict='diagnosed')
        return response
    result = ops_pipeline.cmd_measure(conn, cfg, proj, now, mock=reclassify)
    check('ops measure LLM cannot reclassify', result['notes'][0]['verdict'] == '판정 보류' and result['notes'][0]['category'] == diagnosis['category'])
    check('ops measure upsert unique', conn.execute('SELECT COUNT(*) FROM ops_measure_notes').fetchone()[0] == 1)
    conn.close()


def test_ops_cli_dryrun_batch5(tmp_dir):
    cfg = make_cfg(tmp_dir)
    cfg['ops']['paths'].update(out=os.path.join(tmp_dir, 'ops-out'), dryrun_log=os.path.join(tmp_dir, 'ops-dryrun.md'), prompts=os.path.join(ROOT, 'ops', 'prompts'),
                               cost_log=os.path.join(tmp_dir, 'logs', 'ops_cost.jsonl'))
    now = ab.now_kst()
    conn = ab.db_connect(cfg)
    ops_storage.ensure_ops_schema(conn)
    before = ops_counts(conn)
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(ops_runner.subprocess, 'run', side_effect=AssertionError('dryrun cannot spawn CLI')):
            for name in ('plan', 'draft', 'compliance', 'schedule', 'publish-guide', 'measure'):
                rc, stdout, stderr = ops_cli(cfg, now, name, '--dry-run')
                result = json.loads(stdout)
                check('ops dryrun CLI ' + name, rc == 0 and result['status'] in ('ok', 'skipped'), stderr)
    check('ops six dryrun audits', conn.execute("SELECT COUNT(*) FROM ops_runs WHERE mode='dry-run'").fetchone()[0] == 6)
    check('ops six dryrun business tables untouched', before == ops_counts(conn))
    with open(cfg['ops']['paths']['dryrun_log']) as stream:
        log = stream.read()
    check('ops six dryrun log sections', log.count('\n## ') == 6)
    with patch.dict(os.environ, {}, clear=True):
        rc, stdout, stderr = ops_cli(cfg, now, 'plan')
    check('ops live missing key rc2', rc == 2 and json.loads(stdout)['reason'] == 'deepseek_key_missing')
    fixture = os.path.join(tmp_dir, 'mock.json')
    with open(fixture, 'w') as stream:
        json.dump({'plan': {'plan': 'password=do-not-leak contact@example.test opaque-key'}}, stream)
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'opaque-key'}):
        rc, stdout, stderr = ops_cli(cfg, now, 'plan', '--mock-file', fixture)
    run = conn.execute('SELECT * FROM ops_runs ORDER BY id DESC LIMIT 1').fetchone()
    with open(cfg['ops']['paths']['dryrun_log']) as stream:
        log = stream.read()
    check('ops mock CLI and all sinks safe', rc == 0 and run['mode'] == 'mock' and all(s not in stdout + stderr + log + run['output'] for s in ('do-not-leak', 'contact@example.test', 'opaque-key')))
    rc, stdout, stderr = ops_cli(cfg, now, 'plan', '--project', 'kitchengenome', '--dry-run')
    check('ops kitchen CLI blocked rc2', rc == 2 and json.loads(stdout)['reason'] == 'brand_hold')
    with patch.object(ops_pipeline, 'cmd_plan', side_effect=ValueError('password=internal-secret')):
        rc, stdout, stderr = ops_cli(cfg, now, 'plan', '--dry-run')
    check('ops unexpected error safe rc1', rc == 1 and 'internal-secret' not in stderr and 'Traceback' in stderr)
    conn.close()


def test_ops_cli_mock_lifecycle_batch5(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    evidence = dict(conn.execute('SELECT * FROM evidence WHERE id=?', (eid,)).fetchone())
    fixture = os.path.join(tmp_dir, 'responses.json')
    responses = {'draft': ops_mock('draft', {'evidence': [evidence]}),
                 'compliance': ops_mock('compliance', {}), 'plan': {'plan': '# Offline plan'}}
    with open(fixture, 'w') as stream:
        json.dump(responses, stream)
    def cli(command, *args):
        with patch.dict(os.environ, {}, clear=True), patch.object(ops_runner.subprocess, 'run', side_effect=AssertionError('mock must not spawn CLI')):
            rc, stdout, stderr = ops_cli(cfg, now, command, '--mock-file', fixture, *args)
        return rc, json.loads(stdout), stderr
    rc, result, stderr = cli('draft')
    check('ops mock CLI draft creates local rows', rc == 0 and len(result['drafts']) == 3, stderr)
    did = result['drafts'][0]['id']
    rc, result, stderr = cli('schedule', '--ops-draft-id', str(did))
    check('ops mock CLI schedule requires human approval rc2', rc == 2 and result['results'][0]['reason'] == 'draft_not_approved')
    rc, result, stderr = cli('compliance', '--ops-draft-id', str(did))
    check('ops mock CLI compliance PASS', rc == 0 and result['verdict'] == 'PASS')
    anchor = conn.execute('SELECT id FROM drafts LIMIT 1').fetchone()[0]
    ab.transition(conn, 'draft', anchor, 'fact_checked', now)
    ab.transition(conn, 'draft', anchor, 'approved', now)
    conn.commit()
    with patch.object(ab, 'record_post', side_effect=ValueError('expired during scheduling')):
        result = ops_pipeline.cmd_schedule(conn, cfg, proj, now, ops_draft_ids=[did])
    check('ops record_post ValueError maps evidence block', result['results'][0]['reason'] == 'evidence_expired' and conn.execute('SELECT COUNT(*) FROM published').fetchone()[0] == 0 and conn.execute('SELECT COUNT(*) FROM ops_schedule').fetchone()[0] == 0)
    rc, result, stderr = cli('schedule', '--ops-draft-id', str(did))
    check('ops mock CLI approved schedule', rc == 0, result)
    rc, result, stderr = cli('publish-guide', '--ops-schedule-id', '1')
    check('ops mock CLI publish guide', rc == 0 and result['results'][0]['guide']['no_browser_automation'] is True)
    responses['compliance']['checks']['C7']['pass'] = False
    with open(fixture, 'w') as stream:
        json.dump(responses, stream)
    rc, result, stderr = cli('compliance', '--ops-draft-id', str(did))
    check('ops mock CLI FAIL rc2', rc == 2 and result['verdict'] == 'FAIL')
    rc, result, stderr = cli('publish-guide', '--ops-schedule-id', '1')
    check('ops mock CLI FAIL blocks guide rc2', rc == 2 and result['results'][0]['reason'] == 'compliance_pass_required')
    pid = batch4_post(conn, now, 'cli-measure')
    diag = ab.diagnose_post(conn, cfg, pid, '48h', now)
    responses['measure'] = ops_mock('measure', diag)
    with open(fixture, 'w') as stream:
        json.dump(responses, stream)
    rc, result, stderr = cli('measure', '--post-id', pid)
    check('ops mock CLI measure records notes', rc == 0 and len(result['notes']) == 1, stderr)
    check('ops mock CLI no credential required audit', conn.execute("SELECT COUNT(*) FROM ops_runs WHERE mode='mock'").fetchone()[0] == 8)
    conn.close()


def test_ops_runner_batch5(tmp_dir):
    import subprocess
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    # These are fake process responses; the real CLI and network are never invoked.
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs, kwargs['stdin'].name))
        prompt = kwargs['stdin'].read()
        check('ops runner prompt scrubbed', 'password=hidden' not in prompt and 'contact@example.test' not in prompt)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({'result': '{"plan":"safe"}'}), 'password=stderr-secret')
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'opaque-key', 'ANTHROPIC_API_KEY': 'other-key', 'CLAUDE_CODE_OTHER': 'bad'}):
        with patch.object(ops_runner.subprocess, 'run', side_effect=fake_run):
            result = ops_runner.generate('plan', {'text': 'password=hidden contact@example.test'}, cfg)
        check('ops runner parses Claude envelope', result['plan'] == 'safe')
        cmd, kwargs, prompt_path = calls[0]
        env = kwargs['env']
        check('ops runner DeepSeek isolation', env['ANTHROPIC_BASE_URL'] == 'https://api.deepseek.com/anthropic' and env['ANTHROPIC_AUTH_TOKEN'] == 'opaque-key' and env['CLAUDE_CONFIG_DIR'] == os.path.expanduser('~/.claude-deepseek') and 'ANTHROPIC_API_KEY' not in env and 'CLAUDE_CODE_OTHER' not in env)
        check('ops runner tool and MCP isolation', cmd[cmd.index('--tools') + 1] == '' and '--strict-mcp-config' in cmd and '--dangerously-skip-permissions' not in cmd)
        check('ops runner secret not in arguments', 'opaque-key' not in str(cmd))
        check('ops runner temp prompt and cwd removed', not os.path.exists(prompt_path) and not os.path.exists(kwargs['cwd']))
        for exception, reason in ((subprocess.TimeoutExpired('fake', 1), 'llm_timeout'), (OSError('password=hidden'), 'llm_exit')):
            with patch.object(ops_runner.subprocess, 'run', side_effect=exception):
                ops_expect_block('ops runner maps ' + reason, lambda: ops_runner.generate('plan', {}, cfg), reason)
        with patch.object(ops_runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, 'password=hidden', 'opaque-key')):
            ops_expect_block('ops runner exit redacts provider details', lambda: ops_runner.generate('plan', {}, cfg), 'llm_exit')
        with patch.object(ops_runner.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'not json sk-secret', '')):
            ops_expect_block('ops runner invalid JSON', lambda: ops_runner.generate('plan', {}, cfg), 'llm_output_invalid')
    for text in ('[]', 'null', '{"is_error":true,"result":"bad"}'):
        ops_expect_block('ops runner invalid envelope', lambda: ops_runner.parse_response(text), 'llm_output_invalid')
    conn.close()


def test_ops_source_guards_batch5(tmp_dir):
    import ast
    sources = []
    for name in os.listdir(os.path.join(ROOT, 'ops')):
        if name.endswith('.py'):
            with open(os.path.join(ROOT, 'ops', name)) as stream:
                sources.append((name, stream.read()))
    calls = [n for _, source in sources for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)]
    check('ops does not collect global due checks', not any(isinstance(n.func, ast.Attribute) and n.func.attr == 'run_due_checks' for n in calls))
    source = '\n'.join(s for _, s in sources)
    check('ops network restricted to X adapter', all('urllib.request' not in s and 'urlopen(' not in s for name, s in sources if name != 'x_public.py'))
    check('ops has no browser, payment, git or social SDK calls', all(term not in source for term in ('git push', 'git commit', 'playwright', 'selenium', 'stripe', 'tweepy')))
    check('ops package init empty', os.path.getsize(os.path.join(ROOT, 'ops', '__init__.py')) == 0)
    for name in ('config.py', 'guards.py', 'storage.py', 'mocks.py'):
        check('ops independent module ' + name, 'import autobot' not in dict(sources)[name])


def live_ops_fixture(tmp_dir):
    cfg, conn, proj, now, qid, eid = ops_fixture(tmp_dir)
    for key, value in dict(daily_packages='daily', history_json='history.json', manual_metrics='manual',
                           strategy_next='strategy-next.md', strategies='strategies.md',
                           weekly_plan_glob='hanset-weekly-plan-*.md', cost_log='ops_cost.jsonl').items():
        cfg['ops']['paths'][key] = os.path.join(tmp_dir, value)
    with open(cfg['ops']['paths']['history_json'], 'w') as stream: json.dump([], stream)
    with open(cfg['ops']['paths']['strategies'], 'w') as stream: stream.write('HANSET 체크리스트\nKitchen Genome hold')
    cfg['paths']['rounds_log'] = os.path.join(tmp_dir, 'rounds.log')
    cfg['paths']['status_md'] = os.path.join(tmp_dir, 'status.md')
    with open(cfg['paths']['rounds_log'], 'w') as stream: stream.write('')
    return cfg, conn, proj, now


def test_ops_package_batch6(tmp_dir):
    import copy
    import glob
    from ops import packet, package
    cfg, conn, proj, now = live_ops_fixture(tmp_dir)
    source = os.path.join(HERE, 'fixtures', 'factcheck-1108.md')
    with open(source) as stream: raw = stream.read()
    parsed = packet.parse_packet(raw, '1108')
    check('b6 explicit separate packet dates/id', (parsed['id'], parsed['package_date'], parsed['checked'], parsed['checked_phrase']) ==
          ('2026-11-08_kintex_getting_there', '2026-11-08', '2026-11-07', 'checked Nov 7'))
    corpus = '\n'.join(f['text'] for f in parsed['facts'])
    check('b6 nested buses with source provenance', any('9703' in f['text'] and f['sources'] and 'transportation' in f['sources'][0] for f in parsed['facts']))
    check('b6 packet excludes reference/correction', all(t not in corpus for t in ('EPIC SEOUL', '17:50', '18:40', 'Do not invent', 'do not say')))
    check('b6 card hint D-5 preserved', any('D-5' in f['text'] for f in parsed['facts']))
    check('b6 original positions and sections retained', all(f['line'] > 0 and f['section'] for f in parsed['facts']) and bool(parsed['drafts']))
    for label, text, date in [('year', raw.replace('2026-11-07', '11-07').replace('2026-11-08', '11-08'), '1108'),
                              ('date mismatch', raw, '1109'), ('invalid day', raw, '0231'),
                              ('content traversal', raw.replace('1108_kintex_getting_there', '../evil'), '1108'),
                              ('checked conflict', raw+'\nchecked Nov 8', '1108')]:
        try: packet.parse_packet(text, date)
        except ops_guards.OpsError: check('b6 packet rejects '+label, True)
        else: check('b6 packet rejects '+label, False)
    payload = dict(packet=parsed, motifs=list(package.MOTIFS), palettes=list(package.PALETTES))
    response = package.mock_package(payload)
    check('b6 mock obeys all final guards', package.validate(response, parsed, [], cfg, proj) == [])
    for path in ('post.title', 'post.subtitle', 'post.cover.kicker', 'post.summary.0', 'post.sources.0', 'post.items.0.price', 'post.cta', 'post.label', 'post.footer', 'x', 'threads', 'ig_caption', 'reddit'):
        for injection in (' 999999원', ' RUNPORT', ' test@example.org', ' 010 - 1234 - 5678'):
            bad = copy.deepcopy(response); node = bad
            keys = path.split('.')
            for k in keys[:-1]: node = node[int(k)] if isinstance(node, list) else node[k]
            key = int(keys[-1]) if isinstance(node, list) else keys[-1]
            node[key] += injection
            check('b6 all visible guards '+path+injection, bool(package.validate(bad, parsed, [], cfg, proj)))
    for text in ('Entry fee ₩3,720 per hour', 'Race starts 05:00–23:00', 'HYROX is in Hall 2'):
        bad = copy.deepcopy(response); bad['post']['title'] = text; bad['claim_refs']['post.title'] = [f['fact_id'] for f in parsed['facts']]
        check('b6 semantic reassignment blocked '+text, bool(package.validate(bad, parsed, [], cfg, proj)))
    bus = next(f for f in parsed['facts'] if f['text'].startswith('9703'))
    bad = copy.deepcopy(response); bad['post']['items'][0]['memo'] = bus['text']; bad['claim_refs']['post.items.0.memo'] = [bus['fact_id']]
    check('b6 official Hall 2 bus stop allowed', package.validate(bad, parsed, [], cfg, proj) == [])
    # A URL that is part of an allowed fact is packet-sourced, not hallucinated.
    url_fact = next(f for f in parsed['facts'] if 'https://www.kintex.com/' in f['text'])
    bad = copy.deepcopy(response); bad['post']['items'][0]['memo'] = url_fact['text']
    bad['claim_refs']['post.items.0.memo'] = [url_fact['fact_id']]
    check('b6 fact-embedded URL allowed', package.validate(bad, parsed, [], cfg, proj) == [])
    # Contentless output cannot pass: empty presentation text or a channel with
    # no referenced fact is rejected.
    empty = copy.deepcopy(response); empty['post']['title'] = ''
    try: package.validate(empty, parsed, [], cfg, proj)
    except ops_guards.OpsError: check('b6 empty title rejected', True)
    else: check('b6 empty title rejected', False)
    empty = copy.deepcopy(response); empty['post']['summary'][0] = '  '
    try: package.validate(empty, parsed, [], cfg, proj)
    except ops_guards.OpsError: check('b6 empty summary rejected', True)
    else: check('b6 empty summary rejected', False)
    empty = copy.deepcopy(response); empty['post']['items'][0].update(area='', price='', memo='')
    try: package.validate(empty, parsed, [], cfg, proj)
    except ops_guards.OpsError: check('b6 contentless item rejected', True)
    else: check('b6 contentless item rejected', False)
    empty = copy.deepcopy(response); empty['claim_refs'].pop('x')
    check('b6 channel without fact rejected', 'package_claim_refs' in package.validate(empty, parsed, [], cfg, proj))
    for path, value in [('seed', True), ('layout', 'oops'), ('palette', 'unknown')]:
        bad = copy.deepcopy(response); bad['post']['cover'][path] = value
        try: package.validate(bad, parsed, [], cfg, proj)
        except ops_guards.OpsError: check('b6 schema cover '+path, True)
        else: check('b6 schema cover '+path, False)
    for key, value in [('items_per_slide', True), ('items_per_slide', 0), ('id', '../oops'), ('uninspected', 'hidden')]:
        bad = copy.deepcopy(response); bad['post'][key] = value
        try: package.validate(bad, parsed, [], cfg, proj)
        except ops_guards.OpsError: check('b6 schema '+key+str(value), True)
        else: check('b6 schema '+key+str(value), False)
    for suffix in ('&utm_campaign=wrong', '&utm_content=evil'):
        bad = copy.deepcopy(response); bad['x'] += suffix
        check('b6 UTM duplicates blocked '+suffix, 'package_utm' in package.validate(bad, parsed, [], cfg, proj))
    bad = copy.deepcopy(response); bad['ig_caption'] += '\nhanset.soulbound.co.kr'
    check('b6 IG bare domain forbidden', 'package_ig_link' in package.validate(bad, parsed, [], cfg, proj))
    check('b6 fixed weighted boundaries', ops_guards.x_weighted_length('a'*280) == 280 and ops_guards.x_weighted_length('a'*281) == 281 and ops_guards.x_weighted_length('₩한😀') == 6)
    cfg['ops']['limits'].update(url_weight=1, wide_char_weight=1, x_weighted=999, threads_chars=999)
    bad = copy.deepcopy(response); bad['x'] = 'HANSET'*47+'\n'+parsed['checked_phrase']+'\n'+response['x'].splitlines()[-1]
    check('b6 package cannot relax X weights via config', 'x_length_exceeded' in package.validate(bad, parsed, [], cfg, proj))
    bad = copy.deepcopy(response)
    bad['threads'] = ' ' * (500 - len(bad['threads'])) + bad['threads']
    check('b6 final Threads 500 allowed', package.validate(bad, parsed, [], cfg, proj) == [])
    bad['threads'] = ' ' + bad['threads']
    check('b6 final Threads 501 blocked', 'threads_length_exceeded' in package.validate(bad, parsed, [], cfg, proj))
    seeded = copy.deepcopy(response); del seeded['post']['cover']['seed']
    check('b6 code default seed', package.validate(seeded, parsed, [], cfg, proj) == [] and type(seeded['post']['cover']['seed']) is int)
    hist = [dict(id='other', motif=response['post']['cover']['motif'], palette='clay')]
    check('b6 motif alone dedup', 'cover_duplicate' in package.validate(response, parsed, hist, cfg, proj))
    hist = [dict(id='other', motif='legacy', palette='unknown', bg=package.PALETTES[response['post']['cover']['palette']])]
    check('b6 background alone dedup', 'cover_duplicate' in package.validate(response, parsed, hist, cfg, proj))
    with open(cfg['ops']['paths']['history_json'], 'w') as stream:
        json.dump([dict(id=str(i), motif='legacy', palette='sky') for i in range(7)] + [dict(id=parsed['id'], motif='ticket', palette='mint')], stream)
    check('b6 exclude same id BEFORE last six', [e['id'] for e in package.history(cfg, parsed['id'])] == [str(i) for i in range(1, 7)])
    with open(cfg['ops']['paths']['history_json'], 'w') as stream: stream.write('broken')
    ops_expect_block('b6 malformed history fail closed', lambda: package.history(cfg, parsed['id']), 'cover_history_invalid')
    with open(cfg['ops']['paths']['history_json'], 'w') as stream: json.dump([], stream)
    target = os.path.join(tmp_dir, 'success')
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', out_dir=target, mock=ops_mock)
    check('b6 package end to end PASS', result['status'] == 'ok', result)
    check('b6 complete render contract files', set(os.listdir(target)) == {'post.json', 'x.txt', 'threads.txt', 'ig_caption.txt', 'reddit.md', 'cards'})
    check('b6 no render output', os.listdir(os.path.join(target, 'cards')) == [])
    check('b6 package linked to audit run', conn.execute('SELECT run_id,compliance_verdict FROM ops_packages').fetchone()['compliance_verdict'] == 'PASS')
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108',
                                      out_dir=os.path.join(tmp_dir, 'schema-block'), mock={'package': {'post': {}}})
    row = conn.execute('SELECT compliance_verdict,status FROM ops_packages ORDER BY id DESC LIMIT 1').fetchone()
    check('b6 non-compliance block marked skipped',
          result['status'] == 'blocked' and row['compliance_verdict'] == 'SKIPPED' and row['status'] == 'blocked')
    ops_expect_block('b6 existing output preserved', lambda: ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', out_dir=target, mock=ops_mock), 'output_exists')
    # Length-only failures may retry; forbidden content must never retry.
    long = copy.deepcopy(response); fact = min((f for f in parsed['facts'] if f['subject'] != 'card hint' and not ops_guards.URL.search(f['text'])), key=lambda f: len(f['text']))
    long['x'] = (fact['text']+'\n')*5+parsed['checked_phrase']+'\n'+response['x'].splitlines()[-1]
    fixtures = {'package': [long, response], 'compliance': ops_mock('compliance', {})}
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', out_dir=os.path.join(tmp_dir, 'retry'), mock=fixtures)
    check('b6 shortening retry succeeds', result['status'] == 'ok' and fixtures['package'] == [], result)
    forbidden = copy.deepcopy(response); forbidden['x'] += ' RUNPORT'
    fixtures = {'package': [forbidden, response]}
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', out_dir=os.path.join(tmp_dir, 'forbidden'), mock=fixtures)
    check('b6 forbidden never retries', result['status'] == 'blocked' and len(fixtures['package']) == 1)
    duplicate = copy.deepcopy(response); duplicate['post']['cover']['motif'] = 'ticket'
    with open(cfg['ops']['paths']['history_json'], 'w') as stream: json.dump([dict(id='old', motif='ticket', palette='clay')], stream)
    fresh = copy.deepcopy(response); fresh['post']['cover']['motif'] = 'tattoo'
    fixtures = {'package': [duplicate, fresh], 'compliance': ops_mock('compliance', {})}
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', out_dir=os.path.join(tmp_dir, 'cover-retry'), mock=fixtures)
    check('b6 cover retry succeeds', result['status'] == 'ok' and not fixtures['package'], result)
    with open(cfg['ops']['paths']['history_json'], 'w') as stream: json.dump([], stream)
    fixtures = {'package': [copy.deepcopy(long) for _ in range(4)]}
    blocked_target = os.path.join(tmp_dir, 'blocked')
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', out_dir=blocked_target, mock=fixtures)
    check('b6 exactly four attempts and no partial publication', result['status'] == 'blocked' and not fixtures['package'] and not os.path.exists(blocked_target) and not glob.glob(blocked_target+'*.partial'))
    check('b6 blocked report only reason/check/hash', set(json.load(open(os.path.join(result['output_dir'], 'COMPLIANCE_FAIL.json')))) == {'reasons', 'checks', 'packet_hash'})
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', out_dir=blocked_target, mock=ops_mock)
    check('b6 blocked date can be retried', result['status'] == 'ok')
    fail = ops_mock('compliance', {}); fail['checks']['C2']['pass'] = False
    cfg['ops']['compliance']['block_on_fail'] = False
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', out_dir=os.path.join(tmp_dir, 'fail'), mock={'package': response, 'compliance': fail})
    check('b6 package compliance mandatory', result['status'] == 'blocked' and not os.path.exists(os.path.join(tmp_dir, 'fail')))
    before = conn.execute('SELECT count(*) FROM ops_packages').fetchone()[0]
    result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date='1108', dry_run=True)
    check('b6 package preview isolated and no business writes', result['status'] == 'ok' and '/previews/' in result['output_dir'] and not os.path.exists(cfg['ops']['paths']['daily_packages']) and conn.execute('SELECT count(*) FROM ops_packages').fetchone()[0] == before)
    check('b6 dryrun/mock have no cost log', not os.path.exists(cfg['ops']['paths']['cost_log']))
    # Draft also cannot bypass automatic compliance, even with block_on_fail false.
    def fail_draft(command, data): return fail if command == 'compliance' else ops_mock(command, data)
    result = ops_pipeline.cmd_draft(conn, cfg, proj, now, mock=fail_draft)
    check('b6 draft mandatory compliance', result['status'] == 'blocked' and all(d['status'] == 'blocked' for d in result['drafts']))
    conn.close()


def test_ops_live_packets_batch6(tmp_dir):
    import re
    from ops import packet
    cfg, conn, proj, now = live_ops_fixture(tmp_dir)
    for mmdd, content in (('1109', '1109_hyrox_d4'), ('1110', '1110_race_prep_checklist')):
        source = os.path.join(HERE, 'fixtures', f'factcheck-{mmdd}.md')
        with open(source) as stream: raw = stream.read()
        parsed = packet.parse_packet(raw, mmdd)
        check(f'b6 {mmdd} live packet ready', parsed['id'].endswith(content[5:]) and parsed['checked'] and parsed['facts'])
        check(f'b6 {mmdd} landing from packet', parsed['landing'] == 'https://hanset.soulbound.co.kr/')
        check(f'b6 {mmdd} official source carried',
              any('hyrox.com/event/hyrox-seoul' in url for url in parsed['sources']))
        check(f'b6 {mmdd} UTM content from packet', parsed['content'] == content)
        corpus = '\n'.join(f['text'] for f in parsed['facts'])
        check(f'b6 {mmdd} no refund claim in allowed facts', not re.search(r'refund|환불', corpus, re.I))
        if mmdd == '1109':
            check('b6 1109 name-change fact retained', 'Lite Flex' in corpus)
            check('b6 1109 refund class forbidden', {'환불', 'refund'} <= set(parsed['forbidden']))
        else:
            check('b6 1110 refund directive forbidden', {'환불', 'refund'} <= set(parsed['forbidden']))
        target = os.path.join(tmp_dir, 'daily-' + mmdd)
        result = ops_pipeline.cmd_package(conn, cfg, proj, now, packet=source, date=mmdd, out_dir=target, mock=ops_mock)
        check(f'b6 {mmdd} end to end package', result['status'] == 'ok' and os.path.isdir(target), result)
        check(f'b6 {mmdd} render contract files',
              set(os.listdir(target)) == {'post.json', 'x.txt', 'threads.txt', 'ig_caption.txt', 'reddit.md', 'cards'})
        with open(os.path.join(target, 'post.json')) as stream: post = json.load(stream)
        check(f'b6 {mmdd} post identity and checked', post['id'] == parsed['id'] and post['checked'] == parsed['checked'])
    conn.close()


def test_ops_metrics_strategy_batch6(tmp_dir):
    import copy
    from ops import metrics, x_public, strategy
    cfg, conn, proj, now = live_ops_fixture(tmp_dir)
    pid = batch4_post(conn, now-timedelta(days=8), 'manual-b6', channel='instagram')
    url = 'https://www.instagram.com/p/example/'
    conn.execute('UPDATE published SET url=? WHERE post_id=?', (url, pid)); conn.commit()
    path = os.path.join(cfg['ops']['paths']['manual_metrics'], pid+'_48h.json'); os.makedirs(os.path.dirname(path))
    manual = dict(post_id=pid, check_kind='48h', channel='instagram', public_url=url, observed_at=ab.iso(now), metrics=dict(likes=3, replies=0, reposts=0))
    def write(value):
        with open(path, 'w') as stream: json.dump(value, stream)
    write(manual)
    result = ops_pipeline.cmd_measure(conn, cfg, proj, now, post_id=pid, check_kind='48h', mock=ops_mock)
    check('b6 manual collects canonical zero metrics', result['status'] == 'ok' and conn.execute("SELECT count(*) FROM metric_observations WHERE metric IN ('replies','reposts') AND value=0").fetchone()[0] == 2)
    check('b6 unknown is not numeric observation', conn.execute("SELECT count(*) FROM metric_observations WHERE metric='views'").fetchone()[0] == 0 and result['snapshots'][0]['metrics']['views'] == 'unverified')
    check('b6 measure note snapshot linked', conn.execute('SELECT snapshot_id FROM ops_measure_notes').fetchone()[0] == result['snapshots'][0]['snapshot_id'])
    count = conn.execute('SELECT count(*) FROM ops_metric_snapshots').fetchone()[0]
    ops_pipeline.cmd_measure(conn, cfg, proj, now+timedelta(minutes=1), post_id=pid, check_kind='48h', mock=ops_mock)
    check('b6 same manual snapshot dedup ignores collection time', conn.execute('SELECT count(*) FROM ops_metric_snapshots').fetchone()[0] == count)
    post = conn.execute('SELECT * FROM published WHERE post_id=?', (pid,)).fetchone()
    for key, value in [('post_id','other'),('channel','threads'),('public_url','https://evil.invalid/p/a'),('check_kind','7d'),('observed_at',ab.iso(now+timedelta(seconds=1))),('observed_at',ab.iso(now-timedelta(days=8))),('observed_at','2026-11-09T00:00:00'),('project','other')]:
        bad = copy.deepcopy(manual); bad[key] = value; write(bad)
        try: metrics.snapshot(post, '48h', cfg, now, mock=ops_mock)
        except ops_guards.OpsError: check('b6 manual identity/time '+key+str(value), True)
        else: check('b6 manual identity/time '+key+str(value), False)
    for bad_metrics in ({'comments':0}, {'likes':True}, {'likes':-1}, {'likes':1.5}, {'likes':None}):
        bad = dict(manual, metrics=bad_metrics); write(bad)
        ops_expect_block('b6 invalid metric '+str(bad_metrics), lambda: metrics.snapshot(post, '48h', cfg, now, mock=ops_mock), 'manual_metrics_invalid')
    write(manual)
    for field in ('hypothesis','actual_with_denominator','alternative_causes','next_single_change'):
        def fabricate(command, data, field=field):
            value = ops_mock(command, data); value[field] = ['views 999'] if field == 'alternative_causes' else 'views 999'; return value
        ops_expect_block('b6 fabricated measure '+field, lambda: ops_pipeline.cmd_measure(conn, cfg, proj, now, post_id=pid, check_kind='48h', mock=fabricate), 'measure_fabricated_number')
    check('b6 blocked LLM preserves raw snapshot', conn.execute('SELECT count(*) FROM ops_metric_snapshots').fetchone()[0] == count)
    altered = ops_mock('measure', dict(hypothesis='가설', actual_with_denominator='likes 3', alternative_causes=['관측 부족']))
    altered['actual_with_denominator'] = 'views 3'
    ops_expect_block('b6 same number wrong metric blocked', lambda: metrics.validate_note(altered, 'likes 3'), 'measure_fabricated_number')
    os.unlink(path)
    result = ops_pipeline.cmd_measure(conn, cfg, proj, now, post_id=pid, check_kind='7d', mock=ops_mock)
    check('b6 missing manual file becomes all unknown', set(result['snapshots'][0]['metrics'].values()) == {'unverified'} and result['notes'][0]['verdict'] == '판정 보류')
    before = ops_counts(conn); snapshots_before = conn.execute('SELECT count(*) FROM ops_metric_snapshots').fetchone()[0]
    ops_pipeline.cmd_measure(conn, cfg, proj, now, post_id=pid, check_kind='48h', dry_run=True)
    check('b6 measure dryrun read only', before == ops_counts(conn) and conn.execute('SELECT count(*) FROM ops_metric_snapshots').fetchone()[0] == snapshots_before)
    # Fixed-host read-only X adapter, with a fake transport for every enabled path.
    cfg['ops']['measure']['x_public'] = True
    xurl = 'https://x.com/hanset_mag/status/12345'
    class Response(io.BytesIO):
        status=200
        def geturl(self): return 'https://api.x.com/2/tweets/12345?tweet.fields=public_metrics'
    requests=[]
    def transport(request, timeout):
        requests.append(request)
        return Response(json.dumps({'data':{'id':'12345','public_metrics':{'like_count':3,'reply_count':0,'retweet_count':0}}}).encode())
    with patch.dict(os.environ, {'X_BEARER_TOKEN':'x-private-credential'}):
        check('b6 live X maps canonical metrics', x_public.collect(xurl, cfg, transport=transport) == dict(likes=3,replies=0,reposts=0,views='unverified'))
        check('b6 X only fixed HTTPS GET', requests[0].method == 'GET' and requests[0].full_url.startswith('https://api.x.com/2/tweets/'))
        for kwargs in ({'dry_run':True},{'mock':{}},{'mock':ops_mock}):
            check('b6 mock/dry X no network '+str(kwargs), set(x_public.collect(xurl,cfg,transport=lambda *a,**k: (_ for _ in ()).throw(AssertionError()),**kwargs).values()) == {'unverified'})
        for bad_url in ('http://x.com/a/status/12345','https://evil.test/a/status/12345','https://x.com/a/status/notnumber'):
            check('b6 X URL validation '+bad_url, set(x_public.collect(bad_url,cfg,transport=transport).values()) == {'unverified'})
        def error(*args, **kwargs): raise ValueError('x-private-credential')
        check('b6 X failure unknown', set(x_public.collect(xurl,cfg,transport=error).values()) == {'unverified'})
        cfg['ops']['measure']['x_public'] = False
        check('b6 X disabled never calls transport', set(x_public.collect(xurl,cfg,transport=error).values()) == {'unverified'})
        check('b6 X credential redacted', 'x-private-credential' not in ops_guards.redact('x-private-credential'))
    class Redirect(Response):
        def geturl(self): return 'https://evil.test/'
    cfg['ops']['measure']['x_public'] = True
    with patch.dict(os.environ, {'X_BEARER_TOKEN':'opaque'}):
        check('b6 X rejects redirects', set(x_public.collect(xurl,cfg,transport=lambda *a,**k: Redirect(b'{}')).values()) == {'unverified'})
        check('b6 X identity error unknown', set(x_public.collect(xurl,cfg,transport=lambda *a,**k: Response(b'{"data":{"id":"other","public_metrics":{}}}')).values()) == {'unverified'})
    check('b6 X redirect handler refuses', x_public.NoRedirect().redirect_request(None,None,302,'',{},'https://evil') is None)
    result = ops_pipeline.cmd_plan(conn, cfg, proj, now, mode='next-week', mock=ops_mock)
    check('b6 strategy unknown forced defer', result['status'] == 'ok' and all(i['action']=='defer' for i in result['recommendations']) and '판정 보류' in open(result['path']).read())
    # Create independent verified snapshot evidence for keep/kill/iterate.
    original = metrics.snapshot(post, '48h', cfg, now, mock=ops_mock)
    original.update(source='manual', observed_at=ab.iso(now), metrics=dict(likes=3,replies=0,reposts=0,views=50))
    strategy_sid = metrics.save_snapshot(conn, original, now)
    conn.execute("UPDATE ops_measure_notes SET snapshot_id=?,verdict='pass',category=NULL WHERE post_id=? AND check_kind='48h'", (strategy_sid, pid))
    conn.execute("UPDATE diagnoses SET verdict='pass',category=NULL WHERE post_id=? AND check_kind='48h'", (pid,)); conn.commit()
    ctx = strategy.build_context(conn,cfg,proj,now)
    keep_ref = next(k for k,v in ctx['evidence'].items() if v['verdict']=='pass')
    recommendations = [dict(topic='HANSET',format='카드',action='keep',evidence_refs=[keep_ref],reason='검증된 진단에 따라 유지',template_hint='훅: 질문형; 카드: 기존 구성; 채널: x, threads, instagram')]
    result = ops_pipeline.cmd_plan(conn,cfg,proj,now,mode='next-week',mock={'plan_next':{'recommendations':recommendations}})
    check('b6 strategy supported keep and metrics output', result['recommendations'][0]['action']=='keep' and '"views": 50' in open(result['path']).read())
    relaxed = dict(recommendations[0], format='카드뉴스', reason='근거 확인 후 유지',
                   template_hint='훅: 재사용; 카드: 요약 장수; 채널: x, threads')
    result = ops_pipeline.cmd_plan(conn,cfg,proj,now,mode='next-week',mock={'plan_next':{'recommendations':[relaxed]}})
    check('b6 strategy live phrasing accepted', result['recommendations'][0]['action']=='keep')
    conn.execute("UPDATE diagnoses SET verdict='diagnosed',category='low_impressions' WHERE post_id=? AND check_kind='48h'",(pid,))
    conn.execute("UPDATE ops_measure_notes SET verdict='diagnosed',category='low_impressions' WHERE snapshot_id=?", (strategy_sid,)); conn.commit()
    for action in ('kill','iterate'):
        recommendation = dict(recommendations[0],action=action,reason='검증된 진단에 따라 종료' if action=='kill' else '검증된 진단에 따라 개선',template_hint='retire: 검증된 실패 진단' if action=='kill' else '단일 변수 개선')
        result = ops_pipeline.cmd_plan(conn,cfg,proj,now,mode='next-week',mock={'plan_next':{'recommendations':[recommendation]}})
        check('b6 strategy supported '+action, result['recommendations'][0]['action']==action)
    bad = dict(recommendations[0],reason='likes 999')
    ops_expect_block('b6 strategy invented numbers',lambda:ops_pipeline.cmd_plan(conn,cfg,proj,now,mode='next-week',mock={'plan_next':{'recommendations':[bad]}}),'strategy_fabricated_number')
    held = dict(recommendations[0],topic='KitchenGenome')
    result=ops_pipeline.cmd_plan(conn,cfg,proj,now,mode='next-week',mock={'plan_next':{'recommendations':[held]}})
    check('b6 KG hold omitted from recommendation', result['recommendations']==[])
    operational = open(cfg['ops']['paths']['strategy_next']).read()
    result=ops_pipeline.cmd_plan(conn,cfg,proj,now,mode='next-week',dry_run=True)
    check('b6 next-week dryrun preview isolated', '/previews/' in result['path'] and open(cfg['ops']['paths']['strategy_next']).read()==operational)
    # Selection follows packet dates, never mtime.
    for name,text in [('hanset-weekly-plan-1101.md','2026-11-01 old'),('hanset-weekly-plan-1108.md','2026-11-08 HANSET')]:
        with open(os.path.join(tmp_dir,name),'w') as stream:stream.write(text)
    plan_now = datetime(2026,11,10,tzinfo=now.tzinfo)
    check('b6 weekly plan chooses filename/body date', '2026-11-08' in strategy.weekly_plan(cfg,plan_now))
    with open(os.path.join(tmp_dir,'hanset-weekly-plan-1108.md'),'w') as stream:
        stream.write('# HANSET 주간 소셜 플랜 (11/8 ~ 11/14)\n작성: 2026-11-06 KST\nHANSET')
    check('b6 weekly authored date distinct from start', '2026-11-06' in strategy.weekly_plan(cfg,plan_now))
    with open(os.path.join(tmp_dir,'hanset-weekly-plan-1108.md'),'w') as stream:stream.write('2026-11-09 conflict')
    ops_expect_block('b6 weekly date conflict',lambda:strategy.weekly_plan(cfg,plan_now),'weekly_plan_date_conflict')
    with open(os.path.join(tmp_dir,'hanset-weekly-plan-1108.md'),'w') as stream:
        stream.write('# HANSET 주간 플랜 (2026-11-08 ~ 2026-11-14)\n작성: 2026-11-08\nHANSET\n')
    check('b6 weekly same-week date range accepted', '2026-11-08' in strategy.weekly_plan(cfg,plan_now))
    # Both round header formats accepted, without changing the queue identity.
    entries=ab.parse_rounds_log('=== 2026-11-01 19:00 KST HANSET ===\n- HANSET posted '+url,cfg['_projects'])
    check('b6 real rounds header timestamp', entries and entries[0][3].year==2026 and entries[0][3].day==1)
    conn.close()


def test_ops_cost_cli_batch6(tmp_dir):
    import subprocess
    from ops import cli
    cfg,conn,proj,now=live_ops_fixture(tmp_dir)
    with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'opaque-b6-secret'}):
        for outcome in (subprocess.CompletedProcess([],0,'{"plan":"ok"}',''),subprocess.CompletedProcess([],0,'invalid',''),subprocess.TimeoutExpired('cli',1)):
            try:
                with patch.object(ops_runner.subprocess,'run',side_effect=outcome if isinstance(outcome,Exception) else None,return_value=outcome):
                    ops_runner.generate('plan',{'text':'opaque-b6-secret'},cfg)
            except ops_guards.OpsError:pass
    records=[json.loads(line) for line in open(cfg['ops']['paths']['cost_log'])]
    check('b6 one cost line per attempted live call',len(records)==3)
    check('b6 minimal cost fields and no credentials', all(set(r)=={'model','command','input_tokens_est','output_tokens_est','latency_ms'} for r in records) and 'opaque-b6-secret' not in open(cfg['ops']['paths']['cost_log']).read() and records[-1]['output_tokens_est'] is None)
    source=os.path.join(HERE,'fixtures','factcheck-1108.md')
    rc,out,err=ops_cli(cfg,now,'package','--packet',source,'--date','1108','--dry-run')
    check('b6 package CLI wired',rc==0 and json.loads(out)['status']=='ok',out+err)
    rc,out,err=ops_cli(cfg,now,'plan','--mode','next-week','--dry-run')
    check('b6 strategy CLI wired',rc==0 and json.loads(out)['status']=='ok',out+err)
    audit=conn.execute('SELECT prompt_path FROM ops_runs ORDER BY id DESC LIMIT 1').fetchone()[0]
    check('b6 next-week prompt audit correct', audit.endswith('plan_next.md'))
    conn.close()


def main():
    import tempfile
    print("offline tests for autobot.py")
    real_urlopen = urllib.request.urlopen

    def local_urlopen(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else request
        if urllib.parse.urlsplit(url).hostname not in ("127.0.0.1", "localhost", "::1"):
            check("offline: external HTTP request forbidden", False)
            raise AssertionError("offline tests permit loopback HTTP only")
        return real_urlopen(request, *args, **kwargs)

    urllib.request.urlopen = local_urlopen
    server = start_server()
    try:
        for test in (test_ops_package_batch6, test_ops_live_packets_batch6, test_ops_metrics_strategy_batch6, test_ops_cost_cli_batch6):
            test(tempfile.mkdtemp(prefix='ab-batch6-'))
        for test in (test_ops_cli_mock_lifecycle_batch5, test_ops_config_batch5, test_ops_guards_batch5, test_ops_plan_batch5,
                     test_ops_draft_batch5, test_ops_compliance_batch5, test_ops_schedule_batch5,
                     test_ops_publish_guide_batch5, test_ops_measure_batch5, test_ops_cli_dryrun_batch5,
                     test_ops_runner_batch5, test_ops_source_guards_batch5):
            test(tempfile.mkdtemp(prefix='ab-batch5-'))
        test_experiment_create_batch4(tempfile.mkdtemp(prefix="ab-batch4-create-"))
        test_funnel_snapshots_batch4(tempfile.mkdtemp(prefix="ab-batch4-funnel-"))
        test_diagnosis_five_class_batch4(tempfile.mkdtemp(prefix="ab-batch4-diagnose-"))
        test_judgment_hold_batch4(tempfile.mkdtemp(prefix="ab-batch4-hold-"))
        test_rules_promote_rollback_batch4(tempfile.mkdtemp(prefix="ab-batch4-rules-"))
        test_rule_regression_batch4(tempfile.mkdtemp(prefix="ab-batch4-regression-"))
        test_config_batch4(tempfile.mkdtemp(prefix="ab-batch4-config-"))
        test_evaluation_edges_batch4(tempfile.mkdtemp(prefix="ab-batch4-edges-"))
        test_channel_formats_batch3(tempfile.mkdtemp(prefix="ab-batch3-formats-"))
        test_approval_queue(tempfile.mkdtemp(prefix="ab-batch3-queue-"))
        test_metrics_three_tiers(tempfile.mkdtemp(prefix="ab-batch3-metrics-"))
        test_config_batch3(tempfile.mkdtemp(prefix="ab-batch3-config-"))
        test_question_candidates(tempfile.mkdtemp(prefix="ab-questions-"))
        test_evidence_validation(tempfile.mkdtemp(prefix="ab-evidence-"))
        test_evidence_expiry(tempfile.mkdtemp(prefix="ab-expiry-"))
        test_needs_research_and_no_demand(tempfile.mkdtemp(prefix="ab-research-"))
        test_hooks(tempfile.mkdtemp(prefix="ab-hooks-"))
        test_question_draft_packet(tempfile.mkdtemp(prefix="ab-packet-"))
        test_config_batch2(tempfile.mkdtemp(prefix="ab-batch2-config-"))
        test_batch2_cli_lifecycle(tempfile.mkdtemp(prefix="ab-batch2-cli-"))
        test_utm()
        test_urls()
        test_dates()
        test_robots_rules()
        test_rounds_parsing(tempfile.mkdtemp(prefix="ab-rounds-"))
        test_drafts(tempfile.mkdtemp(prefix="ab-drafts-"))
        test_crawl(tempfile.mkdtemp(prefix="ab-crawl-"), server)
        test_feed_fallback_config(tempfile.mkdtemp(prefix="ab-feed1-"))
        test_feed_fallback_autodiscover(tempfile.mkdtemp(prefix="ab-feed2-"))
        test_discovery_robots_blocked(tempfile.mkdtemp(prefix="ab-disc1-"))
        test_discovery_reddit_store(tempfile.mkdtemp(prefix="ab-disc2-"))
        test_status(tempfile.mkdtemp(prefix="ab-status-"), server)
        test_status_write(tempfile.mkdtemp(prefix="ab-write-"))
        test_env_and_slack(tempfile.mkdtemp(prefix="ab-env-"))
        test_daemon_stages_and_fallback(tempfile.mkdtemp(prefix="ab-daemon-"))
        test_is_limit_or_unavailable_content()
        test_config_batch1(tempfile.mkdtemp(prefix="ab-config-"))
        test_metric_retry(tempfile.mkdtemp(prefix="ab-retry-"))
        test_page_reverify(tempfile.mkdtemp(prefix="ab-reverify-"), server)
        test_post_id_state_machine(tempfile.mkdtemp(prefix="ab-state-"))
    finally:
        server.shutdown()
        urllib.request.urlopen = real_urlopen
    print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
    for failure in FAILURES:
        print("  -", failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
