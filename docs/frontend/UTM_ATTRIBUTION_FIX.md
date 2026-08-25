# 광고 유입 귀속 — 프론트 작업 요청 (v2 · 최종본)

**작성** 2026-08-25 · **대상** 서비스 프론트(TurnflowLink) + 랜딩(turnflow_landing)
**상태** 백엔드 4건 배포 완료 · 프론트 6건 대기

> **v1 을 이미 보셨다면 이 문서로 대체해 주세요.** 회신 8건과 실기기 확인 결과를 반영해
> **우선순위가 바뀌었고 v1 의 두 항목을 정정·철회**했습니다. 변경 이력은 맨 아래 §8.

---

## 1. 한 장 요약

마케팅 대행사가 *"메타 광고 방문 123명인데 가입 0명"* 으로 신고 → prod 전수 조사 결과
**신고된 숫자는 사실**이었고(집계 버그 아님), 대신 **계측 결함 세 개**가 따로 나왔습니다.

| 항목 | 담당 | 상태 |
|---|---|---|
| 인증 리다이렉트가 '자연 검색'으로 둔갑 | 백엔드 | ✅ 배포 `a167c12` |
| 과거 111건 채널 교정 | 백엔드 | ✅ 완료 |
| 광고 소재별 `utm_content` 링크 매칭 | 백엔드 | ✅ 배포 (링크 행 방문 27 → 122) |
| `is_new_user` 응답 필드 | 백엔드 | ✅ 배포 `301632e` |
| **CTA 가 UTM 을 URL 로 유지** | **프론트** | ⬜ **P0** |
| `captureAttribution` 덮어쓰기 가드 | 프론트 | ⬜ P1 |
| `is_new_user` 로 전환 이벤트 분기 | 프론트 | ⬜ P1 |
| `eventID` — StartTrial 기준 id 합의 | 프론트 | ⬜ P1 |
| `fbclid`/`_fbp`/`_fbc` 수집 | 프론트 | ⬜ P2 |
| iOS 인앱 안내 보강 | 프론트 | ⬜ P2 |
| Meta CAPI 서버 전송 | 백엔드 | ⛔ **토큰 재발급 대기** |

---

## 2. 확정된 사실 (prod 실측 + 배포 번들 분석)

### 2-1. 웹 구글 로그인은 전체 페이지 리다이렉트다

배포 번들에서 직접 확인:

```js
window.location.replace(
  "https://accounts.google.com/o/oauth2/v2/auth?…&redirect_uri=<origin>/login&response_type=id_token"
)
```

`response_type=id_token` 은 맞지만 **전달이 redirect** 입니다. (네이티브 앱은 `GoogleAuth`
플러그인이라 페이지를 안 떠나는 게 맞습니다 — **웹만** 해당.)

### 2-2. 그래서 저장된 UTM 이 덮어써진다

```js
function captureAttribution(){
  const utm = readUtmFromQuery();
  const referrer = getReferrer();          // host !== 자기 host 면 채택
  if (!hasSignal(utm, referrer)) return;   // 둘 다 없을 때만 skip
  localStorage.setItem("tf_attribution", JSON.stringify({ ...utm, referrer, landing_path }));
}                                          // ← 조건 없는 덮어쓰기
```

`/login` 복귀 시 `referrer = "https://accounts.google.com/"` 이 되어 **UTM 이 지워집니다.**

| 확인 | 수치 |
|---|---|
| `SignupAttribution` 전체 | 160건 |
| `referrer="https://accounts.google.com/"` + `landing_path="/login"` | **105건 (66%)** |
| 그 105건의 서버 채널 판정 | 전부 `search_organic` |
| "검색 유입 가입 77건" 중 실제 검색 | **약 5건** |
| UTM 달고 방문 → **구글**로 가입 | 5명 → **전원 UTM 소실** |
| 같은 UTM → **이메일**로 가입 | 2명 → **UTM 보존** ← 경로가 갈린다는 증거 |

### 2-3. 광고 트래픽의 77%가 인앱 브라우저다

