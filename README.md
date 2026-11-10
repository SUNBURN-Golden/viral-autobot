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
선택 이유·자료 ID·검색량 확인 상태·심층 도착지가 들어갑니다. 검색 페이지는 질문·답·근거,
Instagram은 비교 카드·경력 지도·체크리스트, Threads는 의견·근거·질문 세 블록,
Reddit은 직접 답변과 홍보 허용·관계 표시 검토 항목으로 생성합니다. 기존 X 문안도 유지합니다.
각 채널 섹션에 `why_this_channel`, `why_now`, `evidence_ids`가 붙으며 Reddit 문안에는
링크를 넣지 않습니다. 모든 문안은 게시 전 사실·유효기간·말투·플랫폼 규칙을 검토해야 합니다.

## 승인 큐

큐 조회·갱신은 로컬 SQLite 원장만 바꾸며 실제 게시나 예약 요청을 보내지 않습니다.
`draft`는 여러 채널을 묶은 초안 패킷, `post`는 채널별 게시 원장입니다.
Python 헬퍼 `record_post(..., state="candidate", draft_id=초안_ID)`로 초안의 자료·귀속 정보를
물려받는 게시 후보를 등록할 수 있습니다. `queue_list()`로 같은 큐를 조회합니다.

```sh
python3 autobot.py queue list --project hanset --state candidate
python3 autobot.py queue move --entity draft --id 1 --to fact_checked --reason '근거 검수 완료'
python3 autobot.py queue move --entity draft --id 1 --to approved
python3 autobot.py queue move --entity draft --id 1 --to scheduled
python3 autobot.py queue verify --post-id p_EXAMPLE --url https://example.test/actual-post --verified-at 2026-10-24T09:00:00+09:00
```

전이는 `candidate → fact_checked → approved → scheduled → published_verified → measured → evaluated`
순서로만 가능합니다. 게시 전에는 `needs_research`, `no_demand_hold`, `expired_hold`로 보류할 수 있고
보류에서 복구하면 `candidate`부터 다시 검수합니다. 큐 명령은 만료된 연결 자료의 보류를 먼저
적용합니다. 모든 상태 변경은 `post_events`에 기록합니다. `post`의 게시 확인은 사람이 실제 URL을
확인한 뒤 `queue verify`로 기록해야 합니다. 최초 확인 시각부터 48h/7d 검사를 계산하며 재입력해도
시각·검사를 중복 생성하지 않습니다. 측정·평가 전이는 운영자가 명시적으로 진행합니다.

## 3층 성과와 귀속 입력

모든 새 초안·게시 원장에는 브랜드(`project`), `asset_id`, `keyword_cluster`, `hook_type`,
`campaign_id`가 붙습니다. 초안 패킷은 채널별 문안과 UTM을 포함하며 게시 원장은 개별 `channel`을
저장합니다. 질문 초안에서 등록한 게시 원장은 같은 `asset_id`를 유지합니다. 과거 기록의 알 수 없는
클러스터·훅 등은 `미확인`으로 둡니다. 링크에는 `utm_source=<channel>`, `utm_medium=social`
(검색 페이지는 `organic`), `utm_campaign=<campaign_id>`, `utm_content=<hook_type>`,
`utm_term=<keyword_cluster>`와 확장 필드 `utm_asset=<asset_id>`, `utm_brand=<brand>`를 연결합니다.

성과 원장 `metric_observations`는 기존 예약 검사 `metric_checks`와 별도로 보관합니다.
계정·사이트의 실제 확인된 집계값만 수동 입력하거나 JSON으로 가져옵니다. 로그인·유료 API를
호출하지 않습니다. 값이 없으면 `null`을 입력하며 0으로 추정하지 않습니다.

| 층 | `metric` 값 |
| --- | --- |
| 1: 공개 반응 | `views`, `likes`, `comments`, `score` |
| 2: 계정 반응 | `reach`, `shares`, `saves`, `watch`, `link_clicks` |
| 3: 사이트 행동 | `resource_views`, `comparisons`, `saves`, `signups`, `revisits`, `confirmed_purchases` |

