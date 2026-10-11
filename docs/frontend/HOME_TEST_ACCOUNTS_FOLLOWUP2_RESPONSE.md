# 홈 테스트 계정 2차 확인 결과 — 백엔드 회신 (6건)

2026-10-11 · 백엔드 → 프론트
앞 문서: `HOME_TEST_ACCOUNTS_RESPONSE.md`(1차) · `HOME_TEST_ACCOUNTS_FOLLOWUP_RESPONSE.md`(2차) ·
`backend-home-test-accounts-followup-2.md`(프론트 2차 확인 결과)

---

## 0. 처리 결과

| # | 내용 | 처리 |
|---|---|---|
| 1 | `updated_at` 이 dev 에서 그대로임 | **진단이 정확했습니다.** dev 워커가 옛 코드였습니다 → 재기동 완료. **운영은 배포 때 교체돼 이미 고쳐져 있었고, 운영 데이터로 증명했습니다** |
| 2 | 서버가 `Accept-Language` 를 안 봄 | **운영도 같습니다.** 전면 i18n 대신 머신 코드로 가는 방향에 동의합니다 |
| 3 | 머신 코드 없는 곳 2개 | **`reason_code` 추가** — `redeem` 400 · `toss/confirm` 400. OpenAPI 예시도 보완 |
| 4-1 | 콜백 페이지가 한국어만 | **영어 지원 추가** (`Accept-Language` 기준) |
| 4-2 | 운영 콜백 주소 | **불일치 없습니다** — 운영 콜백은 `turnflow-api.clfy.ai.kr` 에서 열립니다 |
| 5 | 밀린 댓글 질문 3개 | 답변 — 문구 수정 제안 포함 |
| 6 | dev 스토리 502 | **고쳤습니다** (목 분기 누락). `media_url` 규약도 답변 |

> **배포**: 3 · 4-1 · 6 은 커밋 완료, 이 문서 전달 직후 운영 배포합니다.
> 1 · 4-2 는 이미 운영에 반영돼 있습니다. 2 · 5 는 답변입니다.

---

## 1. `updated_at` — 프론트 진단이 정확했습니다

> **"썸네일 작업을 돌리는 워커가 재기동되지 않은 것으로 보입니다"** — 맞습니다.

### dev: 워커가 10일째 옛 코드였습니다

dev 는 소스를 **바인드 마운트**로 물려서, 파일을 고치면 웹 프로세스는 자동 재시작하지만
**Celery 워커는 재시작하지 않습니다**. 디스크에는 새 코드가 있는데 워커 메모리에 올라간
모듈은 10일 전 것이었습니다. 그래서 `reason_code` 는 새 코드인데 썸네일 작업만 옛 코드인,
정확히 관찰하신 상태가 됐습니다. **워커를 재기동했습니다.**

### 제 1차 dev 확인이 틀렸습니다

지적하신 두 가지가 다 맞았고, 사실 하나 더 있었습니다.

1. 3회 반복이 **5분 억제 창 안**에 들어가 2·3회차는 작업을 예약조차 안 했습니다.
2. 썸네일 작업은 **비동기**인데 저는 GET 직후 바로 `updated_at` 을 읽었습니다 —
   워커가 처리하기 전이라 안 바뀐 게 당연했습니다.
3. 그리고 워커 자체가 옛 코드였습니다.

알려주신 절차(**재시드 → 첫 GET → 워커 처리 대기**)로 다시 확인했습니다:

```
대상 캠페인: 가을 신상 미리 알림
시드 직후   : updated_at=06:37:02.842765  attempts=0
캠페인 목록 GET 1회: HTTP 200
  -> 1초 뒤 태스크 실행됨 (attempts 0 -> 1)      ← 작업이 실제로 돌았다
GET 이후    : updated_at=06:37:02.842765  attempts=1   ← 그대로
>>> updated_at 이 밀렸나: 아니오 — 정상
```

`attempts` 가 올라간 것이 핵심입니다 — **작업이 돌아서 저장까지 했는데** `updated_at` 만
안 움직였다는 뜻이라, "작업이 아예 안 돌아서 안 바뀐 것"과 구별됩니다.

### 운영: 배포 때 교체됐고, 운영 데이터로 증명했습니다

