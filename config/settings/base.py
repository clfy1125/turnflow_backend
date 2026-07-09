"""
Django settings for Instagram Service Backend project.

Base settings - shared across all environments.
"""

from datetime import timedelta
from pathlib import Path

from celery.schedules import crontab
from decouple import config

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config("SECRET_KEY", default="django-insecure-local-dev-key-change-in-production")

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party apps
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "django_extensions",
    # Local apps
    "apps.core",
    "apps.authentication",
    "apps.workspace",
    "apps.billing",
    "apps.integrations",
    "apps.pages",
    "apps.ai_jobs",
    "apps.insights",
    "apps.emails.apps.EmailsConfig",
    "apps.tiktok",
    "apps.youtube",
    "apps.admin_api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
    "apps.core.middleware.LoggingMiddleware",
    # DR: passive 사이트(SITE_ID != SiteControl.active_site)에서 요청 503 차단 (split-brain 방지).
    # 평상시 active 사이트에서는 완전 no-op. /healthz*, /internal/scheduler/* 는 하드 예외.
    "apps.core.middleware.ActiveSiteGateMiddleware",
    # /api/v1/insights/* 전체 503 차단 (INSIGHTS_API_ENABLED=False 일 때만 동작)
    "apps.insights.middleware.InsightsDisabledMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
# https://docs.djangoproject.com/en/5.0/ref/settings/#databases
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", default="instagram_service"),
        "USER": config("DB_USER", default="postgres"),
        "PASSWORD": config("DB_PASSWORD", default="postgres"),
        "HOST": config("DB_HOST", default="localhost"),
        "PORT": config("DB_PORT", default="5432"),
        # PgBouncer transaction-pool 사용 시 반드시 CONN_MAX_AGE=0 (영속 커넥션 금지) +
        # DISABLE_SERVER_SIDE_CURSORS=True (transaction pooling 은 named cursor 미지원).
        # prod(.env.production): DB_HOST=pgbouncer / DB_PORT=6432 / DB_CONN_MAX_AGE=0 / DB_DISABLE_SERVER_SIDE_CURSORS=True
        # 마이그레이션 one-shot 은 db:5432 직결(session pool) 로 실행 — deploy.sh 참고.
        "CONN_MAX_AGE": config("DB_CONN_MAX_AGE", default=0, cast=int),
        "CONN_HEALTH_CHECKS": config("DB_CONN_HEALTH_CHECKS", default=True, cast=bool),
        "DISABLE_SERVER_SIDE_CURSORS": config(
            "DB_DISABLE_SERVER_SIDE_CURSORS", default=False, cast=bool
        ),
    }
}

# Password validation
# https://docs.djangoproject.com/en/5.0/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
# https://docs.djangoproject.com/en/5.0/topics/i18n/
LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.0/howto/static-files/
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# NOTE: STATICFILES_STORAGE 는 USE_R2=False 인 경우에만 사용.
# USE_R2=True 인 경우 아래 STORAGES dict 에서 staticfiles 를 함께 지정하므로
# 두 설정을 동시에 쓰면 Django 가 ImproperlyConfigured 를 던짐.

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ─────────────────────────────────────────────────────────────
# Object Storage (Cloudflare R2, S3-compatible)
# ─────────────────────────────────────────────────────────────
# USE_R2=True 이면 FileField 기본 스토리지를 R2로 전환.
# False면 로컬 MEDIA_ROOT 사용 (개발/폴백).
#
# 컷오버 절차:
#   1) rclone sync ./media r2:<bucket>   (라이브 상태에서 1차 복사)
#   2) 쓰기 잠깐 정지 → 2차 sync
#   3) USE_R2=True 로 재배포
#   4) 문제 생기면 USE_R2=False 내리면 즉시 로컬 서빙 복귀
USE_R2 = config("USE_R2", default=False, cast=bool)

if USE_R2:
    _R2_ACCOUNT_ID = config("R2_ACCOUNT_ID")
    _R2_PUBLIC_DOMAIN = config("R2_PUBLIC_DOMAIN")  # 예: media.turnflow.clfy.ai.kr
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": config("R2_BUCKET_NAME"),
                "access_key": config("R2_ACCESS_KEY_ID"),
                "secret_key": config("R2_SECRET_ACCESS_KEY"),
                "endpoint_url": f"https://{_R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                "region_name": "auto",
                "signature_version": "s3v4",
                "addressing_style": "path",
                "default_acl": None,  # R2는 ACL 미지원
                "querystring_auth": False,  # 퍼블릭 버킷 → 서명 URL 불필요
                "file_overwrite": False,
                "custom_domain": _R2_PUBLIC_DOMAIN,
            },
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
        },
    }
    MEDIA_URL = f"https://{_R2_PUBLIC_DOMAIN}/"
else:
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Default primary key field type
# https://docs.djangoproject.com/en/5.0/ref/settings/#default-auto-field
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Custom User Model
AUTH_USER_MODEL = "authentication.User"