```sh
python3 autobot.py metrics record --tier 2 --metric saves --value 7 --post-id p_EXAMPLE --attribution instagram --window 48h
python3 autobot.py metrics record --tier 3 --metric confirmed_purchases --value null --note 미확인
python3 autobot.py metrics import --project hanset
python3 autobot.py metrics summary --project hanset
```

기본 JSON 경로는 `data/metrics/<project>.json`이며 `metrics.dir`로 변경합니다. 배열 또는 아래
봉투 형식을 받습니다. 다음은 형식 설명용 가상 예시입니다.

```json
{
  "project": "hanset",
  "metrics": [
    {"post_id": "p_EXAMPLE", "tier": 2, "metric": "reach", "value": 120,
     "observed_at": "2026-10-24T09:00:00+09:00", "window": "48h"},
    {"tier": 3, "metric": "resource_views", "value": 5,
     "observed_at": "2026-10-24T09:00:00+09:00"}
  ]
}
```

JSON은 `observed_at`과 `value`가 필수입니다. 연결 순서는 `post_id`, `entity`(`draft`/`post`)+
`entity_id`, 동일 브랜드 `landing_url`의 UTM 순입니다. 알려진 게시 원장에는 브랜드·자산·채널·훅·
캠페인을 자동 연결하며 다른 브랜드의 원장·UTM·도착 URL은 거부합니다. 명시적인 `brand`나
식별자가 없는 행은 파일명만 보고 브랜드를 붙이지 않습니다. 유입 출처를 확인할 수 없으면
`직접 유입/출처 미확인`으로 기록합니다. 도착 도메인만으로 유입 채널을 추정하지 않습니다.
수동 CLI·헬퍼에서는 `attribution`을 명시하고, 누락하면 같은 미식별 표시를 사용합니다.

같은 관측값을 다시 가져오면 중복 저장하지 않습니다. 잘못된 항목은 거부 개수에 포함하고
나머지는 계속 입력합니다. `metrics import`는 거부 항목이 있으면 종료 코드 2를 반환합니다.
`run`의 metrics 단계와 `status`도 로컬 JSON을 가져옵니다. Reddit 공개 검사에서 실제로 얻은
score·댓글 수는 1층으로 연결하지만 HTTP 상태 코드만 얻은 경우에는 성과를 만들지 않습니다.
요약의 `count`, `known`, `sum`은 저장된 관측값의 집계이며, 여러 시점의 누적 스냅샷을 더한
값을 고유 이용자·매출 총계로 해석하면 안 됩니다. 상태 표 아래에는 층별 관측 개수와 미식별
개수를 표시합니다.

## 작은 실험과 운영 규칙 (Batch 4)

브랜드·변수별로 진행 중인 실험은 하나이며, 기본 기간은 14일, 단위 수는 최대 6개입니다.
각 단위의 값은 서로 달라야 합니다. `hook_type`은 기존 3종만 허용하므로 최대 3개 단위로
비교합니다. 나머지 변수는 `channel`, `landing_page`, `posting_window`입니다.
`posting_window` 값은 KST 0/6/12/18시 기준 `dawn`/`morning`/`afternoon`/`evening`입니다.
`leader`는 결과 JSON에서 사용하는 예약 단위 키입니다.

```sh
python3 autobot.py experiments create --project hanset --key hooks-01 --variable hook_type --hypothesis '발견 훅의 가입률이 높을 것이다' --unit control=loss_prevention --unit variant=discovery
python3 autobot.py experiments attach --id 1 --unit control --post-id p_CONTROL
python3 autobot.py experiments attach --id 1 --unit variant --post-id p_VARIANT
python3 autobot.py experiments list --project hanset --status running
python3 autobot.py experiments evaluate --id 1
python3 autobot.py rules promote --project hanset --key hook_type --value discovery --experiment 1
python3 autobot.py rules list --project hanset
python3 autobot.py rules check --project hanset --key hook_type
python3 autobot.py rules rollback --project hanset --key hook_type --reason '후속 성과 악화 확인'
```

