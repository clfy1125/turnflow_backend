# 무료 체험 중 추가 IG 계정 — 0원 즉시 추가 (프론트 변경 요청)

**작성**: 2026-08-21 · 백엔드
**배포**: 백엔드 반영 완료 (마이그레이션 없음)
**성격**: **비파괴 변경**(필드 추가 + 400 → 200). 프론트를 안 고쳐도 **동작은 됩니다.**
단, 아래 **§3 필수 2건**을 안 하면 사용자에게 잘못된 금액 문구가 보입니다.

---

## 0. 왜 고쳤나 (실제 사고)

프로 **무료 체험 중**인 사용자가 IG 계정을 1개 더 붙이려다 **완전히 막혀 있었습니다.**

CS 티켓 `#d34572b3` (suecap1@gmail.com, 프로 체험 중, 첫 결제 2026-09-12):
> "25000원인가 그거 클릭했는데 더 진행 안 되네요."

이 사용자는 **3분 동안 8번** 시도했습니다(`IGOAuthState` 잔존 행 8개로 확인). 원인은 두 벽이
서로를 가리키는 순환 고리였습니다.

```
설정 → 다른 계정 연결하기 → OAuth 성공 → 콜백에서 PLAN_LIMIT_EXCEEDED
   "…프로 요금제에서 계정을 추가하면 더 연결할 수 있어요"
        ↓ (안내를 따라감)
요금제 → 프로 카드 스테퍼 2개 → [결제하고 변경] (24,800원 = 14,900 + 9,900)
   POST /billing/extra-accounts/preview/ → 400 "무료 체험 중에는 추가 계정을 변경할 수 없습니다"
        ↓ (프론트가 사유를 버리고 일반 실패로 처리)
   QuoteGate: "결제 금액을 불러오지 못했습니다 / 잠시 후 다시 시도해 주세요" [다시 시도]
        ↓ 눌러도 영원히 같은 실패 → 위로 되돌아감
```

백엔드가 체험 중 추가 계정을 **하드 블록**한 이유는 "무료 기간에 일할 청구(약 6,900원)를 하면
무료 체험이 아니게 된다"였습니다. 막는 것 자체는 맞았지만 **대안이 없었던 것**이 문제였습니다.

**새 정책 (2026-08-21 제품 결정)**
> 체험 중에는 **0원으로 즉시** 추가해 주고, **체험 종료일 첫 결제부터** 합산된 총액을 청구한다.

---

## 1. 백엔드가 바뀐 것 — 요약 3줄

| | 전 | 후 |
|---|---|---|
| `POST /billing/extra-accounts/preview/` (체험 중) | **400** `무료 체험 중에는…` | **200** + `trial: true`, `immediate_charge.amount: 0` |
| `POST /billing/extra-accounts/` (체험 중) | **400** 같은 메시지 | **200** — 슬롯 즉시 증가, `payment: null`, **토스 승인 호출 없음** |
| 체험 종료 시 첫 결제 | 플랜가만 | **플랜가 + 9,900 × 추가 계정** (= `next_renewal_amount`) |

- 다른 상태(`past_due` 미납 / `cancelled` 해지예약 / `paused` 일시정지)는 **그대로 400**입니다.
- **감소(축소)** 는 체험 중에도 종전과 동일 — 즉시 반영 없이 다음 갱신(=첫 결제) 예약.
- 응답 스키마는 **필드 추가만** 했습니다. 기존 필드의 의미·타입은 그대로입니다.
- 마이그레이션 없음. 새 엔드포인트 없음.

---

## 2. 새 필드 — `trial` (boolean)

`POST /billing/extra-accounts/preview/` 응답 **최상위**에 추가됐습니다.
`direction` 3종(`increase`/`decrease`/`noop`) **모두**에 항상 들어갑니다.

### 왜 `amount === 0` 로 판별하면 안 되나

0원이 나오는 경우가 **두 가지**이고, 사용자에게 보여줄 문구가 다릅니다.

| 상황 | `trial` | `amount` | 화면 문구 |
|---|---|---|---|
| ① 체험 중이라 무과금 | `true` | `0` | "체험 중이라 **결제 없이 바로 사용**, 첫 결제일부터 합산 청구" |
| ② 유료인데 갱신일이 오늘 → 잔여 비례액 0 | `false` | `0` | "**오늘 0원 결제** · 다음 갱신부터 월 N원" |

②는 기존에도 존재하던 경로입니다(백엔드 문서에 "잔여 비례액이 0이면 무과금 즉시 적용"으로
명시돼 있었음). 그래서 금액만으로는 갈라낼 수 없어 판정 결과를 필드로 내려줍니다.

