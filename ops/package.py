"""Render-compatible packages, deterministic provenance guards and atomic publication."""
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import unicodedata
from urllib.parse import urlsplit, parse_qsl
from . import guards, runner
from .config import resolve_path
from .packet import parse_packet
from .guards import OpsBlocked, OpsLLMError

MOTIFS = ('ticket', 'tattoo', 'locker_key', 'lamb_towel', 'eggs_sikhye', 'hot_tub', 'map_pins', 'bucket_ladle')
PALETTES = dict(mint='#E3EFEA', butter='#F7EDC8', sky='#E2ECF5', blush='#F6E1E0', clay='#EFDCCB',
                moss='#E4E8D4', stone='#E6E4E0', slate='#DDE3E6', ice='#EEF1F0', peach='#F6E3D8', sage='#EAEEE4', sand='#F3EAD6')
SAFE = {'HANSET', 'Official information', 'Confirm before you go.', '프로필 링크에서 확인하세요.',
        'Link in profile.', 'Official sources', 'Title:', 'Facts:', 'Sources:', 'Home:', 'Getting there', 'Parking',
        'Transport', 'Visit', 'Information'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def history(cfg, post_id):
    if cfg['ops']['package']['cover_lookback'] != 6:
        raise OpsBlocked('cover_lookback_invalid')
    try:
        with open(resolve_path(cfg['ops']['paths']['history_json']), encoding='utf-8') as stream:
            entries = json.load(stream)
        if not isinstance(entries, list) or any(not isinstance(e, dict) or
                any(not isinstance(e.get(k), str) or not e[k] for k in ('id', 'motif', 'palette')) or
                ('bg' in e and (not isinstance(e['bg'], str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', e['bg']))) or
                (e['palette'] not in PALETTES and 'bg' not in e) or
                (e['palette'] in PALETTES and 'bg' in e and e['bg'].lower() != PALETTES[e['palette']].lower())
                for e in entries):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise OpsBlocked('cover_history_invalid') from None
    return [e for e in entries if e['id'] != post_id][-6:]


def available_covers(entries):
    motifs = [m for m in MOTIFS if all(m != e['motif'] for e in entries)]
    palettes = [p for p in PALETTES if all(PALETTES[p].lower() != e.get('bg', PALETTES.get(e['palette'], '')).lower() for e in entries)]
    if not motifs or not palettes: raise OpsBlocked('cover_candidates_exhausted')
    return motifs, palettes


def schema(response, packet):
    if not isinstance(response, dict) or set(response) != {'post', 'x', 'threads', 'ig_caption', 'reddit', 'claim_refs'}:
        raise OpsBlocked('package_schema')
    if any(not isinstance(response[k], str) or not response[k] for k in ('x', 'threads', 'ig_caption', 'reddit')):
        raise OpsBlocked('package_schema')
    post = response['post']
    fields = {'id', 'checked', 'title', 'subtitle', 'cover', 'items', 'summary', 'sources', 'footer', 'cta', 'label', 'items_per_slide'}
    if not isinstance(post, dict) or not fields <= set(post) or set(post) - fields - {'pill_label', 'example'}:
        raise OpsBlocked('package_schema')
    for k in ('id', 'checked', 'title', 'subtitle', 'footer', 'cta', 'label', 'pill_label'):
        if k in post and not isinstance(post[k], str): raise OpsBlocked('package_schema')
    # Presentation text cannot be contentless: every rendered field carries copy.
    for k in ('title', 'subtitle', 'footer', 'cta', 'label'):
        if not post[k].strip(): raise OpsBlocked('package_schema')
    if 'example' in post and type(post['example']) is not bool: raise OpsBlocked('package_schema')
    if post['id'] != packet['id'] or post['checked'] != packet['checked']:
        raise OpsBlocked('package_identity')
    if type(post['items_per_slide']) is not int or post['items_per_slide'] <= 0: raise OpsBlocked('package_schema')
    cover = post['cover']
    if isinstance(cover, dict) and 'seed' not in cover:
        cover['seed'] = int(hashlib.sha256(packet['id'].encode()).hexdigest()[:8], 16)
    if not isinstance(cover, dict) or set(cover) != {'motif', 'palette', 'kicker', 'seed', 'layout'} or any(
            not isinstance(cover.get(k), str) for k in ('motif', 'palette', 'kicker', 'layout')):
        raise OpsBlocked('package_schema')
    if type(cover['seed']) is not int or cover['layout'] not in ('text_top', 'art_top'):
        raise OpsBlocked('package_schema')
    if cover['motif'] not in MOTIFS or cover['palette'] not in PALETTES: raise OpsBlocked('cover_invalid')
    for key in ('summary', 'sources'):
        if not isinstance(post[key], list) or not post[key] or any(not isinstance(t, str) or not t.strip() for t in post[key]): raise OpsBlocked('package_schema')
    if not isinstance(post['items'], list) or not post['items']: raise OpsBlocked('package_schema')
    for item in post['items']:
        if not isinstance(item, dict) or not {'name', 'area', 'price', 'tattoo'} <= set(item) or set(item) - {'name', 'area', 'price', 'tattoo', 'memo'} or any(not isinstance(v, str) for v in item.values()):
            raise OpsBlocked('package_schema')
        if not item['name'].strip() or not any(item.get(k, '').strip() for k in ('area', 'price', 'memo')):
            raise OpsBlocked('package_schema')
    if not isinstance(response['claim_refs'], dict): raise OpsBlocked('package_schema')


def strings(value, path=''):
    if isinstance(value, str): yield path, value
    elif isinstance(value, dict):
        for k, v in value.items(): yield from strings(v, path + '.' + k if path else k)
    elif isinstance(value, list):
        for i, v in enumerate(value): yield from strings(v, path + '.' + str(i))


def validate(response, packet, entries, cfg, proj):
    schema(response, packet)
    reasons = []
    refs = response['claim_refs']
    facts = {f['fact_id']: f['text'] for f in packet['facts']}
    code_paths = {'post.id', 'post.checked', 'post.cover.motif', 'post.cover.palette', 'post.cover.layout'}
    urls = {u: None for u in packet['sources']}
    import autobot as ab
    for ch in ('x', 'threads', 'reddit'):
        urls[ab.add_utm(packet['landing'], ch, packet['campaign'], content=packet['content'], medium='social')] = ch
    for path, text in strings({k: v for k, v in response.items() if k != 'claim_refs'}):
        if path in code_paths: continue
        reasons += guards.find_prohibited(text)
        brand_terms = list(getattr(proj, 'demand', {}).get('forbidden_terms', [])) + cfg.get('hooks', {}).get('banned_terms', [])
        if any(t.casefold() in text.casefold() for t in packet['forbidden'] + brand_terms if t): reasons.append('packet_forbidden')
        normalized = unicodedata.normalize('NFKC', text)
        if re.search(r'\+\s*\d{1,3}[\s().-]*(?:\d[\s().-]*){7,12}\b', normalized):
            reasons.append('personal_or_secret_data')
        if guards.redact(text) != text or re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:\+\s*\d[\s().-]*)?(?:0\s*1\s*[016789]|0\s*2|0\s*[3-6]\s*\d)[\s().-]*\d(?:[\s().-]*\d){6,8}\b', text):
            reasons.append('personal_or_secret_data')
        if re.search(r'(?:race|HYROX|레이스).{0,35}(?:is in|at|장소|가|는).{0,12}Hall\s*[12]', text, re.I): reasons.append('packet_context_forbidden')
        # Every displayed fragment must match an entire source fragment or a fixed presentation string.
        # A numeric substring alone is never a claim: this prevents price/time reassignment.
        used = refs.get(path, [])
        if not isinstance(used, list) or any(not isinstance(fid, str) or fid not in facts for fid in used):
            reasons.append('package_claim_refs'); continue
        allowed = SAFE | {packet['checked_phrase']} | {facts[fid] for fid in used}
        # URLs that appear verbatim inside a referenced packet fact are packet
        # sources too (e.g. an allowed fact that names the venue homepage).
        path_urls = set(urls)
        for fid in used:
            path_urls.update(guards.URL.findall(facts[fid]))
        remaining = text
        text_urls = guards.URL.findall(text)
        for url in text_urls:
            if url not in path_urls: reasons.append('package_url')
            if 'utm_' in url:
                q = parse_qsl(urlsplit(url).query)
                if len(q) != len({k for k, _ in q}) or dict(q).get('utm_campaign') != packet['campaign'] or dict(q).get('utm_content') != packet['content']:
                    reasons.append('package_utm')
                if path in ('x', 'threads', 'reddit') and dict(q).get('utm_source') != path: reasons.append('package_utm')
        # Remove whole allowed facts before bare URLs: an allowed fact may itself
        # contain a packet URL, and stripping the URL first would break its match.
        for fragment in sorted(allowed - {''}, key=len, reverse=True):
            remaining = remaining.replace(fragment, '')
        for url in text_urls:
            remaining = remaining.replace(url, '')
        if remaining.strip(' \n\r\t#>*-:|.,()'):
            reasons.append('package_unsupported_claim')
    # Each channel body must carry at least one packet fact, not just a footer
    # and a tracked link.
    for ch in ('x', 'threads', 'ig_caption', 'reddit'):
        channel_refs = refs.get(ch)
        if not isinstance(channel_refs, list) or not channel_refs:
            reasons.append('package_claim_refs')
    for ch in ('x', 'threads', 'reddit'):
        expected = ab.add_utm(packet['landing'], ch, packet['campaign'], content=packet['content'], medium='social')
        if response[ch].splitlines().count(expected) != 1: reasons.append('package_utm')
        if packet['checked_phrase'] not in response[ch]: reasons.append('package_checked_footer')
    if packet['checked_phrase'] not in response['ig_caption']: reasons.append('package_checked_footer')
    if packet['checked_phrase'] not in response['post']['footer']: reasons.append('package_checked_footer')
    ig = response['ig_caption']
    if 'hanset.soulbound.co.kr' in ig.casefold() or not any(x in ig for x in ('프로필 링크', 'Link in profile')): reasons.append('package_ig_link')
    reddit = response['reddit']
    if not reddit.startswith('Title:') or 'Facts:' not in reddit or not any(u in reddit for u in packet['sources']): reasons.append('package_reddit_contract')
    if guards.x_weighted_length(response['x']) > 280: reasons.append('x_length_exceeded')
    if len(response['threads']) > 500: reasons.append('threads_length_exceeded')
    cover = response['post']['cover']
    motifs, palettes = available_covers(entries)
    if cover['motif'] not in motifs or cover['palette'] not in palettes: reasons.append('cover_duplicate')
    return sorted(set(reasons))


def mock_package(payload):
    packet = payload['packet']; fact = min((f for f in packet['facts'] if f['subject'] != 'card hint' and not guards.URL.search(f['text'])), key=lambda f: len(f['text']))
    # Use an entire short fact, retaining its subject and units.
    text = fact['text']; checked = packet['checked_phrase']
    post = dict(id=packet['id'], checked=packet['checked'], title='Getting there', subtitle='Official information',
                cover=dict(motif=payload['motifs'][0], palette=payload['palettes'][0], kicker='HANSET', seed=7, layout='text_top'),
                items=[dict(name='Information', area='', price='', tattoo='', memo=text)], summary=[text],
                sources=packet['sources'], footer=checked, cta='Link in profile.', label='HANSET', items_per_slide=1,
                pill_label='')
    import autobot as ab
    urls = {ch: ab.add_utm(packet['landing'], ch, packet['campaign'], content=packet['content'], medium='social') for ch in ('x', 'threads', 'reddit')}
    result = dict(post=post, x=text+'\n'+checked+'\n'+urls['x'], threads=text+'\n'+checked+'\n'+urls['threads'],
                  ig_caption=text+'\n'+checked+'\nLink in profile.',
                  reddit='Title: Getting there\nFacts:\n'+text+'\n'+checked+'\nSources:\n'+'\n'.join(packet['sources'])+'\nHome:\n'+urls['reddit'])
    result['claim_refs'] = {path: [fact['fact_id']] for path, val in strings(result) if text in val}
    return result


def run(conn, cfg, proj, now, *, packet, date, out_dir=None, dry_run=False, mock=None, run_id=None, **kwargs):
    from .pipeline import project_checks, begin_run
    from .compliance import evaluate_compliance
    project_checks(cfg, proj)
    try:
        with open(resolve_path(packet), encoding='utf-8') as stream: raw = stream.read()
    except OSError: raise OpsBlocked('packet_missing') from None
    parsed = parse_packet(raw, date, cfg['ops']['package']['default_campaign'])
    if run_id is None: run_id = begin_run(conn, 'package', proj.name, cfg, mode='dry-run' if dry_run else 'mock' if mock is not None else 'live', now=now)
    target = resolve_path(out_dir) if out_dir else os.path.join(resolve_path(cfg['ops']['paths']['daily_packages']), date)
    if dry_run: target = os.path.join(resolve_path(cfg['ops']['paths']['out']), 'previews', str(run_id), date)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    lock_path = target + '.lock'
    partial = None
    with open(lock_path, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.lexists(target): raise OpsBlocked('output_exists')
        reasons = []; checks = {}; response = None
        # compliance_verdict reports compliance only; non-compliance blocks stay SKIPPED.
        compliance_verdict = 'SKIPPED'
        try:
            entries = history(cfg, parsed['id']); motifs, palettes = available_covers(entries)
            retries = cfg['ops']['package']['max_retries']
            if type(retries) is not int or not 0 <= retries <= 10: raise OpsBlocked('package_retries_invalid')
            for attempt in range(retries + 1):
                import autobot as ab
                utm_urls = {ch: ab.add_utm(parsed['landing'], ch, parsed['campaign'], content=parsed['content'], medium='social') for ch in ('x', 'threads', 'reddit')}
                payload = dict(packet=parsed, motifs=motifs, palettes=palettes, attempt=attempt, retry_reasons=reasons, utm_urls=utm_urls)
                response = runner.generate('package', payload, cfg, dry_run=dry_run, mock=mock)
                reasons = validate(response, parsed, entries, cfg, proj)
                if not reasons: break
                if set(reasons) - {'x_length_exceeded', 'threads_length_exceeded', 'cover_duplicate'}: break
            if not reasons:
                compliance = evaluate_compliance(response, [], cfg, dry_run=dry_run, mock=mock)
                compliance_verdict = compliance['verdict']
                reasons = compliance['reasons']; checks = compliance['checks']
            if not reasons:
                reasons = validate(response, parsed, history(cfg, parsed['id']), cfg, proj)
        except (OpsBlocked, OpsLLMError) as exc:
            reasons = [str(exc)]
        status = 'blocked' if reasons else 'ready'
        if reasons:
            report = dict(reasons=sorted(set(reasons)), checks=checks, packet_hash=parsed['packet_hash'])
            report_dir = target + f'.{run_id}.{uuid.uuid4().hex}.blocked'
            os.mkdir(report_dir)
            with open(os.path.join(report_dir, 'COMPLIANCE_FAIL.json'), 'w', encoding='utf-8') as stream: json.dump(report, stream)
            output = report_dir
        else:
            try:
                partial = tempfile.mkdtemp(prefix=os.path.basename(target)+'.', suffix='.partial', dir=os.path.dirname(target))
                for filename, content in {'post.json': canonical(response['post'])+'\n', 'x.txt': response['x'], 'threads.txt': response['threads'], 'ig_caption.txt': response['ig_caption'], 'reddit.md': response['reddit']}.items():
                    with open(os.path.join(partial, filename), 'w', encoding='utf-8') as stream: stream.write(content)
                os.mkdir(os.path.join(partial, 'cards'))
                if os.path.lexists(target): raise OpsBlocked('output_exists')
                final_reasons = validate(response, parsed, history(cfg, parsed['id']), cfg, proj)
                if final_reasons: raise OpsBlocked(final_reasons[0])
                os.rename(partial, target); partial = None; output = target
            finally:
                if partial: shutil.rmtree(partial)
        if not dry_run:
            import autobot as ab
            conn.execute('INSERT INTO ops_packages(run_id,project,package_date,checked_date,packet_hash,package_hash,output_dir,status,blocked_reasons,compliance_verdict,compliance_checks,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (run_id, proj.name, parsed['package_date'], parsed['checked'], parsed['packet_hash'], digest(response) if not reasons else None, output, status, canonical(reasons), compliance_verdict, canonical(checks), ab.iso(now)))
            conn.commit()
        return dict(status='blocked' if reasons else 'ok', package_status=status, output_dir=output, reasons=reasons, run_id=run_id)
