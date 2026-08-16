# 어드민 인증 하드닝 (MFA + 계정 분리)

작성: 2026-08-16 · 대상: prod 전용, dev 는 게이트 OFF

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 계정 3개 분리 · 공유/불명 계정 회수 | ✅ **완료** (개인 계정 3개는 이미 존재 · 불명 계정 1건 회수 · 운용 결정 3건 — §4 Phase 0) |
| 1 | 어드민 전용 토큰 + 게이트 | ✅ **구현 완료** (dev 반영, prod 미배포) |
| 2 | TOTP 등록·백업코드·이메일 기기승인·비상 리셋 | ✅ **구현 완료** (dev 반영, prod 미배포) |
| 3a | Django admin 세션 MFA | ⬜ 미착수 |
| 3b | 패스키(웹) · 기기 바인딩(앱) | ⬜ 미착수 |
| 4 | 로그인 알림·계정 잠금 | ⬜ 미착수 |

구현 산출물은 §7. 프론트 계약은 `docs/frontend/ADMIN_AUTH_MFA_FRONTEND.md`(v2),
회신은 `docs/frontend/ADMIN_AUTH_MFA_RESPONSE.md`.

---

## 0. 한 줄 요약

어드민 API 는 지금 **일반 사용자와 똑같은 이메일·비밀번호 로그인**으로 열린다. 이걸
①개인 계정 3개로 분리하고 ②어드민 전용 토큰(2요소로만 발급)을 도입해 ③`/api/v1/admin/**`
와 Django admin 을 그 토큰/세션 뒤로 옮긴다.

---

## 1. 현재 상태 (2026-08-16 코드 기준 확인)

| 항목 | 실제 | 근거 |
|---|---|---|
| 어드민 로그인 경로 | **일반 사용자와 동일** `/api/v1/auth/login/`. 어드민 전용 경로 없음 | `apps/authentication/views.py:294` |
| 발급 토큰 | 일반 JWT 그대로. access **1일** / refresh 7일, RS256, rotate+blacklist | `config/settings/base.py:362` |
| 토큰의 어드민 표식 | **없음**. 클레임은 `user_id`/`email`/`full_name` 뿐 | `apps/authentication/tokens.py:22` |
| 어드민 판정 | 요청마다 DB 의 `is_staff` 한 줄 | `apps/admin_api/permissions.py`, DRF `IsAdminUser` |
| 결과 | **같은 토큰 하나가 유저 API 와 어드민 API 양쪽에 통용**. curl 로 접근 가능 | — |
| Django admin `/admin/` | 세션 로그인, 같은 이메일·비번, 2FA 없음. 세션 1h | `config/urls.py:19`, `base.py:215` |
| RBAC | Group 기반 `marketing_viewer` 만 존재. 나머지는 전부 `full` | `apps/admin_api/roles.py:95` |
| 감사로그 | `AdminActionLog.actor` FK 존재 | `apps/admin_api/models.py:59` |
| 앱 지문 인증 | 구현돼 있음 — 단 **기기 내 판정**, 서버로 증거가 오지 않음 | `12_turnflow_admin_app_native/src/lib/biometric.ts` |

### 1-1. "지문인증"에 대한 사실 확인 (설계 전제)

앱의 지문은 `SecureStore` 에 잠가둔 refresh 토큰을 꺼내는 **잠금장치**이지 인증 요소가 아니다.
서버로 오는 증거가 없어서 서버가 "지문을 요구"하는 것은 원리적으로 불가능하다.
서버가 검증할 수 있는 생체는 **WebAuthn/패스키**(하드웨어 서명이 서버로 전달됨) 뿐이다.
→ 서버 강제가 목적이면 실제 선택지는 **TOTP · 이메일 OTP · 패스키** 셋. 지문은 그 위에
얹는 UX 레이어로 남긴다(Phase 3).

### 1-2. 확정된 결정