게시 확인된 같은 브랜드의 게시물만 연결하며 게시 시각은 실험 기간 안이어야 합니다.
테스트 변수 이외의 훅·채널·정규화한 도착 URL·게시 시간대는 모두 확인되고 같아야 합니다.
기간 종료 후 모든 단위의 7d 자료 열람 분모가 기본 30 이상이어야 가입률로 비교합니다.
가입률 동률은 CTR로 비교하고, 그래도 동률이거나 지표가 누락되면 `판정 보류`입니다.
종료된 실험 결과는 고정됩니다. 새 검증은 새 실험으로 등록합니다.

`run`의 metrics 단계와 `status`는 완료된 48h/7d 검사에 대해 5분류 진단을 DB에 저장합니다.
`run --no-metrics`는 진단도 건너뜁니다. 진단은 노출 → 클릭 → 열람 → 가입 → 재방문 순서로
첫 실패 또는 표본 부족을 기록하며, 예상·실제 수치와 분모·가능한 다른 원인·다음 한 가지
변경을 남깁니다. 정상 게시물은 `pass`, 표본 부족이나 미측정은 `판정 보류`입니다.
같은 기간·지표의 최신 비어 있지 않은 스냅샷만 읽으며 기간이 없거나 `48h`/`7d`가 아닌
관측은 제외합니다. 상태 보고서에 진단별 개수와 제외한 관측 개수를 표시합니다.

규칙 승격에는 48h와 7d의 충분한 표본에서 같은 승자가 나와야 합니다. 또는
`--confirm-experiment ID`로 같은 브랜드·변수·승리 값을 가진 별도 종료 실험을 지정합니다.
근거는 DB에서 검증하며 보류 결과는 승격할 수 없습니다. 승격 시 이전 버전은 보존합니다.
이후 해당 값을 사용하고 승격 이후 게시 확인된 게시물의 7d 가입률을 합산해 점검하며,
최소 표본에서 기준 가입률보다 기본 20% 넘게 악화하면 자동 롤백합니다. 가입 수나 자료
열람 수가 미확인인 게시물은 이 합산에서 제외합니다. 과거 버전이 없으면 활성 규칙을
제거합니다. DB가 원본이며 `paths.rules`(기본 `data/rules.json`)는 임시 파일과 rename으로
갱신하는 브랜드별 JSON 사본입니다. `write_rules`로 DB에서 사본을 다시 생성할 수 있습니다.
설정은 `experiments`, `diagnosis`, `rules`에서 기간·상한·최소 표본·비율 기준을 조절합니다.

## 운영 규칙과 cron

공개 접근만 사용하고 robots.txt, 요청 간격, 사이트별 상한을 지킵니다. 개인정보·유료 API·
가짜 검색량·성과·방문기·브랜드 간 인기 연출을 금지합니다. 확인할 수 없는 지표는
`미확인`입니다. 실제 게시 자동화 기능은 없습니다.

`cron.txt`에 KST 기준 실행 예시가 있습니다. 이 도구는 crontab을 설치하지 않습니다.
운영자가 경로와 설정을 확인한 뒤 직접 적용합니다. 데몬 제어 명령은
`python3 autobot.py daemon {start,stop,restart,status,stages,loop}`입니다.

## Batch 5: DeepSeek 운용 CLI

`python3 autobot.py ops`는 HANSET 질문·evidence·기존 승인 큐·성과 진단을 연결합니다. Python 3 표준 라이브러리만 사용하며, 게시·브라우저 자동화·계정 전환을 실행하지 않습니다. 다른 브랜드는 설정 훅만 제공하며 `blocked`입니다. Kitchen Genome은 `hold=true`일 때 항상 차단됩니다. 기존 SOULBOUND 제외 규칙은 유지합니다.

