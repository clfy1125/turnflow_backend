"""
Production settings
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

# ── H-2: SECRET_KEY fail-fast 가드 ──
# .env.production 에 SECRET_KEY 가 누락/오타/placeholder 면 base.py 의 insecure default
# 로 조용히 부팅되어 JWT 위조 + IG 토큰 복호화가 가능해진다. 프로덕션에서는 부팅을 막는다.
# (이 키는 SIMPLE_JWT.SIGNING_KEY + IG 토큰 Fernet 암호화를 겸함 — M-3/M-4 참조.)
if (
    not SECRET_KEY
    or SECRET_KEY.startswith("django-insecure")
    or "CHANGE_ME" in SECRET_KEY.upper()
    or len(SECRET_KEY) < 50
):
    raise ImproperlyConfigured(
        "SECRET_KEY must be a strong, unique value in production "
        "(missing / insecure default / placeholder / too short). "
        "Set a 50+ char random value in .env.production."
    )

# ── C-1: 웹훅 HMAC 서명 검증 강제 (프로덕션 기본 ON) ──
# base.py 기본값은 False(로컬/mock 관측 모드)지만, 프로덕션에서는 위조 웹훅으로 임의 DM
# 트리거를 막기 위해 기본 True 로 상향한다. env 로 명시 override 가능(비상 롤백용).
WEBHOOK_HMAC_ENFORCED = config("WEBHOOK_HMAC_ENFORCED", default=True, cast=bool)
if WEBHOOK_HMAC_ENFORCED and not (
    config("INSTAGRAM_APP_SECRET", default="") or config("META_APP_SECRET", default="")
):
    raise ImproperlyConfigured(
        "WEBHOOK_HMAC_ENFORCED=True requires INSTAGRAM_APP_SECRET or META_APP_SECRET "
        "to be set (otherwise signature verification silently passes)."
    )

ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=lambda v: [s.strip() for s in v.split(",")])
# 컨테이너 내부 healthcheck(Docker)는 http://127.0.0.1:8000/... 으로 호출하므로 내부 호스트를 항상 허용.
# (없으면 DisallowedHost 400 → healthcheck 실패 → web 컨테이너가 unhealthy 로 표시됨)
for _h in ("127.0.0.1", "localhost"):
    if _h not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_h)

# Security settings
# Caddy handles SSL termination, so Django should NOT redirect to HTTPS itself
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=False, cast=bool)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = config("SESSION_COOKIE_SECURE", default=True, cast=bool)
CSRF_COOKIE_SECURE = config("CSRF_COOKIE_SECURE", default=True, cast=bool)
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# HSTS is handled by Caddy; disable Django HSTS to avoid double headers
SECURE_HSTS_SECONDS = 0

# CORS settings for production
CORS_ALLOWED_ORIGINS = config(
    "CORS_ALLOWED_ORIGINS", cast=lambda v: [s.strip() for s in v.split(",")]
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
    "ngrok-skip-browser-warning",
]

# Email backend for production (configure with actual email service)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST", default="")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")

# Logging - production configuration
_LOG_DIR = BASE_DIR / "logs"
os.makedirs(_LOG_DIR, exist_ok=True)

LOGGING["handlers"]["file"] = {
    "class": "logging.handlers.RotatingFileHandler",
    "filename": _LOG_DIR / "django.log",
    "maxBytes": 1024 * 1024 * 10,  # 10 MB
    "backupCount": 10,
    "formatter": "verbose",
    "filters": ["scrub_secrets"],  # H-9/M-22: 파일 로그에도 토큰 마스킹 적용
}

LOGGING["root"]["handlers"].append("file")
LOGGING["loggers"]["django"]["handlers"].append("file")
LOGGING["loggers"]["apps"]["handlers"].append("file")
