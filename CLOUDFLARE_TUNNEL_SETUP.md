# Cloudflare Tunnel 셋업 — 개발 서버 공개 (ngrok 대체)

목표: **`https://dev-api.turnflow.link` → Cloudflare → cloudflared → docker `web:8000`**
고정 URL 하나로 개발 서버를 공개해서 webhook 테스트·외부 API 콜백·팀 공유에 사용. ngrok 불필요.

```
브라우저 / Meta webhook
  → https://dev-api.turnflow.link        (Cloudflare 엣지에서 TLS 종단, Universal SSL 무료)
  → cloudflared (이 컨테이너가 Cloudflare로 outbound-only 연결, 포트개방 X)
  → http://web:8000                       (docker-compose 의 web 서비스 = Django runserver)
```

> 방식: **remotely-managed(토큰) 터널**. 공개 호스트명/라우팅은 Cloudflare Zero Trust 대시보드에 저장되고,
> 컨테이너는 토큰 하나만 있으면 됨. 로컬 `config.yml` 안 씀.
> 코드/설정 변경은 **이미 다 해뒀습니다.** 아래 [§4 "내가 해야 할 행동"](#4-내가-해야-할-행동-체크리스트) 만 순서대로 하면 됩니다.

---

## 1. 왜 이 구성인가 (요약)

- **ngrok CNAME 거는 방식은 비추천**: ngrok에서 custom domain은 유료(Pay-as-you-go)고, Host/cert 불일치로 잘 깨짐.
- **`dev-api.turnflow.link` (점 없이 하이픈)**: Cloudflare 무료 Universal SSL은 apex + **1단계 서브도메인(`*.turnflow.link`)** 만 커버. `dev-api` 는 점이 없어 **1개 라벨 = 커버됨**. 반면 `dev.api.turnflow.link` 는 점이 2개라 2단계 → 무료 인증서 미커버(유료 ACM/Total TLS 필요). 그래서 `dev-api` 가 정답.
- **cloudflared 를 docker-compose 서비스로**: web 과 같은 네트워크라 `web:8000` 으로 바로 프록시. 포트포워딩/공인IP 불필요(outbound-only, 포트 7844).

---

## 2. 이미 적용된 변경 (코드/설정)

| 파일 | 변경 |
|---|---|
| `docker-compose.yml` | `cloudflared` 서비스 추가 — `cloudflare/cloudflared:latest`, `tunnel --no-autoupdate --protocol http2 run --token ${CLOUDFLARE_TUNNEL_TOKEN}`, **`profiles: ["tunnel"]`** (기본 `make up` 엔 안 뜸), `depends_on: web`. `--protocol http2` 는 Windows/Docker Desktop 에서 QUIC(UDP) 연결이 깨지는 문제 회피용(§6). |
| `.env` | `dev-api.turnflow.link` 를 `ALLOWED_HOSTS` / `CORS_ALLOWED_ORIGINS` / `CSRF_TRUSTED_ORIGINS` 에 추가 + `CLOUDFLARE_TUNNEL_TOKEN=` 자리 추가 |
| `.env.example` | `CLOUDFLARE_TUNNEL_TOKEN` 문서화 |
| `config/settings/local.py` | `SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO","https")` 추가 — 엣지가 TLS 종단 후 평문 HTTP 로 보내므로, 이게 있어야 https 로 연 Swagger/Browsable API/admin 에서 절대URL·CSRF 가 정상 |

> 즉 Django 쪽 `DisallowedHost(400)` / `CSRF(403)` / mixed-content 는 **미리 막아둠**. 남은 건 Cloudflare 대시보드 작업 + 토큰 + 기동뿐.

---

## 3. 사전 확인 1가지

`turnflow.link` 가 **Cloudflare에 "full setup"(네임서버가 Cloudflare) 으로 올라가 있어야** 공개 호스트명 추가 시 DNS(CNAME)가 **자동 생성**됩니다.
- `app.turnflow.link` 가 이미 동작 중 → full setup 일 가능성 높음. (대시보드 → Websites → `turnflow.link` 가 **Active** 면 OK)
- 만약 "partial(CNAME) setup" 이거나 DNS가 외부에 있으면 → 공개 호스트명 추가 후 DNS provider 에 `dev-api` CNAME → `<UUID>.cfargotunnel.com` 을 **수동**으로 넣어야 함(§4-3 참고). 같은 Cloudflare 계정의 zone 이어야 라우팅됨.

---

## 4. 내가 해야 할 행동 (체크리스트)

순서대로. **소요 ~5분.** 코드/설정은 이미 끝나 있음.

### 4-1. 터널 생성 + 토큰 받기 (대시보드)
1. https://one.dash.cloudflare.com → **Zero Trust → Networks → Connectors → Cloudflare Tunnels → Create a tunnel**
2. **Cloudflared** 선택 → 터널 이름 `turnflow-dev-api` → **Save tunnel**
3. "Install and run cloudflared" 화면에 나오는 설치 명령에서 **`--token eyJ...` 의 토큰 문자열만 복사**
   (우리는 docker-compose 로 돌릴 거라 그 화면의 docker/명령 자체는 실행하지 말고 **토큰만** 가져오면 됨)

### 4-2. 토큰을 .env 에 붙여넣기
`turnflow_backend/.env` 의 빈 줄을 채우기:
```bash
CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi... (복사한 토큰)
```
> 토큰은 시크릿입니다. `.env` 는 git 추적 안 됨(`.gitignore`) — 그대로 두면 됩니다. 절대 커밋 금지.

### 4-3. 공개 호스트명(Public Hostname) 추가 — ⚠️ 여기서 Service 값이 핵심
대시보드에서 방금 만든 터널 → **Edit → Published applications(Public Hostname) → Add a public hostname**
- **Subdomain**: `dev-api`
- **Domain**: `turnflow.link`
- **Path**: (비움)
- **Service Type**: `HTTP`
- **Service URL**: **`web:8000`**  ← ⚠️ **`localhost:8000` 아님!**
  (cloudflared 컨테이너 입장에서 `localhost` 는 자기 자신이라 502/1033 납니다. compose 서비스 이름 `web` 로 가야 함)
- **Save**

저장하면 full-setup zone 인 경우 **DNS CNAME 이 자동 생성**됩니다.
(partial/외부 DNS 면: DNS 에서 `dev-api.turnflow.link` → `<터널UUID>.cfargotunnel.com` proxied CNAME 수동 추가. UUID 는 터널 상세에 표시.)

### 4-4. cloudflared 기동 (터미널, `turnflow_backend` 폴더에서)
```powershell
# 개발 스택이 이미 떠 있다면 cloudflared 만 추가 기동:
docker compose --profile tunnel up -d cloudflared

# 또는 전체를 tunnel 포함해서:
docker compose --profile tunnel up -d
```
> 기본 `make up` / `docker compose up` 에는 `tunnel` profile 이 빠져 cloudflared 가 안 뜹니다. 의도된 동작(토큰 없을 때 안 깨지게).

### 4-5. 확인
```powershell
docker logs instagram_backend_cloudflared --tail 30   # "Registered tunnel connection" 4개 뜨면 OK
```
```powershell
curl https://dev-api.turnflow.link/api/v1/healthz       # 200 + JSON
```
- Swagger: https://dev-api.turnflow.link/api/docs/
- (대시보드 터널 상태가 **HEALTHY** 인지도 확인)

**끝.** 이후 `dev-api.turnflow.link` 는 컨테이너가 떠 있는 한 고정 URL 입니다.

---

## 5. (선택) Instagram OAuth / Webhook 도 ngrok → dev-api 로 이전

ngrok 을 **완전히 끊으려면** IG OAuth redirect 와 webhook 콜백도 옮겨야 합니다. **Meta 재등록은 자동이 안 되고 수동**입니다(아무것도 자동 마이그레이션 안 됨).
1. `.env` 에서 `INSTAGRAM_REDIRECT_URI` 를 주석 처리된 dev-api 값으로 교체:
   `https://dev-api.turnflow.link/api/v1/integrations/instagram/connect/callback/`
   → `docker compose restart web`
2. **Meta 개발자센터**(developers.facebook.com) → 앱 → **"유효한 OAuth 리디렉션 URI"** 에 위 URL **정확히 동일**하게 추가(끝 슬래시까지, 쿼리스트링 X).
3. Webhook 콜백 URL 도 새 https 엔드포인트로 바꾸면 Meta 가 `hub.challenge` 검증을 다시 보냄 — 우리 엔드포인트가 verify token 으로 응답해야 함. 필요시 구독 필드 재구독.
4. App Domains 에 `turnflow.link` 추가가 필요할 수 있음.

> 지금 당장 안 옮겨도 됩니다. `.env` 의 ngrok 값은 그대로 두면 기존 IG 흐름이 안 깨집니다. dev-api 는 그동안 webhook/API 테스트·팀 공유용으로 쓰면 됨.

---

## 6. 흔한 장애 (검증 완료)

| 증상 | 원인 / 해결 |
|---|---|
| **HTTP 400 DisallowedHost** | 호스트가 `ALLOWED_HOSTS` 에 없음 → **이미 추가함**. 그래도 나면 `docker compose restart web`(설정은 부팅 시 로드). |
| **CSRF 403** (admin 로그인/Browsable API POST) | `CSRF_TRUSTED_ORIGINS`(스킴 포함 `https://...`) 누락 → **이미 추가함** + `SECURE_PROXY_SSL_HEADER` **추가함**. restart 후 확인. |
| **502 / Error 1033** | cloudflared 미동작(가장 흔함, 재부팅 후 죽음) / 공개 호스트명 ingress 없음 / Service 가 `localhost:8000` 로 잘못됨 / web 미기동. → `docker logs instagram_backend_cloudflared`, Service=`web:8000` 재확인, `restart: unless-stopped` 라 docker desktop 켜지면 자동 복귀. |
| **524 / 100초 타임아웃** | Cloudflare 무료~Business 는 proxy read timeout **100초 고정**. 100초 넘는 동기 작업(대용량 export, 인라인 LLM)은 **Celery 로 비동기 처리 후 202 반환**. (DM/webhook 페이로드는 수백 B 라 무관) |
| **413 업로드 실패** | 무료 플랜 요청 본문 ~100MB 한도. 미디어는 R2 로 오프로드 중이라 무관. |
| **엣지 연결 실패 `tls: no application protocol` (QUIC)** | Windows/Docker Desktop 등에서 QUIC(UDP/7844) 경로가 깨질 때. cloudflared 가 4개 엣지 IP에 계속 retry 실패. → compose command 에 **`--protocol http2`** 추가(이미 적용함). 로그에 `Registered tunnel connection ... protocol=http2` 4줄 뜨면 정상. |
| **간헐적 502 (요청 절반만 성공)** | **같은 토큰으로 커넥터가 2개 이상** 떠 있을 때. 흔한 사례: docker 커넥터 + 과거에 `cloudflared.exe service install <토큰>` 으로 깔린 **호스트 Windows 서비스**가 동시에 같은 ingress(`http://web:8000`)를 받음 → 호스트에선 `web` resolve 불가라 그 커넥터만 502. Cloudflare 가 두 커넥터를 번갈아 라우팅 → 50% 502. → 한쪽만 남길 것. docker 로 갈 거면 호스트 서비스 제거: 관리자 PowerShell 에서 `& "C:\Program Files (x86)\cloudflared\cloudflared.exe" service uninstall`. (대시보드 터널 상세의 커넥터 개수로도 중복 확인 가능) |
| **첫 셋업 직후 502 (라우팅 안 잡힘)** | 커넥터가 **Public Hostname 추가 _전_** 에 등록되면 엣지 라우팅이 502 로 굳음. → 호스트명 추가 후 `docker restart instagram_backend_cloudflared` 로 재등록. |

---

## 7. 롤백 (ngrok 로 복귀, ~1분)

코드/Django 설정은 그대로 둬도 무방(추가일 뿐 ngrok 도 계속 동작). cloudflared 만 내리면 됨:
```powershell
docker compose --profile tunnel stop cloudflared
docker compose --profile tunnel rm -f cloudflared
```
ngrok 을 다시 쓰던 방식대로 띄우면 끝. (IG redirect 를 dev-api 로 바꿨다면 §5 를 ngrok 값으로 되돌리고 Meta 도 원복.)

---

## 8. 참고 (검증 출처)
- Cloudflare Tunnel(remotely-managed/토큰): run-parameters, create-remote-tunnel, routing-to-tunnel/dns 문서
- Universal SSL 커버리지(1단계 서브도메인): ssl/edge-certificates/universal-ssl/limitations
- 524/100초, 100MB, websocket 100초: cloudflare 5xx-errors/error-524, network/websockets
- Meta OAuth redirect_uri 정확 일치 + webhook 재검증: developers.facebook.com (수동 재등록)
