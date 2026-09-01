# Meta CAPI — 프론트 요청 (최종 · 1건만 남았습니다)

**작성** 2026-08-26 · **대상** 서비스 프론트(TurnflowLink)
**이 문서는 이전 버전을 대체합니다.** 회신 주신 내용 전부 반영해서 **남은 것만** 남겼습니다.

> 이전 버전에서 요청했던 **StartTrial 픽셀 추가는 취소**합니다. 이미 발사되고 있었고,
> 제가 `index-*.js` 만 grep 하고 "호출부 0곳"이라 단정한 오류였습니다. 라우트 청크
> (`TossBillingSuccessPage-*.js`)에서 확인했습니다. 지적 감사합니다.

---

## 요청 — Purchase 발사 조건에 `is_initial_payment` 추가 (이것뿐입니다)

```diff
- if (p.status === 'paid' && p.amount > 0) {
+ if (p.status === 'paid' && p.amount > 0 && p.is_initial_payment) {
    fbq('track', 'Purchase', { value: p.amount, currency: 'KRW' }, { eventID: p.id });
  }
```

`is_initial_payment` 는 **오늘 배포한 신규 필드**입니다. `GET /api/v1/billing/payments/history/` 응답에 실려 갑니다.

> ⚠️ 초판에 `GET /api/v1/billing/payments/` 로 적었던 것은 **오기**입니다(그 경로는 404).
> 실제 경로는 `/billing/payments/history/` (`PaymentHistoryView`) 하나뿐이고, 프론트가
> 이미 쓰고 있는 그 경로가 맞습니다. 2026-08-26 프론트 지적으로 정정.

```jsonc
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "amount": 14900,
  "status": "paid",
  "is_initial_payment": true,          // ← 신규. 이 값이 true 인 건만 Purchase 발사
  "toss_order_id": "tfsub-67142901e1-20260809-a0",
  "paid_at": "2026-08-09T14:01:00+09:00"
}
```

### 왜 필요한가

현재 프론트는 결제내역에 잡히는 **모든** 유료 결제에 Purchase 를 쏘고 서버는 **첫 결제만** 보냅니다. 그래서 **갱신분이 브라우저만 집계되어 ROAS 가 부풀려집니다** — 회신에서 직접 짚어주신 그 문제입니다.

기준을 서버와 맞추면 해결됩니다. 광고 최적화가 봐야 하는 신호는 *"이 광고가 유료 고객을 만들었나"* 라서 **첫 결제 1회**가 맞고, 매달 갱신을 세면 같은 고객을 반복 계상합니다.

### ⚠️ `toss_order_id` 의 `-init-` 패턴으로 판별하면 안 됩니다

이게 이 문서의 핵심입니다. prod 실데이터를 보면:

```
★첫결제   user=54    14900원   tfsub-67142901e1-20260809-a0     ← 갱신 패턴인데 첫 결제
★첫결제   user=70    15900원   tfsub-039a09a58a-20260818-a0     ← 같음
★첫결제   user=73     5900원   tfsub-cf9559d1bf-init-1ce20228
  갱신     user=73     9994원   tfsub-cf9559d1bf-up-pro-0-...    ← 업그레이드 비례배분
  갱신     user=73    14900원   tfsub-cf9559d1bf-20260819-a0
```

**체험으로 시작한 사용자는 `-init-` 주문이 아예 없습니다.** 카드 등록 시점엔 과금이 0원이고, 실제 첫 과금은 체험 종료 후 `process_due_renewals` 가 만드는 **갱신 주문**으로 들어옵니다.

유료 고객 5명 중 **2명(54·70)이 이 케이스**라, 주문번호로 판별하면 가장 중요한 전환인 **체험→유료를 40% 놓칩니다.**

> 참고: 이 함정에 **서버가 먼저 걸렸습니다.** 초판은 `charge_now`(즉시 과금) 경로에서만
> Purchase 를 발사해서 체험→유료 전환이 통째로 빠져 있었습니다. 그래서 판정을
> "그 사용자의 가장 이른 `paid & amount>0` 결제인가" 로 바꾸고 발사 지점을 첫 결제·갱신
> 양쪽에 뒀습니다. `is_initial_payment` 는 서버가 쓰는 **바로 그 판정값**입니다.

### 성능

