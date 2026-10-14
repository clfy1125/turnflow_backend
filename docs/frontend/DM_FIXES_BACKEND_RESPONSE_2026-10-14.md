# DM 자동화 버그 수정 — 백엔드 회신 (2026-10-14 요청 / 2026-10-15 회신)

요청: `backend-dm-fixes-2026-10-14.md` (브랜치 `fix/dm-code-issues`)

**결론 먼저**

| # | 질문 | 지금(기존 배포) | 조치 |
|---|---|---|---|
| 1-1 | 이미 `paused` 인 건에 `POST /pause/` → 200? | ✅ **원래 200** (상태 전제가 없다) | 계약으로 문서화 |
| 1-2 | 그 요청이 `auto_paused_at`·`auto_paused_reason` 을 비우나? | ❌ **안 비웠다** — 지적이 맞다 | 🔧 **고쳤다**(아래) |
| 2 | `trigger_feature` 에 새 값 추가 가능? | — | ✅ **서버 변경 없이 바로 가능**. enum 아님 |

---

## 1. 자동 정지된 자동화의 「끄기」

### 1-1. 이미 정지 상태여도 200 입니다

`pause` 액션에는 상태 전제 조건이 없습니다. `status` 가 무엇이든 `paused` 로 덮고 200 +
갱신된 캠페인 객체(목록 항목과 같은 형태)를 돌려줍니다. 409/400 은 **재개(`resume`)에만**
붙어 있습니다. 토스트 걱정은 안 하셔도 됩니다.

### 1-2. 표식은 안 비워지고 있었습니다 → 고쳤습니다

지적하신 그대로였습니다. 종전 구현은 `status` 만 바꾸고 저장해서, 응답으로 행을 갱신하면
`auto_paused_at` 이 그대로 와 다시 「게시물 제한 · 켜짐」으로 돌아갑니다.

**2026-10-15 수정** — `POST /auto-dm-campaigns/{id}/pause/` 는 이제 시스템 자동 정지 표식을
비웁니다. "사용자가 직접 껐다"로 상태가 바뀌는 것이 맞습니다.

```jsonc
// POST /api/v1/integrations/auto-dm-campaigns/{id}/pause/  → 200
{
  "id": "…",
  "status": "paused",
  "auto_paused_at": null,      // ← 비워집니다
  "auto_paused_reason": "",    // ← 비워집니다
  …
}
```

- **일괄 정지**(`POST .../bulk-pause/`)도 같은 규칙입니다.
- 응답 본문을 그대로 그 행에 써 넣으면 됩니다. 재조회 불필요합니다.
- 즉 **제한 때문에 `resume` 이 409 인 자동화를 사용자가 끌 수 있는 유일한 경로**가
  `pause` 이고, 프론트가 보내기로 한 그 요청이 정답입니다.

### 알아 두실 부작용 2가지

1. **홈 화면 알림이 같이 사라집니다.** `GET /api/v1/home/alerts/` 의
   「게시물 제한으로 멈춘 자동화」 카드는 `auto_paused_at` 이 있는 캠페인을 셉니다.
   사용자가 끄면 그 건은 알림에서 빠집니다 — 사용자가 이미 처리한 항목이므로 의도된 동작입니다.
2. **표식을 지운다고 제한 게이트가 열리지는 않습니다.** 끈 뒤 다시 켜려 하면
   `resume` 은 **여전히 409**(`error.details.code='media_content_restricted'`) 입니다.
   게이트 판정은 표식이 아니라 그 게시물의 **실제 발송 실패 이력**(`blocking`)을 봅니다.
   테스트로 못 박아 뒀습니다(`test_끈_뒤에도_재개는_여전히_409`).

> 참고 — 자동 정지 스위퍼(`sweep_restricted_campaigns`)는 `status=active` 인 것만 봅니다.
> 정지 상태인 동안에는 표식을 지워도 다시 잡히지 않고, 사용자가 나중에 재개하면 그때부터
> 다시 감시 대상이 됩니다.

