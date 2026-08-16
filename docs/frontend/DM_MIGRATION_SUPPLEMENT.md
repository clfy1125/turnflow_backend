# [백엔드 보완 전달] DM 캠페인 이전 — 추가 확정 3건 + UI 상태별 더미 계정

작성: 2026-08-14 (백엔드) · 선행 문서: `DM_MIGRATION_BACKEND_RESPONSE.md`
대상 서버: **dev** (`https://dev-api.turnflow.link`) — 이미 반영돼 있습니다.

> 앞서 드린 회신 이후 **확정된 3건**과, 화면 상태를 바로 확인하실 수 있는
> **더미 계정 9개**를 전달드립니다. 선행 문서와 겹치는 내용은 뺐습니다.

---

## 1. 글자수 잘림 — 안내 문구 만들지 않으셔도 됩니다

회의에서 나온 "우리만 잘리는 것 같다" 건입니다. 결론부터:

**인스타(Meta) 정책 한도라 어느 서비스든 같습니다.**

| DM 형태 | 한도 |
|---|---|
| 버튼 없는 일반 텍스트 | UTF-8 **1000바이트** = 한글 약 **333자** |
| 버튼 카드(링크 버튼 또는 팔로우 확인) | **640자** |

타사가 더 길게 보내는 건 **여러 통으로 쪼개기** 때문입니다. 실서버 복원에서 타사 캠페인이
게이트 DM + 오퍼 DM **2통 구조**로 나가는 것을 확인했습니다.

### 이전 기능은 이렇게 처리합니다

1. 원본이 **클릭 게이트 구조**면 → 게이트 DM + 리워드 DM 으로 나눠 복원(각 640자)
2. 복원한 링크를 **본문이 아니라 버튼으로** 올림 → 한도가 333자 → **640자로 늘어남**
3. 그래도 길면 → **초안 생성 단계가 한도 안에서 다시 씀**(LLM 실패 시엔 규칙 기반 짧은 초안)

→ **`draft_opening_message` 는 항상 한도 안입니다. `transfer.drops` 에 `opening_too_long`
은 내려가지 않습니다.** "뒷부분이 잘려요" 안내는 준비하지 않으셔도 됩니다.

> ⚠️ 다만 우리가 2통을 보내는 경로는 **팔로우 게이트 하나뿐**입니다(버튼을 눌러야 두 번째가 나감).
> 버튼 없이 연속 2통을 보내던 캠페인은 **1통으로 합쳐집니다** — 이건 못 옮기는 항목이 맞습니다.

---

## 2. 무료 플랜에서도 그대로 이전됩니다

요금제 `features` 키를 전수 확인한 결과, **팔로우 게이트 · 링크 버튼 · 공개답글 · 오프닝
회전은 요금제 기능이 아닙니다**(캠페인 설정일 뿐). 캠페인 생성 경로에도 플랜 검사가 없습니다.

- 무료 플랜 사용자도 **게이트 달린 캠페인을 그대로 불러오고 켤 수 있습니다.**
- 실제로 걸리는 건 **월 DM 발송 한도**(`dm_monthly_limit`) 하나뿐입니다.
- 분석(잡 실행) 자체도 전 플랜 허용입니다.

→ 프론트에서 **플랜별 분기·업셀 게이트를 넣지 않으셔도 됩니다.** 발송 한도에 걸릴 때만
기존 한도 안내가 뜨면 됩니다.

---

## 3. 모델 정책 — 신경 안 쓰셔도 됩니다

분석에 쓰는 LLM 은 `deepseek` 로 고정 운영합니다(내부 GPU 부하 회피). 요청에 `llm_model` 을
보내실 필요 없습니다. 그리고 이번 재작성으로 **LLM 사용이 초안 문구 작성 1단계로 줄었습니다** —
링크·버튼 문구·게이트 구조·트리거 키워드는 전부 **관측값**이라 모델 상태와 무관하게 정확합니다.

---

## 4. UI 상태별 더미 계정 9개 (dev)

로그인만 바꿔가며 **각 화면 상태를 바로** 보실 수 있습니다.
비밀번호는 전부 **`Test1234!`** 이고, 워크스페이스는 계정당 1개입니다.

| 이메일 | `workspace_id` | 확인할 화면 |
|---|---|---|
| `mig-ready@turnflow.dev` | `2eb847c1-6b0b-5c0c-8402-db9707c1dcc0` | **완료** — 후보 13개. 목록·필터칩·페이지네이션·일괄적용 |
| `mig-running@turnflow.dev` | `91850b4b-e6a3-551d-b547-378a11179af4` | **진행 중** — `progress: 42`, `estimate` 있음 → 진행바 |
| `mig-estimating@turnflow.dev` | `aa6842b7-6517-5bc8-a12b-c52023726b92` | **예상시간 계산 중** — `estimate: null` → **null 처리 확인용** |
| `mig-prefetched@turnflow.dev` | `19819beb-e1f5-506c-aede-422b82eee22f` | **선분석 완료** — `trigger_source: auto_connect`. 기다림 없이 결과 |
| `mig-partial@turnflow.dev` | `75b85947-1e1c-5b0c-9231-2463e5728748` | **부분 완료** — 경고 배너 + 결과 동시 표시 |
| `mig-failed@turnflow.dev` | `78489e9a-07d3-5f64-8e1e-7ca378429792` | **실패** — `error.code: token_expired` → 에러/재시도 |
| `mig-empty@turnflow.dev` | `ceebd1fa-534e-524b-aab1-45e237356f20` | **후보 0개** — 빈 상태 화면 |
| `mig-fresh@turnflow.dev` | `f6ed1caa-8736-5bad-a905-8cbeef3357f3` | **시작 전** — 잡 없음 + 설문 미응답 → 설문/시작 화면 |
| `mig-firsttime@turnflow.dev` | `19c0395c-a54c-5b1a-ad4b-2caf2dd30cc7` | **"처음이에요" 응답함** — 안내가 다시 안 뜨는지 |