라이브 문구 생성은 설치된 Claude CLI를 DeepSeek의 Anthropic-compatible endpoint `https://api.deepseek.com/anthropic`에 연결합니다. 인증은 환경변수 `DEEPSEEK_API_KEY`만 사용합니다. 키를 명령 인자·설정 파일에 넣지 마세요. 기본 모델은 `deepseek-flash`, 프로필은 격리된 `~/.claude-deepseek`입니다. `config.json`의 `ops.model`, `claude_bin`, `config_dir`로 조정합니다. 도구와 MCP를 비활성화하고 임시 프롬프트를 실행 후 삭제합니다. 프롬프트 템플릿은 `ops/prompts/*.md`에서 관리합니다.

| 동작 | `--dry-run` | `--mock-file PATH` | 기본 live |
| --- | --- | --- | --- |
| LLM | 결정적 로컬 응답 | JSON fixture 응답 | DeepSeek CLI |
| 외부 네트워크 | 없음 | 없음 | DeepSeek 및 명시 설정 시 X 공개 metrics GET |
| 비즈니스 원장 쓰기 | 없음 | 허용 | 허용 |
| `ops_runs` 감사 행 | 씀 | 씀 | 씀 |
| `logs/ops_dryrun.md` | append | append | 안 씀 |
| 계획 보고서 | 씀 | 씀 | 씀 |

`--config PATH`는 `ops` 앞에 둡니다. 키 없는 예시:

```sh
python3 autobot.py ops plan --project hanset --dry-run
python3 autobot.py ops draft --project hanset --dry-run --limit 1
python3 autobot.py ops compliance --ops-draft-id 1 --dry-run
python3 autobot.py ops schedule --ops-draft-id 1 --dry-run
python3 autobot.py ops publish-guide --ops-schedule-id 1 --dry-run
python3 autobot.py ops measure --project hanset --dry-run
```

실제 로컬 초안을 만들려면 dry-run을 제거합니다. `plan`은 rounds.log/status.md와 DB를 읽어 `out/ops/plan/`에 1페이지 계획을 씁니다(dry-run은 `out/ops/previews/<run_id>/plan.md`). `draft`는 ready/drafted 질문에 연결된 활성 evidence만 사용하며 X 가중 280자(URL=23, 한글·₩·이모지=2), Threads 500자, Instagram 카드 카피를 검사합니다. 숫자·가격·시간은 evidence corpus에 있어야 합니다. `--draft-id`로 기존 질문 앵커를 선택할 수 있습니다. `ops-draft-id`와 `ops-schedule-id`는 반복 가능합니다. 지정하지 않은 compliance/schedule은 최신 초안을 선택합니다. Instagram 실제 카드 파일은 사람이 준비하고 `ops_drafts.card_path`에 연결해야 가이드를 만들 수 있습니다.

`compliance`는 한국 표시광고법·공정위 추천·보증 지침·플랫폼 규칙·커뮤니티 홍보 금지를 C1–C15 체크리스트로 검사합니다. 모든 항목에 명시적 boolean `pass=true`가 필요하며 누락·불확실·코드 검사 실패는 FAIL입니다. FAIL이면 schedule/publish-guide를 거부합니다. PASS는 현재 copy와 card_copy의 composite hash에만 유효합니다. 법률·플랫폼 규칙의 최신성 및 증빙은 담당자가 검토하고 evidence와 템플릿을 갱신해야 합니다.

기존 `queue`에서 앵커를 `fact_checked` → `approved`로 승인한 뒤 schedule을 실행합니다. X는 `ops.accounts.hanset.x.confirmed=true`로 실제 `@hanset_mag` 확인을 기록해야 합니다. `@Crom_kr`·`@agamajung`은 대소문자와 관계없이 금지하며 어떤 계정도 로그아웃하지 않습니다. Threads/Instagram은 별도 X 확인 조건 없이 허용 alias를 검사합니다. 예약에는 `utm_campaign`과 `utm_content`를 붙이고 ops 초안 ID로 중복을 방지합니다.

`publish-guide`는 채널·계정·승인된 본문(`copy`)·추적 링크를 붙인 최종 문구(`delivery_copy`)·UTM·카드 경로·체크리스트가 있는 JSON/MD를 `out/ops/publish_guides/`에 씁니다. 최종 문구도 채널 길이 제한을 검사합니다. 사람이 직접 게시한 후:

