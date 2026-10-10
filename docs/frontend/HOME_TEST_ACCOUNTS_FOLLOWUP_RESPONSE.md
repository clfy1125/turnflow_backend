# 홈 테스트 계정 확인 결과 — 백엔드 회신 (9건)

2026-10-11 · 백엔드 → 프론트
앞 문서: `backend-home-test-accounts.md`(요청) · `HOME_TEST_ACCOUNTS_RESPONSE.md`(1차 회신) ·
`backend-home-test-accounts-followup.md`(프론트 확인 결과)

---

## 0. 처리 결과 한눈에

| # | 내용 | 처리 |
|---|---|---|
| 1 | 조회만 했는데 캠페인 `updated_at` 이 바뀜 | **고쳤습니다 · 운영 영향 있었습니다** (미배포) |
| 2 | H16 · H17 시드 값이 OpenAPI 와 다름 | **고쳤습니다 · dev 재시드 완료.** 운영에 소문자 경로는 **없습니다** |
| 3 | 추천인 코드 `reason` 이 한국어 | **`reason_code` 추가** (5종, `reason` 은 영구 유지) |
| 4 | 로그인 시 인증 코드를 보내는지 | **안 보냅니다** → 프론트가 한 번 부르는 게 맞습니다 |
| 5 | 카카오 「이번에 새로 연결됨」 표시 | **`kakao_linked_now` 추가** |
| 6 | PC 인스타 팝업 실패에 사유 코드 | **`reason` 추가**. `errorCode` 로는 **이미 내려가고 있었습니다** |
| 7 | 복구 뒤 밀린 댓글에 DM 이 나가는지 | **나갑니다.** 단 버튼이 아니라 **1시간 주기 배치**가, **댓글 7일** 안에서 |
| 8 | `media_url` 이 이미지인가 링크인가 | **엔드포인트마다 다릅니다** — 1차 회신 §2-2 가 틀렸습니다. 목 픽스처도 운영에 맞췄습니다 |
| 9 | 3시간 뒤 사라지는 시드 상태 | 1차 회신 §4 에 추가했습니다 |

> **배포**: 1·3·5·6·8 은 운영 코드 변경이라 아직 **dev 에만** 있습니다. 운영 배포 시점은
> 따로 알려 드리겠습니다. 2·9 는 시드·문서라 운영과 무관합니다.
>
> dev-api 에서 **공개 URL 로 직접 확인**했습니다 — 바로 쓰실 수 있습니다:
> ```
> GET https://dev-api.turnflow.link/api/v1/billing/referral/validate/?code=NOPE0000
> {"valid":false,"reason":"존재하지 않는 코드입니다.","reason_code":"not_found"}
> ```
> ⚠️ 혹시 **옛 응답이 보이면** 백엔드 dev 서버가 코드를 아직 못 읽은 것입니다(윈도우 바인드
> 마운트에서 자동 리로드가 가끔 걸릴 때가 있습니다). 알려주시면 바로 재기동하겠습니다 —
> 프론트 쪽 문제가 아닙니다.

---

## 1. 조회(GET)만 했는데 `updated_at` 이 바뀌던 문제 — 고쳤습니다

**진단이 정확했습니다.** 말씀하신 "사진 대체(백필) 경로가 찾은 게 없어도 `save()` 하는 것"
그대로였습니다. 그리고 **운영에도 같은 코드였습니다.**

### 무슨 일이 있었나

1. 캠페인 목록 GET → 썸네일 사본이 없는 캠페인은 동기화를 **비동기 예약**합니다
   (새로고침 연타 방지로 캠페인당 5분 억제 — 11:06 → 11:17 간격이 여기서 나옵니다).
2. 그 작업이 Graph 에서 이미지 소스를 못 찾으면 **실패 카운터만 올리고 저장**합니다.
3. 그 저장이 `update_fields` 에 `updated_at` 을 넣고 있어서 **조회만 해도 "수정 시각"이
   밀렸습니다.**

### 운영 영향 범위

