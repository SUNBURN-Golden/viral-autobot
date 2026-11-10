"""Validate public aggregate snapshots and preserve unknowns independently of LLM notes."""
import datetime as dt
import json
import os
import re
from urllib.parse import urlsplit
import autobot as ab
from . import guards, runner, x_public
from .config import resolve_path
from .package import canonical, digest
from .guards import OpsBlocked, OpsLLMError

KEYS = ('likes', 'reposts', 'replies', 'views')


def channel_for(url):
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return None
    if guards.redact(url) != url:
        return None
    if p.scheme != 'https' or p.username or p.password or port not in (None, 443): return None
    if x_public.public_id(url): return 'x'
    if p.hostname in ('instagram.com', 'www.instagram.com') and re.fullmatch(r'/(?:p|reel)/[\w-]+/?', p.path): return 'instagram'
    if p.hostname in ('threads.net', 'www.threads.net', 'threads.com', 'www.threads.com') and re.fullmatch(r'/@[^/]+/post/[\w-]+/?', p.path): return 'threads'
    return None


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None: raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError): raise OpsBlocked('metric_observed_at_invalid') from None


def snapshot(post, kind, cfg, now, *, dry_run=False, mock=None):
    if kind not in ('48h', '7d'):
        raise OpsBlocked('metric_check_kind_invalid')
    pid = post['post_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', pid or ''): raise OpsBlocked('metric_post_id_invalid')
    url = post['url']; channel = channel_for(url or '')
    if not channel or post['channel'] not in (channel, 'web') or post['state'] not in ('published_verified', 'measured', 'evaluated') or post['hold_reason']:
        raise OpsBlocked('metric_post_not_verified')
    if post['source'] == 'rounds.log' and (not re.search(r'posted|published|live|게시', post['raw_line'] or '', re.I) or re.search(r'held|published none', post['raw_line'] or '', re.I)):
        raise OpsBlocked('metric_post_not_verified')
    published = timestamp(post['verified_at'])
    due = published + (dt.timedelta(hours=48) if kind == '48h' else dt.timedelta(days=7))
    if due > now: raise OpsBlocked('metric_not_due')
    result = dict(project=post['project'], post_id=pid, check_kind=kind, public_url=url, channel=channel,
                  source='unverified', observed_at=None, metrics={k: 'unverified' for k in KEYS})
    path = os.path.join(resolve_path(cfg['ops']['paths']['manual_metrics']), f'{pid}_{kind}.json')
    if channel == 'x':
        metrics = x_public.collect(url, cfg, dry_run=dry_run, mock=mock)
        if any(type(v) is int for v in metrics.values()): result.update(metrics=metrics, observed_at=ab.iso(now), source='x_public_api')
    elif os.path.lexists(path):
        if not os.path.isfile(path):
            raise OpsBlocked('manual_metrics_invalid')
        try:
            with open(path, encoding='utf-8') as stream: raw = json.load(stream)
        except (OSError, ValueError): raise OpsBlocked('manual_metrics_invalid') from None
        fields = {'post_id', 'check_kind', 'channel', 'public_url', 'observed_at', 'metrics'}
        if not isinstance(raw, dict) or not fields <= set(raw) or set(raw) - fields - {'project'}:
            raise OpsBlocked('manual_metrics_invalid')
        if any(raw.get(k) != result[k] for k in ('post_id', 'check_kind', 'channel', 'public_url')) or raw.get('project', post['project']) != post['project']:
            raise OpsBlocked('manual_metrics_identity')
        observed = timestamp(raw['observed_at'])
        if not published <= due <= observed <= now: raise OpsBlocked('metric_observed_at_invalid')
        metrics = raw['metrics']
        if not isinstance(metrics, dict) or set(metrics) - set(KEYS) or any(v != 'unverified' and (type(v) is not int or v < 0) for v in metrics.values()):
            raise OpsBlocked('manual_metrics_invalid')
        result.update(source='manual', observed_at=raw['observed_at'], metrics={k: metrics.get(k, 'unverified') for k in KEYS})
    return result


def save_snapshot(conn, result, now):
    hashed = digest(result)
    conn.execute('INSERT OR IGNORE INTO ops_metric_snapshots(project,post_id,check_kind,public_url,channel,source,observed_at,collected_at,payload,payload_hash) VALUES (?,?,?,?,?,?,?,?,?,?)',
                 tuple(result[k] for k in ('project', 'post_id', 'check_kind', 'public_url', 'channel', 'source', 'observed_at')) + (ab.iso(now), canonical(result), hashed))
    sid = conn.execute('SELECT id FROM ops_metric_snapshots WHERE project=? AND post_id=? AND check_kind=? AND payload_hash=?',
                       (result['project'], result['post_id'], result['check_kind'], hashed)).fetchone()[0]
    for metric, value in result['metrics'].items():
        if type(value) is int:
            ab.record_metric(conn, 1, metric, value, now, entity='post', post_id=result['post_id'], brand=result['project'],
                             source=result['source'], observed_at=result['observed_at'], window=result['check_kind'])
    conn.commit()
    return sid


