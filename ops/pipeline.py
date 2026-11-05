"""Local operations orchestration. LLMs propose text; code controls every state change."""
import json
import os
import re

import autobot as ab
from . import context, guards, runner
from .config import resolve_path
from .guards import OpsBlocked, OpsLLMError


def dumps(value):
    return json.dumps(guards.redact_obj(value), ensure_ascii=False, sort_keys=True)


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(guards.redact(text))
    return path


def out_path(cfg, *parts):
    return os.path.join(resolve_path(cfg['ops']['paths']['out']), *parts)


def begin_run(conn, command, project, cfg, *, mode, prompt_path=None, now=None):
    cur = conn.execute('INSERT INTO ops_runs(command,project,mode,model,prompt_path,status,created) VALUES (?,?,?,?,?,?,?)',
                       (command, project, mode, guards.redact(cfg['ops']['model']), guards.redact(prompt_path) if prompt_path else None,
                        'running', ab.iso(now or ab.now_kst())))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status, *, output=None, error=None, now=None):
    conn.execute('UPDATE ops_runs SET status=?,output=?,error=?,finished=? WHERE id=?',
                 (status, dumps(output), guards.redact(error) if error else None, ab.iso(now or ab.now_kst()), run_id))
    conn.commit()


def write_dryrun_log(cfg, record):
    path = resolve_path(cfg['ops']['paths']['dryrun_log'])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write('\n## ' + guards.redact(record['command']) + ' (' + record['mode'] + ')\n\n```json\n' +
                     dumps(record)[:2000] + '\n```\n')


def skipped(reason):
    return {'status': 'skipped', 'reason': reason}


def project_checks(cfg, proj):
    guards.check_brand(cfg, proj.name)
    guards.check_cost_config(cfg)


def active_evidence(conn, project, ids, now):
    if not ids:
        raise OpsBlocked('no_verified_evidence')
    result = []
    for eid in ids:
        row = conn.execute('SELECT * FROM evidence WHERE id=? AND project=?', (eid, project)).fetchone()
        expiry = ab._evidence_date(row['valid_until']) if row else None
        verified = ab._evidence_date(row['verified_at']) if row else None
        if not row or row['status'] != 'active' or not expiry or expiry < ab.kst(now) or not verified or verified > ab.kst(now):
            raise OpsBlocked('evidence_expired')
        result.append(row)
    return result


def ensure_anchor_draft(conn, proj, question, evidences, cfg, now, landing_url):
    existing = conn.execute('SELECT id FROM drafts WHERE project=? AND question_id=? ORDER BY id LIMIT 1',
                            (proj.name, question['id'])).fetchone()
    hook = ab._select_hook(ab.generate_hooks(question, evidences, proj, cfg), cfg)
    if not hook:
        raise OpsBlocked('no_safe_hook')
    page_url = 'question:' + question['question_key']
    asset = ab.make_asset_id('draft', proj.name, page_url)
    ids = json.dumps([e['id'] for e in evidences])
    channels = ','.join(cfg['ops']['channels'])
    campaign = cfg['ops']['utm']['campaign']
    if existing:
        draft_id = existing['id']
        conn.execute('UPDATE drafts SET channels=?,evidence_ids=?,hook_type=?,asset_id=?,keyword_cluster=?,campaign_id=? WHERE id=?',
                     (channels, ids, hook, asset, question['keyword_cluster'], campaign, draft_id))
    else:
        path = out_path(cfg, 'anchors', proj.name, f"{ab.kst(now):%Y%m%d}_{ab.slugify(question['question_key'])}.md")
        cur = conn.execute('INSERT INTO drafts(project,page_url,path,channels,created,state,question_id,landing_url,hook_type,'
                           'evidence_ids,asset_id,keyword_cluster,campaign_id,why_this_channel,why_now) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (proj.name, page_url, path, channels, ab.iso(now), 'candidate', question['id'], landing_url, hook,
                            ids, asset, question['keyword_cluster'], campaign, ab._why_this_channel(question), question['why_now']))
        draft_id = cur.lastrowid
        write_file(path, '# DRAFT — human approval required\n\n' + context.clean(question['question']) + '\n')
    conn.execute("DELETE FROM evidence_links WHERE entity='draft' AND entity_id=?", (draft_id,))
    for e in evidences:
        ab.link_evidence(conn, e['id'], 'draft', draft_id)
    return draft_id


