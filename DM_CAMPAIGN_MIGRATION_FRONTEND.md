# DM 캠페인 이전 (from 매니챗 등) — 프론트 연동 가이드

> 타 DM 자동화 툴(매니챗·인포크링크·소셜비즈 등)에서 넘어온 사용자가 **수십 개 캠페인을
> 처음부터 다시 만들지 않도록**, 연동된 IG 계정의 최근 게시물·댓글·발신 DM 이력을 분석해
> "기존 DM 캠페인으로 보이는 게시물"을 찾아 **비활성(초안) 캠페인 후보**로 재구성하는 기능.

이 기능은 매니챗 등의 내부 설정을 직접 복제하지 않는다. Meta API 로 관측 가능한 것(게시물·
댓글·발신 DM)만 근거로 **추론**해 초안을 만든다. 버튼 분기·태그·delay·CRM 등 관측 불가 요소는
복원하지 않는다. 생성된 캠페인은 **항상 비활성(INACTIVE)** 이고, 사용자가 검수·수정 후 활성화한다.

- 전 플랜 사용 가능(획득 기능). 남용 방지 = 연결당 진행 잡 1개 + 완료 결과 24h 재사용 + `force` 재분석 쿨다운 1h.
- 소요 시간: 보통 10~20분(백그라운드). 폴링으로 진행률 표시.
- 원본(타인 댓글·DM 원문)은 완료 **7일 뒤 자동 파기**(초안·집계 근거는 유지) — 개인정보 최소보관.

---

## 1. 전체 사용자 흐름

```
[DM 캠페인 이전] 클릭
  → IG 계정 선택 (여러 개면)
  → "최근 N개 게시물 분석" (기본 50, 최대 100)
  → POST /dm-migration/jobs/      (분석 시작)
  → 3초 간격 폴링으로 진행률 표시  (GET /dm-migration/jobs/{id}/)
  → 완료(ready/partial) 시 후보 목록  (GET /dm-migration/jobs/{id}/candidates/)
  → 각 후보 검수/수정
  → 적용  (POST /dm-migration/candidates/{id}/apply/)  → 비활성 캠페인 생성
  → (안내) 기존 툴 자동화 OFF 확인
  → 캠페인 활성화 (기존 캠페인 활성화 플로우)
```

**분석 시작 화면 안내 문구(권장)**
> 최근 게시물·댓글·최근 DM 이력을 분석해 캠페인 초안을 만듭니다. 기존 도구의 숨은 조건·버튼
> 분기·오래된 메시지는 일부 복원되지 않을 수 있어요. 생성된 캠페인은 **비활성 상태**로 만들어지며,
> 검수 후 직접 활성화하시면 됩니다.

---

## 2. 엔드포인트

베이스: `/api/v1/integrations/` · 인증: `Authorization: Bearer <JWT>` · 모든 요청에 `?workspace_id=<uuid>` 필수.

### 2-1. 분석 시작 — `POST /dm-migration/jobs/?workspace_id=`

요청 body (전부 선택):
```json
{ "ig_connection_id": "b1a2…", "media_limit": 50, "force": false, "llm_model": "deepseek" }
```
- `ig_connection_id`: 미지정 시 워크스페이스의 첫 활성 IG 연결.
- `media_limit`: 10~100 (기본 50).
- `force`: 24h 내 완료 결과가 있어도 새로 분석. 단, 직전 종료 후 1h 이내면 429.

응답:
- **201** `{ "reused": false, "job": {…} }` — 새 잡 생성·실행 시작.
- **200** `{ "reused": true, "job": {…} }` — 진행 중 잡이 있거나 24h 내 완료 결과 재사용.
- **429** — `force` 쿨다운(1h) 이내:
  ```json
  { "success": false, "error": { "code": 429, "message": "최근 분석이 방금 끝났어요. 잠시 후 다시 시도해주세요.",
    "details": { "code": "migration_cooldown", "cooldown_until": "2026-07-20T…", "retry_after": 1800 } } }
  ```
