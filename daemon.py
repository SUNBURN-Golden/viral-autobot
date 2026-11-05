#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Soulbound viral-autobot 7-stage dev & blueprint-audit daemon.

Implements the 2026-11-03 12:55 KST 7-stage model and fallback order:
  1. 설계 (design):            Codex astra 6 high -> Deepseek flash -> GLM -> Swe2
  2. 설계리뷰 (design_review):  Deepseek flash -> Codex sol 6.1 high -> GLM -> Swe2
  3. 개발 (build):             Codex sol 6.1 high -> Deepseek flash -> GLM -> Swe2
  4. 개발리뷰 (build_review):   GLM -> Deepseek flash -> Codex sol 6.1 high -> Swe2
  5. 리뷰에 따른 수정 (review_fix): Deepseek flash -> Codex sol 6.1 high -> GLM -> Swe2
  6. 재리뷰 (re_review):        Devin (automatic) -> Deepseek flash -> Codex sol 6.1 high -> Swe2
  7. 10개 PR마다 감리 (audit):  Codex astra 6 high -> GLM -> Swe2

Audits every 10 merged PRs against BLUEPRINT.md and SPEC.md.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
HOME = os.path.expanduser("~")
CLAUDE_BIN = os.path.join(HOME, ".local/bin/claude")
CODEX_BIN = os.path.join(HOME, ".local/bin/codex")
DEVIN_BIN = os.path.join(HOME, ".local/bin/devin")
FABLE_CONFIG_DIR = "/home/box/.claude-fable"

OPUS_MODEL = "claude-opus-5-5"
SONNET_MODEL = "claude-sonnet-5-5"
FABLE_MODEL = "claude-fable-5-1"
CODEX_ASTRA = "gpt-6-astra"
CODEX_SOL = "gpt-6.1-sol"
DEVIN_MODEL = "swe-2"
DEVIN_AUTO = "automatic"
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_ANTHROPIC_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_CONFIG_DIR = os.path.join(HOME, ".claude-deepseek")
AUDIT_EVERY_PRS = 10
DEFAULT_REPO = "SUNBURN-Golden/viral-autobot"

# Tuple representation matching peer box daemons (kixloop / onjeonloop / capitalloop / commerceloop)
STAGES: Dict[str, List[Tuple[str, ...]]] = {
    "design": [("claude", OPUS_MODEL, "medium"), ("codex", CODEX_ASTRA, "high"), ("glm",), ("devin",)],
    "design_review": [("codex", CODEX_ASTRA, "high"), ("claude", OPUS_MODEL, "medium"), ("glm",), ("devin",)],
    "build": [("codex", CODEX_SOL, "high"), ("claude", SONNET_MODEL, "high"), ("glm",), ("devin",)],
    "build_review": [("glm",), ("claude", SONNET_MODEL, "medium"), ("codex", CODEX_SOL, "high"), ("devin",)],
    "review_fix": [("codex", CODEX_SOL, "high"), ("claude", SONNET_MODEL, "high"), ("glm",), ("devin",)],
    "re_review": [("glm",), ("codex", CODEX_SOL, "high"), ("claude", SONNET_MODEL, "medium"), ("devin",)],
    "audit": [("claude", FABLE_MODEL, "medium"), ("codex", CODEX_ASTRA, "high"), ("glm",), ("devin",)],
}

STAGE_ALIASES: Dict[str, str] = {
    "design": "design",
    "design-revise": "design",
    "design_revise": "design",
    "planner": "design",
    "design-review": "design_review",
    "design_review": "design_review",
    "build": "build",
    "build-review": "build_review",
    "build_review": "build_review",
    "review": "build_review",
    "review-fix": "review_fix",
    "review_fix": "review_fix",
    "fix": "review_fix",
    "re-review": "re_review",
    "re_review": "re_review",
    "rereview": "re_review",
    "audit": "audit",
}

WRITE_STAGES = {"build", "review_fix"}

LIMIT_RE = re.compile(
    r"rate.?limit|usage limit|session limit|weekly limit|opus limit|quota|"
    r"resource_exhausted|too many requests|limit reached|hit your [a-z ]*limit|"
    r"upgrade your plan|\b401\b|\b402\b|\b403\b|\b429\b|\b529\b|"
    r"not logged in|unauthori[sz]ed|billing|payment|insufficient|exceeded|"
    r"cannot use this model|model (?:is )?(?:not available|unavailable|not found)|"
    r"unknown model|invalid model|resets? at|try again",
    re.I,
)

VERDICT_RE = re.compile(r"(?:^|\n)\s*VERDICT:\s*(APPROVE|CHANGES)\b", re.I)

