# [어드민팀 회신] 11차 요청 DM-6 ~ DM-12 — **7건 전부 반영 완료**

2026-07-31 · 백엔드 → 어드민 콘솔팀 · 이전 문서: `ADMIN_DM_ERROR_PROPOSAL.md`(개편 안내서)

---

## 0. 결론

| 번호 | 상태 | 비고 |
|---|---|---|
| **DM-6** 후속 DM 사람 단위 블록 | ✅ 완료 | `stats.follow_up` · `basis="latest_per_person"` |
| **DM-7** 캠페인·사람 단위 policy 집계 | ✅ 완료 | `stats.not_sent` + `stats.follow_up.not_sent` |
| **DM-8** recipients `error_policy`·`latest_followup_status` | ✅ 완료 | `?error_policy=` 서버 필터 포함 (**상한 있음**, §DM-8) |
| **DM-9** `100`/`failed_param` 문구 + `2534001` | ✅ 완료 | 제안 문구 그대로 채택 + 2534001 등록 |
| **DM-10** 내부 용어 제거 | ✅ 완료 | 16행 표 전부 반영 |
| **DM-11** `unique_accepted_pending` | ✅ 완료 | 두 축 모두 |
| **DM-12** 이름 충돌 | ✅ 완료 — **단, 반대쪽을 바꿨습니다** | `policy` 표시명을 바꿨습니다 (§DM-12) |

마이그레이션 없음. 기존 필드·값 변경 없음(문구 제외). 신규 테스트 20건 + 기존 회귀 694건 통과.

**요청서 표와 본문 개수 일치 확인** — 표 7건, 본문 7건. 누락 없습니다.

---

## DM-6 · 후속 DM 사람 단위 블록

`build_dm_stats()` 응답에 `follow_up` 을 추가했습니다. 요청하신 키를 그대로 씁니다.

```jsonc
"follow_up": {
  "targets": 84, "delivered": 71, "read": 52,
  "waiting": 6, "accepted_pending": 1,
  "failed": 7, "unconfirmed": 0,
  "reach_rate": 0.8452,
  "basis": "latest_per_person",
  "not_sent": { ... }            // DM-7 (어드민 응답에만 붙습니다)
}
```

### 정의 — 제안하신 대로 `dm_kind = 'reward'`

오프닝 재시도(`dm_kind=opening` + `parent_log`)를 섞지 않는다는 근거에 동의합니다.
`FOLLOW_UP_KIND` 상수로 고정하고, 재시도가 후속으로 새지 않는 것을 테스트로 박았습니다
(`test_retry_is_not_counted_as_followup`).

### 집계 규칙 — **마지막 후속 DM 1건** 기준으로 고정

`latest_followup_rows()` 하나가 규칙의 단일 소스입니다. DM-7 의 사유 분해와 DM-8 의
`latest_followup_status` 도 **같은 함수**를 통해 같은 행을 봅니다 — 규칙을 복제하지 않았습니다.

### ⚠️ `unconfirmed` 의 소속 — 오프닝 축과 반대입니다 (의도)

- 오프닝 축: `unconfirmed ⊆ sent` (발송은 됐고 도착만 미확인)
- **후속 축: `unconfirmed ⊆ failed`**

요청서의 불변식 `targets == delivered + waiting + failed` 와 화면의 "발송 안 됨" 줄이
`failed` 를 그대로 쓰기 때문에 이렇게 맞췄습니다. 시안의 숫자 계산과 일치합니다.
시리얼라이저 help_text 에도 경고로 적어 뒀습니다.

### 확인 요청 답변 — **후속 DM 2건 이상, 실제로 가능합니다**

prod 쿼리는 제 환경에서 못 돌리지만 **코드로 확정**됩니다.

리워드 로그의 멱등키는 `sha256("reward:" + 오프닝의 idempotency_key)` 입니다
(`apps/integrations/tasks.py:3321`). 즉 **오프닝 1건당 리워드는 정확히 1건**이지만,
한 사람이 같은 캠페인에서 **오프닝을 2건 이상** 받으면(댓글을 두 번 달고 수신자 쿨다운
5분을 넘긴 경우) 리워드도 2건이 됩니다.

→ "마지막 기준"이 실제로 필요한 규칙이고, 그대로 고정했습니다. 요청하신 대로
화면 문구도 마지막 기준으로 쓰시면 됩니다.

---

## DM-7 · 사람 단위 `policy` 집계 + 사유별 내역

두 축 모두 요청하신 모양 그대로입니다. `policy_display` 만 한 필드 더 넣었습니다
(프론트에 한국어를 두지 않기 위해).

