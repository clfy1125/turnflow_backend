# CLAUDE.md

이 파일은 Claude Code가 이 저장소에서 작업할 때 참조할 지침입니다.
작업 시작 전 반드시 숙지하고, 관련 변경이 생기면 업데이트하세요.

---

## 1. 프로젝트 개요

**TurnFlow Backend (Instagram Service Backend)**
Instagram Business 계정의 댓글 수집/분류, 자동 DM 발송, 키워드 기반 자동화, LLM 기반 컨텐츠 분석을 제공하는 SaaS 백엔드.

- **서비스 모델**: 멀티테넌시(Workspace) 기반 SaaS
- **요금제**: Free / Basic / Pro (+운영용 Admin) — DB-driven(`SubscriptionPlan.features`), 사용량 제한 있음
- **결제**: 토스페이먼츠 빌링키 정기결제 — **PG 스케줄러 없음, 우리 Celery(`billing.process_due_renewals`)가 매월 승인 호출**. 프로는 카드 등록 시 1개월 무료 체험(제휴코드 +1개월, 1인 1회 `trial_used_at`), 가입 시점 가격 스냅샷(그랜드파더링)
- **1차 MVP 기능**: IG 계정 연동, 댓글 수집/분류, 자동 DM, 템플릿/시나리오, 지표 대시보드
- **2차 확장**: LLM 기반 의도 분석, 릴스/게시물 컨텐츠 분석, A/B 테스트, CRM/웹훅

### ⭐ 제품 목표 — DM 캠페인 이전 (변경 금지, 판단이 갈릴 때의 기준)

**타 도구(매니챗·인포크링크·NHN 소셜비즈 등)의 DM 캠페인을 하나도 빠짐없이 이전시키는 것이
목표다.** 시간이 더 걸려도 좋다. 회수율(찾아낸 캠페인 수)이 속도·비용·호출 수보다 우선한다.

- 속도/비용 최적화를 위해 **탐색을 조기에 포기하는 설계는 이 목표에 어긋난다.**
  (실제 사고: "댓글러 3명 조회해서 0건이면 게시물 포기" → @highestlevel33 에서 연구 실측
  313건 중 206건만 나왔다. 253개 게시물이 3명만 보고 버려졌고, 예산은 3,189이나 남아 있었다.)
- 정밀도(없는 캠페인을 만들지 않기)와 충돌하면 **회수는 하되 등급으로 가른다** — 후보를
  버리지 말고 `needs_review`/`confirm_required` 로 내려 사용자가 판단하게 한다.

**⭐ 소진의 기준 — 검수로 넘기기 전에 두 축을 다 파라 (2026-08-17 제품 결정)**

"조금 파보고 안 나오면 사람이 검수하세요" 는 **우리가 덜 한 일을 사람에게 떠넘기는 것**이다.
검수 목록에 올리기 전에 반드시 둘 다 소진할 것:

1. **댓글 축** — 제일 첫 댓글까지 넘긴다 (`EXHAUSTIVE_COMMENT_PAGES`).
   `/comments` 는 **최신순 고정**이라 1페이지는 캠페인이 끝난 뒤 댓글러다. 실측: 댓글
   10,050개 게시물의 1페이지가 2024-12~2026-02 인데 게시물은 2024-02 — 캠페인 시기
   댓글러는 **200페이지 뒤**에 있었다.
2. **대화 축** — 그 사람 대화의 처음까지 넘긴다 (`CONVO_DEEP_MAX_PAGES`).
   ⚠️ "Meta 는 대화당 최근 ~20통만 준다" 는 **틀린 결론이었다.** 중첩 필드
   (`messages.limit(25)`)로 받고 그 안의 `paging.next` 를 우리가 버렸던 것이다.
   실측(2026-08-17): 중첩 13통(26분치) → `/{conversation_id}/messages` 엣지 페이징
   43통(3년 6개월치, 2022-10 까지).

그러고도 흔적이 없으면 **그건 인정하고** 사람에게 넘긴다. 그때가 정당한 검수다.

관련 함정 — **게이트 DM 을 "찾았다" 로 세지 말 것.** 게이트("팔로우 확인")는 전 게시물에
같은 문구가 나가므로 이 게시물의 근거가 못 된다. `if tmpl:` 로 판정했다가 게이트 2명이
잡힌 것 때문에 위 두 축을 **통째로 건너뛴** 사고가 있었다 → `_found_offer()` 로 판정한다.

비용 걱정으로 이 상한을 깎지 말 것. 색인 전수 대조(`outbound_from_index`)는 **Graph 호출
0** 이고, 비싼 경로(대화 페이징)는 "글·댓글이 캠페인 확실 & 오퍼 미발견" 인 소수 게시물에만
걸린다. 상한을 깎아야 할 이유가 생기면 **회수율을 같이 측정**할 것.

**⭐ 비싼 것은 한 번만 산다 — 재사용 (2026-08-17)**

깊게 파는 대신 **같은 것을 두 번 조사하지 않는다.** 이게 상한을 깎지 않고도 쿼터를 지키는 방법이다.

| 무엇 | 실측 비용 | 어디에 저장 | 무효화 |
|---|---|---|---|
| 발신함 색인 | 122분·1,577페이지·88,899건 | 직전 잡 `stage_data.outbox` | `OUTBOX_REUSE_HOURS`(72h) |
| 게시물 판정 | 19.6콜/게시물 | `IGPostAnalysis` (영구) | `cache.RULES_VERSION` |
| 댓글 끝까지 페이징 | 1만 개 게시물 = 200페이지 | `IGPostAnalysis.probe_pool` (id·시각만) | 위와 같음 |

- **재조사하지 않는 것** = 자동채택된 것 + 글 점수가 캠페인 컷 미만인 것. 캡션은 안 바뀌고
  끝난 캠페인도 안 바뀐다. `needs_review` 와 "글은 강한데 문구 못 살림" 은 **끝난 게 아니라**
  다시 판다.