def latest_compliance(conn, ops_draft_id, copy_hash):
    return conn.execute('SELECT * FROM ops_compliance WHERE ops_draft_id=? AND copy_hash=? ORDER BY id DESC LIMIT 1',
                        (ops_draft_id, copy_hash)).fetchone()


def require_compliance_pass(conn, ops_draft):
    actual = guards.content_hash(ops_draft['copy'], ops_draft['card_copy'])
    if actual != ops_draft['copy_hash']:
        raise OpsBlocked('stale_copy_hash')
    row = latest_compliance(conn, ops_draft['id'], actual)
    if not row or row['verdict'] != 'PASS':
        raise OpsBlocked('compliance_pass_required')
    return row


def require_approved_anchor(conn, project, draft_id):
    row = conn.execute('SELECT * FROM drafts WHERE id=? AND project=?', (draft_id, project)).fetchone()
    if not row or row['state'] != 'approved':
        raise OpsBlocked('draft_not_approved')
    return row


def select_drafts(conn, project, ids=None):
    if ids:
        rows = [conn.execute('SELECT * FROM ops_drafts WHERE id=? AND project=?', (i, project)).fetchone() for i in ids]
        if any(r is None for r in rows):
            raise OpsBlocked('ops_draft_not_found')
        return rows
    row = conn.execute("SELECT * FROM ops_drafts WHERE project=? AND status!='archived' ORDER BY id DESC LIMIT 1", (project,)).fetchone()
    return [row] if row else []


def record_block(conn, draft, reason, dry_run):
    if dry_run:
        return
    reasons = json.loads(draft['blocked_reasons'])
    if reason not in reasons:
        reasons.append(reason)
    conn.execute('UPDATE ops_drafts SET blocked_reasons=? WHERE id=?', (dumps(reasons), draft['id']))
    conn.commit()


def draft_code_reasons(conn, cfg, proj, draft, now):
    reasons = []
    try:
        guards.check_cost_config(cfg)
    except OpsBlocked as exc:
        reasons.append(str(exc))
    if guards.content_hash(draft['copy'], draft['card_copy']) != draft['copy_hash']:
        reasons.append('stale_copy_hash')
    try:
        evidence = active_evidence(conn, proj.name, json.loads(draft['evidence_ids']), now)
    except OpsBlocked as exc:
        evidence = []
        reasons.append(str(exc))
    reasons.extend(guards.validate_copy(draft['channel'], draft['copy'], draft['card_copy'], proj, evidence, cfg))
    reasons.extend(delivery_length_reasons(draft['channel'], draft['copy'], draft['utm_url'], cfg))
    if not ab.is_deep_landing(draft['landing_url'], proj, cfg['_projects'])[0]:
        reasons.append('landing_missing')
    # Privacy checks run on raw stored content; cleaning never grants permission to publish a secret.
    if context.clean(draft['copy']) != draft['copy'].strip() and re.search(r'@|password|otp|sk-|rk-', draft['copy'], re.I):
        reasons.append('personal_or_secret_data')
    return sorted(set(reasons)), evidence


def delivery_copy(copy, url):
    """Keep approved body text immutable and supply the tracked destination separately."""
    return copy if url in copy else copy + '\n' + url


def delivery_length_reasons(channel, copy, url, cfg):
    text = delivery_copy(copy, url)
    if channel == 'x' and guards.x_weighted_length(text, cfg) > min(280, cfg['ops']['limits']['x_weighted']):
        return ['x_length_exceeded']
    if channel == 'threads' and len(text) > min(500, cfg['ops']['limits']['threads_chars']):
        return ['threads_length_exceeded']
    return []


def utm_content(cfg, *, draft=None, hook=None):
    """Resolve utm_content from the configured source without inventing a value."""
    field = cfg['ops']['utm'].get('content', 'hook_type')
    if draft is not None and field in draft.keys() and draft[field]:
        return draft[field]
    if field == 'hook_type' and hook:
        return hook
    return f"ops:{draft['id']}" if draft is not None else hook


