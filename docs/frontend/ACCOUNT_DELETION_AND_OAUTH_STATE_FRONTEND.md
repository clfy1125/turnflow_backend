# 회원탈퇴 차단 해제 + 소셜 로그인 state 유실 — 프론트 작업 요청서

> 2026-09-22 · 백엔드 → 프론트
> 발단: CS #247995c5 (`tjd****@naver.com`, 프로(cancelled))
> — *"원래 아이디가 있는데 카카오톡으로 새로 가입해버려서 탈퇴하고 싶은데요ㅠㅠ"*
>
> 한 건의 문의로 보였지만 실서버 계측을 따라가니 **서로 다른 결함 3개**가 한 사람에게
> 연달아 터진 것이었습니다. 백엔드 몫은 **2026-09-22 운영 배포를 마쳤고**(§2, `706b995`),
> 이 문서는 프론트 몫(§3)입니다.

---

## 0. 요약 — 누가 무엇을 하나

| # | 문제 | 담당 | 상태 |
|---|---|---|---|
| B-1 | 카드 없는 자동 프로체험이 탈퇴를 409로 막음 | 백엔드 | ✅ **운영 배포 완료** (`706b995`) |
| B-2 | 409 응답에 `detail` 없음 → 프론트가 사유를 못 읽음 | 백엔드 | ✅ **운영 배포 완료** (`706b995`) |
| **F-1** | **탈퇴 모달이 401 외 모든 오류를 generic 문구로 뭉갬** | **프론트** | 요청 |
| **F-2** | **`/delete-account`(공개 탈퇴) 진입점이 앱 안에 없음** | **프론트** | 요청 |
| **F-3** | **OAuth `state` 를 sessionStorage 에 두어 탭이 바뀌면 로그인 전멸** | **프론트** | 요청 (가장 큼) |

**F-3 이 이 사고의 뿌리입니다.** 고객은 탈퇴가 하고 싶었던 게 아니라 **원래 계정으로
로그인이 안 돼서** 새로 가입했고, 그 새 계정을 지우려다 B-1 에 막혔습니다.

---

## 1. 실제로 무슨 일이 있었나 (전부 실서버 계측)

`FunnelEvent` 기준, 같은 `visitor_id` 의 31분입니다. 환경은 **iOS · 네이버 앱 인앱 브라우저**.

| 시각(UTC) | 이벤트 | 해석 |
|---|---|---|
| 13:41:23 ~ 13:42:47 | `login_provider_click` ×5 | 로그인 시도, 전부 세션 안 붙음 |
| **13:42:56** | `auto_trial_granted` (`provider: kakao`) | **원치 않은 신규 가입** + 프로 30일 자동 지급 |
| 13:43:03 | `first_action_done` | 가입된 줄 모르고 8초 만에 페이지 생성 |
| 13:43:49 ~ 13:45:03 | `login_provider_click` ×2 | 여전히 원래 계정을 찾는 중 |
| **13:46:12** | **`login_error` `code: state_mismatch`** | `/auth/kakao/callback` — F-3 |
| 13:46:12 | `signup_modal_view` | 실패 → 로그인 모달 재노출(무한 루프의 시작) |
| 13:48:49 ~ 13:50:37 | `signup_modal_view`·click ×4 | 계속 실패 |
| **13:50:25** | `subscription_cancel_scheduled` (사유 `paused`) | **해지 의사 없이** 탈퇴하려고 구독부터 해지 |
| 14:11 ~ 14:12 | 로그인 시도 ×2 | 여전히 못 들어감 |
| ~14:36 | CS 접수 | |

로그인 버튼을 **12번** 눌러 성공은 0번, 대신 계정 하나가 생겼습니다.

### 이 함정에 빠진 사람이 더 있습니다

