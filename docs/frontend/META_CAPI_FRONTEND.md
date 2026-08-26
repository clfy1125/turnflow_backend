# Meta 전환 API(CAPI) — 서버 구현 완료 + 프론트 요청 1건

**작성** 2026-08-26 · **대상** 서비스 프론트(TurnflowLink)
**백엔드 상태** ✅ 구현·배포 완료 (**플래그 OFF — 토큰 대기 중**)

---

## 1. 한 장 요약

서버에서 Meta 로 전환 이벤트를 직접 보내는 CAPI 를 구현했습니다.
**프론트에 필요한 건 딱 하나 — `StartTrial` 픽셀이 안 쏘이고 있습니다.**

| 이벤트 | 브라우저 픽셀 | 서버 CAPI | `event_id` |
|---|---|---|---|
| CompleteRegistration | ✅ 발사 중 | ✅ 구현 완료 | `String(user.id)` |
| Purchase | ✅ 발사 중 | ✅ 구현 완료 | `String(payment.id)` |
| **StartTrial** | ❌ **미발사** ← 요청 | ✅ 구현 완료 | **`String(subscription.id)`** |

---

## 2. ⭐ 프론트 요청 — `StartTrial` 픽셀 호출 추가

### 현황

배포 번들(`index-CSK7HEJl.js`)을 확인해 보니 **함수는 있는데 부르는 곳이 없습니다.**

```js
// 정의는 있음
function Q9(e){ const t = e ? String(e) : undefined;
  ja("StartTrial", {value:0, currency:"KRW"}, t ? {eventID:t} : undefined) }

// → 이 함수를 호출하는 코드가 번들 전체에 0곳
```

`ja("...")` 로 실제 발사되는 이벤트는 `PageView` · `CompleteRegistration` · `Purchase` 3개뿐입니다.

### 왜 중요한가

**우리 전환 구조상 무료체험 시작이 사실상 주 전환입니다.**

| | 현재 |
|---|---|
| 무료체험 중 | **52명** |
| 실결제 | **5명** |

지금 Meta 는 "가입"만 보고 최적화하고 있습니다. 가장 값진 신호(체험 시작)가 **0%** 전달되는 중입니다. CAPI 가 신호를 10~30% 더 잘 전달하는 것이라면, 이건 **가장 중요한 신호가 아예 없는 것**이라 우선순위가 더 높습니다.

### 무엇을 하면 되나

**카드 등록으로 무료체험이 시작된 시점** — `POST /api/v1/billing/toss/confirm/` 이 성공하고 응답의 시나리오가 체험일 때 — 아래를 호출해 주세요.

```js
// 응답 예시
// { subscription: { id: 123, status: "trialing", ... }, first_charge_at: "...", detail: "..." }

const data = await res.json();
if (data.subscription?.status === "trialing") {
  fbq('track', 'StartTrial',
      { value: 0, currency: 'KRW' },
      { eventID: String(data.subscription.id) });   // ← 서버 CAPI 와 같은 값
}
```

### ⚠️ `event_id` 는 반드시 `subscription.id` 입니다

`user.id` 를 쓰면 안 됩니다. **한 사람이 체험을 두 번 시작할 수 있어서**, `user.id` 를 쓰면 두 번째 체험이 첫 번째와 같은 id 가 되고 Meta 가 **중복으로 판단해 지웁니다.**

서버는 이미 `str(subscription.id)` 로 보내도록 구현돼 있습니다
([`apps/analytics/conversions.py`](../../apps/analytics/conversions.py) `track_trial_started`).

### 응답에 `subscription.id` 가 내려가나요?

내려갑니다. 안 보이면 알려주세요 — 시리얼라이저에 추가하겠습니다.

---

## 3. 확인만 부탁드립니다 (코드 수정 없음)

### 3-1. `CompleteRegistration` 의 `eventID`

번들에서 이렇게 읽었습니다:

```js
const s = e?.id != null ? String(e.id) : undefined;
ja("CompleteRegistration", undefined, s ? {eventID: s} : undefined);
```

→ **`String(user.id)`** 로 이해했고, 서버도 같은 값을 씁니다. **맞습니까?**

다르면 알려주세요 — 어긋나면 가입 전환이 **2배로 집계**됩니다.

### 3-2. `Purchase` 의 `eventID`

```js
ja("Purchase", {value: e.amount, currency:"KRW"}, {eventID: e.id});
```

→ `e.id` 가 **`PaymentHistory.id`(UUID)** 맞습니까? 서버도 그 값을 씁니다.

