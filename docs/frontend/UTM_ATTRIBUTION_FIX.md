# UTM 귀속 소실 수정 요청 (프론트) + Meta CAPI 사전 합의

**작성일** 2026-08-25 · **대상** 서비스 프론트(TurnflowLink) 개발자
**백엔드 상태** 아래 §2 는 **구현·배포 완료**. 프론트는 §3 만 하면 됩니다.

---

## 0. 요약 — 무엇이 틀렸고 누가 무엇을 하나

마케팅 대행사가 *"UTM 방문 27건인데 가입이 0"* 이라고 신고했습니다. prod 를 전수 조사한 결과:

| 신고/주장 | 판정 |
|---|---|
| 메타 광고 가입이 0으로 뜬다 | ✅ **사실입니다. 집계 버그 아님** — 실제로 0명이 가입했습니다 |
| 원인이 "소셜 로그인 리다이렉트에서 UTM 소실" | ⚠️ **원인 추정은 맞습니다** — 다만 이번 0의 원인은 아닙니다 |
| (프론트 회신) "구글 로그인은 ID Token 방식이라 페이지를 떠나지 않는다" | ❌ **웹은 떠납니다** — 근거는 §1 |
| (프론트 회신) "UTM 저장·전송은 이미 완료" | ⚠️ 저장·전송은 맞으나 **저장값이 로그인 왕복에 덮어써집니다** |

**결론: 프론트에 할 일이 있습니다.** 대행사 요청서의 "저장했다가 보내달라"는 이미 되어 있는 게 맞고,
진짜 문제는 **저장한 값을 스스로 지우고 있다**는 점입니다.

---

## 1. 근거 (prod 실측, 2026-08-25 13:5x KST)

### 1-1. 웹 구글 로그인은 전체 페이지 리다이렉트입니다

`https://turnflow.link` 에 **현재 배포된** 번들(`/assets/index-tCC9SDKz.js`)을 직접 받아 확인했습니다:

```js
window.location.replace(
  "https://accounts.google.com/o/oauth2/v2/auth?client_id=…&redirect_uri=<origin>/login&response_type=id_token&…"
)
```

`response_type=id_token` 은 맞지만 **전달 방식이 redirect** 입니다. One Tap(`google.accounts.id.initialize`)은
초기화만 되어 있고, 버튼 클릭 경로는 위 리다이렉트입니다. (네이티브 앱은 `GoogleAuth` 플러그인이라
페이지를 안 떠나는 게 맞습니다 — **웹만** 해당합니다.)

### 1-2. 그래서 저장값이 덮어써집니다

같은 번들의 캡처 함수(minify 해제):

```js
function captureAttribution(){
  const utm = readUtmFromQuery();
  const referrer = getReferrer();              // host !== 자기 host 면 채택
  if (!hasSignal(utm, referrer)) return;       // 둘 다 없을 때만 skip
  localStorage.setItem("tf_attribution", JSON.stringify({
    ...utm, referrer, landing_path, expiresAt: Date.now() + 30일
  }));                                          // ← 조건 없는 덮어쓰기
}
```

**UTM 이 이미 저장돼 있어도, 외부 리퍼러만 있으면 그대로 덮어씁니다.**

실제 시나리오:

```
① 메타 광고 클릭 → /?utm_source=meta&utm_medium=cpc&utm_campaign=…
   tf_attribution = {utm_source:"meta", …}                    ✅ 정상 저장
② "구글로 로그인" 클릭 → accounts.google.com 으로 전체 페이지 이동
③ 복귀 → /login#id_token=…
   captureAttribution() 재실행: utm 없음 + referrer="https://accounts.google.com/" (외부)
   tf_attribution = {referrer:"https://accounts.google.com/", landing_path:"/login"}
                                                              ❌ 메타 UTM 삭제됨
④ 가입 요청 body.attribution 에는 UTM 이 없음
```

### 1-3. 실측 수치