```jsonc
"not_sent": {
  "total": 125, "investigate": 34, "normal": 91,
  "breakdown": [
    { "policy": "investigate", "policy_display": "조사 필요",
      "title": "창이 닫힌 뒤 도착한 요청", "people": 12 },
    { "policy": "normal", "policy_display": "자동 처리",
      "title": "숨겨진 요청 · 스팸함으로 들어감", "people": 27 }
  ]
}
```

### 계약 4가지 — 전부 테스트로 고정

1. `investigate + normal == total` ✅ (`test_contract_totals`)
2. `Σ breakdown[].people == total` ✅ (같은 테스트)
3. `title` 은 서버 사전 문구 그대로 ✅ — 사전에 없는 조합은 `"분류되지 않은 실패 (status)"` 로 채워 **빈 칩이 뜨지 않게** 했습니다
4. **대표 사유 = 가장 최근 실패 로그** ✅ — 제안하신 규칙을 채택했습니다 (`test_representative_is_latest_failure`)

추가로 `total == unique_failed + unique_unconfirmed` 도 테스트로 박았습니다
(`test_matches_unique_failed_plus_unconfirmed`) — 표의 "발송 안 됨" 줄과 팝업 합계가
어긋나면 CI 가 먼저 잡습니다.

### 정렬

`breakdown` 은 **🔴 먼저, 그 안에서 인원 많은 순**으로 서버가 정렬해 보냅니다.
프론트에서 다시 정렬하지 않으셔도 됩니다.

### 주의 — `error_title`(최신 로그) vs `error_policy`(최신 **실패** 로그)

대표 사유를 "최신 실패 로그"로 잡았으므로, **마지막 발송이 성공인 사람**은
`error_title` 이 비어 있는데 `error_policy` 는 값이 있을 수 있습니다.
"과거에 실패했지만 결국 성공한 사람"이 그 경우입니다. 배지를 나란히 놓으실 때 참고하세요.
(둘을 일치시키려면 `error_title` 도 최신 실패 기준으로 바꿔야 하는데, 그건 기존 계약
변경이라 하지 않았습니다. 원하시면 다음 라운드에 맞추겠습니다.)

---

## DM-8 · recipients 필드 + 필터

세 가지 모두 반영했습니다.

```jsonc
{
  "...": "...",
  "latest_status": "delivered",
  "latest_followup_status": "failed_param",   // ★ 신규 — 후속이 없으면 ""
  "error_title": "",
  "error_policy": "investigate"               // ★ 신규 — 실패 이력이 없으면 ""
}
```

- `error_policy` 판정은 DM-7 §4 의 **대표 사유 규칙과 동일**합니다(같은 `dm_policy_rollup` 호출).
  카드 인원과 목록 배지가 구조적으로 어긋날 수 없습니다.
- `latest_followup_status` 는 DM-6 과 **같은 함수**로 뽑습니다.

### `?error_policy=investigate` — 지원하되 **상한 500명**

솔직하게 제약을 적습니다. `policy` 는 `(code, subcode, status)` 4단 폴백이라 SQL 조건으로
옮길 수 없습니다. 그래서 대표 실패 로그를 뽑아(실패 계열만 스캔) 파이썬에서 분류한 뒤
`(campaign, recipient)` 쌍으로 좁힙니다.

- 대상이 **500쌍 이하**면 정확히 필터됩니다 → 캠페인 드릴다운은 항상 여기 들어옵니다
- 500쌍을 넘으면 **조용히 자르지 않고 400** 을 돌려드립니다:
  `"error_policy 필터 대상이 너무 많습니다(N명). campaign_id 또는 ig_connection_id 로 범위를 좁혀 주세요."`

전역 조회에서도 쓰셔야 하면 알려주세요 — 그때는 판정 결과를 컬럼으로 들고 가는(=마이그레이션)
설계가 필요합니다. 지금은 드릴다운 용도에 맞춰 마이그레이션 없이 갔습니다.

허용값은 `all` / `investigate` / `normal` 이고, 그 외는 400(`details.field = "error_policy"`)입니다.

---

## DM-9 · 문구 교체 + `2534001` 등록

### 제안 문구 그대로 채택했습니다

`_BY_CODE["100"]` · `_BY_STATUS["failed_param"]` 둘 다 **"보낼 대상이 없거나 만료됨"** 으로
바꾸고, 원인은 단정하지 않고 세 가지를 나열합니다. 지적하신 대로 신규 subcode 가 이 통에
떨어져도 문구가 거짓말이 되지 않습니다.

