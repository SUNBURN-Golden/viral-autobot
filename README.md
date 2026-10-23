# Soulbound viral autobot

Python 3.9+ 표준 라이브러리만 사용하는 수집·검증·초안 도구입니다. SQLite 원장은
`data/autobot.db`, 초안은 `out/drafts/<project>/`에 저장합니다. 게시·댓글·DM·이메일·
로그인 기능은 없으며, 초안에는 항상 사람의 검토가 필요하다는 헤더가 붙습니다.

```sh
python3 autobot.py --help
python3 autobot.py run --help
python3 -m py_compile autobot.py daemon.py tests/test_offline.py
python3 -I tests/test_offline.py
```

`run`은 크롤링 → 발견 → 수요·자료 연결 → 초안 → 기존 지표 확인 → 상태 보고 순서로
실행합니다. `--project hanset`처럼 브랜드를 한정할 수 있습니다. `--no-crawl`,
`--no-discover`, `--no-demand`, `--no-drafts`, `--no-metrics`로 각 단계를 생략합니다.
수요 파이프라인 자체는 HTTP 요청을 하지 않습니다. 다음 명령은 기존 로컬 데이터로만
수요·자료를 연결하고 초안과 상태 파일을 만듭니다.

```sh
python3 autobot.py run --no-crawl --no-discover --no-metrics
```

`status`는 자료 만료를 로컬에서 적용한 뒤 기존 rounds.log 처리와 공개 지표 확인을
수행합니다. 상태 표의 열은 유지하고 표 아래 `demand:`에 질문·자료 상태별 개수를
표시합니다. Slack 전송은 `--post`를 명시하고 기존 webhook 환경 파일을 설정한
경우에만 수행합니다. 비밀 값은 출력하지 않습니다.

## 질문과 검증 자료 입력

기본 입력 위치는 `data/questions/<project>.json`, `data/evidence/<project>.json`입니다.
`config.json`의 `questions.dir`, `evidence.dir`로 변경할 수 있습니다. 상대 경로는
`autobot.py`가 있는 디렉터리를 기준으로 합니다. 프로젝트 키는 `hanset`, `goallit`,
`kitchengenome`, `pitchboard`, `agamajung`입니다. 아가마중의 도메인은 `agamajoong`이지만
기존 프로젝트 키는 `agamajung`을 유지합니다.

파일은 항목 배열 또는 아래 봉투 형식입니다. 최초에는 파일이 없어도 정상 동작합니다.
아래는 형식 예시이며 실제 사실이나 도착 URL을 제공하지 않습니다.

```json
{"project": "hanset", "evidence": []}
```

```json
{"project": "hanset", "questions": []}
```

자료 항목 필드:

| 필드 | 입력 규칙 |
| --- | --- |
| `id` | 재수입 시 같은 자료를 갱신하는 고유 키. 생략 시 프로젝트·출처·사실 해시 |
| `fact` | 사람이 확인한 사실, 빈 값 금지 |
| `source_url` | 실제 확인한 공개 HTTP(S) 출처 |
| `verified_at` | ISO 8601 확인 시각, 미래 시각 금지 |
| `valid_until` | ISO 8601 만료 시각, 확인 시각 이후여야 함 |
| `allowed_claims` | 사용할 수 있는 표현의 문자열 배열, 최소 한 개 |
| `forbidden_claims` | 사용할 수 없는 표현의 문자열 배열, 빈 배열 허용하나 필드 필수 |
| `keyword_cluster`, `keywords` | 해당 브랜드의 질문과 연결할 클러스터 키와 키워드 배열 |
| `kind` | `price`, `deadline`, `entry_condition`, `career`, `scene`, `comparison`, `counterexample` 등 |
| `project` | 선택 필드, 있으면 파일명·봉투의 브랜드와 일치해야 함 |

잘못된 날짜·출처, 허용/금지 표현 중복, 허용 표현의 낚시성 어휘는 거부합니다.
원문 `fact`는 검수용 근거에 남기고, 게시용 훅에는 `allowed_claims`만 사용합니다.
KitchenGenome의 `제자`·`수제자`·`protégé` 표현은 허용 주장에 명시된 경우에만 쓸 수
있습니다. 파일의 브랜드가 다르면 거부하고 다른 브랜드의 자료를 연결하지 않습니다.

