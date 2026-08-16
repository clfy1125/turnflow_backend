# [백엔드 → 프론트] 2026-08-16 요청 회신 — §A 반영 · §B/§C 확답 · 스펙 정정

작성: 2026-08-16 (백엔드) · 받은 문서: `backend-dm-migration (3).md`
대상 서버: **dev** (`https://dev-api.turnflow.link`) — 아래는 **전부 이미 반영**돼 있습니다.

> **먼저 사과드립니다.** §C(`backfill_existing_comments`)는 **2026-08-14 첫 회신
> `DM_MIGRATION_BACKEND_RESPONSE.md` 에 이미 "서버 강제 false" 로 적어 드렸는데**,
> 그 문서가 §4 질문에 대한 답이라는 걸 알아보기 어렵게 써서 미회신처럼 보였습니다.
> **코드는 처음부터 (A) 로 들어가 있었습니다** — 아래 §C 에 실측 증거를 붙였습니다.

| 항목 | 결론 |
|---|---|
| §A 삭제 시 이력 소실 | **(A) 채택 — `applied_at` 보존.** 반영 완료 |
| §B 08-15 → 08-16 동작 변화 | **의도된 변경입니다.** 계약으로 확정 |
| §C `backfill_existing_comments` | **(A) 서버 강제 `false` — 이미 그렇게 동작 중** |
| §D `mig-applied` 재시드 | 완료 |
| §E 회신 항목 9건 | 아래 전부 답변 |
| §F 스펙 정정 16건 | 9건 코드로 수정 · 나머지는 설명 |

---

## A. `applied_at` 을 보존하도록 고쳤습니다 ✅

`status` 는 `detected` 로 되돌리되(재적용을 위해), **`applied_at` 은 남깁니다.**

```
판별식:  status == "detected" && applied_at != null   →  "불러왔다가 사용자가 지운 것"
         status == "detected" && applied_at == null   →  "한 번도 안 불러온 것"
```

dev 실측(`mig-applied`):

```
캠페인 삭제 전:  status=applied   applied_at=2026-08-16T11:26:48  applied_campaign_id=<uuid>
캠페인 삭제 후:  status=detected  applied_at=2026-08-16T11:26:48  applied_campaign_id=null
```

**`dismiss` 우회를 걷어내셔도 됩니다.** 「N개 찾음 · 불러오기」 배너에서 `applied_at != null` 인
후보를 빼시면 사용자의 삭제 결정이 유지됩니다.

> 별도 상태값(B안)은 채택하지 않았습니다 — enum 이 늘면 양쪽 동시 배포가 필요한데,
> `applied_at` 만으로 구분이 되므로 그 비용을 치를 이유가 없습니다.

---

## B. 08-15 의 409 → 08-16 의 `detected` 복귀는 **의도된 변경**입니다 ✅

그 사이에 서버가 바뀐 게 맞습니다. **2026-08-15 에 저희가 고쳤습니다.**

08-15 에 보신 상태가 원래 결함이었습니다 — 캠페인은 0개인데 후보는 `applied` 라
「이미 다 불러왔어요」만 뜨고 **재적용도 dismiss 도 막힌 막다른 상태**였습니다.

### 계약으로 확정합니다

> **적용했던 캠페인이 삭제되면 그 후보는 다시 `apply` 할 수 있습니다.**
> 목록·집계·일괄적용을 조회하는 시점에 서버가 자동으로 `detected` 로 되돌립니다.

dev 실측 — 삭제된 캠페인의 후보를 그대로 재적용:

```
POST candidates/{id}/apply/  →  201  campaign=8e4c9672-…   (새 캠페인 생성됨)
```

**`POST jobs/ {force:true}` 재분석 경로는 걷어내셔도 됩니다.** 수 분 대기·IG API 재소모·
쿨다운 429 없이 즉시 복구됩니다.

---

## C. `backfill_existing_comments` — **(A) 서버 강제 `false`, 이미 적용돼 있습니다** ✅

`apply` 는 후보에서 캠페인을 만들 때 이 값을 **덮어씁니다.** 프론트가 보낼 수단이 없는 게
맞고, **보낼 필요도 없습니다.**