### 응답 예시 — 체험 중 증가 (0 → 1)

```json
{
  "direction": "increase",
  "delta": 1,
  "trial": true,
  "immediate_charge": {
    "amount": 0,
    "currency": "KRW",
    "description": "무료 체험 중이라 추가 계정 1개는 지금 결제 없이 바로 사용할 수 있습니다. 체험이 끝나는 날 첫 결제부터 합산된 금액이 청구됩니다.",
    "proration": null
  },
  "effective_at": null,
  "next_renewal_amount": 24800,
  "unit_price": 9900
}
```

★ **`immediate_charge.proration` 이 `null`** 입니다(유료 증가일 때는 객체). 무과금인데
`remaining_days`·`full_amount` 같은 일할 내역을 붙이면 화면이 계산 근거를 보여주게 되므로
일부러 비웠습니다. **`proration!.remaining_days` 처럼 non-null 단정하면 런타임 크래시**입니다.

### 응답 예시 — 유료 증가 (종전과 동일, `trial: false` 만 추가)

```json
{
  "direction": "increase",
  "delta": 2,
  "trial": false,
  "immediate_charge": {
    "amount": 7920,
    "currency": "KRW",
    "description": "추가 계정 2개 잔여 12일분 비례 청구",
    "proration": {
      "period_days": 30, "remaining_days": 12,
      "unit_price": 9900, "units": 2, "full_amount": 19800, "net": 7920
    }
  },
  "effective_at": null,
  "next_renewal_amount": 35700,
  "unit_price": 9900
}
```

### 타입 정의 (`src/app/types/billing.ts`)

```ts
export interface ExtraAccountsPreview {
  direction: 'increase' | 'decrease' | 'noop';
  delta: number;
  /** true = 무료 체험 중 → 증가도 오늘 청구 0원, proration 은 null */
  trial: boolean;                    // ← 추가
  immediate_charge: {
    amount: number;
    currency: 'KRW';
    description: string;
    proration: ExtraProration | null; // 체험 중 증가 · 감소 · noop 에서 null
  };
  effective_at: string | null;
  next_renewal_amount: number;
  unit_price: number;
}
```

### 실행 API 응답 (`POST /billing/extra-accounts/`)

스키마 **변경 없음**. 체험 중에는 이 조합으로 옵니다:

```json
{
  "detail": "추가 IG 계정이 1개로 즉시 반영되었습니다. 무료 체험 중이라 지금 결제되는 금액은 없고, 체험이 끝나는 날 첫 결제부터 합산된 금액이 청구됩니다.",
  "subscription": { "status": "trialing", "extra_ig_accounts": 1, "pending_extra_ig_accounts": null, "plan": { "name": "pro" } },
  "payment": null,
  "next_renewal_amount": 24800,
  "effective_at": null
}
```

`payment: null` 은 **정상**입니다(결제가 아예 일어나지 않았으므로). 200/202/402 분기 로직은
그대로 두면 됩니다 — 체험 중 증가는 항상 **200**입니다.

---

## 3. 프론트 작업 — 필수 2건

### 필수 ①: `QuoteGate` 가 서버의 400 사유를 보여줄 것 ⭐ 최우선

**현재 코드** — `PricingDialog.tsx` 의 견적 조회는 `res.ok` 만 보고 **에러 메시지를 버립니다**:

```ts
// src/app/components/PricingDialog.tsx  (현행)
previewExtraAccounts(proExtra)
  .then((res) => {
    if (cancelled) return;
    if (res.ok && res.data) setExtraPreview(res.data);
    else setExtraPreviewFailed(true);   // ← 사유 소실
  });
```

그래서 **모든 정책 거절이 "일시적 오류"로 보입니다**:
> 결제 금액을 불러오지 못했습니다. / 금액을 확인할 수 없는 상태에서는 결제를 진행하지
> 않습니다. **잠시 후 다시 시도해 주세요.** [다시 시도]

체험 400 은 이번 패치로 사라지지만, **미납·해지예약·일시정지·프로 아님·카드 미등록**은
여전히 400 입니다. 이 화면이 그대로면 그 사용자들도 똑같이 "다시 시도"를 무한 반복합니다.
**이게 이번 사고의 절반입니다** — 백엔드는 정확한 사유를 문자열로 줬는데 화면이 삼켰습니다.