- **`state_mismatch` 60건 / 36명** (`login_error` 총 121건 중 최다).
  provider 별로 **instagram 32 · kakao 28** — 두 경로 모두입니다.
  환경별로는 **android·비인앱 55건**, ios·네이버인앱 3건, android·카카오톡인앱 2건
  (§3 F-3 에서 설명: *콜백이 도착한 곳*이 기록되기 때문에 비인앱으로 찍힙니다).
- **카드 없는 자동 프로체험 217건 중 해지 5건인데, 4건이 가입 1시간 이내**
  (3.4분 · 5.3분 · 7.5분 · 31.1분). 체험을 써보고 안 맞아서 해지한 게 아니라,
  **탈퇴가 막혀서 우회로로 해지한 것**입니다. 이탈 지표가 그만큼 오염돼 있습니다.
- 공개 탈퇴 경로 `/delete-account` 는 **역대 사용 1건**(2026-08-21, 테스트로 추정),
  유예 중 계정 **0건**. 페이지는 멀쩡히 살아 있는데 아무도 못 찾습니다 → F-2.

---

## 2. 백엔드에서 이미 고친 것 (프론트가 알아야 할 계약 변경)

`DELETE /api/v1/auth/me/delete/`

### B-1. 카드 없는 무료 체험은 더 이상 막지 않습니다

409 차단 조건에 **빌링키 보유**가 추가됐습니다.

```python
if sub and sub.is_paid_plan and sub.status in blocking_statuses and sub.has_billing_key:
```

차단의 근거였던 *"잔여 유료기간이 환불 없이 소멸 + 토스 빌링키 고아"* 는 **카드가 등록돼
있을 때만 성립**합니다. 가입 1초 뒤 자동 지급되는 프로 체험은 정리할 빌링키도, 환불을
따질 결제도 없습니다. 이제 그대로 **204** 로 삭제됩니다.

> 카드가 등록된 유료 구독(`active`/`trialing`/`past_due`/`paused`)은 **여전히 409** 입니다.
> 잔여기간 소멸 동의를 앱에서 받고 있지 않기 때문입니다(F-2 의 `/delete-account` 가 그 동의를 받습니다).

### B-2. 409 응답이 사유를 두 가지 형태로 동시에 담습니다

이 뷰는 `Response` 를 직접 만들어 DRF 예외 핸들러를 거치지 않습니다. 그래서 `detail` 과
공통 envelope 를 **같이** 실어 보냅니다 — 어느 쪽을 읽어도 같은 문구입니다.

```jsonc
// HTTP 409
{
  "detail": "구독 중에는 탈퇴할 수 없습니다. 먼저 요금제/결제 설정에서 구독을 해지한 뒤 다시 시도해 주세요.",
  "success": false,
  "error": {
    "code": 409,
    "message": "구독 중에는 탈퇴할 수 없습니다. 먼저 요금제/결제 설정에서 구독을 해지한 뒤 다시 시도해 주세요.",
    "details": {
      "code": "active_subscription",
      "plan": "pro",
      "status": "active",
      "web_deletion_url": "https://turnflow.link/delete-account"
    }
  }
}
```

`web_deletion_url` 이 신규 필드입니다. **구독을 해지하지 않아도 탈퇴가 진행되는 공개 경로**로,
서버가 구독을 즉시 해지하고 7일 유예 후 파기합니다(잔여기간 소멸 동의를 그 화면에서 받습니다).

**응답 코드 표**

| 코드 | 의미 | 프론트 처리 |
|---|---|---|
| 204 | 삭제 완료 | 토큰 3종 삭제 → 홈/로그인 |
| 401 | 미인증 | 로그인 유도 |
| 409 `active_subscription` | 카드 등록 구독 중 | **사유 표시** + `web_deletion_url` 안내 |

---

## 3. 프론트 작업 요청

### F-1. 탈퇴 모달이 401 외의 사유를 버리고 있습니다 (우선순위 1, 작업량 최소)

2026-09-22 배포본(`assets/SettingsPage-Pq-ewkn3.js`) 기준 현재 핸들러입니다:

