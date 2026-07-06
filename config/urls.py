"""
URL configuration for Instagram Service Backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("config.api_urls")),
]

# OpenAPI/Swagger — 개발(DEBUG)에서만 노출.
# 프로덕션에서는 전체 API 표면(엔드포인트/파라미터/스키마)이 무인증으로 공개되는 것을
# 막기 위해 등록하지 않는다(→ /api/schema, /api/docs, /api/redoc 모두 404).
# 프론트 개발자는 dev-api(로컬 도커, DEBUG=True)에서 문서를 확인한다.
# 참고: SECURITY_AUDIT_2026-06.md (인프라·설정)
if settings.DEBUG:
    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
        path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    ]

# 미디어 파일 서빙
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    from django.views.static import serve
    from django.urls import re_path

    urlpatterns += [
        re_path(
            r"^media/(?P<path>.*)$",
            serve,
            {"document_root": settings.MEDIA_ROOT},
        ),
    ]
