"""Extractive Markdown packet parser. Never executes packet instructions."""
import calendar
import datetime as dt
import hashlib
import re
from urllib.parse import urlsplit, parse_qsl
from .guards import OpsBlocked, URL, redact

# Policy vocabulary that must never reach published copy. Terms are matched as
# case-folded substrings, so a word class (refund) is used instead of a brand
# token (Flex) that also appears in legitimate name-change facts.
PROHIBITED_TERMS = ('RUNPORT', 'MZ 사우나', '네이버', '효능', '완치', '회복 보장',
                    'T2', 'T3', 'T4')
PROHIBITION_CUE = re.compile(
    r'금지|쓰지\s*말|넣지\s*말|만들지\s*말|언급\s*금지|무시|do\s+not\s+mention|'
    r"don'?t\s+mention|forbidden", re.I)
CONSTRAINT_PATH = re.compile(r'빼야|금지|꼭 반영|forbidden', re.I)
EXCLUDED_PATH = re.compile(r'참고|변경 알림|정정|^.* / B\.')
LIST_MARKER = re.compile(r'^\s*(?:\d+[.)]|[-*])\s+')


def plain(text):
    return text.replace('**', '').replace('`', '').strip()


def _walk(text):
    """Yield (line_no, raw, path, fenced) with Markdown heading state applied."""
    sections = []
    fenced = False
    for line_no, raw in enumerate(text.splitlines(), 1):
        heading = re.match(r'^(#{1,6})\s+(.+)', raw)
        if heading:
            level = len(heading[1])
            sections = [(n, t) for n, t in sections if n < level] + [(level, plain(heading[2]))]
            continue
        path = ' / '.join(t for _, t in sections)
        if raw.strip().startswith('```'):
            fenced = not fenced
            continue
        yield line_no, raw, path, fenced


def _quoted(line):
    """Literal exclusions quoted in packet directives, never bare brand tokens."""
    found = re.findall(r'["“]([^"”]+)["”]', line)
    found += re.findall(r"(?<![A-Za-z])'([^'\n]{2,50})'(?![A-Za-z])", line)
    return [q.strip() for q in found if re.search(r'[A-Za-z가-힣]', q)]


def _extract_forbidden(line, forbidden):
    for quoted in _quoted(line):
        forbidden.add(quoted)
    for term in PROHIBITED_TERMS:
        if term in line:
            forbidden.add(term)
    # Refund policy is a class of claim: block "refund"/"환불" without blocking
    # the allowed "Lite Flex / Flex name change" facts.
    if re.search(r'환불|refund', line, re.I):
        forbidden.update(('환불', 'refund'))
    for match in re.finditer(r'(?:do\s+not|don\'?t)\s+mention\s+([^.;\n]+)', line, re.I):
        core = re.split(r'\s+(?:in|on|or|and)\s+', match.group(1), flags=re.I)[0]
        core = core.strip(' .,:;!?()[]"\'')
        if re.search(r'[A-Za-z가-힣]', core):
            forbidden.add(core)
            if core.casefold().endswith('s'):
                forbidden.add(core[:-1])


def _forbidden_terms(text):
    forbidden = set()
    for _, raw, path, fenced in _walk(text):
        if fenced:
            continue
        line = plain(re.sub(r'^\s*(?:[-*]|\d+[.)])\s+', '', raw))
        if not line:
            continue
        # "빼야/금지" sections are exclusion lists, and any explicit prohibition
        # clause counts wherever it appears. Advisory ("꼭 반영") lines that do
        # not prohibit anything are fact material, never forbidden vocabulary.
        if re.search(r'빼야|금지|forbidden', path, re.I) or PROHIBITION_CUE.search(line):
            _extract_forbidden(line, forbidden)
    return forbidden


def _header(text):
    return text.split('\n## ', 1)[0]


def _header_sources(text):
    sources = []
    for raw in URL.findall(_header(text)):
        url = raw.rstrip('`.,)')
        if urlsplit(url).hostname != 'hanset.soulbound.co.kr' and url not in sources:
            sources.append(url)
    return sources