### `2534001` 등록 완료 — 실측 근거는 **주신 원문**입니다

`dump_dm_error_census` 는 제 환경에서 prod 에 붙지 못하지만, 보내주신 Meta 원문

```
대화 소유자가 이 대화를 보관했거나 삭제했습니다. 또는 대화가 존재하지 않습니다.
| http=400 | code=100 | subcode=2534001
```

자체가 실측입니다. 제안하신 판정(`policy=normal` + `RECIPIENT_NOTICE`)과 문구를 그대로
등록했습니다. 사전 주석에 이 원문과 출처(어드민팀 제보)를 남겨 뒀습니다.

→ `../system/DM_ERROR_POLICY_MATRIX.html` 의 `gap:2534001`("추정·실측 확인 필요")은 이제 해소입니다.

### 확인 요청 답변 — "7일 초과" 원문 파싱은 **아직 미정입니다**

`../system/DM_ERROR_POLICY_PLAN.md` §Phase 0 ③ 그대로 미확인입니다(prod census 미실행).
**그 전까지 7일 초과 건은 🔴로 뜹니다** — 말씀하신 전제가 맞습니다.

다만 DM-9 문구 교체로 **화면 문구는 그 전제 위에 있지 않게** 됐습니다.
"보낼 대상이 없거나 만료됨 / 7일 초과·삭제·대화방 삭제 중 하나"이므로, 7일 초과 건이
🔴에 섞여 있어도 설명은 정확합니다. 카드 이름만 안내서 §4 의 "파라미터 오류·원인 미확정"
대신 **"보낼 대상이 없거나 만료됨"** 으로 써 주시면 됩니다.

census 를 돌리면 바로 알려드리고, 갈라지면 ⚪ 카드가 하나 생깁니다.

---

## DM-10 · 내부 용어 제거

16행 표를 **전부** 반영했습니다. 재작성 예시 2건도 주신 문장을 거의 그대로 썼습니다.

바뀐 문구 몇 개:

| 이전 | 지금 |
|---|---|
| 파라미터 오류 | 보낼 대상이 없거나 만료됨 |
| 발송 방치로 메시징 창 만료 · 발송 파이프라인 쪽 문제 | **우리가 제때 못 보냄** · 우리 시스템 문제 |
| requeue_deferred_dms 가 도는지, 워커가 살아 있는지, Action Block 쿨다운… | 개발팀에 알려 **발송 처리기**가 멈췄는지 확인하세요 |
| 구독 auto-disable 이력·poll_missed_stat | 이 계정의 **Instagram 알림 연결**이 끊긴 적이 있는지 |
| 보정 폴러가 주운 오래된 건 | **놓친 댓글을 나중에 주워온** 건 |
| 토큰 만료 · 무효 | Instagram 연결이 끊김 |
| 수신자 도달 불가 | 상대가 메시지를 받을 수 없음 |
| 레이트 리밋 | Instagram 이 잠시 속도를 늦춤 |
| 재검증(reverify)·`/admin/…/reverify/` | **[다시 확인] 버튼** |
| 원문 메시지(sample_error_message) | **Instagram 원문** |

기준 3가지(제목에 코드·내부 상태명 금지 / 원인 단정 금지 / 조치란에 함수명·경로 금지)를
사전 전체에 적용했습니다.

> **부탁 하나** — 문구를 하드코딩해서 단언하는 테스트가 프론트에도 있으면 바꿔 주세요.
> 저희 쪽은 이번에 `"토큰" in title` 같은 단언 4건이 한꺼번에 깨져서, **사전 값과 대조**하는
> 방식으로 고쳤습니다. 문구는 앞으로도 다듬을 예정이라 같은 일이 반복됩니다.

---

## DM-11 · `unique_accepted_pending`

두 축 모두 넣었습니다. 뺄셈은 `_derive_people` 한 곳에만 있습니다(주석에 DM-5 회귀 유형 명시).

- 오프닝 축: `unique_accepted_pending`
- 후속 축: `follow_up.accepted_pending` — 블록 안의 다른 키가 접두 없는 이름이라 맞췄습니다

화면 3줄 합이 전체 요청과 같은지를 테스트로 고정했습니다(`test_waiting_row_adds_up`).
말씀하신 대로 **대기중 = `unique_waiting + unique_accepted_pending`** 입니다.

목록 응답의 `people` 블록에도 `accepted_pending` 키가 함께 생깁니다(목록·상세가 같은
함수를 쓰기 때문). 쓰지 않으셔도 되지만 키가 늘어난 것은 알아 두세요.

