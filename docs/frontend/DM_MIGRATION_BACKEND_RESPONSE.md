# [백엔드 회신] DM 캠페인 이전 — 요청 13건 답변 + 변경된 계약

작성: 2026-08-14 (백엔드) · 원문: `backend-dm-migration.md`(프론트, 2026-08-14)
마이그레이션: `integrations 0050_dm_migration_precision`

> **먼저 알아두실 것 — 복원 방식이 바뀌었습니다.**
> 실서버 계정 4곳·게시물 1,000여 개로 연구한 결과, 기존 파이프라인이 **발신 DM 의 67~100%를
> 못 읽고 있었습니다**(버튼 DM 은 본문이 `message` 가 아니라 첨부 안에 있습니다).
> 고친 뒤 **캠페인 게시물 복원율 99%+ · 오퍼 링크 확보 100%** 가 됐습니다.
> 그 과정에서 후보의 **신뢰도 판정 방식이 바뀌었고**, 그게 아래 여러 답변의 배경입니다.
> 상세: `docs/system/DM_MIGRATION_PRECISION_FINAL.md`(연구 보고서 시리즈)

---

## 0. 질문 13개 요약 답변

| # | 질문 | 답 |
|---|---|---|
| 1 | 후보 목록 `page`·`search`·`ordering`·`media_after/before`, `summary` | ✅ **전부 추가** (§2) |
| 2 | `auto-dm-campaigns` 페이지네이션 있나 | ✅ **있습니다** — `{count,next,previous,results}` (§7) |
| 3 | 캠페인에 출처 표시 | ✅ **`source: "dm_migration"`** 추가 (§7) |
| 4 | `attachment_image` 판정 가능한가 | ✅ **가능** — 첨부를 직접 읽습니다 (§4) |
| 5 | `backfill_existing_comments` | ✅ **(A) 서버 강제 `false`** (§5) |
| 6 | 일괄 적용 엔드포인트 | ✅ **`apply-all` 추가** (§6) |
| 7 | `template_only` 에 `first/last_sent_at` 채워지나 | ⚠️ **밴드 자체를 없앴습니다** (§8) |
| 8 | `follow_up_candidates` 매핑 (A/B/C) | ✅ **(A) 채택 — 단, 게이트를 실제로 복원해서** (§9) |
| 9 | `trigger_type` 감지 | ❌ **불가** — `specific_media` 고정 (§10) |
| 10 | 플래그 2개 얹을 자리 | ✅ **`prompt-answer` 엔드포인트** (§11) |
| 11 | `media_limit` 안 보내도 되나 / `progress` 단조 증가하나 | ✅ **안 보내도 됩니다. 전체를 봅니다** / ✅ 단조 증가 (§12) |
| 12 | 이미 있는 캠페인(특히 일시정지) 처리 | ✅ **분석 대상에서 제외 + `existing_campaign` 필드** (§13) |
| 13 | "안 되는 것" 목록 중 틀린 항목 | ⚠️ **2개 정정** — 팔로우 게이트와 링크 버튼은 **됩니다** (§14) |

**추가로 알려드릴 변경 3가지** — §1 (연동 즉시 선분석) · §3 (링크 확인 화면) · §15 (예상 시간)

---

## 1. 🆕 연동 즉시 백그라운드 선분석 — 대기 시간이 사라집니다

분석은 계정당 **4~40분**이 걸립니다(게시물 수에 비례). 사용자가 「불러오기」를 누른 **뒤에**
시작하면 그만큼 기다려야 합니다.

→ **IG 연동이 완료되는 순간 서버가 알아서 분석을 시작합니다.** 결과는 **7일** 캐시됩니다.

```
IG 연동 완료 ──▶ (서버) 자동 분석 시작 ······· 4~40분
                                              ↓
사용자가 "불러오기" 클릭 ──▶ 이미 끝나 있으면 **즉시 결과**
```

### 프론트가 할 일

`POST /dm-migration/jobs/` 를 **그대로** 호출하시면 됩니다. 서버가 알아서 판단합니다.

| 서버 상태 | 응답 | 화면 |
|---|---|---|
| 선분석이 이미 끝나 있음 | `200` + `reused: true` + `status: "ready"` | **바로 결과 목록** |
| 선분석이 돌고 있음 | `200` + `reused: true` + `status: "running"` | 진행바 (남은 시간만큼만) |
| 캐시 없음(7일 경과 등) | `201` + `reused: false` | 진행바 (처음부터) |