- **댓글이 늘면 다시 본다** (`COMMENT_GROWTH_*`) — 새 댓글러가 받았을 수 있다.
- ⚠️ **판정 규칙을 고치면 `cache.RULES_VERSION` 을 올릴 것.** 안 올리면 버그가 있던 버전의
  결론이 영구히 남는다. 실제로 "게이트 하나 찾으면 탐색 종료" 버그가 있었고, 그때의
  '문구 없음' 판정이 영원히 캐시될 수 있었다.
- ⚠️ **영구 캐시에 타인의 DM·댓글 원문을 넣지 말 것.** 7일 파기(`purge_dm_migration_raw`)의
  대상이다. `IGPostAnalysis` 는 우리가 만든 판정 수치만 담고, 문구가 필요한 재사용은 이미
  영구 보관되는 `DMCampaignCandidate` 에서 가져온다(`cache.texts_for`).
- 지표를 "정밀도"로 바꾸는 리팩터링을 할 때는 **회수율을 같이 측정**할 것. 소형 계정에서
  100% 가 나와도 대형 계정에서 무너질 수 있다(9차 정밀도판이 소형 3계정에서만 검증됐다).

**단, prod 의 다른 사용자에게 피해를 주면 안 된다** — 이게 유일한 상한이다.
- `ai_jobs` 큐는 라이브 경로(`run_spam_filter_check` = 댓글→DM)와 **공유**한다. 긴 분석은
  슬라이스로 쪼개 슬롯을 오래 물지 말 것(`dm_migration/pipeline.py` SLICE_SECONDS).
- Meta Graph 호출량은 **앱 단위 쿼터**를 공유한다 — 한 계정을 깊게 파는 것이 다른 워크스페이스의
  댓글 수집·DM 발송을 굶길 수 있다. 페이서(`RateLimiter`)·레이트리밋 pause 를 우회하지 말 것.
- 대량 재분석은 **한 번에 한 계정씩**. 배포·마이그레이션과 겹치지 않게 할 것.

**외부 제약 (반드시 숙지)**
- Instagram/Meta Graph API: OAuth(단기/장기 토큰), Webhook 구독/검증, 권한 스코프 승인 흐름 필요
- **24시간 메시징 정책**: DM 발송은 사용자와의 최근 상호작용 창(24h) 제약. 정책 검증 레이어 필수
- 운영 승인 전에도 개발/테스트 가능하도록 `INSTAGRAM_MOCK_MODE` 환경변수로 Mock 모드 분기

---

## 2. 기술 스택

| 영역 | 스택 |
|---|---|
| Framework | Django 5.0 + Django REST Framework 3.14 |
| DB | PostgreSQL 16 |
| Cache/Queue | Redis 7 + Celery 5.3 |
| 인증 | JWT (`djangorestframework-simplejwt`), Google OAuth, Session |
| API 문서 | drf-spectacular (Swagger UI / ReDoc) |
| 암호화 | `cryptography` (IG 토큰 저장용) |
| GeoIP | `geoip2` + GeoLite2-Country.mmdb |
| LLM | `openai` SDK + `httpx` |
| 결제 | 토스페이먼츠 빌링(자동결제) — httpx 직접 연동 (`apps/billing/toss_service.py`) |
| 리포트 렌더 | Jinja2 + Chart.js 인라인 → **자기완결 HTML 1파일**(다운로드 산출물) |
| 테스트 | pytest + pytest-django + factory-boy + faker |
| 포매터/린터 | Black, isort, Ruff, flake8 |
| 컨테이너 | Docker + Docker Compose |
| Python | 3.11 |

---

## 3. 프로젝트 구조

```
turnflow_backend/
├── apps/                       # Django 앱
│   ├── core/                   # 미들웨어(RequestID, Logging), 커스텀 예외, healthz
│   ├── authentication/         # User(email 로그인) + JWT + Google OAuth
│   ├── workspace/              # Workspace(테넌트) + Membership + permissions
│   ├── billing/                # 요금제/구독/토스 빌링 (toss_service, toss_flows, toss_views, dm_limits) + Celery 갱신 배치
│   │                           #   consent.py = 결제 전 고지·유료전환 2차 동의 **판정 단일 소스**
│   │                           #   (프론트 플래그·D-14/D-3 메일·과금 차단 게이트가 공유) + consent_views.py
│   ├── integrations/           # Instagram OAuth/토큰 암호화(encryption.py)/Webhook
│   ├── pages/                  # 페이지/게시물/DM 관련 뷰 (multi_views, image_views, stats, aiviews)
│   ├── ai_jobs/                # LLM 작업 큐 + services(llm_client, model_router, prompt_builder)
│   ├── analytics/              # 랜딩 방문 추적(LandingVisit) + 가입 어트리뷰션(SignupAttribution) — POST /api/v1/track/visit/ (공개·silent 204), 채널 파생 단일 소스 channels.derive_channel()
│   ├── insta_reports/          # 인스타 성장 리포트(프로 전용·IG 계정당 월1회) — 공개 데이터로 HTML 리포트 생성.
│   │                           #   pipeline/(랩 이식: 수집→지표→샘플→Gemini 피처→집계→DeepSeek 합성→검증→렌더) +
│   │                           #   quota.py + progress.py. 산출물=자기완결 HTML. 마운트: /api/v1/insta-reports/
│   │                           #   auth/ = 어드민 2단계 로그인(TOTP) — 일반 로그인과 **분리된 관문**.
│   └── admin_api/              # 백오피스(어드민) 전용 API — 신원/대시보드(overview + 운영/마케팅: dashboard_ops·dashboard_marketing, 임계값=dashboard_constants.py)/회원/워크스페이스/페이지/자동DM 모니터링/레퍼럴 코드/마케팅 채널링크(marketing/channel-links — UTM 링크 서버 저장 CRUD, MarketingChannelLink·url/channel 서버 계산) (serializers/, views/ 패키지 + AdminActionLog 감사로그). 마운트: /api/v1/admin/
│                               #   snapshot_rosters.py = 전체 현황 타일의 **모수 쿼리 단일 소스** —
│                               #   대시보드 타일(_snapshot/_trial_now)과 명단(views/snapshot.py)이 공유해야
│                               #   "타일 숫자 == 명단 count" 가 성립한다. 조건 복제 금지
│                               #   auth/ = 어드민 2단계 로그인(TOTP) — 일반 로그인과 **분리된 관문**.
│                               #   totp.py 가 인증 판정 단일 소스(재사용 방지 포함), gate.py + middleware 가
│                               #   /api/v1/admin/** 에 어드민 토큰(adm 클레임)을 강제한다.
│                               #   ⚠️ ADMIN_MFA_ENFORCED 기본 False — 전원 등록 전에 켜면 관리자가 잠긴다
├── config/                     # Django 프로젝트 설정
│   ├── settings/               # base.py / local.py / prod.py
│   ├── urls.py                 # 루트 URL (admin, api/v1, swagger, redoc)
│   ├── api_urls.py             # /api/v1/ 아래 라우팅
│   ├── celery.py               # Celery 앱
│   └── wsgi.py / asgi.py
├── docs/                       # 📄 모든 문서 (2026-08-10 루트에서 이관) — 색인: docs/README.md
│   ├── frontend/               #   API 계약·연동 가이드 (프론트/어드민 콘솔팀 전달분)
│   ├── ops/                    #   운영·인프라·보안·배포 (배포는 docs/ops/배포방법.md 부터)
│   ├── system/                 #   백엔드 내부 동작 설명 (DM 라이프사이클·스팸필터·오류정책)
│   ├── archive/                #   완료·대체된 일회성 문서 (최신 아님, 이력 참고용)
│   └── legal/                  #   법무 자료 (수정 금지)
├── api-mcp/                    # 사내 API 문서 검색용 MCP 서버 (별도 파이썬 패키지)
├── geoip/                      # GeoLite2 DB (런타임 다운로드)
├── media/                      # 사용자 업로드 파일
├── templates/
├── scripts/
├── test/                       # 임시 테스트/실험 코드
├── docker-compose.yml          # 로컬 개발(web + db + redis + celery_worker)
├── docker-compose.prod.yml     # 프로덕션 (gunicorn)
├── Dockerfile                  # python:3.11-slim 기반
├── entrypoint.sh               # DB 대기 → migrate → (prod: collectstatic) → exec
├── Makefile                    # make <command> 단축 명령
├── requirements.txt
├── pyproject.toml              # Black/Ruff/isort/pytest 설정
├── manage.py
└── 프로젝트 지침서.md          # 제품 요구사항 원본 (참조용)
```

