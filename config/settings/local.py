"""
Local development settings
"""

from .base import *

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config("DEBUG", default=True, cast=bool)

ALLOWED_HOSTS = config(
    "ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=lambda v: [s.strip() for s in v.split(",")]
)

# Django Debug Toolbar (optional - uncomment if needed)
# INSTALLED_APPS += ['debug_toolbar']
# MIDDLEWARE.insert(0, 'debug_toolbar.middleware.DebugToolbarMiddleware')
# INTERNAL_IPS = ['127.0.0.1']

# CORS settings for local development
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS",
    default="http://localhost:3000,http://localhost:8000",
    cast=lambda v: [s.strip() for s in v.split(",")],
)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    "ngrok-skip-browser-warning",  # ngrok 경고 스킵용 헤더
]

# REST Framework - Add BrowsableAPIRenderer for development
REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = [
    "rest_framework.renderers.JSONRenderer",
    "rest_framework.renderers.BrowsableAPIRenderer",
]

# Email backend for development (console)
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Celery - Eager execution for development (optional)
# CELERY_TASK_ALWAYS_EAGER = True
# CELERY_TASK_EAGER_PROPAGATES = True
DATABASES["default"]["CONN_MAX_AGE"] = 0

# 리버스 프록시(Cloudflare Tunnel / ngrok) 뒤에서 dev 서버에 접속할 때:
# 엣지가 TLS 를 종단하고 origin 으로는 평문 HTTP 로 전달하므로, X-Forwarded-Proto 를 신뢰해야
# request.is_secure()/scheme 이 https 로 잡힌다. (없으면 https 로 연 Swagger/Browsable API 의
# 절대 URL 이 http 로 생성돼 mixed-content/CSRF 문제 발생.) 직접 localhost:8000 접속엔 이 헤더가
# 없어 영향 없음. prod.py 는 이미 동일 설정 보유. dev 박스라 헤더 스푸핑 위험은 무시 가능.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")