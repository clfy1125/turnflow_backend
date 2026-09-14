# 긴급 전환 개선 — 백엔드 3차 회신 (변경분만)

작성 2026-09-13 · **2차 회신서(`URGENT_CONVERSION_BACKEND_RESPONSE_2.md`) 전달 이후 바뀐 것만** 담았습니다.
§2~§6(이메일 등록 API·배포·체험 만료·`trial_kind`)은 **변경 없음** — 2차 회신서 그대로입니다.

| 무엇 | 상태 |
|---|---|
| A. 인스타 로그인 dev 활성화 (2차 §1 대체) | ✅ 완료 — **터널 불필요** |
| B. **인스타 로그인 계정 매칭 정책 변경** | ⚠️ **프론트 처리 필요** (409 신규) |

---

## A. 인스타 로그인 — dev 켜짐 (2차 회신서 §1 을 이걸로 대체해 주세요)

2차 회신서 §1 은 "콘솔 작업이 막혔습니다 / 터널 주소를 주세요" 였는데, **전부 해결됐습니다.**
터널 만드실 필요 없습니다.

### Meta 앱이 두 개였습니다

| 앱 | Facebook App ID | Instagram App ID |
|---|---|---|
| TurnFlow **dev** | 959975189724026 | 1577778924348206 |
| TurnFlow (**운영**) | 1497161828629570 | **36036852472566509** |

각각 맞는 곳에 등록했습니다(저장 후 새로고침으로 반영 확인, 기존 항목은 손대지 않음):

- **dev 앱** ← `https://turnflowlink-dev.pages.dev/auth/instagram/callback`
- **운영 앱** ← `https://app.turnflow.link/auth/instagram/callback` (미리 등록만, 기능은 OFF)

### 🔴 `http://localhost` 는 등록이 안 됩니다 (확정)

dev 앱에 `http://localhost:3006/auth/instagram/callback` 을 넣고 저장해 봤습니다.
입력창은 받아 주는데 **저장하면 오류 메시지도 없이 조용히 사라집니다.**
Instagram Business Login 의 리디렉션 URI 는 **HTTPS 전용**입니다.

### ✅ 그래서 dev 는 Pages 프리뷰로 갑니다

`turnflowlink-dev` Cloudflare Pages 배포가 이미 있어서 그걸 씁니다. 백엔드 dev 설정도 끝냈습니다.

```bash
# dev .env (적용 완료, web 재시작 완료)
INSTAGRAM_LOGIN_ENABLED=True
INSTAGRAM_LOGIN_REDIRECT_URI=https://turnflowlink-dev.pages.dev/auth/instagram/callback
CORS_ALLOWED_ORIGINS=...,https://turnflowlink-dev.pages.dev
```

dev-api 종단 확인:

```bash
curl "https://dev-api.turnflow.link/api/v1/auth/instagram/start/"
# → authorize_url 정상 (client_id=1577778924348206, redirect_uri=turnflowlink-dev.pages.dev/...)
curl "https://dev-api.turnflow.link/api/v1/auth/instagram/start/?redirect_uri=https://evil.example.com/cb"
# → 400 INSTAGRAM_INVALID_REDIRECT_URI
```

**프론트에서 `/auth/instagram/callback` 라우트만 배포하면 바로 테스트됩니다.**

> ⚠️ IG 로그인은 **배포된 dev 빌드**에서만 테스트됩니다. `localhost:3006` 은 불가합니다.
> 로컬로 꼭 하셔야 하면 각자 PC 에서 `cloudflared tunnel --url http://localhost:3006`
> (로그인 불필요) 띄우고 주소를 주세요 — 다만 재시작마다 바뀌어 그때마다 Meta 콘솔에
> 다시 등록해야 합니다.

**운영은 계속 `INSTAGRAM_LOGIN_ENABLED=False`** 입니다. 리디렉션 URI 만 미리 넣어 둔 상태라
운영에서는 두 엔드포인트가 404 `INSTAGRAM_LOGIN_DISABLED` 를 냅니다.

### 배포 전 확인 부탁

운영 `.env` 의 `INSTAGRAM_APP_ID`/`INSTAGRAM_APP_SECRET` 이 **운영 앱 값(36036852472566509)** 인지
봐 주세요. dev 값(1577778924348206)이 들어가 있으면 운영에서 `INSTAGRAM_CODE_INVALID` 로만
떨어져 원인 찾기가 매우 어렵습니다.

---

## B. 인스타 로그인 계정 매칭 — **정책이 바뀌었습니다 (프론트 처리 필요)**

2차 회신서에 적힌 매칭 규칙이 **바뀌었습니다.** 아래가 최종입니다.

### 바뀐 이유

내부 확인 중 이런 동작이 발견됐습니다:

> IG 계정 `@foo` 가 A 의 계정(구글 가입)에 연동돼 있을 때,
> **그 IG 에 접근할 수 있는 다른 사람**(대행사·직원)이 "인스타 로그인" 을 누르면
> → **A 의 계정에 그대로 로그인**됐습니다. 결제·구독 해지까지 전부 열렸습니다.