---

## 4. Docker / 개발 서버 실행

**로컬 스택**: `web`, `celery_worker`, `db`(PostgreSQL 16), `redis`(Redis 7). 모두 `docker-compose.yml` 한 개로 구동.

- `DJANGO_SETTINGS_MODULE=config.settings.local`
- `web`: `python manage.py runserver 0.0.0.0:8000` (소스는 `./:/app` 바인드 마운트 → 코드 수정 즉시 반영)
- `celery_worker`: `celery -A config worker -l info`
- DB/Redis health check 통과 후 web 시작
- `entrypoint.sh`가 PostgreSQL 대기 → `migrate --noinput` 자동 실행

**자주 쓰는 명령** (Makefile 기준):

```bash
make build          # 이미지 빌드
make up             # 포그라운드 실행
make up-d           # 백그라운드 실행
make down           # 중지
make down-v         # 중지 + 볼륨 삭제(DB 초기화)
make logs-web       # 웹 로그
make logs-celery    # Celery 로그
make bash           # web 컨테이너 bash 진입
make shell          # django shell_plus
make db-shell       # psql 접속
make migrate
make makemigrations
make test           # pytest
make lint / lint-fix
make format         # black + isort
make init           # 빌드 + 실행 + 마이그레이션 한 번에
```

**서비스 URL**
- API: `http://localhost:8000/api/v1/`
- Health: `http://localhost:8000/api/v1/healthz`
- Admin: `http://localhost:8000/admin`
- Swagger: `http://localhost:8000/api/docs/`
- ReDoc: `http://localhost:8000/api/redoc/`
- OpenAPI JSON: `http://localhost:8000/api/schema/`

**환경변수**: `.env`에서 로드 (`python-decouple`). `.env.example` 참고. 주요 변수:
- DB/Redis 접속 (`DB_*`, `REDIS_*`)
- `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`
- Meta/Instagram: `META_APP_ID`, `META_APP_SECRET`, `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_REDIRECT_URI`, `INSTAGRAM_WEBHOOK_VERIFY_TOKEN`, `INSTAGRAM_MOCK_MODE`
- LLM: `LLM_URL`(dev=`https://llm.clfy.ai.kr` CF 경유 / **prod=`http://litellm-proxy:4000` 내부**), `LLM_API_KEY`,
  `LLM_TLS_VERIFY`(dev만 False — CF Origin 인증서 수용), `LLM_STREAMING`(기본 True — 끄면 dev 리뉴얼이 CF 524 로 죽는다)
- 토스페이먼츠: `TOSS_SECRET_KEY`, `TOSS_CLIENT_KEY`, `TOSS_API_BASE`, `TOSS_DEV_CARD_AUTH_ENABLED`(dev 전용 카드입력 헬퍼 — 운영 반드시 False)
- 인스타 리포트: `APIFY_API_KEY`(공개 조회수 수집 — 인사이트 권한 미승인 대체), `GEMINI_API_KEY`(영상 피처),
  `DEEPSEEK_API_KEY`(문장 합성), `INSTA_REPORT_FAKE_MODE`(dev 전용 오프라인 모드 — 운영 반드시 False)
- 기타: `PIXABAY_API_KEY`, `GOOGLE_CLIENT_ID`

---

## 5. 아키텍처 원칙 (반드시 준수)