```sh
python3 autobot.py queue verify <post_id> <공개_URL>
python3 autobot.py run --project hanset --no-crawl --no-discover --no-demand --no-drafts
python3 autobot.py metrics import --project hanset
python3 autobot.py ops measure --project hanset --post-id <post_id>
```

`--check-kind` 없는 기존 measure는 로컬 진단 경로를 유지합니다. Batch 6의 `--check-kind 48h|7d`는 아래의 수동 입력/선택적 X 공개 수집 경로를 사용합니다. `queue verify`로 공개 URL을 확인하고 해당 기간 지표 검사가 완료되어야 진단 대상입니다. measure는 로컬 rounds 로그·metrics import·기존 진단을 재사용하고 5분류 및 `판정 보류`를 유지한 문구 초안만 생성합니다. 실제 수치/분모의 숫자와 다음 단일 변경도 코드로 검사합니다.

오프라인 mock fixture는 명령별 응답 객체를 담습니다. 예: `{"plan":{"plan":"오늘의 계획"},"compliance":{"verdict":"PASS","reasons":[],"checks":{"C1":{"pass":true,"note":"..."}}}}`. 실제 PASS fixture에는 C1–C15 전체가 필요합니다. mock PASS도 기존 사람 승인과 계정 확인을 대신하지 않습니다. 비즈니스 행 검증에는 mock을 쓰고, dry-run은 감사 행·로그·계획 보고서 외에는 쓰지 않습니다.

빈 입력은 `skipped`/rc 0, 정책 차단·키 없음·LLM 오류는 `blocked`/rc 2, 예기치 못한 오류는 rc 1입니다. 비밀값과 외부 개인정보는 프롬프트·stdout·로그·감사 출력에서 정화합니다. 가짜 리뷰·부계정·댓글 알바·경쟁사 비방·체험단 미표기·저작권 이미지 무단 사용을 금지하며 광고비·유료 툴 결제·신규 계정 생성 경로는 없습니다. DeepSeek 추론은 설정된 API 사용량을 소비할 수 있습니다. daemon의 기존 PR 흐름을 유지하며 main 직접 push 경로를 추가하지 않습니다.

PR 제목: `feat(ops): DeepSeek-brain viral ops bot (plan/draft/compliance/schedule/measure)`

기존 오프라인 테스트와 현재 daemon 단계 설정의 불일치도 수정했습니다. 기본 단계 순서는 SPEC.md의 7단계 표와 맞추었고 DeepSeek 레인과 사용자 지정 단계 설정 지원은 유지합니다.

## Batch 6: 일일 패키지·공개 지표·다음 주 전략

`ops package`는 사실확인 패킷의 게시일·확인일·허용 사실·출처·금지 조건을 읽어 렌더 호환 패키지를 만듭니다. 게시·로그인·DM·카드 렌더는 수행하지 않습니다. Python 3 표준 라이브러리만 사용합니다.

```sh
python3 autobot.py ops package --packet /workspace/viral/w1/hanset-factcheck-1108.md --date 1108 --dry-run
python3 autobot.py ops package --packet /workspace/viral/w1/hanset-factcheck-1108.md --date 1108 --out-dir out/example-1108 --mock-file fixtures.json
python3 autobot.py ops package --packet /workspace/viral/w1/hanset-factcheck-1108.md --date 1108
```

PASS 산출물은 `post.json`, `x.txt`, `threads.txt`, `ig_caption.txt`, `reddit.md`, 빈 `cards/`입니다. 기본 위치는 `/workspace/viral/w1/daily/MMDD`이며 `ops.paths.daily_packages` 또는 `--out-dir`(최종 디렉터리)로 변경합니다. 확인일은 패킷의 `checked Nov N`에서 가져오며 게시일과 별도로 보존합니다. 패킷에는 명시 연도, 확인일, UTM content, 공식 출처(사실 섹션의 `출처:` 또는 헤더의 `확인:` 줄)와 HANSET 링크(`## 랜딩` 섹션 또는 본문 어디든)가 필요합니다. campaign만 생략하면 `hanset_daily_w2`를 사용합니다. `## 빼야 할 것`/`금지` 섹션과 `Do not mention X`·`X 언급 금지` 같은 지시문에서 금지 어휘를 추출하며, 환불은 브랜드 토큰이 아니라 `refund`/`환불` 클래스로 차단합니다. 허용 사실에 포함된 URL은 그 사실을 인용한 경우에만 통과합니다.

