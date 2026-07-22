# [백엔드 응답] 구독 해지 리텐션 플로우 — 백엔드 구현 완료 보고

대상: 프론트(TurnflowLink) · 작성: Turnflow 백엔드
요청서: `backend-cancel-retention.md` (프론트 → 백엔드)

프론트 요청 4개 항목을 **모두 구현**했습니다. 각 항목은 독립적이라 부분 배포 가능합니다.
공통: 인증 `Authorization: Bearer <access_token>` 필수. 에러는 `{"detail": "...", "code"?: "..."}`.

---

## 확인 부탁 3가지에 대한 답

1. **해지 후 데이터 보존 기간** → **무기한(계정이 유지되는 한)**. 해지 만료 시 free 강등 + 기능만
   게이팅되고, 캠페인/설정/분석 데이터는 삭제하지 않습니다(파기 태스크 없음). 완료화면 문구
   "안전하게 보관" 그대로 사용하시면 됩니다. (계정 탈퇴 시에만 삭제)
2. **일시정지 연 N회** → **연 1회**(마지막 정지 요청 후 365일). `can_pause` 로 내려줍니다.
   추가 조건: **active 유료 + 카드 보유**만 가능(체험/미납/취소/이미 정지 상태 불가).
3. **리텐션 할인 제약** → **1인 1회 + active 유료 + 카드 보유 + 트라이얼 제외**.
   이미 썼으면 400 `retention_offer_already_used`.

---

## 1. 구독 일시정지 (Pause) ✅

`POST /api/v1/billing/pause/`
```jsonc
// 요청
{ "months": 1 | 2 | 3 }   // 그 외 값 400
// 200 → UserSubscription (아래 신규 필드 포함)
```

**동작**
- 정지 시작 = **현재 결제주기 종료일부터**. 잔여 유료기간은 프로로 그대로 이용(무손실).
- 정지 중: 프로 기능 비활성(무료 수준), **데이터·설정·캠페인 전부 보존**. 과금 없음.
- 자동 재개: `pause_ends_at` 도래 시 자동 유료 재개 + 과금. **재개 3일 전 이메일 사전 고지**(구현됨).
- 정지 중 완전 해지 가능(`POST /billing/cancel/`) / 조기 재개 가능(`POST /billing/resume/`).

**재개(정지 해제)** — 별도 엔드포인트 없이 **기존 `POST /billing/resume/` 재사용**:
- 잔여 유료기간 내 재개 → 무과금으로 정지만 취소.
- 이미 정지 개시(기간 경과) 후 재개 → 즉시 갱신 과금 트리거.

**응답 신규 필드** (`GET /billing/my-subscription/` 에도 동일 노출)
```jsonc
{
  "status": "paused",              // status enum 에 "paused" 추가됨
  "pause_ends_at": "2026-12-03T00:00:00Z",  // 자동 재개 예정일(없으면 null)
  "paused_months": 2,              // 정지 개월(없으면 null)
  "can_pause": true                // 이번에 정지 가능한지(연1회·active유료·카드)
}
```
> 프론트 연결: `RETENTION_PAUSE_ENABLED = true`, 오퍼 `onAccept` 에서 위 API 호출 →
> `offer_accepted`(offer:"pause") 발사 + `refreshSubscription` + 모달 닫기.

**400 사유**: 무료/관리자 플랜, 이미 정지, 활성 아님(체험/미납/취소), 카드 미등록,
`code:"pause_limit_reached"`(연 1회 초과).

---

## 2. 리텐션 할인 쿠폰 (다음 1회 50%) ✅

`POST /api/v1/billing/retention-offer/apply/`
```jsonc
// 요청 (offer 생략 시 discount_50)
{ "offer": "discount_50" }
// 200
{
  "applied": true,
  "next_charge_amount": 7950,               // 할인 반영 금액(원) — 하드코딩 말고 이 값 표시
  "next_charge_date": "2026-09-03T00:00:00Z",
  "subscription": { /* UserSubscription */ }
}
```

**동작**
- **다음 1회 갱신에만** 50% 적용 → 성공 시 자동 소멸(이후 정상가).
- **1인 1회** — 이미 사용 시 400 `{"code":"retention_offer_already_used"}`.
- active 유료 + 카드 보유만 대상. 트라이얼/취소/정지/미납 제외.

**구독 응답에 추가된 표시 필드**
```jsonc
{
  "renewal_amount": 7950,                  // 다음 갱신 예정액(할인 대기 시 할인 반영값)
  "retention_discount_pending": true,      // 다음 1회 할인 대기 중
  "retention_discount_available": false    // 지금 할인 받을 수 있는지(1인1회·active유료·카드)
}
```
> 프론트 연결: `RETENTION_DISCOUNT_ENABLED = true`, 오퍼 `onAccept` 에서 호출.

---

## 3. 취소 여정 트래킹 이벤트 enum 추가 ✅

`POST /api/v1/track/cancellation-event/` 의 `event` enum 에 3개 추가 + `offer` 필드 저장:
```
offer_shown      offer_accepted      offer_declined
```
```jsonc
// 예시
{ "event": "offer_accepted", "offer": "downgrade_basic" | "pause" | "discount_50", "from_plan": "pro" }
```
- `offer`(문자열, 최대 40자) 저장됨 → 오퍼별 방어율 퍼널(shown→accepted vs declined) 측정.
- 기존 5개 이벤트/필드 그대로. fire-and-forget 이므로 UX 영향 없음.

---

## 4. 윈백 이메일 (해지 후 복귀 유도) ✅ (백엔드 전용, 프론트 관여 없음)

- 해지 후 `WINBACK_AFTER_DAYS`(기본 30) 일 경과한 **유료 이탈자**에게 복귀 유도 메일 발송.
- **마케팅 수신 동의자**(`user.marketing_opt_in`)에게만(정보통신망법). 발송 이력(EmailLog)으로 중복 방지.
- 매일 배치(`billing.send_winback_emails`) + 전용 이메일 템플릿(`winback`) 구현.
- **현재 dormant**: `WINBACK_ENABLED=False`(기본) + 동의 수집 경로 미연결이라 실발송 0.
  → **동의 수집(가입/설정 체크박스 → `marketing_opt_in`)** 이 붙고 `WINBACK_ENABLED=True` 전환 시 활성화됩니다.
  프론트에서 마케팅 동의 UI를 추가할 계획이면 알려주세요(auth 필드/엔드포인트 연결).

---

## 프론트가 이미 처리한 것 (변경 없음 · 재확인)

| 항목 | 그대로 사용 |
|---|---|
| "이번 달 전송 N명" | `subscription.usage.dm.used` |
| 베이직 다운그레이드 오퍼 | `POST /billing/change-plan/ {plan_name:'basic'}` |
| 해지 확정 | `POST /billing/cancel/` (정지 중에도 호출 가능) |
| 재개 | `POST /billing/resume/` (취소·정지 모두 대응하도록 확장됨) |
| 사유 설문 | `cancel_reason_submitted` 의 `reason`/`reason_detail` |

---

## 배포 메모 (운영)

- 마이그레이션: `billing 0019`(구독 필드), `analytics 0004`(offer), `authentication 0004`(marketing_opt_in),
  `core 0009`(리텐션 주기잡 ScheduledJob 시드).
- 이메일 템플릿 신규 2종 → 배포 후 `python manage.py seed_email_templates` 필요(`pause_resume_reminder`, `winback`).
- 프로덕션 주기 실행은 `core.ScheduledJob`(0009 시드)이 담당 — CELERY_BEAT_SCHEDULE 은 dev/문서용.