- 400(workspace_id 누락/활성 연결 없음/비활성 연결), 403(멤버 아님/타 워크스페이스), 404(워크스페이스/연결 없음).

> `reused` 로 "이미 진행 중이에요 / 최근 결과를 보여드릴게요" 를 구분해 안내하라.

### 2-2. 상태 조회(폴링) — `GET /dm-migration/jobs/{id}/?workspace_id=`

**3초 간격**으로 폴링, 종결 상태면 중단.
```json
{
  "id": "…", "status": "running", "stage": "collecting_comments", "progress": 12,
  "message": "댓글을 수집하고 있습니다...",
  "counters": { "media_scanned": 50, "comments_collected": 830, "conversations_scanned": 0,
                "dm_messages_collected": 0, "templates_found": 0, "candidates_created": 0 },
  "error": null, "candidate_count": 0,
  "media_limit": 50, "llm_model": "deepseek",
  "created_at": "…", "started_at": "…", "finished_at": null,
  "raw_expires_at": null, "raw_purged_at": null, "resume_at": null
}
```

**status** (종결=폴링 중단): `queued` / `running` / `paused_rate_limited`(레이트리밋 대기 — 자동 재개, `resume_at` 참고) / **`ready`**(완료) / **`partial`**(일부만 — 일부 데이터 수집 실패/스코프 없음) / **`failed`**(`error.code`=`token_expired`/`stalled`/`error`) / **`canceled`**.

**stage** (진행 표시용): `queued → collecting_media → collecting_comments → classifying_posts → collecting_dm_conversations → clustering_dm_templates → matching_campaigns → generating_drafts → completed`. `progress`(0~100)로 게이지.

> `paused_rate_limited` 는 실패가 아니다 — "요청이 많아 잠시 대기 중, 곧 이어서 분석" 으로 표시하고 계속 폴링.

### 2-3. 잡 목록 — `GET /dm-migration/jobs/?workspace_id=&ig_connection_id=`
최신순 최대 20건. 페이지 진입 시 "최신 잡" 찾기용.

### 2-4. 취소 — `POST /dm-migration/jobs/{id}/cancel/?workspace_id=`
진행 중 잡 취소. 종결 잡은 **409**(`error.details.code="job_already_terminal"`).

### 2-5. 후보 목록 — `GET /dm-migration/jobs/{id}/candidates/?workspace_id=&status=&band=`
```json
[{
  "id": "…", "job_id": "…", "status": "detected", "band": "auto_draft",
  "media_id": "178…", "media_permalink": "https://…", "media_caption_excerpt": "…", "media_timestamp": "…",
  "suggested_keywords": ["링크", "자료"], "suggested_keyword_mode": "any", "confidence": 0.87,
  "draft_name": "자료 댓글 DM 자동화", "draft_description": "…",
  "draft_opening_message": "안녕하세요! 요청하신 자료 안내드려요. 아래 [링크]를 확인해주세요 😊",
  "draft_public_reply_templates": ["DM 드렸어요! 확인 부탁드려요 :)"],
  "follow_up_candidates": [{ "text": "…", "confidence": 0.58, "source_template_id": "t1", "cluster_size": 83 }],
  "matched_template": { "template_id": "t1", "cluster_size": 83, "variable_slots": ["url"], "first_sent_at": "…", "last_sent_at": "…" },
  "evidence_aggregates": { "matched_comment_count": 142, "total_comment_count": 284, "keyword_hit_counts": {"링크": 88, "자료": 54},
                           "account_replied_publicly": true, "dm_burst_overlap_ratio": 0.72,
                           "has_existing_campaign": false, "own_sends_excluded": 12 },
  "evidence_raw": { "sample_comments": [{"text":"링크","timestamp":"…"}], "sample_outbound_dms": [{"text":"…","created_time":"…"}], "template_representative_text": "…" },
  "applied_campaign_id": null, "applied_at": null, "dismissed_at": null, "created_at": "…"
}]
```