```js
const s = await De.delete("/api/v1/auth/me/delete/");
s.ok ? (localStorage.removeItem("access_token"), /* … */ Qt("/home"))
     : s.status === 401 ? ee(t("settings.deleteModal.needLogin"))
                        : ee(t("settings.deleteModal.errorDefault"));   // ← 409가 여기로 사라진다
```

백엔드가 쓴 *"먼저 구독을 해지한 뒤 다시 시도해 주세요"* 가 **화면에 도달하지 못합니다.**
고객이 이유를 모른 채 스스로 짐작해 구독을 해지한 것이 이번 사고의 절반입니다.
`QuoteGate` 사고(400 사유를 버리고 "잠시 후 다시 시도"로 뭉갬)와 같은 형태입니다.

**이미 있는 파서를 쓰면 됩니다.** 번들 안에 `detail` / `error.message` / `error.details` 를
전부 훑는 헬퍼가 이미 존재합니다(minified `Ss()` / `k0()` — `parseApiError` 계열).
탈퇴 모달만 이 경로를 안 타고 있습니다.

```ts
const res = await api.delete('/api/v1/auth/me/delete/');
if (res.ok) { /* 기존 성공 처리 유지 */ return; }

if (res.status === 401) { setError(t('settings.deleteModal.needLogin')); return; }

const { detail, details } = parseApiError(res.body);      // 기존 헬퍼 재사용
setError(detail ?? t('settings.deleteModal.errorDefault'));

if (details?.code === 'active_subscription' && details?.web_deletion_url) {
  // "구독을 해지하지 않고 탈퇴하기" 보조 CTA 노출
  setSecondaryCta({ label: t('settings.deleteModal.deleteViaWeb'), href: details.web_deletion_url });
}
```

> ⚠️ `detail` 은 **지우지 말고 계속 읽어 주세요.** 백엔드는 envelope 과 `detail` 을 영구히
> 동시 송출합니다(기존 화면 호환). 한쪽만 읽어도 동작하지만 `details.code` 는 envelope 에만 있습니다.

### F-2. 앱 안에 `/delete-account` 진입점이 없습니다

`https://turnflow.link/delete-account` 는 **자기완결 정적 페이지**로 이미 배포돼 있습니다
(`<title>회원탈퇴 | TurnFlow</title>`, 외부 스크립트 0). 로그인 없이 이메일 소유 증명만으로
탈퇴가 되는 Google Play 정책 대응 경로이고, **구독 중이어도 진행**됩니다.

그런데 **역대 사용 1건**입니다. 앱 어디에서도 링크하지 않기 때문입니다.

요청:

1. 설정 > 회원 탈퇴 화면 하단에 상시 링크 — *"로그인이 어려우신가요? 이메일로 탈퇴하기"*
2. F-1 의 409 보조 CTA (위 코드)
3. 로그인 화면의 계정 문제 안내에도 링크

> 두 경로의 **정책이 다르다**는 점을 문구에 반드시 반영해 주세요.
> 앱 내 탈퇴 = **즉시 완전 삭제(복구 불가)** / 웹 탈퇴 = **7일 유예 후 파기(메일 링크로 복구 가능)**.
> 앱 내 탈퇴에 "7일 안에 복구할 수 있어요" 라고 쓰면 **거짓 고지**가 됩니다.

### F-3. OAuth `state` 가 sessionStorage 에 있어 탭이 바뀌면 로그인이 전멸합니다 ★

현재 구현(배포본에서 확인):

| | 카카오 | 인스타그램 |
|---|---|---|
| 저장소 | **`window.sessionStorage`** | **`window.sessionStorage`** |
| 키 | `kakao_oauth_state`, `kakao_oauth_ctx` | `instagram_oauth_ctx` (`{…, state}`) |
| state 발급 | **프론트**가 `crypto.randomUUID()` | **백엔드**가 `/auth/instagram/start/` 에서 발급 |
| 콜백 검증 | 프론트가 로컬 비교 | **프론트가 로컬 비교 + 백엔드도 서버 검증** |