미리 알고 싶으면 `GET /dm-migration/jobs/prompt-answer/` 의 **`prefetched_job`** 을 보세요 —
값이 있으면 "기다림 없음"이라 안내 문구를 다르게 쓸 수 있습니다.

- **전 플랜 사용 가능**(무료 포함). 요금제 게이트 없습니다.
- 재분석(`force: true`)은 **6시간** 쿨다운. 캐시 재사용 창은 **7일**입니다.
  (기존 24시간/1시간에서 늘었습니다 — 분석 1회가 수천 API 호출이라)

---

## 2. ✅ [1순위] 후보 목록 — 페이지네이션·검색·정렬 추가

```
GET /api/v1/integrations/dm-migration/jobs/{id}/candidates/
  ?workspace_id=...
  &page=1&page_size=20            # page_size 최대 100, 기본 20
  &search=룩북                     # draft_name · draft_opening_message
                                  #  · media_caption_excerpt · offer_button_label 부분일치
  &ordering=-media_timestamp      # 허용 목록 밖이면 400
  &media_after=2026-06-01&media_before=2026-06-30
  &band=auto_draft&status=detected
  &needs_confirm=true             # 🆕 링크 확인이 필요한 후보만
  &view=list                      # 🆕 큰 필드 제외(경량)
```

**응답**: `{ count, next, previous, results: [...] }`

**정렬 허용 목록**: `media_timestamp` · `confidence` · `support_score` · `draft_name` · `created_at`
(각각 `-` 내림차순, 기본 **`-media_timestamp`**)

`view=list` 를 주면 `evidence_raw` · `evidence_aggregates` · `follow_up_candidates` ·
`matched_template` 이 빠집니다. 목록은 이걸 쓰시는 걸 권합니다.

### 집계

```
GET /dm-migration/jobs/{id}/candidates/summary/?workspace_id=...
→ {
    "total": 62,
    "by_band":   { "auto_draft": 36, "needs_review": 20, "excluded": 6 },
    "by_status": { "detected": 52, "applied": 8, "dismissed": 2 },
    "needs_confirm": 20,          // 🆕 링크 확인이 필요한 개수
    "with_offer_url": 41,         // 🆕 자료 링크를 확보한 개수
    "media_date_range": { "first": "2026-03-02", "last": "2026-08-11" }
  }
```

---

## 3. 🆕 후보 응답에 `offer` 와 `support` 가 생겼습니다 — 화면의 중심

이 기능의 **산출물 1순위는 인플루언서가 보내려던 자료 링크**입니다. 문구는 조금 달라도 되지만
링크가 틀리면 캠페인이 망가집니다. 그래서 링크를 최상위로 올렸습니다.

```json
{
  "offer": {
    "url": "https://myshop.co.kr/lookbook",   // 복원(또는 사용자가 확정)한 자료 링크
    "button_label": "자료 받기",
    "confirmed": false,                        // 사용자가 확인했는가
    "edited": false                            // 사용자가 링크를 고쳤는가
  },
  "support": { "hits": 8, "probed": 10, "score": 0.72 },
  "confirm_required": false,
  "gate_detected": true,
  "transfer": { "coverage": "full", "drops": [] }
}
```

### `support` — 이게 신뢰도의 근거입니다

같은 게시물에 댓글 단 사람 여러 명이 **같은 DM 을 받았을수록** 그 게시물의 캠페인이 맞습니다.
1~2명에게만 간 DM 은 **86%가 다른 게시물 캠페인에서 흘러든 것**이었습니다(실측).

| `score` | 밴드 | 화면 |
|---|---|---|
| 0.60 이상 | `auto_draft` | 자동 적용 (정밀도 실측 100%) |
| 0.40~0.60 | `needs_review` | 만들되 확인 배지 (77%) |
| 0.40 미만 | `needs_review` + `confirm_required` | **링크 확인을 받고 나서** |

> `score` 는 표본 크기를 함께 반영합니다(Wilson 하한). `1/2`(50%)는 0.09,
> `10/10`(100%)은 0.72 입니다. **비율만 보고 판단하지 마세요.**

### 🔴 확인 화면은 "링크가 맞나요?" 하나면 됩니다