목록 응답에서 행마다 쿼리하지 않습니다 — 사용자당 1쿼리만 돌게 메모해 뒀으니 그냥 쓰셔도 됩니다.

### 부수 효과 하나

회신에 *"서버 cron 자동결제는 다음 접속 시 결제내역을 훑어 후행 발사"* 라고 하셨는데, 그 경로엔 다른 함정도 있습니다 — **Meta 는 `event_time` 이 7일보다 오래되면 이벤트를 거부**합니다. 브라우저 픽셀은 발사 시각으로 스탬프되니 10일 전 갱신이 '오늘 전환'으로 잡힙니다. 첫 결제만 쏘도록 맞추면 이것도 함께 사라집니다.

---

## 확인 완료 — 추가 작업 없습니다

회신 주신 내용 전부 서버와 대조했고 **모두 일치**합니다. 다시 볼 필요 없습니다.

| 항목 | 결과 |
|---|---|
| `StartTrial` 발사 (`scenario === 'trial'`, `eventID = String(subscription.id)`) | ✅ 서버와 **동일 조건·동일 순간** |
| `CompleteRegistration` = `String(user.id)` | ✅ 일치 |
| `Purchase` = `payment.id` (UUID) | ✅ 일치 |
| `is_new_user` 적용 + 구 백엔드 폴백 유지 | ✅ 적절합니다 |
| `fbclid`/`fbp`/`fbc` 길이 상한 500/200/300 | ✅ 서버 동일. **저장 시작했습니다**(전엔 받아서 버렸음) |
| `value` 를 서버 `payment.amount` 로 | ✅ 정확합니다 (할인·그랜드파더링·비례배분 때문에 필수) |
| `tf_fbpx_reg` / `tf_fbpx_paid_ids` 중복 방지 | 👍 서버는 `event_id` 로 Meta 가 합치므로 이중 방어 |

### `attach_only` — 질문 주신 것, 확인했습니다

**서버도 안 쏩니다.** `track_trial_started` 는 `confirm_billing` 의 `scenario == "trial"` 분기에만 있어서 프론트와 완전히 같습니다. → *"서버만 집계"* 상황은 발생하지 않습니다.

무카드 체험 비중도 재봤습니다 (prod 실측):

| | |
|---|---|
| 체험 중 | **55명** |
| 카드 있음 (= `trial` 경로, 양쪽 커버) | **52명 (95%)** |
| 카드 없음 (어드민 수동 부여 — `confirm` 을 안 지남) | **3명 (5%)** |

무카드 3명은 광고 전환이 아니라 어드민 수동 부여라 **양쪽 다 안 쏘는 게 맞습니다.**
→ `attach_only` 추가 발사는 불필요합니다.

---

## 서버 현재 상태

```
META_CAPI_ENABLED         = True
META_CAPI_DATASET_ID      = 1057766930068893
META_CAPI_TEST_EVENT_CODE = TEST67446     ← 검증 모드 ([이벤트 테스트] 탭에만 감)
```

3종 전송해서 Meta 응답 `events_received: 1`, `messages: []` 확인. Celery 끝단(0.39초) 성공.

**남은 단계**: 대행사가 [이벤트 테스트] 탭에서 "서버(Server)" 출처를 확인 → 테스트 코드 제거 → 실집계 전환.

---

## 체크리스트

**프론트**
- [ ] Purchase 발사 조건에 `is_initial_payment` 추가 ← **이것 하나**

**백엔드 (완료)**
- [x] CAPI 전송 3종 (가입 2경로 / 체험 시작 / 첫 유료 결제)
- [x] Purchase 를 '첫 유료 결제' 기준으로 재정의 — 체험→유료 누락 수정
- [x] `is_initial_payment` 응답 필드
- [x] `fbclid`/`fbp`/`fbc` 저장
- [x] 토큰 주입 + `TEST67446` 검증 전송 성공

**백엔드 (대기)**
- [ ] 대행사 확인 → 테스트 코드 제거 → 실집계 전환

---

별건으로 [UTM_ATTRIBUTION_FIX.md](./UTM_ATTRIBUTION_FIX.md) 에 **iOS 인앱 브라우저 안내 보강(P2)** 한 건이 남아 있습니다. 급하지 않습니다 (인앱 트래픽 Android 17 / iOS 7).