### 3-3. `is_new_user` 반영

어제 배포하신 코드에서 확인했습니다:

```js
d.is_new_user !== false && zr(d.user, {definite: d.is_new_user === true})
```

정상입니다. `date_joined` 휴리스틱이 제거돼서 가입 전환의 누락·중복이 사라집니다. 👍

---

## 4. 백엔드가 한 것 (참고)

### 4-1. 전송 구조

```
가입/체험/결제 발생
  → apps/analytics/conversions.py  (event_id 규약 단일 소스)
  → Celery 태스크 (analytics.send_meta_capi_event)
  → apps/analytics/meta_capi.py → graph.facebook.com/v23.0/{dataset}/events
```

**Celery 로 비동기 전송**합니다 — 가입·결제 응답이 Meta 응답을 기다리지 않습니다.
계측이 죽어도 가입·결제는 정상 동작합니다(모든 함수가 예외를 삼킵니다).

### 4-2. `fbclid` / `_fbp` / `_fbc` 저장 시작

**프론트가 이미 보내고 있었는데 백엔드가 받아서 버리고 있었습니다.** 이제 저장합니다.

```jsonc
// POST /api/v1/auth/register/ · /auth/google/ 의 attribution 객체
{
  "utm_source": "meta", "utm_medium": "cpc", "utm_campaign": "...",
  "fbclid": "...", "fbp": "fb.1.<ts>.<random>", "fbc": "fb.1.<ts>.<fbclid>"
}
```

길이 상한은 프론트 절단 규칙과 동일하게 맞췄습니다 — `fbclid` 500 / `fbp` 200 / `fbc` 300.
**한쪽만 짧으면 값이 잘려 Meta 매칭이 조용히 실패합니다.** 프론트에서 이 값을 바꾸면 알려주세요.

### 4-3. 개인정보 처리

| 항목 | 처리 |
|---|---|
| 이메일 · 회원ID | **SHA-256 해시** (Meta 규격: 소문자·공백제거 후) |
| `fbc` · `fbp` · IP · UA | **평문** (해시하면 Meta 가 매칭에 못 씁니다) |
| 원본 IP | **DB 에 저장 안 함** — 요청에서 뽑아 전송 인자로만 쓰고 버립니다 |

### 4-4. 월 갱신 결제는 보내지 않습니다

첫 결제만 `Purchase` 를 보냅니다. 갱신까지 보내면 같은 고객을 매달 다시 세어 **ROAS 가 부풀려지고**, 갱신은 브라우저가 없어 IP/UA 매칭 품질도 낮습니다.

---

## 5. 아직 켜지지 않았습니다

`META_CAPI_ENABLED = False` 상태로 배포했습니다. **토큰이 없으면 어차피 no-op** 이지만, 플래그를 따로 둔 이유가 있습니다:

> **프론트 `event_id` 배포보다 서버가 먼저 켜지면 그 기간 전환이 2배로 집계됩니다.**
> 프론트가 먼저 배포된 지금은 안전하지만, 켜는 시점은 사람이 정하도록 남겨뒀습니다.

**대행사에 요청 중**: 액세스 토큰 재발급 (기존 토큰이 평문으로 메신저에 노출됨).

토큰이 도착하면 순서는 이렇습니다:

1. 환경변수 주입 (`META_CAPI_ACCESS_TOKEN`, `META_CAPI_DATASET_ID`)
2. `META_CAPI_TEST_EVENT_CODE=TEST67446` 로 먼저 켬 → **[이벤트 테스트] 탭에만** 뜨고 실집계에는 안 들어감
3. 대행사가 "서버(Server)" 출처로 3개 이벤트 확인
4. 테스트 코드 비우고 `META_CAPI_ENABLED=True` → 실집계 시작

---

## 6. 체크리스트

**프론트**
- [ ] **§2 `StartTrial` 픽셀 호출 추가** (`eventID = String(subscription.id)`)
- [ ] §3-1 `CompleteRegistration` 의 eventID 가 `String(user.id)` 맞는지 회신
- [ ] §3-2 `Purchase` 의 eventID 가 `payment.id`(UUID) 맞는지 회신

**백엔드 (완료)**
- [x] CAPI 클라이언트 (해싱·재시도·`test_event_code`·토큰 본문 전송)
- [x] 이벤트 3종 발사 지점 (가입 2경로 / 체험 시작 / 첫 결제)
- [x] `fbclid`/`fbp`/`fbc` 저장 (마이그 `analytics.0006`)
- [x] Celery 비동기 + 실패 격리