**배포**: dev/prod 아직 미배포. 다음 배포에 포함됩니다. 프론트가 먼저 나가도
1-1(200)은 원래 성립하므로 오류 토스트는 안 뜹니다 — 다만 표식이 안 지워져 행이
「제한 · 켜짐」으로 돌아오니, **배포 뒤에 켜 주시는 쪽**을 권합니다.

---

## 2. `trigger_feature` 새 값

### 서버는 이 값을 검증하지 않습니다

`CheckoutEvent.trigger_feature` 는 **choices 없는 자유 문자열(40자 이내)** 입니다.
요청 시리얼라이저도 `CharField` 라 **모르는 값을 보내도 400 이 나지 않고 그대로 저장**됩니다.
(enum 인 것은 `event` 하나뿐입니다 — 여기 없는 값을 보내면 400.)

즉 **백엔드 배포를 기다릴 필요 없이 지금 바로 새 값을 보내셔도 됩니다.**
집계·귀속(진입 경로 분석)은 키 단위로 자동으로 잡힙니다. 어드민 대시보드는 한국어 라벨이
없으면 원문 키로 표기할 뿐입니다 — 그 라벨은 이번에 미리 넣어 뒀습니다.

### 값 이름 제안 — `dm_pro_trigger` 말고 기능별 4개를 권합니다

두 가지 이유입니다.

1. **`pro` 가 사실과 다릅니다.** 말씀하신 대로 이 4개는 베이직에서도 씁니다.
   (실제로 서버에는 이 4개에 대한 **플랜 게이트가 아예 없습니다** — 순수 프론트 UI 잠금입니다.
   서버가 플랜으로 막는 DM 기능은 `dm_recovery`(프로)와 `spam_filter`(프로) 둘뿐입니다.)
   나중에 베이직 유도로 바꿀 때 이벤트 이름만 "pro" 로 남습니다.
2. **4개를 한 값으로 합치면 "무엇이 결제를 끌었나"가 사라집니다.** 대시보드의 결제 진입
   경로는 `trigger_feature` 단위로 집계합니다. 하나로 묶으면 스토리 답장이 끈 건지
   팔로우 보상이 끈 건지 영영 못 가릅니다. 쪼개 두면 나중에 합치는 건 언제든 가능하지만,
   합쳐서 쌓은 건 되돌릴 수 없습니다.

**등록해 둔 키 (대시보드 한국어 라벨 포함)**

| 기능 | `trigger_feature` | 대시보드 라벨 |
|---|---|---|
| 모든 게시물 | `dm_trigger_all_posts` | 모든 게시물 트리거 |
| 다음 새 게시물(1회) | `dm_trigger_next_media` | 다음 새 게시물 트리거 |
| 스토리 답장 | `dm_trigger_story_reply` | 스토리 답장 트리거 |
| 팔로우 확인 후 보상 | `dm_follow_gate` | 팔로우 확인 후 보상 |
| (정말 하나로 합쳐야 한다면) | `dm_trigger_advanced` | DM 고급 트리거(미분류) |

같은 김에 이미 프론트가 쓰고 있는데 라벨이 없던 `ai_tokens` · `insta_report` ·
`dm_recovery` 도 라벨을 채웠습니다(종전에는 어드민 화면에 원문 키로 떴습니다).

**요금제 창 머리말 문구**는 프론트가 정하시면 됩니다 — 서버가 내려주는 문구가 아닙니다.

---

## 변경 파일

| 파일 | 내용 |
|---|---|
| `apps/integrations/views.py` | `pause` · `bulk-pause` 가 `clear_auto_pause()` 호출 + 스키마 문서 |
| `apps/integrations/models.py` | `clear_auto_pause()` · `auto_paused_at` 설명 갱신 |
| `apps/integrations/test_content_restriction.py` | `TestPauseClearsMark` 4건 추가 |
| `apps/admin_api/views/dashboard_marketing.py` | `TRIGGER_FEATURE_LABELS` 8키 추가 |
| `apps/analytics/views.py` | `trigger_feature` 가 enum 이 아님을 API 문서에 명시 |

테스트: `apps/integrations` 814건 · `apps/analytics` + 마케팅 대시보드 373건 전부 통과.