`POST /track/visit` 액세스 로그의 User-Agent 실측 (광고 집행 중 3시간, 31건):

| 브라우저 | 요청 | 비율 |
|---|---|---|
| 인스타그램 인앱 | 19 | 61% |
| 페이스북 인앱 | 5 | 16% |
| 일반 브라우저 | 7 | 23% |

인앱 내역: **Android 17 / iOS 7**.

### 2-4. 인앱 안내 모달은 정상 동작한다 (v1 정정)

실기기(Android·인스타 인앱) 확인 결과 모달이 정상적으로 뜨고
**`기본 브라우저에서 열기` · `링크 복사하기` · `이메일로 계속하기`** 세 갈래를 모두 제공합니다.
막다른 길이 아닙니다.

> v1 에서 *"주력 가입 수단이 통째로 막혀 있다"* 고 쓴 것은 **과장이었습니다. 정정합니다.**
> 막힌 게 아니라 한 단계 더 거치는 것이고, 모달 자체는 잘 만들어져 있습니다.

**그리고 탈출 로직도 이미 올바릅니다:**

```js
const l = window.location.origin + window.location.pathname + window.location.search;
const c = () => { qw(l) };        // "기본 브라우저에서 열기" (android: intent://)
const d = async () => { Ww(l) };  // "링크 복사하기"
```

**`search` 를 포함해 넘깁니다.** UTM 이 URL 에 남아 있기만 하면 외부 브라우저까지 그대로
따라가고, 거기서 비콘이 돌아 `tf_vid` 와 UTM 이 새로 잡힙니다.

### 2-5. 파손 지점은 딱 하나 — CTA 가 쿼리를 버린다

```
① 인스타 인앱: turnflow.link/?utm_source=meta…   ← UTM 있음, tf_vid 생성됨
② CTA 클릭 → navigate('/home')                  ← ★ URL 에서 UTM 사라짐  ← 유일한 파손
③ 구글 버튼 → 인앱 안내 모달 (정상)
④ "기본 브라우저에서 열기" → 넘기는 URL 에 UTM 없음 (②때문)
⑤ 크롬: tf_vid 없음 · tf_attribution 없음 → 구글 가입 성공
⑥ 서버 기록: visitor_id 없음 · UTM 없음 · referrer=accounts.google.com → 채널 "direct"
```

**⑥은 8/24~25 에 실제로 관측된 10건의 패턴과 정확히 일치합니다.**
**②만 고치면 ④⑤⑥이 자동으로 정상화됩니다.**

---

## 3. 프론트 작업

### 3-1. ⭐ [P0] CTA 가 UTM 을 URL 로 넘기게

```diff
- navigate('/home')
+ navigate({ pathname: '/home', search: window.location.search })
```

랜딩 내부 이동과 앱 진입 **모든 경로**에서 쿼리스트링을 보존해 주세요.

지금은 UTM 이 localStorage **단일 의존**이라, 브라우저가 바뀌는 순간(= 광고 유입의 주 경로)
귀속이 100% 사라집니다. URL 에 남아 있으면 **인앱 → 외부 브라우저 탈출 경로 전체가 복구**됩니다.

**검증**: 인스타 인앱에서 `turnflow.link/?utm_source=meta&utm_medium=cpc&utm_campaign=test`
→ CTA → 구글 버튼 → `기본 브라우저에서 열기` → **크롬 주소창에 `utm_source=meta` 가 보이면 성공.**

---

### 3-2. [P1] `captureAttribution` 덮어쓰기 가드 2종

