# [백엔드 → 프론트] 추가 요청 3건 처리 완료 + 더미 계정 전체 복구

작성: 2026-08-14 (백엔드) · 대상: **dev** (`https://dev-api.turnflow.link`) — **이미 반영돼 있습니다.**
받은 문서: `backend-dm-migration (2).md` 의 뒤쪽 3개 절

> 보내주신 3건은 **전부 저희 쪽 결함이 맞았고 고쳤습니다.** 우회 코드는 지우셔도 됩니다.
> 더미 계정 4개도 다시 채워 뒀습니다.

---

## 1. `source` 가 캠페인 응답에 없던 건 — 고쳤습니다

**원인**: DB 에는 있었는데 시리얼라이저 `fields` 에서 빠져 있었습니다. 필터도 없었습니다.
지적하신 그대로입니다.

```
GET /api/v1/integrations/auto-dm-campaigns/?workspace_id={ws}
→ [{ ..., "source": "dm_migration", ... }]      # 불러온 캠페인
→ [{ ..., "source": "", ... }]                   # 직접 만든 캠페인
```

**필터도 넣었습니다** (안 쓰셔도 되지만 있습니다):

| 쿼리 | 결과 |
|---|---|
| `?source=dm_migration` | 불러온 캠페인만 |
| `?source=direct` | 직접 만든 것만 (`source: ""`) — 빈 문자열은 쿼리로 못 보내서 별칭을 뒀습니다 |
| `?source=아무거나` | **400** (다른 필터와 같은 정책) |

`source` 는 **읽기 전용**입니다. `PATCH` 로 보내도 무시되니 위조 걱정 안 하셔도 됩니다.

> ✅ **후보 역참조(`applied_campaign_id`) 우회는 지우셔도 됩니다.**
> 지적하신 "잡이 20건 밖으로 밀리면 배지가 조용히 사라진다" 는 실제 문제였습니다.
> `source` 는 캠페인에 영구히 붙어 있어 잡이 사라져도 남습니다.

---

## 2. `conflict_ack_at` 이 저장 안 되던 건 — 고쳤습니다

**원인**: 서버가 `conflict_ack` 만 읽고 있었고, 보내신 `conflict_ack_at` 은 조용히 버려졌습니다.
회신 문서에 필드명을 `conflict_ack` 으로만 적어둔 저희 잘못입니다.

**이제 `conflict_ack` · `conflict_ack_at` 둘 다 받습니다.** 지금 보내시는 그대로 두시면 됩니다.

```
POST .../jobs/prompt-answer/?workspace_id={ws}
  {"conflict_ack_at": "2026-08-14T11:36:00.000Z"}   ← 지금 보내시는 형태
→ 200 {"conflict_ack_at": "2026-08-14T20:59:21+09:00"}
GET  .../jobs/prompt-answer/?workspace_id={ws}
→ 200 {"conflict_ack_at": "2026-08-14T20:59:21+09:00"}   ← 남습니다
```

⚠️ **응답 값은 보내신 시각이 아니라 서버 시각입니다.** 클라이언트 시계를 신뢰하지 않기 때문이라
값은 무시하고 "확인했다" 는 사실만 받습니다. 화면에 시각을 표시하실 거면 **응답 값을 쓰세요.**

- `{"conflict_ack": false}` 또는 `{"conflict_ack_at": false}` → **해제**(null). 재테스트용입니다.
- 아예 안 보내면 기존 값 유지(부분 갱신). `prompt_answer` 만 보내도 확인 시각은 안 지워집니다.

> ✅ **localStorage 폴백(`turnflow_dm_import_conflict_ack:...`)은 지우셔도 됩니다.**
> 기기를 바꿔도 한 번만 묻습니다.

---

## 3. `mig-applied` 캠페인이 `active` 였던 건 — `inactive` 로 고쳤습니다

지적이 정확합니다. **실제 `apply` 는 항상 INACTIVE** 로 만드는데 시드만 `active` 였습니다.

```
mig-applied : 캠페인 5개 · status=inactive · source=dm_migration
```

이제 "켜기 전에 보기" 배지가 이 계정에서 보입니다.

---

## 4. 더미 계정 — 전부 다시 채웠습니다

말씀하신 4개 + **저희가 점검하다 추가로 발견한 2개**까지 새로 만들었습니다.
**`workspace_id` 는 6개 전부 그대로**입니다.

| 계정 | 지금 상태 |
|---|---|
| `mig-ready@turnflow.dev` | 후보 **13**개 (auto_draft 8 · needs_review 5 · 확인필요 5) |
| `mig-prefetched@turnflow.dev` | 후보 **4**개 (전부 적용 전) |
| `mig-partial@turnflow.dev` | 후보 **4**개 (전부 적용 전) |
| `mig-applied@turnflow.dev` | 후보 **5**개 전부 `applied` + 캠페인 5개 **inactive** |
| `mig-fresh@turnflow.dev` | 잡 **0**개 · 설문 미응답 ← 실패 잡이 붙어 있어 되돌림 |
| `mig-firsttime@turnflow.dev` | 잡 **0**개 · `prompt_answer: "first_time"` ← 〃 |

⚠️ **`job_id` 와 후보 `id` 는 새로 바뀌었고, 이 6개 계정은 재로그인이 필요합니다.**
(`workspace_id` 만 그대로입니다.)

### 🔎 `mig-fresh` · `mig-firsttime` 에 실패 잡이 생겼던 이유 (알아두시면 좋습니다)