| # | 결정 | 비고 |
|---|---|---|
| D-1 | 개인 계정 3개로 분리 | `clfy1125@gmail.com`(운영자·superuser) / `baby422p@gmail.com`(박창현) / `sihyun0693@gmail.com`(김시현) |
| D-2 | 2요소 = **TOTP 주**, 이메일 OTP 는 **신규 기기 승인 전용**, 백업코드 10개 | 이메일을 로그인 폴백으로 열면 실질 보안이 "메일함 보안" 수준으로 내려감 |
| D-3 | Django admin `/admin/` 도 **세션 로그인에 MFA 요구** (차단하지 않고 유지) | `django-otp` 의 `OTPAdminSite` |
| D-4 | **마케팅 전용 계정(`marketing_viewer`)은 이 게이트에서 제외** | 외주. 지금 동작 그대로 유지 |

---

## 2. 목표 상태

```
개인 계정 3개 ── 비밀번호 + TOTP ──→ 어드민 전용 JWT
                                       ├ 클레임: adm=1 · amr · did(기기) · pwh(비번해시)
                                       ├ access 2h / refresh 12h  (현재 1d/7d)
                                       └ 기기 바인딩
        ↑ 신규 기기 최초 1회만
   이메일 OTP 로 기기 승인 → 이후 그 기기는 지문(앱)·패스키(웹)로 재발급

게이트 (단일 초크포인트: AdminRoleGuardMiddleware)
  /api/v1/admin/**  ← adm 클레임 없는 토큰이면 403 admin_token_required
  /admin/ (Django) ← OTPAdminSite: 세션에 검증된 OTP 기기가 없으면 로그인 불가
  marketing_viewer ← 게이트 통과 제외 (지금 그대로)
```

**강제할 자리는 이미 있다.** `apps/admin_api/middleware.py:51` `AdminRoleGuardMiddleware` 가
`/api/v1/admin/**` + `/admin/` 전 구간에서 deny-by-default 를 이미 수행한다. MFA 게이트도
여기 붙이면 **새 어드민 엔드포인트가 추가돼도 누락되지 않는다.** 뷰마다 permission 을 다는
방식은 쓰지 않는다(같은 파일 docstring 의 기존 결정).

---

## 3. 라이브러리 선택: `django-otp`

직접 구현하지 않는다. TOTP 는 **재사용 방지(replay)·시간 드리프트·스로틀** 을 정확히
다뤄야 하는데, 이건 손으로 짤 영역이 아니다.

- `django-otp` (>=1.3, Django 5.0 지원) + `qrcode`
- 얻는 것: `TOTPDevice`(시드·drift·`last_t` 재사용 방지), `StaticDevice`(백업코드),
  실패 스로틀, 그리고 **D-3 을 거의 공짜로 해결하는 `OTPAdminSite`**
- 우리 JWT 플로우는 `django_otp.match_token(user, code)` 로 검증만 빌려 쓴다
- Django admin 적용은 `admin.site.__class__ = OTPAdminSite` 한 줄 —
  기존 `@admin.register` 등록이 전부 기본 사이트에 붙어 있으므로 **재등록 불필요**

미채택: `django-two-factor-auth`(자체 뷰·템플릿·URL 을 통째로 끌고 와 우리 JWT 플로우와 겹침).

---

## 4. 단계별 계획

### Phase 0 — 계정 분리 (코드 0줄)

> 이게 실질 P0 다. 셋이 한 계정을 쓰면 MFA 를 붙여도 TOTP 시드를 셋이 나눠 갖게 되고,
> 감사로그 actor·개별 회수·유출 범위 축소가 전부 무너진다.

#### prod 실태 (2026-08-16 실측)

**계정을 새로 만들 필요는 없었다** — 개인 계정 3개가 이미 staff 로 존재한다.
dev 에 있던 `1dlawodnjs@naver.com`·`test@test.com`·`dashsmoke@test.com` 은 **prod 에 없다**.

| id | 계정 | super | last_login | 판정 |
|---|---|---|---|---|
| 1 | `clfy1125@gmail.com` | ✅ | 2026-08-16 | 정상 (실사용) |
| 2 | `baby422p@gmail.com` | ✅ | 2026-07-28 | 정상 (superuser 필요한지 재검토) |
| 7 | `sihyun0693@gmail.com` | — | 2026-05-15 | 정상 (3개월 미사용) |
| 41 | `turnflow@example.com` | — | **없음** | ⚠️ **회수함** (아래) |
| 92 | `marketing@turnflow.link` | — | 2026-07-31 | 정상 (marketing_viewer, 대상 외) |