def utm_medium(cfg):
    return cfg['ops']['utm'].get('medium') or None


def cmd_plan(conn, cfg, proj, now, *, dry_run=False, mock=None, **kwargs):
    project_checks(cfg, proj)
    result = runner.generate('plan', context.plan_context(conn, cfg, proj, now), cfg, dry_run=dry_run, mock=mock)
    plan = result.get('plan')
    if not isinstance(plan, str) or not plan.strip():
        raise OpsLLMError('llm_output_invalid')
    plan = context.clean(plan)[:8000]
    path = out_path(cfg, 'plan', f'{ab.kst(now):%Y%m%d}_{proj.name}_plan.md')
    write_file(path, plan + '\n')
    return {'status': 'ok', 'path': path, 'plan': plan}


def cmd_draft(conn, cfg, proj, now, *, dry_run=False, mock=None, draft_id=None, limit=None, **kwargs):
    project_checks(cfg, proj)
    cap = cfg['ops']['limits']['max_drafts_per_run']
    if limit is not None:
        cap = min(cap, max(0, limit))
    if draft_id is not None:
        anchor = conn.execute('SELECT question_id FROM drafts WHERE id=? AND project=?', (draft_id, proj.name)).fetchone()
        if not anchor:
            raise OpsBlocked('draft_not_found')
        questions = conn.execute("SELECT * FROM questions WHERE id=? AND project=? AND state IN ('ready','drafted')",
                                 (anchor['question_id'], proj.name)).fetchall()
    else:
        questions = conn.execute("SELECT * FROM questions WHERE project=? AND state IN ('ready','drafted') ORDER BY priority DESC,id LIMIT ?",
                                 (proj.name, cap)).fetchall()
    if not questions:
        return skipped('no_questions')
    results = []
    for q in questions:
        linked = ab.linked_evidence(conn, 'question', q['id'])
        try:
            evidence = active_evidence(conn, proj.name, [e['id'] for e in linked], now)
        except OpsBlocked:
            continue
        evidence = evidence[:cfg['ops']['limits']['max_evidence_per_prompt']]
        landing = q['target_landing_url']
        page = conn.execute('SELECT last_status FROM pages WHERE project=? AND url=?', (proj.name, landing)).fetchone()
        if not page or page['last_status'] in ('gone', 'expired') or not ab.is_deep_landing(landing, proj, cfg['_projects'])[0]:
            raise OpsBlocked('landing_missing')
        hook = ab._select_hook(ab.generate_hooks(q, evidence, proj, cfg), cfg)
        if not hook:
            raise OpsBlocked('no_safe_hook')
        anchor_id = None
        if not dry_run:
            anchor_id = ensure_anchor_draft(conn, proj, q, evidence, cfg, now, landing)
            anchor = conn.execute('SELECT * FROM drafts WHERE id=?', (anchor_id,)).fetchone()
            hook = anchor['hook_type'] or anchor['asset_id']
        campaign = cfg['ops']['utm']['campaign']
        payload = {'project': proj.name, 'question': context.clean(dict(q)), 'evidence': context.evidence_context(evidence, cfg),
                   'channels': cfg['ops']['channels'], 'landing_url': context.clean(landing)}
        response = runner.generate('draft', payload, cfg, dry_run=dry_run, mock=mock)
        items = response.get('drafts')
        if not isinstance(items, list) or not items or len(items) > 3 or any(not isinstance(i, dict) for i in items):
            raise OpsLLMError('llm_output_invalid')
        # Every configured channel must be present exactly once; a partial response
        # would otherwise be accepted as a complete multi-channel draft set.
        channels = [item.get('channel') for item in items]
        if len(channels) != len(set(channels)) or set(channels) != set(cfg['ops']['channels']):
            raise OpsLLMError('llm_output_invalid')
        seen = set()
        for item in items:
            ch = item.get('channel')
            copy = item.get('copy')
            card = item.get('card_copy')
            if ch not in cfg['ops']['channels'] or ch not in ('x', 'threads', 'instagram') or ch in seen or not isinstance(copy, str) or (card is not None and not isinstance(card, str)):
                raise OpsLLMError('llm_output_invalid')
            seen.add(ch)
            utm = ab.add_utm(landing, ch, campaign, content=utm_content(cfg, hook=hook),
                             brand=proj.name, medium=utm_medium(cfg))
            # An LLM cannot introduce another destination. Reserve length for the
            # tracked URL even when it supplies just the approved body text.
            allowed_urls = {landing, utm} | {e['source_url'] for e in evidence}
            reasons = ['unverified_url'] if any(u not in allowed_urls for u in guards.URL.findall(copy + (card or ''))) else []
            copy = copy.replace(landing, utm) if landing in copy and utm not in copy else copy
            reasons += guards.validate_copy(ch, copy, card, proj, evidence, cfg)
            reasons += delivery_length_reasons(ch, copy, utm, cfg)
            result = {'channel': ch, 'copy': copy, 'card_copy': card, 'utm_url': utm,
                      'copy_hash': guards.content_hash(copy, card), 'status': 'blocked' if reasons else 'draft',
                      'blocked_reasons': sorted(set(reasons)), 'draft_id': anchor_id}
            if not dry_run:
                cur = conn.execute('INSERT INTO ops_drafts(project,draft_id,question_id,channel,copy,card_copy,copy_hash,evidence_ids,'
                                   'landing_url,utm_url,hook_type,campaign_id,status,blocked_reasons,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (proj.name, anchor_id, q['id'], ch, copy, card, result['copy_hash'], dumps([e['id'] for e in evidence]),
                                    landing, utm, hook, campaign, result['status'], dumps(result['blocked_reasons']), ab.iso(now), ab.iso(now)))
                result['id'] = cur.lastrowid
                write_file(out_path(cfg, 'drafts', proj.name, f'{cur.lastrowid}_{ch}.md'), copy + '\n\n' + (card or ''))
            results.append(result)
        if not dry_run:
            ab._set_question_state(conn, q['id'], 'drafted')
    if not dry_run:
        conn.commit()
    if not results:
        return skipped('no_verified_evidence')
    return {'status': 'blocked' if any(r['status'] == 'blocked' for r in results) else 'ok', 'drafts': results}


