# 어드민 콘솔 인증 — 2단계 로그인(TOTP) + 어드민 전용 토큰

전달: 백엔드 → 어드민 콘솔팀 · **v2 (2026-08-16)** · v1 대비 변경점은 §8
상태: **백엔드 구현 완료 · dev 반영됨 · prod 배포 대기** (롤아웃 §7)
회신 문서: `ADMIN_AUTH_MFA_RESPONSE.md` (Q1~Q5 답변)
대상: `10_turnflow_admin`(웹) · `11_turnflow_admin_app`(웹을 띄우는 안드로이드 셸)

---

## 0. 요약 — 무엇이 바뀌나

지금 어드민 콘솔은 **일반 사용자와 똑같은** `/api/v1/auth/login/` 으로 로그인하고, 거기서
받은 토큰으로 `/api/v1/admin/**` 를 호출합니다. 어드민 API 를 지키는 것이 `is_staff` 플래그
하나뿐이라, 비밀번호만 알면 curl 로도 전 회원 데이터가 열립니다.

| | 지금 | 변경 후 |
|---|---|---|
| 로그인 | `POST /auth/login/` 1단계 | `POST /admin/auth/login/` → `POST /admin/auth/mfa/verify/` **2단계** |
| 2요소 | 없음 | **TOTP**(인증앱 6자리) · 신규 기기는 이메일 코드 1회 추가 |
| 토큰 | 일반 JWT (access 1일) | 어드민 전용 JWT (access **2시간** / refresh 12시간·**신뢰 기기 7일**) |
| 갱신 | `POST /auth/token/refresh/` | `POST /admin/auth/refresh/` |
| `/api/v1/admin/**` | 일반 토큰으로 호출 가능 | 어드민 토큰 아니면 **403 `admin_token_required`** |

> **🔴 Breaking** — 다만 서버 플래그(`ADMIN_MFA_ENFORCED`)로 강제 시점을 분리합니다.
> **프론트 배포가 먼저, 서버 강제는 나중**입니다 (§7).

**마케팅 전용 계정(`admin_role="marketing_viewer"`, 외주)은 대상이 아닙니다.** 같은
`/admin/auth/login/` 을 부르면 2단계 없이 `tokens` 가 바로 내려옵니다 — 콘솔은 로그인
경로를 하나로 유지하면 됩니다. 갱신 URL만 갈라야 합니다(§3-3 주의).

---

## 1. 왜 바꾸나

- 어드민 계정 하나를 팀 3명이 공유하던 상태라 감사로그(`AdminActionLog.actor`)가 항상 같은
  사람으로 찍혀 의미가 없었습니다. 개인 계정 3개로 분리합니다.
- 어드민 API 는 전 회원의 이메일·워크스페이스·DM 로그·결제 이력을 워크스페이스 경계 없이
  가로지릅니다. 비밀번호 하나가 유출되면 그게 전부 열립니다.
- 앱의 지문 로그인은 **기기 안에서만** 판정되어 서버가 검증할 수 없습니다. 서버가 요구할 수
  있는 두 번째 요소는 TOTP / 이메일 코드 / 패스키뿐입니다.

## 2. 새 로그인 플로우

```
[로그인 화면]  이메일 + 비밀번호
      │  POST /api/v1/admin/auth/login/
      ▼
   mfa_required: true, challenge, methods
      │
      ├─ 등록된 기기(device_trusted)      → [OTP 화면] 인증앱 6자리
      └─ 신규 기기(device_verification_required)
                                          → [OTP 화면] 인증앱 6자리 + 메일 6자리
      │  POST /api/v1/admin/auth/mfa/verify/
      ▼
   tokens.access / tokens.refresh  → 기존과 동일하게 저장, 대시보드 진입
```

화면 **3개**가 필요합니다: ①로그인 ②OTP 입력 ③MFA 등록/QR(최초 1회).
백업코드 입력은 ②에 "백업 코드로 로그인" 링크 → **일반 텍스트 입력 1칸**을 권합니다(§3-2).

### 기기 ID (`device_id`)

