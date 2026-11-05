"""Deterministic offline responses; mock PASS never replaces human approval."""
from .guards import CHECKLIST


def response(command, context):
    if command == 'plan':
        return {'plan': '# 오늘의 HANSET 운용 계획\n\n검증된 자료와 만료일을 확인한다.\n질문에 맞는 채널 초안을 준비하고 승인과 compliance PASS 후 예약한다.\n발행은 사람이 계정을 확인한 후 수행한다.\n공개 지표 확인 후 다음 실험에서 한 가지만 바꾼다.'}
    if command == 'draft':
        evidence = context['evidence'][0]
        import json
        claims = evidence.get('allowed_claims', [])
        if isinstance(claims, str):
            claims = json.loads(claims)
        fact = claims[0] if claims else evidence['fact']
        return {'drafts': [
            {'channel': 'x', 'copy': fact},
            {'channel': 'threads', 'copy': fact + '\n방문 전에 어떤 조건을 확인하시나요?'},
            {'channel': 'instagram', 'copy': fact,
             'card_copy': '[CARD 1/3] 방문 전 확인\n[CARD 2/3] ' + fact + '\n[CARD 3/3] 공식 안내 확인'},
        ]}
    if command == 'compliance':
        return {'verdict': 'PASS', 'reasons': [], 'checks': {k: {'pass': True, 'note': 'offline fixture'} for k in CHECKLIST}}
    if command == 'measure':
        import json
        alternatives = context['alternative_causes']
        if isinstance(alternatives, str):
            try:
                alternatives = json.loads(alternatives)
            except ValueError:
                alternatives = [alternatives]
        return {'hypothesis': context['hypothesis'], 'actual_with_denominator': context['actual_with_denominator'],
                'alternative_causes': alternatives, 'next_single_change': '다음 발행 시 제목을 질문형으로'}
    raise ValueError('unknown mock command')