연속 실패 상한(5회)이 있어 무한히 밀리지는 않지만, **5번이면 이미 틀린 값**입니다.
대상은 "썸네일을 받아올 수 없는 캠페인" — **게시물을 지웠거나 재업로드한 경우**가 대표적이고,
운영에 실제로 존재하는 상태입니다. permalink 백필에도 같은 코드가 있었습니다.

### 고친 것

시스템 백필 **4곳**에서 `updated_at` 을 `update_fields` 에서 뺐습니다.
`auto_now=True` 필드는 `update_fields` 에 없으면 쓰이지 않으므로, 값은 그대로 보존됩니다.

| 경로 | 파일 |
|---|---|
| 썸네일 동기화 — 성공 | `apps/integrations/tasks.py` `sync_campaign_thumbnail` |
| 썸네일 동기화 — 실패 카운터 | `apps/integrations/tasks.py` `_record_thumbnail_failure` |
| permalink 백필 | `apps/integrations/tasks.py` `backfill_campaign_media_permalink` |
| 수동 백필 명령 2개 | `backfill_campaign_permalinks` · `backfill_campaign_thumbnails` |

**사용자 편집 경로(캠페인 PATCH/PUT)는 그대로 둡니다** — 거기선 `updated_at` 이 밀리는 게 맞습니다.

### 확인

회귀 테스트 2개를 넣었고, **일부러 고장난 버전에 먼저 돌려** 둘 다 실패하는 것까지
확인했습니다(`apps/integrations/test_campaign_thumbnail.py`).
dev 에서도 재현 절차 그대로 — 사진 없는 캠페인을 두고 캠페인 목록 GET + 미디어 GET 을
3회 반복 → **`updated_at` 이 바뀐 캠페인 0건**.

---

## 2. H16 · H17 시드 값 — 제 시드 버그였습니다

**운영에서 소문자 `error_code` 가 나가는 경로는 없습니다.** `insta_reports` 의 실패 기록은
전부 `ReportErrorCode` enum(대문자)을 거칩니다 — 전수 확인했습니다.
**프론트 표를 대문자 기준으로 그대로 두세요.** 바꾸실 필요 없습니다.

소문자 `extract_failed` 와 존재하지 않는 단계 `analyzing` 은 **제가 시드에 손으로 적은 값**이고,
`CharField` 는 `save()` 때 choices 를 검증하지 않아 조용히 저장됐습니다. 고쳤습니다.

| 계정 | 바뀐 값 |
|---|---|
| H16 | `stage`: `analyzing` → **`extracting`** (`stage_label` = 「영상 분석 중」), `progress=45` |
| H17 | `error_code`: `extract_failed` → **`EXTRACT_FAILED`** / `stage`: `synthesizing` → **`extracting`** |

실패 사유가 `EXTRACT_FAILED` 면 멈춘 단계도 `extracting` 이라야 화면이 앞뒤가 맞아서
단계도 함께 맞췄습니다. **dev 재시드까지 끝냈으니 그대로 다시 열어보시면 됩니다.**

실제 응답(검증 결과):

```
H16  GET /insta-reports/{id}/  status=running stage='extracting' stage_label='영상 분석 중' progress=45
H17  GET /insta-reports/{id}/  status=failed  stage='extracting' error_code='EXTRACT_FAILED'
     error_message='영상 분석 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.'
```

같은 실수가 다시 나지 않도록, 시드가 만든 `stage`·`error_code` 가 **TextChoices 안의 값인지
검사하는 테스트**를 넣었습니다.

---

## 3. 추천인 코드 — `reason_code` 를 추가했습니다

`reason`(한국어 고정 문장)은 **그대로 둡니다**(기존 프론트가 읽고 있어 지우면 문구가 사라집니다).
옆에 머신 키를 함께 보냅니다.

```jsonc
GET /api/v1/billing/referral/validate/?code=NOPE0000
{ "valid": false, "reason": "존재하지 않는 코드입니다.", "reason_code": "not_found" }
```

| `reason_code` | 뜻 | `reason`(한국어) |
|---|---|---|
| `not_found` | 그런 코드가 없음 | 존재하지 않는 코드입니다. |
| `inactive` | 비활성화됨 | 비활성화된 코드입니다. |
| `not_yet_valid` | 사용 시작 전 | 아직 사용할 수 없는 코드입니다. |
| `expired` | 유효 기간 지남 | 유효 기간이 만료된 코드입니다. |
| `exhausted` | 사용 횟수 소진 | 사용 횟수가 모두 소진된 코드입니다. |