클라이언트가 **UUIDv4 를 한 번 만들어 영구 보관**하고 인증 요청 body 에 실어 보냅니다.
같은 기기를 다시 알아보기 위한 값이며 비밀이 아닙니다.

- 웹·안드로이드 셸: `localStorage["tf_admin_device_id"]` (셸은 WebView localStorage)
- 없이 보내면 서버가 발급해 응답에 담습니다 → 받은 값을 저장하세요
- 헤더가 아니라 **body** 입니다 (CORS 허용 헤더 목록을 건드리지 않기 위함)
- **로그아웃해도 지우지 마세요** — 지우면 매 로그인이 신규 기기가 되어 메일 코드가 매번 갑니다
- 앱 데이터 삭제·브라우저 변경으로 사라지면 신규 기기로 잡혀 메일 승인을 1회 더 받습니다.
  **설계상 정상 동작**이며 서버는 UA 지문 등 보조 신호를 쓰지 않습니다(이유는 회신 §6)

---

## 3. API 계약

Base: `https://<api-host>/api/v1/admin/auth/`
에러 형식은 프로젝트 표준 봉투이며 **사유 코드는 항상 `error.details.code`** 입니다
(지금 `apiClient` 가 읽는 그 자리). 전체 코드 목록은 §6.

### 3-1. `POST login/` — 1단계 (인증 불필요)

```jsonc
// 요청
{
  "email": "clfy1125@gmail.com",
  "password": "********",
  "device_id": "3f2b...-uuid",        // 선택. 없으면 서버가 발급
  "device_label": "이재원 MacBook"     // 선택(100자). 보안 화면 목록 표시용
}
```

```jsonc
// 200 — 등록된 기기
{
  "mfa_required": true,
  "challenge": "opaque",              // 2단계에 그대로 전달. TTL 300초
  "expires_in": 300,
  "methods": ["totp", "backup_code"],
  "device_id": "3f2b...-uuid",
  "device_trusted": true,
  "device_verification_required": false
}

// 200 — 신규 기기 (이메일 코드가 함께 발송됨)
{
  "mfa_required": true,
  "challenge": "opaque",
  "expires_in": 300,
  "methods": ["totp", "email", "backup_code"],
  "device_id": "9c81...-uuid",        // 서버 발급분 — 저장하세요
  "device_trusted": false,
  "device_verification_required": true,
  "email_masked": "cl***25@gmail.com"
}

// 200 — 마케팅 전용 계정 (2단계 없음)
{
  "mfa_required": false,
  "tokens": { "access": "eyJ...", "refresh": "eyJ..." },
  "admin": { "admin_role": "marketing_viewer", "allowed_sections": ["marketing"] }
}
```

| 상태 | `details.code` | 화면 처리 |
|---|---|---|
| 401 | `invalid_credentials` | 이메일/비번 불일치. **없는 계정·비스태프도 같은 응답**(계정 열거 방지) |
| 403 | `mfa_setup_required` | 인증앱 미등록 → 등록 화면(§3-4). `setup_token` · `device_verification_required` 동봉 |
| 429 | `RATE_LIMITED` | 5회/분(IP). `details.retry_after` 초 |

### 3-2. `POST mfa/verify/` — 2단계 (인증 불필요)

```jsonc
// 인증앱
{ "challenge": "...", "code": "123456", "remember_device": true }
// 백업코드 — 별도 필드입니다 (v1 에서 변경, §8)
{ "challenge": "...", "backup_code": "ABCD-EFGH-JKLM" }
// 신규 기기면 email_code 도 필수
{ "challenge": "...", "code": "123456", "email_code": "482913", "remember_device": true }
```

- `backup_code` 형식 `ABCD-EFGH-JKLM` (12자 + 하이픈). **하이픈·공백·대소문자를 서버가
  무시**하므로 프론트에서 정규화하지 마세요. 혼동 문자(`0/O`, `1/I`)는 알파벳에서 빠져 있습니다.
- `remember_device: true` → 신뢰 기기 등록. 다음부터 메일 코드를 건너뛰고 **refresh 7일**.

