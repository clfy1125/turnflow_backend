# 방문 트래킹 + 가입 귀속(Attribution) — 프론트엔드 연동 가이드

> "이번 달 가입자 중 몇 명이 메타 광고에서 왔나?"에 답하기 위한 시스템입니다.
> **랜딩 방문 기록**(어느 채널에서 몇 명이 들어왔나)과 **가입 귀속**(각 가입이 어느 채널
> 덕분인가)을 연결합니다. 개인정보: **IP는 SHA-256 해시만 저장**하고 원본은 저장하지 않습니다.

## 1. 개요 — 무엇을 왜 추적하나

- 랜딩(turnflow.link) 방문 1건 = `POST /api/v1/track/visit/` 비콘 1회 (세션당 1회)
- 가입 1건 = 가입 API(`register` / `google`)에 `attribution` 객체를 실어 보내면 서버가 귀속 저장
- 두 데이터는 **`visitor_id`(클라이언트 생성 UUID)** 로 연결됩니다
- 유입 채널(`meta_ads`, `instagram_organic`, …) 판정은 **서버가** 합니다 — 프론트는 utm/리퍼러
  원문만 전달하면 됩니다 (§6 매핑 표)

## 2. 아키텍처 한눈에 보기

```
[랜딩: turnflow.link]                  [서비스 앱: FRONTEND_URL]            [백엔드 API]
트래킹 스니펫(§3.1)
 ├─ visitor_id 생성(localStorage) ──────────────────────────▶ POST /track/visit/  (세션당 1회)
 ├─ utm/리퍼러를 tf_attr 로 저장
 └─ CTA 링크에 ?vid=&utm_*= 부착 ──▶ 쿼리 수집 스니펫(§4.1)
        (localStorage 는 오리진 간         └─ tf_attr 저장
         공유 안 됨 → 쿼리스트링 핸드오프)      └─ 가입 시 attribution 첨부 ──▶ POST /auth/register/
                                                                            POST /auth/google/
```

## 3. 랜딩 사이트 통합 (turnflow.link)

### 3.1 트래킹 스니펫 전문

`<body>` 끝에 그대로 넣으세요 (모든 랜딩 페이지 공통):

```html
<script>
(function () {
  var API = "https://api.turnflow.link";          // backend origin
  // 1) stable visitor id (per browser, per origin)
  var vid = localStorage.getItem("tf_vid");
  if (!vid) { vid = crypto.randomUUID(); localStorage.setItem("tf_vid", vid); }

  // 2) attribution: "last non-direct touch" — overwrite stored attr whenever
  //    this landing has utm params OR an external referrer; else keep previous.
  var q = new URLSearchParams(location.search);
  var utm = {
    utm_source:   q.get("utm_source")   || "",
    utm_medium:   q.get("utm_medium")   || "",
    utm_campaign: q.get("utm_campaign") || "",
    utm_content:  q.get("utm_content")  || "",
  };
  var extRef = document.referrer && document.referrer.indexOf(location.origin) !== 0
             ? document.referrer : "";
  var hasTouch = extRef || utm.utm_source || utm.utm_medium || utm.utm_campaign;
  var attr = JSON.parse(localStorage.getItem("tf_attr") || "null");
  if (hasTouch || !attr) {
    attr = Object.assign({ visitor_id: vid, referrer: extRef,
                           landing_path: location.pathname, ts: Date.now() }, utm);
    localStorage.setItem("tf_attr", JSON.stringify(attr));
  }

  // 3) send visit — once per browser SESSION (sessionStorage flag)
  if (!sessionStorage.getItem("tf_visit_sent")) {
    sessionStorage.setItem("tf_visit_sent", "1");
    fetch(API + "/api/v1/track/visit/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      keepalive: true,                       // survives page navigation
      body: JSON.stringify(Object.assign({ visitor_id: vid, referrer: extRef,
                                           landing_path: location.pathname }, utm)),
    }).catch(function () {});                // NEVER surface errors to the visitor
  }

  // 4) decorate signup CTA links so attribution crosses to the app origin
  document.querySelectorAll('a[data-tf-cta]').forEach(function (a) {
    var u = new URL(a.href);
    u.searchParams.set("vid", attr.visitor_id);
    ["utm_source","utm_medium","utm_campaign","utm_content"].forEach(function (k) {
      if (attr[k]) u.searchParams.set(k, attr[k]);
    });
    if (attr.referrer) u.searchParams.set("ref_url", attr.referrer);
    if (attr.landing_path) u.searchParams.set("lp", attr.landing_path);
    a.href = u.toString();
  });
})();
</script>
```