# Session configuration for OAuth with ngrok
SESSION_COOKIE_SAMESITE = None  # Allow cross-site cookies for OAuth
SESSION_COOKIE_SECURE = False  # Set to True in production with HTTPS
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 3600  # 1 hour

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.custom_exception_handler",
    # Throttle 은 글로벌 활성화하지 않음 (다른 뷰 영향 X). 각 View 가 명시적으로
    # ``throttle_classes = [ScopedRateThrottle]`` + ``throttle_scope = "..."`` 사용.
    # ``DEFAULT_THROTTLE_RATES`` 에 등록된 scope 만 ScopedRateThrottle 로 동작.
    "DEFAULT_THROTTLE_RATES": {
        # 외부 페이지 import 전용 — 사용자별 시간당 30건. 어뷰즈 차단 + 정상 사용 모두 OK.
        "external_import": "30/hour",
        # 인사이트 강제 동기화 — 사용자별 시간당 5회 (IG quota 보호)
        "insights_sync": "5/hour",
        # 외부 링크 메타 조회 — 사용자별 분당 60회 (인터랙티브 붙여넣기 UX + SSRF 어뷰즈 방어)
        "link_meta": "60/min",
        # ── 인증 엔드포인트 brute-force / credential-stuffing / 메일폭탄 방어 (H-1) ──
        # ScopedRateThrottle 은 익명 요청을 IP 로 키잉하므로 무인증 로그인/가입/재설정에 적용된다.
        "auth_login": config("THROTTLE_AUTH_LOGIN", default="10/min"),
        "auth_register": config("THROTTLE_AUTH_REGISTER", default="10/hour"),
        "auth_google": config("THROTTLE_AUTH_GOOGLE", default="20/min"),
        "email_verify": config("THROTTLE_EMAIL_VERIFY", default="10/min"),
        "email_send": config("THROTTLE_EMAIL_SEND", default="5/hour"),
        "password_reset": config("THROTTLE_PASSWORD_RESET", default="10/hour"),
        "password_reset_confirm": config("THROTTLE_PASSWORD_RESET_CONFIRM", default="10/min"),
    },
}

# DRF Spectacular (OpenAPI/Swagger)
SPECTACULAR_SETTINGS = {
    "TITLE": "Instagram Service Backend API",
    "DESCRIPTION": "API documentation for Instagram Business Account automation service",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1/",
    "COMPONENT_SPLIT_REQUEST": True,
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "apps.pages.openapi.postprocess_block_data_schema",
    ],
    "SWAGGER_UI_SETTINGS": {
        "deepLinking": True,
        "persistAuthorization": True,
        "displayOperationId": True,
    },
    "SECURITY": [
        {
            "Bearer": [],
        }
    ],
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "Bearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            }
        }
    },
}

# Simple JWT Settings
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_HEADER_NAME": "HTTP_AUTHORIZATION",
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
}

# Google OAuth
GOOGLE_CLIENT_ID = config("GOOGLE_CLIENT_ID", default="")