콜백 처리:

```js
function readState() {                                 // qN()
  const st = sessionStorage.getItem('kakao_oauth_state');
  sessionStorage.removeItem('kakao_oauth_state');      // ← 읽자마자 삭제
  sessionStorage.removeItem('kakao_oauth_ctx');
  return { expectedState: st, ctx };
}
function verify(search, expected) {                    // WN()
  if (!state || !expected || state !== expected) return { kind: 'state_mismatch' };
  …
}
```

**`!expected` 도 `state_mismatch` 로 떨어집니다.** 즉 "위조됐다"가 아니라 **"우리가 기억을
잃었다"** 인 경우까지 공격으로 취급해 로그인을 중단하고, 곧바로 로그인 모달을 다시 엽니다
— 계측에 찍힌 `login_error` → `signup_modal_view` 무한 루프가 이것입니다.

**기억을 잃는 경로 3가지 (전부 실사용에서 발생):**

1. **sessionStorage 는 탭 단위입니다.** 인앱 브라우저에서 시작 → 카카오톡 앱/외부 브라우저로
   핸드오프 → **다른 탭·웹뷰로 복귀** 하면 `expectedState` 가 `null` 입니다.
   *`state_mismatch` 55건이 `in_app=false`·android 로 찍힌 이유가 이것입니다 — 계측은
   **콜백이 도착한 환경**을 기록하므로, 인앱에서 출발해 일반 Chrome 탭으로 돌아오면
   "인앱 아님"으로 남습니다. 인앱과 무관한 문제로 오독하기 쉽습니다.*
2. **콜백 URL 새로고침·뒤로가기.** 첫 읽기에서 키를 지우므로 두 번째부터는 무조건 실패입니다.
3. **저장소 차단.** iOS 시크릿/ITP, "쿠키와 사이트 데이터 차단" 설정에서 `setItem` 이 throw 되고
   `catch {}` 로 조용히 삼켜집니다.

**요청 (효과 순):**

- **F-3-a. 인스타그램은 로컬 비교를 아예 제거해 주세요.** state 는 **백엔드가 발급하고
  백엔드가 검증**합니다(`InstagramLoginState`, 1회용·TTL 10분, 재사용 시 `INSTAGRAM_STATE_USED` 400).
  프론트의 로컬 비교는 **순수한 중복**이고, 복구 가능한 상황을 하드 실패로 바꾸고 있을 뿐입니다.
  콜백의 `code` / `state` 를 그대로 `POST /auth/instagram/` 에 넘기고 **백엔드 판정을 쓰세요.**
  → 이것만으로 `state_mismatch` **32건이 사라집니다. 백엔드 작업 0.**
- **F-3-b. 카카오는 `sessionStorage` → `localStorage`** 로 옮기고 `ctx.startedAt` 기준
  **10분 TTL** 로 만료시켜 주세요. 탭 핸드오프에서 살아남습니다.
- **F-3-c. 읽자마자 지우지 마세요.** 교환이 끝난 뒤(성공이든 확정 실패든) 삭제하면
  새로고침 한 번에 죽는 경로 2가 사라집니다.
- **F-3-d. `expected` 가 없을 때는 "중단"이 아니라 "재시작"으로.** 지금은 오류를 띄우고
  같은 자리로 돌려보내 루프가 됩니다. 기억이 없으면 조용히 authorize 를 **한 번 다시 태우는 편**이
  사용자에게는 정상 로그인으로 보입니다(1회만, 무한루프 가드 필요).

> **백엔드가 대신 해 줄 수 있는 것:** 카카오도 인스타처럼 **서버 발급 state** 로 바꿀 수 있습니다
> (`InstagramLoginState` 와 같은 구조 — `GET /auth/kakao/start/` 신설 → `{authorize_url, state}`).
> 그러면 브라우저 저장소 의존이 통째로 사라집니다. 다만 프론트 계약이 바뀌므로 **먼저 만들지
> 않았습니다** — 필요하다고 하시면 붙이겠습니다. 인스타 쪽 구현을 그대로 복제하면 됩니다.

