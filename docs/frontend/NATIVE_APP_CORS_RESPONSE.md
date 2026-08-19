# [회신 · 2026-08-19] 네이티브 앱 origin CORS 허용 — 반영 완료 (+ 다음 버그 미리 알림)

> **한 줄:** 요청대로 `https://localhost`(Android) · `capacitor://localhost`(iOS) 두 개만
> 허용했습니다. **dev(`dev-api.turnflow.link`)는 지금 됩니다** — 아래 실측 참고.
> prod 와 CS 워커는 배포가 필요합니다(§3).
>
> 그리고 **프론트가 곧 만날 다음 버그를 §4-1 에 적었습니다.** CORS 를 뚫으면 CS 문의 API 가
> 전부 401 로 떨어질 구성입니다(원인은 CORS 가 아니라 워커/백엔드 환경 짝 어긋남).

---

## 1. 판단 — 요청안 그대로 갑니다

세 갈래를 검토했고, **요청안(origin 2개 허용)** 이 맞습니다.

| 안 | 판단 | 이유 |
|---|---|---|
| **① origin 2개 허용** | ✅ **채택** | Capacitor 앱을 붙이는 표준 절차. 변경 표면이 설정 두 줄. |
| ② `CapacitorHttp` 로 CORS 우회 | ❌ | 프론트 판단에 동의합니다. `FormData` 업로드·`AbortSignal.timeout`·blob 을 이미 쓰는 앱에서 네트워크 계층을 통째로 바꾸는 건 origin 2개와 교환할 리스크가 아닙니다. |
| ③ 와일드카드 `*` | ❌ | `credentials` 와 병용 불가(브라우저가 거부). 애초에 필요도 없습니다. |

보안 우려에 대한 프론트 설명도 맞습니다. 보태면, **이 추가로 실제로 늘어나는 노출은
"사용자 자기 PC 의 `https://localhost:443` 에서 돌아가는 페이지가 우리 API 응답을 읽을 수 있다"
뿐**이고, 그러려면 그 페이지가 **이미 유효한 JWT 를 갖고 있어야** 합니다. 앱 웹뷰의 저장소는
데스크톱 브라우저와 분리되어 있어 토큰이 건너가지 않습니다. 방어선은 그대로 토큰 검증입니다.

---

## 2. 어떻게 넣었나 — env 가 아니라 **코드**

프론트 요청서에는 "`CORS_ALLOWED_ORIGINS` 에 문자열 2개 추가" 로 적혀 있었지만,
**`.env` 가 아니라 settings 코드에 넣었습니다.** 결과는 같고, 이유가 있습니다.

- 이 두 값은 **앱 바이너리에 박히는 상수**입니다. dev/prod/DR 복구본에서 다를 이유가 없습니다.
- env 로 두면 새 환경 구성·DR 복구·prod `.env` 재작성에서 **조용히 빠집니다.** 그리고 그 사실이
  "앱이 통째로 안 됨" 이라는 형태로 **배포 후에** 드러납니다. 이번 제보와 똑같은 증상으로요.
- 코드에 두면 환경이 자동으로 같아집니다. 되돌릴 일이 생기면 `NATIVE_APP_CORS_ORIGINS=` 를
  빈 값으로 주면 전부 빠집니다(킬스위치).

```
config/settings/base.py     NATIVE_APP_CORS_ORIGINS = ["https://localhost", "capacitor://localhost"]
config/settings/local.py    CORS_ALLOWED_ORIGINS += NATIVE_APP_CORS_ORIGINS   (중복 제거)
config/settings/prod.py     동일
```

기존 응답 헤더는 전부 그대로입니다 — `Access-Control-Allow-Credentials: true`,
허용 헤더(`authorization` 포함), `Access-Control-Expose-Headers`(`X-Request-ID` 등), `max-age=86400`.

### `CORS_ALLOWED_ORIGIN_REGEXES` 는 필요 없었습니다

요청서에서 우려한 부분입니다. **불필요합니다** — 컨테이너에서 실측했습니다.
`django-cors-headers` 4.3.1 은 허용목록을 `urlsplit` 의 **scheme + netloc 로 비교**하므로
`capacitor://` 같은 비표준 스킴도 문자열 그대로 매칭되고, 시스템 체크(E013/E014)도 통과합니다.