---

## DM-12 · 이름 충돌 — **policy 쪽을 바꿨습니다**

제안하신 `GROUP_DISPLAY[ATTENTION]: "확인 필요" → "발송 안 됨"` 은 **채택하지 않았습니다.**
대신 반대쪽인 policy 표시명을 바꿨습니다.

```python
POLICY_DISPLAY = {
    "investigate": "조사 필요",   # was "확인해야함"
    "normal":      "자동 처리",   # was "정상 처리"
}
```

### 이유 둘

1. **`GROUP_DISPLAY` 는 유저 콘솔 탭 이름입니다.** 어드민만의 문제를 고치려고 고객 화면
   문구를 바꾸는 건 제품 결정이라 저희가 단독으로 못 정합니다.
2. **`failed_no_trace`(도착 미확인)가 attention 그룹에 있습니다.** Meta 는 접수했고 실제로
   도착했을 수도 있는 건이라, 그룹 전체를 "발송 안 됨"이라 부르면 틀린 말이 됩니다.
   (화면의 "발송 안 됨" **줄**은 `unique_failed + unique_unconfirmed` 라 그룹과 집합도 다릅니다.)

policy 는 **조치 축**이라 동사가 맞고, 그룹(결과 축)과 나란히 놓아도 읽힙니다.

```
@user_a   확인 필요 · 자동 처리     ← 재연동 안내가 자동으로 나감
@user_b   확인 필요 · 조사 필요     ← 사람이 봐야 함
```

그래도 "발송 안 됨"이 낫다고 보시면 말씀해 주세요 — 유저 콘솔 쪽 결정만 받으면
`GROUP_DISPLAY` 한 줄이라 바로 바꿉니다. **둘 중 하나만 바뀌면 된다**는 데는 동의합니다.

---

## 안내서 회신에 대한 답

`B1`~`D3` 처리 계획 확인했습니다. 특히 **D2 를 문구 수정이 아니라 블록 삭제로 푸는 것**
(발송 이벤트 단위 8칸 제거) — 저희도 그게 맞다고 봅니다. 이벤트 단위 숫자가 사람 단위 옆에
있으면 이번 개편의 의미가 없어집니다.

`subcode` 에 숫자가 아닌 값이 오는 것 관련해 **`parseInt` 가 없다**고 확인해 주셔서 감사합니다.
저희 쪽도 백엔드 전 경로에 `int(error_subcode)` 가 없는 것을 확인했습니다(전수 grep).

---

## 배포 · 호환성

- **마이그레이션 없음.** 전부 집계·직렬화·문구입니다.
- 기존 필드·값은 그대로입니다. **문구만** 바뀝니다(DM-9/10) — 화면에 서버 문구를 그대로
  렌더하고 계시니 배포 순서와 무관합니다.
- 신규 키는 전부 **추가**뿐이라 어드민 배포 전에도 무시되고 끝납니다.
- 배포 후 `admin:dash:ops:*` 캐시를 선별 삭제합니다(최대 30초 지연, `window=all` 은 900초).

### 성능 메모

`not_sent` 는 캠페인 상세에서 쿼리 2회(사람별 플래그 집계 + 대표 실패 로그 DISTINCT ON),
`follow_up` 은 1회입니다. 대표 로그 조회는 **실패 계열 상태만** 스캔하므로 대상이 작습니다.
수신자 목록의 배지는 **현재 페이지 20행 범위**에서만 계산합니다(기존 `latest_status` 와 동일 방식).

---

## 남은 것 / 다음 라운드 후보

1. **7일 초과 원문 파싱** — prod census 후 확정 (§DM-9)
2. **`error_title` 기준 통일** — 최신 로그 → 최신 실패 로그로 바꿀지 (§DM-7 주의)
3. **전역 `?error_policy=` 필터** — 500쌍 상한을 없애려면 컬럼화 필요 (§DM-8)
4. **`GROUP_DISPLAY` 변경 여부** — 유저 콘솔 제품 결정 (§DM-12)

## 참고

- 서버 사전: `apps/admin_api/dm_error_catalog.py`
- 사람 단위 policy 집계: `apps/admin_api/dm_policy_rollup.py` (신규)
- 후속 DM 축: `apps/integrations/campaign_stats.py` (`followup_rollup` · `latest_followup_rows`)
- 테스트: `apps/admin_api/tests_dm_followup_policy.py` (20건, 계약 전부 고정) ·
  `apps/admin_api/tests_dm_error_policy.py` (분류 전수)