### 3.2 visitor_id 규칙

- localStorage 키 `tf_vid`, `crypto.randomUUID()` 로 1회 생성 후 **영구 보관** (지우지 마세요)
- 브라우저·오리진 단위 식별자입니다 (개인 식별 아님 — 서버에는 이 UUID 와 IP 해시만 저장)

### 3.3 세션당 1회 전송 규칙

- sessionStorage 키 `tf_visit_sent` 로 **브라우저 세션당 1회만** `/track/visit/` 전송
- 서버에도 이중 방어가 있습니다 (동일 내용 30분 dedup + visitor 당 시간당 6회 캡) —
  스니펫 버그로 여러 번 쏴도 통계는 오염되지 않지만, 스니펫 규칙을 지키는 게 원칙입니다

### 3.4 last non-direct touch 저장 규칙 (`tf_attr`)

- utm 파라미터가 있거나 **외부** 리퍼러가 있는 랜딩 = "touch" → `tf_attr` 를 **덮어씀**
- 직접 재방문(신호 없음)은 이전 touch 를 보존 → "마지막 유의미한 유입"이 가입에 귀속됩니다
- 저장 필드: `visitor_id, utm_source, utm_medium, utm_campaign, utm_content, referrer, landing_path, ts`

### 3.5 CTA 링크 데코레이션

가입/시작하기로 이어지는 모든 `<a>` 에 `data-tf-cta` 속성만 붙이면 스니펫이 자동 처리합니다:

```html
<a href="https://app.turnflow.link/signup" data-tf-cta>무료로 시작하기</a>
```

앱 오리진으로 전달되는 쿼리 파라미터:

| 파라미터 | 내용 |
|---|---|
| `vid` | visitor_id (UUID) |
| `utm_source` / `utm_medium` / `utm_campaign` / `utm_content` | 저장된 touch 의 utm |
| `ref_url` | 외부 리퍼러 원문 |
| `lp` | 랜딩 경로 (`location.pathname`) |

## 4. 서비스 프론트 통합 (app)

### 4.1 쿼리 파라미터 수집 스니펫 (앱 부트스트랩 시 — 아무 페이지나 진입 시점)

```typescript
// on app bootstrap (any page):
const q = new URLSearchParams(location.search);
if (q.get("vid") || q.get("utm_source")) {
  localStorage.setItem("tf_attr", JSON.stringify({
    visitor_id: q.get("vid") || null,
    utm_source: q.get("utm_source") || "", utm_medium: q.get("utm_medium") || "",
    utm_campaign: q.get("utm_campaign") || "", utm_content: q.get("utm_content") || "",
    referrer: q.get("ref_url") || document.referrer || "",
    landing_path: q.get("lp") || "", ts: Date.now(),
  }));
}
```

### 4.2 이메일 가입 — `POST /api/v1/auth/register/` + `attribution`

```typescript
// at register (email) — attribution is OPTIONAL; expiry 30 days:
const raw = localStorage.getItem("tf_attr");
const attr = raw && (Date.now() - JSON.parse(raw).ts < 30*864e5) ? JSON.parse(raw) : undefined;
await api.post("/api/v1/auth/register", { email, password, password_confirm, full_name,
                                          attribution: attr });
```

`attribution` 필드 명세 (선택, 객체):

| 키 | 타입 | 설명 |
|---|---|---|
| `visitor_id` | string(uuid) | `tf_vid` / `vid` 값. 없거나 깨져도 가입은 정상 진행 |
| `utm_source` | string | 그대로 전달 (서버가 100자 절단·채널 판정) |
| `utm_medium` / `utm_campaign` / `utm_content` | string | 〃 |
| `referrer` | string | 외부 리퍼러 원문 |
| `landing_path` | string | 최초 랜딩 경로 |

