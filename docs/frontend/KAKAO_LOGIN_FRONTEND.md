# 카카오 로그인 프론트 연동 가이드

작성 2026-09-10 · 백엔드 구현·카카오 콘솔 설정 완료 · **dev 실계정 종단간 검증 완료**
마이그레이션: `authentication 0006` (User.kakao_id) · `analytics 0007` (SignupKind.KAKAO)

---

## 0. 3줄 요약

1. 프론트가 `https://kauth.kakao.com/oauth/authorize?...` 로 **이동**시킨다 (SDK 불필요).
2. 카카오가 `redirect_uri` 로 `?code=...` 를 달고 되돌려 준다.
3. 프론트가 `POST /api/v1/auth/kakao/` 에 `{ code, redirect_uri }` 를 보내면 **우리 JWT** 가 나온다.

응답 형태는 구글 로그인(`POST /api/v1/auth/google/`)과 **똑같습니다**. 로그인 성공 후 처리
로직(토큰 저장·라우팅·`is_new_user` 전환 이벤트)은 그대로 재사용하시면 됩니다.

---

## 1. 카카오 콘솔 설정 현황 (백엔드가 이미 완료)

| 항목 | 값 |
|---|---|
| 앱 이름 / ID | 턴플로우 / `1573264` (**비즈 앱**) |
| **REST API 키** (= authorize 의 `client_id`) | `d71653959f6d7aa8e906adcea34f032d` |
| 카카오 로그인 사용 | **ON** |
| 동의항목 | 닉네임 `profile_nickname` = **필수 동의**<br>카카오계정(이메일) `account_email` = **필수 동의 + 값 없으면 수집** |
| 클라이언트 시크릿 | 활성화 ON (**서버 전용** — 프론트에 내려가지 않습니다) |

### 등록된 Redirect URI (이 4개만 동작합니다)

```
https://app.turnflow.link/auth/kakao/callback     ← 운영
https://turnflow.link/auth/kakao/callback         ← 랜딩 도메인에서 로그인시킬 경우
http://localhost:3000/auth/kakao/callback         ← 로컬(Next)
http://localhost:5173/auth/kakao/callback         ← 로컬(Vite)
```

> 콜백 경로를 `/auth/kakao/callback` 이 아닌 다른 것으로 하시려면 **말씀해 주세요.**
> 콘솔에 등록된 값과 한 글자라도 다르면 카카오가 authorize 단계에서 바로 거절합니다
> (`KOE006`). 프론트가 임의로 바꿀 수 없는 값입니다.

### ⚠️ Kakao JavaScript SDK 는 쓰지 마세요

`Kakao.Auth.authorize()` 는 `client_id` 로 **JavaScript 키**를 씁니다. 그런데 JS 키에는
클라이언트 시크릿이 없어서, 서버가 하는 토큰 교환이 실패합니다. authorize 와 토큰 교환의
`client_id` 는 반드시 같아야 하므로 **REST API 키로 직접 리다이렉트**하세요.
어차피 SDK 없이 `location.href` 한 줄이면 되고, 모바일 브라우저에서 카카오톡 앱으로
넘어가는 것도 그대로 동작합니다.

---

## 2. 구현

### 2-1. 로그인 버튼 — authorize 로 이동

```js
const KAKAO_REST_API_KEY = 'd71653959f6d7aa8e906adcea34f032d';
const KAKAO_REDIRECT_URI = `${window.location.origin}/auth/kakao/callback`;

function loginWithKakao() {
  // CSRF 방어 — 콜백에서 되돌아온 state 가 우리가 만든 값인지 확인한다.
  // (없으면 공격자가 자기 code 를 심은 콜백 URL 로 피해자를 로그인시킬 수 있다)
  const state = crypto.randomUUID();
  sessionStorage.setItem('kakao_oauth_state', state);

  const url = new URL('https://kauth.kakao.com/oauth/authorize');
  url.searchParams.set('client_id', KAKAO_REST_API_KEY);
  url.searchParams.set('redirect_uri', KAKAO_REDIRECT_URI);
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('state', state);

  window.location.href = url.toString();
}
```

`scope` 는 **보내지 마세요.** 콘솔의 필수 동의 설정이 그대로 적용됩니다.

### 2-2. 콜백 페이지 — code 를 백엔드로

