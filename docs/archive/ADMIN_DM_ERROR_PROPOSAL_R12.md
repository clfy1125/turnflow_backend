# 어드민 API 요청서 회신 — 12차 (DM-13 ~ DM-17)

작성 2026-07-31 · 백엔드 → 프론트
대상 코드: `feat/toss-billing` (미배포)

---

## 0. 요약

**5건 전부 반영했습니다. 마이그레이션 없습니다.**

| 번호 | 요청 | 결과 |
|---|---|---|
| **DM-13** | 축 필터 `?dm_axis=` | ✅ 반영 — 검증 항등 2개 테스트로 고정 |
| **DM-14** | `reason` 머신 키 + `?error_reason=` | ✅ 반영 — 건너뜀 사유까지 **같은 파라미터**로 통합 |
| **DM-15** | 500쌍 상한 | ✅ **상한 자체를 없앴습니다** (A·B·C 아닌 4번째 방안, 마이그레이션 없음) |
| **DM-16** | `error_title` 기준 통일 | ✅ 반영 — `error_reason` 도 같은 로그 기준 |
| **DM-17** | `accepted_pending` 이름 | ✅ 반영 — **rename**(1번) + docstring/help_text 경고(2번) 둘 다 |

**⚠️ 프론트가 반드시 확인해야 할 것 2가지**

1. **DM-15 를 B 로 고르지 않았습니다** → 팝업의 `⚪ 전체 보러가기` 버튼을 **빼지 마세요.**
   그대로 두면 됩니다. 기능 축소 없습니다.
2. **DM-17 은 rename 입니다** → `follow_up.accepted_pending` 키가 **사라졌습니다.**
   `follow_up.accepted_pending_in_waiting` 로 바꿔 주세요. (11차 필드라 아직 배포 전이므로
   지금 바꾸는 편이 안전하다고 판단했습니다.)

그리고 요청 범위를 넘어 하나 더 했습니다 — 자세한 건 §6.
**건너뜀(skipped) 로그가 `not_sent` 분해에서 '분류되지 않은 실패'로 떨어지던 문제**를 함께
고쳤습니다. 화면을 그리다 보면 마주칠 자리였습니다.

---

## DM-13 · 축 필터 `?dm_axis=opening|follow_up`

### 반영

```
GET /admin/auto-dm/recipients/?campaign_id=<uuid>&dm_axis=opening&error_policy=investigate
```

| 값 | 의미 |
|---|---|
| `opening` | 루트 DM 이 발송/대기 어디에도 못 간 사람 + 도착 미확인으로 끝난 사람 |
| `follow_up` | **마지막** 후속 DM 이 실패인 사람 (`basis=latest_per_person`) |
| `all` / 미지정 | 지금 동작 유지 (두 축 합침) |

허용값 외는 400, `details.field = "dm_axis"`, `details.allowed = ["all","opening","follow_up"]`.

### ★ 한 가지 정의를 명시합니다 — "판정 근거"가 아니라 "모수"입니다

요청서에 `?dm_axis=opening` = "오프닝·단발 기준 **판정된** 사람" 이라고 쓰셨는데,
그 말대로 *판정 근거만* 바꾸면 요청하신 검증 항등이 **성립하지 않습니다.**

> 과거에 실패했다가 재시도로 결국 성공한 사람은 오프닝 축의 대표 실패 로그를 갖지만
> `not_sent`(발송 안 됨) 인원에는 **들어가지 않습니다.**

그래서 `dm_axis` 를 **"그 축의 '발송 안 됨' 모수로 좁힌다"** 로 구현했습니다.
카드가 전부 '발송 안 됨' 숫자이므로 이게 요청하신 동선에 맞는 정의입니다.

