"""Evidence-ranked next-week recommendations. Never changes strategies or queues."""
import datetime as dt
import glob
import json
import os
import re
import tempfile
import autobot as ab
from . import context, guards, runner
from .config import resolve_path
from .guards import OpsBlocked

FORMATS = {'체크리스트', '카드', '가이드', '비교', '질문', 'checklist', 'cards', 'guide',
           '카드뉴스', '릴스', '스레드', '포스트', '인포그래픽', 'reel', 'thread', 'post', 'infographic'}


def weekly_plan(cfg, now):
    candidates = []
    for path in glob.glob(resolve_path(cfg['ops']['paths']['weekly_plan_glob'])):
        match = re.search(r'weekly-plan-(\d{4})(?:\.md)$', path)
        if not match: continue
        text = context.bounded_file(path, cfg['ops']['limits']['max_context_chars'])
        # Authored/updated timestamps are not the week start. Read the title or
        # an explicit start label, using other dates only to establish the year.
        title = next((line for line in text.splitlines() if line.strip()), '')
        labels = '\n'.join(line for line in text.splitlines() if re.search(r'week_start|주간 시작|시작일', line, re.I))
        start_text = title + '\n' + labels
        explicit = set(re.findall(r'\b(20\d{2})-(\d{2})-(\d{2})\b', start_text))
        short = re.search(r'(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)', title)
        if short and f'{int(short[1]):02d}{int(short[2]):02d}' != match[1]:
            raise OpsBlocked('weekly_plan_date_conflict')
        years = set(re.findall(r'\b(20\d{2})-\d{2}-\d{2}\b', text[:1500]))
        if len(years) > 1: raise OpsBlocked('weekly_plan_date_conflict')
        explicit_dates = []
        for y, m, d in explicit:
            try: explicit_dates.append(dt.date(int(y), int(m), int(d)))
            except ValueError: raise OpsBlocked('weekly_plan_date_invalid') from None
        year = explicit_dates[0].year if explicit_dates else int(next(iter(years), str(now.year)))
        try: date = dt.date(year, int(match[1][:2]), int(match[1][2:]))
        except ValueError: raise OpsBlocked('weekly_plan_date_invalid') from None
        # A single explicit date must be the filename's date. A date range
        # (e.g. "2026-11-08 ~ 2026-11-14") is valid when the filename date is
        # in it and the span is one week.
        if len(explicit_dates) == 1:
            if explicit_dates[0] != date:
                raise OpsBlocked('weekly_plan_date_conflict')
        elif explicit_dates:
            if max(explicit_dates) - min(explicit_dates) > dt.timedelta(days=6) or date not in explicit_dates:
                raise OpsBlocked('weekly_plan_date_conflict')
        if date <= now.date(): candidates.append((date, path, text))
    if not candidates: return ''
    candidates.sort()
    return candidates[-1][2]