생성 문구는 원문 사실 조각을 선택·배치하는 방식입니다. `claim_refs`가 게시 JSON 경로와 fact ID를 연결하며, 숫자가 같아도 주차비를 참가비로 바꾸는 재서술은 차단합니다. 임의 문구나 새 숫자는 허용하지 않습니다. X는 최종 URL·footer 포함 가중 280, Threads는 URL 포함 500자 제한입니다. IG에는 프로필 링크 안내만 넣습니다. Reddit 파일의 게시에는 사람이 커뮤니티 규칙·승인을 확인해야 합니다.

cover는 렌더러에서 복사한 motif/palette 목록을 사용하며, `ops.paths.history_json`에서 같은 ID를 제외한 마지막 6개의 motif 또는 배경색이 겹치면 재시도합니다. history가 없거나 손상되면 차단합니다. history는 수정하지 않습니다. 길이·cover 실패만 최초 생성 이후 최대 `ops.package.max_retries`회(기본 3회) 재시도합니다. forbidden·근거·UTM·스키마 실패는 즉시 차단합니다. 모든 package와 기존 draft는 C1–C15 compliance PASS가 필요하며 `block_on_fail=false`로 우회할 수 없습니다. 자동 PASS는 사람 승인을 대신하지 않습니다.

PASS 파일은 같은 파일시스템의 고유 `.partial` 디렉터리에 작성한 후 rename합니다. 기존 최종 디렉터리가 있으면 `output_exists`로 중단하므로 별도 `--out-dir` 또는 dry-run을 사용하세요. 차단 시 최종 경로 대신 `<target>.<run_id>.<unique>.blocked/COMPLIANCE_FAIL.json`에 사유 코드·검사 결과·패킷 hash만 남깁니다. 패킷을 수정한 뒤 같은 명령을 다시 실행할 수 있습니다. 강제 종료로 남은 `.partial`은 공개 패키지가 아닙니다. 실행 중인 작업이 없는지 확인한 후 해당 잔존 디렉터리만 수동 삭제하세요.

실측은 `queue verify`된 공개 게시 기록 또는 게시 완료·공개 URL·시각이 명확한 rounds 기록을 대상으로 합니다.

```sh
python3 autobot.py ops measure --post-id p_example --check-kind 48h --dry-run
python3 autobot.py ops measure --post-id p_example --check-kind 7d
python3 autobot.py ops measure --check-kind 48h
```

`--post-id`를 생략하면 프로젝트의 기한 도래 게시물만 처리합니다. `--check-kind`를 생략하면 기존 로컬 진단 경로를 유지합니다. 수동 파일은 `ops.paths.manual_metrics`(기본 `data/metrics/manual`)의 `<post_id>_<48h|7d>.json`입니다. IG와 Threads는 이 파일만 읽으며, 파일이 없으면 모든 값이 `unverified`입니다.

```json
{
  "post_id": "p_example",
  "check_kind": "48h",
  "channel": "instagram",
  "public_url": "https://www.instagram.com/p/example/",
  "observed_at": "2026-11-10T19:10:00+09:00",
  "metrics": {
    "likes": 3,
    "replies": 0,
    "reposts": "unverified",
    "views": "unverified"
  }
}
```

키는 `likes`, `reposts`, `replies`, `views`만 허용합니다. IG 댓글도 `replies`로 입력하며 `comments` 별칭은 받지 않습니다. 값은 비음수 정수 또는 `unverified`; 실제 0은 관측으로 저장하고 미확인은 수치 관측 행을 만들지 않습니다. 게시물·프로젝트·채널·URL·window가 일치해야 하며, `observed_at`은 timezone을 포함하고 게시 및 측정 기한 이후, 현재 이하여야 합니다. 늦은 수집은 실제 시각을 보존합니다. 파일이 있지만 잘못되었으면 차단합니다.

