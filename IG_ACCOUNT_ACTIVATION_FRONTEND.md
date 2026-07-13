# 추가 IG 계정 축소 지연 + 활성 계정 선택 — 백엔드 구현 완료 (프론트 확정 계약)

> 작성: 백엔드 · 2026-07-10 · 브랜치 feat/toss-billing
> 프론트가 미리 만들어 둔 `IgAccountActivationDialog`/`IgAccountActivationController`/
> `billing-api.ts(fetchIgAccountActivation/updateIgAccountActivation)`/`types/billing.ts` 와 맞물립니다.
> 요청서(백엔드 전달 사항) 기준으로 구현했고, **3가지 델타**만 확인해 주세요(§4).

배포되면 아래 계약대로 동작합니다. Swagger `/api/docs/` → `사용자플랜` → `ig-account-activation`.

---

## 1. 추가 IG 계정 "축소" = 지연 반영 — `POST /billing/extra-accounts/`

요청서대로 구현:

- **증가**: 기존과 동일 — 즉시 비례 결제 + 즉시 반영.
- **감소**: 즉시 슬롯을 줄이지 않고 **예약만** 합니다. 연동 계정 수 초과로 **거부하지 않습니다**(이번 주기 그대로 사용).
- **다음 갱신**: `extra_ig_accounts = pending_extra_ig_accounts` 로 확정, `pending_extra_ig_accounts = null`.
- **예약 취소**: 현재 적용값(`extra_ig_accounts`)과 **같은 count** 로 다시 요청하면 예약 해제.
- **응답**에 `effective_at`(=현재 주기 종료일, 감소 예약 시) 추가. 감소 응답의 `detail` 은
  "…다음 갱신일부터 낮아진 금액이 적용됩니다".

`GET /billing/my-subscription/` 노출 필드:
```jsonc
{
  "extra_ig_accounts": 2,             // 현재 적용값
  "pending_extra_ig_accounts": 0,     // 예약된 감소값(없으면 null) — 프론트 타입에 이미 추가됨
  "ig_activation_review_needed": true // 갱신 자동조정 발생 → 활성 계정 재선택 유도
}
```

프리뷰 `POST /billing/extra-accounts/preview/` 의 감소는 그대로 `immediate_charge.amount=0,
direction:"decrease"` 이며, 이제 `effective_at`(현재 주기 종료일)도 채워 보냅니다.

---

## 2. 활성 IG 계정 선택 — 신규 엔드포인트 2개

허용량 = **1 + extra_ig_accounts** (무제한은 999999). 허용량은 **활성(is_active) 계정 수**를 제한합니다.

### GET `/api/v1/billing/ig-account-activation/`
```jsonc
{
  "needs_activation_adjustment": true, // 활성수 > 허용량 또는 갱신 자동조정 발생. ★다이얼로그 트리거로 이것 사용★
  "max_ig_accounts": 1,                // 무제한은 999999
  "total_accounts": 3,                 // 연동된(비-REVOKED) 계정 수
  "active_accounts": 3,
  "can_change_today": true,            // 하루 1회. 강제 조정 상황이면 항상 true
  "accounts": [
    {
      "id": "0a1b2c3d-...",            // 문자열 UUID (POST 로 그대로 전달)
      "username": "turnflow_official",
      "name": "Turnflow",              // 미동기화 시 ""
      "profile_picture_url": "https://media.turnflow.link/...",  // 안정 URL, 미동기화 시 ""
      "is_active": true,
      "status": "active",              // active/expired/error (REVOKED 는 목록에서 제외)
      "workspace_name": "내 워크스페이스"
    }
  ]
}
```

### POST `/api/v1/billing/ig-account-activation/`
```jsonc
// 요청
{ "active_account_ids": ["0a1b2c3d-...", "..."] }
```
- 응답은 GET 과 **동일 스키마**(갱신된 상태).
- 검증: 개수 ≤ `max_ig_accounts`(초과 400), 전부 본인 소유(아니면 400), 최소 1개.
- 하루 1회 제한이 있으나 **강제 조정 상황(needs_activation_adjustment=true)에서는 항상 허용**.
- 동작: 선택 계정은 `is_active=true`, 나머지 소유 계정은 **소프트 비활성**(연결/토큰 보존).

---

## 3. Enforcement (비활성 계정이 실제로 기능에서 빠집니다)

`is_active=false` 계정은 아래에서 전부 제외됩니다(하드 연결해제 아님, 토큰/데이터 보존):
- Auto-DM 트리거/발송/수신(댓글·스토리답장·리워드), 스팸 필터, 인사이트 동기화, 웹훅 재구독, 폴링.
- **소프트 비활성 시 그 계정의 활성 캠페인은 자동 PAUSE, 발송 대기 중이던 DM 은 SKIPPED**.
- 비활성 계정에는 신규 캠페인 생성/재개가 400 으로 차단됩니다.

---

## 4. 프론트에서 확인/반영할 3가지 델타 ⚠️

1. **다이얼로그 트리거는 `active_accounts > max_ig_accounts` 가 아니라 `needs_activation_adjustment` 를 사용**하세요.
   갱신 시 초과분을 **자동 비활성**하므로 보통 `active == max` 가 되어 단순 비교로는 다이얼로그가 안 뜹니다.
   백엔드가 `ig_activation_review_needed` 를 세워 `needs_activation_adjustment=true` 로 유지하니, 이 필드로 여세요.
2. **허용량 기준 = 활성 계정 수**. 비활성 계정은 OAuth 연결 슬롯을 비웁니다(하드 해제 없이 계정 교체 가능).
3. **재활성화된 계정의 캠페인은 자동 재개되지 않습니다.** 사용자가 캠페인을 직접 다시 활성화해야 합니다(안내 문구 권장).

## 5. 배포 순서
- 프론트/백엔드 순서 자유. 미배포 동안 프론트가 GET 404 를 무시하도록 이미 가드돼 있습니다.
- 무제한 판정: `max_ig_accounts < 0 || >= 9999`.