```ts
// ① 인증 제공자 왕복은 유입 신호가 아니다 (백엔드 AUTH_REDIRECT_DOMAINS 와 동일 목록)
const AUTH_REDIRECT_HOSTS = new Set([
  'accounts.google.com', 'appleid.apple.com', 'nid.naver.com',
  'kauth.kakao.com', 'accounts.kakao.com',
  'login.microsoftonline.com', 'login.live.com',
]);

function readReferrer(): string {
  try {
    const raw = document.referrer;
    if (!raw) return '';
    const host = new URL(raw).host.replace(/^www\./, '');
    if (!host || host === window.location.host.replace(/^www\./, '')) return '';
    if (AUTH_REDIRECT_HOSTS.has(host)) return '';        // ← 추가
    return truncate(raw, LIMITS.referrer);
  } catch { return ''; }
}

export function captureAttribution(): void {
  if (!isBrowser()) return;
  const utm = readUtmFromQuery();
  const referrer = readReferrer();
  if (!hasSignal(utm, referrer)) return;

  // ② 리퍼러만 있는 터치는 UTM 이 담긴 저장값을 덮어쓰지 못한다
  const stored = readStoredAttribution();               // 만료 안 된 것만
  const incomingHasUtm = !!(utm.utm_source || utm.utm_medium || utm.utm_campaign || utm.utm_content);
  const storedHasUtm = !!(stored && (stored.utm_source || stored.utm_medium ||
                                     stored.utm_campaign || stored.utm_content));
  if (storedHasUtm && !incomingHasUtm) return;          // ← 추가

  writeStoredAttribution({ ...utm, referrer: referrer || undefined,
                           landing_path: currentPath(), expiresAt: Date.now() + THIRTY_DAYS });
}
```

**왜 ②도 필요한가**: ①만 넣으면 구글은 막히지만 다른 외부 왕복이 남습니다. prod 에 이미
`referrer="https://docs.turnflow.link/"` 로 덮어써진 행이 있고, 토스 결제창 복귀 등 앞으로
늘어날 경로도 같은 함정입니다. **"UTM 터치가 리퍼러 터치보다 세다"** 는 규칙 자체를 넣는 편이
목록 관리보다 안전합니다.

**건드리지 마세요**: `tf_vid` 는 지금 로직 그대로 — 방문↔가입 조인의 유일한 키입니다.

---

### 3-3. [P1] `is_new_user` 로 전환 이벤트 분기 (백엔드 배포 완료)

회신 7번의 *"구글 가입은 `date_joined` 10분 휴리스틱이라 일부 누락 가능"* 은 서버가 풀어야
할 문제였습니다. `POST /api/v1/auth/google/` 응답에 필드를 추가했습니다:

```jsonc
{
  "user": { "id": 1, "email": "…", "date_joined": "…" },
  "is_new_user": true,        // ← 이번 요청으로 계정이 생성됨
  "tokens": { "access": "…", "refresh": "…" }
}
```

```js
const data = await res.json();
if (data.is_new_user) {
  fbq('track', 'CompleteRegistration', {}, { eventID: String(data.user.id) });
}
```

이 값은 서버가 **attribution 을 저장하는 조건과 완전히 같은 분기**(`if created:`)입니다 —
`is_new_user === true` 인 요청과 가입 귀속 행이 1:1 대응합니다. 추정할 이유가 없어졌습니다.

> `/auth/register/` 는 성공하면 **항상 신규**라(201) 이 필드가 없습니다.

---

### 3-4. [P1] `eventID` — StartTrial 기준 합의 + **CompleteRegistration 값 확인**

회신 7번에 *"어제 event_id 도 추가 배포됨"* 이라고 하셨는데, **어떤 값을 쓰셨는지 알려주세요.**
백엔드 CAPI 가 **똑같은 값**을 써야 중복 제거가 됩니다 — 다르면 전환이 2배로 집계됩니다.

| 이벤트 | 백엔드 제안 | 상태 |
|---|---|---|
| `CompleteRegistration` | `String(user.id)` | ⬜ **프론트가 쓴 값 확인 필요** |
| `StartTrial` | `String(subscription.id)` | ⬜ 합의 필요 (사용자당 여러 번 가능해 user.id 부적합) |
| `Purchase` | `payment.id` | ✅ 기존 유지 |

**배포 순서 — 프론트가 먼저여야 합니다:**

| 순서 | 결과 |
|---|---|
| 프론트(eventID) → 백엔드(CAPI) | ✅ **안전.** 서버 이벤트가 없으니 중복될 대상이 없음 |
| 백엔드(CAPI) → 프론트 | ❌ 그 기간 2배 집계 |