def cmd_compliance(conn, cfg, proj, now, *, dry_run=False, mock=None, ops_draft_ids=None, **kwargs):
    guards.check_brand(cfg, proj.name)
    drafts = select_drafts(conn, proj.name, ops_draft_ids)
    if not drafts:
        return skipped('no_ops_drafts')
    results = []
    for draft in drafts:
        reasons, evidence = draft_code_reasons(conn, cfg, proj, draft, now)
        checks = guards.compliance_code_checks(reasons)
        verdict = 'FAIL'
        if not reasons:
            data = {'draft': context.clean(dict(draft)), 'evidence': context.evidence_context(evidence, cfg),
                    'checklist': guards.CHECKLIST}
            try:
                response = runner.generate('compliance', data, cfg, dry_run=dry_run, mock=mock)
                llm_checks = response.get('checks')
                llm_checks = llm_checks if isinstance(llm_checks, dict) else {}
                for key in guards.CHECKLIST:
                    check = llm_checks.get(key)
                    if check is None:
                        reasons.append('llm_check_missing:' + key)
                    elif not isinstance(check, dict) or type(check.get('pass')) is not bool:
                        reasons.append('llm_check_invalid:' + key)
                    elif check['pass'] is not True:
                        reasons.append('llm_check_failed:' + key)
                checks.update({k: llm_checks[k] for k in guards.CHECKLIST if k in llm_checks})
                llm_reasons = response.get('reasons')
                if not isinstance(llm_reasons, list) or any(not isinstance(r, str) for r in llm_reasons):
                    reasons.append('llm_reasons_invalid')
                else:
                    reasons.extend(llm_reasons)
                if response.get('verdict') != 'PASS':
                    reasons.append('llm_verdict_fail')
                verdict = 'FAIL' if reasons else 'PASS'
            except OpsLLMError as exc:
                reasons.append('llm_error:' + str(exc))
        result = {'ops_draft_id': draft['id'], 'verdict': verdict, 'reasons': sorted(set(reasons)), 'checks': checks}
        if not dry_run:
            conn.execute('INSERT INTO ops_compliance(ops_draft_id,copy_hash,verdict,reasons,checks,version,checked_at) VALUES (?,?,?,?,?,?,?)',
                         (draft['id'], guards.content_hash(draft['copy'], draft['card_copy']), verdict, dumps(result['reasons']), dumps(checks),
                          cfg['ops']['compliance']['version'], ab.iso(now)))
            conn.execute('UPDATE ops_drafts SET status=?,blocked_reasons=?,updated=? WHERE id=?',
                         ('compliant' if verdict == 'PASS' else 'blocked', dumps(result['reasons']), ab.iso(now), draft['id']))
        results.append(result)
    if not dry_run:
        conn.commit()
    failed = any(r['verdict'] == 'FAIL' for r in results)
    return {'status': 'blocked' if failed and cfg['ops']['compliance']['block_on_fail'] else 'ok',
            'verdict': 'FAIL' if failed else 'PASS', 'results': results}