> **어떤 값이 잘못 들어가도 가입은 절대 실패하지 않습니다.** 서버가 attribution 을
> 통째로 sanitize 하며, 저장 실패 시에도 가입은 그대로 성공합니다 (silent capture).
> `attribution` 을 아예 안 보내면 그 가입은 `unknown` 채널로 집계됩니다 —
> 연동 배포 전 가입과 구분하기 위한 값이니, 연동 후에는 항상 보내주세요.

### 4.3 Google 가입 — `POST /api/v1/auth/google/` + `attribution`

```typescript
// at Google signup — same object:
await api.post("/api/v1/auth/google/", { token: googleIdToken, attribution: attr });
```

- attribution 은 **신규 가입일 때만** 저장됩니다. 기존 회원의 구글 로그인에는 아무 영향 없음
  (매 로그인마다 보내도 무해).

### 4.4 30일 만료 · 성공 후 삭제 규칙

- `tf_attr.ts` 기준 **30일**이 지난 attribution 은 보내지 않습니다 (§4.2 스니펫에 포함)
- 가입 **성공 후** `localStorage.removeItem("tf_attr")` 로 삭제하세요
  (다음 계정 생성에 옛 touch 가 잘못 귀속되는 것 방지)

## 5. API 명세 — `POST /api/v1/track/visit/`

- **인증 불필요** (공개 비콘). CORS: 랜딩 오리진이 백엔드 `CORS_ALLOWED_ORIGINS` 에 등록돼 있어야 함
- Rate limit: **IP 당 120/hour** (초과 시 429) + visitor_id 당 시간당 6회 기록 캡(초과분은 204 로 조용히 스킵)

요청 바디:

| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `visitor_id` | ✅ | uuid | `tf_vid` |
| `utm_source` | 선택 | string(≤100) | 광고 소스 |
| `utm_medium` | 선택 | string(≤100) | 매체 |
| `utm_campaign` | 선택 | string(≤150) | 캠페인명 |
| `utm_content` | 선택 | string(≤150) | 소재 |
| `referrer` | 선택 | string(≤500) | `document.referrer` (외부만) |
| `landing_path` | 선택 | string(≤300) | `location.pathname` |

응답:

| 상태 | 의미 | 프론트 처리 |
|---|---|---|
| **204** | 기록 완료 **또는 조용히 스킵** (봇/검증실패/중복/캡 초과 — 구분 불가·불필요) | 없음 |
| 429 | IP 스로틀 초과 | 없음 (`.catch(() => {})` — 재시도 금지) |

> 이 엔드포인트는 fire-and-forget 입니다. 응답을 검사하지 말고, 실패를 방문자에게
> 절대 노출하지 마세요.

## 6. 채널 매핑 표 (마케팅팀 UTM 규칙 가이드 겸용)

판정 우선순위: **① influencer medium → ② utm_source 매핑 → ③ 유료 medium → ④ 기타 utm →
⑤ 리퍼러 도메인 → ⑥ direct**. utm 이 하나라도 있으면 리퍼러는 무시됩니다.

### 6.1 utm 기반

| 조건 | channel 키 |
|---|---|
| `utm_medium` ∈ `influencer, creator, ambassador, kol` (source 무관) | `influencer` |
| `utm_source` ∈ `meta, facebook, fb, instagram, ig, instagram_ads` | `meta_ads` |
| `utm_source` ∈ `google, youtube_ads` | `google_ads` |
| `utm_source` ∈ `naver, naver_gfa` | `naver_ads` |
| `utm_source` = `kakao` | `paid_other` |
| 그 외 source + `utm_medium` ∈ `cpc, ppc, paid, paid_social, display, banner` | `paid_other` |
| 그 외 아무 `utm_source` | `other_campaign` |

> 광고 집행 시 UTM 규칙: 메타 광고 = `?utm_source=meta&utm_medium=cpc&utm_campaign=<캠페인>`,
> 인플루언서 협찬 = `?utm_source=instagram&utm_medium=influencer&utm_campaign=<크리에이터>`.

### 6.2 리퍼러 기반 (utm 없을 때)

| 리퍼러 도메인 (서브도메인 포함) | channel 키 |
|---|---|
| instagram.com, l.instagram.com | `instagram_organic` |
| facebook.com, l.facebook.com, fb.com | `facebook_organic` |
| youtube.com, youtu.be | `youtube_organic` |
| tiktok.com | `tiktok_organic` |
| threads.net | `threads_organic` |
| blog.naver.com, cafe.naver.com | `blog_organic` |
| search.naver.com, naver.com, google.com, google.co.kr, daum.net | `search_organic` |
| 그 외 외부 도메인 | `other_referral` |
| 자기 도메인(turnflow.link 등)·리퍼러 없음 | `direct` |