```ts
// 제안
const [extraPreviewError, setExtraPreviewError] = useState('');
previewExtraAccounts(proExtra).then((res) => {
  if (cancelled) return;
  if (res.ok && res.data) { setExtraPreview(res.data); return; }
  setExtraPreviewFailed(true);
  // 400 = 정책 거절 → 서버 문구를 그대로 노출 (재시도 버튼은 숨김)
  // 그 외(5xx/네트워크) = 일시 오류 → 기존 "잠시 후 다시 시도" + [다시 시도]
  setExtraPreviewError(res.status === 400 ? parseApiDetail(res.error, '') : '');
});
```

`QuoteGate` 에 `message?: string` prop 을 받아, 있으면 `failedBody` 대신 그 문구를 띄우고
`[다시 시도]` 를 감춰 주세요. **재시도해도 절대 성공하지 않는 상태**에 재시도 버튼을 두는 것이
사용자를 루프에 가둡니다. `parseApiDetail` 은 `handleApplyProExtra` 에서 이미 쓰고 있는
헬퍼라 그대로 재사용하면 됩니다.

> 백엔드 에러 포맷: `{ "success": false, "error": { "code": 400, "message": "<사유>" } }`

### 필수 ②: `trial: true` 일 때 고지 문구 분기

`buildChangeNotice({ kind: 'extraIncrease' })` 는 지금 이런 문구를 씁니다:

| 키 | 현재 값 | 체험 중에 보이면 |
|---|---|---|
| `…extraIncrease.rowToday` | `오늘 · 비례 결제 · 즉시 사용 가능` | 결제가 없는데 "비례 결제" ❌ |
| `…extraIncrease.rowTodaySub` | `남은 기간만큼만 계산한 금액입니다` | 계산 자체가 없음 ❌ |
| `…extraIncrease.scheduleSummary` | `오늘 {{immediate}} 결제 · {{date}}부터 매월 {{amount}}` | "오늘 0원 결제" ❌ |
| `…extraIncrease.consent` | `오늘 {{immediate}} 결제 및 …에 동의합니다` | "0원 결제에 동의" ❌ |
| `…extraIncrease.cta` | `동의하고 결제 · 추가하기` | 결제 아님 ❌ |

**"오늘 0원 결제"는 틀린 말은 아니지만 사용자를 불안하게 만듭니다** — 무료 체험 중인데
결제 동의를 받는 화면이 뜨니까요. 새 `kind` 를 하나 추가하는 쪽을 권합니다:

```ts
export type ChangeNoticeKind =
  | 'upgrade' | 'downgrade'
  | 'extraIncrease'
  | 'extraIncreaseTrial'   // ← 추가: 체험 중 0원
  | 'extraDecrease';
```

```ts
// PricingDialog.tsx — extraNotice 계산부
kind: extraPreview.trial ? 'extraIncreaseTrial' : 'extraIncrease',
```

권장 i18n (ko):

```json
"extraIncreaseTrial": {
  "title": "추가 인스타그램 계정 늘리기",
  "service": "{{plan}} 플랜 + 추가 계정 (계정당 월 9,900원)",
  "todayNote": "무료 체험 중에는 추가 결제가 없습니다.",
  "rowToday": "오늘 · 결제 없음 · 즉시 사용 가능",
  "rowTodaySub": "무료 체험 중이라 지금 청구되는 금액이 없습니다",
  "rowNext": "체험 종료 · 첫 결제 — 전액",
  "scheduleSummary": "오늘 결제 없음 · {{date}} 첫 결제부터 매월 {{amount}}",
  "renewalImmediate": "지금은 결제되지 않고 계정이 바로 늘어납니다. {{date}} 첫 결제부터 매월 {{amount}}이 청구됩니다.",
  "renewalImmediateNoDate": "지금은 결제되지 않고 계정이 바로 늘어납니다. 체험이 끝나는 날 첫 결제부터 매월 {{amount}}이 청구됩니다.",
  "renewalNote": "체험 중에 추가 계정을 줄이면 첫 결제 금액에 반영됩니다.",
  "consent": "무료 체험 종료일({{date}})부터 정기결제 금액이 월 {{amount}}으로 변경되는 것에 동의합니다",
  "cta": "동의하고 계정 추가"
}
```

`{{date}}` 는 기존 `renewalDateIso` 를 그대로 쓰면 됩니다 —
`next_billing.date → trial_ends_at → 제휴 체험 종료일 → current_period_end` 폴백이 이미
체험 종료일을 가리킵니다. `{{amount}}` 는 `extraPreview.next_renewal_amount`.

⚠️ **동의 문구에서 "오늘 결제"를 반드시 빼 주세요.** 정기결제 금액이 바뀌는 계약 변경이라
고지·동의는 그대로 받아야 하지만(전자상거래법 §13), 없는 결제를 동의받으면 안 됩니다.