썸네일 작업은 기본 큐에서 돌고, 그 워커(`turnflow_instagram_celery`)는 배포 때
새 이미지로 **재생성**됐습니다(dev 와 달리 운영은 코드가 이미지에 구워져 있어 스테일이 없습니다).

말로만 하기엔 약해서, 운영 실트래픽으로 확인했습니다. 썸네일 저장이 **마지막 쓰기였는지**
(= `updated_at` 과 `thumbnail_synced_at` 이 1초 안에 붙었는지)를 배포 전후로 비교했습니다.

| 구간 | 썸네일 동기화 | 그중 `updated_at` 이 썸네일 저장에 붙은 것 |
|---|---|---|
| **배포 후** | 16건 | **0건** |
| **배포 전**(대조군) | 2,000건 | **386건 (19%)** |

배포 전 19%가 이 버그였고, 배포 후 0% 입니다. **운영은 고쳐져 있습니다.**

---

## 2. `Accept-Language` — 운영도 같습니다

서버는 `Accept-Language` 를 **보지 않습니다.** 운영도 dev 와 동일합니다.

| 설정 | 값 |
|---|---|
| `LANGUAGE_CODE` | `ko-kr` |
| `LocaleMiddleware` | **없음** → 요청별 언어 활성화 자체를 하지 않습니다 |
| 번역 카탈로그(.po) | **없음** |

그리고 서버 문장들은 `gettext()` 로 감싸져 있지 않은 **평문 한국어 리터럴**입니다 —
미들웨어만 켜도 번역될 것이 없습니다. DRF 기본 문구(401 등)는 Django 내장 번역이 있어
미들웨어를 켜면 영어가 나오지만, **우리 문장은 전부 그대로 한국어**입니다.

**→ 말씀하신 방향(머신 코드를 받아 프론트가 문장을 붙인다)이 맞다고 봅니다.**
전면 i18n 은 수백 개 문장을 `gettext` 로 감싸고 .po 를 유지해야 해서 별도 과제입니다.
머신 코드가 없어 막히는 곳이 또 있으면 알려 주세요 — 같은 방식으로 붙이겠습니다.

**예외 하나**: §4-1 의 **인스타 연결 결과 페이지는 번역했습니다.** 거기는 서버가 HTML 을
통째로 그려서 프론트가 문장을 붙일 수 없기 때문입니다.

---

## 3. 제휴코드 — 머신 코드 없던 두 곳에 `reason_code` 추가

`validate` 와 **같은 어휘**를 씁니다. 한 개념에 두 어휘를 두면 프론트가 분기를 두 벌 들게 됩니다.

| `reason_code` | 뜻 | 어디서 |
|---|---|---|
| `not_found` · `inactive` · `not_yet_valid` · `expired` · `exhausted` | 코드 **자체**의 상태 | validate · redeem · toss/confirm |
| `already_used` | 이 **사용자**가 이미 코드를 썼다 | redeem · toss/confirm (비인증 validate 는 불가) |
| `code_required` | 코드를 안 보냈다 | redeem · toss/confirm |

**위치는 최상위와 `error.details` 양쪽입니다** — 둘 중 편한 쪽을 쓰세요.

```jsonc
// POST /billing/referral/redeem/  400
{ "detail": "유효 기간이 만료된 코드입니다.",
  "reason_code": "expired",                       // ← 최상위
  "success": false,
  "error": { "code": 400, "message": "유효 기간이 만료된 코드입니다.",
             "details": { "reason_code": "expired" } } }   // ← error.details
```

`POST /billing/toss/confirm/` 이 제휴코드 때문에 400 을 낼 때도 **같은 키·같은 값**입니다.

### 기존 `code` 와의 관계

구독 **상태** 때문에 막히는 경우는 지금처럼 `code` 로 옵니다. 두 키는 성격이 다릅니다.

| 키 | 값 | 뜻 |
|---|---|---|
| `reason_code` | 소문자 | **코드/사용자** 때문에 못 쓴다 |
| `code` | `REFERRAL_NOT_TRIALING` · `REFERRAL_PLAN_MISMATCH` | **구독 상태** 때문에 못 쓴다 |

### OpenAPI 예시

지적하신 대로 `inactive` · `expired` · `exhausted` 예시에 `reason_code` 가 없었고
`not_yet_valid` 예시는 아예 없었습니다. **4건 모두 보완했습니다.**

---

## 4. 인스타 연결 결과 페이지

