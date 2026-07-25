# [백엔드 응답] 마케팅 수신동의(marketing_opt_in) 연결 완료 + 리텐션 확인 4건

응답: 2026-07-23 · 요청 원본: `backend-marketing-opt-in.md` (프론트, 2026-07-22)
브랜치: `feat/toss-billing` · 마이그레이션: **없음**(필드는 auth 0004 에 이미 존재, 시리얼라이저/뷰만 연결)

---

## 0. 요약 (TL;DR)

- ✅ `marketing_opt_in` 을 **register / GET·PATCH me / google** 세 경로에 전부 연결했습니다. 배포 즉시 수집 시작.
- ✅ **수신거부 = `PATCH /auth/me/ {marketing_opt_in:false}`** 한 곳으로 수렴합니다(별도 토큰 엔드포인트 없음).
- ✅ 리텐션 확인 4건 전부 "프론트 구현과 서버 강제가 일치" — 아래 표 참고.
- ℹ️ "취소 시 쿠폰을 메일로 보내는 장치"는 **원래 존재하지 않습니다**. 원하던 "즉시 50% 할인"은
  이미 `POST /billing/retention-offer/apply/` (인앱·즉시)로 제공 중입니다. 윈백 이메일은
  **제품 결정으로 계속 dormant**(발송 0) 유지합니다. → §4.

---

## 1. marketing_opt_in — 연결된 계약

### 1-1. 회원가입 `POST /api/v1/auth/register/`

요청 바디에 `marketing_opt_in`(boolean, optional, **기본 false**) 추가. 프론트가 항상 보내던 그대로 동작합니다.

```jsonc
// 요청
{ "email": "...", "password": "...", "password_confirm": "...", "full_name": "...",
  "marketing_opt_in": true,          // ← 이제 실제로 저장됨 (true 면 동의 시각도 기록)
  "attribution": { ... } }
// 201 → user 객체에 marketing_opt_in 포함
{ "user": { "...": "...", "marketing_opt_in": true }, "tokens": { "...": "..." } }
```

- `true` 로 가입 → `marketing_opt_in=true` + `marketing_opt_in_at`(동의 시각) 기록.
- 미전송/`false` → `false`, 동의 시각 null. (이전에 유실되던 동의가 이제 정상 기록됩니다.)

### 1-2. 내 프로필 `GET/PATCH /api/v1/auth/me/`

**GET 응답에 두 필드 추가** (프론트 fail-closed 토글이 이제 자동 노출됩니다):

```jsonc
{
  "id": 1, "email": "...", "full_name": "...",
  "is_email_verified": true, "email_verified_at": "...",
  "date_joined": "...", "last_login": "...",
  "marketing_opt_in": false,             // ← 설정 토글 렌더 근거
  "marketing_opt_in_at": null            // 동의 시각 (미동의면 null)
}
```

**PATCH 로 토글** (기존 full_name 과 동일 패턴, `marketing_opt_in` 만 보내도 됨):

```jsonc
// 요청
{ "marketing_opt_in": true }
// 200 → 갱신된 '프로필 전체' 반환 (GET /me 와 동일 형식) ★ 응답 형태가 전체로 확장됨
{ "id": 1, "email": "...", "marketing_opt_in": true, "marketing_opt_in_at": "2026-07-23T...", "...": "..." }
```

> ⚠️ 변경점: PATCH `/auth/me/` 응답이 이전엔 `{full_name}` 만 담았지만, 이제 **프로필 전체**를
> 돌려줍니다(요청 파일의 "응답은 갱신된 프로필 전체" 반영). `full_name` 은 그대로 있으니 기존 코드 무영향.

- `true` 전송 → 동의 시각 기록. `false` 전송(=수신거부) → 동의 시각 제거.

### 1-3. 구글 가입 `POST /api/v1/auth/google/`

신규 가입 시에만 `marketing_opt_in`(optional, 기본 false) 반영. 기존 계정 로그인 시엔 무시(변경 안 함).

---

## 2. 수신거부(unsubscribe) — 확인 요청 답변 (요청 §2-3)

- **네, 같은 필드 한 곳으로 수렴합니다.** 설정 토글·수신거부 모두 `PATCH /auth/me/ {marketing_opt_in:false}`.
- **별도 토큰 기반 엔드포인트는 현재 없습니다.** 윈백 메일 하단은 지금 "수신 원치 않으면 고객센터로
  알려주세요" 문구이며(자동 원클릭 링크 아님), 윈백이 **dormant(발송 0)** 이라 실사용 경로가 아직 없습니다.
