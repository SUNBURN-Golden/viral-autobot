"""Deterministic, fail-closed policy checks. No provider or application imports."""
import hashlib
import json
import logging
import os
import re
import unicodedata


class OpsError(Exception):
    pass


class OpsBlocked(OpsError):
    pass


class OpsLLMError(OpsError):
    pass


SECRET_PATTERNS = (
    re.compile(r'(?i)\b(?:sk|rk)-[A-Za-z0-9_-]+'),
    re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+'),
    re.compile(r'''(?i)\b(?:password|passwd|pwd|api[_-]?key|apikey|secret|token|access[_-]?token|client[_-]?secret|otp|인증번호)["']?\s*[=:]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s&"',}]+)'''),
    re.compile(r'(?i)[?&](?:token|secret|password|api[_-]?key|access[_-]?token|key)=[^&\s]+'),
)
_SECRET_KEYS = re.compile(r'(?i)^(?:password|passwd|pwd|api[_-]?key|apikey|secret|token|access[_-]?token|client[_-]?secret|otp|인증번호)$')


REGISTERED_SECRETS = set()


def register_secret(value):
    if value: REGISTERED_SECRETS.add(value)


def redact(text, secrets=()):
    text = str(text)
    values = [s for s in (*secrets, *REGISTERED_SECRETS, os.environ.get('DEEPSEEK_API_KEY', ''), os.environ.get('X_BEARER_TOKEN', '')) if s]
    for secret in sorted(values, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    for pattern in SECRET_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text


def redact_obj(value, secrets=()):
    if isinstance(value, dict):
        return {redact(k, secrets): '[REDACTED]' if _SECRET_KEYS.fullmatch(str(k)) else redact_obj(v, secrets)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v, secrets) for v in value]
    return redact(value, secrets) if isinstance(value, str) else value


class SecretFilter(logging.Filter):
    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = secrets

    def filter(self, record):
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        if record.exc_info:
            import traceback
            record.exc_text = redact(''.join(traceback.format_exception(*record.exc_info)), self.secrets)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self.secrets)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self.secrets)
        return True


def install_redaction(logger, secrets=()):
    for handler in logger.handlers:
        handler.addFilter(SecretFilter(secrets))


URL = re.compile(r'https?://[^\s<>]+')


def x_weighted_length(text, cfg=None):
    limits = (cfg or {}).get('ops', {}).get('limits', {})
    total = len(URL.findall(text)) * limits.get('url_weight', 23)
    wide = limits.get('wide_char_weight', 2)
    # Twitter weights: Latin/general punctuation 1, Hangul/₩/emoji wide by config.
    for char in URL.sub('', text):
        cp = ord(char)
        total += 1 if cp <= 0x10ff or 0x2000 <= cp <= 0x200d or 0x2010 <= cp <= 0x201f or 0x2032 <= cp <= 0x2037 else wide
    return total


PROHIBITED = {
    'fake_review': ('가짜 리뷰', '가짜리뷰', 'fake review', '거짓 방문기'),
    'sockpuppet': ('부계정', 'sockpuppet'),
    'paid_comment': ('댓글 알바', '댓글알바', 'paid comment'),
    'competitor_defamation': ('경쟁사 비방', '경쟁사비방', 'competitor defamation'),
    'undisclosed_sponsorship': ('체험단 미표기', '미표기 체험단', 'undisclosed sponsorship'),
    'copyright_image': ('저작권 이미지 무단 사용', '무단 사진', '타인 시설 사진', 'unlicensed image'),
    'bait': ('충격', '아무도 모르는', '무조건'),
}


def prohibited_terms():
    return PROHIBITED


def find_prohibited(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    return ['prohibited:' + key for key, terms in PROHIBITED.items() if any(t.casefold() in text for t in terms)]


# Numeric claims retain units; currency variants canonicalize to 원.
NUMBER = re.compile(r'(?:₩\s*|KRW\s*|won\s*)?\d[\d,]*(?:\.\d+)?(?:\s*(?:원|KRW|won|%|가지|개|명|시간|분|시|일|년|tips?))?', re.I)


def copy_numbers(text):
    text = URL.sub('', str(text))
    text = re.sub(r'\[CARD\s*\d+(?:/\d+)?\]', '', text, flags=re.I)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.M)
    text = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '', text)
    result = []
    for match in NUMBER.finditer(text):
        token = re.sub(r'\s+|,', '', match.group())
        currency = bool(re.search(r'₩|원|KRW|won', token, re.I))
        if currency:
            token = re.sub(r'₩|원|KRW|won', '', token, flags=re.I) + '원'
        result.append(token.casefold())
    return sorted(set(result))


def evidence_numbers(evidences):
    return set(copy_numbers(' '.join(str(dict(e).get(k, '')) for e in evidences for k in
                                   ('fact', 'allowed_claims', 'keywords', 'verified_at', 'valid_until', 'source_url'))))


def unsupported_numbers(text, evidences, cfg):
    allowed = evidence_numbers(evidences) | set(cfg['ops']['limits']['number_allowlist'])
    reasons = ['unsupported_number:' + n for n in copy_numbers(text) if n not in allowed]
    # Dates are omitted from numeric tokens, but still need exact evidence support.
    corpus = ' '.join(str(dict(e).get(k, '')) for e in evidences for k in
                      ('fact', 'allowed_claims', 'verified_at', 'valid_until'))
    dates = set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', URL.sub('', text)))
    reasons.extend('unsupported_date:' + d for d in sorted(dates) if d not in corpus and d not in allowed)
    return reasons


