"""Stdlib-only configuration; deliberately independent of autobot."""
import copy
import os

OPS_DEFAULTS = {
    'enabled': True, 'model': 'deepseek-flash', 'claude_bin': '~/.local/bin/claude',
    'config_dir': '~/.claude-deepseek', 'base_url': 'https://api.deepseek.com/anthropic',
    'timeout_seconds': 180,
    'brands': {name: {'enabled': name == 'hanset', 'hold': name == 'kitchengenome'}
               for name in ('hanset', 'pitchboard', 'goallit', 'kitchengenome', 'agamajung')},
    'channels': ['x', 'threads', 'instagram'],
    'forbidden_accounts': ['Crom_kr', 'agamajung'],
    'accounts': {'hanset': {ch: {'alias': 'hanset_mag', 'confirmed': False}
                            for ch in ('x', 'threads', 'instagram')}},
    'account_rules': {'confirmation_required': True, 'logout_allowed': False},
    'limits': {'x_weighted': 280, 'url_weight': 23, 'wide_char_weight': 2,
               'threads_chars': 500, 'max_drafts_per_run': 5, 'max_evidence_per_prompt': 12,
               'max_context_chars': 12000, 'number_allowlist': []},
    'utm': {'campaign': 'viral_w1', 'content': 'hook_type', 'medium': 'social'},
    'paths': {'out': 'out/ops', 'dryrun_log': 'logs/ops_dryrun.md', 'prompts': 'ops/prompts',
              'daily_packages': '/workspace/viral/w1/daily',
              'history_json': '/workspace/hanset-insta-daily/history.json',
              'strategies': '/workspace/viral/w1/strategies.md',
              'weekly_plan_glob': '/workspace/viral/w1/hanset-weekly-plan-*.md',
              'strategy_next': '/workspace/viral/w1/strategy-next.md',
              'manual_metrics': 'data/metrics/manual', 'cost_log': 'logs/ops_cost.jsonl'},
    'package': {'max_context_chars': 32000, 'max_retries': 3, 'cover_lookback': 6, 'default_campaign': 'hanset_daily_w2'},
    'measure': {'x_public': False, 'x_bearer_env': 'X_BEARER_TOKEN'},
    'strategy': {'lookback_days': 14},
    'compliance': {'block_on_fail': True, 'version': '2026-11-05'},
    'cost': {'allow_paid_actions': False, 'allow_new_accounts': False},
    'publish': {'browser_automation': False, 'require_compliance_pass': True,
                'require_approved_draft': True},
}


def apply_ops_defaults(cfg):
    def merge(target, defaults):
        for key, value in defaults.items():
            if key not in target:
                target[key] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(target[key], dict):
                merge(target[key], value)
    merge(cfg.setdefault('ops', {}), OPS_DEFAULTS)
    if not cfg['ops']['utm']['campaign']:
        cfg['ops']['utm']['campaign'] = cfg.get('campaign', 'viral_w1')
    return cfg


def ops_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_path(value):
    value = os.path.expanduser(value)
    return os.path.abspath(value if os.path.isabs(value) else os.path.join(ops_root(), value))