> ✅ **`workspace_id` 는 고정값입니다.** 더미를 재생성해도 바뀌지 않으니 그대로 하드코딩하셔도 됩니다.
> 단 **`job_id` 와 후보 `id` 는 재생성 때마다 바뀝니다** — 아래 호출 순서대로 조회해서 쓰세요.

### `mig-ready` 계정에 담긴 후보 구성

목록 화면의 모든 분기를 한 계정에서 볼 수 있게 섞어 뒀습니다.

| 후보 | 개수 | 확인 포인트 |
|---|---|---|
| `band: auto_draft` | 8 | 자동 적용 대상 |
| `band: needs_review` + `confirm_required: true` | 5 | **링크 확인 화면** |
| `transfer.drops` 있음 | 1 | "사진 2장은 못 옮겨요" + "카드 넘김" 배지 |
| `offer.url` 없음 | 1 | 링크를 못 찾은 후보(게이트만 복원) |
| `existing_campaign` 채워짐 | 1 | "이미 쓰고 계신 캠페인이에요" (일시정지 상태) |

실제 응답 예시(`?view=list`):

```json
{
  "band": "auto_draft",
  "offer": { "url": "https://example.com/lookbook-2026fw", "button_label": "룩북 받기",
             "confirmed": false, "edited": false },
  "support": { "hits": 8, "probed": 10, "score": 0.72 },
  "transfer": { "coverage": "full", "drops": [] },
  "confirm_required": false,
  "gate_detected": true,
  "existing_campaign": null
}
```

집계(`candidates/summary/`):

```json
{ "total": 13, "by_band": { "auto_draft": 8, "needs_review": 5 },
  "needs_confirm": 5, "with_offer_url": 12,
  "media_date_range": { "first": "2026-07-08", "last": "2026-08-13" } }
```

### 호출 순서

```
POST /api/v1/auth/login/                     { email, password }  → tokens.access
GET  /api/v1/integrations/dm-migration/jobs/?workspace_id={ws}    → 잡 목록
GET  .../jobs/{job_id}/candidates/summary/?workspace_id={ws}      → 필터칩 개수
GET  .../jobs/{job_id}/candidates/?workspace_id={ws}&view=list&page_size=20
GET  .../jobs/prompt-answer/?workspace_id={ws}                    → 설문/선분석 상태
```

### ⚠️ 더미 데이터 주의

- **화면 확인 전용**입니다. 실제 인스타 계정이 아니라 `apply` 로 캠페인을 만들 수는 있지만
  **켜도 DM 은 나가지 않습니다**(mock 토큰).
- `offer.url` 은 전부 `example.com` 입니다.
- 데이터가 지저분해지면 백엔드에 말씀 주세요. 한 줄로 재생성됩니다
  (`python manage.py seed_dm_migration_dummy`). **재생성해도 `workspace_id` 는 유지됩니다.**
- **dev 전용**입니다(운영에서는 실행 자체가 거부됩니다).

### 검증 완료

작성 시점에 `https://dev-api.turnflow.link` 를 통해 실제로 호출해 확인했습니다.

```
mig-ready       jobs → summary(total=13 · needs_confirm=5 · with_offer_url=12)
                     → candidates(count=13 · page_size 동작 · next 있음)
                     → prompt-answer(prefetched_job 채워짐)
mig-estimating  status=running · progress=8 · estimate=null
mig-prefetched  status=ready  · progress=100 · estimate 있음
mig-failed      status=failed · progress=35
mig-empty       status=ready  · 후보 0개
```

---

## 5. 선행 문서에서 바뀐 것 (요약)

| 항목 | 선행 문서 | 지금 |
|---|---|---|
| `opening_too_long` | "발생하지 않습니다"(근거 없이) | **확실히 안 내려갑니다** — §1 의 3중 처리 |
| 무료 플랜 | 언급 없음 | **전 기능 이전 가능** — §2 |
| `llm_model` 파라미터 | 선택 가능 | **보내지 마세요** — deepseek 고정 §3 |

나머지(목록 페이지네이션 · `summary` · `apply-all` · `confirm-link` · `prompt-answer` ·
`backfill` 강제 OFF · `existing_campaign` · `source`)는 **선행 문서 그대로**이고 dev 에
반영돼 있습니다.