### 6.3 특수 채널

| channel 키 | 의미 |
|---|---|
| `direct` | attribution 은 보냈으나 유입 신호 없음 (URL 직접 입력) |
| `unknown` | attribution 자체가 안 온 가입 (구버전 프론트/API 가입) |
| `referral` | 제휴코드 가입 — 가입 **이후** 코드 사용 시 확정되므로 대시보드가 조회 시점에 오버레이 |

## 7. 테스트 방법

```bash
# 1) 방문 기록 (204 기대)
# ⚠️ curl 기본 User-Agent 는 봇으로 필터링됨 — 반드시 브라우저 UA 를 지정할 것
curl -s -o /dev/null -w "%{http_code}" -X POST https://dev-api.turnflow.link/api/v1/track/visit/ \
  -H "Content-Type: application/json" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36" \
  -d '{"visitor_id":"3f1c2b74-9a1e-4f7b-8f52-1d2c3e4a5b6c","utm_source":"meta","utm_medium":"cpc"}'

# 2) 가입 + attribution (201 기대)
curl -X POST https://dev-api.turnflow.link/api/v1/auth/register/ \
  -H "Content-Type: application/json" \
  -d '{"email":"attr-test@example.com","password":"SecurePass123!","password_confirm":"SecurePass123!",
       "attribution":{"visitor_id":"3f1c2b74-9a1e-4f7b-8f52-1d2c3e4a5b6c","utm_source":"meta","utm_medium":"cpc"}}'
```

로컬(백엔드) 확인 쿼리:

```bash
docker compose exec web python manage.py shell -c \
  "from apps.analytics.models import LandingVisit, SignupAttribution; \
   print(LandingVisit.objects.values('visitor_id','channel','utm_source').last()); \
   print(SignupAttribution.objects.values('user__email','channel','signup_kind').last())"
```

- 동일 페이로드 재전송은 30분간 기록되지 않는 게 정상입니다 (burst dedup)
- Swagger: `analytics` 태그 → `POST /api/v1/track/visit/`

## 8. FAQ

**Q. 애드블록이 비콘을 막으면?**
일부 차단 목록이 `/track/` 경로를 막을 수 있습니다. 그 방문은 유실되지만 (통계 특성상 허용),
**가입 귀속은 별도 경로**(가입 API 의 `attribution` 필드)라 애드블록 영향을 받지 않습니다 —
CTA 쿼리스트링 → `tf_attr` → register 페이로드는 일반 API 호출이기 때문입니다.

**Q. 왜 쿠키/localStorage 가 아니라 쿼리스트링으로 넘기나요?**
랜딩(turnflow.link)과 앱(FRONTEND_URL)은 **다른 오리진**이라 localStorage 가 공유되지 않습니다.
서드파티 쿠키는 브라우저가 차단하는 추세라, CTA 링크 쿼리 파라미터가 가장 견고한 핸드오프입니다.

**Q. 봇 트래픽은 걸러지나요?**
서버가 User-Agent 로 봇(`bot/crawler/spider/headless/curl/python-requests` 등)을 판별해
**기록 없이 204** 로 응답합니다. 추가로 visitor 당 시간당 6회 캡 + 동일 내용 30분 dedup +
IP 스로틀(120/hour)이 있습니다. UA 를 위장한 정교한 봇까지 완벽 차단하진 않습니다(허용 오차).

**Q. 방문을 여러 번 보내면 통계가 부풀지 않나요?**
방문자 수는 `COUNT(DISTINCT visitor_id)` 로 집계돼 중복 전송에 안전하고, 방문(세션) 수는
서버 dedup/캡이 방어합니다.

**Q. 가입 API 에 attribution 을 잘못 보내면 가입이 실패하나요?**
아니요. 어떤 형태(문자열, 깨진 UUID, 초과 길이)든 서버가 무시/절단하고 가입은 항상
정상 진행됩니다. 검증 에러(400)의 원인이 되지 않습니다.