ROADMAP_BATCHES: List[Tuple[str, str, str]] = [
    (
        "01-batch1-config-and-reliability.md",
        "feat(batch-1): 설정·신뢰성 수리 및 상태 머신 골격",
        """# Batch 1: 설정·신뢰성 수리 (BLUEPRINT.md §8.1, §4, §5)

## 목표
`BLUEPRINT.md`의 첫 번째 묶음(Batch 1)을 구현하고 오프라인 테스트(`tests/test_offline.py`)로 검증한다.

## 필수 구현 항목
1. **`config.json` 수리**:
   - `agamajung` 프로젝트 URL을 `"https://agamajoong.soulbound.co.kr"`로 설정하고 `aliases.domains`에 `"agamajoong.soulbound.co.kr"` 추가, 기존 `blocker_note` 제거 (단, `make_cfg` 등에서 url=None 테스트 시 기존 `NO_PUBLIC_URL` 동작은 그대로 유지).
   - `goallit` 프로젝트의 한글 표기(`tagline_ko`, `aliases.names`)에서 `골릿`을 `꼴릿`으로 교정(`aliases.names`에는 하위 호환을 위해 `꼴릿`, `골릿` 모두 허용 가능하되 기본 표기는 `꼴릿`).
2. **실패 지표 재시도 및 페이지 재확인(만료/404 점검)**:
   - `metric_checks`: 일시적 오류(`checked=False`) 시 최대 3회까지 재시도하되, 3회 실패 시 성공(`done`)으로 위장하지 않고 `status='failed'`(또는 명시적 실패/미확인 사유)로 구분 기록.
   - 페이지 재확인(`reverify_pages` / 크롤링 시 기존 저장 페이지 점검): HTTP 404/410 또는 만료된 페이지를 감지해 `last_status='expired'` 또는 `'gone'`으로 표시하고 관련 예약/초안을 자동 보류(`expired_hold`).
3. **고유 `post_id` 기반 게시 기록 중복 방지 및 상태 머신 골격**:
   - 상태 머신 단계 상수 및 전이 검증 함수: `candidate -> fact_checked -> approved -> scheduled -> published_verified -> measured -> evaluated` (보류 상태: `needs_research`, `no_demand_hold`, `expired_hold`).
   - 게시 항목에 결정론적 `post_id`를 부여해 재시도·재파싱 시 중복 기록을 방지하고, 실제 게시 URL이 확인된 시점(`published_verified`)부터 48h/7d 측정 시각을 계산.
4. **테스트**:
   - `tests/test_offline.py`의 기존 테스트가 모두 통과해야 하며, Batch 1 신규 기능(아가마중 URL/꼴릿 표기, 실패 재시도 상태 분리, 페이지 404/만료 보류, `post_id` 멱등성 및 상태 머신 전이)에 대한 오프라인 단위 테스트를 추가한다.
""",
    ),
    (
        "02-batch2-demand-and-evidence.md",
        "feat(batch-2): 문제형 질문 후보·홍보 재료 창고·3종 훅 연결",
        """# Batch 2: 수요와 자료 연결 (BLUEPRINT.md §1, §2, §3, §4, §8.2)

## 목표
`BLUEPRINT.md`의 두 번째 묶음(Batch 2)을 구현하고 `tests/test_offline.py`로 검증한다.

## 필수 구현 항목
1. **문제형 질문 후보(Question Candidate) 스키마 및 수집/정규화**:
   - 필수 필드: `persona_decision`, `why_now`, `unique_evidence`, `natural_channel`, `target_landing_url`, `search_volume_status` (미확인 시 반드시 `'검색량 미확인'`, 임의 추정 금지), 우선순위 점수(수요·행동의도·브랜드 적합성·독자 자료·시의성).
   - 댓글 작성자 아이디 등 개인정보 저장 금지 유지.
2. **홍보 재료 창고(Evidence Warehouse) 테이블 및 검증기**:
   - 필수 메타데이터: `fact`, `source_url`, `verified_at`, `valid_until`, `allowed_claims`, `forbidden_claims`.
   - `valid_until` 만료 시 연결된 초안/예약 항목을 자동으로 `expired_hold`로 전환.
   - 질문 후보는 있으나 연결할 검증 자료가 없으면 억지 초안을 생성하지 않고 `needs_research` 상태로 분류.
   - 자료는 있으나 질문 수요 근거가 없으면 `no_demand_hold` 상태로 보류.
3. **강한 훅 3종(`loss_prevention`, `unexpected_diff`, `discovery`) 비교 생성기**:
   - 콘텐츠/질문 후보당 3가지 각도의 훅을 모두 생성해 비교 가능하게 저장.
   - 낚시성 금지 어휘(`충격`, `아무도 모르는`, `무조건` 등) 자동 필터링/차단.
   - 5개 브랜드(아가마중, Pitchboard, 꼴릿, KitchenGenome, HANSET) 도메인·말투·심층 도착 페이지(`target_landing_url`, 메인 화면 지양) 분리 규칙 반영.
4. **테스트**:
   - `tests/test_offline.py`에 질문 후보 검증, `검색량 미확인` 기본값, Evidence 만료 시 `expired_hold`, 자료 부재 시 `needs_research`, 3종 훅 생성 및 금지어 차단 테스트를 추가한다.
""",
    ),
    (
        "03-batch3-approval-and-metrics.md",
        "feat(batch-3): 채널별 맞춤 초안·승인 큐·3층 성과 측정",
        """# Batch 3: 승인 큐와 실제 성과 연결 (BLUEPRINT.md §5, §6, §8.3)

## 목표
`BLUEPRINT.md`의 세 번째 묶음(Batch 3)을 구현하고 `tests/test_offline.py`로 검증한다.

## 필수 구현 항목
1. **채널별 맞춤 포맷 분리 (4개 채널 복붙 금지)**:
   - `search_page`(장기 보존용 질문-답-근거), `instagram`(비교 카드·경력 지도·체크리스트 구조), `threads`(의견 하나 + 근거 하나 + 대화 질문 하나), `reddit`(질문 직접 답변, 홍보 허용/관계 표시 점검, 무관한 링크 살포 금지) 포맷을 서로 다른 구조로 생성.
   - 모든 게시 후보에 `why_this_channel`, `why_now`, `evidence_ids` 필수 포함.
2. **승인 큐(Approval Queue) 관리**:
   - `candidate -> fact_checked -> approved -> scheduled -> published_verified -> measured -> evaluated` 단계 전이 및 조회/갱신 함수(및 CLI 서브커맨드 또는 헬퍼) 제공.
3. **3층 성과 측정 구조 및 귀속(Attribution) 메타데이터**:
   - 모든 콘텐츠에 `brand`, `asset_id`, `keyword_cluster`, `hook_type`, `channel`, `campaign_id` 및 UTM 파라미터 연결.
   - 1층(외부 공개 반응: 조회·좋아요·댓글), 2층(계정 반응: 도달·공유·저장·시청·링크 클릭), 3층(사이트 행동: 자료 열람·비교·저장·가입·재방문·확인된 구매) 저장 구조 및 수동/JSON 인사이트 입력 기능 구현.
   - 출처 미식별 유입은 `'직접 유입/출처 미확인'`으로 정직하게 기록.
4. **테스트**:
   - `tests/test_offline.py`에 4개 채널 포맷 비동일성(복붙 방지), 필수 필드(`why_this_channel`, `why_now`, `evidence_ids`), 승인 큐 전이, 3층 성과 입력 및 `'직접 유입/출처 미확인'` 처리 테스트를 추가한다.
""",
    ),
    (
        "04-batch4-experiments-and-rules.md",
        "feat(batch-4): 단일 변수 실험·5분류 실패 진단·운영 규칙 갱신 루프",
        """# Batch 4: 작은 실험으로 운영 규칙 갱신 (BLUEPRINT.md §7, §8.4)

## 목표
`BLUEPRINT.md`의 네 번째 묶음(Batch 4)을 구현하고 `tests/test_offline.py`로 검증한다.

## 필수 구현 항목
1. **2주 6개 단위 단일 변수(훅 유형 등) 비교 실험 관리**:
   - 브랜드별 실험 묶음(최대 6개 단위, 단일 변경 변수 명시) 등록·추적 구조 구현.
2. **48h / 7d 실패 원인 5분류 진단기**:
   - 5분류: `low_impressions`(노출 부족), `low_ctr`(노출 대비 클릭 부족), `high_bounce`(클릭 후 즉시 이탈), `low_signup`(열람 후 미가입), `low_retention`(가입 후 미재방문).
   - 진단 기록 필수 포맷: `hypothesis`(예상) -> `actual_with_denominator`(실제 수치와 분모) -> `alternative_causes`(가능한 다른 원인) -> `next_single_change`(다음에 바꿀 한 가지).
3. **표본 부족 시 `판정 보류` 및 브랜드별 운영 규칙(`rules.json` / DB) 갱신·롤백 루프**:
   - 표본(분모)이 최소 기준 미달일 때는 규칙을 성급히 바꾸지 않고 `'판정 보류'`로 기록.
   - 재검증을 통과한 패턴만 브랜드별 운영 규칙으로 승격하고, 후속 성과 악화 시 이전 규칙 버전으로 안전하게 롤백하는 함수 구현.
4. **테스트**:
   - `tests/test_offline.py`에 6개 단위 단일 변수 실험 생성, 5분류 실패 진단 리포트 포맷, 표본 부족 시 `'판정 보류'`, 운영 규칙 갱신 및 롤백에 대한 오프라인 테스트를 추가한다.
""",
    ),
]