---

## 4. 재현 방법

**F-1 (409 사유 표시)** — dev 에서:

1. 카드 등록된 프로 계정으로 로그인 (`TOSS_DEV_CARD_AUTH_ENABLED=true` 헬퍼 사용)
2. 설정 > 회원 탈퇴 실행 → 409
3. 기대: 화면에 *"구독 중에는 탈퇴할 수 없습니다…"* + "구독 해지 없이 탈퇴하기" CTA

**B-1 회귀 확인** — 카드 없는 자동 체험 계정으로 탈퇴 → **204** 여야 합니다.
백엔드 테스트가 이 두 갈래를 고정합니다: `apps/authentication/tests/test_account_delete_gate.py`
(고장난 버전에 먼저 돌려 2건이 실패하는 것까지 확인했습니다.)

**F-3 (state 유실)** — 실기기 필요:

1. 네이버/인스타 앱 안에서 `turnflow.link` 열기 (인앱 브라우저)
2. 카카오 로그인 → 카카오톡 앱으로 핸드오프 → 복귀
3. 현재: `login_error state_mismatch` + 로그인 모달 재노출
4. 간이 재현(기기 없이): 로그인 시작 후 콜백 URL 도착 시 **새로고침** → 같은 증상

---

## 5. 관련 계약 (변경 없음, 참고용)

| 엔드포인트 | 용도 | 비고 |
|---|---|---|
| `DELETE /api/v1/auth/me/delete/` | 앱 내 탈퇴 | **즉시 하드 삭제**, 유예 없음 |
| `GET /api/v1/auth/deletion/policy/` | 삭제·법정보존 고지문 | 서버가 단일 소스 (프론트 하드코딩 금지) |
| `POST /api/v1/auth/deletion/request/` | 이메일로 탈퇴 링크 발송 | 열거 방지 — **항상 200·동일 문구** |
| `POST /api/v1/auth/deletion/verify/` | 확인 화면 데이터 | 토큰 소비 안 함 |
| `POST /api/v1/auth/deletion/confirm/` | 탈퇴 확정 | 구독 자동 해지 + 7일 유예 |
| `POST /api/v1/auth/deletion/restore/` | 유예 중 복구 | |
| `POST /api/v1/auth/kakao/` | 카카오 로그인 | 탈퇴 유예 계정은 **409** |
| `GET /api/v1/auth/instagram/start/` | IG 로그인 시작 | **서버 발급 state** 반환 |
| `POST /api/v1/auth/instagram/` | IG 로그인 교환 | **서버가 state 검증** |

---

## 6. 하지 않기로 한 것 (되살리기 전에 이유부터 볼 것)

- **`me/delete/` 에서 409 를 통째로 없애고 구독을 자동 해지하는 것.**
  공개 웹 경로는 그렇게 하지만, 그쪽은 *"잔여 유료기간이 환불 없이 소멸한다"* 는 **별도 동의를
  확인 화면에서 받습니다.** 앱에 그 동의 화면이 생기기 전에 자동 해지를 붙이면 **고지 없이
  돈을 소멸**시키게 됩니다. 앱에도 같은 동의 화면을 만들 계획이면 그때 함께 바꾸면 됩니다.
- **카카오 `state` 검증 자체를 없애는 것.** CSRF 방어가 사라집니다. F-3-a(인스타)는
  *백엔드가 이미 검증하므로* 로컬 비교만 지우는 것이지, 검증을 없애는 게 아닙니다.
- **`is_email_verified=false` 인 카카오 이메일로 기존 계정 자동 연결.** 계정 탈취 경로입니다
  (구글과 동일 게이트). 지금처럼 403 으로 막고 비밀번호 로그인을 안내하는 게 맞습니다.