---

### 3-5. [P2] `fbclid` / `_fbp` / `_fbc` 수집

사이트는 `turnflow.link`, API 는 `turnflow-api.clfy.ai.kr` 로 **등록가능도메인이 달라** 쿠키가
붙지 않습니다. `_fbc` 는 애초에 `fbclid` URL 파라미터에서 브라우저가 만드는 값입니다.
→ **프론트가 읽어서 body 에 실어야 합니다.**

`tf_attribution` 에 같이 담아주세요:

```ts
{
  utm_source, utm_medium, utm_campaign, utm_content, referrer, landing_path, expiresAt,
  fbclid: readQueryParam('fbclid') || undefined,   // 랜딩 진입 시점에만 존재
  fbp:    readCookie('_fbp') || undefined,
  fbc:    readCookie('_fbc') || undefined,
}
```

**지금 보내도 백엔드는 조용히 무시합니다** (`attribution` 은 느슨한 JSONField 라 모르는 키가
있어도 400 이 나지 않습니다 — 가입이 깨질 위험 없음). **그래도 먼저 넣어주시는 편이 좋습니다**:
`_fbc` 는 광고 클릭 **그 순간에만** 얻을 수 있어서, 지금 저장을 시작해야 CAPI 착수 시점에
이미 30일치가 쌓여 있습니다. 백엔드는 CAPI 착수 시 컬럼을 추가하고 저장을 켜겠습니다.

---

### 3-6. [P2] iOS 인앱 안내 보강

코드상 `ios-inapp` 은 자동 탈출이 불가하고(`qw()` 가 `false` 반환) `링크 복사하기` 로만
빠져나갈 수 있습니다. 실측 인앱 트래픽이 Android 17 / iOS 7 이라 급하진 않지만, 여유 될 때
*"우측 상단 ⋯ → Safari로 열기"* 안내 한 줄을 추가해 주세요.

---

### 3-7. ~~`tf_vid` 항상 생성~~ — **철회**

v1 에서 제안했다가 회신 8번을 받고 철회합니다. **회신이 맞습니다** — `/login` 직행자는 랜딩
방문 기록 자체가 없어서 `tf_vid` 를 새로 만들어도 붙일 대상이 없습니다. 37%가 줄지 않습니다.

---

## 4. 확인만 (짧게 답변 주시면 됩니다)

- [ ] **`CompleteRegistration` 의 `eventID` 로 무슨 값을 쓰셨습니까?** (§3-4 — 가장 급함)
- [ ] 픽셀 **`StartTrial` · `Purchase` 도 실제로 발사되고 있습니까?** 확인서 7번은
      CompleteRegistration 만 물었습니다. 우리 실적은 **체험 52명 / 실결제 5명**이라
      체험 시작이 사실상 주 전환인데, 이게 안 쏘이면 메타가 최적화할 신호가 없습니다.
- [ ] **`www.turnflow.link` 가 522** 로 죽어 있습니다(회신 3번). Cloudflare Pages 커스텀
      도메인 설정이면 프론트 소관입니다 — **누가 소유인지만** 알려주세요.
      (광고 URL 은 www 가 아니라 급하진 않습니다.)
- [ ] 8/23 무렵 `/login` 을 직접 가리키는 외부 링크(메일·카카오·검색결과)가 새로 생겼는지.
      그날 `visitor_id` 없는 가입이 16%→54% 로 뛰었는데 프론트 배포도 메타 트래픽도
      없었습니다. 안 풀리면 앞으로도 가입의 절반이 계측 밖에 남습니다.

**배포하시면 알려주세요** — prod 에서 즉시 재측정해 귀속이 실제로 붙는지 확인하고 회신하겠습니다.
광고가 돌고 있어 하루면 표본이 쌓입니다.

---

## 5. 백엔드가 한 것 (전부 배포 완료)

