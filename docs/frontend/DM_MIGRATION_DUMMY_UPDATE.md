# [백엔드 → 프론트] DM 캠페인 이전 더미 계정 — 복구 완료 + 계정 1개 추가

작성: 2026-08-14 (백엔드) · 대상: **dev** (`https://dev-api.turnflow.link`) — 이미 반영돼 있습니다.
선행 문서: `DM_MIGRATION_BACKEND_RESPONSE.md` → `DM_MIGRATION_SUPPLEMENT.md`

> 알려주신 **`mig-prefetched` · `mig-partial` 소진** 건입니다. 아래 3가지만 새로 확인해 주세요.
> 선행 문서 내용은 **전부 그대로 유효**합니다(바뀐 것 없음).

---

## 1. 소진된 2개 계정 — 복구했습니다

| 계정 | 지금 상태 |
|---|---|
| `mig-prefetched@turnflow.dev` | 후보 **4개**, 전부 `detected` (적용 전) |
| `mig-partial@turnflow.dev` | 후보 **4개**, 전부 `detected` (적용 전) |

- ✅ **`workspace_id` 는 그대로입니다.** 하드코딩하신 값 안 고치셔도 됩니다.
- ⚠️ **`job_id` 와 후보 `id` 는 새로 바뀌었습니다.** 저장해 두신 값이 있으면 버리고
  `jobs/` → `candidates/` 순서로 다시 조회해 주세요.
- ⚠️ **로그인 토큰도 무효**입니다. 이 두 계정은 **다시 로그인**해 주세요.

나머지 7개 계정은 **건드리지 않았습니다** — 하시던 테스트 그대로 이어가시면 됩니다.

---

## 2. `mig-applied` 계정을 새로 만들었습니다 (**계정 추가**)

"이미 다 불러왔어요" 화면을 보시려고 멀쩡한 계정에 `apply` 를 눌러 태우신 것으로 보입니다.
**그 상태 전용 계정이 없던 게 원인**이라, 새로 만들었습니다.

| 이메일 | `workspace_id` | 확인할 화면 |
|---|---|---|
| `mig-applied@turnflow.dev` | `8d6eb0d5-829b-57c4-a30e-0ce3a1f9cd0a` | **전부 적용 완료** — "이미 다 불러왔어요" |

비밀번호는 다른 더미와 같은 **`Test1234!`** 입니다.

담겨 있는 것:

- 후보 **5개 전부 `status: "applied"`**, 각각 `applied_campaign_id` 채워짐
- 그 후보로 만들어진 **실제 캠페인 5개**가 `source: "dm_migration"` 로 존재
  → **"불러온 캠페인" 배지·필터**도 이 계정에서 같이 확인하실 수 있습니다

```json
{ "status": "applied", "band": "auto_draft",
  "applied_campaign_id": "5a23ee81-2024-4ac3-adf5-da52dc276f32",
  "applied_at": "2026-08-14T17:05:33+09:00" }
```

집계(`candidates/summary/`)에서도 `by_status: { "applied": 5 }` 로 내려갑니다.

---

## 3. `apply` 는 되돌릴 수 없습니다 — 소진되면 말씀만 주세요

`apply` / `apply-all` 을 누른 후보는 `status: "applied"` 로 확정되고 **되돌리는 API 는 없습니다**
(운영에서 실수로 되돌리면 캠페인이 유실되므로 의도적으로 막아둔 부분입니다).

권장 사용법:

| 목적 | 쓰실 계정 |
|---|---|
| 적용 **전** 목록·필터·확인 화면 | `mig-ready`(13개) · `mig-prefetched`(4개) |
| 적용 **후** 화면 | **`mig-applied`** ← 태울 필요 없음 |
| `apply` 누르는 흐름 자체를 테스트 | **`mig-partial`**(4개) — 다 쓰고 요청 주세요 |

소진되면 **해당 계정만 30초 안에 되살립니다.** 다른 계정 상태는 그대로 둡니다.
(백엔드에서 `seed_dm_migration_dummy --only partial` 실행 — 이번에 계정별 재생성이 가능해졌습니다.)

복구 후에는 그 계정만 **재로그인 + `job_id` 재조회**가 필요합니다(§1 과 동일).

---

## 검증 결과

`https://dev-api.turnflow.link` 실제 호출로 확인했습니다.

```
mig-ready       후보 13  detected   ← 재생성 대상 아님. 그대로 보존된 것 확인
mig-prefetched  후보  4  detected   ← 복구
mig-partial     후보  4  detected   ← 복구
mig-applied     후보  5  applied    ← 신규 (캠페인 5개 · source=dm_migration)
```