인스타 계정은 개인 신원이 아니라 **여러 사람이 공유하는 자산**이라(대행사·마케터·직원),
"IG 접근 권한 = 턴플로우 계정 전체 접근" 이 되는 구조였습니다. 그래서 막았습니다.

### 최종 규칙

```
① instagram_user_id 일치        → 그 계정으로 로그인   (= 인스타로 가입한 계정)
② 그 IG 가 다른 계정에 연동 중   → 409 거부  ← 신규
③ 그 외                          → 신규 가입
```

**핵심 한 줄: "인스타로 가입한 계정만 인스타로 로그인된다."**

이전에 있던 "그 IG 를 연동해 둔 워크스페이스의 owner 로 로그인" 규칙은 **제거**됐습니다.

### ⚠️ 프론트가 처리해야 할 것 — 새 409

```jsonc
409 {
  "detail": "이 인스타그램 계정은 이미 다른 턴플로우 계정에 연결되어 있습니다. 기존 계정에서 연동을 해제한 뒤 다시 시도해 주세요.",
  "code": "INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE",
  "masked_email": "ow***@example.com",
  "instagram_username": "myhandle",
  "success": false,
  "error": {
    "code": 409,
    "message": "…",
    "details": {
      "code": "INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE",
      "masked_email": "ow***@example.com",
      "instagram_username": "myhandle"
    }
  }
}
```

안내 문구 예시:

> **@myhandle 은 이미 다른 계정에 연결되어 있어요**
> `ow***@example.com` 계정에서 인스타그램 연동을 해제한 뒤 다시 시도해 주세요.

- `masked_email` — 본인 확인에는 충분하고 남의 이메일을 노출하진 않는 수준으로 마스킹돼
  있습니다(IG 연동 충돌 화면과 **같은 규칙**). **원본 이메일은 내려주지 않습니다.**
- `instagram_username` — 어느 인스타 계정인지 문구에 쓰라고 같이 보냅니다.
- 기존 계정에서 **연동을 해제하면** 다음 시도부터 ③ 으로 떨어져 **새 계정으로 가입**됩니다.

### 409 는 이제 두 종류입니다

| `code` | 뜻 |
|---|---|
| `INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE` | 이 IG 가 다른 계정에 연동 중 (신규) |
| `account_deletion_pending` | 탈퇴 유예 중인 계정 (기존) |

`code` 로 분기해 주세요.

### 같이 없어진 버그

연동을 **해제한 뒤에도** 한 번 인스타 로그인했던 구글 계정으로 계속 들어가던 문제가 있었습니다.
원인은 첫 로그인 때 그 구글 계정에 `instagram_user_id` 를 박아 버린 것이었습니다.
이제 이 값은 **인스타로 가입할 때 한 번만** 박히고, 그 뒤로 바뀌지 않습니다.

### 신규 가입 응답은 그대로입니다

```jsonc
200 {
  "user": { "id": 1234, "email": "ig_17841…@ig.invalid", "email_is_placeholder": true, … },
  "is_new_user": true,
  "tokens": { "access": "…", "refresh": "…" },
  "ig_connection": { "id": "…", "username": "myhandle", "status": "active" },
  "ig_connection_error": null
}
```

---

## C. 참고 — 인스타로 가입한 사람의 추가 IG 연동

질문 주셔서 실측했습니다. **구글 가입자와 완전히 동일**합니다 — 요금제 허용량 안에서만 됩니다.

```
IG 가입 직후   플랜 free · 허용량 1 · 이미 1개 연동됨
2번째 IG 시도  → PLAN_LIMIT_EXCEEDED
프로 체험 중에도 허용량 1 (프로도 기본 1개) → PLAN_LIMIT_EXCEEDED
```

⚠️ **다만 인스타 가입자는 가입과 동시에 IG 1개가 이미 채워진 상태로 시작합니다.**
다른 계정을 붙이려면 곧바로 추가 IG 계정 구매(월 9,900원)가 필요합니다.
그 안내가 없으면 "왜 안 되지?" 가 나올 수 있으니 화면에서 잡아 주세요.

---

## 변경된 것 요약 (2차 회신서 대비)

| 항목 | 2차 회신서 | 지금 |
|---|---|---|
| §1 인스타 로그인 | ⏳ 콘솔 막힘 · 터널 주소 요청 | ✅ dev 켜짐 · Pages 프리뷰 · 터널 불필요 |
| 매칭 규칙 ② | 그 IG 연동 워크스페이스 owner 로 로그인 | **409 거부** |
| 409 | `account_deletion_pending` 하나 | **두 종류** (`code` 분기 필요) |
| 그 외 (§2~§6) | — | **변경 없음** |

마이그레이션 추가 없음(1차·2차와 동일): `billing 0026` · `authentication 0007·0008·0009` · `analytics 0008·0009`