[`migration_views.py` `apply_candidate()`]
```python
payload = {
    ...
    "backfill_existing_comments": False,  # ← 서버 강제
}
```

dev 실측 — 방금 후보 하나를 `apply` 해서 만들어진 캠페인:

```
POST candidates/{id}/apply/ → 201
GET  auto-dm-campaigns/{id}/
→ backfill_existing_comments = false
  status = inactive · source = "dm_migration" · public_reply_enabled = true (템플릿 50개)
```

> 소급 발송으로 500명에게 중복 DM 이 가는 시나리오는 **구조적으로 발생하지 않습니다.**
> 켜기 확인창에 소급 발송 경고를 넣지 않으셔도 됩니다.
> (사용자가 원하면 캠페인 수정에서 직접 켤 수 있게 하는 건 별건입니다 — 지금은 에디터에도
> 노출돼 있지 않다고 하셨으니, 필요하면 그때 여세요. 기본값이 위험한 방향이 아니라 괜찮습니다.)

---

## D. `mig-applied` 재시드 완료 ✅

```
후보 5개 전부 applied · 캠페인 5개 inactive · source=dm_migration
workspace_id 는 그대로 (8d6eb0d5-829b-57c4-a30e-0ce3a1f9cd0a)
```

⚠️ `job_id` 와 후보 `id` 는 새로 바뀌었고 **재로그인이 필요**합니다.

---

## E. 회신만 필요한 항목

### E-4. `transfer.drops` — **`attachment_image` 판정 가능합니다** ✅

원본 DM 의 `attachments` 를 직접 읽어 판정합니다(텍스트로 평탄화하기 **전에** 뽑습니다).
실제로 채워지는 코드는 **5종**입니다. 나머지 7종은 **절대 안 내려갑니다** —
원본 DM 만 봐서는 알 수 없는 정보라 추측하지 않습니다.

| code | 언제 | 프론트 문구 |
|---|---|---|
| `attachment_image` | 원본 DM 에 사진 첨부 | 사진 N장은 못 옮겨요 |
| `attachment_video` | 동영상 첨부 | 동영상은 못 옮겨요 |
| `attachment_file` | 파일 첨부 | 파일은 못 옮겨요 |
| `carousel` | 첨부 2장 이상 | 넘겨보는 카드는 못 옮겨요 |
| `message_sequence` | 게이트는 찾았는데 **뒤따르는 본 DM 을 못 찾음** | 메시지 일부만 가져왔어요 |

**안 내려가는 7종**: `delay_between_messages` · `quick_replies` · `link_buttons_overflow` ·
`personalization_vars` · `opening_too_long` · `unsupported_trigger` · `ab_test`
→ `DROP_CODES` 12종은 그대로 두시되, **화면 문구는 위 5종만 준비**하시면 됩니다.
`opening_too_long` 은 길이 3단 보정이 있어 **의도적으로 발생시키지 않습니다**(아래 §F 참조).

스키마에도 enum 으로 박아 뒀습니다 (`TransferDrop.code`).

### E-5. `apply` 오버라이드 확장 — **열지 않겠습니다. 대신 이렇게 하세요**

`apply` 는 **INACTIVE 캠페인을 만드는 것**이고, 그 뒤 사용자는 어차피 캠페인 에디터에서
열어 봅니다. 캠페인 `PATCH` 에는 **요청하신 필드가 전부 이미 있습니다** —
`opening_message_templates` · `scheduled_start_at/end_at` · `recovery_reply_*` ·
`public_reply_batch_size/_pause_seconds/_limit`.

`apply` 바디에 같은 필드를 또 만들면 **캠페인 스키마 전체가 두 곳으로 갈라집니다**
(한쪽만 고쳐지는 사고가 납니다). 필요하면 `apply` → `PATCH` 두 번 부르는 쪽을 권합니다.

- **`trigger_type` 감지**: 현실적으로 어렵습니다. `any_media` 는 "여러 게시물에서 같은 오퍼가
  반복" 으로 추정할 수는 있으나, **오탐하면 전 게시물에 발동**해 피해가 큽니다.
  `story_reply` 는 댓글 기반 관측이라 흔적 자체가 없습니다. → **`specific_media` 고정**입니다.