---

## 4. 권장 (선택) 2건

### 권장 ①: Meta 픽셀 Purchase 오발동 확인

```ts
trackPurchasePixel(res.data?.payment);   // 체험 중 = null
```

`payment` 가 `null` 일 때 이벤트를 쏘지 않는지 확인해 주세요. 0원 Purchase 가 섞이면 ROAS 가
왜곡됩니다. (감소 예약도 `payment: null` 이라 원래 같은 가드가 필요한 자리입니다.)

### 권장 ②: `PLAN_LIMIT_EXCEEDED` 안내 문구

IG 연동 콜백이 한도 초과일 때 백엔드가 띄우는 페이지 문구입니다(백엔드 소유, 수정 불필요):
> 연결할 수 있는 계정을 모두 사용했어요 … 기존 연결을 해제하거나, **프로 요금제에서 계정을
> 추가하면** 더 연결할 수 있어요.

이제 이 안내가 **체험자에게도 참**이 됐습니다(전에는 거짓이었고, 그게 루프의 원인). 프론트가
이 `error_code` 를 받았을 때 요금제 다이얼로그를 `trigger=multi_ig` 로 바로 열어 주면
사용자가 화면을 찾아 헤매지 않습니다. `onPurchased?.()` 콜백이 이미 "즉시 반영 후 IG 연동
재시도"를 이어가도록 만들어져 있으니 그 흐름을 그대로 쓰면 됩니다.

---

## 5. 회귀 체크리스트

**체험 중 (`subscription.status === 'trialing'`, 프로, 카드 있음)**
- [ ] 요금제 → 프로 카드 스테퍼 `1 → 2` → CTA 클릭 → **고지 시트가 뜬다** (QuoteGate 실패 화면 ❌)
- [ ] 시트에 "오늘 결제 없음" 계열 문구가 보인다 ("오늘 0원 결제" ❌)
- [ ] 확정 → 성공 토스트, 결제 없음, 스테퍼 기준값이 2로 재동기화
- [ ] 설정 → 인스타그램 → 다른 계정 연결하기 → **OAuth 성공하고 연동됨** (PLAN_LIMIT ❌)
- [ ] `GET /billing/my-subscription/` 의 다음 결제 금액이 14,900 → **24,800** 으로 오른다
- [ ] 스테퍼 `2 → 1` (축소) → "다음 갱신부터 반영" 예약 문구, 결제 없음

**유료 (`active`) — 종전 동작 유지 확인**
- [ ] 증가 → 잔여일 비례 금액이 화면에 뜨고 그 금액이 실제 결제된다
- [ ] `proration.remaining_days` 등 계산 내역이 그대로 표시된다

**여전히 400 이어야 하는 상태 — §3 ① 검증**
- [ ] 미납(`past_due`) / 해지예약(`cancelled`) / 일시정지(`paused`) 에서 CTA
      → **서버 사유가 그대로 보이고, [다시 시도] 버튼이 없다**

---

## 6. 백엔드 변경 파일 (참고)

| 파일 | 내용 |
|---|---|
| `apps/billing/toss_flows.py` | `compute_extra_accounts_charge` 에 체험 → 0원 단일 판정(견적·실행 공유) · `change_extra_accounts` / `preview_change_extra_accounts` 의 TRIALING 하드 블록 제거 · 견적에 `trial` 추가 |
| `apps/billing/toss_views.py` | Swagger 문서·예시 갱신 (`/api/docs/` → 결제(토스) 태그) |
| `apps/billing/test_toss_flows.py` | 체험 0원 즉시 반영 / **첫 결제 합산 청구** / 견적 `trial` / 유료 회귀 — 4건 추가 |

체험 종료 시 첫 결제가 추가 계정을 합산하는 경로는 원래부터 정상이었습니다
(`billing.process_due_renewals` → `tasks._renewal_amount_for` 가 pro 일 때
`+ 9,900 × extra_ig_accounts`). 이번에 테스트로 못 박았습니다.

---

## 7. 문의

백엔드 담당자에게 이 문서 경로(`docs/frontend/EXTRA_IG_ACCOUNT_TRIAL_FRONTEND.md`)와 함께
질문 주세요. 관련 문서:

- `docs/frontend/TOSS_BILLING_FRONTEND.md` — 토스 빌링 전체 연동 (추가계정 §3-2)
- `docs/frontend/IG_ACCOUNT_ACTIVATION_FRONTEND.md` — 활성 계정 선택(소프트 비활성)·허용량
- `docs/frontend/PAYMENT_CONSENT_FRONTEND.md` — 결제 전 고지·동의 원칙