#### ✅ 조치한 것 — `turnflow@example.com` 백오피스 권한 회수

로그인 이력이 **0** 인데 `is_staff=True` 였다. 코드 어디에서도 참조되지 않는 데이터 전용
계정이며(레포 전체 grep 0건), 워크스페이스·멤버십·구독을 각 1건씩 가진 **실제 테넌트**다.

- 건드린 것: **`is_staff` 한 필드뿐.** `is_active` 는 그대로 뒀다 — 비활성화하면 그 계정이
  소유한 워크스페이스가 죽는다. 문제는 "고객 계정에 백오피스 권한이 붙어 있는 것" 하나였다.
- 로그인 이력이 0이라 끊어질 세션·토큰이 없다.
- `AdminActionLog`(`user.update`) 에 before/after 와 사유를 남겼다.
- 롤백: `User.objects.filter(email__iexact="turnflow@example.com").update(is_staff=True)`

#### 계정 운용 — 2026-08-16 결정

감사로그 60건의 actor 는 **단 둘**이다: `marketing@turnflow.link` 34건,
`clfy1125@gmail.com` 26건. 박창현·김시현 계정의 어드민 조작 이력은 **0건** — 계정은 따로
있는데 실제로는 `clfy1125` 하나로 써 왔다는 뜻이다. 이에 대한 결정:

| # | 사안 | 결정 |
|---|---|---|
| 1 | 개인 계정 전환 | ✅ **이미 각자 비밀번호를 알고 있다** — 별도 재설정 없이 각자 계정으로 전환 |
| 2 | `clfy1125` 비밀번호 회전 | ❌ 하지 않는다 — **MFA 를 등록하면 비밀번호만으로는 어드민에 못 들어온다** |
| 3 | `baby422p` superuser | ✅ **유지** |

②의 근거를 남겨 둔다. MFA 등록 후에는 비밀번호를 알아도 `/api/v1/admin/**` 에 닿지 못한다 —
2단계가 막고, 등록된 계정은 부트스트랩 경로(`already_enrolled`)로도 우회할 수 없으며, 신규
기기 승인 코드는 본인 메일함으로만 간다.

**다만 MFA 가 지키는 것은 어드민 표면뿐이다.** 같은 비밀번호로 일반 유저 콘솔
(`/api/v1/auth/login/`)은 그대로 열린다. 실측한 잔여 노출은 작다 — `clfy1125` 는
워크스페이스 1개(plan=admin)에 **연동된 IG 계정이 0개**다. 이 계정에 IG 를 연동하게 되면
그때 비밀번호 회전을 다시 검토할 것.

⚠️ **남은 전제**: 세 명이 각자 계정으로 **각자 MFA 를 등록**해야 한다. 한 계정에 등록하고
시드를 공유하면 도입 목적이 그대로 무너진다.

#### 별도 티켓

`apps/admin_api` 어디에도 통계 모수에서 staff 를 제외하는 필터가 없다 — 어드민 계정이
회원수·퍼널에 그대로 섞인다.

**검증**: 각자 로그인 → `GET /api/v1/admin/me/` 200 · 회수한 계정으로 어드민 API → 403.

#### prod 접속 레시피 (읽기 전용 조회)

```bash
# ⚠️ `ssh colo` 별칭은 이 PC 에 없다. 키는 08-04 하드닝 때 새로 발급된 것을 써야 한다.
ssh -p 2222 -i ~/.ssh/turnflow_prod_admin_ed25519 root@121.126.99.70
# 새 컨테이너를 만들지 않는다 (compose run 금지 — exec 로 붙는다)
docker exec -i turnflow_instagram_web_dashboard python manage.py shell < probe.py
```

---

### Phase 1 — 어드민 세션 분리 (백엔드, 1~2일)

**신규**: `apps/admin_api/auth/` (`tokens.py` / `views.py` / `gate.py` / `urls.py`)

