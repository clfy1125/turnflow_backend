# 어드민 3차 회신 — prod 응답 실측 + 플래그 ON 절차

회신: 백엔드 → 어드민 콘솔팀 · 2026-08-16
대상: `10_turnflow_admin/docs/ADMIN_AUTH_MFA_REPLY.md` (3차)

프론트 배포(`8087c8b9`) 확인했습니다. 롤아웃 2번 완료 — 다음은 3번(관리자 3명 등록)입니다.

---

## 1. ✅ `pending_total` — prod 응답에 **실제로 옵니다**

뷰를 직접 부르지 않고 **컨테이너 안에서 진짜 HTTP 호출**로 찍었습니다(`?refresh=1` 로 캐시
우회). 라우팅·시리얼라이저를 다 거친 실제 응답입니다.

```
GET /api/v1/admin/dashboard/operations/?window=24h&refresh=1   → HTTP 200

dm_quality.pending_total   = 2
dm_quality.rate_limited    = 0
dm_quality.legacy_pending  = 0
검산: accepted_pending 2 + queued 0 + submitting 0 + rate_limited 0 + legacy_pending 0 = 2  (일치)

status_summary.basis       = "now_24h"
status_summary.basis_hours = 24
action_required            = 없음 (제거 확인)
최상위 키 = [dm_quality, generated_at, ig_connections, range, recent_errors,
             risk_accounts, since, spam, status_summary, window]

GET /api/v1/admin/me/preferences/  → 200 {"preferences": {}}
```

**폴백을 지우셔도 됩니다.**

### 다만 폴백 조건 한 곳만 손봐 주세요

> "값이 **0 이거나** 필드가 없으면 옛 공식으로 폴백"

**0 은 정상값입니다** — 진행 중 DM 이 없으면 당연히 0 입니다. 위 실측에서도 오늘 이 순간
차이가 0 인데, 그건 `rate_limited`·`legacy_pending` 이 지금 마침 0 이기 때문이지 두 공식이
같아서가 아닙니다(dev 실측은 2 vs 16 이었습니다).

숫자상 사고가 나지는 않습니다 — `pending_total` 은 옛 공식의 **상위집합**이라 항상
`pending_total ≥ 옛 공식` 이고, `pending_total == 0` 이면 옛 공식도 0 입니다. 그래도
**존재 여부(`=== undefined`)로 판정**하시길 권합니다. 값이 0인지로 "필드가 없다"를 대신
판단하는 패턴은 필드 의미가 바뀌는 날 조용히 깨집니다.

## 2. 플래그 ON 절차 — 이렇게 진행하겠습니다

3번(3명 등록) 완료를 알려주시면:

1. 제가 **켜기 직전에** 이 문서에 "지금 켭니다" 를 남깁니다
2. `ADMIN_MFA_ENFORCED=True` 로 전환 (env 만, 재배포 아님 — 즉시 반영)
3. 그쪽이 30분 안에 `admin_token_required` 403 경로 확인 → 회신
4. 이상 있으면 **즉시 False 로 롤백** (같은 방식, 수십 초)

확인 방법은 알려드린 대로입니다 — 일반 토큰(`/api/v1/auth/login/` 발급분)으로 아무 어드민
API 를 호출하면 403 `admin_token_required` 가 떨어져야 합니다. 플래그가 꺼져 있으면 200 이라
**켠 뒤에만 재현됩니다.**

> 참고: 켜는 순간 **일반 토큰을 들고 있던 기존 세션이 전부 403** 이 됩니다. 관리자 3명이
> 그 시점에 콘솔을 열어두고 있다면 재로그인이 필요합니다. 업무 중이 아닌 시간대를
> 골라주시면 그 시각에 맞추겠습니다.

## 3. `mobile_nav` 키 선점 — 접수했습니다

서버는 키를 해석하지 않지만, 다른 기능이 같은 이름을 쓰지 않도록 기록해 두겠습니다.
현재 사용 중인 최상위 키:

| 키 | 소유 | 용도 |
|---|---|---|
| `mobile_nav` | 어드민 콘솔 | 모바일 하단 탭 구성 (경로 문자열 배열) |

새 키를 추가하실 때 이 문서에 한 줄 남겨주시면 충돌이 안 납니다. 판정 값을 넣지 않기로 하신
것도 확인했습니다 — 사용자가 PATCH 로 바꿀 수 있는 칸이라 신뢰 경계 밖이 맞습니다.

## 4. 백업코드 `length` 렌더 — 확인했습니다

`ADMIN_BACKUP_CODE_COUNT` 를 바꿔도 화면이 안 깨집니다. 바꿀 계획은 없습니다.

---

## 남은 순서

| 순서 | 주체 | 상태 |
|---|---|---|
| 1 | 백엔드 | prod 배포 ✅ |
| 2 | 프론트 | 웹 배포 ✅ (`8087c8b9`) |
| 3 | **관리자 3명** | **인증앱 등록 + 백업코드 보관** ← 지금 |
| 4 | 양측 | 확인 하루 |
| 5 | 백엔드 | 플래그 ON (§2 절차) |

실서버 로그인을 끝까지 밟아보시고 이상이 있으면 이 문서에 회신 주세요. 특히 **신규 기기
이메일 코드**가 실제로 도착하는지 확인 부탁드립니다 — dev 는 콘솔 출력이라 실제 메일 발송
경로는 prod 가 처음입니다.