def _landing(text, explicit):
    if explicit:
        return explicit
    found = []
    for raw in URL.findall(text):
        url = raw.rstrip('`.,)')
        if urlsplit(url).hostname != 'hanset.soulbound.co.kr':
            continue
        base = url.split('?')[0]
        if base not in found:
            found.append(base)
    if not found:
        return None
    # Prefer the site root when the packet also links a deep page; the root is
    # the UTM destination the daily package contract expects.
    roots = [u for u in found if urlsplit(u).path in ('', '/')]
    if roots:
        return roots[0]
    if len(found) == 1:
        return found[0]
    raise OpsBlocked('packet_landing_conflict')


def _clean_fact(line, forbidden):
    """Drop packet-encoded prohibitions while keeping the factual prefix."""
    def drop_parenthetical(match):
        inner = match.group(1)
        if PROHIBITION_CUE.search(inner) or re.search(r'\bnot allowed\b', inner, re.I):
            return ''
        if any(term.casefold() in inner.casefold() for term in forbidden if term):
            return ''
        return match.group(0)

    line = re.sub(r'\(([^()]*)\)', drop_parenthetical, line)
    line = re.split(r'\s+→\s*do not\s+|\bDo not\b|\bThe HYROX venue map does not\b|'
                    r'쓰지\s*말\s*것|언급\s*금지',
                    line, flags=re.I)[0]
    # A prohibition inside an otherwise factual sentence keeps only the prefix.
    if re.search(r'\bnot allowed\b', line, re.I):
        prefix = re.split(r'(?<=[.!?])\s+(?=[^.]*not allowed)', line)[0].strip()
        if re.search(r'\bnot allowed\b', prefix, re.I):
            return ''
        line = prefix
    line = re.sub(r'\s+', ' ', line).strip()
    if not line or line.endswith(':'):
        return ''
    line = line.strip(' .,;:-')
    if PROHIBITION_CUE.search(line) or re.search(r'금지|쓰지|미확인|not allowed', line, re.I):
        return ''
    if any(term and term.casefold() in line.casefold() for term in forbidden):
        return ''
    return line