class LaneSkip(Exception):
    """Raised when a model lane is unavailable or rate-limited so the next backup runs."""


class AllLanesUnavailable(Exception):
    """Raised when all 4 lanes in a stage fail or hit usage limits."""

    def __init__(self, stage: str, reasons: List[str], reset_epoch: Optional[float] = None):
        super().__init__(f"all lanes unavailable for {stage}: {' | '.join(reasons)}")
        self.stage = stage
        self.reasons = reasons
        self.reset_epoch = reset_epoch


def now_kst() -> datetime.datetime:
    if KST is not None:
        return datetime.datetime.now(KST)
    return datetime.datetime.now()


def iso_kst(dt: Optional[datetime.datetime] = None) -> str:
    dt = dt or now_kst()
    return dt.isoformat(timespec="seconds")


def canonical_stage(stage: str) -> str:
    key = STAGE_ALIASES.get(stage.strip().lower())
    if not key:
        raise ValueError(f"unknown daemon stage: {stage!r}")
    return key


def is_stage_readonly(stage: str) -> bool:
    return canonical_stage(stage) not in WRITE_STAGES


def load_daemon_config(config_path: str = CONFIG_PATH) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return raw.get("daemon", {})


def get_stage_chain(stage: str, config_path: str = CONFIG_PATH) -> List[Dict[str, Any]]:
    canon = canonical_stage(stage)
    cfg = load_daemon_config(config_path)
    stages_cfg = cfg.get("stages", {})
    if canon in stages_cfg:
        return list(stages_cfg[canon])
    models_by_id = {
        OPUS_MODEL: "opus55",
        CODEX_ASTRA: "astra6",
        CODEX_SOL: "sol61",
        SONNET_MODEL: "sonnet",
        FABLE_MODEL: "fable",
    }
    chain: List[Dict[str, Any]] = []
    for tup in STAGES[canon]:
        prov = tup[0]
        if prov in ("claude", "codex"):
            chain.append({
                "model": models_by_id.get(tup[1], tup[1]),
                "provider": prov,
                "id": tup[1],
                "effort": tup[2],
            })
        elif prov == "glm":
            chain.append({"model": "glm", "provider": "glm"})
        elif prov == "deepseek":
            chain.append({"model": "dsflash", "provider": "deepseek", "id": DEEPSEEK_MODEL})
        elif prov == "devin" and len(tup) > 1 and tup[1] == DEVIN_AUTO:
            chain.append({"model": "devin_auto", "provider": "devin", "id": DEVIN_AUTO})
        else:
            chain.append({"model": "swe2", "provider": "devin", "id": DEVIN_MODEL})
    return chain


def lane_label(choice: Dict[str, Any]) -> str:
    prov = choice.get("provider", "")
    model = choice.get("model", "")
    effort = choice.get("effort")
    if prov == "claude":
        return f"Claude ({model} / {choice.get('id', model)}) {effort or 'medium'}"
    if prov == "codex":
        return f"Codex ({choice.get('id', model)}) {effort or 'high'}"
    if prov == "glm":
        return "GLM (Claude CLI default profile)"
    if prov == "deepseek":
        return f"Deepseek flash ({choice.get('id', DEEPSEEK_MODEL)})"
    if prov == "devin":
        return f"Devin ({choice.get('id', DEVIN_MODEL)})"
    return f"{prov}:{model}"


def parse_verdict(text: str) -> Optional[str]:
    matches = [m.group(1).upper() for m in VERDICT_RE.finditer(text or "")]
    if not matches:
        return None
    unique = set(matches)
    if len(unique) > 1:
        return None
    return matches[-1]


def is_limit_or_unavailable(rc: int, text: str, stderr: str = "") -> bool:
    combined = f"{text or ''}\n{stderr or ''}"
    if LIMIT_RE.search(combined):
        return True
    if rc != 0:
        return True
    if not (text or "").strip():
        return True
    return False


def parse_reset_epoch(text: str, now_ts: Optional[float] = None) -> Optional[float]:
    t = text or ""
    now_ts = now_ts if now_ts is not None else time.time()
    cand: Optional[float] = None

    m = re.search(r"\|\s*(\d{10})\b", t)
    if m:
        cand = float(m.group(1))

    if cand is None:
        m = re.search(
            r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?\s*(Z|[+-]\d{2}:?\d{2})?",
            t,
        )
        if m:
            y, mo, d, hh, mi = map(int, m.groups()[:5])
            tz_s = m.group(6)
            try:
                if not tz_s:
                    dt = datetime.datetime(y, mo, d, hh, mi, tzinfo=KST)
                elif tz_s == "Z":
                    dt = datetime.datetime(y, mo, d, hh, mi, tzinfo=datetime.timezone.utc)
                else:
                    sign = 1 if tz_s[0] == "+" else -1
                    off = datetime.timedelta(hours=int(tz_s[1:3]), minutes=int(tz_s[-2:])) * sign
                    dt = datetime.datetime(y, mo, d, hh, mi, tzinfo=datetime.timezone(off))
                cand = dt.timestamp()
            except Exception:
                cand = None

    if cand is None:
        m = re.search(
            r"(?:in|after)\s+((?:\d+\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b[\s,]*(?:and\s*)?)+)",
            t,
            re.I,
        )
        if m:
            sec = 0
            for n_s, unit in re.findall(r"(\d+)\s*([a-z]+)", m.group(1), re.I):
                u = unit.lower()[0]
                sec += int(n_s) * {"d": 86400, "h": 3600, "m": 60, "s": 1}.get(u, 0)
            if sec > 0:
                cand = now_ts + sec

    if cand is None or cand <= now_ts or cand > now_ts + 8 * 86400:
        return None
    return cand + 60.0