`hits`·`probed`·수신자 댓글 같은 내부 근거는 **화면에 노출하지 말아 주세요**(방식 노출 우려).
보여줄 것은 **게시물 + 링크** 뿐입니다.

```
[게시물 썸네일] 6월 12일 게시물
이 게시물에서 보내시던 자료 링크가 맞나요?
🔗 https://myshop.co.kr/lookbook

[맞아요]  [다른 링크예요 ✏️]  [이 게시물은 캠페인이 아니에요]
```

```
POST /dm-migration/candidates/{id}/confirm-link/?workspace_id=...

{}                                        → 복원된 링크로 확정
{"url": "https://..."}                    → 링크 교체
{"url": ""}                               → "링크 없음" 으로 확정
{"correct": false}                        → 후보를 무시(dismiss)
```

응답은 후보 객체입니다(`offer.confirmed=true`, `confirm_required=false`).
**확정한 링크는 `apply` 시 링크 버튼으로 들어갑니다.**

물어볼 대상은 `confirm_required=true` 인 후보뿐이고, 개수는 `summary.needs_confirm` 입니다.

---

## 4. ✅ [1순위] `transfer.drops` — 판정 가능합니다

첨부를 직접 읽으므로 **원본 DM 에 사진이 붙어 있었는지 판정됩니다.**

```json
"transfer": {
  "coverage": "full" | "partial",
  "drops": [{ "code": "attachment_image", "count": 3 }]
}
```

| code | 판정 |
|---|---|
| `attachment_image` / `attachment_video` / `attachment_file` | ✅ 첨부 종류로 직접 판정 |
| `carousel` | ✅ 첨부 2장 이상이면 표시 |
| `message_sequence` | ✅ 게이트 DM 은 찾았는데 뒤따르는 오퍼 DM 을 못 찾은 경우 |
| `link_buttons_overflow` | ✅ 버튼 3개 초과 시 |
| `delay_between_messages` · `quick_replies` · `personalization_vars` · `ab_test` | ❌ 판정 불가(안 내려갑니다) |
| `opening_too_long` | ❌ **안 내려갑니다** — 아래 §4-1 |

모르는 code 는 안 보내니 **일반 폴백 문구는 준비만 해두시면** 됩니다.

### 4-1. 글자수 잘림 — 화면에 안 띄우셔도 됩니다

인스타 한도는 **버튼 없으면 UTF-8 1000바이트(한글 약 333자), 버튼 카드면 640자**입니다.
Meta 정책이라 어느 서비스든 동일하고, 타사가 더 길게 보내는 건 **여러 통으로 쪼개기** 때문입니다
(실측: 복원한 타사 캠페인이 게이트 DM + 오퍼 DM **2통** 구조였습니다).

**서버가 잘린 문구를 내보내지 않습니다.** 3중으로 막습니다:

1. **원본이 클릭 게이트 구조면** 게이트 DM + 리워드 DM 으로 나눠 복원 → 각각 640자를 씁니다.
   (우리가 2통을 보내는 경로는 **팔로우 게이트 하나뿐**입니다 — 버튼을 눌러야 두 번째가 나갑니다.
   버튼 없이 연속 2통을 보내던 캠페인은 **1통으로 합쳐집니다.**)
2. **복원한 링크를 본문이 아니라 버튼으로** 올림 → 한도가 333자 → **640자로 늘어남**
3. 그래도 길면 **초안 생성 단계가 한도 안에서 다시 씀** (LLM 실패 시엔 규칙 기반 짧은 초안)

→ `draft_opening_message` 는 **항상 한도 안**이고, `opening_too_long` 은 내려가지 않습니다.
"뒷부분이 잘려요" 안내 문구는 만들지 않으셔도 됩니다.

---

## 5. ✅ [1순위] `backfill_existing_comments` — **(A) 서버 강제 `false`**

`apply` / `apply-all` 로 만들어지는 캠페인은 **항상 `false`** 입니다. 오버라이드도 안 받습니다.

> 이전 대상 게시물의 과거 댓글 작성자는 **이미 예전 서비스로 DM 을 받은 사람들**입니다.
> 켜면 최대 500명에게 같은 DM 이 두 번째로 갑니다. 사용자가 정말 원하면 캠페인 수정에서
> 직접 켤 수 있습니다.

테스트로 고정해 뒀습니다(`test_apply_forces_backfill_off_and_promotes_link_to_button`).