def content_hash(copy, card_copy=None):
    return hashlib.sha256(json.dumps({'copy': copy, 'card_copy': card_copy or ''},
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def check_brand(cfg, project):
    ops = cfg['ops']
    brand = ops['brands'].get(project, {})
    if brand.get('hold') is True or ops.get('holds', {}).get(project) is True:
        raise OpsBlocked('brand_hold')
    if not ops['enabled'] or project != 'hanset' or brand.get('enabled') is not True:
        raise OpsBlocked('brand_not_supported')


def check_account(cfg, project, channel):
    account = cfg['ops']['accounts'].get(project, {}).get(channel, {})
    alias = str(account.get('alias') or '').lstrip('@') or None
    forbidden = {'crom_kr', 'agamajung'} | {a.lstrip('@').casefold() for a in cfg['ops']['forbidden_accounts']}
    if alias and alias.casefold() in forbidden:
        return alias, 'forbidden_account'
    if channel == 'x' and alias != 'hanset_mag':
        return alias, 'x_account_not_allowed'
    if not alias:
        return None, 'account_alias_missing'
    # Hard rule cannot be disabled by a permissive configuration.
    if channel == 'x' and account.get('confirmed') is not True:
        return alias, 'x_account_confirmation_required'
    if channel not in ('x', 'threads', 'instagram'):
        return alias, 'channel_not_supported'
    return alias, None


def check_cost_config(cfg):
    ops = cfg['ops']
    if any(ops['cost'].get(k) is not False for k in ('allow_paid_actions', 'allow_new_accounts')):
        raise OpsBlocked('paid_actions_forbidden')
    if ops['publish'].get('browser_automation') is not False or ops['account_rules'].get('logout_allowed') is not False:
        raise OpsBlocked('unsafe_publish_config')


def validate_copy(channel, copy, card_copy, proj, evidences, cfg):
    reasons = []
    if channel not in ('x', 'threads', 'instagram'):
        reasons.append('channel_not_supported')
    for text in (copy, card_copy):
        if text is None:
            continue
        if not isinstance(text, str) or not text.strip():
            reasons.append('empty_copy')
            continue
        reasons.extend(find_prohibited(text))
        # Retain brand exclusions and the repository's existing bait-word configuration.
        terms = list(getattr(proj, 'demand', {}).get('forbidden_terms', [])) + cfg.get('hooks', {}).get('banned_terms', [])
        reasons.extend('prohibited:brand_term:' + t for t in terms if t.casefold() in text.casefold())
        reasons.extend(unsupported_numbers(text, evidences, cfg))
        for e in evidences:
            forbidden = dict(e).get('forbidden_claims', '[]')
            try:
                forbidden = json.loads(forbidden) if isinstance(forbidden, str) else forbidden
            except ValueError:
                forbidden = []
            if any(str(t).casefold() in text.casefold() for t in forbidden if t):
                reasons.append('forbidden_evidence_claim')
        if redact(text) != text or re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\w)(?:/?u/|@)[\w-]+', text):
            reasons.append('personal_or_secret_data')
    if not isinstance(copy, str) or not copy.strip():
        reasons.append('empty_copy')
    elif channel == 'x' and x_weighted_length(copy, cfg) > min(280, cfg['ops']['limits']['x_weighted']):
        reasons.append('x_length_exceeded')
    elif channel == 'threads' and len(copy) > min(500, cfg['ops']['limits']['threads_chars']):
        reasons.append('threads_length_exceeded')
    if channel == 'instagram' and (not isinstance(card_copy, str) or not re.search(r'\[CARD\s+\d+/3\]', card_copy)):
        reasons.append('card_copy_format')
    if channel != 'instagram' and card_copy:
        reasons.append('card_copy_channel')
    return sorted(set(reasons))


CHECKLIST = {
    'C1': '한국 표시광고법: 거짓·과장·기만 광고 금지, 검증된 evidence만 사용',
    'C2': '공정위 추천·보증 지침: 광고·협찬·체험단 관계를 명확히 표시',
    'C3': '가짜 리뷰·거짓 방문기 금지',
    'C4': '부계정·브랜드 간 인기 연출 금지',
    'C5': '댓글 알바 금지',
    'C6': '경쟁사 비방 금지',
    'C7': '체험단 미표기 금지',
    'C8': '저작권 이미지 무단 사용·타인 시설 사진 무단 사용 금지',
    'C9': '플랫폼 규칙·스팸·무관한 링크 살포 금지',
    'C10': '커뮤니티 홍보 금지 및 운영자 관계 표시·홍보 허용 여부 확인',
    'C11': '숫자·가격·시간·비교 주장 evidence 근거 및 유효기간 확인',
    'C12': '개인정보·API 키·비밀번호·OTP 비공개',
    'C13': 'X @hanset_mag 확인, @Crom_kr/@agamajung 금지, 어떤 계정도 로그아웃 금지',
    'C14': '광고비·유료 툴 결제·신규 계정 생성 경로 금지',
    'C15': '사람의 승인·PASS 후 예약, 브라우저 자동화 없음, SOULBOUND 제외 규칙 유지',
}


def compliance_code_checks(reasons):
    return {'code': {'pass': not reasons, 'reasons': reasons}}


def validate_single_change(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 120 or '\n' in text or '\r' in text or re.search(
            r'[;·•+&]|\d+[).]|그리고|또한|동시에|and also|plus|as well as', text, re.I) or len(re.findall(r'[.!?。！？]', text)) > 1:
        raise OpsBlocked('measure_multiple_changes')