| 확인 | 결과 |
|---|---|
| `SignupAttribution` 전체 | 160건 |
| 그중 `referrer="https://accounts.google.com/"` + `landing_path="/login"` | **105건 (66%)** |
| 그 105건의 서버 채널 판정 | 전부 `search_organic`(자연 검색) |
| 대시보드 "검색 유입 가입 77건" 중 실제 검색 | **약 5건** (나머지는 전부 로그인 잔상) |
| UTM 을 달고 방문했다가 **구글**로 가입한 사람 | 5명 → **전원 UTM 소실** |
| 같은 UTM 을 달고 **이메일**로 가입한 사람 | 2명 → **UTM 보존됨** ← 경로가 갈린다는 증거 |
| 최신 발생 | **오늘 12:16 KST** (진행 중) |

> `landing_path` 가 105/105 전부 `/login` 입니다. 페이지를 안 떠났다면 나올 수 없는 값입니다.

### 1-4. 다만, 이번 "메타 가입 0" 의 원인은 이게 아닙니다

`visitor_id`(`tf_vid`)는 `tf_attribution` 과 **별개 localStorage 키**라 덮어쓰기의 영향을 받지 않습니다.
그 키로 방문↔가입을 조인한 결과:

- meta_ads 채널 방문자 **125명 → 가입 0명**
- '턴플로우 대행 프로젝트' 캠페인 방문자 **119명 → 가입 0명**

즉 *"가입은 했는데 채널만 잘못 붙었다"* 가 아니라 **가입 이벤트 자체가 없습니다.**
(광고 집행이 사실상 8/25 00:24 KST 에 시작돼 트래픽 대부분이 어제·오늘 들어왔습니다.)

**그래도 지금 고쳐야 하는 이유**: 메타에서 첫 가입이 나오는 순간부터, 구글로 가입한 사람은
**전부 '검색'으로 새어나갑니다.** 광고비가 본격 집행되기 시작한 지금이 마지막 타이밍입니다.

---

## 2. 백엔드가 이미 한 것 (배포 완료 — 프론트 조치 불필요)

### 2-1. 인증 리다이렉트 도메인을 채널 파생에서 제외

`accounts.google.com` 이 `google.com` suffix 에 매칭돼 `search_organic` 이 되던 것을 막았습니다.
이제 인증 왕복 리퍼러는 **자기 도메인과 동일하게 "신호 없음"** 으로 취급합니다 → `direct`(출처 미상).

대상: `accounts.google.com` `appleid.apple.com` `nid.naver.com` `kauth.kakao.com`
`accounts.kakao.com` `login.microsoftonline.com` `login.live.com`

> 프론트가 §3-1 을 안 해도 **채널 오분류는 더 이상 생기지 않습니다.**
> 하지만 **소실된 UTM 은 백엔드가 복구할 수 없습니다** — 서버로 온 적이 없으니까요.
> 그래서 §3-1 이 여전히 필요합니다.

### 2-2. 과거 105건 교정

`manage.py fix_auth_referrer_channels --apply` 로 기존 행의 `channel` 을 재파생했습니다.
(`referrer` 원문은 사후 조사 근거라 보존합니다.)

### 2-3. 광고 소재별 `utm_content` 대응

메타가 8/25 부터 `utm_content=120251297076190315` 같은 **광고 ID** 를 자동으로 붙이기 시작했는데,
저장된 채널 링크는 `utm_content` 가 비어 있어 4-튜플 매칭이 어긋났습니다 → 이틀간 94명이
'저장 안 된 링크(UTM)' 로 떨어져 있었습니다. 이제 **`utm_content` 를 비워 저장한 링크는
그 캠페인의 모든 소재를 흡수**합니다(정확일치가 있으면 그쪽이 우선).

---

## 3. 프론트가 해야 할 일

### 3-1. ⭐ [P0] `captureAttribution` 덮어쓰기 가드

가드 두 개를 넣어주세요. **둘 다 필요합니다.**