수동 질문 항목은 `question`, `persona_decision`, `why_now`, `unique_evidence`,
`natural_channel`, `target_landing_url`을 채웁니다. `source`는 `manual`, `search_console`,
`reddit`, `rss`, `official` 중 하나입니다. `keyword_cluster`는 브랜드 설정의
`demand.clusters[].key`에 맞춥니다. `natural_channel`은 `reddit`, `threads`,
`instagram`, `x`, `search_page` 중 하나입니다. 도착 URL은 이미 크롤링된 살아 있는
동일 브랜드 심층 페이지여야 합니다. 홈 화면과 외부·다른 브랜드의 도착지는 거부합니다.
`unique_evidence`는 파이프라인에서 실제 연결된 허용 표현으로 다시 채웁니다.

`source_refs`에는 수집된 `candidates.id` 또는 공개 스레드 URL만 넣습니다.
임의 객체와 작성자 프로필은 저장하지 않으며, 제목 등의 `u/아이디`, `@아이디`, 이메일도
제거합니다. 검색 유입어는 실제 확인한 내보내기 자료만 `source: search_console`로
입력합니다. 검색량은 `search_volume_status`와 `search_volume_source`를 함께 제공한
경우만 유지하고, 출처가 없으면 반드시 `검색량 미확인`으로 남깁니다. 수치를 추정하지 않습니다.

## 보류·복구와 훅 비교

최근 공개 질문은 브랜드별 클러스터·언어로 묶고, 서로 다른 스레드 수·행동 의도·브랜드
적합성·독자 자료·시의성에 각각 0–3점을 매깁니다. 우선순위는 설정 가중합입니다.
오래된 스레드를 다시 수집해도 최근 수요로 세지 않습니다. 확인된 양수 검색량은 수요의
존재만 반영하고 추정 스레드 수나 검색량 등급으로 변환하지 않습니다.

- 질문에 검증 자료가 없으면 `needs_research / no_verified_evidence`로 남깁니다.
- 살아 있는 심층 도착지가 없으면 `needs_research / landing_missing`로 남깁니다.
- 수요 근거가 없는 질문과 자료는 `no_demand_hold`로 보류합니다.
- 연결할 자료를 추가하거나 실제 수요가 생기면 다음 실행에서 다시 연결·분류합니다.
- `valid_until < 현재 시각`이면 자료를 `expired`로 바꾸고 연결된 게시 전 초안과 예약
  원장을 `expired_hold`로 전환합니다. 감사 이벤트를 남기며 이미 확인된 게시는 유지합니다.
  남은 유효 자료가 없는 질문은 조사 대기로 돌아갑니다.

준비된 질문에는 `loss_prevention`, `unexpected_diff`, `discovery` 훅을 각각 저장합니다.
낚시성 어휘·브랜드 금지 표현·자료의 금지 표현을 검사하며, 차단 이유도 비교 섹션에
표시합니다. 허용 훅이 하나도 없으면 초안을 만들지 않습니다. 조사·수요 보류 질문에도
초안 파일과 DB 행을 만들지 않습니다. 기존 페이지 초안은 파일을 유지하되 질문 수요가
없으면 원장을 `no_demand_hold`로 둡니다.

질문 초안은 프로젝트당 실행당 기본 5개까지 우선순위 순으로 만듭니다
(`limits.max_question_drafts_per_run`). 메타데이터에는 질문의 대상·지금 답할 이유·채널
선택 이유·자료 ID·검색량 확인 상태·심층 도착지가 들어갑니다. 네 채널의 문안은 구분하고,
Reddit 문안에는 링크를 넣지 않습니다. 다른 채널의 링크는 `utm_content=<hook_type>`으로
선택한 훅을 구분합니다. 모든 문안은 게시 전 사실·유효기간·말투·플랫폼 규칙을 검토해야 합니다.

## 운영 규칙과 cron

공개 접근만 사용하고 robots.txt, 요청 간격, 사이트별 상한을 지킵니다. 개인정보·유료 API·
가짜 검색량·성과·방문기·브랜드 간 인기 연출을 금지합니다. 확인할 수 없는 지표는
`미확인`입니다. 승인 큐와 실제 게시 자동화는 이 묶음의 범위에 포함하지 않습니다.

`cron.txt`에 KST 기준 실행 예시가 있습니다. 이 도구는 crontab을 설치하지 않습니다.
운영자가 경로와 설정을 확인한 뒤 직접 적용합니다. 데몬 제어 명령은
`python3 autobot.py daemon {start,stop,restart,status,stages,loop}`입니다.
