# [배포 완료] 어드민 2단계 로그인 + 20차 4건 — prod 반영됐습니다

발신: 백엔드 → 어드민 콘솔팀 · 2026-08-16
배포 커밋: `79375a7` (prod 전 컨테이너 동일 이미지 확인)

---

## 1. 🟢 롤아웃 1-b 통지 — **프론트를 올리셔도 됩니다**

`ADMIN_AUTH_MFA_RESPONSE.md` §9 에서 약속드린 **"prod 배포 완료 신호"** 입니다.
`/api/v1/admin/auth/refresh/` 를 포함한 엔드포인트 8종이 **모두 prod 에서 응답합니다.**

기다리셨던 조건이 충족됐습니다 — 갱신 URL 을 바꿔 배포하셔도 2시간 뒤 전원 로그아웃되지
않습니다.

**아직 강제는 켜지 않았습니다.** `ADMIN_MFA_ENFORCED=False` 라서 지금은 기존 로그인
(`/api/v1/auth/login/` + 일반 토큰)도 그대로 동작합니다. 즉 **구·신 경로가 동시에 살아 있는
상태**이며, 이게 §7 롤아웃 표의 2번 구간입니다.

### 순서

| 순서 | 주체 | 상태 |
|---|---|---|
| 1 | 백엔드 | 엔드포인트 prod 배포 ✅ **완료 (지금)** |
| 2 | **프론트** | **웹 배포** (= 안드로이드 셸 자동 반영) ← 지금 하시면 됩니다 |
| 3 | 관리자 3명 | 인증앱 등록 + 백업코드 보관 |
| 4 | 양측 | 확인 기간 하루 |
| 5 | 백엔드 | `ADMIN_MFA_ENFORCED=True` (env 전환, 재배포 아님) |

**5번은 3번이 끝난 것을 확인한 뒤에만** 합니다. 먼저 켜면 관리자 3명이 동시에 잠깁니다.

### 배포 후 확인 부탁

프론트 배포 직후 **로그인 한 번**만 끝까지 밟아봐 주세요(비번 → 인증앱 등록 → 백업코드 →
대시보드 진입). 실서버에서만 확인 가능한 경로라고 하셨던 부분입니다.

`admin_token_required` 403 경로는 5번(플래그 ON) 직후에만 재현됩니다 — 켜는 시점을 미리
알려드릴 테니 그때 30분 안에 확인해 주시면, 이상하면 즉시 되돌리겠습니다.

---

## 2. 계약 변경 재확인 (v1 → v2)

이미 `ADMIN_AUTH_MFA_RESPONSE.md` §7 로 보내드렸지만, 배포됐으니 다시 짚습니다.
**이 3가지는 프론트 코드에 손이 갑니다.**

1. **백업코드는 `code` 가 아니라 `backup_code` 필드** · 8자 → **12자** (`ABCD-EFGH-JKLM`)
2. **`mfa/confirm/` 의 `setup_token` 필수** — `setup` 응답값을 그대로 전달
3. **재등록 `setup/` 은 `password` + 현재 `code`** (v1 은 password 만)

그리고 `password_changed` 사유 코드는 **발생하지 않습니다**(기능을 넣지 않기로 했습니다).

> ⚠️ **마케팅 전용 계정은 갱신 URL 이 갈립니다.** `admin_role === "marketing_viewer"` 면
> 기존 `/api/v1/auth/token/refresh/`, 그 외는 `/api/v1/admin/auth/refresh/` 입니다.
> 마케팅 계정은 `/admin/auth/login/` 에서 `mfa_required:false` + `tokens` 를 바로 받습니다.

---

## 3. 20차 4건도 같은 배포에 들어갔습니다

전문은 `ADMIN_20TH_ROUND_RESPONSE.md`. 프론트가 손봐야 할 것은 **OPS-6 하나**입니다.

### 🔴 OPS-6 — '대기' 숫자가 지금까지 틀렸습니다

`accepted_pending + queued + submitting` 공식이 **`rate_limited` 와 legacy `pending` 을
빠뜨리고** 있었습니다. dev 실측으로 **2건 vs 실제 16건**(`rate_limited`만 14건)입니다.

```jsonc
"dm_quality": {
  "rate_limited": 4,      // 신규
  "legacy_pending": 5,    // 신규
  "pending_total": 15     // 신규 — 이 값을 쓰세요
}
```

**라벨을 '대기' 대신 '진행 중' 으로** 바꿔 주세요. `?status_group=waiting` 드릴다운은
`accepted` 를 빼므로 이 값보다 작게 나오는 것이 정상인데, 라벨이 같으면 버그로 보입니다.

### 나머지 셋 (프론트 변경 없이 안전)

- **OPS-5** `action_required` **응답에서 제거**됐습니다. zod 에서 이미 빼셨으니 그대로 두시면 됩니다.
- **OPS-7** `status_summary` 가 이제 **선택 window 와 무관하게 최근 24h** 입니다.
  `basis: "now_24h"` 가 함께 옵니다 → **전체 기간일 때 24h 를 한 번 더 조회하던 것을 지우세요.**
- **UI-1** `GET/PATCH /admin/me/preferences/` 사용 가능합니다. 키 단위 병합(깊이 1),
  `null` 로 키 삭제, 4KB 상한, 마케팅 계정도 허용.

---

## 3-b. 🔴 실제 코드를 대조했습니다 — 지금 고쳐야 할 4곳

회신서에 적어주신 구현 목록을 **실제로 열어 배포된 API 와 대조**했습니다. v1 계약으로
만드신 상태라 **네 곳이 어긋납니다.** 이대로 배포하면 백업코드 로그인과 MFA 등록이 동작하지
않습니다. 파일·행을 그대로 적으니 바로 고치실 수 있습니다.