- 향후 윈백/마케팅 메일을 실제로 켤 경우, **메일 내 원클릭 수신거부 토큰 링크**(로그인 없이 `marketing_opt_in`
  를 끄는 공개 엔드포인트)를 추가 구현하는 것이 정보통신망법상 안전합니다. 켜기로 결정되면 그때 붙이겠습니다.

---

## 3. 리텐션 연동 확인 4건 (요청 §3)

| # | 프론트 질문 | 서버 실제 동작 | 결론 |
|---|---|---|---|
| 1 | `next_billing.amount` 가 리텐션 할인을 반영하나? | `next_billing.amount = sub.renewal_amount` 이고, `renewal_amount` 는 `retention_discount_pending` 일 때 **50% 반영**. 즉 `renewal_amount` == `next_billing.amount`(둘 다 할인 반영). | ✅ **반영함**. 프론트의 `renewal_amount ?? next_billing.amount` 어느 쪽이든 할인가. |
| 2 | paused 에서 `change-plan`·`extra-accounts` 는 400? | 두 경로 모두 서버가 거부: `{"detail": "일시정지 중인 구독입니다. 정지를 해제한 후 변경해주세요."}` (HTTP 400). | ✅ **400 + 사용자 문구**. 그대로 토스트 노출 가능. |
| 3 | 할인율 변경 시 오퍼 코드도 새로? | 동의. 현재 지원 오퍼는 `discount_50` 하나. 30% 등으로 바뀌면 `discount_50` 재정의하지 않고 `discount_30` 새 코드로 내려주고 스웨거 enum 갱신하겠습니다. | ✅ 규칙 확정 |
| 4 | 정지 예약 구간 vs 실제 정지 구간 기능 판정이 서버 강제와 일치? | 서버 게이팅(`get_effective_plan`)이 **정확히 동일**: `paused && current_period_end > now` → 유료 플랜 유지, 그 이후 → free 반환. 수치 한도(페이지 수·DM 월 한도)도 free 기준으로 강제됨. | ✅ **완전 일치** |

**#4 상세** — 실제 정지 구간(정지 개시 후)의 수치 한도:
- 페이지: free `max_pages`(기본 1) 초과분은 다운그레이드/갱신 시 자동 비활성(초과 보유는 게이지에서 `active` 기준으로 표기).
- DM 월 한도: free `dm_monthly_limit`(기본 200)로 강제(`get_dm_monthly_limit` 이 `get_effective_plan` 사용).
- IG 계정: free `max_ig_accounts`(기본 1) 초과분 소프트 비활성.
→ 게이지는 잔여 유료기간엔 유료 한도, 정지 개시 후엔 free 한도로 맞추면 서버와 일치합니다.

---

## 4. "취소 시 쿠폰 메일" / 즉시 50% 할인 / 윈백 — 정리

요청·구두 논의에서 언급된 "구독취소 시 쿠폰을 메일로 보내 붙잡는 장치"는 **코드에 존재한 적이 없습니다.**

- 취소(`POST /billing/cancel/`)는 **메일을 보내지 않습니다.**
- 원하던 "다음달 50% 할인 즉시 적용"은 **이미 인앱 API로 제공 중**입니다:
  `POST /api/v1/billing/retention-offer/apply/` (다음 1회 50%·1인1회·성공 시 자동 소멸).
  상세는 [CANCEL_RETENTION_FRONTEND.md](CANCEL_RETENTION_FRONTEND.md) §2. → **프론트는 이 API로 UI 구성하면 됩니다.**
- 리텐션 관련 유일한 메일인 **윈백**(해지 30일 뒤 "돌아오세요" 안내, 쿠폰 없음)은
  **제품 결정으로 계속 dormant** 유지합니다(`WINBACK_ENABLED=False`, 실발송 0). 코드/템플릿은
  존치하므로, 추후 마음이 바뀌면 env 하나로 켤 수 있습니다.

`marketing_opt_in` 수집 자체는 윈백과 무관하게 **정보통신망법상 필수**이고 프론트 UI가 이미 준비돼
있어 그대로 연결했습니다.