1. **API-First**: 프론트 없이 Swagger/Postman만으로 전 기능 검증 가능해야 함.
2. **멀티테넌시**: 모든 데이터는 `Workspace(UUID)` 단위로 논리 분리. 쿼리 작성 시 워크스페이스 필터 누락 금지.
3. **비동기 처리**: 댓글 수집, 분류, DM 발송, 지표 집계 등 시간이 걸리거나 외부 API에 의존하는 작업은 **Celery 태스크**로 분리. 뷰에서 동기 호출 금지.
4. **Idempotency / 재처리**: 웹훅 수신 시 이벤트 키/해시로 중복 방어 (`EventInbox` 패턴).
5. **관측성**: `RequestIDMiddleware`가 요청별 `X-Request-ID` 부여. 4xx/5xx는 `LoggingMiddleware`가 자동 로깅. 새 로그 찍을 때 `request.id` 함께 남길 것.
6. **보안**: IG/Meta 토큰은 `apps/integrations/encryption.py`로 암호화 저장. 평문 저장/로깅 금지. 개인정보 최소 수집.
7. **정책 분리**: DM 발송 전 24h 정책 검증 레이어를 통과하도록 구현. Mock 모드에서도 동일 로직 타도록.
8. **요금제 제한**: `PlanLimitExceededError`(apps/core/exceptions.py) 사용 → 자동으로 HTTP 429 + `PLAN_LIMIT_EXCEEDED` 응답.

---

## 6. API / URL 규칙

- 버전 prefix: **`/api/v1/`** (변경 금지, 새 버전은 `v2/` 등 신규)
- 인증: JWT (`Authorization: Bearer <token>`), 기본 permission `IsAuthenticated`
- JWT: Access 1d, Refresh 7d, 회전 + 블랙리스트
- 에러 포맷 통일 (`apps/core/exceptions.custom_exception_handler`):
  ```json
  { "success": false, "error": { "code": 400, "message": "...", "details": { ... } } }
  ```
  → 응답을 직접 만들 때도 이 포맷 유지. 새 예외 타입 추가 시 exception handler에 분기 추가.
- 페이지네이션: `PageNumberPagination`, PAGE_SIZE=20
- 필터: `DjangoFilterBackend` + `SearchFilter` + `OrderingFilter` 기본 탑재

**URL 라우팅 (config/api_urls.py)**
- `auth/` → authentication
- `(빈)` → workspace, billing
- `integrations/` → IG 연동
- `pages/` → 페이지/게시물/DM
- `ai/` → LLM 작업
- `insta-reports/` → 인스타 성장 리포트 (integrations 라우터가 `instagram` 을 ViewSet prefix 로
  쓰고 있어 `integrations/instagram/reports/` 는 pk="reports" 로 먹힌다 → 별도 prefix)

---

## 7. API 문서화 규칙 (CRITICAL — 프로젝트 지침서 0-7 강제)

**모든 `@extend_schema`에 반드시 포함할 것**:

1. `summary` — 30자 이내 한 줄 요약
2. `description` — 목적, 사용 시나리오/타이밍, 인증 요구사항, 비즈니스 로직, 주의사항
3. `request` — POST/PUT/PATCH면 스키마 + 필드 설명 + 필수/선택 + 검증 규칙 + 예시
4. `responses` — **200/201, 400, 401, 403, 404, 500 모두 문서화**
5. `examples` — curl 또는 JavaScript fetch 요청 예시 + JSON 응답 예시

**금지**
- ❌ summary만 있고 description 생략
- ❌ "사용자 생성" 같은 의미 없는 요약
- ❌ 에러 응답 누락
- ❌ 인증 요구사항 생략

프론트 개발자가 **이 문서만 보고 즉시 구현할 수 있어야** 한다. 템플릿과 체크리스트는 `프로젝트 지침서.md` 섹션 0-7 참고.

**스키마 후처리 훅**: `apps/pages/openapi.postprocess_block_data_schema`가 등록돼 있음 — pages 앱의 block 관련 스키마 수정 시 여기 확인.

---

## 8. 코드 스타일

- **Python 3.11**, `line-length = 100`
- **Black** (target py311, migrations 제외)
- **isort** profile=black, `known_first_party = ["apps", "config"]`
- **Ruff** 활성 규칙: E, W, F, I, B, C4, UP / 무시: E501, B008, C901
- **Flake8**: pre-commit에서 추가 검사
- **Pre-commit 훅**: trailing-whitespace, end-of-file-fixer, check-yaml/json/toml, merge-conflict, debug-statements, black, isort, ruff(--fix), flake8

**관례**
- 모델: `class Meta`에 `db_table`, `verbose_name[_plural]`, `ordering`, `indexes` 명시 (예: `apps/workspace/models.py`)
- UUID PK 사용 가능 (`Workspace`가 UUID), 그 외엔 `BigAutoField` 기본
- Custom User: `AUTH_USER_MODEL = "authentication.User"` — 항상 `get_user_model()`로 참조
- 모델 파일 상단에 module-level docstring 권장
- 타임존: `Asia/Seoul`, 언어: `ko-kr`, DB는 `USE_TZ=True` UTC 저장
- 캐시 키 / Celery 큐 이름은 기능별로 네임스페이스 (예: billing 큐)

**커밋 메시지 (Conventional Commits)**
```
feat:     새 기능
fix:      버그 수정
docs:     문서
style:    포맷팅
refactor: 리팩토링
test:     테스트
chore:    빌드/설정
```

**브랜치**
- `main`: 프로덕션
- `develop`: 개발 통합
- `feature/*`, `hotfix/*`

---

## 9. 테스트

- `pytest` + `pytest-django` (`DJANGO_SETTINGS_MODULE=config.settings.local`)
- 커버리지: `--cov=apps` (term + html + xml 자동)
- 테스트 파일 패턴: `test_*.py`, `*_test.py`, `tests.py`
- 팩토리: `factory-boy` + `faker` 사용
- 새 기능 추가 시 해당 앱의 `tests/` 또는 `tests.py`에 테스트 동반

```bash
make test                                 # 전체
docker compose exec web pytest apps/core/ # 특정 앱
make test-cov                             # HTML 커버리지 리포트
```

---

## 10. Celery / 백그라운드 작업