### E-6. `follow_up_candidates` — **(C) 입니다. 소비처 만들지 마세요**

정밀도 재작성 이후 이 필드는 **항상 빈 배열**입니다(파이프라인이 `[]` 로 고정). 데이터를
버리고 계신 게 아니라 **원래 안 채웁니다.**

우리 모델에서 2통이 나가는 경로는 **팔로우 게이트 하나뿐**이고(버튼을 눌러야 두 번째가 나감),
게이트가 관측되면 **이미 자동으로 복원**합니다 — `gate_detected` · `follow_gate_prompt_templates` ·
`reward_message_template` 로 들어갑니다. 게이트 없이 연속 2통이던 캠페인은 **1통으로 합쳐집니다**
(이건 못 옮기는 항목이 맞습니다).

→ 후보 상세의 "이어지는 DM" 자리는 **지우셔도 됩니다.**

### E-7. 분석 범위 · `progress` · 소요 시간

| 질문 | 답 |
|---|---|
| `media_limit` 안 보내도 되나요 | 네. **서버 기본 50**, 범위 10~100 |
| 실제 분석 범위 | **최근 게시물 50개** 중 **댓글 8개 이상**인 것만. → 문구는 **"최근 게시물을 봤어요"** 가 맞습니다 |
| `progress` 단조 증가? | **네.** 5(게시물) → 8(예상 계산) → 15~85(DM 복원) → 90(초안) → 100. 되돌아가지 않습니다 |
| 소요 p90 | 실계정 표본이 적어 아직 못 드립니다. 관측된 건 **1~3분**(게시물 3개) 수준이고, 게시물이 많으면 15~20분입니다. **"최대 20분"** 상한 표시가 안전합니다 |

`estimate` 는 1단계(estimating)가 끝나면 채워지니, 그때부터 "약 N분" 을 쓰셔도 됩니다.

### E-8. §11 "안 되는 것" 목록 — **전부 사실입니다** ✅

이미지/동영상/파일 첨부, 캐러셀, 조건 분기, 메시지 간 대기, 태그/커스텀 필드 —
5개 항목 모두 맞습니다. 안내 문구의 근거로 쓰셔도 됩니다.

### E-9. 캠페인 수 분포 p50/p90/max — **아직 못 드렸습니다**

운영 DB 접근에 Cloudflare Access 브라우저 인증이 필요해 자동으로 못 뽑고 있습니다.
잊지 않았고, 별도로 전달드리겠습니다. (페이지네이션은 opt-in 이라 이 숫자가 프론트 작업을
막지는 않습니다.)

---

## F. 스펙 정정 — 코드로 고친 것

| # | 상태 | 내용 |
|---|---|---|
| **F-1** | ✅ 수정 | `candidates_list` 200 을 **봉투**(`PaginatedCandidate`)로 정정 |
| **F-2** | ✅ 수정 | `jobs_create` 200/201 을 `{reused, job}`(`DMMigrationJobStartResponse`)으로 정정 |
| **F-3** | ✅ 확답 | **`jobs_list` 는 배열 유지**입니다. 바꿀 계획 없고, 바꾸면 미리 알려드립니다 |
| **F-4** | ✅ 수정 | `page`·`page_size`·`search`·`ordering`·`media_after`·`media_before`·`needs_confirm`·`view` **전부 선언**. `page_size` 기본 20 · **최대 100 · 범위 밖은 clamp**(400 아님) |
| **F-5** | ✅ 수정 | `offer`·`support`·`transfer`·`existing_campaign` 하위 키 선언. **`offer.button_label`** 확정(`label` 아님) |
| **F-6** | ✅ 수정 | `error.code` 는 실제로 **`token_expired` · `error` 2종뿐**입니다. `token_invalid`·`connection_inactive` 는 이 잡에서 안 씁니다(비활성 연결은 잡 생성 전 400) |
| **F-7** | ✅ 수정 | `error.message` 에서 `(code=190)` 꼬리를 **없앴습니다.** 이제 그대로 노출 가능한 문장입니다 → **정규식 지우셔도 됩니다** |
| **F-8** | ✅ 부분 | `summary` 는 스키마 붙였습니다. `apply`/`apply-all`/`prompt-answer` 는 다음 차례 |
| **F-9** | ✅ 수정 | **7일 캐시 / 6시간 쿨다운**이 맞습니다. `force` 필드 설명의 24h·1h 는 옛 값이라 정정했습니다 |
| **F-10** | ✅ 수정 | `progress` `maximum: 100` |
| **F-11** | ✅ 수정 | 400/403/404 조건 명시 — **활성 연동 없음·비활성 = 400**, 없는 `ig_connection_id` = 404, 남의 워크스페이스 = 403. 프론트의 `is_active && status==='active'` 게이트가 **서버 판정과 동일**합니다 |
| **F-13** | ✅ 수정 | `confirm-link` 의 `url` — **빈 문자열이 유효**하다고 명시. 링크를 지우려는 게 아니면 **필드를 아예 빼고** 보내는 쪽을 권합니다 |
| **F-14** | ⚠️ **정정** | 아래 참조 — **밴드는 2종이 아니라 3종**입니다 |
| **F-16** | ✅ 이미 | cancel 은 "다음 단계 경계에서 멈춘다" 가 이미 description 에 있습니다 |
| F-12·F-15 | 다음 | 429 스키마 · required 조이기는 다음 차례 |