---

## 6. ✅ [1순위] 일괄 적용

```
POST /dm-migration/jobs/{id}/apply-all/?workspace_id=...
body: { "bands": ["auto_draft"] }          # 기본값. needs_review 도 넣을 수 있음
→ {
    "applied": [{ "candidate_id": "...", "campaign_id": "..." }],
    "failed":  [{ "candidate_id": "...", "code": "validation_error", "message": "..." }],
    "skipped": 3                            // 이미 적용된 후보
  }
```

- `media_id` 가 없는 후보는 **자동으로 제외**됩니다(게시물 특정 불가).
- **건별 성공/실패**가 함께 오므로 부분 성공을 그대로 그리시면 됩니다.
- 재호출해도 안전합니다(이미 적용분은 `skipped`).

---

## 7. ✅ [1순위] 캠페인 목록 — 페이지네이션 **있습니다** + 출처 표시

`GET /api/v1/integrations/auto-dm-campaigns/` 는 **`{count, next, previous, results}`** 입니다.
설명 문구가 잘못돼 있었습니다(수정 예정). `page` · `page_size` 둘 다 먹습니다.

**출처 필드를 추가했습니다:**

```json
{ "source": "dm_migration" }   // 빈 문자열이면 사용자가 직접 만든 캠페인
```

`?source=dm_migration` 필터도 씁니다. 「불러온 캠페인」 배지·필터를 이걸로 그리시면 됩니다.

---

## 8. ⚠️ `template_only` 밴드는 없어졌습니다

**이유**: 새 방식은 **게시물 단위로만** 복원합니다. "문구는 찾았는데 어느 게시물인지 모름"
상태가 원리적으로 생기지 않습니다 — 게시물의 댓글러를 조회해서 DM 을 찾기 때문입니다.

→ 프론트의 **「게시물 없음」 필터 칩과 게시물 피커 화면은 필요 없습니다.**
남는 밴드는 **`auto_draft` · `needs_review` · `excluded`** 셋입니다.

(`excluded` 는 캠페인 흔적은 있는데 DM 을 못 찾은 게시물입니다. 목록에 보여주고
"이 게시물의 DM 문구를 붙여넣어 주세요" 로 안내하시면 좋습니다.)

---

## 9. ✅ `follow_up_candidates` → **(A) 채택. 다만 진짜 게이트를 복원합니다**

프론트가 걱정하신 "버튼 클릭이 끼어들어 원본과 다르게 동작" 문제가 **없습니다.**
원본이 실제로 팔로우 게이트를 쓰고 있었는지 **판정할 수 있게 됐기 때문**입니다.

- 링크 없는 버튼(`postback`)이 붙은 DM = **팔로우 확인 게이트** → `gate_detected: true`
- 링크 있는 버튼(`web_url`)이 붙은 DM = **오퍼(자료) DM**

`apply` 시 `gate_detected=true` 면 **복원된 게이트 문구·버튼 라벨 그대로** 게이트를 켜고,
오퍼 DM 을 `reward_message_template` 에 넣습니다. **원본 2단 구조가 그대로 재현됩니다.**

`follow_up_candidates` 는 이제 빈 배열로 나갑니다(위 경로로 대체). 화면에서 빼셔도 됩니다.

---

## 10. ❌ `trigger_type` 감지는 불가

`any_media`·`story_reply` 였는지는 **발신 DM 기록만으로 판별할 수 없습니다.**
모든 후보는 `specific_media` 로 생성됩니다. 사용자가 필요하면 수정에서 바꿉니다.

---

## 11. ✅ 플래그 2개 — 전용 엔드포인트

```
GET  /dm-migration/jobs/prompt-answer/?workspace_id=...
POST /dm-migration/jobs/prompt-answer/?workspace_id=...

body: { "prompt_answer": "used" | "first_time" }   // 설문 답
      { "conflict_ack": true }                      // "타 서비스 해제했어요" 확인

→ {
    "prompt_answer": "used",
    "prompt_answered_at": "2026-08-14T...",
    "conflict_ack_at": null,
    "prefetched_job": { ... } | null      // 🆕 선분석 결과(§1)
  }
```

IG 연결 단위로 서버에 저장되므로 **기기가 바뀌어도 다시 묻지 않습니다.**

---

## 12. ✅ 분석 범위와 진행률

