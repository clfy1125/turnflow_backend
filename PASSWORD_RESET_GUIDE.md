# 비밀번호 재설정 (Password Reset) — 프론트엔드 연동 가이드

이메일/비밀번호로 가입한 회원의 비밀번호 찾기 흐름입니다. **백엔드는 이미 구현·배포되어 있고**,
상세 스키마는 Swagger(`/api/docs/`) 또는 api-mcp 로 조회하세요. 이 문서는 2단계 흐름 요약입니다.

> 소셜(구글) 로그인 계정은 비밀번호가 없으므로 재설정 대상이 아닙니다(요청해도 메일이 가지 않음).

---

## 흐름 (2단계)

```
[1] 사용자가 이메일 입력
      └─ POST /api/v1/auth/password/reset-request/   { email }
         → 202 (항상 동일 응답: 계정 존재 여부 비노출)
      → 사용자 메일함에 "재설정 링크"(버튼) 도착 — 링크의 ?token= 쿼리에 토큰 포함

[2] 사용자가 메일의 링크 클릭 → 링크의 token 으로 새 비밀번호 설정
      └─ POST /api/v1/auth/password/reset-confirm/   { token, new_password, new_password_confirm }
         → 200  "비밀번호가 재설정되었습니다."
      → 로그인 화면으로 이동 (기존 세션은 전부 만료됨)
```

---

## 1) 재설정 요청 — `POST /api/v1/auth/password/reset-request/`

- 인증 불필요(public).
- 요청: `{ "email": "user@example.com" }`
- 응답: **항상 `202`** (`{ "detail": "..." }`). 가입되지 않은 이메일이어도 동일하게 202 — 계정 존재 여부를
  드러내지 않기 위함이니, FE 는 "메일을 보냈습니다(존재하는 경우)" 식으로 안내하면 됩니다.
- `400`: 이메일 형식 오류/누락.

```javascript
await fetch('/api/v1/auth/password/reset-request/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email }),
});
// 응답 본문에 의존하지 말고 "메일 확인 안내" 화면으로 전환
```

## 2) 재설정 확인 — `POST /api/v1/auth/password/reset-confirm/`

- 인증 불필요(public).
- 요청: `{ "token", "new_password", "new_password_confirm" }`
  - `token`: **메일 링크(`/reset-password?token=...`)의 쿼리스트링 값**. 재설정 확인은 이 링크 토큰만 받습니다.
    (이메일 인증과 달리 6자리 코드 입력 방식은 지원하지 않음 — 링크 기반 흐름으로 구현하세요.)
  - `new_password` / `new_password_confirm`: 일치해야 하며 Django 비밀번호 정책 통과 필요.
- 응답 `200`: `{ "detail": "비밀번호가 재설정되었습니다." }`
- 응답 `400`: 토큰 만료/사용됨, 비밀번호 불일치 또는 정책 위반.

```javascript
// 예: /reset-password?token=XXXX 페이지에서
const token = new URLSearchParams(location.search).get('token');
const res = await fetch('/api/v1/auth/password/reset-confirm/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ token, new_password, new_password_confirm }),
});
if (res.ok) redirectToLogin();      // 재설정 후 전 기기 로그아웃 → 다시 로그인
```

---

## 동작/보안 요약 (FE 가 알아두면 좋은 것)

- **유효시간**: `PASSWORD_RESET_TTL_MINUTES`(기본 60분). 만료/이미 사용된 토큰은 `400`.
- **1회용**: 토큰은 한 번 쓰면 무효. 재설정 후 발급된 모든 refresh 토큰이 블랙리스트 처리되어
  **다른 기기는 전부 로그아웃**됩니다 → 재설정 성공 후 로그인 화면으로 보내세요.
- 재설정에 성공하면 해당 계정의 이메일 인증이 자동 완료(`is_email_verified=true`)됩니다.

상세 요청/응답 스키마는 Swagger `/api/docs/` 의 `auth` 태그 또는 api-mcp 검색으로 확인하세요.

메일에 포함되는 링크는 아래와 같은구조임.
프론트 도메인/reset-password?token=<토큰> 을 엽니다
따라서 프론트엔드 페이지에서  그 프론트 앱에 /reset-password 라우트가 구현해야할 필요가 있음.