정규식으로 옮기지 **않은** 것이 오히려 중요합니다 — 정규식이 되면 앵커 실수 하나로
`capacitor://localhost.evil.com` 류가 새어 들어옵니다. 정확 일치 목록이 정답입니다.

---

## 3. 반영 상태

| 대상 | 상태 | 필요한 조치 |
|---|---|---|
| **dev API** `dev-api.turnflow.link` | ✅ **지금 적용됨** | 없음 (runserver 자동 리로드) |
| **prod API** `turnflow-api.clfy.ai.kr` | ⏳ 코드 반영 완료 | 다음 prod 배포 때 함께 |
| **CS 워커 staging** `turnflow-cs.clfy1125.workers.dev` | ✅ **배포 완료** (버전 `543a631b`) | 없음 |
| **CS 워커 prod** `turnflow-cs-production.clfy1125.workers.dev` | ⏳ 설정 반영 완료 | `wrangler deploy --env production` (백엔드 prod 배포와 같은 타이밍에) |
| `turnflow.link/flags.json` | ✅ 원래 됨 (`*`) | 없음 |

### dev 실측 (2026-08-19)

```
$ curl -i -X OPTIONS -H "Origin: https://localhost" \
       -H "Access-Control-Request-Method: GET" \
       -H "Access-Control-Request-Headers: authorization,content-type" \
       https://dev-api.turnflow.link/api/v1/auth/me/

HTTP/1.1 200 OK
access-control-allow-origin: https://localhost          ← ✅
access-control-allow-credentials: true
access-control-allow-headers: ... authorization, content-type ...
access-control-max-age: 86400
vary: origin
```

`capacitor://localhost` 도 동일하게 자기 origin 을 반사합니다.
제보에 찍힌 **그 호출**도 확인했습니다 — `POST /api/v1/track/visit/` preflight 200 →
본 요청 **204**(설계상 silent). 모르는 origin(`https://evil.example`)은 여전히 헤더 없음.

> 참고: 응답에 `vary: origin` 이 붙고 Cloudflare 는 `cf-cache-status: DYNAMIC` 입니다 →
> 엣지가 한 origin 의 응답을 다른 origin 에 재사용할 위험은 없습니다(이것도 실측 확인).

---

## 4. 프론트가 알아야 할 것 3개

### 4-1. ⚠️ **다음 버그** — CS 워커와 백엔드의 환경 짝이 어긋나 있습니다

제보서의 curl 두 개가 서로 다른 환경을 가리키고 있습니다.

| 앱이 부른 곳 | 실제 정체 |
|---|---|
| `turnflow-api.clfy.ai.kr` | **prod** 장고 |
| `turnflow-cs.clfy1125.workers.dev` | **staging** CS 워커 → 백엔드는 `dev-api`, JWT 검증은 **dev 공개키** |

CS 워커는 JWT 를 **환경별 RS256 공개키**로 검증합니다(dev 키 ≠ prod 키). 그래서 CORS 를 뚫고
로그인까지 되면, prod 토큰을 staging 워커에 들고 가는 구성이라 **문의/티켓 API 가 전부 401**
로 떨어집니다. CORS 문제가 아니라 401 이 나오면 이 줄을 먼저 보세요.

짝을 맞춰 주세요.

| 앱 빌드 | API 베이스 | CS 워커 베이스 |
|---|---|---|
| 개발 | `https://dev-api.turnflow.link/api/v1` | `https://turnflow-cs.clfy1125.workers.dev` |
| 프로덕션 | `https://turnflow-api.clfy.ai.kr/api/v1` | `https://turnflow-cs-production.clfy1125.workers.dev` |

### 4-2. live-reload 오리진은 포함되지 않았습니다

`npx cap run android --live-reload` 로 붙이면 웹뷰 origin 이 `https://localhost` 가 아니라
**개발 PC 의 LAN 주소**(`http://192.168.x.x:5173` 등)가 됩니다 → 그 조합은 여전히 CORS 로 막힙니다.
쓰실 계획이면 알려주세요. **dev 서버에만** 사설망 오리진 정규식을 열겠습니다(prod 는 열지 않습니다).
그 전까지는 `cap sync` 후 실기기 빌드로 확인하는 흐름만 동작합니다.