1. `AdminRefreshToken(AppRefreshToken)` — 클레임 추가
   | 클레임 | 뜻 |
   |---|---|
   | `adm` | `1` — 어드민 토큰 표식. 이게 없으면 어드민 경로 거부 |
   | `amr` | 인증 수단 배열 `["pwd","totp"]` / `["pwd","totp","email"]` |
   | `did` | 기기 ID — 신뢰 기기 판정·원격 회수용 |
   | `pwh` | `get_md5_hash_password(user.password)` — 비번 변경 시 토큰 전량 무효 |

   > ⚠️ `SIMPLE_JWT.CHECK_REVOKE_TOKEN` **전역 활성 금지**. 기존 토큰에는 클레임이 없어
   > **전 사용자가 즉시 재로그인** 하게 된다(RS256 전환 때 이미 겪음). 어드민 토큰에서만
   > 같은 검사를 우리가 직접 한다.

2. 엔드포인트 (`/api/v1/admin/auth/`) — 계약 상세는 `docs/frontend/ADMIN_AUTH_MFA_FRONTEND.md`
   - `POST login/` → `{mfa_required, challenge(TTL 5분), methods, device_verification_required}`
   - `POST mfa/verify/` → 어드민 토큰 발급
   - `POST refresh/` → 어드민 전용 회전 (일반 refresh 는 400 `not_admin_token`)
   - `POST mfa/setup/` · `POST mfa/confirm/` · `GET mfa/status/` · `DELETE devices/{id}/`

3. 게이트 — `AdminRoleGuardMiddleware` 확장
   - `ADMIN_MFA_ENFORCED` env (prod True / local False) — `WEBHOOK_HMAC_ENFORCED` 와 동일 패턴
   - `adm` 없음 → 403 `admin_token_required` / `pwh` 불일치 → 401 `password_changed`
   - **제외**: `admin/auth/**` 자체, `marketing_viewer` 역할(D-4)
   - 차단은 기존 `admin.access_denied` 감사로그 경로 재사용

4. 스로틀 (기존 패턴 — prod 만 값, local 은 `None`)
   `admin_login` 5/min · `admin_mfa` 10/min · `admin_email_otp` 5/hour

5. 토큰 수명: 어드민 access **2h** / refresh **12h** (`AdminRefreshToken` 자체 lifetime)

---

### Phase 2 — TOTP 등록·복구 + 이메일 기기 승인 (2~3일)

1. `django-otp` 설치 · `INSTALLED_APPS` + `OTPMiddleware` 추가 · 마이그레이션
2. 신규 모델 `apps/admin_api/models.py`
   - `AdminTrustedDevice(user, device_id, label, last_seen_ip, last_seen_at, revoked_at)`
   - TOTP 시드·백업코드는 **django-otp 모델을 그대로 쓴다**(`TOTPDevice`/`StaticDevice`)
3. 등록 플로우: `mfa/setup/`(비밀번호 재확인 → `otpauth://` URL + QR) → `mfa/confirm/`(코드 확인)
   → 백업코드 10개를 **이 응답에서만 1회** 노출
4. 이메일 OTP — **새로 만들지 않는다**. `apps/emails/models.py:71` `EmailToken` 이 6자리 코드 +
   TTL + 1회용 + 원자적 사용처리(`:136`)를 이미 구현. `purpose="admin_device"` 만 추가하고
   템플릿 키 `admin_device_code` 를 `constants.py` + DB 시드 마이그레이션에 추가.
5. 복구 경로 (**셋 다 잠기는 사고를 막는 비상구**)
   - 백업코드 10개(1회용)
   - superuser 가 타인 MFA 리셋 → `AdminActionLog` 신규 액션 `admin.mfa_reset`
   - 최후: `manage.py admin_mfa_reset <email>` (SSH 전제) — **Phase 2 에서 같이 만들 것**
6. 감사로그 액션 추가: `admin.login` · `admin.mfa_enrolled` · `admin.mfa_reset` ·
   `admin.device_trusted` · `admin.device_revoked`
   → 그제서야 "누가·언제·어디서" 가 남는다(Phase 0 의 계정 분리가 전제)

---

### Phase 3 — Django admin 세션 MFA (D-3) + 지문/패스키

**3-a. Django admin** (Phase 2 직후, 반나절)
- `admin.site.__class__ = OTPAdminSite` — 로그인 폼에 OTP 필드가 붙고, 검증된 기기가 없으면
  `has_permission()` 이 False