```jsonc
// 200
{
  "tokens": { "access": "eyJ...", "refresh": "eyJ..." },
  "admin": { /* GET /admin/me/ 와 동일 스키마 — 추가 호출 없이 캐시에 심으세요 */ },
  "device_id": "9c81...-uuid",
  "device_trusted": true
}
```

| 상태 | `details.code` | 화면 처리 |
|---|---|---|
| 400 | `invalid_code` | "코드가 올바르지 않습니다" — 입력칸 유지 |
| 400 | `invalid_email_code` | 메일 코드만 틀림 — 해당 칸만 에러 |
| 400 | `challenge_expired` | TTL 초과 **또는 5회 실패로 파기** → 로그인 화면으로 |
| 429 | `RATE_LIMITED` | 10회/분 |

> 인증앱 코드는 **1회용**입니다. 같은 30초 창의 코드를 다시 넣으면 `invalid_code` 입니다
> (재사용 방지). 사용자가 "방금 그 코드"를 다시 넣는 상황을 문구로 안내해 주세요.

### 3-3. `POST refresh/` — 어드민 토큰 갱신

```jsonc
// 요청 { "refresh": "eyJ..." }
// 200  { "access": "eyJ...", "refresh": "eyJ..." }   ← 회전됨. 새 refresh 를 반드시 저장
```

| 상태 | `details.code` | 화면 처리 |
|---|---|---|
| 400 | `not_admin_token` | 일반 토큰을 넣었음 → 로그아웃 |
| 401 | `token_expired` / `device_revoked` / `user_inactive` | 로그아웃 (device_revoked 는 안내 문구) |

> ⚠️ **갱신 URL 을 역할에 따라 갈라야 합니다.**
> `admin_role === "marketing_viewer"` → 기존 `POST /api/v1/auth/token/refresh/`
> 그 외 → `POST /api/v1/admin/auth/refresh/`
> `apiClient.ts` 의 single-flight 구조는 그대로 두시고 URL 선택만 넣으면 됩니다.

### 3-4. MFA 등록 — `setup` → `confirm`

```jsonc
// ① 최초 등록 (아직 어드민 토큰이 없음)
POST mfa/setup/   ← { "setup_token": "<login 403 응답의 값>" }
// ①' 재등록 (폰 교체 등) — 어드민 토큰 + 비밀번호 + 현재 코드
POST mfa/setup/   ← { "password": "********", "code": "123456" }

// 200
{
  "setup_token": "opaque",          // ← confirm 에 그대로 전달 (TTL 300초)
  "expires_in": 300,
  "otpauth_url": "otpauth://totp/TurnFlow%20Admin:me%40x.com?secret=...&issuer=TurnFlow%20Admin",
  "secret": "JBSWY3DPEHPK3PXP",     // QR 을 못 읽을 때 수동 입력용
  "qr_svg": "<svg .../>",           // 그대로 렌더 (프론트 QR 라이브러리 불필요)
  "device_verification_required": true
}
```

```jsonc
// ② 확인
POST mfa/confirm/ ← { "setup_token": "...", "code": "123456", "email_code": "482913" }
// 200
{
  "backup_codes": ["ABCD-EFGH-JKLM", ...10개],  // ⚠️ 이 응답에서만 1회 노출
  "tokens": { "access": "...", "refresh": "..." },
  "admin": { ... },
  "device_id": "9c81...-uuid",
  "device_trusted": true
}
```

- **백업코드는 다시 볼 수 없습니다** — 서버는 해시만 보관합니다. 복사/다운로드 + "저장했습니다"
  확인 체크를 받으세요. 잃어버렸으면 §3-6 재발급이 유일한 경로입니다.
- 등록 중인 시드는 확인 전까지 별도 자리에 있습니다 — 중간에 창을 닫아도 **기존 인증앱은
  그대로 동작**합니다.
- 에러: `challenge_expired` · `invalid_code` · `invalid_email_code` · `already_enrolled`
  (등록된 계정이 최초 등록 경로 사용) · `setup_not_started` (QR 발급 없이 confirm)