def parse_packet(text, mmdd, default_campaign='hanset_daily_w2'):
    if not re.fullmatch(r'\d{4}', mmdd or '') or redact(text) != text:
        raise OpsBlocked('packet_metadata_invalid')
    # Explicit year only; the runtime clock is never a source of packet dates.
    years = set(re.findall(r'\b(20\d{2})-\d{2}-\d{2}\b', _header(text)))
    if len(years) != 1:
        raise OpsBlocked('packet_year_missing_or_conflicting')
    year = int(next(iter(years)))
    try:
        package_date = dt.date(year, int(mmdd[:2]), int(mmdd[2:]))
    except ValueError:
        raise OpsBlocked('packet_date_invalid') from None
    phrases = set(re.findall(r'checked ([A-Z][a-z]{2}) (\d{1,2})\b', text))
    if len(phrases) != 1:
        raise OpsBlocked('packet_checked_missing_or_conflicting')
    mon, day = next(iter(phrases))
    try:
        checked = dt.date(year, list(calendar.month_abbr).index(mon), int(day))
    except ValueError:
        raise OpsBlocked('packet_checked_invalid') from None
    confirmations = set(re.findall(r'확인\s*:\s*(20\d{2}-\d{2}-\d{2})', _header(text)))
    if confirmations and confirmations != {str(checked)}:
        raise OpsBlocked('packet_checked_conflict')
    if checked > package_date:
        raise OpsBlocked('packet_checked_after_publication')
    contents = set(re.findall(r'utm_content\s*[=`:"\s]+([a-zA-Z0-9_./-]+)', text))
    campaigns = set(re.findall(r'utm_campaign\s*[=`:"\s]+([a-zA-Z0-9_-]+)', text))
    campaigns.update(re.findall(r'\bcampaign\s+`([^`]+)`', text))
    for url in URL.findall(text):
        for key, value in parse_qsl(urlsplit(url.rstrip('`')).query):
            if key == 'utm_campaign': campaigns.add(value)
            if key == 'utm_content': contents.add(value)
    if len(contents) != 1 or len(campaigns) > 1:
        raise OpsBlocked('packet_utm_missing_or_conflicting')
    content = next(iter(contents))
    if not re.fullmatch(r'[a-z0-9]+(?:_[a-z0-9]+)*', content):
        raise OpsBlocked('packet_content_invalid')
    suffix = content
    if re.match(r'^\d{4}_', content):
        if content[:4] != mmdd:
            raise OpsBlocked('packet_content_date_mismatch')
        suffix = content[5:]
    campaign = next(iter(campaigns), default_campaign)
    if not re.fullmatch(r'[a-z0-9_]+', campaign):
        raise OpsBlocked('packet_campaign_invalid')
    result = dict(package_date=str(package_date), checked=str(checked), checked_phrase=f'checked {mon} {day}',
                  id=f'{package_date}_{suffix}', campaign=campaign, content=content, facts=[], forbidden=[],
                  constraints=[], hints=[], drafts=[], sources=[], landing=None,
                  packet_hash=hashlib.sha256(text.encode()).hexdigest())
    forbidden = _forbidden_terms(text)
    header_sources = _header_sources(text)
    result['sources'].extend(header_sources)
    sources = []
    parent = ''
    last_path = None
    for line_no, raw, path, fenced in _walk(text):
        if path != last_path:
            # A new heading starts a new provenance scope.
            sources = []
            parent = ''
            last_path = path
        excluded = bool(EXCLUDED_PATH.search(path))
        constraints = bool(CONSTRAINT_PATH.search(path))
        line = plain(re.sub(r'^\s*(?:[-*]|\d+[.)])\s+', '', raw))
        if not line:
            continue
        urls = [u.rstrip('`.,)') for u in URL.findall(line)]
        if excluded:
            continue
        if constraints:
            result['constraints'].append({'line': line_no, 'section': path, 'text': line})
            continue
        if '랜딩' in path:
            for url in urls:
                if urlsplit(url).hostname == 'hanset.soulbound.co.kr':
                    landing = url.split('?')[0]
                    if result['landing'] and result['landing'] != landing:
                        raise OpsBlocked('packet_landing_conflict')
                    result['landing'] = landing
                elif url not in result['sources']:
                    result['sources'].append(url)
            continue
        if '그대로 써도 되는 사실' in path or 'allowed facts' in path.lower():
            if urls and not LIST_MARKER.match(raw):
                for url in urls:
                    if url not in sources:
                        sources.append(url)
                    if url not in result['sources']:
                        result['sources'].append(url)
                continue
            if re.match(r'^\s*\d+[.)]', raw):
                parent = line.split(':', 1)[0]
            if not LIST_MARKER.match(raw):
                continue
            cleaned = _clean_fact(line, forbidden)
            if not cleaned:
                continue
            candidates = sources or header_sources
            linked = list(candidates)
            if 'public-transport' in cleaned or 'Buses' in parent or 'Rail' in parent or 'Driving' in parent:
                linked = [u for u in candidates if 'transportation' in u]
            elif 'official rate' in cleaned:
                linked = [u for u in candidates if 'parking_user' in u]
            elif 'HYROX official' in cleaned or parent in ('Event', 'Address', 'Race-day registration'):
                linked = [u for u in candidates if '/event/' in u]
            if not linked:
                raise OpsBlocked('packet_fact_source_missing')
            if 'Venue map' in cleaned:
                linked = [u for u in candidates if u.endswith('.pdf')]
            result['facts'].append(dict(fact_id=f'f{line_no}', text=cleaned, line=line_no, section=path,
                                        subject=parent, sources=linked))
        elif '카드 구성' in path:
            if re.match(r'^\s*\d+[.)]', raw):
                result['hints'].append(dict(text=line, line=line_no, section=path))
        elif '훅' in path or fenced:
            result['drafts'].append(dict(text=line, line=line_no, section=path))
    result['landing'] = _landing(text, result['landing'])
    if not result['facts'] or not result['landing']:
        raise OpsBlocked('packet_facts_or_landing_missing')
    # Card display hints have separate provenance; structural numbering is omitted.
    corpus = ' '.join(f['text'] for f in result['facts'])
    from .guards import copy_numbers
    allowed_numbers = set(copy_numbers(corpus))
    for hint in result['hints']:
        candidate = hint['text']
        if (not set(copy_numbers(candidate)) - allowed_numbers and
                not re.search(r'금지|쓰지|미확인|do not', candidate, re.I) and
                not any(term and term.casefold() in candidate.casefold() for term in forbidden)):
            result['facts'].append(dict(fact_id=f'h{hint["line"]}', text=candidate, line=hint['line'],
                                        section=hint['section'], subject='card hint', sources=result['sources']))
    result['forbidden'] = sorted(set(forbidden))
    return result