def schedule_url(cfg, proj, draft):
    # Rebuild from deterministic metadata, allowing campaign defaults to change without changing identity.
    return ab.add_utm(draft['landing_url'], draft['channel'], cfg['ops']['utm']['campaign'],
                      content=utm_content(cfg, draft=draft), brand=proj.name, medium=utm_medium(cfg))


def cmd_schedule(conn, cfg, proj, now, *, dry_run=False, mock=None, ops_draft_ids=None, **kwargs):
    project_checks(cfg, proj)
    drafts = select_drafts(conn, proj.name, ops_draft_ids)
    if not drafts:
        return skipped('no_ops_drafts')
    results = []
    for draft in drafts:
        try:
            require_compliance_pass(conn, draft)
            anchor = require_approved_anchor(conn, proj.name, draft['draft_id'])
            alias, error = guards.check_account(cfg, proj.name, draft['channel'])
            if error:
                raise OpsBlocked(error)
            if draft['status'] not in ('compliant', 'scheduled'):
                raise OpsBlocked('draft_not_compliant')
            url = schedule_url(cfg, proj, draft)
            attr = ab.parse_utm_attribution(url)
            if not attr or not attr.get('utm_campaign') or not attr.get('utm_content'):
                raise OpsBlocked('utm_required')
            ids = json.loads(draft['evidence_ids'])
            # record_post also inherits anchor evidence: validate that full union before any mutation.
            all_ids = sorted(set(ids) | set(ab._json_list(anchor['evidence_ids'])))
            active_evidence(conn, proj.name, all_ids, now)
            reasons, _ = draft_code_reasons(conn, cfg, proj, draft, now)
            if reasons:
                raise OpsBlocked(reasons[0])
            pid = ab.make_post_id(proj.name, draft['channel'], '', f"ops:{draft['id']}")
            if not dry_run:
                existing_post = conn.execute('SELECT state FROM published WHERE post_id=?', (pid,)).fetchone()
                if existing_post and existing_post['state'] != 'scheduled':
                    raise OpsBlocked('post_already_progressed')
                try:
                    pid = ab.record_post(conn, proj.name, draft['channel'], target_url='', label=f"ops:{draft['id']}", now=now,
                                         state='scheduled', draft_id=draft['draft_id'], evidence_ids=all_ids,
                                         campaign_id=cfg['ops']['utm']['campaign'], hook_type=draft['hook_type'])
                except ValueError:
                    raise OpsBlocked('evidence_expired') from None
                conn.execute('UPDATE published SET target_url=?,campaign_id=? WHERE post_id=?', (url, cfg['ops']['utm']['campaign'], pid))
                conn.execute('INSERT INTO ops_schedule(ops_draft_id,post_id,copy_hash,utm_url,account_alias,status,created,updated) '
                             "VALUES (?,?,?,?,?,'scheduled',?,?) ON CONFLICT(ops_draft_id) DO UPDATE SET copy_hash=excluded.copy_hash,"
                             "utm_url=excluded.utm_url,account_alias=excluded.account_alias,status='scheduled',updated=excluded.updated",
                             (draft['id'], pid, draft['copy_hash'], url, alias, ab.iso(now), ab.iso(now)))
                conn.execute("UPDATE ops_drafts SET status='scheduled',blocked_reasons='[]',updated=? WHERE id=?", (ab.iso(now), draft['id']))
                conn.commit()
            results.append({'status': 'ok', 'ops_draft_id': draft['id'], 'post_id': pid, 'utm_url': url})
        except OpsBlocked as exc:
            record_block(conn, draft, str(exc), dry_run)
            results.append({'status': 'blocked', 'ops_draft_id': draft['id'], 'reason': str(exc)})
    return {'status': 'blocked' if any(r['status'] == 'blocked' for r in results) else 'ok', 'results': results}