```ts
// ① 인증 제공자 왕복은 유입 신호가 아니다 (백엔드 AUTH_REDIRECT_DOMAINS 와 동일 목록)
const AUTH_REDIRECT_HOSTS = new Set([
  'accounts.google.com',
  'appleid.apple.com',
  'nid.naver.com',
  'kauth.kakao.com',
  'accounts.kakao.com',
  'login.microsoftonline.com',
  'login.live.com',
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

**왜 ② 도 필요한가**: ① 만 넣으면 구글은 막히지만, 다른 외부 왕복이 그대로 남습니다.
prod 에 이미 `referrer="https://docs.turnflow.link/"` 로 덮어써진 행이 있고, 토스 결제창 복귀 등
앞으로 늘어날 경로도 같은 함정입니다. **"UTM 터치가 리퍼러 터치보다 세다"** 는 규칙 자체를
넣는 편이 목록 관리보다 안전합니다.

**건드리면 안 되는 것**: `tf_vid`(visitor_id)는 지금 로직 그대로 두세요 — 방문↔가입 조인의
유일한 키이고, 이번 조사도 이 키 덕분에 가능했습니다.

**검증 방법** (배포 후 직접 확인 가능)
1. 시크릿 창에서 `https://turnflow.link/?utm_source=meta&utm_medium=cpc&utm_campaign=test_guard` 접속
2. DevTools → `localStorage.tf_attribution` 에 `utm_source:"meta"` 확인
3. 구글로 로그인 → 복귀 후 **다시** `tf_attribution` 확인 → `utm_source:"meta"` 가 **살아 있어야** 합니다
4. 가입 완료 후 백엔드에 확인 요청 → `SignupAttribution.channel == "meta_ads"` 여야 합니다

---

### 3-2. [P1] Meta 픽셀 `eventID` 부여 — **프론트 먼저 배포해도 안전합니다**

프론트 회신에서 *"한쪽만 배포하면 그 기간에 집계가 2배로 뜹니다"* 라고 하셨는데,
**순서에 따라 다릅니다**:

| 배포 순서 | 결과 |
|---|---|
| 프론트(eventID) 먼저 → 백엔드(CAPI) 나중 | ✅ **안전**. 서버 이벤트가 없으니 중복될 대상이 없습니다 |
| 백엔드(CAPI) 먼저 → 프론트 나중 | ❌ 그 기간 2배 집계 |

**그래서 프론트가 먼저 넣어주시면 됩니다.** 백엔드는 프론트 배포를 확인한 뒤에 CAPI 를 붙입니다.

합의할 `event_id` 규칙 (백엔드가 CAPI 에서 **똑같은 값**을 씁니다):

| 이벤트 | event_id | 비고 |
|---|---|---|
| `CompleteRegistration` | `String(user.id)` | `metaPixel.ts` 가 이미 `dedupKey` 로 계산해 두고 `fbqTrack` 에 안 넘기고 있는 값 |
| `StartTrial` | `String(subscription.id)` | 체험 시작 = 구독 1건. 사용자당 여러 번 가능하므로 user.id 는 부적합 |
| `Purchase` | `payment.id` | 이미 적용돼 있음 — 변경 없음 |

> ⚠️ 각 id 는 **이벤트당 유일**해야 합니다. 재렌더로 픽셀이 두 번 발사돼도 같은 event_id 면
> Meta 가 합쳐주므로, id 를 매번 새로 만들지 말고 위 서버 값 그대로 쓰세요.

---

### 3-3. [P1] `fbclid` / `_fbc` / `_fbp` 수집 — 지적하신 대로 프론트 몫이 맞습니다

사이트는 `turnflow.link`, API 는 `turnflow-api.clfy.ai.kr` 로 **등록가능도메인이 달라** 쿠키가
붙지 않습니다. `_fbc` 는 애초에 `fbclid` URL 파라미터에서 브라우저가 만드는 값입니다.
→ **프론트가 읽어서 body 에 실어야 합니다.** 대행사 요청서 21번 줄이 정확히 이 얘기입니다.

`tf_attribution` 에 같이 담아주세요 (`captureAttribution` 안, UTM 과 같은 자리):

```ts
{
  utm_source, utm_medium, utm_campaign, utm_content,
  referrer, landing_path, expiresAt,
  fbclid: readQueryParam('fbclid') || undefined,   // 랜딩 진입 시점에만 존재
  fbp:    readCookie('_fbp') || undefined,         // 픽셀이 심는 값
  fbc:    readCookie('_fbc') || undefined,         // fbclid 유입 시 픽셀이 생성
}
```