5종 모두 실제 응답으로 확인했습니다. `valid: true` 일 때는 `reason`·`reason_code` 둘 다 없습니다.

> ⚠️ 요청하신 `already_used`(이 사용자가 이미 코드를 씀)는 **여기서 줄 수 없습니다.**
> 이 엔드포인트는 **비인증**이라 "누가 묻는지"를 모릅니다. 그 판정은 카드 등록
> (`POST /billing/toss/confirm/`)에서만 가능하고 **400** 으로 돌아옵니다.
> 비인증 엔드포인트에서 사용자별 상태를 알려주려면 인증을 걸어야 하는데, 그러면 결제
> 화면 진입 전 코드 미리보기가 막힙니다 — 필요하시면 별도로 논의하시죠.

---

## 4. 이메일 미인증 계정 로그인 — **코드를 보내지 않습니다**

`POST /auth/login/` 은 인증 메일을 **보내지 않습니다**(토큰만 발급하고 끝납니다).
인증 메일이 나가는 시점은 **가입 순간 한 번**뿐입니다(`post_save` 시그널).

**→ 프론트가 인증 화면을 띄울 때 `POST /auth/email/send-verification/` 를 한 번 부르는 것이
맞습니다. 중복 발송 걱정 없습니다.**

오히려 **지금은 꼭 부르셔야 합니다.** 가입하고 며칠 뒤에 로그인한 사용자는 가입 때 받은
코드가 이미 만료돼서, 화면이 "보낸 코드를 입력해 주세요"라고 해도 **입력할 코드가 없습니다.**

| | 값 |
|---|---|
| 엔드포인트 | `POST /api/v1/auth/email/send-verification/` (Bearer 필요, body 없음) |
| 성공 | **202** `{ "detail": "인증 메일을 발송했습니다.", "expires_minutes": N }` |
| 이미 인증됨 | **409** — 화면 전환 경쟁 상태에서 날 수 있으니 조용히 무시하세요 |
| 스로틀 | **5회/시간** (사용자 단위). 초과 시 429 — 「잠시 후 다시 시도」 안내 |

---

## 5. 카카오 — `kakao_linked_now` 를 추가했습니다

지적이 정확합니다. `is_new_user=false` 는 "신규 가입이 아니다"일 뿐이라,
**카카오로 가입한 사람의 재로그인**과 **기존 이메일 계정에 카카오를 막 붙인 순간**을
구별하지 못합니다.

응답에 필드를 하나 더했습니다. **이번 요청에서 기존 계정에 카카오를 처음 붙였을 때만 `true`** 입니다.

```jsonc
POST /api/v1/auth/kakao/
{ "user": {...}, "tokens": {...},
  "is_new_user": false,
  "kakao_linked_now": true }     // ← 이때만 「기존 계정에 연결했어요」 안내
```

| 상황 | `is_new_user` | `kakao_linked_now` | 안내 |
|---|---|---|---|
| 카카오로 신규 가입 | `true` | `false` | 가입 완료 |
| 기존 이메일/구글 계정에 **이번에** 연결 | `false` | **`true`** | 「기존 계정에 카카오 로그인을 연결했어요」 |
| 카카오 계정으로 재로그인 | `false` | `false` | 안내 없음 |

세 경우를 모두 단언하는 테스트를 넣었습니다(`apps/authentication/tests/test_kakao_login.py`).

---

## 6. PC 인스타 팝업 — `reason` 을 추가했습니다 (사유는 원래 있었습니다)

확인해 보니 `postMessage` 페이로드에 **`errorCode` 로는 이미 내려가고 있었습니다.**
키 이름만 달랐던 것입니다 — 같은-탭은 `reason=`, 팝업은 `errorCode:`.

그 불일치 자체가 이번 같은 사고의 원인이므로, **`reason` 을 같은 값으로 함께 보냅니다.**
(`errorCode` 는 기존 프론트가 읽고 있어 **영구 유지**합니다.)