- 워커: `celery -A config worker -l info`
- 브로커/백엔드: Redis (`/0` DB)
- 캐시: Redis `/1` DB (`django_redis`)
- 태스크 등록 파일: `apps/*/tasks.py` (autodiscover)
- 기존 정기 스케줄 (`CELERY_BEAT_SCHEDULE` in base.py) — billing 갱신 파이프라인:
  - `billing.process_due_renewals` — 10분 (갱신 도래 구독 과금 디스패치 — **토스 정기결제의 심장**)
  - `billing.reconcile_pending_payments` — 30분 (모호 실패 PENDING 결제 확정)
  - `billing.check_missed_payments` — 매시간 (갱신 파이프라인 고장 감시)
  - `billing.handle_grace_period_expiry` / `handle_cancelled_expiry` / `handle_trial_expiry` — 매시간
  - `billing.handle_pause_expiry` — 매시간 (리텐션 정지 만료 → 자동 유료 재개 + 갱신 과금)
  - `billing.notify_pause_resume_reminder` — 매일 09:30 KST (정지 재개 3일 전 사전 고지 메일)
  - `billing.notify_conversion_consent` — 매일 10:30 KST (유료전환 2차 동의 D-14/D-3 메일. **2026-08-10 제품 결정으로 dormant** — `CONVERSION_SECOND_CONSENT_ENABLED=False` 기본이라 즉시 no-op. core 0014 시드)
  - `billing.send_winback_emails` — 매일 10:00 KST (해지 후 복귀 유도, `WINBACK_ENABLED` 게이트·기본 dormant)
  - `insta_reports.sweep_stale` — 30분 (running 에 박힌 리포트 잡 실패 확정 — 동시생성 1건 제한 해제)
  - `insta_reports.purge_caches` — 매일 04:40 KST (90일 경과 AI 캐시 정리, 리포트 파일·집계는 보관)
  - `billing.snapshot_daily_metrics` — 매일 00:20 KST (일별 구독 상태/MRR/결제 코호트 스냅샷 적재 — 어드민 유지·해지 분석 P-4, 멱등 upsert, core 0010 시드)
- 태스크 타임 리밋: 30분 (`CELERY_TASK_TIME_LIMIT`)
- 큐: billing 등 기능별 `options: {queue: "..."}` 지정
- `reports` 큐 = 인스타 성장 리포트 전용(1건 13~18분). prod 는 `celery_reports` 워커
  (`-Q reports -c 2 --max-tasks-per-child=1`)가 소비 — child 재활용이 없으면 영상 임시파일이 누적된다.

**주의**: Celery Beat 전용 컨테이너는 현재 compose에 없음 — 주기 배치가 필요하면 Beat를 별도 서비스로 추가하거나 `django-celery-beat`로 전환 고려.

---

## 11. 모델 핵심 엔티티 (지침서 0-4)

현재 구현된 주요 모델 (수정/확장 시 마이그레이션 필수):
- `authentication.User` — email 기반, `full_name` 추가
- `workspace.Workspace` — UUID PK, slug, owner(FK User, PROTECT), plan(starter/pro/enterprise)
- `workspace.Membership` — Owner/Admin/Member 역할
- `billing.*` — 구독/결제/토스 빌링 (빌링키는 Fernet 암호화 `_encrypted_toss_billing_key` + SHA-256 해시 컬럼으로 웹훅 역조회)
- `integrations.*` — IG 계정 연결, 토큰 암호화
- `pages.*` — 게시물/블록/통계
- `ai_jobs.*` — LLM 작업 큐

지침서에 있으나 아직 미구현/확인 필요한 것: `IGMedia`, `IGComment`, `CommentClassification`, `KeywordRule`, `AutomationScenario`, `DMSendAttempt`, `DMTemplate`, `EventInbox`, `MetricDaily`/`MetricEvent` — 작업 전에 현재 상태를 `git grep` / 모델 파일로 확인할 것.

---

## 12. 작업 시 체크리스트

**코드 변경 전**
- [ ] `프로젝트 지침서.md`의 해당 섹션 확인
- [ ] 관련 앱의 기존 모델/시리얼라이저/뷰 읽기
- [ ] 멀티테넌시: Workspace 필터링 누락 없는지
- [ ] 24h 정책 / Mock 모드 분기 필요한지

**API 추가 시**
- [ ] `@extend_schema` 필수 필드 전부 작성 (섹션 7)
- [ ] 에러 응답 포맷 통일 (섹션 6)
- [ ] 요금제 제한 필요하면 `PlanLimitExceededError` 사용
- [ ] 시리얼라이저 검증 + 타입 힌트

**커밋 전**
- [ ] `make format` (black + isort)
- [ ] `make lint-fix` (ruff)
- [ ] `make test` 통과
- [ ] pre-commit 훅 통과
- [ ] 마이그레이션 파일 포함 (모델 변경 시)

**외부 API (Meta/Instagram/토스페이먼츠/LLM) 건드릴 때**
- [ ] 토큰/키 평문 저장·로깅 금지
- [ ] 실패 재시도 + 타임아웃 설정
- [ ] Mock 모드에서도 동작하도록 분기
- [ ] Webhook이면 idempotency 키 저장

---

## 13. 참고 문서

**📄 문서는 전부 `docs/` 아래에 있습니다 — 색인은 `docs/README.md`.**
(2026-08-10 정리: 루트에 흩어져 있던 47개를 `git mv` 로 용도별 이관. 루트에 남긴 건
`README.md` · `CLAUDE.md` · `프로젝트 지침서.md` 셋뿐. 새 문서도 해당 폴더에 만들 것.)

| 폴더 | 용도 |
|---|---|
| `docs/frontend/` | API 계약·연동 가이드 (프론트/어드민 콘솔팀 전달분) |
| `docs/ops/` | 운영·인프라·보안·배포 — **배포는 `docs/ops/배포방법.md` 부터** |
| `docs/system/` | 백엔드 내부 동작 설명 |
| `docs/archive/` | 완료·대체됨 (최신 아님, 이력 참고용) |
| `docs/legal/` | 법무 자료 (수정 금지) |

저장소 내 문서:
- `프로젝트 지침서.md` — 제품 요구사항 원본 (이 파일의 상위)
- `README.md` — 일반 개발자용 셋업 가이드
- `docs/README.md` — **문서 색인** (아래 목록의 요약판)
- `docs/ops/배포방법.md` — prod 배포 절차. ⚠️ 수동 `compose build`/`up -d` 금지 이유(48초 DB 블립,
  은퇴한 `celery_beat` 기동 시 주기잡 이중 발사) 포함