부작용: `?dm_axis=opening` **단독** 호출은 `not_sent.total` 인원만 옵니다("그 축의 전체
수신자"가 아닙니다). 4개 조합만 쓰신다고 하셨으니 문제 없다고 봤지만, "그 축의 **모든**
사람" 목록이 따로 필요하면 말씀해 주세요 — `?dm_axis_scope=all` 을 추가하겠습니다.

### 배지도 축을 따릅니다

`dm_axis` 를 주면 그 행의 `error_title` · `error_reason` · `error_policy` 도 **그 축의
대표 로그** 기준으로 바뀝니다. 안 주면 축을 가리지 않은 '가장 최근 실패·정체 로그' 기준
(기존 동작)입니다. 그래야 "카드를 눌러 들어온 목록의 배지가 카드와 같다"가 성립합니다.

### 검증 항등 — 테스트로 박았습니다

`apps/admin_api/tests_dm_error_filters.py::TestAxisFilter`

```
stats.not_sent.total              == ?dm_axis=opening                             .count
stats.not_sent.investigate        == ?dm_axis=opening&error_policy=investigate     .count
stats.not_sent.normal             == ?dm_axis=opening&error_policy=normal          .count
stats.follow_up.not_sent.total    == ?dm_axis=follow_up                            .count
stats.follow_up.not_sent.*        == ?dm_axis=follow_up&error_policy=*             .count
not_sent.breakdown[].people       == ?dm_axis=<축>&error_reason=<그 reason>        .count   ← DM-14
```

집계와 필터가 **같은 함수**(`dm_policy_rollup.rep_log_qs`)를 호출하도록 만들어서,
규칙이 두 벌로 갈라지는 것 자체가 불가능합니다.

---

## DM-14 · 사유 머신 키 `reason`

### 반영 — 3곳 전부

1. 사전 항목마다 `reason` 부여 (`apps/admin_api/dm_error_catalog.py`)
2. `dm_quality.failure_breakdown[]` · `skipped_breakdown[]` · `not_sent.breakdown[]` 에 `reason`
3. `?error_reason=` 필터 — **로그 목록 + 수신자 목록 둘 다**

추가로 **로그 행 자체에도** `error_reason` 을 넣었습니다(목록·상세). 로그 행에서
"이 행과 같은 사유 전체 보기"를 만들 때 프론트가 코드로 역추론하지 않아도 됩니다.

### 키 목록 (영구 고정)

주신 예시 키를 최대한 채택했습니다.

| reason | title | policy |
|---|---|---|
| `window_after_close` | 창이 닫힌 뒤 도착한 요청 | 🔴 |
| `already_replied` | 그 댓글에 이미 답장이 있음 | 🔴 |
| `stalled_by_us` | 우리가 제때 못 보냄 | 🔴 |
| `window_peak_backlog` | 요청이 몰려 기간 안에 못 보냄 | ⚪ |
| `no_target_or_expired` | 보낼 대상이 없거나 만료됨 | 🔴 |
| `no_trace` | 도착 미확인 | 🔴 |
| `post_blocked` | 이 게시물만 자동 DM 이 막힘 | 🔴 |
| `permission_or_window_unknown` | 권한 문제인지 기간 만료인지 불명 | 🔴 |
| `scoped_permission` | 이 사람·이 게시물에 대한 권한 문제 | 🔴 |
| `legacy_failure` | 실패 (예전 형식 기록) | 🔴 |
| `no_reason_given` | Instagram 이 이유를 알려주지 않음 | 🔴 |
| `hidden_spam_inbox` | 숨겨진 요청 · 스팸함으로 들어감 | ⚪ |
| `recipient_not_found` | 받는 사람을 찾을 수 없음 | ⚪ |
| `conversation_deleted` | 상대가 DM 대화방을 지움 | ⚪ |
| `recipient_unreachable` | 상대가 메시지를 받을 수 없음 | ⚪ |
| `connection_lost` | Instagram 연결이 끊김 | ⚪ |
| `session_expired` | Instagram 연결이 끊김 (세션 만료) | ⚪ |
| `token_invalid` | Instagram 연결이 끊겨 보내지 못함 | ⚪ |
| `window_expired_legacy` | 보낼 수 있는 기간이 지남 | ⚪ |
| `rate_limited` | Instagram 이 잠시 속도를 늦춤 | ⚪ |
| `app_rate_limited` | 앱 전체 호출 한도 초과 | ⚪ |
| `recovery_pending` | 복구 진행 중 | ⚪ |
| `recovery_expired` | 복구 기한 만료 | ⚪ |
| `unclassified` | 분류되지 않은 실패 | 🔴 |

**건너뜀 사유는 기존 `skipped_breakdown[].reason` 키를 그대로 씁니다**
(`monthly_dm_limit` · `campaign_not_active` · `outside_schedule_window` ·
`ig_account_inactive` · `self_recipient` · `connection_disconnected` ·
`duplicate_campaign_cleanup` · `ghost_opening_cleanup` · `other`).
오류 사유 키와 겹치지 않으므로 `?error_reason=` **한 파라미터로 두 표 모두** 착지합니다.
프론트는 어느 표의 행인지 신경 쓸 필요가 없습니다.

### 지켜지는 불변식 (테스트가 강제)

- **`reason` ↔ `title` 은 1:1** — reason 으로 묶어 title 을 보여줘도 한 칩에 두 문구가 안 생깁니다
- **`reason` ↔ `policy` 도 1:1** — 사유 하나가 🔴 이면서 ⚪ 일 수 없습니다
- 오류 사유 키 ∩ 건너뜀 사유 키 = ∅
- 사유별 인원/건수의 합 == 전체, 서로소

### 계약

```
failure_breakdown / skipped_breakdown 의 같은 reason 행들의 count 합
  == GET /admin/auto-dm/logs/?error_reason=<reason> 의 count      (이벤트 단위)

not_sent.breakdown[].people
  == GET /admin/auto-dm/recipients/?dm_axis=<축>&error_reason=<reason> 의 count   (사람 단위)
```

`tests_dashboard_ops.py::test_breakdown_count_equals_logs_filter_count` +
`tests_dm_error_filters.py::test_breakdown_reason_identity` 로 고정.

### `unclassified` 에 대해

현재 **항상 0건**입니다. 오류 8종이 전부 status 사전에 등록돼 있어서 4단 폴백의 마지막
단계가 항상 받아 주기 때문입니다. 새 실패 status 를 추가하면서 사전 항목을 빼먹으면
그때 살아나 화면에 '분류되지 않은 실패'로 보입니다 — 의도된 안전망이고,
`test_every_error_status_has_a_dictionary_entry` 가 그 상태를 실수로 만들지 못하게 막습니다.
프론트는 이 키를 **표시할 준비만** 해 두시면 됩니다(0건이라 안 보일 겁니다).

### `error_title` 로 필터하지 못한다는 지적 — 맞습니다

주신 대로 문구는 계속 다듬을 예정이라 필터 키로 쓸 수 없습니다. 그리고 문구 하드코딩
단언을 걷어낸 것도 같은 이유입니다. `reason` 은 **영구 고정**이라고 모듈 최상단에
못박아 뒀습니다.

---

## DM-15 · 500쌍 상한 — **없앴습니다** (A·B·C 아닌 4번째)

### 방식

주신 3안 중에는 B(상한 올리기)가 현실적이었지만 `⚪ 전체` 가 막히는 건 그대로였고,
A(컬럼화)는 마이그레이션 + 백필 + 판정 결과 이중 저장이 생깁니다.

**대신 사전 자체를 SQL 로 컴파일했습니다** (`apps/admin_api/dm_error_filters.py`).
`policy` 가 SQL 로 못 간다고 본 게 11차의 전제였는데, 다시 보니 판정이
`(code, subcode, status)` 4단 폴백이라는 **구조**만 옮기면 됩니다.

```python
reason_q("window_after_close")
#  → (code=100,sub=2534022) OR (code=10,sub=2534022) OR (code=10,sub=2018278)
#    OR (code=10 AND status=failed_window AND sub NOT IN {상위 레벨 키})
```

결과:

- **상한 없음.** `_POLICY_FILTER_MAX_PAIRS` 상수와 그 400 분기를 삭제했습니다.
- **마이그레이션 없음.** 컬럼도, 백필도, 판정 결과 이중 저장도 없습니다.
- 사람 단위(수신자 목록)는 대표 로그를 `DISTINCT ON` 서브쿼리로 뽑아 그 안에서
  SQL 필터를 겁니다 — 쌍 OR 체인이 사라졌습니다.

### 위험 관리 — 판정 규칙을 두 벌로 들게 됐습니다

파이썬(`classify`)과 SQL(`dm_error_filters`)이 갈라지면 조용히 틀립니다. 그래서
`tests_dm_error_filters.py::TestSqlPythonEquivalence` 가 **사전 전 조합 + 미등록 조합
+ 성공 상태**를 DB 에 넣고 행 단위로 대조합니다:

```
set(qs.filter(policy_q(p)))  ==  {row | classify(row)["policy"] == p}
set(qs.filter(reason_q(r)))  ==  {row | classify(row)["reason"] == r}     # 전 사유
```

사전에 항목을 추가하면 그 조합까지 자동으로 검증 대상이 됩니다.
`🔴 + ⚪ == 전체` · `서로소` 도 함께 단언합니다.

### 팝업 시안 그대로 두세요

```
🔴 조사 필요 · 전체 보러가기      42건    → 200
⚪ 자동 처리 · 전체 보러가기   1,192건    → 200   ← 400 안 납니다
⚪ 사유별 보러가기 (숨김함 유입)  412건    → 200
```

**기능 축소 없으니 `⚪ 전체 보러가기` 버튼 빼지 마세요.**

### ★ 다만 모수를 확인해 주세요 — `?error_scope=` 를 함께 넣었습니다

`?error_policy=` 의 모수를 **오류 8종 + 건너뜀**으로 잡았습니다.
성공·진행 중 로그는 어느 쪽에도 안 들어갑니다(`normal` 을 눌렀을 때 도착한 DM 전부가
딸려 나오면 무의미하므로 — 테스트로 고정).

그런데 팝업 예시의 `1,192건` 이 `failure_breakdown` 만인지 `skipped_breakdown` 까지인지
확실하지 않아, 골라 쓸 수 있게 파라미터를 뒀습니다.

| `?error_scope=` | 모수 |
|---|---|
| `all` (기본) | 오류 8종 + 건너뜀 = `failure_breakdown` + `skipped_breakdown` |
| `error` | 오류 8종만 = `failure_breakdown` 모수 |
| `skipped` | 건너뜀만 = `skipped_breakdown` 모수 |

**팝업에 건너뜀 표를 안 그리신다면 `&error_scope=error` 를 붙이세요.** 그러면
`Σ failure_breakdown[policy==X].count` 와 정확히 맞습니다.
`error_reason` 은 사유 키가 스코프를 담고 있어 신경 쓸 필요 없습니다.

### 성능

상한이 없어졌지만 전역 조회(campaign 미지정)의 사람 단위 그룹 집계는 여전히 무겁습니다
(11차 이전에도 그랬고, 파이썬으로 전건 분류하던 것보다는 나아졌습니다).
팝업 → 로그 동선은 **이벤트 단위인 `/logs/`** 로 보내시는 게 훨씬 가볍습니다.
사람 단위가 필요한 자리에서는 `campaign_id` / `ig_connection_id` 를 함께 주세요.

---

## DM-16 · `error_title` 기준을 최신 **실패** 로그로

### 반영

`/admin/auto-dm/recipients/` 의 행에서:

| 필드 | 기준 |
|---|---|
| `error_title` · `error_reason` · `error_policy` | **가장 최근 실패·정체 로그** (축을 주면 그 축의 대표) |
| `latest_status` | 그대로 **최신 로그** (= "지금 상태", 의미가 달라서 안 건드렸습니다) |
| `latest_followup_status` | 그대로 마지막 후속 DM |

세 값이 **한 로그**에서 나오므로 "조사 필요 34명" 목록에 사유 빈칸 행이 섞이지 않습니다.
실패 이력이 아예 없는 사람은 셋 다 빈 문자열 — 정합입니다.

```
수신자           분류         사유
@yerin_makeup   조사 필요    우리가 제때 못 보냄
@onlybook_      조사 필요    도착 미확인          ← 전에는 빈칸
```

### `error_cause` / `error_action` 은 원래 문제가 없었습니다

그 둘은 **로그 엔드포인트에만** 있고 각 로그 1건을 그대로 설명하므로 기준이 어긋날 자리가
없습니다. 수신자 행에는 없습니다(20행 × 장문이면 응답이 부풀어서). 사유 전문이 필요하면
행을 눌러 `GET /admin/auto-dm/logs/{id}/` 로 가시면 됩니다.

### 회귀 테스트 정리

11차 회신에 남겼던 주의(*"마지막 발송이 성공인 사람은 title 이 비는데 policy 는 값이 있다"*)는
**해소**됐고, 그 caveat 을 검증하던 테스트를 새 계약으로 바꿨습니다
(`tests_dm_people_stats.py::test_recipient_row_title_survives_later_success`).

---

## DM-17 · `accepted_pending` — rename + 경고 둘 다

주신 두 안 중 하나만 고르지 않고 둘 다 했습니다. 이름만 바꿔도 다음 사람이 또 헷갈릴
자리라 판단했습니다.

### 1) rename (breaking — 프론트 수정 필요)

```diff
- stats.follow_up.accepted_pending
+ stats.follow_up.accepted_pending_in_waiting
```

오프닝 축의 `unique_accepted_pending` 은 **그대로**입니다(부모가 `unique_sent` 인 건
`unique_` 접두와 함께 이미 다른 이름이라).

### 2) 경고 추가

`followup_rollup()` docstring 에 부모 집합 대조표를, 시리얼라이저 `help_text` 에 축별
줄 계산식을 넣었습니다. Swagger 에서 바로 보입니다.

```
대기중     오프닝 = unique_waiting + unique_accepted_pending
           후속   = follow_up.waiting              (단독)
발송 안 됨  오프닝 = unique_failed  + unique_unconfirmed
           후속   = follow_up.failed               (단독)
```

`unique_accepted_pending` 쪽 help_text 에도 *"부모 집합은 `unique_waiting` 이 아니라
**`unique_sent`**"* 를 명시했습니다.

### 왜 관계가 반대인가 (참고)

두 축이 `ACCEPTED`(Meta 접수, 도착 미확정)를 다르게 봅니다 —
오프닝 축은 '발송됨(sent)', 후속 축은 '대기(waiting)'. 후속 축에는 sent 버킷이 아예
없어서(delivered/waiting/failed 3개) 그렇게 맞춘 것이고, 지금 바꾸면
`follow_up.failed`(= `not_sent`)가 흔들려 카드 숫자가 변합니다. **이름으로 해결**했습니다.

`test_screen_row_sums_hold_per_axis` 가 축별 산술을 고정합니다.

---

## 6. 요청 범위를 넘어 하나 더 — 건너뜀 로그의 사유

화면을 그리다 마주칠 자리라 함께 고쳤습니다. 사전을 통합하다 발견했습니다.

**문제**: `not_sent` 모수에는 건너뜀(`skipped`) 로그도 들어가는데, 건너뜀은 **오류 사전에
없어서** 사유가 '분류되지 않은 실패 (skipped)' 로 떨어졌습니다. 같은 로그가 운영
대시보드에서는 '월 DM 한도 도달' 같은 라벨을 갖는, **두 갈래 판정**이었습니다.
그리고 `policy` 도 갈렸습니다 — 미분류 건너뜀이 운영 대시보드에서는 🔴 인데
`not_sent` 에서는 ⚪ 였습니다.

**조치**: 건너뜀 사유표를 `views/dashboard_ops.py` → `dm_error_catalog.py` 로 옮기고,
`classify()` 를 **단일 판정 함수**로 만들었습니다. 이제:

- `not_sent.breakdown` 에 건너뜀도 제대로 라벨이 붙습니다 (`월 DM 한도 도달` 등)
- 미분류 건너뜀은 어디서든 🔴 (사전에 없는 문구가 찍혔다는 뜻)
- 로그 행에도 `error_reason` / `error_title` 이 뜹니다 (전엔 건너뜀 행이 빈칸)
- `?error_reason=monthly_dm_limit` 로 건너뜀도 드릴다운됩니다

`skipped_breakdown[]` 의 응답 키·값은 **하나도 바뀌지 않았습니다** — 기존 화면 영향 없음.

---

## 7. 프론트가 해야 할 일

### 반드시

- [ ] `follow_up.accepted_pending` → **`accepted_pending_in_waiting`** (DM-17, breaking)
- [ ] 팝업 `⚪ 전체 보러가기` **유지** (DM-15 — 빼지 마세요)
- [ ] 팝업이 건너뜀 표를 안 그린다면 `전체 보러가기` 링크에 `&error_scope=error` 추가

### 새로 쓸 수 있는 것

- [ ] `?dm_axis=opening|follow_up` — 캠페인 상세 카드 4개 드릴다운
- [ ] `?error_reason=` — 팝업 사유별 `보러가기` (`breakdown[].reason` 을 그대로)
- [ ] 로그 행의 `error_reason` — "이 행과 같은 사유 전체 보기"
- [ ] 로그 행의 `error_title` 이 건너뜀에도 뜹니다 (전엔 빈칸)

### 안 하셔도 되는 것

- `unclassified` 는 현재 0건 (표시 준비만)
- `error_cause` / `error_action` 기준 걱정 — 원래 문제 없었습니다

---

## 8. 남은 결정 · 미결

| 항목 | 상태 |
|---|---|
| `?dm_axis_scope=all`("그 축의 모든 사람") | 필요하면 말씀해 주세요 — 안 만들었습니다 |
| `1,192건` 의 모수 | `error_scope` 로 프론트가 고르는 걸로. 시안 확정 후 알려 주시면 기본값 조정 |
| 7일 초과 원문 파싱 | **여전히 미정** — prod census(`dump_dm_error_census`) 미실행. 그전까지 7일 초과 건은 `no_target_or_expired`(🔴)에 섞입니다. DM-9 문구 교체 덕에 화면 설명은 정확합니다 |
| `GROUP_DISPLAY[attention]` 이름 | 11차 그대로 보류 (유저 콘솔 탭 이름 = 제품 결정) |
| `docs/DM_AUTO_NOTICE_TODO.md` | ⚪ 인데 고객 안내가 안 나가는 건 — 유저 콘솔·백엔드 소관, 별건으로 진행 |

---

## 9. 변경 파일

```
신규
  apps/admin_api/dm_error_filters.py          사전 → SQL 컴파일 (DM-14/15)
  apps/admin_api/tests_dm_error_filters.py    34건 (SQL↔파이썬 등가성 · 축 항등 · 상한 제거)

수정
  apps/admin_api/dm_error_catalog.py          reason 키 · classify() 단일 판정 · 건너뜀 사유표 이관
  apps/admin_api/dm_policy_rollup.py          축 개념 · rep_log_qs 단일화 · HAVING 판
  apps/admin_api/views/autodm.py              ?dm_axis / ?error_reason / ?error_scope · 상한 삭제
  apps/admin_api/views/dashboard_ops.py       failure_breakdown[].reason
  apps/admin_api/serializers/autodm.py        error_reason · help_text
  apps/admin_api/serializers/dashboard_ops.py reason 필드 선언 · 표시명 정정
  apps/integrations/campaign_stats.py         followup_failed_q() · DM-17 rename
  apps/integrations/serializers.py            DM-17 부모 집합 경고
  apps/admin_api/tests_*.py                   계약 변경 4건 반영
```

**마이그레이션 없음 · `SentDMLog.status` 값 변경 없음 · 기존 집계 정의 변경 없음.**
prod 에 캠페인이 돌고 있는 상태에서 안전한 변경 범위를 유지했습니다.

### 테스트

```
apps/admin_api/tests_*.py         → 통과 (신규 34 + 기존 전부)
apps/integrations 전체            → 통과 (기존 실패 2건은 이 패치와 무관, git stash 로 확인)
```