### ⚠️ F-14 — 제가 지난번에 틀리게 답했습니다: **밴드는 3종입니다**

08-14 회신에서 "실제 밴드는 `auto_draft`/`needs_review` 둘뿐" 이라고 드렸는데 **틀렸습니다.**
코드를 다시 확인하니 세 번째가 실제로 저장됩니다.

| band | 의미 | 발생 |
|---|---|---|
| `auto_draft` | 지지 0.60 이상 — 자동 적용 대상 | O |
| `needs_review` | 근거 약함 — 링크 확인 대상 | O |
| **`excluded`** | **DM 은 못 찾았지만 캠페인 정황(캡션 트리거·문구 반복)이 있는 게시물** | **O** |
| `template_only` | — | **구조적으로 0건** (예약값) |

`excluded` 는 "여기서 캠페인 돌리셨던 것 같은데 DM 기록을 못 찾았어요" 에 해당합니다.
자동 적용 대상이 아니고 `apply` 도 권하지 않습니다 — **목록에서 숨기거나 별도 섹션**으로
두시면 됩니다. `template_only` 는 enum 에만 남겨 뒀습니다(제거하면 양쪽 동시 배포 필요).

### 스펙 밖 사실 — 확인 요청하신 3건

- **`source`**: 불러온 캠페인 `"dm_migration"`, 직접 만든 캠페인 **빈 문자열**이 맞습니다.
  캠페인 스키마에 정식 선언돼 있고 **읽기 전용**입니다. 빈 문자열은 쿼리로 못 보내서
  **`?source=direct`** 를 별칭으로 뒀습니다 — 우회 지우고 이걸 쓰세요.
- **첫 DM 길이 3단 보정**: ①게이트 구조면 2통 분할 ②링크를 버튼으로 승격(333자→640자)
  ③그래도 넘치면 초안 단계가 한도 안에서 다시 씀. **유지되는 계약입니다.**
  `draft_opening_message` 는 항상 한도 안이니 **프론트에서 길이를 재지 마세요.**
- **`is_active`**: IG 연결 응답에 항상 옵니다. 판정 기준도 프론트와 동일합니다.

---

## 검증

```
apps/integrations                     354 passed
스키마 재생성                          candidates 200 → PaginatedCandidate
                                      jobs_create 201 → DMMigrationJobStartResponse
                                      params: page·page_size·search·ordering·media_after·
                                              media_before·needs_confirm·view·band·status
                                      progress: {minimum:0, maximum:100}
                                      drop enum: 5종
dev 실측  §A  캠페인 삭제 → status=detected · applied_at 보존 ✔
          §B  삭제된 캠페인의 후보 재적용 → 201 새 캠페인 ✔
          §C  생성된 캠페인 backfill_existing_comments=false ✔
          §D  mig-applied 재시드 (후보 5 applied · 캠페인 5 inactive) ✔
```