def build_lane_command(
    choice: Dict[str, Any],
    prompt_file: str,
    cwd: str,
    output_file: str,
    readonly: bool = True,
    base_env: Optional[Dict[str, str]] = None,
) -> Tuple[List[str], Dict[str, str]]:
    """Build exact CLI command and sanitized environment for a stage model choice."""
    raw_env = dict(base_env if base_env is not None else os.environ)
    for secret_key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        raw_env.pop(secret_key, None)
    raw_env["GH_PROMPT_DISABLED"] = "1"
    raw_env["GIT_TERMINAL_PROMPT"] = "0"

    provider = choice.get("provider")
    model_id = choice.get("id") or choice.get("model")
    effort = choice.get("effort")

    if provider == "claude":
        env = {
            k: v
            for k, v in raw_env.items()
            if not re.match(r"^(ANTHROPIC_|CLAUDE_CODE_|API_TIMEOUT_MS|CLAUDE_FABLE_LOGIN_CODE|CLAUDE_SETUP_CODE)", k)
        }
        env["CLAUDE_CONFIG_DIR"] = choice.get("config_dir") or FABLE_CONFIG_DIR
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        cmd = [
            CLAUDE_BIN,
            "-p",
            "--model",
            str(model_id),
            "--effort",
            str(effort or "medium"),
            "--settings",
            '{"fastMode":false}',
            "--output-format",
            "json",
        ]
        if readonly:
            cmd += [
                "--permission-mode",
                "default",
                "--allowedTools",
                "Read",
                "Grep",
                "Glob",
                "--disallowedTools",
                "Bash",
                "Edit",
                "Write",
                "MultiEdit",
                "NotebookEdit",
                "WebFetch",
                "WebSearch",
                "Task",
            ]
        else:
            cmd += [
                "--permission-mode",
                "bypassPermissions",
                "--disallowedTools",
                "WebFetch",
                "WebSearch",
            ]
        return cmd, env

    if provider == "codex":
        env = {k: v for k, v in raw_env.items() if k not in ("OPENAI_API_KEY", "CODEX_API_KEY", "SSH_AUTH_SOCK")}
        cmd = [
            CODEX_BIN,
            "exec",
            "-m",
            str(model_id),
            "-c",
            f'model_reasoning_effort="{effort or "high"}"',
        ]
        if not readonly:
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        cmd += [
            "-s",
            "read-only" if readonly else "workspace-write",
            "--skip-git-repo-check",
            "-C",
            cwd,
            "-o",
            output_file,
            "-",
        ]
        return cmd, env

    if provider == "glm":
        env = dict(raw_env)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        cmd = [
            CLAUDE_BIN,
            "-p",
            "--effort",
            "xhigh",
            "--output-format",
            "json",
        ]
        if readonly:
            cmd += [
                "--permission-mode",
                "default",
                "--allowedTools",
                "Read",
                "Grep",
                "Glob",
                "--disallowedTools",
                "Bash",
                "Edit",
                "Write",
                "MultiEdit",
                "NotebookEdit",
                "WebFetch",
                "WebSearch",
                "Task",
            ]
        else:
            cmd += ["--dangerously-skip-permissions"]
        return cmd, env

    if provider == "deepseek":
        key = raw_env.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise LaneSkip("DEEPSEEK_API_KEY not set")
        env = {k: v for k, v in raw_env.items() if not re.match(r"^(ANTHROPIC_|CLAUDE_CODE_)", k)}
        # separate config dir so the default (GLM) profile's settings.json env does not override the endpoint
        env["CLAUDE_CONFIG_DIR"] = choice.get("config_dir") or DEEPSEEK_CONFIG_DIR
        env["ANTHROPIC_BASE_URL"] = DEEPSEEK_ANTHROPIC_URL
        env["ANTHROPIC_AUTH_TOKEN"] = key
        env["ANTHROPIC_MODEL"] = str(model_id or DEEPSEEK_MODEL)
        env["ANTHROPIC_SMALL_FAST_MODEL"] = str(model_id or DEEPSEEK_MODEL)
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        cmd = [CLAUDE_BIN, "-p", "--model", str(model_id or DEEPSEEK_MODEL), "--output-format", "json"]
        if readonly:
            cmd += ["--permission-mode", "default", "--allowedTools", "Read", "Grep", "Glob",
                    "--disallowedTools", "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit",
                    "WebFetch", "WebSearch", "Task"]
        else:
            cmd += ["--dangerously-skip-permissions", "--disallowedTools", "WebFetch", "WebSearch"]
        return cmd, env

    if provider == "devin":
        env = dict(raw_env)
        cmd = [
            DEVIN_BIN,
            "--respect-workspace-trust",
            "false",
            "--model",
            str(model_id or DEVIN_MODEL),
            "--permission-mode",
            "auto" if readonly else "dangerous",
            "-p",
            "--prompt-file",
            prompt_file,
        ]
        return cmd, env

    raise ValueError(f"unsupported provider: {provider!r}")


def execute_lane_subprocess(
    choice: Dict[str, Any],
    prompt_text: str,
    cwd: str,
    log_prefix: str,
    readonly: bool = True,
    timeout: int = 3600,
) -> Tuple[int, str, str]:
    """Execute one model lane subprocess and return (returncode, output_text, stderr_text)."""
    os.makedirs(os.path.dirname(log_prefix) or ".", exist_ok=True)
    rule_suffix = (
        "\n\n[Supervisor rule] READ-ONLY session: use Read/Grep/Glob only; do not modify any files, "
        "do not run git commit/push or network calls."
        if readonly
        else "\n\n[Supervisor rule] Edit files in this worktree only. Never run git commit, git push, "
             "gh, or network calls; the daemon runs tests, commits, and opens PRs."
    )
    prompt_file = f"{log_prefix}.prompt.md"
    out_file = f"{log_prefix}.out"
    last_file = f"{log_prefix}.last.md"
    err_file = f"{log_prefix}.err"
    with open(prompt_file, "w", encoding="utf-8") as fh:
        fh.write(prompt_text + rule_suffix)

    provider = choice.get("provider")
    target_out = last_file if provider == "codex" else out_file
    cmd, env = build_lane_command(choice, prompt_file, cwd, target_out, readonly=readonly)

    try:
        with open(prompt_file, "r", encoding="utf-8") as stdin_fh, \
             open(out_file, "w", encoding="utf-8") as stdout_fh, \
             open(err_file, "w", encoding="utf-8") as stderr_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=stdin_fh if provider != "devin" else subprocess.DEVNULL,
                stdout=stdout_fh,
                stderr=stderr_fh,
                text=True,
                start_new_session=True,
            )
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                proc.wait()
                return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)

    raw_out = open(out_file, "r", encoding="utf-8", errors="replace").read() if os.path.exists(out_file) else ""
    raw_err = open(err_file, "r", encoding="utf-8", errors="replace").read() if os.path.exists(err_file) else ""

    if provider == "codex":
        last_txt = open(last_file, "r", encoding="utf-8", errors="replace").read() if os.path.exists(last_file) else ""
        return rc, (last_txt if last_txt.strip() else raw_out), (raw_err + "\n" + raw_out[-1500:])

    if provider in ("claude", "glm", "deepseek"):
        try:
            payload = json.loads(raw_out)
            if isinstance(payload, list):
                payload = next(
                    (x for x in reversed(payload) if isinstance(x, dict) and x.get("type") == "result"),
                    {},
                )
            text = payload.get("result") or ""
            models_used = ",".join((payload.get("modelUsage") or {}).keys())
            if payload.get("is_error"):
                rc = rc or 1
            if provider == "claude" and "glm" in models_used.lower():
                rc = 1
            return rc, text, raw_err
        except Exception:
            return (rc or 1), raw_out, raw_err

    return rc, raw_out, raw_err