X는 live 모드에서 `ops.measure.x_public=true`이고 기존 `X_BEARER_TOKEN`(변수명은 `x_bearer_env`로 변경 가능)이 있을 때만 고정 HTTPS API에 공개 metrics GET을 보냅니다. 기본값은 false입니다. 새 유료 서비스 가입·결제는 없습니다. API 오류·인증 실패·redirect·ID 불일치는 전체 미확인으로 기록합니다. private/organic/promoted insights는 사용하지 않습니다. dry-run/mock은 X 요청을 보내지 않습니다.

원본 snapshot은 `ops_metric_snapshots`에 먼저 저장하고 기존 `metric_observations`와 `ops_measure_notes.snapshot_id`로 연결합니다. 동일 수동 파일을 재수입해도 중복 snapshot을 만들지 않습니다. DeepSeek는 코드가 만든 raw metrics·분모 블록을 그대로 사용해야 하며, 어느 필드에서든 숫자를 만들거나 metric을 바꾸면 `measure_fabricated_number`로 차단합니다. 진단이 차단되어도 원본 snapshot은 보존합니다.

```sh
python3 autobot.py ops plan --mode next-week --dry-run
python3 autobot.py ops plan --mode next-week --mock-file fixtures.json
```

next-week는 strategies·rounds·status·날짜로 선택한 주간플랜·최근 snapshot·진단을 읽어 `ops.paths.strategy_next`(기본 `/workspace/viral/w1/strategy-next.md`)에 순위와 keep/kill/iterate를 씁니다. 미확인 또는 표본 부족은 `판정 보류`로 강제하며 KG hold 후보는 제외합니다. 성과 숫자는 참조 snapshot에서 코드가 출력합니다. 전략 원문과 게시 큐는 변경하지 않습니다. 기본 `ops plan`은 기존 일일 계획입니다.

`--dry-run`은 LLM·X 네트워크와 업무 테이블 변경 없이 `out/ops/previews/<run_id>/`에 새 package/plan 미리보기를 만듭니다. `ops_runs`·감사 로그는 기록합니다. `--mock-file`은 LLM만 대체하고 로컬 저장 흐름을 수행하므로 테스트 DB·입출력 경로를 별도로 설정하세요. 명령별 응답 배열은 재시도 순서로 소비되며 소진하면 차단합니다.

```json
{
  "package": [{"post": "replace with full package response"}, {"post": "replace with full retry response"}],
  "compliance": {"verdict": "PASS", "reasons": [], "checks": "replace with all C1–C15 boolean checks"},
  "plan_next": {"recommendations": []}
}
```

위 예시의 placeholder는 실제 스키마 객체로 교체해야 합니다. 단일 명령 객체도 기존 방식대로 사용할 수 있습니다. `ops/prompts/package.md`와 `plan_next.md`에 응답 계약이 있습니다. 설정에는 `ops.paths.{daily_packages,history_json,strategies,weekly_plan_glob,strategy_next,manual_metrics,cost_log}`, `ops.package.{max_retries,cover_lookback,default_campaign,max_context_chars}`, `ops.measure.{x_public,x_bearer_env}`, `ops.strategy.lookback_days`가 추가되었습니다. cover lookback은 6으로 고정합니다.

실제 DeepSeek 프로세스 호출 시도마다 `logs/ops_cost.jsonl`(경로 설정 가능)에 model·입출력 추정 token·latency·command만 한 줄 기록합니다. timeout·잘못된 응답도 기록하고 dry-run/mock은 비용 로그를 만들지 않습니다. API 키·프롬프트·응답 전문은 비용 로그에 넣지 않습니다.

검증: `python3 -m py_compile autobot.py daemon.py tests/test_offline.py` 및 `python3 -I tests/test_offline.py`.

PR 제목: `feat(ops): DeepSeek live package/measure/strategy upgrade`
