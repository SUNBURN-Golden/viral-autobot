"""Bounded, scrubbed inputs. Source material is data, never instructions."""
import json
import os

import autobot as ab
from .guards import redact, redact_obj


def clean(value):
    if isinstance(value, dict):
        return redact_obj({clean(str(k)): clean(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return redact('\n'.join(ab.scrub_personal(line) for line in value.splitlines()))
    return value


def bounded_file(path, limit):
    try:
        with open(os.path.expanduser(path), encoding='utf-8', errors='replace') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit * 4))
            text = stream.read()
        return clean('\n'.join(text.splitlines()[-80:]))[-limit:]
    except (OSError, UnicodeError):
        return ''


def plan_context(conn, cfg, proj, now):
    cap = cfg['ops']['limits']['max_context_chars']
    return clean({
        'date': ab.iso(now), 'project': proj.name,
        'rounds': bounded_file(ab.abs_path(cfg, 'rounds_log'), cap // 4),
        'status': bounded_file(ab.abs_path(cfg, 'status_md'), cap // 4),
        'counts': {t: conn.execute(f'SELECT COUNT(*) FROM {t} WHERE project=?', (proj.name,)).fetchone()[0]
                   for t in ('questions', 'evidence', 'drafts')},
        'questions': [dict(r) for r in conn.execute('SELECT question,state,priority FROM questions WHERE project=? ORDER BY priority DESC LIMIT 5', (proj.name,))],
        'evidence': [dict(r) for r in conn.execute('SELECT fact,valid_until,status FROM evidence WHERE project=? ORDER BY valid_until LIMIT 5', (proj.name,))],
        'recent_runs': [dict(r) for r in conn.execute('SELECT command,status FROM ops_runs WHERE project=? ORDER BY id DESC LIMIT 5', (proj.name,))],
    })


def evidence_context(evidences, cfg):
    fields = ('id', 'fact', 'allowed_claims', 'forbidden_claims', 'source_url', 'verified_at', 'valid_until', 'keywords')
    return clean([{k: dict(e).get(k) for k in fields}
                  for e in evidences[:cfg['ops']['limits']['max_evidence_per_prompt']]])