- 방어 심층화: 미들웨어에서 `/admin/` 접근 시 세션의 OTP 검증 여부를 한 번 더 확인
  (MFA 도입 **이전에 생성된 세션**이 그대로 통과하는 창을 막는다)
- 배포 시 기존 어드민 세션 전부 무효화(`django_session` 정리)

**3-b. 지문/패스키** (별도 라운드)
- 앱(12): 지금 흐름(지문 → 대시보드) 그대로. 저장 토큰만 **어드민 refresh + `did` 바인딩**으로
  교체 → 서버가 "신뢰된 기기" 를 인지하게 된다
- 웹(10): WebAuthn 패스키(`webauthn` 파이썬 패키지 + `AdminWebAuthnCredential`).
  Windows Hello / Touch ID / YubiKey — **서버가 검증하는 유일한 진짜 생체**
- TOTP 는 백업 수단으로 계속 유지

---

### Phase 4 — 부수 하드닝 (Phase 1 과 묶어도 됨)

- 어드민 로그인 알림 메일 — **새 IP·새 기기일 때만** (매번이면 소음)
- `AdminActionLog` 조회 화면(어드민 콘솔)에 로그인 이력 노출
- 실패 임계 초과 시 계정 잠금(10회/10분) + superuser 알림

---

## 5. 리스크 · 함정

| 리스크 | 대응 |
|---|---|
| **`ADMIN_MFA_ENFORCED=True` 로 배포 → 3명 동시 잠김** | False 로 먼저 배포 → 3명 등록 확인 → env 만 True 로 전환(재배포 아님) |
| 백업코드 분실 + TOTP 기기 분실 | `manage.py admin_mfa_reset` 을 Phase 2 에서 **함께** 만든다 |
| `CHECK_REVOKE_TOKEN` 전역 활성 | 금지. 전 사용자 재로그인. 어드민 토큰에서만 자체 검사 |
| Django admin 세션 MFA 적용 시 기존 세션 통과 | 배포 시 세션 테이블 정리 + 미들웨어 이중 확인 |
| 프론트 3개 동기화 | 10(Next.js 웹) · 11(Capacitor WebView **셸** — 10 을 감싼 것이라 자동 반영, 확인 필요) · 12(Expo 네이티브) |
| 어드민 access 1d→2h 로 만료 빈도 증가 | 웹은 이미 single-flight refresh 인터셉터 보유(`10_turnflow_admin/src/lib/apiClient.ts`) — URL 만 교체 |
| 마케팅 외주 계정이 게이트에 걸려 장애 | `is_restricted(role)` 이면 MFA 게이트 **스킵**. 회귀 테스트 필수 |

---

## 6. 작업 순서 요약

| Phase | 내용 | 규모 | 선행 |
|---|---|---|---|
| 0 | 계정 3개 분리 · 공유/불명 계정 회수 | 코드 0 | — |
| 1 | 어드민 전용 토큰 + 게이트 (`ADMIN_MFA_ENFORCED=False` 로) | 1~2일 | 0 |
| 2 | TOTP 등록·백업코드·이메일 기기승인·비상 리셋 → env True | 2~3일 | 1 |
| 3a | Django admin 세션 MFA | 0.5일 | 2 |
| 3b | 패스키(웹) · 기기 바인딩(앱) | 별도 라운드 | 2 |
| 4 | 로그인 알림·계정 잠금 | 0.5일 | 2 |

---

## 7. 구현 산출물 (Phase 1+2, 2026-08-16)

### 코드