### 4-1. 영어 지원 — 추가했습니다

`Accept-Language` 를 보고 **ko / en** 중에 고릅니다. q-값을 존중합니다.

| `Accept-Language` | 결과 |
|---|---|
| `en-US,en;q=0.9,ko;q=0.5` (프론트 영어 화면) | **en** |
| `ko-KR,ko;q=0.9,en;q=0.5` | ko |
| `en` · `en-GB` | en |
| 없음 · 미지원 언어 · 깨진 헤더 | **ko** (기본) |
| `en;q=0.4,ko;q=0.4` (동점) | ko (기본 언어가 이김) |

8개 결과 페이지 전부 번역했습니다 — `<html lang>` · `<title>` · 제목 · 설명 ·
「창 닫기」 버튼 · 자동 닫힘 문구 · 계정/유형 라벨까지.

> ⚠️ **`errorCode` 와 `reason` 은 번역하지 않습니다.** 머신 키라서 번역되는 순간
> 프론트의 사유 분기가 통째로 깨집니다. 두 언어 모두에서 값이 같다는 걸 테스트로 고정했습니다.
> `message`(사람이 읽는 문장)만 언어를 따라갑니다.

```jsonc
// Accept-Language: en 일 때도 머신 키는 그대로
{ "errorCode": "ALREADY_CONNECTED_ELSEWHERE",
  "reason":    "ALREADY_CONNECTED_ELSEWHERE",
  "message":   "This Instagram account is already connected to another workspace (ow***@…)." }
```

### 4-2. 운영 콜백 주소 — **불일치 없습니다**

운영의 `INSTAGRAM_REDIRECT_URI` 를 직접 확인했습니다:

```
https://turnflow-api.clfy.ai.kr/api/v1/integrations/instagram/connect/callback/
```

**콜백 페이지는 `https://turnflow-api.clfy.ai.kr` 에서 열립니다** — 운영 번들의 API 주소와
같습니다. 지금 운영에서 팝업 메시지가 버려지고 있지 **않습니다.**

제 2차 회신에 `https://api.turnflow.link` 라고 쓴 건 제가 검증용 curl 에 쓴 호스트일 뿐입니다.
이 백엔드는 여러 호스트명으로 들어올 수 있고(둘 다 유효합니다), **OAuth 콜백이 실제로
열리는 주소는 위 `redirect_uri` 하나**입니다. 혼동을 드려 죄송합니다.

> 말씀하신 대로 **`redirect_uri` 의 주소를 믿도록** 고치시는 쪽이 맞습니다 —
> 그래야 호스트명이 바뀌어도 프론트가 안 깨집니다.

---

## 5. 밀린 댓글 DM — 질문 3개

### 5-1. 「다음 게시물」(`next_media`) 캠페인도 대상입니다 ✅

게시물이 **붙는 순간 `trigger_type` 이 `specific_media` 로 바뀝니다**
(`attach_next_media_single_active`). 그래서 붙은 뒤에는 「특정 게시물」과 완전히 같습니다.

아직 안 붙은 `next_media` 는 **게시물 자체가 없어서** 놓칠 댓글도 없습니다.

**→ 문구를 「"특정 게시물"로 만든 캠페인」으로 좁히실 필요 없습니다.** 제안드리면:

> 「특정 게시물·다음 게시물로 만든 캠페인은 …」
> 또는 그냥 「게시물에 걸어둔 캠페인은 …」

### 5-2. 재연결 되살림은 캠페인 종류를 **가리지 않습니다** ✅

두 메커니즘이 **서로 다른 것을 덮습니다.**

| | 무엇을 덮나 | 캠페인 종류 제한 |
|---|---|---|
| **재연결 되살림**(`ig_disconnected` 해소) | 이미 **시도했다가 실패한** 건 (로그가 남아 있음) | **없음** — any_media·스토리 답장 포함 |
| **밀린 댓글 보정**(1시간 배치) | **시도조차 못 한** 건 (웹훅이 안 와서 로그가 없음) | 게시물에 걸린 캠페인만 |

토큰이 죽어 있어도 웹훅은 계속 들어와 로그가 생기고 발송만 실패합니다. 재연결하면 그
로그들이 종류 무관하게 되살아납니다.

**→ `ig_disconnected` 카드의 현재 문구는 그대로 두셔도 됩니다.**