**지금 보내도 백엔드는 조용히 무시합니다** (`attribution` 은 느슨한 JSONField 라 모르는 키가
있어도 400 이 나지 않습니다 — 가입이 깨질 위험 없음). **그래도 먼저 넣어주시는 편이 좋습니다**:
`_fbc` 는 광고 클릭 **그 순간에만** 얻을 수 있어서, 지금 저장을 시작해야 CAPI 착수 시점에
이미 30일치가 쌓여 있습니다.

백엔드는 CAPI 착수 시 `SignupAttribution` 에 컬럼을 추가하고 저장을 켜겠습니다.
(그 전까지는 컬럼을 미리 만들지 않습니다 — 쓰지 않는 컬럼을 마이그레이션으로 남기지 않으려고요.)

---

## 4. 백엔드가 아직 안 한 것 / 결정 필요

### 4-1. Meta CAPI 서버 전송 — **착수 전 두 가지가 선행돼야 합니다**

1. **액세스 토큰 재발급 필요** ⚠️
   요청서의 토큰이 **평문으로 메신저를 통해 전달**됐습니다. 그대로 쓰면 안 됩니다.
   (프론트 개발자 지적에 동의합니다.) 재발급 후 백엔드 환경변수로만 주입하겠습니다.
2. **§3-2 프론트 배포 확인** — 그 후에 붙여야 2배 집계가 안 납니다.

### 4-2. 갱신 결제(월 정기)의 CAPI 전송 여부 — 결정 필요

프론트 개발자가 짚은 대로, 월 갱신은 브라우저 없이 Celery 가 돌리므로 `client_ip_address` /
`client_user_agent` 가 없습니다. 선택지:

- (a) 갱신 `Purchase` 는 CAPI 로 **안 보낸다** — 광고 최적화 신호는 첫 결제로 충분
- (b) 가입 시점의 `fbp`/`external_id` 를 저장해 뒀다가 쓴다 (매칭 품질 하락 감수)

백엔드 의견은 **(a)** 입니다. 메타 광고 최적화는 "이 광고가 유료 고객을 만들었는가"를 보는 것이라
첫 결제가 신호이고, 갱신은 오히려 같은 사람을 반복 카운트해 ROAS 를 부풀립니다.

### 4-3. 대행사에 전달할 것

- **"메타 가입 0" 은 계측 오류가 아닙니다.** 어제 시작한 캠페인이고 실제로 0명입니다.
- **CAPI 는 대시보드 숫자를 만들지 않습니다.** 요청서 완료 기준 1번("대시보드 채널 표에 Meta
  가입·유료전환이 잡힘")은 CAPI 로 달성되지 않습니다 — 실제 가입이 나와야 잡힙니다.
  CAPI 는 Meta 쪽 최적화·측정 정확도를 위한 것입니다.
- **유료전환 0 은 전 채널 공통입니다.** prod 전체 실결제 사용자가 5명이고, 그중 UTM 링크
  유입은 0명입니다. 메타만의 문제가 아닙니다.

---

## 5. 체크리스트

**프론트**
- [ ] §3-1 `captureAttribution` 가드 2종 (P0)
- [ ] §3-1 검증 4단계 수행
- [ ] §3-2 `eventID` — CompleteRegistration / StartTrial (Purchase 는 이미 있음)
- [ ] §3-3 `fbclid` / `_fbp` / `_fbc` 를 `tf_attribution` 에 수집

**백엔드 (완료)**
- [x] 인증 리다이렉트 도메인 채널 제외
- [x] 과거 105건 채널 교정
- [x] 광고 소재별 `utm_content` 링크 매칭

**백엔드 (대기)**
- [ ] CAPI 전송 — 토큰 재발급 + 프론트 §3-2 배포 후
- [ ] `SignupAttribution` 에 fbp/fbc 컬럼 — CAPI 착수 시

---

관련 문서: [SIGNUP_ATTRIBUTION_FRONTEND.md](./SIGNUP_ATTRIBUTION_FRONTEND.md) (방문→가입 귀속 계약 원본)