```jsonc
// window.opener.postMessage(...) — 실패
{ "source": "ig-connect", "type": "INSTAGRAM_ERROR", "success": false,
  "errorCode": "ALREADY_CONNECTED_ELSEWHERE",
  "reason":    "ALREADY_CONNECTED_ELSEWHERE",   // ← 추가 (errorCode 와 항상 같은 값)
  "message": "..." }
```

이제 PC(팝업)와 모바일(같은 탭)이 **같은 키 · 같은 값**을 씁니다. 값 목록도 동일합니다:

`OAUTH_AUTHORIZATION_FAILED` · `MISSING_PARAMETERS` · `INVALID_STATE` ·
`INSTAGRAM_API_ERROR` · `PLAN_LIMIT_EXCEEDED` · `ALREADY_CONNECTED_ELSEWHERE` · `INTERNAL_ERROR`

> 지금 바로 쓰실 수 있는 우회책: 배포 전이라면 `event.data.errorCode` 를 읽으시면 됩니다
> — 그 값은 예전부터 들어 있었습니다.

---

## 7. 댓글 받기 복구 뒤 — 밀린 댓글에도 DM 이 나갑니다 (단, 조건이 있습니다)

**결론부터**: 나갑니다. 다만 **「복구하기」 버튼이 하는 일이 아닙니다.**

- `POST .../resubscribe-webhooks/` 는 **구독만 되살립니다.** 밀린 댓글을 긁지 않습니다.
- 밀린 댓글은 **1시간 주기 배치**(`poll_missed_comments`)가 따로 메웁니다. 버튼을 누르지
  않아도 돕니다 — 실제로 운영에서 "웹훅은 죽었는데 실패 0건"인 계정들이 이 배치로
  메워지고 있습니다.

### 조건 (문구에 반영하실 것)

| 축 | 값 |
|---|---|
| **기한** | 댓글 작성 **7일 이내** (`PRIVATE_REPLY_WINDOW_DAYS`) — `ig_disconnected` 안내와 **같은 숫자** |
| **시점** | 최대 **1시간 뒤** (주기 배치). 버튼을 눌러도 즉시는 아닙니다 |
| **대상** | **활성 + 예약창 안**인 **특정 게시물** 캠페인만. `any_media`·스토리 답장 캠페인은 제외 |

### 문구 제안

> 「다시 받기 시작했어요. 끊긴 동안 달린 댓글에도 **7일 이내라면** 자동으로 DM 이 나갑니다
> (최대 1시간 안에).」

`ig_disconnected` 의 「7일 이내 댓글은 다시 발송」과 같은 기한이라 **문구를 통일하셔도 됩니다.**

> **버튼이 즉시 긁게 만들 수도 있습니다만 하지 않았습니다.** 연결에 활성 캠페인이 많으면
> 버튼 한 번이 Graph 호출 수백~수천 건이 되고, 그 호출량은 **앱 전체가 공유하는 쿼터**라
> 다른 워크스페이스의 댓글 수집·DM 발송을 굶길 수 있습니다. 필요하다고 판단하시면
> (대상 축소 + 쿨다운을 붙여서) 따로 논의하시죠.

---

## 8. `media_url` — 1차 회신이 틀렸습니다. 엔드포인트마다 뜻이 다릅니다

**OpenAPI 쪽이 맞고 제 1차 회신 §2-2 가 틀렸습니다.** 이름이 같은 필드 두 개를 제가 하나로
뭉쳐서 설명했습니다. 혼란을 드려 죄송합니다.

| 어디 | `media_url` 의 정체 | `<img src>` 에 쓸 수 있나 |
|---|---|---|
| **게시물 목록** `GET .../workspaces/{ws}/media/` | **Graph 값 그대로** = IG CDN 미디어 URL (이미지/영상) | **네** — 단 **서명 만료**가 있어 저장은 금지 |
| **캠페인 응답** (`auto-dm-campaigns`) | **permalink (게시물 링크)** — 우리 DB 컬럼 | **아니요** — `thumbnail_url` 을 쓰세요 |