이 두 계정에 `status: "failed"`(`token_expired`) 잡이 하나씩 남아 있었습니다.
**시작 흐름을 테스트하시면서 "불러오기"를 누르신 것으로 보입니다.**

> **더미 계정에서 분석 시작은 항상 실패합니다.** 목업 토큰이라 실제 인스타 호출이 안 됩니다.
> **버그가 아닙니다.** 실패까지 30초쯤 걸리고, `error.code: "token_expired"` 로 끝납니다.

그래서 이렇게 쓰시면 됩니다.

- **시작 → 로딩 → 실패** 흐름 테스트: `mig-fresh` 에서 눌러보세요 (실패 화면까지 실제로 검증됩니다)
- **시작 → 로딩 → 성공** 흐름: 더미로는 못 만듭니다 → **`mig-running`**(진행 중) ·
  **`mig-estimating`**(예상 계산 중) · **`mig-ready`**(완료) 를 갈아타며 보세요
- 한 번 누르면 `mig-fresh` 의 "시작 전" 상태는 사라집니다 → 되돌리려면 말씀 주세요

소진되면 **말씀만 주세요.** 이번에 계정별 복구가 가능해져서 다른 계정은 안 건드립니다.

---

## 5. 문서와 스키마가 어긋난다고 하신 건 (§2) — 답변

**평면 배열이 맞습니다. 페이지네이션은 없습니다.** `?page=1` 을 붙여도 배열로 옵니다(실측).

```
GET .../auto-dm-campaigns/?workspace_id={ws}&page=1
→ [{"id":"8a1d8004-...","ig_connection_id":"...", ...}]      ← 배열
```

Swagger 에 `page` 와 `{count,next,previous,results}` 가 보이는 건 **자동 생성 부작용**입니다 —
같은 ViewSet 의 **`logs` 액션**이 페이지네이션을 쓰기 때문에 목록에도 같이 그려졌습니다.
스키마에 경고 문구를 넣어 뒀습니다. **`search`·`status`·`trigger_type`·`created_after/before`·
`ordering`·`source` 는 전부 정상 동작**하니 그대로 쓰시면 됩니다.

건수가 많아 목록이 무거워지면 그때 페이지네이션을 붙이겠습니다(그 시점엔 미리 알려드립니다).

---

## 6. 확정본 흐름을 보고 저희가 확인한 것 (§0)

보내주신 화면 구성이 지금 백엔드와 어떻게 맞물리는지 점검했습니다. **레이아웃을 바꾸실 필요는 없고**,
아래 두 가지만 알고 계시면 됩니다.

### 6-1. `template_only` 밴드는 **더 이상 나오지 않습니다** — §6 은 통째로 지우셔도 됩니다

정밀도 재작성 이후 후보는 **게시물 단위 복원**으로만 만들어집니다. 게시물을 특정하지 못하면
후보 자체가 생기지 않습니다. 그래서 `template_only` 는 **구조적으로 0건**입니다.

- "6월쯤 보내던 문구예요" 시기 표시, 게시물 피커, `first_sent_at`/`last_sent_at` — **전부 불필요**합니다.
- 밴드는 실제로 **`auto_draft` / `needs_review`** 둘만 내려갑니다.
  (`excluded` 는 저장되지 않고, `template_only` 는 값만 남아 있는 유물입니다.)

### 6-2. "이미 있는 캠페인" 중복은 **분석 단계에서 원천 차단**됩니다 — §10 답변

우리 캠페인이 걸린 게시물은 **분석 대상에서 아예 빠집니다. 상태를 가리지 않습니다**
(활성 · 일시정지 · 비활성 · 완료 전부). 걱정하신 "일시정지 캠페인이 두 벌 생기는" 일은 없습니다.

> 이건 중복 방지 목적만이 아닙니다. 우리 캠페인이 도는 게시물은 발신 DM 의 절반이 **우리 것**이라
> (실측 164/313) 우리 DM 을 타사 캠페인으로 오인해 **자기 자신을 복제한 후보**가 생깁니다.

`existing_campaign` 필드는 그래도 남겨뒀습니다 — **분석 후 사용자가 직접 캠페인을 만든 경우**를
잡기 위한 응답 시점 재확인입니다. 자동 전체 적용 전에 이 값이 있으면 건너뛰시면 됩니다.

### 6-3. 자동 전체 적용 구성은 그대로 가셔도 됩니다

`auto_draft` 만 자동 적용하고 `needs_review` 는 남기신 판단이 저희 등급 기준과 정확히 맞습니다.
`auto_draft` 는 **같은 DM 을 받은 사람 비율(지지비율) 0.60 이상**만 들어가고, 실측에서 이 구간의
링크 정확도는 100% 였습니다. `needs_review`(= `confirm_required: true`)만 사용자에게
"이 링크가 맞나요?" 를 물으시면 됩니다.

---

## 검증

전부 `https://dev-api.turnflow.link` 실호출로 확인했습니다.

```
mig-applied  캠페인 5 · source=dm_migration · status=inactive
             ?source=dm_migration → 5      ?source=direct → 0
mig-ready    캠페인 1 · source=""
             ?source=direct → 1            ?source=dm_migration → 0
             ?source=bogus → 400
prompt-answer  POST {conflict_ack_at:"...Z"} → 저장됨 · GET 재조회 시 유지
               POST {conflict_ack_at:false} → null 로 해제
더미 10계정    전부 로그인 성공 · 상태 의도대로 (fresh/firsttime = 잡 0개)
자동 테스트    apps/integrations 344개 통과 (신규 2개 포함)
```
