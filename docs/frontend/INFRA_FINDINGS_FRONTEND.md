# 인프라 작업 중 발견한 프론트 사항 3건

**2026-10-13 · 백엔드 → 프론트** · 코드 변경 없음(백엔드), 프론트 조치 2건

> 한국에서 사이트가 런던·암스테르담을 거치는 문제를 고치려고 CloudFront 전환 작업을 하다가
> 눈에 띈 것들입니다. 급한 건 아니지만 **02 는 나중에 크게 터질 수 있어** 공유드립니다.

| # | 건 | 상태 | 언제 |
|---|---|---|---|
| 01 | `www.turnflow.link` 522 | **백엔드가 조치 완료** | 완료 — 확인만 |
| 02 | CSP `connect-src` 에 운영 API 누락 | **프론트** | **차단 모드 켜기 전 반드시** |
| 03 | 네이버 소유확인 파일 308 | 프론트 | 여유 있을 때 |

⚠️ **왜 저희가 직접 안 고쳤나** — 02·03 은 `Changus99/TurnflowLink` 레포 몫입니다.
저희가 가진 로컬 사본은 `Changus99/TurnflowLanding`(브랜치 `design/lim`)인데 **배포본과 내용이
다릅니다**(아래 02 의 CSP 비교 참고). 손대면 오히려 꼬일 것 같아 전달만 드립니다.

---

## 01. www 로 들어오면 접속이 안 됐습니다 — ✅ 조치 완료

`https://www.turnflow.link` 이 **522** 를 내고 있었습니다. 그동안 www 를 붙여 들어온
방문자는 오류 화면만 봤습니다.

### 원인

DNS 는 **정상**이었습니다.

```
www.turnflow.link  CNAME  turnflowlink.pages.dev  (프록싱됨)
```

문제는 **Pages 프로젝트 `turnflowlink` 의 커스텀 도메인 목록에 `www.turnflow.link` 가
등록되어 있지 않았던 것**입니다. Cloudflare 입장에선 "모르는 호스트"라 origin 을 찾지 못하고
522 를 냈습니다.

그 결과 `public/_redirects` 의 이 규칙이 **실행될 기회조차 없었습니다** — 주석에 적어두신
*"세 도메인이 모두 부착돼 있어야 함"* 조건이 깨져 있었습니다.

```
https://www.turnflow.link/*  https://turnflow.link/:splat  301!
```

### 조치

Pages → `turnflowlink` → 사용자 설정 도메인에 `www.turnflow.link` 추가.
**DNS 레코드는 건드리지 않았습니다** — Cloudflare 가 "기존 레코드 = 새 레코드"라고
확인해 주었고, 등록만 빠져 있던 상황이었습니다.

| 요청 | 전 | 후 |
|---|---|---|
| `www.turnflow.link/` | 522 | **301 → `turnflow.link/`** |
| `www.turnflow.link/pricing` | 522 | **301 → `turnflow.link/pricing`** |

경로 보존까지 정상 동작합니다.

### ⚠️ 같은 목록에 하나 더 깨져 있습니다

`link.turnflow.clfy.ai.kr` 이 **비활성(오류)** 상태입니다. `_redirects` 가 기대하는 세 도메인
중 하나인데, `clfy.ai.kr` 존 쪽 DNS 레코드가 없어 보여 **저희가 손대지 않았습니다**.

```
https://link.turnflow.clfy.ai.kr/*  https://turnflow.link/:splat  301!
```

주석에 *"이미 발송된 DM 안의 링크 / 외부에 공유된 공개 링크 영구 보존용"* 이라고 적혀 있어서,
**예전 DM 에 이 도메인으로 나간 링크가 있다면 지금 죽어 있습니다.** 확인 부탁드립니다.

---

## 02. CSP 에 운영 API 주소가 없습니다 — ⚠️ 지뢰

실제 서빙 중인 `Content-Security-Policy-Report-Only` 의 `connect-src` 를 확인했는데,
**지금 쓰는 운영 API 주소가 빠져 있습니다.**

| 주소 | CSP 에 | 실제 |
|---|---|---|
| `https://api.turnflow.link` | ❌ **없음** | **현재 운영 API** |
| `https://turnflow-api.clfy.ai.kr` | ✅ 있음 | 옛 주소 (웹훅·OAuth 콜백용으로 아직 살아 있음) |
| `https://dev-api.turnflow.link` | ✅ 있음 | 개발 |

2026-09-28 에 API 를 `api.turnflow.link` 로 옮겼는데(CloudFront 경유·서울, 325ms → 12.7ms),
CSP 목록이 그때 같이 갱신되지 않은 것으로 보입니다.

### 지금은 아무 일도 안 일어납니다

`Report-Only` 라 위반을 **기록만 하고 차단하지 않습니다**.

### ⚠️ 문제는 차단 모드로 전환하는 순간입니다

`_headers` 주석에 적어두신 절차 — *"콘솔 위반 로그를 0건으로 만든 뒤
`Content-Security-Policy` 로 바꿔 실제 차단으로 전환"* — 가 맞는데,
**그 0건 확인을 운영 API 호출이 포함된 화면에서 하지 않으면 전환 직후 모든 API 요청이
막힙니다.**

### 제안

차단 모드 전환 전에 추가해 주세요.

```
connect-src 'self'
  https://api.turnflow.link        ← 추가 (현재 운영 API)
  https://turnflow-api.clfy.ai.kr  ← 유지 (옛 주소, 아직 살아 있음)
  https://dev-api.turnflow.link
  ...
```