def run_stage_chain(
    stage: str,
    prompt_text: str,
    cwd: str,
    log_dir: str,
    runner: Optional[Callable[[Dict[str, Any], str, str, str, bool], Tuple[int, str, str]]] = None,
    config_path: str = CONFIG_PATH,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Run a stage across its 4-step fallback chain until the first lane succeeds."""
    canon = canonical_stage(stage)
    readonly = is_stage_readonly(canon)
    chain = get_stage_chain(canon, config_path=config_path)
    exec_fn = runner or (
        lambda ch, pr, wd, lp, ro: execute_lane_subprocess(ch, pr, wd, lp, readonly=ro)
    )

    skipped: List[str] = []
    earliest_reset: Optional[float] = None

    for idx, choice in enumerate(chain):
        label = lane_label(choice)
        log_prefix = os.path.join(log_dir, f"{canon}_{idx}_{choice.get('model', 'lane')}")
        if log_fn:
            log_fn(f"[{canon}] trying lane {idx + 1}/{len(chain)}: {label}")
        rc, text, err = exec_fn(choice, prompt_text, cwd, log_prefix, readonly)
        if is_limit_or_unavailable(rc, text, err):
            probe = f"{text or ''}\n{err or ''}".strip()
            reset_ts = parse_reset_epoch(probe)
            if reset_ts is not None and (earliest_reset is None or reset_ts < earliest_reset):
                earliest_reset = reset_ts
            reason = f"{label} (rc={rc}: {probe[-160:] if probe else 'empty'})"
            skipped.append(reason)
            if log_fn:
                log_fn(f"[{canon}] lane skipped: {reason}")
            continue
        if log_fn:
            log_fn(f"[{canon}] lane succeeded: {label} ({len(text)} chars)")
        return {
            "stage": canon,
            "readonly": readonly,
            "choice": choice,
            "label": label,
            "index": idx,
            "skipped": skipped,
            "text": text,
        }

    raise AllLanesUnavailable(canon, skipped, reset_epoch=earliest_reset)


def should_run_audit(merged_prs: int, audited_through: int, audit_every: int = AUDIT_EVERY_PRS) -> bool:
    return (
        merged_prs > 0
        and audit_every > 0
        and merged_prs % audit_every == 0
        and audited_through < merged_prs
    )


def ensure_queue_dirs(queue_dir: str) -> Dict[str, str]:
    subdirs = {}
    for name in ("inbox", "running", "done", "failed", "audits", "logs", "worktrees"):
        path = os.path.join(queue_dir, name)
        os.makedirs(path, exist_ok=True)
        subdirs[name] = path
    return subdirs


def seed_roadmap_batches(queue_dir: str) -> List[str]:
    """Seed the 4 blueprint batches into inbox/ if they have not been queued or completed yet."""
    dirs = ensure_queue_dirs(queue_dir)
    existing = set()
    for state_dir in ("inbox", "running", "done", "failed"):
        existing.update(os.listdir(dirs[state_dir]))
    seeded: List[str] = []
    for filename, _title, body in ROADMAP_BATCHES:
        if filename not in existing:
            path = os.path.join(dirs["inbox"], filename)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            seeded.append(filename)
    return seeded


def load_daemon_status(queue_dir: str, config_path: str = CONFIG_PATH) -> Dict[str, Any]:
    status_path = os.path.join(queue_dir, "status.json")
    default_stages = {
        stage: [
            f"{c.get('model')}:{c.get('effort')}" if c.get("effort") else str(c.get("model"))
            for c in get_stage_chain(stage, config_path=config_path)
        ]
        for stage in (
            "design",
            "design_review",
            "build",
            "build_review",
            "review_fix",
            "re_review",
            "audit",
        )
    }
    base = {
        "version": 2,
        "updated_at": iso_kst(),
        "state": "idle",
        "pid": None,
        "active_task": None,
        "active_stage": None,
        "merged_prs": 0,
        "audited_through": 0,
        "audit_every_prs": AUDIT_EVERY_PRS,
        "blueprint": "BLUEPRINT.md",
        "stages": default_stages,
    }
    if os.path.exists(status_path):
        try:
            with open(status_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                base.update(saved)
                base["stages"] = default_stages
                base["audit_every_prs"] = AUDIT_EVERY_PRS
                base["blueprint"] = "BLUEPRINT.md"
        except Exception:
            pass
    return base


def save_daemon_status(queue_dir: str, status: Dict[str, Any]) -> None:
    os.makedirs(queue_dir, exist_ok=True)
    status["updated_at"] = iso_kst()
    status_path = os.path.join(queue_dir, "status.json")
    tmp_path = f"{status_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp_path, status_path)


def format_stages_table(config_path: str = CONFIG_PATH) -> str:
    titles = [
        ("design", "1. 설계 (design)"),
        ("design_review", "2. 설계리뷰 (design_review)"),
        ("build", "3. 개발 (build)"),
        ("build_review", "4. 개발리뷰 (build_review)"),
        ("review_fix", "5. 리뷰에 따른 수정 (review_fix)"),
        ("re_review", "6. 재리뷰 (re_review)"),
        ("audit", "7. 10개 PR마다 감리 (audit)"),
    ]
    lines = ["# viral-autobot 7-stage daemon fallback order (2026-10-23)", ""]
    for key, label in titles:
        chain = get_stage_chain(key, config_path=config_path)
        formatted = " -> ".join(
            f"{c['model']} ({c['effort']})" if c.get("effort") else c["model"]
            for c in chain
        )
        mode = "read-only" if is_stage_readonly(key) else "write"
        lines.append(f"- {label} [{mode}]: {formatted}")
    return "\n".join(lines)


def _sh(cmd: List[str], cwd: Optional[str] = None, timeout: int = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def _prepare_worktree(task_slug: str, worktrees_dir: str) -> Tuple[str, str]:
    branch = f"autobot/{task_slug}"
    wt_path = os.path.join(worktrees_dir, task_slug)
    _sh(["git", "-C", HERE, "fetch", "origin", "main"])
    if os.path.isdir(wt_path):
        _sh(["git", "-C", HERE, "worktree", "remove", "--force", wt_path])
        if os.path.isdir(wt_path):
            shutil.rmtree(wt_path, ignore_errors=True)
    _sh(["git", "-C", HERE, "worktree", "prune"])
    res = _sh(["git", "-C", HERE, "worktree", "add", "-B", branch, wt_path, "origin/main"])
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {res.stderr}")
    return wt_path, branch


def _cleanup_runtime_artifacts(wt_path: str) -> None:
    for rel in ("data", "logs", "out", "__pycache__", "logs_claude.txt"):
        target = os.path.join(wt_path, rel)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.isfile(target):
            try:
                os.remove(target)
            except OSError:
                pass
    tests_cache = os.path.join(wt_path, "tests", "__pycache__")
    if os.path.isdir(tests_cache):
        shutil.rmtree(tests_cache, ignore_errors=True)


def _run_wt_tests(wt_path: str) -> Tuple[bool, str]:
    _cleanup_runtime_artifacts(wt_path)
    c1 = _sh([sys.executable, "-m", "py_compile", "autobot.py", "daemon.py", "tests/test_offline.py"], cwd=wt_path)
    if c1.returncode != 0:
        return False, f"py_compile failed:\n{c1.stdout}\n{c1.stderr}"
    c2 = _sh([sys.executable, "-I", "tests/test_offline.py"], cwd=wt_path, timeout=180)
    _cleanup_runtime_artifacts(wt_path)
    if c2.returncode != 0:
        return False, f"test_offline.py failed:\n{c2.stdout[-3000:]}\n{c2.stderr[-2000:]}"
    return True, c2.stdout[-1000:]


def _head_guard_before(wt_path: str) -> str:
    return _sh(["git", "-C", wt_path, "rev-parse", "HEAD"]).stdout.strip()


def _head_guard_after(wt_path: str, head0: str) -> None:
    cur = _sh(["git", "-C", wt_path, "rev-parse", "HEAD"]).stdout.strip()
    if head0 and cur and cur != head0:
        _sh(["git", "-C", wt_path, "reset", "--soft", head0])


def process_task(
    task_filename: str,
    dirs: Dict[str, str],
    queue_dir: str,
    config_path: str,
    log_fn: Callable[[str], None],
) -> bool:
    task_slug = os.path.splitext(task_filename)[0]
    inbox_path = os.path.join(dirs["inbox"], task_filename)
    running_path = os.path.join(dirs["running"], task_filename)
    done_path = os.path.join(dirs["done"], task_filename)
    failed_path = os.path.join(dirs["failed"], task_filename)
    task_log_dir = os.path.join(dirs["logs"], task_slug)
    os.makedirs(task_log_dir, exist_ok=True)

    os.replace(inbox_path, running_path)
    task_body = open(running_path, "r", encoding="utf-8").read()

    commit_title = f"feat({task_slug}): implement {task_slug}"
    for fname, title, _ in ROADMAP_BATCHES:
        if fname == task_filename:
            commit_title = title
            break

    def _set_stage(stage_name: str) -> None:
        st = load_daemon_status(queue_dir, config_path=config_path)
        st["state"] = "running"
        st["active_task"] = task_slug
        st["active_stage"] = stage_name
        save_daemon_status(queue_dir, st)

    try:
        wt_path, branch = _prepare_worktree(task_slug, dirs["worktrees"])
        log_fn(f"[{task_slug}] worktree ready at {wt_path} on branch {branch}")

        # 1. Stage 1: 설계 (design)
        _set_stage("design")
        design_prompt = (
            f"You are executing Stage 1 (설계 / design) for SUNBURN-Golden/viral-autobot.\n"
            f"Read BLUEPRINT.md, SPEC.md, config.json, autobot.py, and tests/test_offline.py.\n\n"
            f"Task specification:\n{task_body}\n\n"
            f"Produce a concise, concrete technical implementation plan covering exact functions, "
            f"SQLite tables/columns, config keys, and unit tests in tests/test_offline.py."
        )
        design_res = run_stage_chain("design", design_prompt, wt_path, task_log_dir, config_path=config_path, log_fn=log_fn)
        design_doc = design_res["text"]

        # 2. Stage 2: 설계리뷰 (design_review)
        _set_stage("design_review")
        drev_prompt = (
            f"You are executing Stage 2 (설계리뷰 / design_review) for SUNBURN-Golden/viral-autobot.\n"
            f"Check whether the proposed design satisfies BLUEPRINT.md, SPEC.md, and the task specification.\n\n"
            f"Task:\n{task_body}\n\nProposed Design:\n{design_doc}\n\n"
            f"End your response with a standalone line: VERDICT: APPROVE or VERDICT: CHANGES"
        )
        drev_res = run_stage_chain("design_review", drev_prompt, wt_path, task_log_dir, config_path=config_path, log_fn=log_fn)
        if parse_verdict(drev_res["text"]) == "CHANGES":
            _set_stage("design_revise")
            rev_prompt = (
                f"Revise the technical design to address every point in the design review.\n\n"
                f"Task:\n{task_body}\n\nOriginal Design:\n{design_doc}\n\n"
                f"Design Review Feedback:\n{drev_res['text']}"
            )
            design_res = run_stage_chain("design", rev_prompt, wt_path, task_log_dir, config_path=config_path, log_fn=log_fn)
            design_doc = design_res["text"]

        # 3. Stage 3: 개발 (build)
        _set_stage("build")
        head0 = _head_guard_before(wt_path)
        build_prompt = (
            f"You are executing Stage 3 (개발 / build) in worktree {wt_path}.\n"
            f"Implement the task according to BLUEPRINT.md, SPEC.md, and the approved design.\n"
            f"Keep Python 3 stdlib-only compatibility, preserve all existing tests in tests/test_offline.py, "
            f"add comprehensive offline tests for the new functionality, and run:\n"
            f"  python3 -m py_compile autobot.py daemon.py tests/test_offline.py\n"
            f"  python3 -I tests/test_offline.py\n\n"
            f"Task:\n{task_body}\n\nApproved Design:\n{design_doc}"
        )
        run_stage_chain("build", build_prompt, wt_path, task_log_dir, config_path=config_path, log_fn=log_fn)
        _head_guard_after(wt_path, head0)
        _cleanup_runtime_artifacts(wt_path)

        # 4. Stage 4: 개발리뷰 (build_review)
        _set_stage("build_review")
        diff_txt = _sh(["git", "-C", wt_path, "diff"], timeout=60).stdout[-25000:]
        tests_ok, test_out = _run_wt_tests(wt_path)
        brev_prompt = (
            f"You are executing Stage 4 (개발리뷰 / build_review) for SUNBURN-Golden/viral-autobot.\n"
            f"Review the implementation diff and test output against BLUEPRINT.md and the task requirements.\n\n"
            f"Task:\n{task_body}\n\nLocal test status: {'PASS' if tests_ok else 'FAIL'}\n{test_out}\n\n"
            f"Git diff:\n{diff_txt}\n\n"
            f"End your response with a standalone line: VERDICT: APPROVE or VERDICT: CHANGES"
        )
        brev_res = run_stage_chain("build_review", brev_prompt, wt_path, task_log_dir, config_path=config_path, log_fn=log_fn)
        verdict = parse_verdict(brev_res["text"])
        if not tests_ok:
            verdict = "CHANGES"
        last_review_text = brev_res["text"]

        # 5 & 6. Stage 5 (review_fix) & Stage 6 (re_review) loop (up to 2 rounds)
        fix_round = 0
        while (verdict != "APPROVE" or not tests_ok) and fix_round < 2:
            fix_round += 1
            _set_stage(f"review_fix_r{fix_round}")
            head0 = _head_guard_before(wt_path)
            fix_prompt = (
                f"You are executing Stage 5 (리뷰에 따른 수정 / review_fix, round {fix_round}).\n"
                f"Fix all issues identified in the review and ensure `python3 -I tests/test_offline.py` passes.\n\n"
                f"Task:\n{task_body}\n\nReview feedback:\n{last_review_text}\n\n"
                f"Current test output:\n{test_out}"
            )
            run_stage_chain("review_fix", fix_prompt, wt_path, task_log_dir, config_path=config_path, log_fn=log_fn)
            _head_guard_after(wt_path, head0)
            tests_ok, test_out = _run_wt_tests(wt_path)

            _set_stage(f"re_review_r{fix_round}")
            diff_txt = _sh(["git", "-C", wt_path, "diff"], timeout=60).stdout[-25000:]
            rerev_prompt = (
                f"You are executing Stage 6 (재리뷰 / re_review, round {fix_round}).\n"
                f"Verify whether all blocking review findings and tests are resolved.\n\n"
                f"Task:\n{task_body}\n\nLocal test status: {'PASS' if tests_ok else 'FAIL'}\n{test_out}\n\n"
                f"Git diff:\n{diff_txt}\n\n"
                f"End your response with a standalone line: VERDICT: APPROVE or VERDICT: CHANGES"
            )
            rerev_res = run_stage_chain("re_review", rerev_prompt, wt_path, task_log_dir, config_path=config_path, log_fn=log_fn)
            last_review_text = rerev_res["text"]
            verdict = parse_verdict(last_review_text) or ("APPROVE" if tests_ok else "CHANGES")

        tests_ok, test_out = _run_wt_tests(wt_path)
        if not tests_ok:
            raise RuntimeError(f"offline tests failed after review/fix rounds:\n{test_out}")

        # Commit, push branch, create PR, merge PR, sync main
        _set_stage("pr_merge")
        _cleanup_runtime_artifacts(wt_path)
        _sh(["git", "-C", wt_path, "add", "-A"])
        if _sh(["git", "-C", wt_path, "diff", "--cached", "--quiet"]).returncode == 0:
            raise RuntimeError("no file changes produced by build")

        c_res = _sh(["git", "-C", wt_path, "commit", "-m", commit_title])
        if c_res.returncode != 0:
            raise RuntimeError(f"git commit failed: {c_res.stderr}")

        p_res = _sh(["git", "-C", wt_path, "push", "-u", "origin", f"HEAD:refs/heads/{branch}"])
        if p_res.returncode != 0:
            raise RuntimeError(f"git push failed: {p_res.stderr}")

        cfg = load_daemon_config(config_path)
        repo = cfg.get("repo", DEFAULT_REPO)
        pr_body = (
            f"Automated 7-stage daemon PR for `{task_slug}`.\n\n"
            f"- Blueprint: `BLUEPRINT.md`\n"
            f"- Offline tests (`python3 -I tests/test_offline.py`): PASS\n"
        )
        pr_create = _sh([
            "gh", "pr", "create",
            "--repo", repo,
            "--base", "main",
            "--head", branch,
            "--title", commit_title,
            "--body", pr_body,
        ])
        pr_url = pr_create.stdout.strip().splitlines()[-1] if pr_create.stdout.strip() else ""
        log_fn(f"[{task_slug}] PR opened: {pr_url}")

        pr_merge = _sh([
            "gh", "pr", "merge", branch,
            "--repo", repo,
            "--squash",
            "--delete-branch",
        ])
        if pr_merge.returncode != 0:
            raise RuntimeError(f"gh pr merge failed: {pr_merge.stderr}")
        log_fn(f"[{task_slug}] PR merged: {pr_url}")

        _sh(["git", "-C", HERE, "pull", "--ff-only", "origin", "main"])

        st = load_daemon_status(queue_dir, config_path=config_path)
        st["merged_prs"] = int(st.get("merged_prs", 0)) + 1
        st["last_merged_pr"] = pr_url
        st["last_completed_task"] = task_slug
        st["active_task"] = None
        st["active_stage"] = None
        st["state"] = "idle"
        save_daemon_status(queue_dir, st)

        os.replace(running_path, done_path)

        # 7. Stage 7: 10개 PR마다 감리 (audit every 10 PRs vs BLUEPRINT.md)
        if should_run_audit(st["merged_prs"], int(st.get("audited_through", 0)), AUDIT_EVERY_PRS):
            maybe_run_blueprint_audit(dirs, queue_dir, config_path, log_fn)

        return True
    except Exception as exc:
        log_fn(f"[{task_slug}] FAILED: {exc}")
        if os.path.exists(running_path):
            os.replace(running_path, failed_path)
        st = load_daemon_status(queue_dir, config_path=config_path)
        st["state"] = "idle"
        st["active_task"] = None
        st["active_stage"] = None
        st["last_error"] = f"{task_slug}: {str(exc)[:400]}"
        save_daemon_status(queue_dir, st)
        return False


def maybe_run_blueprint_audit(
    dirs: Dict[str, str],
    queue_dir: str,
    config_path: str,
    log_fn: Callable[[str], None],
) -> Optional[str]:
    st = load_daemon_status(queue_dir, config_path=config_path)
    merged_prs = int(st.get("merged_prs", 0))
    audited_through = int(st.get("audited_through", 0))
    if not should_run_audit(merged_prs, audited_through, AUDIT_EVERY_PRS):
        return None

    st["state"] = "auditing"
    st["active_stage"] = "audit"
    save_daemon_status(queue_dir, st)

    audit_prompt = (
        "You are executing Stage 7 (10개 PR마다 감리 / blueprint audit) for SUNBURN-Golden/viral-autobot.\n"
        "Compare the current repository implementation (autobot.py, daemon.py, config.json, tests/test_offline.py) "
        "against the original blueprint in BLUEPRINT.md and SPEC.md.\n"
        "List any drift, missing requirements, or regressions, and end with VERDICT: APPROVE or VERDICT: CHANGES."
    )
    audit_log_dir = os.path.join(dirs["logs"], f"audit_pr_{merged_prs}")
    os.makedirs(audit_log_dir, exist_ok=True)
    res = run_stage_chain("audit", audit_prompt, HERE, audit_log_dir, config_path=config_path, log_fn=log_fn)
    report_path = os.path.join(dirs["audits"], f"audit-pr-{merged_prs}.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(res["text"])

    st = load_daemon_status(queue_dir, config_path=config_path)
    st["audited_through"] = merged_prs
    st["state"] = "idle"
    st["active_stage"] = None
    save_daemon_status(queue_dir, st)
    log_fn(f"[audit] completed blueprint audit at PR count {merged_prs} -> {report_path}")
    return report_path


def _paths_from_config(config_path: str = CONFIG_PATH) -> Tuple[str, str, str, str, int]:
    cfg = load_daemon_config(config_path)
    queue_dir = os.path.join(HERE, cfg.get("queue_dir", "data/dev-queue"))
    pid_file = os.path.join(HERE, cfg.get("pid_file", "logs/daemon.pid"))
    lock_file = os.path.join(HERE, cfg.get("lock_file", "logs/daemon.lock"))
    log_file = os.path.join(HERE, cfg.get("log_file", "logs/daemon.log"))
    poll_seconds = int(cfg.get("poll_seconds", 15))
    return queue_dir, pid_file, lock_file, log_file, poll_seconds


def _read_running_pid(pid_file: str) -> Optional[int]:
    if not os.path.exists(pid_file):
        return None
    try:
        pid = int(open(pid_file, "r", encoding="utf-8").read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def run_daemon_loop(config_path: str = CONFIG_PATH) -> int:
    queue_dir, pid_file, lock_file, log_file, poll_seconds = _paths_from_config(config_path)
    os.makedirs(os.path.dirname(pid_file) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(lock_file) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    dirs = ensure_queue_dirs(queue_dir)

    lock_fh = open(lock_file, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("viral-autobot daemon already holds lock", file=sys.stderr)
        return 0

    pid = os.getpid()
    with open(pid_file, "w", encoding="utf-8") as fh:
        fh.write(f"{pid}\n")

    stop_requested = {"flag": False}

    def _handle_sig(signum, _frame):
        stop_requested["flag"] = True

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    def _log(msg: str) -> None:
        line = f"{iso_kst()} [daemon] {msg}\n"
        with open(log_file, "a", encoding="utf-8") as lfh:
            lfh.write(line)
            lfh.flush()

    # Recover any interrupted task in running/ back to inbox/ on fresh daemon start
    for leftover in sorted(os.listdir(dirs["running"])):
        if leftover.endswith(".md"):
            os.replace(
                os.path.join(dirs["running"], leftover),
                os.path.join(dirs["inbox"], leftover),
            )

    seeded = seed_roadmap_batches(queue_dir)
    status = load_daemon_status(queue_dir, config_path=config_path)
    status["pid"] = pid
    status["state"] = "idle"
    status["started_at"] = iso_kst()
    save_daemon_status(queue_dir, status)

    _log(f"started pid={pid} (seeded={seeded})")
    with open(log_file, "a", encoding="utf-8") as lfh:
        lfh.write(format_stages_table(config_path=config_path) + "\n")
        lfh.flush()

    try:
        while not stop_requested["flag"]:
            inbox_tasks = sorted(
                f for f in os.listdir(dirs["inbox"])
                if f.endswith(".md") and not f.startswith(".")
            )
            status = load_daemon_status(queue_dir, config_path=config_path)
            status["pid"] = pid
            status["inbox_count"] = len(inbox_tasks)
            save_daemon_status(queue_dir, status)

            if inbox_tasks:
                next_task = inbox_tasks[0]
                _log(f"picking up task {next_task}")
                try:
                    process_task(next_task, dirs, queue_dir, config_path, _log)
                except AllLanesUnavailable as exc:
                    wait_s = max(60, int((exc.reset_epoch or (time.time() + 600)) - time.time()))
                    _log(f"all lanes unavailable in {exc.stage}; sleeping {wait_s}s before retry")
                    for _ in range(min(wait_s, 600)):
                        if stop_requested["flag"]:
                            break
                        time.sleep(1)
                continue

            status = load_daemon_status(queue_dir, config_path=config_path)
            status["pid"] = pid
            status["state"] = "idle"
            status["active_task"] = None
            status["active_stage"] = None
            save_daemon_status(queue_dir, status)

            for _ in range(max(1, poll_seconds)):
                if stop_requested["flag"]:
                    break
                time.sleep(1)
    finally:
        status = load_daemon_status(queue_dir, config_path=config_path)
        status["pid"] = None
        status["state"] = "stopped"
        save_daemon_status(queue_dir, status)
        try:
            if os.path.exists(pid_file) and _read_running_pid(pid_file) in (None, pid):
                os.remove(pid_file)
        except Exception:
            pass

    return 0


def start_daemon(config_path: str = CONFIG_PATH) -> int:
    queue_dir, pid_file, _lock_file, log_file, _poll = _paths_from_config(config_path)
    os.makedirs(os.path.dirname(pid_file) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    ensure_queue_dirs(queue_dir)
    seed_roadmap_batches(queue_dir)

    running = _read_running_pid(pid_file)
    if running is not None:
        print(f"viral-autobot daemon already running (PID {running})")
        return 0

    with open(log_file, "a", encoding="utf-8") as out_fh:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--config", config_path, "loop"],
            cwd=HERE,
            stdin=subprocess.DEVNULL,
            stdout=out_fh,
            stderr=out_fh,
            start_new_session=True,
        )
    time.sleep(0.4)
    print(f"Started viral-autobot daemon (PID {proc.pid})")
    return 0


def stop_daemon(config_path: str = CONFIG_PATH) -> int:
    queue_dir, pid_file, _lock_file, _log_file, _poll = _paths_from_config(config_path)
    running = _read_running_pid(pid_file)
    if running is None:
        print("viral-autobot daemon is not running")
        return 0
    try:
        os.kill(running, signal.SIGTERM)
        for _ in range(20):
            if _read_running_pid(pid_file) is None:
                break
            time.sleep(0.1)
    except ProcessLookupError:
        pass
    if os.path.exists(pid_file):
        try:
            os.remove(pid_file)
        except OSError:
            pass
    status = load_daemon_status(queue_dir, config_path=config_path)
    status["pid"] = None
    status["state"] = "stopped"
    save_daemon_status(queue_dir, status)
    print(f"Stopped viral-autobot daemon (PID {running})")
    return 0


def status_daemon(config_path: str = CONFIG_PATH) -> int:
    queue_dir, pid_file, _lock_file, _log_file, _poll = _paths_from_config(config_path)
    running = _read_running_pid(pid_file)
    status = load_daemon_status(queue_dir, config_path=config_path)
    state_str = f"RUNNING (PID {running}, state={status.get('state')}, task={status.get('active_task')}, stage={status.get('active_stage')})" if running else "STOPPED"
    print(f"viral-autobot daemon: {state_str}")
    print(format_stages_table(config_path=config_path))
    print(
        f"merged_prs={status.get('merged_prs', 0)} "
        f"audited_through={status.get('audited_through', 0)} "
        f"audit_every_prs={status.get('audit_every_prs', AUDIT_EVERY_PRS)} "
        f"blueprint={status.get('blueprint', 'BLUEPRINT.md')}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daemon.py",
        description="Soulbound viral-autobot 7-stage dev & audit daemon",
    )
    parser.add_argument("--config", default=CONFIG_PATH, help="path to config.json")
    sub = parser.add_subparsers(dest="action")
    for name in ("start", "stop", "restart", "status", "stages", "seed", "loop"):
        sub.add_parser(name)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    action = args.action or "status"
    if action == "start":
        return start_daemon(args.config)
    if action == "stop":
        return stop_daemon(args.config)
    if action == "restart":
        stop_daemon(args.config)
        return start_daemon(args.config)
    if action == "status":
        return status_daemon(args.config)
    if action == "stages":
        print(format_stages_table(args.config))
        return 0
    if action == "seed":
        queue_dir, *_ = _paths_from_config(args.config)
        seeded = seed_roadmap_batches(queue_dir)
        print(f"Seeded roadmap batches: {seeded}")
        return 0
    if action == "loop":
        return run_daemon_loop(args.config)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
