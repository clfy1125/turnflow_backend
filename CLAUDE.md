# CLAUDE.md

이 파일은 Claude Code가 이 저장소에서 작업할 때 참조할 지침입니다.
작업 시작 전 반드시 숙지하고, 관련 변경이 생기면 업데이트하세요.

---

## 1. 프로젝트 개요

**TurnFlow Backend (Instagram Service Backend)**
Instagram Business 계정의 댓글 수집/분류, 자동 DM 발송, 키워드 기반 자동화, LLM 기반 컨텐츠 분석을 제공하는 SaaS 백엔드.

- **서비스 모델**: 멀티테넌시(Workspace) 기반 SaaS
- **요금제**: Starter / Pro / Enterprise (사용량 제한 있음)
- **결제**: PayApp 연동 (정기결제)
- **1차 MVP 기능**: IG 계정 연동, 댓글 수집/분류, 자동 DM, 템플릿/시나리오, 지표 대시보드
- **2차 확장**: LLM 기반 의도 분석, 릴스/게시물 컨텐츠 분석, A/B 테스트, CRM/웹훅

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
| 결제 | PayApp |
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
│   ├── billing/                # 요금제/구독/PayApp 결제 + Celery 정기 배치
│   ├── integrations/           # Instagram OAuth/토큰 암호화(encryption.py)/Webhook
│   ├── pages/                  # 페이지/게시물/DM 관련 뷰 (multi_views, image_views, stats, aiviews)
│   └── ai_jobs/                # LLM 작업 큐 + services(llm_client, model_router, prompt_builder)
├── config/                     # Django 프로젝트 설정
│   ├── settings/               # base.py / local.py / prod.py
│   ├── urls.py                 # 루트 URL (admin, api/v1, swagger, redoc)
│   ├── api_urls.py             # /api/v1/ 아래 라우팅
│   ├── celery.py               # Celery 앱
│   └── wsgi.py / asgi.py
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
- LLM: `LLM_URL`, `LLM_API_KEY`
- PayApp: `PAYAPP_USERID`, `PAYAPP_LINKKEY`, `PAYAPP_LINKVAL`, `PAYAPP_API_URL`, `PAYAPP_FEEDBACK_URL`, `PAYAPP_FAIL_URL`, `PAYAPP_RETURN_URL`
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
- 기존 정기 스케줄 (`CELERY_BEAT_SCHEDULE` in base.py):
  - `billing.check_missed_payments` — 매일
  - `billing.handle_grace_period_expiry` — 매일
  - `billing.handle_cancelled_expiry` — 매일
- 태스크 타임 리밋: 30분 (`CELERY_TASK_TIME_LIMIT`)
- 큐: billing 등 기능별 `options: {queue: "..."}` 지정

**주의**: Celery Beat 전용 컨테이너는 현재 compose에 없음 — 주기 배치가 필요하면 Beat를 별도 서비스로 추가하거나 `django-celery-beat`로 전환 고려.

---

## 11. 모델 핵심 엔티티 (지침서 0-4)

현재 구현된 주요 모델 (수정/확장 시 마이그레이션 필수):
- `authentication.User` — email 기반, `full_name` 추가
- `workspace.Workspace` — UUID PK, slug, owner(FK User, PROTECT), plan(starter/pro/enterprise)
- `workspace.Membership` — Owner/Admin/Member 역할
- `billing.*` — 구독/결제/PayApp
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

**외부 API (Meta/Instagram/PayApp/LLM) 건드릴 때**
- [ ] 토큰/키 평문 저장·로깅 금지
- [ ] 실패 재시도 + 타임아웃 설정
- [ ] Mock 모드에서도 동작하도록 분기
- [ ] Webhook이면 idempotency 키 저장

---

## 13. 참고 문서

저장소 내 문서:
- `프로젝트 지침서.md` — 제품 요구사항 원본 (이 파일의 상위)
- `README.md` — 일반 개발자용 셋업 가이드
- `INSTAGRAM_OAUTH_FLOW.md` — IG OAuth 플로우
- `INSTAGRAM_TEST_GUIDE.md` — IG 테스트 가이드
- `NGROK_INSTAGRAM_SETUP.md` — ngrok로 IG 콜백 받는 법
- `STEP1~5_COMPLETION.md` — 단계별 완료 보고서
- `개인정보처리방침_변호사_전달자료.md`, `이용약관_변호사_전달자료.md` — 법무 자료 (수정 금지, 요청 시에만)
- `api-mcp/README.md` — 사내 API 문서 검색 MCP 서버

외부 참조:
- Meta Graph API / Instagram Graph API 공식 문서
- DRF Spectacular: https://drf-spectacular.readthedocs.io/
- Celery 공식 문서

---

## 14. 금지 / 주의 사항

- ❌ `.env` 커밋 금지 (`.gitignore`에 포함)
- ❌ IG/Meta 토큰, PayApp 키 평문 저장·로그 출력 금지
- ❌ 프로덕션에서 `DEBUG=True`, 기본 `SECRET_KEY` 사용 금지
- ❌ 마이그레이션 없이 모델 필드 변경 금지
- ❌ Workspace 필터 없이 전역 쿼리 실행 금지 (테넌트 누수)
- ❌ 동기 뷰에서 외부 API 장시간 호출 (Celery로)
- ❌ 문서 없는 API 엔드포인트 추가 (섹션 7 위반)
- ⚠️ `migrations/` 디렉터리는 Black/Ruff/isort 모두 제외 — 수동 편집 지양
- ⚠️ `geoip/GeoLite2-Country.mmdb`는 Dockerfile 빌드 시 다운로드 — 로컬 파일은 `.gitignore`