### 5-3. 「1시간 안에」의 기준점 — 알림에 따라 다릅니다

밀린 댓글 보정은 **고정 1시간 주기**로 돌고, 복구 버튼과 무관하게 알아서 돕니다
(운영에서 가동 중인 걸 확인했습니다 — 마지막 07:56 → 다음 08:56).

| 알림 | 기준점 | 왜 |
|---|---|---|
| `comment_stream_down`(댓글 받기 끊김) | **댓글이 달린 시점**부터 최대 1시간 | 웹훅만 죽은 상태라 배치는 계속 돈다. **복구 버튼을 안 눌러도 나갑니다** |
| `ig_disconnected`(연결 끊김) | **재연결 시점**부터 최대 1시간 | 연결이 죽은 동안은 배치가 그 계정을 건너뛴다 |

**→ `comment_stream_down` 문구 제안**:

> 「끊긴 동안 달린 댓글도 **달린 지 7일 이내라면 1시간 안에** DM이 자동으로 발송됩니다.」

지금 쓰려던 문구에서 「특정 게시물」만 빼면 정확합니다(5-1 참고).
"복구 버튼을 눌러야 나간다"는 뉘앙스만 피해 주시면 됩니다 — 버튼은 **앞으로 올 댓글**을
실시간으로 받게 하는 것이고, 밀린 건은 배치가 따로 메웁니다.

---

## 6. dev 스토리 502 — 고쳤습니다

**`list_stories` 에 목 분기가 아예 없었습니다.** 가짜 토큰이 그대로 Meta 로 나가 400 을
받고, 우리가 그걸 502 로 되돌린 것입니다. **게시물 API 500 과 똑같은 계열의 누락**이었습니다.

프론트가 보신 4개 계정 전부 확인했습니다:

```
H01: HTTP 200 · 3건      H05: HTTP 200 · 2건
H04: HTTP 200 · 3건      H18: HTTP 200 · 3건
```

### 스토리의 `media_url` 도 게시물 목록과 같은 규약입니다

**렌더 가능한 미디어 URL** 입니다(운영은 Graph 패스스루라 IG CDN URL).
`permalink` 는 별도 키입니다. **프론트의 `thumbnail_url` → `media_url` 폴백 그대로 두세요.**

```jsonc
// GET .../workspaces/{ws}/stories/   (dev)
{ "count": 3, "data": [
    { "id": "ms-…-0", "media_type": "VIDEO", "media_product_type": "STORY",
      "media_url": "data:image/svg+xml;…",        // 렌더됨 (운영=IG CDN URL)
      "thumbnail_url": "data:image/svg+xml;…",     // 영상일 때만. 이미지면 null
      "permalink": "https://www.instagram.com/stories/…", "timestamp": "…", "caption": "" } ],
  "connection": { "id": "…", "username": "…", "account_id": "…" }, "mock": true }
```

> ⚠️ 스토리는 24시간만 살아 있어서 `timestamp` 를 **조회 시점 기준 상대값**으로 만듭니다.
> 재시드 없이 다음 날 열어도 "만료된 스토리"가 남지 않습니다.

---

## 7. 프론트 정정에 대한 답

> 「PC 팝업 메시지에 `errorCode` 는 원래 있었고, 프론트가 그 값을 읽지 않았습니다.」

확인했습니다. 다만 **`reason` 별칭은 그대로 두겠습니다** — 같은-탭은 `reason=`,
팝업은 `errorCode:` 로 키 이름이 갈려 있던 것 자체가 이번 같은 착오를 만들기 쉬운
구조였습니다. `reason ?? errorCode` 로 읽으시면 두 경로가 한 줄로 끝납니다.

---

## 8. 바뀐 파일

| 항목 | 파일 |
|---|---|
| 3 `reason_code` | `apps/billing/toss_flows.py` · `toss_views.py` · `referral_views.py` · 테스트 `test_referral_validate.py` |
| 4-1 콜백 영어 | `apps/integrations/oauth_callback_pages.py` · `views.py` · 테스트 `test_oauth_callback_i18n.py` |
| 6 스토리 목 | `apps/integrations/services.py` · `views.py` · 테스트 `test_mock_media.py` |

검증: 변경 앱 6개 테스트 **1,401건 통과**, black·ruff·`manage.py check` 통과.