**백엔드 (대기)**
- [ ] 토큰 수령 → `TEST67446` 검증 → 실집계 전환

---

---

# 회신 반영 (2026-08-26 저녁) — 질문 2건 답변 + 백엔드 정정 1건

회신 감사합니다. **§2 StartTrial 지적이 맞고 제가 틀렸습니다.** 그리고 그쪽 질문 덕분에
서버에서 **더 큰 결함**을 찾았습니다.

## A. 정정 — StartTrial 은 이미 발사 중입니다

`index-CSK7HEJl.js` 만 grep 하고 *"호출부 0곳"* 이라고 단정한 것이 제 오류였습니다.
지연 로드 청크를 확인했습니다:

```bash
$ grep -o 'TossBillingSuccessPage-[A-Za-z0-9_-]*\.js' index-CSK7HEJl.js
TossBillingSuccessPage-BhDNdWsS.js

$ grep -oE '.{80}scenario==="trial".{140}' TossBillingSuccessPage-BhDNdWsS.js
… t.data.scenario==="trial" && Y((l=t.data.subscription)==null?void 0:l.id) …
```

확인했습니다. **§2 요청은 취소합니다 — 프론트 작업 없습니다.**

> 교훈: Vite/Rollup SPA 는 라우트가 코드 스플리팅되므로 **엔트리 번들만 보면 안 됩니다.**
> 앞으로 번들 검증할 때 청크까지 따라가겠습니다.

## B. 답변 ① `track_trial_started` 발사 시점 → **프론트와 완전히 같은 지점**

```python
# apps/billing/toss_flows.py — confirm_billing
if scenario == "trial":
    ...
    track_trial_started(sub, request=request)
```

`POST /billing/toss/confirm/` 의 `scenario == "trial"` 분기입니다. **프론트의
`t.data.scenario==="trial"` 와 같은 조건, 같은 순간**입니다.

| scenario | 브라우저 | 서버 | 결과 |
|---|---|---|---|
| `trial` | ✅ | ✅ | **정상 짝 — 중복은 event_id 로 제거** |
| `attach_only` | ❌ | ❌ | **양쪽 다 안 쏨** — 커버리지 공백 없음 |
| `charge_now` | ❌ | ❌ | Purchase 로 처리 |
| `card_change` | ❌ | ❌ | — |

**`attach_only` 는 서버도 안 쏩니다.** 우려하신 "서버만 집계" 상황은 발생하지 않습니다.

**무카드 체험 비중도 재봤습니다** (prod 실측):

| | |
|---|---|
| 체험 중 | **55명** |
| 카드 있음 (= `trial` 경로, 양쪽 커버) | **52명 (95%)** |
| 카드 없음 (어드민 수동 부여 — `confirm` 을 안 지남) | **3명 (5%)** |

무카드 3명은 광고 전환이 아니라 어드민이 수동 부여한 계정이라 **양쪽 다 안 쏘는 게 맞습니다.**
→ `attach_only` 추가 발사는 불필요합니다.

## C. 답변 ② 갱신 결제 구분 → **필드 추가했습니다. 그리고 제 구현이 틀렸었습니다**

### 먼저, 제가 틀린 부분

§4-4 에서 *"첫 결제만 보낸다"* 고 썼는데, **구현은 `charge_now`(즉시 과금) 경로에서만
발사**하고 있었습니다. 그런데 prod 를 보니:

```
★첫결제  user=54   renewal   14900원  tfsub-67142901e1-20260809-a0
★첫결제  user=70   renewal   15900원  tfsub-039a09a58a-20260818-a0
★첫결제  user=73   init       5900원  tfsub-cf9559d1bf-init-1ce20228
  갱신    user=73   up         9994원  tfsub-cf9559d1bf-up-pro-0-20260819
  갱신    user=73   renewal   14900원  tfsub-cf9559d1bf-20260819-a0
```

**체험으로 시작한 사용자의 첫 유료 결제는 `init` 이 아니라 '갱신' 주문입니다.**
카드 등록 시엔 0원이고, 실제 첫 과금은 체험 종료 후 `process_due_renewals` 가 하기 때문입니다.

→ 제 구현은 **가장 중요한 전환인 체험→유료를 통째로 놓치고 있었습니다.**
(user 54·70 이 정확히 그 케이스 — 전체 유료 고객 5명 중 2명)

### 고친 방식 — 호출 지점이 아니라 "첫 결제인가"로 판정