| 파일 | 역할 |
|---|---|
| `apps/admin_api/auth/totp.py` | **TOTP·백업코드 검증의 단일 소스**. 재사용 방지(`last_step`)·드리프트·백업코드 발급/소모. Phase 3a 의 Django admin 폼도 이 함수를 쓴다 |
| `apps/admin_api/auth/tokens.py` | 어드민 JWT (`adm`/`amr`/`did` 클레임, 수명, 회전) |
| `apps/admin_api/auth/challenge.py` | 1↔2단계 티켓 (Redis, TTL 300초, 시도 5회) |
| `apps/admin_api/auth/devices.py` | 기기 식별·신뢰 등록·회수 |
| `apps/admin_api/auth/emails.py` | 신규 기기 승인 코드 (`EmailToken` 재사용) |
| `apps/admin_api/auth/views.py` · `views_manage.py` | 엔드포인트 8종 |
| `apps/admin_api/gate.py` | 어드민 토큰 게이트 판정 (플래그·제외 대상) |
| `apps/admin_api/middleware.py` | 기존 역할 게이트에 토큰 게이트 결합 |
| `apps/admin_api/management/commands/admin_mfa_reset.py` | **비상 복구** (SSH) |
| `apps/admin_api/tests_admin_mfa.py` | 42건 |

모델 3종(`AdminMFADevice`·`AdminBackupCode`·`AdminDevice`) + `AdminActionLog` 액션 6종 추가.
마이그레이션: `admin_api.0008`, `emails.0009`.

### 설계에서 정한 것 (되돌리기 전에 이유부터)

- **TOTP 시드는 암호화 저장** (`EncryptedTextField`) — IG 토큰·빌링키와 같은 등급.
- **등록 중 시드는 pending 자리에 따로 둔다.** 재등록 도중 이탈해도 기존 인증앱이 살아
  있어야 한다 — 정본을 바로 덮으면 QR 만 띄우고 창을 닫은 순간 계정이 비밀번호 하나로 떨어진다.
- **재사용 방지는 우리 책임.** pyotp 는 계산만 해준다. 같은 30초 창의 코드를 두 번 쓰지
  못하도록 성공 스텝을 기록하고 `select_for_update` 로 직렬화한다(TOCTOU).
- **`pwh`(비밀번호 변경 시 토큰 무효) 클레임은 넣지 않았다.** 전역 `CHECK_REVOKE_TOKEN` 은
  일반 회원까지 전부 재로그인시키고, 어드민만 따로 해도 얻는 것은 "최대 2시간"을 "즉시"로
  줄이는 것뿐이다. 계정을 끊어야 할 때는 `is_active=False`(발급된 토큰까지 즉시 401)가 있다.
- **부트스트랩(최초 등록)은 미등록 계정 전용.** 등록된 계정이 이 경로를 타면 비밀번호만으로
  인증앱을 갈아끼울 수 있다 → `already_enrolled` 로 차단. 재등록은 토큰+비번+현재 코드.
- **신뢰 기기 refresh 7일** (프론트 Q3 수락). access 는 2시간 그대로라 권한 회수 지연은
  늘지 않는다. **신뢰에 만료는 두지 않는다** — 실질 상한이 이미 refresh 수명이다.

### prod 배포 시 추가로 필요한 것

1. **이미지 재빌드** — `pyotp==2.9.0`, `qrcode==8.0` 신규 의존성 (requirements.txt).
   dev 컨테이너에는 `pip install` 로 넣어 뒀으므로 **컨테이너를 다시 만들면 사라진다.**
2. 마이그레이션 2건 (`admin_api.0008`, `emails.0009`) — entrypoint 가 자동 적용.
3. **`python manage.py seed_email_templates`** — `admin_device_code` 템플릿이 없으면 신규 기기
   승인 메일이 안 나간다(`EmailTemplateMissing`). 로그인 자체는 막히지 않지만 코드를
   받을 수 없어 신규 기기에서 들어올 수 없다.
4. `.env.production` 에 `ADMIN_MFA_ENFORCED` 를 **넣지 말 것** — 기본 False. 전원 등록 확인 후
   추가하고 web/celery 재기동.

### 남은 것

- **Phase 0 (계정 분리)** — prod staff 실태 확인부터. 이게 되기 전에는 MFA 를 켜도
  감사로그가 여전히 무의미하다.
- Phase 3a — Django admin 세션 MFA. `totp.verify_second_factor` 를 그대로 쓰는 커스텀
  `AdminAuthenticationForm` 이면 된다(라이브러리 추가 불필요 — 검증 로직이 이미 우리 것이다).

프론트 전달본: `docs/frontend/ADMIN_AUTH_MFA_FRONTEND.md` (v2) ·
회신: `docs/frontend/ADMIN_AUTH_MFA_RESPONSE.md`