def build_context(conn, cfg, proj, now):
    days = cfg['ops']['strategy']['lookback_days']
    since = ab.iso(now - dt.timedelta(days=days))
    cap = cfg['ops']['limits']['max_context_chars']
    evidence = {}
    rows = conn.execute('SELECT * FROM ops_metric_snapshots WHERE project=? AND collected_at>=? ORDER BY id DESC', (proj.name, since)).fetchall()
    seen = set()
    for row in rows:
        if len(evidence) >= cfg['ops']['limits']['max_evidence_per_prompt']:
            break
        identity = (row['post_id'], row['check_kind'])
        if identity in seen: continue
        seen.add(identity)
        payload = json.loads(row['payload'])
        diagnosis = conn.execute('SELECT verdict,category FROM ops_measure_notes WHERE project=? AND post_id=? AND check_kind=? AND snapshot_id=?',
                                 (proj.name, row['post_id'], row['check_kind'], row['id'])).fetchone()
        known = any(type(v) is int for v in payload['metrics'].values())
        verdict = diagnosis['verdict'] if diagnosis and known else '판정 보류'
        evidence[f'snapshot:{row["id"]}'] = dict(payload=payload, verdict=verdict,
                                                category=diagnosis['category'] if diagnosis else None)
    data = dict(project=proj.name, evidence=evidence, strategies=context.bounded_file(resolve_path(cfg['ops']['paths']['strategies']), cap//6),
                rounds=context.bounded_file(ab.abs_path(cfg, 'rounds_log'), cap//6),
                status=context.bounded_file(ab.abs_path(cfg, 'status_md'), cap//6), weekly_plan=weekly_plan(cfg, now),
                kg_hold=cfg['ops']['brands'].get('kitchengenome', {}).get('hold') is True)
    if data['kg_hold']:
        # Held-brand material is never offered as a candidate.
        for key in ('strategies', 'rounds', 'status', 'weekly_plan'):
            data[key] = '\n'.join(line for line in data[key].splitlines() if not re.search(r'kitchen\s*genome|kitchengenome|키친\s*게놈|\bKG\b', line, re.I))
    data['notes'] = [dict(r) for r in conn.execute('SELECT post_id,check_kind,category,verdict FROM ops_measure_notes WHERE project=? AND updated>=? ORDER BY id DESC LIMIT 10', (proj.name, since))]
    return data


def mock_plan(payload):
    return {'recommendations': [dict(topic='HANSET', format='체크리스트', action='iterate' if value['verdict'] == 'diagnosed' else 'keep', evidence_refs=[key],
                                    reason='검증된 진단에 따라 개선' if value['verdict'] == 'diagnosed' else '검증된 진단에 따라 유지',
                                    template_hint='훅: 질문형; 카드: 기존 구성; 채널: x, threads, instagram') for key, value in payload['evidence'].items()] or
                              [dict(topic='HANSET', format='체크리스트', action='defer', evidence_refs=[], reason='관측 부족으로 판정 보류', template_hint='기존 형식 유지')]}


def run(conn, cfg, proj, now, *, dry_run=False, mock=None, run_id=None, **kwargs):
    from .pipeline import project_checks, begin_run
    project_checks(cfg, proj)
    payload = build_context(conn, cfg, proj, now)
    result = runner.generate('plan_next', payload, cfg, dry_run=dry_run, mock=mock)
    items = result.get('recommendations')
    if not isinstance(items, list) or not items or len(items) > 30: raise OpsBlocked('strategy_schema')
    accepted = []
    corpus = '\n'.join(payload[k] for k in ('strategies', 'weekly_plan', 'rounds', 'status')) + '\nHANSET'
    for raw in items:
        fields = {'topic', 'format', 'action', 'evidence_refs', 'reason', 'template_hint'}
        if not isinstance(raw, dict) or set(raw) != fields or any(not isinstance(raw[k], str) or not raw[k].strip() for k in fields - {'evidence_refs'}) or raw['action'] not in ('keep', 'kill', 'iterate', 'defer'):
            raise OpsBlocked('strategy_schema')
        item = dict(raw)
        if payload['kg_hold'] and re.search(r'kitchen\s*genome|키친\s*게놈|\bKG\b', json.dumps(item), re.I): continue
        refs = item['evidence_refs']
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in payload['evidence'] for ref in refs): raise OpsBlocked('strategy_evidence')
        # Performance values are rendered by code from referenced snapshots only.
        # Free-form factual rewrites cannot relabel a value or invent a result.
        if any(re.search(r'\d', item[k]) for k in fields - {'evidence_refs'}): raise OpsBlocked('strategy_fabricated_number')
        # Accept the canonical formats plus live phrasings that appear literally
        # in the source plans. Prose remains digit-free (checked above), so this
        # relaxes wording without relaxing any factual claim.
        if item['topic'] not in corpus or (item['format'] not in FORMATS and item['format'] not in corpus):
            raise OpsBlocked('strategy_unsupported_claim')
        sources = [payload['evidence'][ref] for ref in refs]
        held = not sources or all(s['verdict'] == '판정 보류' for s in sources) or any(s['verdict'] == '판정 보류' for s in sources)
        if held:
            item.update(action='defer', reason='관측 부족으로 판정 보류')
        if item['action'] == 'keep' and any(s['verdict'] != 'pass' for s in sources): raise OpsBlocked('strategy_verdict_conflict')
        if item['action'] == 'kill' and (not sources or any(s['verdict'] != 'diagnosed' for s in sources)):
            raise OpsBlocked('strategy_verdict_conflict')
        if item['action'] == 'keep' and not all(s in item['template_hint'] for s in ('훅:', '카드:', '채널:')): raise OpsBlocked('strategy_template_missing')
        if item['action'] == 'kill' and not item['template_hint'].startswith('retire:'): raise OpsBlocked('strategy_retire_missing')
        accepted.append(item)
    order = {'keep': 0, 'iterate': 1, 'kill': 2, 'defer': 3}
    def priority(item):
        weekly_position = payload['weekly_plan'].find(item['topic'])
        return (order[item['action']], weekly_position if weekly_position >= 0 else len(payload['weekly_plan']), item['topic'], item['format'])
    accepted.sort(key=priority)
    lines = ['# 다음 주 전략', '']
    for rank, item in enumerate(accepted, 1):
        action = '판정 보류' if item['action'] == 'defer' else item['action']
        lines += [f'{rank}. {item["topic"]} / {item["format"]} — {action}', f'   {item["reason"]}', f'   {item["template_hint"]}']
        for ref in item['evidence_refs']:
            source = payload['evidence'][ref]['payload']
            lines.append('   근거 ' + ref + ': ' + json.dumps(source, ensure_ascii=False, sort_keys=True))
        lines.append('')
    if dry_run:
        if run_id is None: run_id = begin_run(conn, 'plan', proj.name, cfg, mode='dry-run', now=now)
        path = os.path.join(resolve_path(cfg['ops']['paths']['out']), 'previews', str(run_id), 'strategy-next.md')
    else: path = resolve_path(cfg['ops']['paths']['strategy_next'])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=os.path.dirname(path), delete=False, suffix='.partial') as stream:
            temporary = stream.name; stream.write('\n'.join(lines))
        os.replace(temporary, path); temporary = None
    finally:
        if temporary: os.unlink(temporary)
    return dict(status='ok', path=path, recommendations=accepted)