### 참고 — 레포와 배포본이 다릅니다

저희 로컬 사본(`TurnflowLanding`)의 `public/_headers` 와 실제 서빙되는 CSP 가 다릅니다.
**배포본이 더 최신**입니다.

| | 실제 서빙 | 로컬 레포 사본 |
|---|---|---|
| `connect-src` 항목 수 | 15개 | 10개 |
| 토스·페이스북 | 있음 | 없음 |
| API 주소 | `turnflow-api.clfy.ai.kr` | `api.turnflow.clfy.ai.kr` |

Pages 프로젝트 `turnflowlink` 의 빌드 소스가 **`Changus99/TurnflowLink`** 로 되어 있어,
그쪽이 정본으로 보입니다.

### CloudFront 전환과 CSP의 관계

**무관합니다.** 도메인이 `turnflow.link` 그대로이고 API 주소도 바뀌지 않습니다.
(CORS 도 같은 이유로 무관합니다 — 운영 API 가 `turnflow.link` Origin 을 이미 허용하는 것을
preflight 로 확인했습니다.)

다만 한 가지, Cloudflare 가 자동으로 주입하던
`/cdn-cgi/scripts/…/email-decode.min.js` 가 **사라집니다**(이메일 난독화 기능).
`script-src` 의 인라인 해시를 다시 계산하실 때 그 스크립트는 빼고 보시면 됩니다.
HTML 이 1,179B 줄어드는 것도 이 때문입니다.

---

## 03. 네이버 소유확인 파일이 SPA 껍데기를 돌려줍니다 — 참고

```
GET /naver1f45b4e9cb73e62937555f42a003cf3b.html
  → 308 Permanent Redirect
  → /naver1f45b4e9cb73e62937555f42a003cf3b   (.html 이 떨어짐)
  → 200 text/html                            (SPA index.html)
```

Cloudflare Pages 가 `.html` 확장자를 자동으로 떼면서 생긴 일이고, 결과적으로 네이버가
받는 것은 **파일 내용이 아니라 앱 껍데기**입니다.

### 그래도 지금은 괜찮습니다

`index.html` 에 메타태그가 함께 들어 있어 **소유확인이 그쪽으로 유지되고 있습니다.**

```html
<meta name="naver-site-verification" content="2b6edd143cae3aa18d6f23fb01593430003d0cfa" />
```

### 다만

네이버가 재확인을 요구하거나 파일 방식으로 검증하는 시점이 오면 실패합니다.
`_redirects` 의 *"정적 SEO / 메타 파일"* 섹션에 설명 주석은 있는데 **실제 핀 줄이
비어 있었습니다**. 한 줄이면 됩니다.

```
/naver1f45b4e9cb73e62937555f42a003cf3b.html  /naver1f45b4e9cb73e62937555f42a003cf3b.html  200!
```

확장자 없는 경로로도 들어올 수 있으니 두 경로를 다 핀하거나, `sitemap.xml` 처럼
정상 동작하는지 한 번 확인해 보시면 좋겠습니다.
(`robots.txt` → `text/plain`, `sitemap.xml` → `application/xml` 은 **정상**이었습니다.)

---

## ⭐ 좋았던 것 하나 — 도메인 가드 / 색인 가드

`index.html` 의 인라인 스크립트 두 개가 아주 잘 만들어져 있었습니다.

```js
// 도메인 가드
host === 'turnflow.link' || 'app.turnflow.link' || 'dev.turnflow.link' || localhost
  → 그 외는 location.replace('https://turnflow.link' + path + search + hash)

// 색인 가드
hostname !== 'turnflow.link'
  → robots 를 noindex 로 덮어쓰기
```

**인프라를 바꾸면서 SEO 색인이 깨질까 가장 걱정했던 부분인데, 이 코드가 이미 막고
있었습니다.** 미러 도메인이 생겨도 색인이 분산되지 않습니다.

실제로 저희가 새 호스트(`cf-test.turnflow.link`)로 렌더 테스트를 하려다 가드에 튕겼는데,
그게 정확히 의도된 동작이었습니다. 화이트리스트에 `dev.turnflow.link` 가 있어서
그쪽에서 검증을 마쳤습니다.

⚠️ 그래서 **새 호스트를 추가하실 일이 있으면 이 화이트리스트도 함께 보셔야 합니다.**

---

## 참고 — 저희가 하고 있는 작업

한국 사용자가 우리 사이트에 접속할 때 **서울이 아니라 런던·암스테르담을 거치는** 문제를
CloudFront 로 우회하는 중입니다. Cloudflare Pages·Workers 는 **그대로 두고 앞에 한 겹**
세우는 방식이라, 빌드·배포·코드는 바뀌지 않습니다.

| 호스트 | 연결 | 총 |
|---|---:|---:|
| `turnflow.link` (현재) | 263ms | 1,231ms |
| 같은 내용, CloudFront 경유 | **8ms** | **67ms** |

현재 `dev.turnflow.link` 까지 전환해 검증을 마쳤고, 실서비스(`turnflow.link`)는 아직입니다.
전환해도 프론트에서 하실 일은 없습니다.

---

**궁금한 점이나 "이건 이래서 이렇게 둔 것"인 부분이 있으면 편하게 알려주세요.**
저희가 바깥에서(응답 헤더·DNS·Pages 설정) 본 것이라 맥락을 놓쳤을 수 있습니다.

측정 2026-10-13 · 실제 응답 헤더·DNS·Cloudflare Pages 설정 직접 조회
