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

## 운영 규칙과 cron

공개 접근만 사용하고 robots.txt, 요청 간격, 사이트별 상한을 지킵니다. 개인정보·유료 API·
가짜 검색량·성과·방문기·브랜드 간 인기 연출을 금지합니다. 확인할 수 없는 지표는
`미확인`입니다. 실제 게시 자동화 기능은 없습니다.

`cron.txt`에 KST 기준 실행 예시가 있습니다. 이 도구는 crontab을 설치하지 않습니다.
운영자가 경로와 설정을 확인한 뒤 직접 적용합니다. 데몬 제어 명령은
`python3 autobot.py daemon {start,stop,restart,status,stages,loop}`입니다.