**band** (신뢰도 밴드):
| band | 의미 | UI 제안 |
|---|---|---|
| `auto_draft` | 강한 후보(게시물+DM 매칭 확실) | 기본 노출·"적용" 강조 |
| `needs_review` | 불확실 — 검수 권장 | "확인 후 적용" |
| `template_only` | 반복 DM 템플릿은 찾았으나 게시물 미상 | 적용 시 게시물(`media_id`) 지정 필요 |
| `excluded` | 후보 아님(참고) | 기본 숨김 |

- `confidence` = 최종 매칭 점수(0~1). `evidence_aggregates.has_existing_campaign=true` 면 **이미 TurnFlow 캠페인이 있는 게시물** → "중복 발송 주의" 배지.
- `evidence_raw` 는 완료 **7일 후 파기되면 `null`** 로 내려간다(집계 근거는 유지). 근거 원문 표시는 이 값이 있을 때만.

### 2-6. 후보 적용 — `POST /dm-migration/candidates/{id}/apply/?workspace_id=`

body(전부 선택 — 미지정 필드는 후보 초안값 사용):
```json
{ "name": "자료 DM", "keyword_filter": ["자료","링크"], "keyword_mode": "any",
  "opening_message_template": "…", "public_reply_enabled": true, "public_reply_templates": ["…"],
  "description": "…", "media_id": "178…" }
```
- 후보를 **비활성(INACTIVE) Auto DM 캠페인**으로 생성. 응답 **201** `{ "candidate": {…}, "campaign": {…} }`.
- 검증은 기존 캠페인 생성 규칙 재사용 → DM 본문 한도(버튼 640자/일반 1000바이트) 초과 시 **400**.
- 이미 적용된 후보 재적용 → **409**(`candidate_already_applied`). 무시(dismissed)했던 후보는 다시 적용 가능(되살리기).
- `template_only` 후보는 **`media_id` 를 반드시 지정**(없으면 400).

> 생성된 캠페인은 비활성이라 "활성 캠페인 1개/게시물" 중복(409) 검사에 걸리지 않는다 —
> **활성화 시점**에 그 검사가 발동한다.

### 2-7. 후보 무시 — `POST /dm-migration/candidates/{id}/dismiss/?workspace_id=`
목록에서 숨김용 상태(dismissed). 적용된 후보는 409.

---

## 3. 활성화 전 안내 (중요)

적용은 캠페인을 **만들기만** 한다(비활성). 사용자가 활성화하기 전에 반드시 안내하라:

> ⚠️ 매니챗 등 **기존 자동화가 같은 게시물에 켜져 있으면 DM 이 중복 발송**되거나, 인스타그램의
> "댓글 1개당 자동 DM 1회" 제한 때문에 TurnFlow DM 이 조용히 실패할 수 있어요. **기존 도구의
> 해당 자동화를 먼저 끈 뒤** TurnFlow 캠페인을 활성화하세요. (자세한 해제 방법은 다른 DM 툴 연결
> 해제 가이드 참고)

`evidence_aggregates.has_existing_campaign=true` 인 후보는 TurnFlow 내에도 이미 캠페인이 있으니
활성화 시 특히 주의(활성 중복 409 가능)하도록 표시.

---

## 4. 상태 머신 요약(폴링 UX)

```
queued ─▶ running ─▶ (collecting_media→…→generating_drafts) ─▶ ready / partial
             │
             └▶ paused_rate_limited ──(resume_at 후 자동)──▶ running
running/queued ──cancel──▶ canceled
running ──토큰만료/스톨──▶ failed(error.code = token_expired / stalled / error)
```

- 폴링은 `ready`/`partial`/`failed`/`canceled` 에서 멈춘다.
- `partial` = 후보는 있으나 일부 데이터 수집 실패/DM 스코프 없음 → "일부만 분석됨" 배너 + 후보는 정상 노출.