```python
# 발사 지점을 첫 결제·갱신 양쪽에 두고, 판정은 한 곳에 맡긴다
def track_purchase(payment, request=None):
    if not payment.is_initial_payment:   # ← 단일 소스
        return
    ...
```

`is_initial_payment` = **그 사용자의 가장 이른 유료 결제**(`status=paid` & `amount>0`).
주문번호 패턴을 안 봅니다. 업그레이드 비례배분·2회차 이후 갱신·0원 결제는 자연히 걸러집니다.

### 프론트가 쓸 필드 — `is_initial_payment`

**요청하신 플래그를 결제 내역 응답에 추가했습니다.**

```jsonc
// GET /api/v1/billing/payments/ (결제 내역)
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "amount": 14900,
  "status": "paid",
  "is_initial_payment": true,     // ← 신규
  "toss_order_id": "tfsub-...-20260809-a0",
  "paid_at": "2026-08-09T14:01:00+09:00"
}
```

```js
// 프론트 권장 변경
if (p.status === 'paid' && p.amount > 0 && p.is_initial_payment) {
  fbq('track', 'Purchase', { value: p.amount, currency: 'KRW' }, { eventID: p.id });
}
```

⚠️ **`toss_order_id` 에 `-init-` 이 있는지로 판별하지 마세요.** 위 표대로 체험자는 그 패턴이
없습니다. 서버가 계산한 `is_initial_payment` 를 그대로 쓰시면 서버 CAPI 와 정확히 일치합니다.

> N+1 걱정 없이 쓰셔도 됩니다 — 목록 응답에서 사용자당 1쿼리만 돌게 메모해 뒀습니다.

### 부수 효과 — 갱신 Purchase 지연 발사 문제도 해결됩니다

회신에 *"서버 cron 자동결제는 다음 접속 시 결제내역을 훑어 후행 발사"* 라고 하셨는데,
그 경로엔 다른 함정도 있습니다: **Meta 는 `event_time` 이 7일보다 오래되면 거부**합니다.
브라우저 픽셀은 발사 시각으로 스탬프되니 10일 전 갱신이 '오늘 전환'으로 잡힙니다.
첫 결제만 쏘도록 맞추면 이 문제도 같이 사라집니다.

## D. 나머지 회신 항목 — 전부 확인했습니다

| 항목 | 결과 |
|---|---|
| §3-1 `CompleteRegistration` = `String(user.id)` | ✅ 서버 동일 |
| §3-2 `Purchase` = `payment.id`(UUID) | ✅ 서버 동일 |
| §3-3 `is_new_user` 폴백 유지 | ✅ 좋습니다 — 구 백엔드 대비가 맞습니다 |
| §4-2 길이 상한 500/200/300 | ✅ 동일 |
| `status==='paid' && amount>0` 만 발사 | ✅ 서버도 같은 조건 |
| `value` 를 서버 `payment.amount` 로 | ✅ 정확합니다 (할인·그랜드파더링·비례배분 때문에 필수) |
| `tf_fbpx_reg` / `tf_fbpx_paid_ids` 중복 방지 | 👍 서버는 `event_id` 로 Meta 가 합치므로 이중 방어가 됩니다 |

## E. 현재 상태 — 켜져 있습니다 (검증 모드)

말씀대로 프론트가 먼저 배포돼 있어 순서 위험이 없으므로 **켰습니다.**

```
META_CAPI_ENABLED         = True
META_CAPI_DATASET_ID      = 1057766930068893
META_CAPI_TEST_EVENT_CODE = TEST67446    ← [이벤트 테스트] 탭에만 감
```

3종 전송해서 Meta 응답 `events_received: 1`, `messages: []` 확인했습니다.
Celery 끝단(0.39초)까지 성공했습니다.

**대행사가 [이벤트 테스트] 탭에서 "서버(Server)" 출처를 확인하면 테스트 코드를 비우고
실집계로 전환합니다.**

## F. 프론트 남은 작업 — 1건

- [ ] **Purchase 발사 조건에 `is_initial_payment` 추가** (§C)
      → 갱신분이 브라우저만 집계돼 ROAS 가 부풀는 문제가 사라집니다

그 외에는 없습니다. §2 는 취소, §3 은 전부 일치 확인했습니다.

---

관련 문서: [UTM_ATTRIBUTION_FIX.md](./UTM_ATTRIBUTION_FIX.md) (대시보드 귀속 — **별건, 이미 완료**)