```jsx
// /auth/kakao/callback
export default function KakaoCallback() {
  const ran = useRef(false);   // ⚠️ 아래 "code 는 1회용" 참고

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;

    const params = new URLSearchParams(window.location.search);
    const code = params.get('code');
    const state = params.get('state');
    const expected = sessionStorage.getItem('kakao_oauth_state');
    sessionStorage.removeItem('kakao_oauth_state');

    // 사용자가 동의 화면에서 취소하면 code 없이 error 가 온다
    if (params.get('error')) return goLogin('카카오 로그인이 취소되었습니다.');
    if (!code) return goLogin('로그인 정보를 받지 못했습니다.');
    if (!state || state !== expected) return goLogin('잘못된 접근입니다. 다시 시도해 주세요.');

    (async () => {
      const res = await fetch(`${API_BASE}/api/v1/auth/kakao/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          code,
          // authorize 때 쓴 값과 **완전히 동일**해야 한다
          redirect_uri: `${window.location.origin}/auth/kakao/callback`,
          // 아래 둘은 구글 로그인과 동일 (선택)
          attribution: readAttribution(),
          marketing_opt_in: false,
        }),
      });

      const data = await res.json();
      if (!res.ok) return goLogin(data.detail ?? '로그인에 실패했습니다.', data.code);

      saveTokens(data.tokens);
      if (data.is_new_user) trackCompleteRegistration();   // ⚠️ 반드시 이 값으로 분기
      router.replace(data.is_new_user ? '/onboarding' : '/dashboard');
    })();
  }, []);

  return <FullPageSpinner />;
}
```

---

## 3. API 스펙 — `POST /api/v1/auth/kakao/`

인증 불필요(AllowAny) · 스로틀 **IP 기준 20/min** · Swagger 에도 전문이 있습니다.

### 요청

| 필드 | 필수 | 타입 | 설명 |
|---|---|---|---|
| `code` | ✅¹ | string | 카카오 인가 코드 |
| `redirect_uri` | ✅² | string | authorize 에 쓴 값과 **완전 동일** |
| `access_token` | ✅¹ | string | 네이티브(카카오 SDK) 경로 전용. `code` 대신 |
| `attribution` | ❌ | object | 가입 유입 정보. **신규 가입 시에만** 저장 |
| `marketing_opt_in` | ❌ | bool | 기본 `false`. **신규 가입 시에만** 반영 |

¹ `code` 와 `access_token` 중 **정확히 하나**. 둘 다 보내면 400 입니다.
² `code` 를 보낼 때만 필수.

### 200 응답

```json
{
  "user": {
    "id": 42,
    "email": "user@kakao.com",
    "full_name": "홍길동",
    "is_email_verified": true,
    "email_verified_at": "2026-09-10T18:00:00+09:00",
    "date_joined": "2026-09-10T18:00:00+09:00",
    "last_login": null,
    "marketing_opt_in": false,
    "marketing_opt_in_at": null
  },
  "is_new_user": false,
  "tokens": { "refresh": "eyJ...", "access": "eyJ..." }
}
```

`is_new_user` — 가입과 로그인이 **같은 엔드포인트**라 이 값이 유일한 구분자입니다.
`date_joined` 로 추정하지 마세요(구글 쪽에서 이미 겪은 문제 — 전환이 누락/중복 발사됩니다).

---

## 4. 오류 — 코드별 화면 처리

**오류 응답은 두 포맷을 동시에 담습니다.** 최상위 `detail`/`code` 와 표준 envelope
`error.details.code` 에 같은 값이 들어갑니다. 편한 쪽으로 읽으세요 (구글 로그인과
같은 자리를 쓰려면 `detail`/`code`).

```json
{
  "detail": "카카오 인가 코드가 유효하지 않습니다. 로그인을 다시 시도해 주세요.",
  "code": "KAKAO_CODE_INVALID",
  "success": false,
  "error": { "code": 400, "message": "...", "details": { "code": "KAKAO_CODE_INVALID" } }
}
```

> 예외 하나: **요청 형식 오류**(필드 누락 등)는 DRF 검증이라 `detail`/`code` 없이
> envelope 만 나갑니다 → `error.details` 에 필드명이 담깁니다. 이건 프론트 버그일 때만
> 나오는 응답이라 사용자 문구는 "일시적 오류" 로 뭉개도 됩니다.

| HTTP | `code` | 의미 | 화면 처리 |
|---|---|---|---|
| 400 | `KAKAO_CODE_INVALID` | 코드 만료/재사용/위조 | **로그인 화면으로 되돌리고 재시도 버튼.** 자동 재시도 금지(같은 코드로는 영원히 실패) |
| 400 | `KAKAO_EMAIL_REQUIRED` | 이메일 동의를 안 받음 | "카카오 이메일 제공에 동의해야 가입할 수 있습니다" + 다시 로그인 버튼 |
| 400 | `KAKAO_TOKEN_INVALID` | 액세스 토큰 무효(네이티브 경로) | 재로그인 |
| 403 | `KAKAO_EMAIL_UNVERIFIED` | 그 이메일의 **기존 계정**이 있는데 카카오가 소유 확인을 못 해줌 | `detail` 을 그대로 노출. "이메일/비밀번호로 로그인" 버튼을 함께 |
| 403 | `KAKAO_TOKEN_FOREIGN_APP` | 다른 앱 토큰 | 개발 오류. "일시적 오류" |
| 409 | `account_deletion_pending` | 탈퇴 유예 중 (`error.details.code`) | 구글/이메일 로그인과 **동일 처리** — 탈퇴 취소 안내. `error.details.purge_at` 노출 |
| 429 | — | 스로틀 | "잠시 후 다시 시도" |
| 502 | `KAKAO_UNAVAILABLE` | 카카오 장애/타임아웃 | **재시도 버튼**(이 건은 재시도가 의미 있음, 단 code 는 새로 받아야 함 → 로그인부터 다시) |
| 503 | `KAKAO_NOT_CONFIGURED` | 서버 설정 누락 | 운영 이슈. 백엔드에 알려주세요 |

### ⚠️ 함정 — 400 사유를 "잠시 후 다시 시도"로 뭉개지 마세요

추가 IG 계정 견적에서 이미 한 번 겪은 사고입니다(CS #d34572b3). `KAKAO_CODE_INVALID`
· `KAKAO_EMAIL_REQUIRED` 는 **재시도해도 영원히 실패**하는 상태라, 같은 화면에서
"다시 시도"만 보여주면 사용자가 무한 루프에 갇힙니다. 위 표대로 사유별 CTA 를 주세요.

---

## 5. 반드시 지켜야 할 것 3가지

### ① `code` 는 **1회용** — React StrictMode 를 조심하세요

개발 모드에서 `useEffect` 가 2번 실행되면 두 번째 요청은 반드시
400 `KAKAO_CODE_INVALID` 입니다. 위 예제의 `useRef` 가드를 꼭 넣으세요.
"로컬에서만 로그인이 실패한다"의 대부분이 이것입니다.

### ② `redirect_uri` 는 authorize 와 **완전 동일**

끝의 `/` 하나, `http`/`https`, 포트까지 전부 같아야 합니다. authorize 를
`window.location.origin` 으로 만들었으면 POST 에도 같은 식으로 만드세요
(하드코딩과 섞으면 환경별로 어긋납니다).

### ③ 클라이언트 시크릿은 프론트에 두지 않습니다

서버만 갖고 있습니다. 이 문서에도 일부러 적지 않았습니다. 프론트에 필요한 건
**REST API 키뿐**이고, 그건 authorize URL 에 드러나는 공개 값입니다.

---

## 6. 계정이 어떻게 이어지는가 (알아두면 좋은 동작)

| 상황 | 결과 |
|---|---|
| 완전 신규 | 카카오 이메일로 계정 생성. `is_new_user: true` |
| **이미 이메일/구글로 가입한 사용자**가 카카오로 로그인 | 같은 계정에 **연결**됨. `is_new_user: false` — 계정이 갈라지지 않습니다 |
| 카카오에서 이메일을 바꾼 뒤 재로그인 | **같은 계정** 유지 (회원번호로 매칭). 우리 DB 의 이메일은 바뀌지 않습니다 |
| 카카오로 가입한 사용자가 비밀번호 로그인 시도 | 실패 — 비밀번호가 없는 계정입니다(구글과 동일). 비밀번호 재설정으로 만들 수 있습니다 |

닉네임은 **비어 있을 때만** `full_name` 에 채웁니다. 사용자가 우리 쪽에서 이름을
바꿔 놨는데 카카오 닉네임으로 덮어쓰는 일은 없습니다.

---

## 7. 아직 안 한 것 (백엔드 후속, 프론트 작업 없음)

- **연결 해제 웹훅 미설정.** 사용자가 카카오 설정에서 "턴플로우 연결 끊기"를 하면
  우리가 알 방법이 없습니다. 카카오 콘솔이 개인정보 처리 누락 가능성을 경고하고 있어
  별도로 붙일 예정입니다. **로그인 동작에는 영향 없습니다.**
- **네이티브 앱(Capacitor) 경로.** `access_token` 을 받는 입구는 열어 뒀지만
  네이티브용 Redirect URI/스킴은 아직 등록하지 않았습니다. 앱에 붙일 때 알려주세요.
- **OpenID Connect 는 OFF.** 지금 방식에 필요 없습니다.

---

## 8. 확인된 것 (dev 실측 2026-09-10)

- 동의 화면에 "닉네임, 카카오계정(이메일)" 필수로 정상 노출
- 실제 인가 코드 → `POST /api/v1/auth/kakao/` → **HTTP 200 + JWT 발급** 확인
- 기존 이메일 계정에 자동 연결(`is_new_user: false`) + 회원번호 저장 확인
- 같은 코드 재전송 → **400 `KAKAO_CODE_INVALID`** (두 포맷 동시 송출 확인)

궁금한 점이나 콜백 경로 변경이 필요하면 백엔드로 알려주세요.