### 4-3. IG 계정 연동(`return_to`)은 **의도적으로** 열지 않았습니다

우리 백엔드는 IG OAuth 의 복귀 주소(`return_to`)와 콜백 `postMessage` 대상 허용목록이 비어
있으면 **CORS 목록을 상속**하는 구조였습니다. 그대로 두면 이번 추가가
"`https://localhost` 로 OAuth 결과를 넘겨도 된다" 까지 **조용히** 늘렸을 겁니다.

CORS 허용은 "이 origin 의 JS 에게 응답을 보여줘도 된다" 이고, `return_to` 는 "OAuth 결과를
이 주소로 넘긴다" 는 훨씬 강한 권한이라 등급이 다릅니다. 그래서 네이티브 origin 은 상속에서
**제외**했습니다(`apps/integrations/oauth_return.py`).

즉 앱에서 IG 연동을 붙일 때는 **별도 설계가 필요합니다** — 커스텀 스킴/App Link 딥링크 복귀냐,
`IG_OAUTH_RETURN_TO_ORIGINS` 에 명시적으로 등록하느냐. 그 시점에 같이 정합시다.
(참고: iOS 는 `instagram.com/oauth/authorize` 를 유니버설 링크로 잡아 IG 앱을 띄우므로
`@capacitor/browser` 로 여는 방식에는 복귀 경로 설계가 반드시 필요합니다.)

---

## 5. 안 한 것과 이유

- **`http://localhost` (스킴 http, 포트 없음)** — 지금 필요 없습니다. Capacitor 6 의 Android
  기본은 `androidScheme: 'https'` 이고 제보 로그도 `https://localhost` 였습니다.
  `androidScheme` 을 바꿀 계획이면 알려주세요.
- **와일드카드 · `CapacitorHttp` · 정규식 허용** — §1, §2 사유.
- **`server.hostname` 을 우리 도메인으로 바꾸는 안** (예: `https://app-native.turnflow.link` 로
  양 플랫폼 origin 통일) — 검토했고 **지금은 권하지 않습니다.** 로그·WAF 에서 origin 이
  자기설명적이 되는 장점은 있지만, 프론트 설정 변경이 필요하고 **hostname 을 바꾸는 순간
  웹뷰 localStorage/IndexedDB 가 origin 변경으로 초기화**됩니다. 출시 후에 하면 사용자
  로그아웃·로컬 상태 유실이 되니, 하려면 **출시 전에** 결정해야 합니다. 이번 건과 별개 논의로 둡니다.

---

## 6. 확인 방법

```bash
# dev — 지금 통과합니다
curl -i -X OPTIONS -H "Origin: https://localhost" \
     -H "Access-Control-Request-Method: GET" \
     -H "Access-Control-Request-Headers: authorization,content-type" \
     https://dev-api.turnflow.link/api/v1/auth/me/

# prod — 배포 후 동일하게 통과해야 합니다
curl -i -X OPTIONS -H "Origin: capacitor://localhost" \
     -H "Access-Control-Request-Method: GET" \
     https://turnflow-api.clfy.ai.kr/api/v1/auth/me/

# CS 워커 staging — 지금 통과합니다 (배포 후 실측: 204 + Access-Control-Allow-Origin 반사)
curl -i -X OPTIONS -H "Origin: https://localhost" \
     -H "Access-Control-Request-Method: POST" \
     https://turnflow-cs.clfy1125.workers.dev/api/tickets
```

`access-control-allow-origin` 이 보낸 origin 과 **같게** 돌아오면 정상입니다.

---

## 7. 회귀 방지

`apps/core/test_native_app_cors.py` — 7개. 두 origin 의 preflight, 401 응답에도 헤더가 붙는지,
모르는 origin 차단, `vary: origin` 존재, IG `return_to` 상속 제외까지 봅니다.
**고장난 버전에 먼저 돌려 검증했습니다**(병합 루프 제거 → 4개 실패 / 상속 제외 제거 → 1개 실패).
