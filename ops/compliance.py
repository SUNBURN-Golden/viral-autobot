"""One fail-closed compliance evaluator shared by drafts and packages."""
from . import guards, runner


def evaluate_compliance(data, code_reasons, cfg, *, dry_run=False, mock=None):
    reasons = list(code_reasons)
    checks = guards.compliance_code_checks(reasons.copy())
    if not reasons:
        try:
            response = runner.generate('compliance', {'content': data, 'checklist': guards.CHECKLIST}, cfg, dry_run=dry_run, mock=mock)
            llm_checks = response.get('checks', {})
            if not isinstance(llm_checks, dict): llm_checks = {}
            for key in guards.CHECKLIST:
                check = llm_checks.get(key)
                if check is None:
                    reasons.append('llm_check_missing:' + key)
                elif not isinstance(check, dict) or type(check.get('pass')) is not bool:
                    reasons.append('llm_check_invalid:' + key)
                elif check['pass'] is not True: reasons.append('llm_check_failed:' + key)
            checks.update({k: llm_checks[k] for k in guards.CHECKLIST if k in llm_checks})
            if not isinstance(response.get('reasons'), list) or any(not isinstance(r, str) for r in response['reasons']):
                reasons.append('llm_reasons_invalid')
            elif response['reasons']:
                # Do not persist untrusted provider prose in failure reports.
                reasons.append('llm_policy_fail')
            if response.get('verdict') != 'PASS': reasons.append('llm_verdict_fail')
        except guards.OpsLLMError as exc: reasons.append('llm_error:' + str(exc))
    # Reports contain only booleans/reason codes, never untrusted notes or copy.
    safe_checks = {k: {'pass': type(v) is dict and v.get('pass') is True} for k, v in checks.items()}
    return {'verdict': 'FAIL' if reasons else 'PASS', 'reasons': sorted(set(reasons)), 'checks': safe_checks}