# Redis & Caching
REDIS_HOST = config("REDIS_HOST", default="localhost")
REDIS_PORT = config("REDIS_PORT", default="6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}"

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{REDIS_URL}/1",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

# Celery Configuration
CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=f"{REDIS_URL}/0")
CELERY_RESULT_BACKEND = config("CELERY_RESULT_BACKEND", default=f"{REDIS_URL}/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes

# 기능별 큐 라우팅 (P3a) — SLA/부하 격리. 각 큐는 docker-compose.prod.yml 의 전용 워커가 consume:
#   dm_send → celery_dm(threads) · webhook_followup,verify → celery_followup(threads)
#   celery,snapshot → celery_default(prefork) · billing → celery_billing(prefork)
# 단일 워커 환경(로컬/레거시)에서도 기본 worker 가 모든 큐를 consume 하면 호환됨.
# ⚠️ 라우팅된 큐를 consume 하는 워커가 없으면 태스크가 영원히 적체된다 — 워커 구성과 항상 일치시킬 것.
CELERY_TASK_ROUTES = {
    "pages.capture_reference_snapshot": {"queue": "snapshot"},
    # DM 외부 발송 (I/O-bound, 고동시성)
    "apps.integrations.tasks.send_dm_task": {"queue": "dm_send"},
    "apps.integrations.tasks.post_public_reply": {"queue": "dm_send"},
    # 댓글 처리 + 웹훅 delivered/read 후속 UPDATE
    "apps.integrations.tasks.process_comment_and_send_dm": {"queue": "webhook_followup"},
    "apps.integrations.tasks.process_messaging_event": {"queue": "webhook_followup"},
    # 스팸 필터 LLM 판정(3-7초 gemma) — DM 디스패치를 굶기지 않게 ai_jobs 워커로 격리.
    # celery_ai 가 이미 최대 600s LLM 작업용으로 구동중이라 신규 인프라 불필요.
    "apps.integrations.tasks.run_spam_filter_check": {"queue": "ai_jobs"},
    # 도착 검증
    "apps.integrations.tasks.verify_dm_delivery": {"queue": "verify"},
    # 정기 결제 배치
    "billing.*": {"queue": "billing"},
    # AI 작업 격리(#5): run_ai_job(최대 600s)·campaign-assist 를 전용 워커(celery_ai)로 라우팅해
    # snapshot/DM-reconcile 가 도는 celery_default 8슬롯과 head-of-line blocking 분리.
    "apps.ai_jobs.tasks.*": {"queue": "ai_jobs"},
}

# Celery Beat Schedule (정기 결제 + DM 발송 보증 워커)
# NOTE: 토스는 PG측 스케줄러가 없다 — 갱신 과금의 주체는 process_due_renewals.
#       만료 처리 계열은 매시간 실행해 단일 실행 누락/지연 위험을 줄인다.
#       각 태스크는 멱등하다 (중복 과금 방지: 결정적 orderId + Idempotency-Key).
CELERY_BEAT_SCHEDULE = {
    # ── 토스 빌링 갱신 파이프라인 ──
    "process-due-renewals": {
        "task": "billing.process_due_renewals",
        "schedule": 60 * 10,  # 10분 — 갱신 도래 구독 과금 디스패치
        "options": {"queue": "billing"},
    },
    "reconcile-pending-payments": {
        "task": "billing.reconcile_pending_payments",
        "schedule": 60 * 30,  # 30분 — 모호 실패(PENDING) 결제 확정
        "options": {"queue": "billing"},
    },
    "check-missed-payments": {
        "task": "billing.check_missed_payments",
        "schedule": 60 * 60,  # 매시간 — 갱신 파이프라인 고장 감시
        "options": {"queue": "billing"},
    },
    "handle-grace-period-expiry": {
        "task": "billing.handle_grace_period_expiry",
        "schedule": 60 * 60,  # 매시간
        "options": {"queue": "billing"},
    },
    "handle-cancelled-expiry": {
        "task": "billing.handle_cancelled_expiry",
        "schedule": 60 * 60,  # 매시간
        "options": {"queue": "billing"},
    },
    "handle-trial-expiry": {
        "task": "billing.handle_trial_expiry",
        "schedule": 60 * 60,  # 매시간
        "options": {"queue": "billing"},
    },
    # ===== DM 발송 99.9% 보증 시스템 =====
    "dm-reconcile-accepted": {
        "task": "apps.integrations.tasks.reconcile_accepted_dms",
        "schedule": 60,  # 1분 — ACCEPTED + 5분 경과 건 능동 검증 enqueue
    },
    "dm-reconcile-stuck-submitting": {
        "task": "apps.integrations.tasks.reconcile_stuck_submitting",
        "schedule": 30,  # 30초 — SUBMITTING 정체 건 재시도
    },
    "dm-requeue-deferred": {
        "task": "apps.integrations.tasks.requeue_deferred_dms",
        "schedule": 30,  # 30초 — next_retry_at 도래한 defer(QUEUED) 건 순차(FIFO) 재투입
    },
    "dm-reconcile-pacer-pointers": {
        "task": "apps.integrations.tasks.reconcile_pacer_pointers",
        "schedule": 60,  # 60초 — 삭제/일시중지로 생긴 페이서 '빈 슬롯 홀' 회수(v4.3 Fix 2)
    },
    "dm-dead-letter-alerter": {
        "task": "apps.integrations.tasks.dead_letter_alerter",
        "schedule": 60 * 10,  # 10분 — 토큰 만료/도착 미확인 누적 알림
    },
    # 백로그 위험(윈도우 만료 임박/오래된 대기) Telegram 경고 (P7 — E1 손실 예방).
    "dm-backlog-alert": {
        "task": "apps.integrations.tasks.dm_backlog_alert",
        "schedule": 60 * 30,  # 30분
    },
    # ===== DM 예약 발송: 활성 기간 자동 종료 =====
    # scheduled_end_at 이 지난 ACTIVE 캠페인을 COMPLETED 로 전환 (멱등).
    "dm-enforce-campaign-schedules": {
        "task": "apps.integrations.tasks.enforce_campaign_schedules",
        "schedule": 60,  # 1분 — 종료 예약 시각 경과 캠페인 자동 종료
    },
    # ===== 댓글 웹훅 누락 보정 =====
    # comments 웹훅 유실 대비 — specific_media 캠페인의 (connection,media) 댓글을 재조회해
    # 누락 DM 보정. 발송은 rate_governor 가 throttle. MISSED_COMMENT_POLL_ENABLED 로 토글.
    "dm-poll-missed-comments": {
        "task": "integrations.poll_missed_comments",
        "schedule": 60 * 60,  # 1시간
    },
    # ===== 웹훅 구독 재확정 =====
    # Meta 는 콜백 실패(엣지 장애·DR 컷오버)가 쌓이면 계정별 웹훅 구독을 auto-disable →
    # 댓글 웹훅이 조용히 끊겨 캠페인 무음. 6시간마다 ACTIVE 연동의 comments/messages 구독을
    # 재확정(활성 사이트에서만 실변경, 재구독/실패 시 Telegram). DR promote 직후엔 startup.sh 가 즉시 실행.
    # 실제 구동은 core.ScheduledJob(0003 시드, 0005 에서 6h→1h 상향). CELERY_BEAT_SCHEDULE 은 fallback/문서용.
    "integrations-resubscribe-webhooks": {
        "task": "apps.integrations.tasks.resubscribe_all_webhooks",
        "schedule": 60 * 60,  # 1시간 (#6 — Meta auto-disable 무음창 6h→1h 축소)
    },
    # 인프라 헬스 경고(#6): Redis noeviction freeze·브로커 큐 적체·deferred DM 밀림 5분 감시(core.ScheduledJob 0005).
    "integrations-dm-infra-health-alert": {
        "task": "apps.integrations.tasks.dm_infra_health_alert",
        "schedule": 60 * 5,  # 5분
    },
    # ===== GATE-0 백업 관측 =====
    # 실제 백업은 호스트 cron(deploy/backups/pg_backup.sh + pgBackRest)에서 수행.
    # 이 beat 는 연속 WAL 아카이빙 상태만 감시(pg_stat_archiver) → 실패/지연 시 Telegram.
    "backup-health-check": {
        "task": "apps.core.tasks.backup_health_check",
        "schedule": 60 * 30,  # 30분
        "options": {"queue": "billing"},
    },
    # ===== IG Long-lived Token 자동 갱신 =====
    # 6시간마다 — 만료까지 14일 미만 ACTIVE 연동의 token refresh + (후보 있을 때만) Telegram 요약.
    # v3.10: daily 09:00 → 6h 주기로 강화 (한 번 실패한 연동을 하루 방치하지 않도록).
    # 토큰 복구 성공 시 그 연결의 FAILED_TOKEN 발송을 자동 되살림(revive_failed_token_logs).
    # Meta 정책: ig_refresh_token 호출 시 60일 신규 발급. 활성 사용자는 사실상 영구 유지.
    "ig-refresh-tokens-pending-expiry": {
        "task": "apps.integrations.tasks.refresh_ig_tokens_pending_expiry",
        "schedule": crontab(minute=0, hour="*/6"),  # CELERY_TIMEZONE=Asia/Seoul 기준
    },
    # 매일 KST 04:00 — 미인증 상태로 N일(기본 1일) 경과한 가입 계정 정리.
    # 기본은 비활성(UNVERIFIED_ACCOUNT_CLEANUP_ENABLED=False) + dry-run 이라 안전.
    "cleanup-unverified-accounts": {
        "task": "authentication.cleanup_unverified_accounts",
        "schedule": crontab(hour=4, minute=0),  # CELERY_TIMEZONE=Asia/Seoul 기준
        "options": {"queue": "billing"},
    },
    # 매일 KST 04:30 — 만료된 댓글 관측 장부(SeenComment) 정리.
    "cleanup-comment-ledger": {
        "task": "integrations.cleanup_comment_ledger",
        "schedule": crontab(hour=4, minute=30),  # CELERY_TIMEZONE=Asia/Seoul 기준
    },
    # 매일 KST 02:00 — EventInbox 일별 파티션 유지(선생성 + 보존 초과 DROP) + (옵션)SentDMLog 아카이브. (§15.8)
    "maintain-partitions": {
        "task": "integrations.maintain_partitions",
        "schedule": crontab(hour=2, minute=0),  # CELERY_TIMEZONE=Asia/Seoul 기준
        "options": {"queue": "billing"},
    },
    # ===== Instagram Insights 동기화 (임시 비활성) =====
    # insights 기능 출시 보류 — Meta IG insights API 호출이 발생하지 않도록 4개 beat 모두 주석 처리.
    # 활성화 시점에 아래 4개를 복원 + INSIGHTS_API_ENABLED=True 로 전환.
    # 호출량 절감 정책(stale TTL) 은 apps/insights/models.py 의 IGMedia.insight_stale_ttl() 참고.
    # "insights-sync-active-accounts-media": {
    #     "task": "insights.sync_active_accounts_media",
    #     "schedule": 60 * 30,  # 30분 — 신규 미디어 메타데이터 감지
    # },
    # "insights-refresh-recent-insights": {
    #     "task": "insights.refresh_recent_insights",
    #     "schedule": 60 * 30,  # 30분 — 최근 7일 게시물 인사이트
    # },
    # "insights-refresh-old-insights": {
    #     "task": "insights.refresh_old_insights",
    #     "schedule": 60 * 60 * 24,  # 24시간 — 그 외 게시물 인사이트
    # },
    # "insights-refresh-account-audience": {
    #     "task": "insights.refresh_account_audience_insights",
    #     "schedule": 60 * 60 * 24,  # 24시간 — 계정 단위 follow_type 도달 분포
    # },
    # NOTE: 아래는 deprecate (v3.5/v3.6) 로 Beat 에서 제거됨:
    #   - dm-expire-gate-pending: Follow-gate 가 deprecated 됨
    #   - dm-poll-new-media-for-next-campaigns: next_media 가 webhook 기반으로 전환
    #   - dm-check-polling-anomalies: 폴링 자체가 사라져 감시 불필요
}

# TossPayments 빌링(정기결제) 연동
# 라이브 전환 = 키만 test_* → live_* 로 교체 (+ 개발자센터 웹훅 URL 등록)
TOSS_SECRET_KEY = config("TOSS_SECRET_KEY", default="")  # test_sk_... / live_sk_...
TOSS_CLIENT_KEY = config("TOSS_CLIENT_KEY", default="")  # test_ck_... / live_ck_...
TOSS_API_BASE = config("TOSS_API_BASE", default="https://api.tosspayments.com")
# 카드번호 직접 입력으로 빌링키를 발급하는 dev 헬퍼 API 활성화 스위치.
# 프론트 SDK 없이 Swagger만으로 결제 플로우를 검증하기 위한 것 — 운영에서는 반드시 False.
TOSS_DEV_CARD_AUTH_ENABLED = config("TOSS_DEV_CARD_AUTH_ENABLED", default=False, cast=bool)

# Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        # H-9/M-22: 로그에 섞여 들어갈 수 있는 토큰/시크릿 쿼리 파라미터 값 마스킹.
        "scrub_secrets": {"()": "apps.core.log_filters.SecretScrubFilter"},
    },
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["scrub_secrets"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": config("LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": config("LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
        # httpx는 INFO에서 요청 URL 전체를 로깅함 — 토스 빌링키가 URL path에
        # 들어가므로(POST /v1/billing/{billingKey}) INFO 로그는 시크릿 누출.
        # WARNING으로 올려 차단한다 (앱 자체 로그가 마스킹된 경로를 남김).
        "httpx": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "httpcore": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# Instagram Integration
INSTAGRAM_APP_ID = config("INSTAGRAM_APP_ID", default="")
INSTAGRAM_APP_SECRET = config("INSTAGRAM_APP_SECRET", default="")
INSTAGRAM_REDIRECT_URI = config("INSTAGRAM_REDIRECT_URI", default="")
INSTAGRAM_MOCK_MODE = config("INSTAGRAM_MOCK_MODE", default=True, cast=bool)
INSTAGRAM_WEBHOOK_VERIFY_TOKEN = config(
    "INSTAGRAM_WEBHOOK_VERIFY_TOKEN", default="my_verify_token_12345"
)

# Meta App (Facebook Login for Instagram Business)
META_APP_ID = config("META_APP_ID", default="")
META_APP_SECRET = config("META_APP_SECRET", default="")

# P2c — 웹훅 echo/read 이벤트를 EventInbox 멱등 INSERT + Celery(webhook_followup) 비동기 처리.
# True(기본): 동시 UPDATE 레이스 제거 + webhook 응답 빨라짐.
# False: 레거시 inline 처리로 즉시 롤백 (코드 재배포 없이 env 만으로).
WEBHOOK_ASYNC_MESSAGING = config("WEBHOOK_ASYNC_MESSAGING", default=True, cast=bool)

# P3 — 웹훅 POST 의 X-Hub-Signature-256 (HMAC) 검증 강제 여부.
# False(기본): 불일치 시 경고만 남기고 처리(롤아웃 중 Meta 서명 수신 여부 관측).
# True: 불일치/누락 시 403 (위조 페이로드 차단). META/INSTAGRAM_APP_SECRET 설정 + 검증 후 전환.
WEBHOOK_HMAC_ENFORCED = config("WEBHOOK_HMAC_ENFORCED", default=False, cast=bool)

# ─────────────────────────────────────────────────────────────
# DM 발송 속도 제어 (per-IG-account)
# ─────────────────────────────────────────────────────────────
# v4.3 — 스무스 페이서(dm_pacer): 계정별 발송 간격을 지터 있는 슬롯으로 직렬화.
# Meta 버킷과 1:1 — 사설답장(오프닝, Meta 750/hr)은 3~7s(평균 5.0s ≈ 720/hr),
# Send API(리워드/재안내/스토리답장 — 유저 개시 스레드, 시간당 캡 없음)는 1~3s(봇 지문 회피).
# 정확히 N초 간격은 봇 지문이므로 매 간격 uniform 지터.
DM_PACER_ENABLED = config("DM_PACER_ENABLED", default=True, cast=bool)
DM_PACER_PRIVATE_REPLY_MIN_S = config("DM_PACER_PRIVATE_REPLY_MIN_S", default=3.0, cast=float)
DM_PACER_PRIVATE_REPLY_MAX_S = config("DM_PACER_PRIVATE_REPLY_MAX_S", default=7.0, cast=float)
DM_PACER_SEND_API_MIN_S = config("DM_PACER_SEND_API_MIN_S", default=1.0, cast=float)
DM_PACER_SEND_API_MAX_S = config("DM_PACER_SEND_API_MAX_S", default=3.0, cast=float)

# 시간당 백스톱(rate_governor) — 페이서가 rate 를 구조적으로 보장하므로 이제 '최후 방어선'.
# Meta 물리 한도: 게시물/릴스 댓글 Private Reply = 계정당 750 calls/hour.
# 백스톱은 740 — 페이서 자연율(3~7s ≈ 720/hr) 위, Meta 750 아래에 둬서 페이서가 정상일 때는
# 절대 걸리지 않게(=정시 버스트 재발 방지) 하되, 페이서 버그/우회 시에만 750 직전에서 막는다.
# (v4.3: 분당 캡·Redis flush 동결은 페이서가 대체해 제거됨. 캠페인 200/hr 도 미강제.)
DM_GOVERNOR_ENABLED = config("DM_GOVERNOR_ENABLED", default=True, cast=bool)
IG_PRIVATE_REPLY_HOURLY_CAP = config("IG_PRIVATE_REPLY_HOURLY_CAP", default=740, cast=int)

# P4 — Action Block(code 368 등) 감지 시 계정별 발송 쿨다운(에스컬레이팅 24h→×2, 상한 7일).
# 차단 중 재시도가 차단 기간을 연장시키므로, 쿨다운 동안 그 계정 DM 을 Meta 로 보내지 않는다.
DM_ACTION_BLOCK_BASE_COOLDOWN_HOURS = config(
    "DM_ACTION_BLOCK_BASE_COOLDOWN_HOURS", default=24, cast=int
)
DM_ACTION_BLOCK_MAX_COOLDOWN_DAYS = config("DM_ACTION_BLOCK_MAX_COOLDOWN_DAYS", default=7, cast=int)

# P10 — 동일 수신자(같은 캠페인) 쿨다운 초. 단시간 다중 댓글/답장 시 한 번만 발송(계정 보호).
DM_RECIPIENT_COOLDOWN_SECONDS = config("DM_RECIPIENT_COOLDOWN_SECONDS", default=300, cast=int)

# P7 — 백로그 경고 임계. window 만료 임박 판정 시간 / 가장 오래된 QUEUED 경고 시간.
DM_BACKLOG_RISK_HOURS = config("DM_BACKLOG_RISK_HOURS", default=6, cast=int)
DM_BACKLOG_OLDEST_ALERT_HOURS = config("DM_BACKLOG_OLDEST_ALERT_HOURS", default=2, cast=int)

# 수신자 목록 열람 시 빈 username(IGSID만 있는 Story 답장 등)을 IG User Profile API 로
# 지연 해석할지 여부. False 면 해석 호출 없이 user_{IGSID} 폴백만 표기(킬 스위치).
DM_RESOLVE_RECIPIENT_USERNAME = config("DM_RESOLVE_RECIPIENT_USERNAME", default=True, cast=bool)

# ─────────────────────────────────────────────────────────────
# Telegram 운영 알림 (토큰 refresh / 장애 등)
# ─────────────────────────────────────────────────────────────
# 두 값 모두 비어 있으면 알림 비활성 (개발/로컬 안전).
# 봇 생성: @BotFather → /newbot → 토큰 발급. chat_id 는 봇과 대화 후
# https://api.telegram.org/bot<TOKEN>/getUpdates 로 확인.
TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHAT_ID = config("TELEGRAM_CHAT_ID", default="")

# ─────────────────────────────────────────────────────────────
# Insights API kill-switch
# ─────────────────────────────────────────────────────────────
# False (기본) 면 /api/v1/insights/* 전체와 관련 Celery beat 가 비활성.
# Insights 기능 출시 시 env 로 INSIGHTS_API_ENABLED=True 로 전환.
INSIGHTS_API_ENABLED = config("INSIGHTS_API_ENABLED", default=False, cast=bool)

# ─────────────────────────────────────────────────────────────
# Coupang Partners Open API (https://partners.coupang.com)
# ─────────────────────────────────────────────────────────────
# 어필리에이트 등록 후 발급되는 ACCESS_KEY/SECRET_KEY 로 HMAC-SHA256 인증.
# 상품 검색, 가격 조회, 딥링크(어필리에이트 트래킹 URL) 생성에 사용.
# MOCK 모드: 키 발급 전이거나 로컬 개발 시 외부 호출 없이 더미 응답으로 동작.
COUPANG_MOCK_MODE = config("COUPANG_MOCK_MODE", default=True, cast=bool)
COUPANG_PARTNERS_ACCESS_KEY = config("COUPANG_PARTNERS_ACCESS_KEY", default="")
COUPANG_PARTNERS_SECRET_KEY = config("COUPANG_PARTNERS_SECRET_KEY", default="")

# ─────────────────────────────────────────────────────────────
# 외부 링크 메타 스크랩 (link/fetch-meta) — anti-bot 폴백
# ─────────────────────────────────────────────────────────────
# 오늘의집·네이버 스마트스토어 등 Akamai/WAF 로 서버 직접 fetch 가 막히는 사이트는
# 외부 스크랩 서비스(residential IP + 렌더링)로만 메타를 가져올 수 있다.
# PROVIDER 미설정 시 폴백 비활성 — 차단 사이트는 빈 {} 응답(=수동 입력).
# 직접 fetch 가 실패할 때만 호출하므로 유료 호출은 최소화된다.
LINK_SCRAPER_PROVIDER = config("LINK_SCRAPER_PROVIDER", default="")  # scraperapi | scrapingbee
LINK_SCRAPER_API_KEY = config("LINK_SCRAPER_API_KEY", default="")
LINK_SCRAPER_RENDER_JS = config("LINK_SCRAPER_RENDER_JS", default=True, cast=bool)
LINK_SCRAPER_COUNTRY = config("LINK_SCRAPER_COUNTRY", default="")  # 예: kr (플랜 지원 시)
LINK_SCRAPER_TIMEOUT = config("LINK_SCRAPER_TIMEOUT", default=20, cast=int)  # read timeout(초)
# provider 별 프리미엄/스텔스 플래그 (Akamai 우회용). 예: "premium=true,stealth_proxy=true"
LINK_SCRAPER_EXTRA_PARAMS = config("LINK_SCRAPER_EXTRA_PARAMS", default="")

# ─────────────────────────────────────────────────────────────
# TikTok Business API (business-api.tiktok.com)
# ─────────────────────────────────────────────────────────────
# Scope: Ad Comments + TikTok Accounts. 광고 댓글 list/hide/delete/reply +
# blockedword 관리. 영상 발행은 지원하지 않음.
TIKTOK_MOCK_MODE = config("TIKTOK_MOCK_MODE", default=True, cast=bool)
TIKTOK_BUSINESS_APP_ID = config("TIKTOK_BUSINESS_APP_ID", default="")
TIKTOK_BUSINESS_APP_SECRET = config("TIKTOK_BUSINESS_APP_SECRET", default="")
TIKTOK_BUSINESS_REDIRECT_URI = config("TIKTOK_BUSINESS_REDIRECT_URI", default="")

# ─────────────────────────────────────────────────────────────
# YouTube / Google OAuth (Data API v3)
# ─────────────────────────────────────────────────────────────
# Default daily quota: 10,000 units. videos.insert costs 1,600 units per call.
GOOGLE_OAUTH_CLIENT_ID = config("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = config("GOOGLE_OAUTH_CLIENT_SECRET", default="")
GOOGLE_OAUTH_REDIRECT_URI = config("GOOGLE_OAUTH_REDIRECT_URI", default="")
YOUTUBE_MOCK_MODE = config("YOUTUBE_MOCK_MODE", default=True, cast=bool)
YOUTUBE_DAILY_QUOTA = config("YOUTUBE_DAILY_QUOTA", default=10000, cast=int)

# Resend (Email)
RESEND_API_KEY = config("RESEND_API_KEY", default="")
RESEND_FROM_EMAIL = config("RESEND_FROM_EMAIL", default="no-reply@turnflow.clfy.ai.kr")
RESEND_FROM_NAME = config("RESEND_FROM_NAME", default="TurnFlow")

# Frontend URL (used in email verification / password reset links)
FRONTEND_URL = config("FRONTEND_URL", default="http://localhost:3000")

# AI 레퍼런스 페이지 스냅샷 캡쳐 대상 (Playwright headless Chromium).
# 실서버: https://turnflow.link, 개발: ngrok 도메인.
# 미지정 시 FRONTEND_URL 로 폴백.
SNAPSHOT_BASE_URL = config("SNAPSHOT_BASE_URL", default=FRONTEND_URL)

# ── AI 페이지 생성 — 스크린샷 비평 보정 루프 ──────────────────────
# True 면 새-페이지 생성 후 렌더 스크린샷을 비전 모델로 비평해 디자인(색/CSS)을 1~2회 보정한다.
# 기본 ON. 단 렌더+비전 호출로 페이지당 +20~40s 지연이 있으니, 지연이 부담되면(또는 운영에서
# 프리미엄 티어 한정으로 쓰려면) AI_VISUAL_REFINE=False 로 끌 수 있다. 켜려면 SNAPSHOT_BASE_URL
# 이 실제 렌더 가능한 프론트(예: app.turnflow.link)를 가리켜야 한다.
AI_VISUAL_REFINE = config(
    "AI_VISUAL_REFINE", default=False, cast=bool
)  # 론칭 기본 OFF(#4): 페이지당 +20~40s(Chromium 렌더+gemma 비전 패스)·shm/vLLM 부하 제거. 켜려면 env 로.
AI_VISUAL_REFINE_CYCLES = config("AI_VISUAL_REFINE_CYCLES", default=1, cast=int)
# 비평기 모델 — 생성기와 다른 독립 모델 권장(비전 필수). 무료 자체호스팅 gemma-4 기본.
AI_CRITIC_MODEL = config("AI_CRITIC_MODEL", default="gemma-4")

# 이미지 관련도 게이트: Pixabay 후보 N장을 비전 모델에 보여 키워드에 맞는 1장을 고르는 옵션.
# 키워드당 비전 1콜이 이미지 단계 지연의 주범이라 **기본 OFF**(검색 1순위 그대로 사용) —
# 무관한 스톡이 간헐 혼입되는 트레이드오프는 감수(사용자 결정 2026-06-11). 필요 시 env 로 재활성화.
AI_IMAGE_VLM_RERANK = config("AI_IMAGE_VLM_RERANK", default=False, cast=bool)
AI_IMAGE_VLM_MODEL = config("AI_IMAGE_VLM_MODEL", default="gemma-4")

# Service metadata (used as default email template variables)
SERVICE_NAME = config("SERVICE_NAME", default="TurnFlow")
SUPPORT_EMAIL = config("SUPPORT_EMAIL", default="support@turnflow.clfy.ai.kr")

# Email token lifetimes
EMAIL_VERIFICATION_TTL_MINUTES = config("EMAIL_VERIFICATION_TTL_MINUTES", default=30, cast=int)
PASSWORD_RESET_TTL_MINUTES = config("PASSWORD_RESET_TTL_MINUTES", default=60, cast=int)

# 미인증 계정 정리(authentication.cleanup_unverified_accounts) — 비가역 삭제이므로 안전 기본값.
# ENABLED=False 면 태스크가 아무것도 하지 않음. DRY_RUN=True 면 후보만 로그로 남기고 삭제하지 않음.
# 운영 투입: 먼저 ENABLED=True + DRY_RUN=True 로 후보를 며칠 관찰 → 이후 DRY_RUN=False 로 실삭제 전환.
UNVERIFIED_ACCOUNT_CLEANUP_ENABLED = config(
    "UNVERIFIED_ACCOUNT_CLEANUP_ENABLED", default=False, cast=bool
)
UNVERIFIED_ACCOUNT_CLEANUP_DRY_RUN = config(
    "UNVERIFIED_ACCOUNT_CLEANUP_DRY_RUN", default=True, cast=bool
)
UNVERIFIED_ACCOUNT_RETENTION_DAYS = config("UNVERIFIED_ACCOUNT_RETENTION_DAYS", default=1, cast=int)

# ===== 댓글 웹훅 누락 보정 (integrations.poll_missed_comments) =====
# Instagram comments 웹훅이 유실되면 트리거 댓글이 누락되므로, 시간당 댓글 edge 를 재조회해
# 누락 DM 을 보정한다. 본문은 저장 안 하고 comment_id 최소 장부(SeenComment)만 TTL 보관.
# 발송량은 기존 rate_governor 가 throttle 하므로 폴링 측 추가 제한 불필요.
MISSED_COMMENT_POLL_ENABLED = config("MISSED_COMMENT_POLL_ENABLED", default=True, cast=bool)
MISSED_COMMENT_LEDGER_TTL_DAYS = config("MISSED_COMMENT_LEDGER_TTL_DAYS", default=10, cast=int)
PRIVATE_REPLY_WINDOW_DAYS = config("PRIVATE_REPLY_WINDOW_DAYS", default=7, cast=int)
MISSED_COMMENT_POLL_PAGE_SIZE = config("MISSED_COMMENT_POLL_PAGE_SIZE", default=50, cast=int)
# 폭주 방지 상한 — 정상 종료는 앵커 또는 7일 baseline. 소진 시 Telegram 경고.
MISSED_COMMENT_POLL_MAX_PAGES = config("MISSED_COMMENT_POLL_MAX_PAGES", default=20, cast=int)
MISSED_COMMENT_POLL_MAX_TARGETS = config("MISSED_COMMENT_POLL_MAX_TARGETS", default=1000, cast=int)

# ===== EventInbox 일별 파티션 유지 (integrations.maintain_partitions, §15.8) =====
# 보존일 초과 일별 파티션은 즉시 DROP(WAL≈0). 행 도착 전에 파티션이 있어야 DEFAULT 로 안 새므로
# DAYS_AHEAD 만큼 미리 선생성. 보존일은 웹훅 재전송 창(~36h)보다 넉넉히 크게(기본 7일).
EVENTINBOX_PARTITION_RETENTION_DAYS = config(
    "EVENTINBOX_PARTITION_RETENTION_DAYS", default=7, cast=int
)
# 14일 선생성 버퍼 — maintain_partitions(일1회 beat)가 며칠 누락돼도 DEFAULT 로 새지 않게 여유.
EVENTINBOX_PARTITION_DAYS_AHEAD = config("EVENTINBOX_PARTITION_DAYS_AHEAD", default=14, cast=int)
# SentDMLog 배치 아카이브 — 0 이면 비활성(기본). ⚠️ 활성화 전 R2 export 선행 필수(업무기록 손실 방지).
SENTDMLOG_ARCHIVE_RETENTION_DAYS = config("SENTDMLOG_ARCHIVE_RETENTION_DAYS", default=0, cast=int)

# Onboarding drip campaign offsets (days after signup)
ONBOARDING_DRIP_DAYS = [3, 7, 14]

# 3/7/14 day drip campaign toggle (marketing-style).
# Welcome, verification, password reset are transactional and unaffected by this.
# Re-enable later by setting ONBOARDING_ENABLED=True in .env.
ONBOARDING_ENABLED = config("ONBOARDING_ENABLED", default=False, cast=bool)

# CSRF trusted origins
# Use a comma-separated env var `CSRF_TRUSTED_ORIGINS`, e.g.
# CSRF_TRUSTED_ORIGINS=https://pro-earwig-presently.ngrok-free.app,https://example.com
_csrf_env = config("CSRF_TRUSTED_ORIGINS", default="")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in _csrf_env.split(",") if o.strip()]

# ─────────────────────────────────────────────────────────────
# DR (Disaster Recovery) — active_site 락 / 헬스 / 외부 스케줄러
# 상세: DR_IMPLEMENTATION_PLAN.md
# ─────────────────────────────────────────────────────────────
# 이 서버의 정체성. 콜로=colo / 사내=office / Azure=azure. .env 로 서버별 지정.
SITE_ID = config("SITE_ID", default="colo")

# passive 사이트가 읽기 GET 을 서빙할지. 기본 False = fully-dark(쓰기·읽기 모두 503, 헬스/내부 제외).
PASSIVE_ALLOW_READS = config("PASSIVE_ALLOW_READS", default=False, cast=bool)

# /healthz/ready 가 스케줄러 heartbeat 신선도까지 요구할지. 기본 False(초기 alert-only, 자기유발 failover 방지).
READY_REQUIRE_SCHEDULER_HEARTBEAT = config(
    "READY_REQUIRE_SCHEDULER_HEARTBEAT", default=False, cast=bool
)
# /healthz/ready 가 형제 tier(web_webhook/web_external)의 /healthz/live 까지 프로빙할지.
READY_PROBE_SIBLINGS = config("READY_PROBE_SIBLINGS", default=False, cast=bool)
# 프로빙 대상 URL(콤마구분). 예: "http://web_webhook:8000/api/v1/healthz/live,http://web_external:8000/api/v1/healthz/live"
_ready_siblings = config("READY_SIBLING_URLS", default="")
READY_SIBLING_URLS = [u.strip() for u in _ready_siblings.split(",") if u.strip()]

# 외부 스케줄러 tick 인증 — 공유 시크릿(상수시간 비교) + 송신 IP 허용목록.
SCHEDULER_TICK_SECRET = config("SCHEDULER_TICK_SECRET", default="")
_tick_ips = config("SCHEDULER_TICK_ALLOWED_IPS", default="")
SCHEDULER_TICK_ALLOWED_IPS = [ip.strip() for ip in _tick_ips.split(",") if ip.strip()]
# tick 성공 시 ping 할 dead-man 모니터 URL(Healthchecks.io 등). 비면 no-op.
HEALTHCHECKS_TICK_URL = config("HEALTHCHECKS_TICK_URL", default="")