### 3-5. `GET mfa/status/` — 상태·기기 목록 (어드민 토큰 필요)

```jsonc
{
  "enrolled": true,
  "confirmed_at": "2026-08-20T04:12:00Z",
  "backup_codes_remaining": 8,
  "backup_codes_low_threshold": 3,          // 서버 기준 — 하드코딩 대신 이 값과 비교
  "last_login_at": "2026-08-21T09:02:11Z",  // 계정 단위
  "trusted_devices": [{
    "id": 1, "device_id": "3f2b...", "label": "이재원 MacBook",
    "is_trusted": true, "is_current": true,
    "created_at": "2026-08-18T02:11:00Z",
    "last_seen_at": "2026-08-21T09:02:11Z", "last_seen_ip": "121.130.44.14",
    "expires_at": null                       // 항상 null = 해제할 때까지 유지
  }]
}
```

- `is_trusted: false` 행도 옵니다 — 신뢰 등록 없이 들어온 임시 세션. **해제 대상에서 빼지 마세요.**
- `is_current: true` 를 해제하면 본인이 로그아웃됩니다 → 확인 모달.

### 3-6. `POST mfa/backup-codes/regenerate/` — 백업코드 재발급

```jsonc
← { "password": "********", "code": "123456" }   // 비밀번호 + 현재 인증앱 코드
→ { "backup_codes": [ ...10개 ], "remaining": 10 }
```
**기존 코드는 전부 폐기**됩니다 — "종이에 적어둔 옛 코드는 버리세요" 안내를 넣어주세요.
에러: `invalid_credentials`(401) · `invalid_code` · `not_enrolled`

### 3-7. `DELETE devices/{id}/` — 기기 해제

204. 본인 기기가 아니거나 이미 해제됐으면 404 `device_not_found`.
해제된 기기는 **다음 갱신부터** 막히고(이미 발급된 access 는 최대 2시간 유효), 다음 로그인에
메일 승인을 다시 요구합니다.

### 3-8. 기존 어드민 API 에서 달라지는 것

일반 토큰으로 `/api/v1/admin/**` 호출 시:

```jsonc
// 403
{ "success": false,
  "error": { "code": 403, "message": "관리자 인증이 필요합니다. 다시 로그인해 주세요.",
             "details": { "code": "admin_token_required" } } }
```

`section_forbidden`(권한 없는 섹션)과 **다른 화면**이어야 합니다 — 앞은 "다시 로그인",
뒤는 "권한 없음". `isSectionForbidden()` 옆에 `isAdminTokenRequired()` 를 나란히 두세요.

---

## 4. 웹(10) 체크리스트

- [ ] `device_id` 생성/보관 (로그아웃 시 유지)
- [ ] 로그인 2단계 분리 + OTP 화면 (6칸 · `autocomplete="one-time-code"`)
- [ ] **백업코드 입력 = 별도 텍스트 1칸** (`backup_code` 필드)
- [ ] MFA 등록 화면 (QR + 수동 시크릿 + 백업코드 1회 노출 + 저장 확인)
- [ ] `apiClient.ts` — 갱신 URL 을 역할별로 분기 (§3-3 주의)
- [ ] `apiClient.ts` — `admin_token_required` 403 → 로그인 리다이렉트
- [ ] 보안 설정 화면 — 기기 목록/해제 · 백업코드 남은 개수 + **재발급 버튼**(§3-6)
- [ ] `challenge_expired` 는 시간이 남아 있어도(5회 실패) 올 수 있음 → 로그인 화면 복귀

## 5. 앱 — 지금은 웹 배포로 커버됩니다

운영 앱 `11_turnflow_admin_app` 은 Capacitor **WebView 셸**로 `admin.turnflow.link` 를 그대로
띄웁니다. **웹 배포 하나로 앱까지 반영**되며 앱 재빌드·스토어 심사가 필요 없습니다.
`device_id` 는 WebView localStorage 에 저장됩니다.