def cmd_publish_guide(conn, cfg, proj, now, *, dry_run=False, mock=None, ops_schedule_ids=None, **kwargs):
    project_checks(cfg, proj)
    if ops_schedule_ids:
        schedules = [conn.execute('SELECT s.* FROM ops_schedule s JOIN ops_drafts d ON d.id=s.ops_draft_id WHERE s.id=? AND d.project=?',
                                  (sid, proj.name)).fetchone() for sid in ops_schedule_ids]
        if any(s is None for s in schedules):
            raise OpsBlocked('schedule_not_found')
    else:
        schedules = conn.execute("SELECT s.* FROM ops_schedule s JOIN ops_drafts d ON d.id=s.ops_draft_id WHERE d.project=? AND s.status='scheduled' ORDER BY s.id", (proj.name,)).fetchall()
    if not schedules:
        return skipped('not_scheduled')
    results = []
    for schedule in schedules:
        draft = conn.execute('SELECT * FROM ops_drafts WHERE id=?', (schedule['ops_draft_id'],)).fetchone()
        try:
            if schedule['status'] != 'scheduled':
                raise OpsBlocked('not_scheduled')
            compliance = require_compliance_pass(conn, draft)
            require_approved_anchor(conn, proj.name, draft['draft_id'])
            if schedule['copy_hash'] != draft['copy_hash']:
                raise OpsBlocked('stale_schedule_hash')
            if any('utm_' in url and url != schedule['utm_url'] for url in guards.URL.findall(draft['copy'])):
                raise OpsBlocked('stale_copy_utm')
            alias, error = guards.check_account(cfg, proj.name, draft['channel'])
            if error:
                raise OpsBlocked(error)
            active_evidence(conn, proj.name, json.loads(draft['evidence_ids']), now)
            reasons, _ = draft_code_reasons(conn, cfg, proj, draft, now)
            reasons.extend(delivery_length_reasons(draft['channel'], draft['copy'], schedule['utm_url'], cfg))
            if reasons:
                raise OpsBlocked(reasons[0])
            if draft['channel'] == 'instagram' and (not draft['card_path'] or not os.path.isfile(resolve_path(draft['card_path']))):
                raise OpsBlocked('missing_card_asset')
            # The approved copy is immutable; tracked link is also provided separately if campaign changed.
            payload = {
                'project': proj.name, 'channel': draft['channel'],
                'account': {'alias': alias, 'confirmed': cfg['ops']['accounts'][proj.name][draft['channel']].get('confirmed') is True,
                            'logout_allowed': False},
                'forbidden_accounts': sorted(set(['Crom_kr', 'agamajung'] + cfg['ops']['forbidden_accounts'])),
                'copy': draft['copy'], 'card_copy': draft['card_copy'], 'card_path': draft['card_path'], 'utm_url': schedule['utm_url'],
                'delivery_copy': delivery_copy(draft['copy'], schedule['utm_url']),
                'compliance': {k: compliance[k] for k in ('id', 'verdict', 'copy_hash', 'checked_at')},
                'schedule': {'id': schedule['id'], 'post_id': schedule['post_id'], 'state': 'scheduled'},
                'checklist': ['X @hanset_mag 확인 후 계정 사용', '어떤 계정도 로그아웃하지 않음', '@Crom_kr / @agamajung 사용 금지',
                              'compliance PASS·copy_hash 일치 확인', 'UTM utm_campaign/utm_content 확인',
                              'X 가중 280 / Threads 500자 이하', 'IG 카드 경로와 저작권 확인', '체험단·협찬·광고 표시 확인',
                              '브라우저 자동화 없음 — 사람이 직접 게시',
                              '게시 후 queue verify <post_id> <공개 URL> 실행; 기존 metrics로 48h/7d 수집 후 ops measure'],
                'no_browser_automation': True,
            }
            payload = guards.redact_obj(payload)
            if not dry_run:
                stem = out_path(cfg, 'publish_guides', proj.name, f"{ab.kst(now):%Y%m%d}_{draft['channel']}_{schedule['post_id']}")
                jpath = write_file(stem + '.json', dumps(payload) + '\n')
                mpath = write_file(stem + '.md', '# 발행 지시서 — 사람이 직접 게시\n\n```json\n' + dumps(payload) + '\n```\n')
                conn.execute('INSERT INTO ops_publish_guides(ops_schedule_id,json_path,md_path,payload,created) VALUES (?,?,?,?,?) '
                             'ON CONFLICT(ops_schedule_id) DO UPDATE SET json_path=excluded.json_path,md_path=excluded.md_path,payload=excluded.payload,created=excluded.created',
                             (schedule['id'], jpath, mpath, dumps(payload), ab.iso(now)))
                conn.execute("UPDATE ops_drafts SET status='guide_ready',updated=? WHERE id=?", (ab.iso(now), draft['id']))
                conn.commit()
            results.append({'status': 'ok', 'guide': payload})
        except OpsBlocked as exc:
            record_block(conn, draft, str(exc), dry_run)
            results.append({'status': 'blocked', 'ops_draft_id': draft['id'], 'reason': str(exc)})
    return {'status': 'blocked' if any(r['status'] == 'blocked' for r in results) else 'ok', 'results': results}


