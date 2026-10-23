Build a standalone non-LLM Python3 (stdlib only preferred; requests ok if installed) "viral autobot" in /workspace/viral/autobot/.
Entry: autobot.py with subcommands: run (crawl+discover+draft+metrics+status), status (status only). SQLite DB data/autobot.db. Logs to logs/.
Projects (config.json): hanset https://hanset.soulbound.co.kr ; goallit https://goal-lit.soulbound.co.kr ; kitchengenome https://kitchengenome.soulbound.co.kr ; pitchboard https://pitchboard.soulbound.co.kr ; agamajung url=null (no public site yet -> blocker "공개 URL 없음"). Give each project 4-6 English+Korean keywords (hanset: crossfit drop-in korea, korean sauna tattoo, hyrox seoul; goallit: webtoon review, web novel critique, korean literature; kitchengenome: korean chef, fine dining seoul, chef lineage; pitchboard: open call, call for submissions, grants deadline; agamajung: baby gear korea, stroller deal).
1. Crawl: robots.txt check (urllib.robotparser), sitemap.xml (+ sitemap index, + robots Sitemap: lines) or RSS/feed fallback; cap 500 URLs/site; store url, lastmod, content hash (fetch page only if lastmod changed or new, cap 30 page fetches/site/run, extract <title>, meta description). Mark new/changed per run. Sleep 2-5s random between requests. UA "SoulboundViralAutobot/1.0 (+https://hanset.soulbound.co.kr)".
2. Discover: Reddit public JSON https://www.reddit.com/search.json?q=...&sort=new&t=week&limit=25 (respect robots, sleep >=6s, handle 403/429 gracefully as blocker, no auth); also Google News RSS? NO — use only reddit + optional per-project rss feeds list in config. Store candidates: thread url, title, subreddit, created, score, num_comments, keyword. NO usernames/personal data stored.
3. Drafts: for each new/changed page, template drafts (no LLM) for channels x, threads, instagram, reddit (reddit draft = no link, informational), with UTM ?utm_source=<channel>&utm_medium=social&utm_campaign=viral_w1 (merge with existing query correctly). Write markdown packets to out/drafts/<project>/<date>_<slug>.md, header "DRAFT - VIRAL compliance check required before posting". First run: limit to 5 newest pages per project to avoid flood.
4. Metrics: parse /workspace/viral/w1/rounds.log (free text; extract URLs and timestamps; map to project by domain/keywords) -> published entries; queue 48h and 7d checks in DB; when due, fetch public signals without login (reddit: url + '.json' score/num_comments; other: HTTP status only). Never fabricate; unknown = "미확인".
5. Status: write /workspace/viral/w1/status.md (per-project table columns: project | new content | candidates found | drafts ready | published (rounds.log) | metrics | blockers, plus generated timestamp KST) and out/status_slack.txt (Slack mrkdwn). 
6. Slack: if ~/.secrets/viral_autobot.env has SLACK_WEBHOOK_URL, POST status_slack text (only in `status` subcommand, or `run --post`); else log "webhook missing, skipped". Never print/log secrets.
Hard rules: no posting/commenting/DM/email/follow/login anywhere; no paid APIs; no personal data. Write README.md (usage, cron, rules). Also cron.txt with:
0 */3 * * * cd /workspace/viral/autobot && /usr/bin/python3 autobot.py run >> logs/run.log 2>&1
50 20 * * * cd /workspace/viral/autobot && /usr/bin/python3 autobot.py status >> logs/status.log 2>&1
(box TZ is Asia/Seoul; include CRON_TZ=Asia/Seoul line). Do NOT install crontab and do NOT run the full crawl; just ensure `python3 -m py_compile` passes and `python3 autobot.py --help` works.

## 7-Stage Dev & Blueprint-Audit Daemon (Updated 2026-10-23)

Entry: `daemon.py` (or `python3 autobot.py daemon {start,stop,restart,status,stages,loop}`), configured via `config.json` -> `daemon`.

| Stage | Primary | 1st Backup | 2nd Backup | 3rd Backup | Mode |
| --- | --- | --- | --- | --- | --- |
| 1. 설계 (`design`) | Opus 5.5 medium (`claude-opus-5-5`) | Codex astra 6 high (`gpt-6-astra`) | GLM | Swe2 (`swe-2`) | read-only |
| 2. 설계리뷰 (`design_review`) | Codex astra 6 high (`gpt-6-astra`) | Opus 5.5 medium (`claude-opus-5-5`) | GLM | Swe2 (`swe-2`) | read-only |
| 3. 개발 (`build`) | Codex sol 6.1 high (`gpt-6.1-sol`) | Claude Sonnet high (`claude-sonnet-5-5`) | GLM | Swe2 (`swe-2`) | write |
| 4. 개발리뷰 (`build_review`) | GLM | Claude Sonnet 5.5 medium (`claude-sonnet-5-5`) | Codex sol 6.1 high (`gpt-6.1-sol`) | Swe2 (`swe-2`) | read-only |
| 5. 리뷰에 따른 수정 (`review_fix`) | Codex sol 6.1 high (`gpt-6.1-sol`) | Claude Sonnet high (`claude-sonnet-5-5`) | GLM | Swe2 (`swe-2`) | write |
| 6. 재리뷰 (`re_review`) | GLM | Codex sol 6.1 high (`gpt-6.1-sol`) | Claude Sonnet medium (`claude-sonnet-5-5`) | Swe2 (`swe-2`) | read-only |
| 7. 10개 PR마다 감리 (`audit`) | Fable medium (`claude-fable-5-1`) | Codex astra 6 high (`gpt-6-astra`) | GLM | Swe2 (`swe-2`) | read-only |
