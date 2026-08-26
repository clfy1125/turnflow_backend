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

관련 문서: [UTM_ATTRIBUTION_FIX.md](./UTM_ATTRIBUTION_FIX.md) (대시보드 귀속 — **별건, 이미 완료**)