| 항목 | 내용 |
|---|---|
| 인증 리다이렉트 도메인 제외 | `accounts.google.com` 외 6개를 채널 파생에서 제외 → `direct` |
| 과거 데이터 교정 | 가입 귀속 105건 + 랜딩 방문 6건 재파생 |
| 광고 소재 `utm_content` | `utm_content` 를 비워 저장한 링크가 그 캠페인의 **모든 소재**를 흡수 |
| `is_new_user` | §3-3 |

> **소실된 UTM 자체는 복구 불가**입니다 — 프론트가 덮어쓴 뒤 서버로 온 적이 없어 어디에도
> 남아 있지 않습니다. 원천 차단은 §3-1 · §3-2 가 필요합니다.

## 6. 백엔드 대기

- **Meta CAPI 서버 전송** — ⛔ 두 가지 선행 필요
  1. **액세스 토큰 재발급** — 대행사 요청서의 토큰이 **평문으로 메신저를 통해 전달**됐습니다.
     그대로 쓰면 안 됩니다.
  2. **§3-4 프론트 배포 확인** — 순서가 뒤바뀌면 2배 집계.
- **갱신 결제 CAPI 전송 여부** — 월 갱신은 브라우저 없이 Celery 가 돌려 IP/UA 가 없습니다.
  **백엔드 의견: 보내지 않는다.** 광고 최적화는 "이 광고가 유료 고객을 만들었나"를 보는 것이라
  첫 결제가 신호이고, 갱신은 같은 사람을 반복 카운트해 ROAS 를 부풀립니다.
- `SignupAttribution` 에 fbp/fbc 컬럼 — CAPI 착수 시.

---

## 7. 대행사에 전달할 것 (참고)

- **"메타 가입 0" 은 계측 오류가 아닙니다.** 어제 시작한 캠페인이고 실제로 0명입니다.
  방문 132건 중 **126건이 최근 24시간** 이내라 전환할 시간 자체가 거의 없었습니다.
  통계적으로도 진짜 가입률이 1%라면 123명 중 0명일 확률이 **29%** 입니다.
- **CAPI 는 대시보드 숫자를 만들지 않습니다.** 요청서 완료 기준 1번("대시보드 채널 표에 Meta
  가입·유료전환이 잡힘")은 CAPI 로 달성되지 않습니다 — 실제 가입이 나와야 잡힙니다.
- **유료전환 0 은 전 채널 공통입니다.** prod 전체 실결제 사용자가 5명이고, 그중 UTM 링크
  유입은 0명입니다.
- **비한국 트래픽 18%** (US 15 · SE 6 · AL 2 · IE 2 / 130명 중). 한국어 전용 서비스이니
  타게팅 국가 제한을 확인해 달라고 하세요.

---

## 8. 변경 이력 (v1 → v2)

| 항목 | v1 | v2 | 이유 |
|---|---|---|---|
| 인앱 브라우저 가입 경로 | **P0 "통째로 막혀 있다"** | **P2 로 강등** | 실기기 확인 결과 모달이 정상 동작하고 탈출·이메일 대안을 모두 제공. **v1 표현은 과장이었습니다** |
| 탈출 시 UTM 전달 | "탈출 시 UTM 붙여 내보내기" 요청 | **불필요** | 탈출 로직이 이미 `window.location.search` 를 넘김. CTA 만 고치면 됨 |
| `tf_vid` 항상 생성 | 제안 | **철회** | 회신 8번이 맞습니다 — 37%가 줄지 않음 |
| CTA UTM 유지 | P0 | **P0 유지 (유일 P0)** | 사슬의 유일한 파손 지점으로 확정 |
| `is_new_user` | 없음 | **신설 (백엔드 배포 완료)** | 회신 7번의 휴리스틱 문제 해결 |

---

관련 문서:
[SIGNUP_ATTRIBUTION_FRONTEND.md](./SIGNUP_ATTRIBUTION_FRONTEND.md) (방문→가입 귀속 계약 원본)
· [UTM_ATTRIBUTION_VERIFY_REQUEST.md](./UTM_ATTRIBUTION_VERIFY_REQUEST.md) (확인 요청서 — 회신 완료)