| # | 위치 | 지금 | 배포된 서버가 기대하는 것 | 그대로 두면 |
|---|---|---|---|---|
| 1 | `useAdminAuth.ts` `useVerifyMfa` 타입<br>`MfaStep.tsx:89` | 백업코드를 **`code` 에 담아** 전송 (`useBackup ? backupCode : code`) | **별도 필드 `backup_code`** | 🔴 **백업코드 로그인 전부 실패** — 서버가 `code` 를 TOTP 로만 보므로 6자리 숫자가 아니면 `invalid_code` |
| 2 | `useAdminAuth.ts` `useMfaConfirm` | `{ code }` | `{ setup_token, code, email_code? }` | 🔴 **MFA 등록 불가** — 400 `challenge_expired` |
| 3 | `useAdminAuth.ts` `useMfaSetup` | `{ password?, setup_token? }` | **재등록**은 `password` + **현재 `code`** | 재등록 400 `invalid_code` (최초 등록은 정상) |
| 4 | `apiClient.ts:77` `REFRESH_PATH` | 항상 `/admin/auth/refresh/` 고정 | **마케팅 계정은 `/auth/token/refresh/`** | 마케팅 파트너 계정 토큰 갱신 실패 |

추가로 **`MfaStep.tsx:81` 의 백업코드 길이 검증이 `>= 8`** 인데 v2 는 **12자**
(`ABCD-EFGH-JKLM`)입니다. 하이픈·공백·대소문자는 서버가 무시하니 **프론트에서 정규화하지
마시고** 길이 검증만 완화해 주세요.

그리고 `login/page.tsx:236` 의 `AUTH_CODES.backupCodeUsed` 분기는 **죽은 코드**입니다 —
v2 는 이미 쓴 백업코드도 `invalid_code` 로 답합니다(유효했던 코드라는 정보를 흘리지 않기
위해서입니다). 지우셔도 되고 남겨두셔도 무해합니다.

### 사유 코드 — 불일치 1건 + 누락 7건

`src/types/adminAuth.ts` 의 `AUTH_CODES` 를 서버와 대조했습니다.

**🔴 불일치 — 이건 조용히 깨집니다.**

```ts
rateLimited: "rate_limited",   // ← 서버는 대문자 "RATE_LIMITED" 를 보냅니다
```

프로젝트 표준(`apps/core/exceptions.py`)이 스로틀 429 를 **`RATE_LIMITED`(대문자)** 로 내보냅니다.
소문자로 비교하면 **429 분기가 영영 안 걸려** "요청이 너무 잦습니다" 대신 일반 오류로 떨어집니다.
어드민 로그인은 5회/분이라 실제로 마주칠 값입니다.

**누락 — 화면 분기가 필요한 것들.**

| 코드 | HTTP | 언제 | 권장 처리 |
|---|---|---|---|
| `already_enrolled` | 400 | 등록된 계정이 최초등록 경로 사용 | 로그인 화면으로 |
| `setup_not_started` | 400 | QR 없이 confirm 호출 | 등록 처음부터 |
| `not_enrolled` | 400 | 미등록 상태로 백업코드 재발급 | 등록 화면으로 |
| `token_expired` | 401 | refresh 만료·폐기 | 로그아웃 |
| `device_revoked` | 401 | 보안 화면에서 해제된 기기 | 로그아웃 + "이 기기 로그인이 해제됨" 안내 |
| `user_inactive` | 401 | 계정 비활성/스태프 해제 | 로그아웃 |
| `device_not_found` | 404 | 남의 기기·이미 해제된 기기 | 목록 새로고침 |

### `mfa/status/` 응답 — Q4 요청분이 목에 아직 없습니다

요청하신 필드를 **전부 넣어 배포**했는데 `src/mocks/handlers/adminAuth.ts` 의 목에는 아직
없습니다. 실서버 응답에는 옵니다.

```jsonc
{
  "last_login_at": "2026-08-21T09:02:11Z",   // Q4 요청분 (계정 단위)
  "backup_codes_low_threshold": 3,           // Q4 요청분 — 3 하드코딩 대신 이 값과 비교
  "trusted_devices": [{
    "is_trusted": true,                       // false = 신뢰 등록 없는 임시 세션 (해제 대상에서 빼지 마세요)
    "created_at": "2026-08-18T02:11:00Z",     // Q4 요청분
    "expires_at": null                        // Q4 답: 항상 null = "해제할 때까지"
  }]
}
```

### 4번 보충 — 역할별 갱신 URL

마케팅 계정은 `/admin/auth/login/` 에서 **일반 토큰**을 받습니다(2단계 없음). 일반 토큰은
어드민 갱신 엔드포인트가 거부하므로(400 `not_admin_token`) 갱신 경로를 갈라야 합니다.

```ts
// apiClient.ts — single-flight 구조는 그대로, URL 만 역할로 고른다
const isMarketing = qc.getQueryData<AdminMe>(ADMIN_ME_KEY)?.admin_role === "marketing_viewer";
const path = isMarketing ? "/auth/token/refresh/" : "/admin/auth/refresh/";
```

## 4. 지금 prod 상태 (참고)

- 마이그레이션 4건 적용 (`admin_api` 0008·0009, `emails` 0009, `integrations` 0050)
- 전 컨테이너 동일 이미지 · DB/Redis 무중단(재생성 없음)
- 자동 DM 발송 정상: 배포 직후 10분간 8건 생성, 정체 0, 실패 0
- 어드민 MFA 등록자 0명 (아직 아무도 등록 전 — 정상)

문의는 이 문서에 회신 주시면 됩니다.