- `docs/frontend/AI_PAGE_GENERATION_GUIDE.md` — AI 페이지 생성 API 프론트엔드 연동 가이드 (4단계 흐름 + category 파라미터)
- `docs/ops/INSTAGRAM_OAUTH_FLOW.md` — IG OAuth 플로우
- `docs/ops/INSTAGRAM_TEST_GUIDE.md` — IG 테스트 가이드
- `docs/ops/CLOUDFLARE_TUNNEL_SETUP.md` — 개발 서버 공개(고정 URL `dev-api.turnflow.link`) Cloudflare Tunnel 셋업 (ngrok 대체)
- `docs/system/AUTODM_DELIVERY_LIFECYCLE.md` (+ `.html`) — 자동 DM 발송 라이프사이클: 웹훅 수신→발송 확정→실패 처리·무손실 하드닝(v3.10.1)
- `docs/frontend/TOSS_BILLING_FRONTEND.md` — 토스 빌링 프론트 연동 가이드 (SDK v2 카드등록 → prepare/confirm, 플랜 표시, 체험/해지/카드변경/추가계정 UX; 추가계정 축소=지연 반영 §3-2)
- `docs/frontend/PAYMENT_CONSENT_FRONTEND.md` — **결제 전 고지·동의**(2026-08-10, 마이그 billing0025·emails0008·core0014). 전자상거래법 §13②⑥·시행령 §20-2. **현재 정책: 동의는 결제 화면 1회.** 살아있는 것 = ①체험 `+30일` 고정(달력월 아님) + 견적 `GET /billing/subscription/preview/`(부작용 없음, `trial_last_day`=마지막 이용일 ≠ `trial_ends_at`=결제 시각) ②동의 원장 `POST /billing/consents/` `kind=initial`(**confirm 동봉 아님** — 체결 전 성립 + 실패 시 증거 보존, 세 동의 전부 true 아니면 400). **dormant** = 2차 동의 파이프라인 3종(모달 플래그 `conversion_consent_required`·과금 차단 게이트·D-14/D-3 메일) — `CONVERSION_SECOND_CONSENT_ENABLED=False` 기본. 45일 전 재동의가 리텐션을 깎고 대상이 지인 범위여서 폐기. **44일 쿠폰을 일반 마케팅에 여는 시점에 재검토**(총 체험을 30일 이내로 맞추면 2차 동의 개념 자체가 불필요). 과금 게이트를 되살릴 때 주의: 게이트는 due 재검증보다 앞에 있어 `current_period_end <= now` 를 스스로 확인해야 한다(안 하면 오배치 1건이 남은 체험을 무료로 내린다)
- `docs/ops/ADMIN_AUTH_HARDENING_PLAN.md` + `docs/frontend/ADMIN_AUTH_MFA_FRONTEND.md`(v2 계약) + `..._RESPONSE.md`(회신) — 어드민 2단계 로그인(2026-08-16, 마이그 admin_api0008·emails0009). **일반 로그인과 분리된 관문**: `POST /admin/auth/login/` → `mfa/verify/` → 어드민 JWT(`adm` 클레임, access 2h / refresh 12h·신뢰기기 7d). 게이트는 기존 `AdminRoleGuardMiddleware` 단일 초크포인트에 결합(뷰마다 달면 새 엔드포인트가 조용히 열린다). TOTP 판정 단일 소스 = `auth/totp.py`(**재사용 방지는 pyotp 가 안 해준다 — 우리 책임**), 시드는 Fernet 암호화, 등록 중 시드는 pending 자리 분리(재등록 중 이탈해도 기존 인증앱 생존). 이메일 기기승인은 `EmailToken` 재사용(purpose=admin_device)이나 **2026-08-20 부터 기본 OFF**(`ADMIN_MFA_EMAIL_DEVICE_CODE_ENABLED=False`) — 2단계는 인증앱 코드 하나. 판정 단일 소스는 `auth/devices.needs_email_verification()`(로그인·등록이 갈리면 재등록이 통째로 막힌다). TOTP 허용 오차 ±60초(`ADMIN_TOTP_DRIFT_STEPS=2`). 비상 복구 `manage.py admin_mfa_reset`. **마케팅 전용 계정(marketing_viewer)은 전 구간 제외** — 1단계 로그인 유지 + 갱신 URL 이 갈린다. ⚠️ `ADMIN_MFA_ENFORCED` 기본 False(전원 등록 전에 켜면 관리자 동시 잠김) · prod 배포 시 **이미지 재빌드**(pyotp·qrcode 신규) + `seed_email_templates` 필수
- `docs/frontend/ADMIN_SNAPSHOT_ROSTER_RESPONSE.md` — 어드민 18차 회신(SNAP-1/2). 전체 현황 타일 → 회원 명단 `GET /admin/snapshot/paying|trial/`. **타일-명단 항등**을 위해 모수 쿼리를 `snapshot_rosters.py` 로 단일화 + 타일이 만든 **id→축 매핑을 스냅샷 캐시에 함께 얼려** 부분합(`?plan=`/`?bucket=`)까지 일치. `no_card` 는 SNAP-2 제외(≠`trial_now.total`), ordering 화이트리스트 밖은 400, page_size 상한 500, `/admin/snapshot/**` 는 RBAC 미화이트리스트라 marketing_viewer 자동 403(PII 마스킹 미적용)
- `docs/frontend/REFERRAL_COUPON_FRONTEND.md` — 쿠폰(제휴/레퍼럴 코드) 프론트 변경 요청(2026-08-04, **breaking**). 무카드 `POST /billing/referral/redeem/` **폐지**(항상 400 + `REFERRAL_REQUIRES_CARD`) — 이 경로가 기본 체험 30일을 가산하지 않아 14일 쿠폰이 44일 아닌 **14일**로 나갔다(HLEVEL26 17건 중 3건 피해). 쿠폰은 `toss/confirm` 의 `referral_code` 동봉이 **유일 경로**. `validate` 에 결제 전 미리보기 5필드 추가(`requires_card`/`trial_ends_at`/`first_charge_at`/`first_charge_amount`/`extra_ig_account_price`). ⚠️ 표기는 `total_trial_days`, **`trial_days`(보너스분)를 노출하면 혜택이 1/3로 축소돼 보인다**
- `docs/frontend/EXTRA_IG_ACCOUNT_TRIAL_FRONTEND.md` — **체험 중 추가 IG 계정 0원 즉시 추가**(2026-08-21, 마이그 없음). 종전엔 `trialing` 이면 추가계정 구매/견적이 통째로 400 이라 프로 체험자가 계정을 못 늘렸다(prod CS #d34572b3 — 3분간 8회 시도). 체험 판정 단일 소스 = `compute_extra_accounts_charge`(TRIALING → 0원, 견적·실행 공유 → 견적=실청구 유지), 첫 결제 합산은 `tasks._renewal_amount_for` 가 원래 담당. 견적 응답에 `trial` 추가 — **`amount==0` 만으로는 "체험 무과금"과 "잔여 0일이라 0원"을 구별 못 한다**. ⚠️ 프론트 `QuoteGate` 가 400 사유를 버리고 "잠시 후 다시 시도"로 뭉개는 것이 이 사고의 절반이었다(재시도해도 영원히 실패) — 미납/해지예약/일시정지는 **여전히 400** 이라 그 화면은 아직 루프다
- `docs/frontend/IG_ACCOUNT_ACTIVATION_FRONTEND.md` — 추가 IG 계정 축소 지연 + 활성 계정 선택(소프트 비활성) 계약. `GET/POST /billing/ig-account-activation/` (page-activation IG 판), 비활성=기능 제외·토큰 보존, 허용량=활성 계정 수 기준. **needs_activation_adjustment 트리거에 "연동≥1 & 활성0" 케이스 추가**(전부 비활성 구제)
- `docs/frontend/WEBHOOK_HEALTH_FRONTEND.md` — IG 연결 종합 헬스 진단 + 웹훅 수동 재구독 프론트 가이드. `GET/POST /integrations/instagram/connections/{id}/health|resubscribe-webhooks/` (토큰 라이브 /me·웹훅 subscribed_apps·만료·status, report-only·항상 200, issues→CTA 매핑, 스로틀 20/min·6/hour). **하나의 IG 계정=하나의 워크스페이스**(콜백 `ALREADY_CONNECTED_ELSEWHERE` 차단, audit_ig_duplicates 로 기존 중복 조사) + 재연결 `reconnect_connection_id`(한도 우회·콜백 재판정) + 콜백 is_active 자동 복구
- `docs/frontend/SIGNUP_ATTRIBUTION_FRONTEND.md` — 방문→가입 채널 귀속 연동 가이드 (랜딩 스니펫 `tf_vid`/세션 1회 전송, CTA 쿼리스트링 핸드오프, register/google `attribution` 필드, 채널 매핑 표 = 마케팅팀 UTM 규칙). 어드민 마케팅 대시보드(`/api/v1/admin/dashboard/marketing/`)의 채널·퍼널 데이터 소스
- `docs/ops/DR_IMPLEMENTATION_PLAN.md` — 재해복구(DR) 설계·결정 로그·코드 자산 맵
- `deploy/dr/gcp/DRILL_RUNBOOK.md` — GCP cold-VM DR 드릴/컷오버 재현 런북 (+ `deploy/dr/gcp/README.md` 운영자 개요)
- `docs/ops/SECURITY_AUDIT_2026-06.md` — 론칭 전 보안 취약점 감사(미해결 P0 포함)
- `docs/frontend/DM_CAMPAIGN_DUPLICATE_PREVENTION_FRONTEND.md` — 게시물당 활성 캠페인 1개(409) 프론트 가이드
- `docs/frontend/DM_QUEUE_STATE_FRONTEND.md` — DM 순차 발송 큐 현황(게이지+ETA) 프론트 가이드 + v4.3 페이서 메커니즘 요약 (`max_sends_per_hour` 필드·DB 컬럼 완전 제거 — 마이그 0042) + v4.4 사람 단위 `people` 블록("N명" 표기는 이벤트 단위 `gauge` 말고 이걸로; stats 의 `unique_targets/failed/unconfirmed/reach_rate` 와 동일 정의 = `campaign_stats.people_rollup`) + **v4.5(2026-07-14)**: 통계 헤드라인=`unique_sent_rate`(구 100% 오표기 정정), '확인 필요'→'숨겨진 요청·스팸'(`unique_hidden_spam`) 분리 + `unique_needs_attention[_excl_hidden]`, 로그 상태 그룹 `status_group`(대기중/전송됨/읽음/숨겨진 요청·스팸/확인 필요 — 단일 소스 `dm_status_groups.py`)·`is_recovering`·서버 필터, needs_attention success-aware(복구 반영)
- `docs/frontend/DM_RECOVERY_FRONTEND.md` — 실패 DM 복구(recovery) 프론트 연동 **v2(재댓글 방식, 2026-07-14)**: 확정실패→"숨김함 수락 후 재댓글" 안내 대댓글→재댓글이 일반 경로로 재발송(성공 시 recovery_delivered 자동 승격). 추천문구 30개(`GET .../recovery-reply-suggestions/`) + 프로 전용 게이트(fail-closed) + 로그 상태 3종. `recovery_keyword`=deprecated(값 무시). 기본 활성
- `docs/frontend/CAMPAIGN_TIMESERIES_FRONTEND.md` — 캠페인 신규 요청자 시계열(진행/모멘텀 차트) 프론트 가이드. `GET .../auto-dm-campaigns/{id}/timeseries/?range=all|24h|7d`, 사람 단위(최초 트리거 시점 1회, `stats` people.total 과 동일), KST 버킷(all·7d=day/24h=hour)·제로필·마지막 버킷 partial, `history_complete`(로그 보존정책 가드). 집계=`campaign_stats.new_requester_timeseries`
- `docs/frontend/DM_CAMPAIGN_THUMBNAIL_FRONTEND.md` — 캠페인 게시물 썸네일 계약(2026-08-05, 마이그 integrations0048·core0013). **`media_url` 은 permalink(링크용, 이미지 아님) / `thumbnail_url` 은 우리 스토리지 재호스팅 사본(영구, 만료 없음)** 으로 의미 분리 — 예전 미러 구조가 permalink 를 `<img src>` 로 내보내 prod 77건 중 68건이 깨져 있었다. IG CDN URL 은 서명 만료라 저장·전달 모두 부적합 → `apps/integrations/media_thumbnail.py`(프로필 사진 규약 확장, 640px). 릴스=커버·캐러셀=첫 슬라이드(`InstagramMediaService.pick_thumbnail_source`), 목록 동기 Graph 호출 제거·비동기 예약+6h 스위퍼, 상세 응답에 통계 4필드 추가, 미디어 목록 `?media_ids=` 배치조회(호출 1회)
- `docs/frontend/CANCEL_RETENTION_FRONTEND.md` — 구독 해지 리텐션 플로우 백엔드 구현 응답. ①일시정지 `POST /billing/pause/`(months 1/2/3, 잔여기간 후 무과금 정지·자동재개+3일전 고지, 재개=기존 resume 재사용, status "paused"·pause_ends_at·paused_months·can_pause) ②리텐션할인 `POST /billing/retention-offer/apply/`(다음1회 50%, 1인1회·active유료, next_charge_amount 응답) ③트래킹 offer_shown/accepted/declined + offer 필드 ④윈백메일(WINBACK_ENABLED 게이트·dormant, marketing_opt_in 동의). 정책: 데이터 무기한 보존·정지 연1회·할인 1인1회. 마이그 billing0019/analytics0004/auth0004/core0009. **윈백은 2026-07-23 제품결정으로 계속 dormant 유지**(인앱 즉시 50% 할인으로 대체)
- `docs/frontend/MARKETING_OPT_IN_FRONTEND.md` — 마케팅 수신동의(`marketing_opt_in`) 연결 응답(2026-07-23, 마이그 없음·필드는 auth0004 기존). register/GET·PATCH me/google 3경로 연결, 수신거부=`PATCH /auth/me/ {marketing_opt_in:false}` 단일 소스(별도 토큰 엔드포인트 없음), PATCH me 응답이 프로필 전체로 확장. 리텐션 확인 4건 답변(next_billing.amount=renewal_amount 할인반영·paused change-plan/extra-accounts 400·오퍼코드 신규발급·정지 게이팅 get_effective_plan 일치). "취소 시 쿠폰 메일"은 원래 없음
- `docs/frontend/PASSWORD_RESET_GUIDE.md` — 비밀번호 재설정 플로우 프론트 가이드
- `docs/frontend/DISCONNECT_OTHER_DM_TOOLS_GUIDE.md` — 다른 DM 자동화 툴(매니챗 등) 연결 해제 안내 (댓글 fan-out·Private Reply 1회 충돌 / IG Login이라 Facebook 라우팅 불필요)
- `docs/frontend/CONNECT_CONFLICT_WARNING_FRONTEND.md` — 다른 DM 툴 충돌 경고 배너 프론트 스펙 (연결 직후 + 대시보드 상단, 닫기 규칙)
- `docs/frontend/DM_CAMPAIGN_MIGRATION_FRONTEND.md` — DM 캠페인 이전(매니챗 등→TurnFlow) 프론트 가이드. 연동 IG 계정의 최근 게시물·댓글·발신 DM(Conversations API) 분석→기존 DM 캠페인 추론→**비활성(INACTIVE) 초안 후보** 생성→검수·apply→활성화. `POST/GET /integrations/dm-migration/jobs/`(시작·폴링 3s·취소, 비종결1개 + **7일 캐시 재사용** + force쿨다운 **6h**·429 — 거부는 force 일 때만), `.../jobs/{id}/candidates/`(band=auto_draft/needs_review/template_only/excluded), `.../candidates/{id}/apply|dismiss/`(apply=AutoDMCampaignCreateSerializer 재사용·status=INACTIVE·활성 중복은 활성화 시점 발동). 전 플랜·v1 토큰차감 없음. 파이프라인=`apps/integrations/dm_migration/`(collect·analyze·llm·pipeline), 태스크 `integrations.run_dm_migration_job`(ai_jobs 큐, 체크포인트 재개·rate-pause)·`integrations.purge_dm_migration_raw`(원본 7일 파기+스테일 스위퍼, core 0008 시드). 자기발송 제외(SentDMLog mid/지문), Mock 픽스처+`DM_MIGRATION_FAKE_LLM`로 오프라인 e2e
- `docs/frontend/INSTA_REPORT_FRONTEND.md` — 인스타 성장 리포트 프론트 연동 가이드(프로 전용·계정당 월1회·평균 15분·3초 폴링·10단계
  진행률 보간·HTML 인증 다운로드·완료 메일). 파이프라인 원본 실험은 `../insta_report_lab/PLAN.md`
- `docs/system/SERVICE_DIFFERENTIATION.md` — 서비스 차별점/경쟁 비교 (세일즈·마케팅)
- `docs/legal/개인정보처리방침_변호사_전달자료.md`, `docs/legal/이용약관_변호사_전달자료.md` — 법무 자료 (수정 금지, 요청 시에만)
- `api-mcp/README.md` — 사내 API 문서 검색 MCP 서버

외부 참조:
- Meta Graph API / Instagram Graph API 공식 문서
- DRF Spectacular: https://drf-spectacular.readthedocs.io/
- Celery 공식 문서

---

## 14. 금지 / 주의 사항

- ❌ `.env` 커밋 금지 (`.gitignore`에 포함)
- ❌ IG/Meta 토큰, 토스 시크릿 키·**빌링키·authKey** 평문 저장·로그 출력 금지 (빌링키는 승인 URL path에 들어가므로 httpx 로거를 WARNING으로 고정해둠 — 되돌리지 말 것)
- ❌ 프로덕션에서 `DEBUG=True`, 기본 `SECRET_KEY` 사용 금지
- ❌ 마이그레이션 없이 모델 필드 변경 금지
- ❌ Workspace 필터 없이 전역 쿼리 실행 금지 (테넌트 누수)
- ❌ 동기 뷰에서 외부 API 장시간 호출 (Celery로)
- ❌ 문서 없는 API 엔드포인트 추가 (섹션 7 위반)
- ⚠️ `migrations/` 디렉터리는 Black/Ruff/isort 모두 제외 — 수동 편집 지양
- ⚠️ `geoip/GeoLite2-Country.mmdb`는 Dockerfile 빌드 시 다운로드 — 로컬 파일은 `.gitignore`