- **`media_limit` 안 보내셔도 됩니다.** 이제 **계정 전체 게시물**을 봅니다
  (댓글 8개 이상인 것만 대상). → 문구는 **"계정 전체를 봤어요"** 가 맞습니다.
- **`progress` 는 단조 증가합니다.** 되돌아가지 않습니다.
  `8`(예상 계산) → `15~85`(게시물별 복원, 선형) → `90`(초안) → `100`.
- 중간에 오래 멈추지 않습니다 — 게시물 5개마다 갱신됩니다.

---

## 13. ✅ 이미 있는 캠페인 — 아예 후보로 안 나옵니다

**분석 단계에서 제외합니다.** 같은 게시물에 우리 캠페인이 있으면(상태 무관 — `active` ·
`paused` · `inactive` 전부) 그 게시물은 **조회조차 하지 않습니다.**

> 이유: 그 게시물에서 발견되는 발신 DM 의 **절반이 우리 것**이었습니다(실측 164/313).
> 우리 DM 을 "타사 캠페인"으로 오인해 자기증식 후보가 생깁니다.

그래도 **분석 후에 사용자가 캠페인을 만들 수 있으므로** 응답 시점에 한 번 더 확인합니다:

```json
"existing_campaign": { "id": "…", "name": "가을 신상 룩북", "status": "paused", "source": "" }
```

값이 있으면 자동 적용에서 빼고 "이미 쓰고 계신 캠페인이에요" 로 표시해 주세요.

---

## 14. ⚠️ "안 되는 것" 목록 — 2개 정정

| 항목 | 프론트 기재 | 정정 |
|---|---|---|
| 이미지/동영상/파일 DM | 못 옮김 | ✅ 맞습니다 (단, **있었다는 사실은 `drops` 로 알려드립니다**) |
| 이미지 캐러셀 | 못 옮김 | ✅ 맞습니다 |
| 조건 분기 | 팔로우 게이트 2갈래만 | ⚠️ **그 게이트를 복원합니다** (§9) |
| 메시지 사이 대기 시간 | 못 옮김 | ✅ 맞습니다 |
| 태그/커스텀 필드 | 못 옮김 | ✅ 맞습니다 |
| 링크 버튼 | (언급 없음) | 🆕 **원본 DM 의 URL 을 버튼으로 승격합니다** — 본문에 URL 을 직박으면 스팸으로 잡히므로 |

---

## 15. 🆕 예상 시간 — 2단계로 나눴습니다

분석을 시작하면 서버가 **먼저 게시물 수를 세어 예상 시간을 계산**하고(수 초), 그다음 본
분석에 들어갑니다. 프론트는 "약 N분 걸려요" 를 먼저 띄울 수 있습니다.

```json
// GET /dm-migration/jobs/{id}/
{
  "stage": "estimating",          // → collecting_targeted_dms → generating_drafts → completed
  "progress": 8,
  "estimate": {
    "seconds": 252,               // 예상(하한)
    "seconds_max": 420,           // 예상(상한) — 복원 실패가 많으면 여기까지
    "media_with_comments": 42,
    "computed_at": "2026-08-14T..."
  },
  "trigger_source": "auto_connect"   // 연동 직후 자동 선분석이면 auto_connect
}
```

- `estimate` 는 **`stage=estimating` 이 끝난 뒤** 채워집니다. 그전엔 `null` — **null 체크 필수.**
- 게시물 50개 ≈ **4~8분**, 400개 ≈ **40~70분**입니다.
- "최대 20분" 상한 표시는 **더 이상 맞지 않습니다**(게시물 많은 계정은 넘습니다).
  `estimate.seconds_max` 를 쓰시거나, 선분석 덕분에 대부분 기다림이 없다는 점을 활용해 주세요.

---

## 16. 배포 순서

1. **§5(backfill)·§13(중복 방지)** 은 이미 서버에 들어가 있습니다 — 프론트 배포 전에 준비 완료.
2. §2(목록)·§6(apply-all)·§3(링크 확인)·§11(플래그) 모두 사용 가능합니다.
3. §8 때문에 **`template_only` 관련 화면은 제거**해 주세요(빈 밴드).
4. §15 때문에 **"최대 20분" 문구 교체**가 필요합니다.

문의는 백엔드로 주세요. 스키마는 `/api/docs/` 의 `DM Migration` 태그에서 확인하실 수 있습니다.
