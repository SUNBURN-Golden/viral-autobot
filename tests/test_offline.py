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
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime
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
def make_cfg(tmp_dir, base_url=None, rounds_text=None, **over):
    cfg = ab.load_config(os.path.join(ROOT, "config.json"))
    cfg["paths"]["db"] = os.path.join(tmp_dir, "autobot.db")
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
    cfg = make_cfg(tmp_dir, rounds_text=ROUNDS)
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
        "design": [("opus55", "claude", "claude-opus-5-5", "medium"),
                   ("astra6", "codex", "gpt-6-astra", "high"),
                   ("glm", "glm", None, None),
                   ("swe2", "devin", "swe-2", None)],
        "design_review": [("astra6", "codex", "gpt-6-astra", "high"),
                          ("opus55", "claude", "claude-opus-5-5", "medium"),
                          ("glm", "glm", None, None),
                          ("swe2", "devin", "swe-2", None)],
        "build": [("sol61", "codex", "gpt-6.1-sol", "high"),
                  ("sonnet", "claude", "claude-sonnet-5-5", "high"),
                  ("glm", "glm", None, None),
                  ("swe2", "devin", "swe-2", None)],
        "build_review": [("glm", "glm", None, None),
                         ("sonnet", "claude", "claude-sonnet-5-5", "medium"),
                         ("sol61", "codex", "gpt-6.1-sol", "high"),
                         ("swe2", "devin", "swe-2", None)],
        "review_fix": [("sol61", "codex", "gpt-6.1-sol", "high"),
                       ("sonnet", "claude", "claude-sonnet-5-5", "high"),
                       ("glm", "glm", None, None),
                       ("swe2", "devin", "swe-2", None)],
        "re_review": [("glm", "glm", None, None),
                      ("sol61", "codex", "gpt-6.1-sol", "high"),
                      ("sonnet", "claude", "claude-sonnet-5-5", "medium"),
                      ("swe2", "devin", "swe-2", None)],
        "audit": [("fable", "claude", "claude-fable-5-1", "medium"),
                  ("astra6", "codex", "gpt-6-astra", "high"),
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
    check("daemon claude opus55 medium cmd",
          "--model" in c_cmd and "claude-opus-5-5" in c_cmd and "--effort" in c_cmd and "medium" in c_cmd,
          str(c_cmd))
    check("daemon claude config dir", c_env.get("CLAUDE_CONFIG_DIR") == "/home/box/.claude-fable", str(c_env.get("CLAUDE_CONFIG_DIR")))

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
        dm.get_stage_chain("audit", cfg_path)[3], "/tmp/p.md", "/tmp", "/tmp/o.out", readonly=True
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
          calls == ["opus55", "astra6", "glm"] and res["choice"]["model"] == "glm" and len(res["skipped"]) == 2,
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


def main():
    import tempfile
    print("offline tests for autobot.py")
    server = start_server()
    try:
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
    finally:
        server.shutdown()
    print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
    for failure in FAILURES:
        print("  -", failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