def validate_note(response, actual):
    required = ('hypothesis', 'actual_with_denominator', 'next_single_change')
    if any(not isinstance(response.get(k), str) or not response[k].strip() for k in required): raise OpsLLMError('llm_output_invalid')
    causes = response.get('alternative_causes')
    if not isinstance(causes, list) or not causes or any(not isinstance(v, str) or not v.strip() for v in causes): raise OpsLLMError('llm_output_invalid')
    if response['actual_with_denominator'] != actual: raise OpsBlocked('measure_fabricated_number')
    # Numeric observations can be cited only in the code-owned actual block.
    # Prohibiting digits elsewhere also prevents metric/value and window reassignment.
    for key, value in response.items():
        if key in ('actual_with_denominator', 'category', 'verdict'): continue
        if re.search(r'\d', canonical(value)) or re.search(r'\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand)\b|[영일이삼사오육칠팔구십]+\s*(?:회|명|개|건|퍼센트)', canonical(value), re.I):
            raise OpsBlocked('measure_fabricated_number')
    guards.validate_single_change(response['next_single_change'])


def run(conn, cfg, proj, now, *, check_kind, post_id=None, dry_run=False, mock=None, **kwargs):
    from .pipeline import project_checks
    project_checks(cfg, proj)
    if not dry_run:
        ab.process_rounds_log(conn, cfg, now)
    timed_round_lines = set()
    try:
        with open(ab.abs_path(cfg, 'rounds_log'), encoding='utf-8') as stream:
            round_time = None
            for line_no, line in enumerate(stream, 1):
                header = ab.ROUND_HEADER_RE.search(line)
                if header:
                    try: round_time = dt.datetime(*map(int, header.groups()), tzinfo=now.tzinfo)
                    except ValueError: round_time = None
                if round_time is not None or ab.FULL_DT_RE.search(line): timed_round_lines.add(line_no)
    except OSError:
        pass
    rows = conn.execute('SELECT * FROM published WHERE project=?' + (' AND post_id=?' if post_id else '') + ' ORDER BY id',
                        (proj.name, post_id) if post_id else (proj.name,)).fetchall()
    if post_id and not rows: raise OpsBlocked('metric_post_not_verified')
    notes = []; snapshots = []
    for post in rows:
        if post['source'] == 'rounds.log' and post['line_no'] not in timed_round_lines:
            if post_id: raise OpsBlocked('metric_post_not_verified')
            continue
        try: result = snapshot(post, check_kind, cfg, now, dry_run=dry_run, mock=mock)
        except OpsBlocked as exc:
            if post_id or str(exc) not in ('metric_not_due', 'metric_post_not_verified'): raise
            continue
        sid = None if dry_run else save_snapshot(conn, result, now)
        snapshots.append(dict(result, snapshot_id=sid))
        if dry_run:
            diagnosis = dict(category='low_impressions', verdict='판정 보류', hypothesis='공개 지표 확인 필요', alternative_causes=['관측 부족'])
        else:
            diagnosis = ab.diagnose_post(conn, cfg, post['post_id'], check_kind, now)
        if result['metrics']['views'] == 'unverified': diagnosis['verdict'] = '판정 보류'
        # Keep canonical metric names, values and identity together, including unknowns.
        actual = canonical({k: result[k] for k in ('post_id', 'check_kind', 'observed_at', 'metrics')})
        payload = dict(diagnosis, actual_with_denominator=actual, raw_metrics=result, snapshot_id=sid)
        response = runner.generate('measure', payload, cfg, dry_run=dry_run, mock=mock)
        validate_note(response, actual)
        response.update(category=diagnosis['category'], verdict=diagnosis['verdict'], post_id=post['post_id'], check_kind=check_kind, snapshot_id=sid)
        if not dry_run:
            conn.execute('INSERT INTO ops_measure_notes(project,post_id,check_kind,category,verdict,payload,created,updated,snapshot_id) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(post_id,check_kind) DO UPDATE SET category=excluded.category,verdict=excluded.verdict,payload=excluded.payload,updated=excluded.updated,snapshot_id=excluded.snapshot_id',
                         (proj.name, post['post_id'], check_kind, response['category'], response['verdict'], canonical(response), ab.iso(now), ab.iso(now), sid))
            conn.commit()
        notes.append(response)
    return dict(status='ok' if notes else 'skipped', notes=notes, snapshots=snapshots)