`12_turnflow_admin_app_native`(Expo, 지문 로그인 보유)를 운영에 투입할 때 할 일:
- 최초 로그인만 2단계, 이후 **지문 → 대시보드** 는 지금 흐름 그대로
- SecureStore 에 보관하는 토큰을 어드민 refresh 로 교체 + 갱신 URL 을 `/admin/auth/refresh/` 로
- 신뢰 기기 refresh 가 7일이라 `BiometricLoginError('expired')` 빈도는 주 1회 수준

---

## 6. 사유 코드 전체

| HTTP | `details.code` | 의미 |
|---|---|---|
| 401 | `invalid_credentials` | 비번 불일치·없는 계정·비스태프 (구분 없음) |
| 403 | `mfa_setup_required` | 인증앱 미등록 (`setup_token` 동봉) |
| 400 | `invalid_code` | 인증앱/백업코드 오답 (재사용 코드 포함) |
| 400 | `invalid_email_code` | 메일 코드 오답 |
| 400 | `challenge_expired` | TTL 초과 또는 5회 실패로 파기 |
| 400 | `already_enrolled` | 등록된 계정이 최초 등록 경로 사용 |
| 400 | `setup_not_started` | QR 발급 없이 confirm |
| 400 | `not_enrolled` | 미등록 상태로 백업코드 재발급 |
| 400 | `not_admin_token` | 일반 refresh 를 어드민 갱신에 사용 |
| 401 | `token_expired` | refresh 만료·폐기 |
| 401 | `device_revoked` | 해제된 기기의 갱신 |
| 401 | `user_inactive` | 계정 비활성/스태프 해제 |
| 403 | `admin_token_required` | 일반 토큰으로 어드민 API 호출 |
| 404 | `device_not_found` | 남의 기기·이미 해제된 기기 |
| 429 | `RATE_LIMITED` | 로그인 5/분 · 코드 10/분 · 기기 메일 5/시간 |

## 7. 롤아웃 순서

| 순서 | 주체 | 내용 |
|---|---|---|
| 1 | 백엔드 | 엔드포인트 배포 (**dev 완료**, prod 예정). 플래그 OFF — 기존 로그인 그대로 |
| 1-b | 백엔드 | **prod 배포 완료 통지** ← 이 신호를 받고 프론트를 올려주세요 |
| 2 | 프론트 | 웹 배포 (= 안드로이드 셸까지 자동 반영). 이 시점엔 구·신 경로 모두 동작 |
| 3 | 관리자 3명 | 인증앱 등록 + 백업코드 보관 |
| 4 | 양측 | **확인 기간 하루** |
| 5 | 백엔드 | 플래그 ON — 이때부터 구 토큰은 403. env 전환만, 재배포 아님 |
| 6 | 백엔드 | 문제 시 즉시 OFF 롤백 |

3번이 끝나기 전에 5번을 하면 **관리자 3명이 동시에 잠깁니다.**
`/admin/auth/refresh/` 가 없는 상태로 프론트가 먼저 나가면 2시간 뒤 전원 로그아웃됩니다 →
**1-b 통지를 기다려 주세요.**

## 8. v1(2026-08-16 초안) 대비 변경점

구현하면서 v1 계약이 안전하지 않거나 불충분한 지점이 나왔습니다. 자세한 이유는 회신 §7.

1. **백업코드는 `code` 가 아니라 `backup_code` 필드** · 8자 → **12자**(`ABCD-EFGH-JKLM`)
2. **`mfa/confirm/` 의 `setup_token` 이 항상 필수** — `setup` 응답값을 그대로 전달
3. **재등록 `setup/` 은 `password` + 현재 `code`** (v1 은 password 만)
4. **`password_changed` 사유 코드 삭제** — 비밀번호 변경으로 토큰을 죽이지 않습니다
5. 추가된 필드: `backup_codes_low_threshold` · `last_login_at` · 기기의 `created_at`/`expires_at`
6. 추가된 엔드포인트: `POST mfa/backup-codes/regenerate/`
7. §5 앱 대상 정정: `12`(Expo) → **`11`(WebView 셸)**, 웹 배포로 커버
8. 신뢰 기기 refresh **12시간 → 7일** (프론트 Q3 수락)