def cmd_measure(conn, cfg, proj, now, *, dry_run=False, mock=None, post_id=None, **kwargs):
    project_checks(cfg, proj)
    if not dry_run:
        ab.process_rounds_log(conn, cfg, now)
        ab.import_metrics_dir(conn, cfg, [proj], now)
        ab.run_diagnostics(conn, cfg, [proj], now)
    rows = conn.execute('SELECT * FROM diagnoses WHERE project=?' + (' AND post_id=?' if post_id else '') + ' ORDER BY id',
                        (proj.name, post_id) if post_id else (proj.name,)).fetchall()
    if not rows:
        return skipped('no_diagnoses')
    results = []
    for row in rows:
        response = runner.generate('measure', context.clean(dict(row)), cfg, dry_run=dry_run, mock=mock)
        required = ('hypothesis', 'actual_with_denominator', 'next_single_change')
        if any(not isinstance(response.get(k), str) or not response[k].strip() for k in required):
            raise OpsLLMError('llm_output_invalid')
        alternatives = response.get('alternative_causes')
        if (not isinstance(alternatives, list) or not alternatives
                or any(not isinstance(cause, str) or not cause.strip() for cause in alternatives)):
            raise OpsLLMError('llm_output_invalid')
        corpus = row['actual_with_denominator'] + ' ' + ' '.join(f'{row[k]:g}' for k in ('numerator', 'denominator') if row[k] is not None)
        if set(guards.copy_numbers(response['actual_with_denominator'])) - set(guards.copy_numbers(corpus)):
            raise OpsBlocked('measure_fabricated_number')
        guards.validate_single_change(response['next_single_change'])
        response['category'] = row['category']
        response['verdict'] = row['verdict']
        response['post_id'] = row['post_id']
        response['check_kind'] = row['check_kind']
        response = context.clean(response)
        if not dry_run:
            conn.execute('INSERT INTO ops_measure_notes(project,post_id,check_kind,category,verdict,payload,created,updated) VALUES (?,?,?,?,?,?,?,?) '
                         'ON CONFLICT(post_id,check_kind) DO UPDATE SET category=excluded.category,verdict=excluded.verdict,payload=excluded.payload,updated=excluded.updated',
                         (proj.name, row['post_id'], row['check_kind'], row['category'], row['verdict'], dumps(response), ab.iso(now), ab.iso(now)))
        results.append(response)
    if not dry_run:
        conn.commit()
    return {'status': 'ok', 'notes': results}