**→ 프론트의 현재 구현(「`thumbnail_url` 먼저, 없으면 `media_url`」)은 게시물 목록에서는
그대로 두시면 됩니다.** 캠페인 카드에서는 `media_url` 폴백을 **빼 주세요** — 거기 들어 있는
건 링크라 넣으면 깨집니다(운영에서 prod 77건 중 68건이 깨졌던 바로 그 경로입니다).

### 목 픽스처도 운영에 맞췄습니다

목이 게시물 목록의 `media_url` 에 permalink 를 넣고 있어서, **dev 에서만 그 폴백이 깨진
이미지로 보였습니다.** 이제 운영과 같이 **렌더 가능한 값**을 줍니다. `permalink` 는 별도
키로 그대로 나갑니다.

```jsonc
// GET .../media/ 의 항목 (dev)
{ "id": "mm-17025691433901-0",
  "media_url":     "data:image/svg+xml;utf8,…",              // 렌더됨 (운영=IG CDN URL)
  "thumbnail_url": "data:image/svg+xml;utf8,…",
  "permalink":     "https://www.instagram.com/p/mm-…-0/" }   // 링크
```

1차 회신 문서(§2-2)도 함께 고쳐 두었습니다.

---

## 9. 시간이 지나면 사라지는 시드 상태 — 문서에 넣었습니다

1차 회신 §4 에 추가했습니다. 두 개입니다.

| 상태 | 수명 | 왜 |
|---|---|---|
| **H16** 리포트 만드는 중 | **1시간** | `sweep_stale`(30분 주기)이 생성 60분 지난 `running` 을 실패로 확정 |
| **H02 · H07** `dm_send_blocked` | **3시간** | 쿨다운이 끝나면 **사라지는 것이 정상 동작**입니다 — "N초 뒤 자동 재개"가 이 알림의 뜻입니다 |

H02 의 칩이 「멈춤 4」 → 「멈춤 3」이 되는 건 **버그가 아니라 알림의 수명**입니다.
확인 전에 재시드하시면 됩니다 (`{"codes":["H02"]}`).

---

## 10. 프론트에서 고치신 것 — 백엔드 확인

| 프론트 조치 | 백엔드 판단 |
|---|---|
| 작성 중 = `inactive`, 오류 = `paused`+`post_restricted` 로 읽기 | **맞습니다.** 서버에 `draft`·`error` status 는 없습니다 |
| 연결 상태를 `is_expired` 와 `status` **둘 다** 보기 | **맞습니다 — 중요합니다.** `is_expired` 는 **토큰 시계 만료**만 봅니다. 인스타가 토큰을 거절해 `status="expired"` 가 되는 건 시계가 남아 있어도 일어나므로, **운영에 실재하는 조합**입니다. 시드 아티팩트가 아닙니다 |
| `link_page_empty`(`no_page`) 의 `target_id="none"` 대신 `data.page_id` 사용 | **맞습니다.** `"none"` 은 "닫음 기록"의 대상 키로 쓰는 자리표시자라 id 가 아닙니다. `null` 로 바꾸면 이미 맞추신 코드가 또 바뀌므로 **그대로 두겠습니다** |
| 영어 단수·복수 | 서버는 숫자만 보냅니다 — 프론트 판단이 맞습니다 |

---

## 11. 바뀐 파일 (참고)

| 항목 | 파일 |
|---|---|
| 1 `updated_at` | `apps/integrations/tasks.py` · `management/commands/backfill_campaign_*.py` · 테스트 `test_campaign_thumbnail.py` |
| 2 시드 값 | `apps/home/dev_seed_specs.py` · 테스트 `apps/home/tests/test_dev_seed.py` |
| 3 `reason_code` | `apps/billing/models.py`(`redeemable_state` = 판정 단일 소스) · `referral_views.py` · `serializers.py` |
| 5 `kakao_linked_now` | `apps/authentication/kakao_views.py` · `serializers.py` · 테스트 `tests/test_kakao_login.py` |
| 6 `reason` | `apps/integrations/oauth_callback_pages.py` |
| 8 목 `media_url` | `apps/integrations/services.py` · 테스트 `test_mock_media.py` |

검증: 변경한 앱 6개 전체 테스트 **1,367건 통과**, black·ruff·`manage.py check` 통과.
