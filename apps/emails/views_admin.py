"""
Admin-only endpoints for managing email templates and inspecting logs.

Routed under `/api/v1/admin/emails/`.  Requires `is_staff=True`.
"""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import filters, generics, status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from .constants import AVAILABLE_VARIABLES
from .models import EmailLog, EmailTemplate, EmailStatus
from .serializers import (
    EmailLogDetailSerializer,
    EmailLogSerializer,
    EmailTemplatePreviewSerializer,
    EmailTemplateSerializer,
    EmailTemplateTestSendSerializer,
)
from .services.renderer import render_template
from .services.sender import _strip_html  # internal util


def _sample_context(key: str) -> dict:
    """Generate placeholder values based on the documented variable catalogue."""
    samples = {
        "full_name": "홍길동",
        "email": "user@example.com",
        "verification_code": "482930",
        "verification_url": f"{settings.FRONTEND_URL}/verify-email?token=SAMPLE_TOKEN",
        "reset_code": "593021",
        "reset_url": f"{settings.FRONTEND_URL}/reset-password?token=SAMPLE_TOKEN",
        "expires_minutes": settings.EMAIL_VERIFICATION_TTL_MINUTES,
        "service_name": settings.SERVICE_NAME,
        "support_email": settings.SUPPORT_EMAIL,
        "dashboard_url": f"{settings.FRONTEND_URL}/dashboard",
        "docs_url": f"{settings.FRONTEND_URL}/docs",
        "joined_date": timezone.localdate().isoformat(),
        "feature_highlight": "Auto DM 자동화",
        "tip_of_week": "댓글 키워드 규칙으로 반복 작업을 줄여보세요.",
        "cta_url": f"{settings.FRONTEND_URL}/dashboard",
        "upgrade_url": f"{settings.FRONTEND_URL}/billing/plans",
        "trial_days_left": 9,
    }
    allowed = AVAILABLE_VARIABLES.get(key, {}).keys()
    return {k: v for k, v in samples.items() if k in allowed}


class EmailTemplateListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = EmailTemplateSerializer
    queryset = EmailTemplate.objects.all()

    @extend_schema(
        tags=["admin-emails"],
        summary="[관리자] 이메일 템플릿 목록 조회",
        description="""
## 개요
등록된 모든 이메일 템플릿(인증/비번재설정/환영/온보딩)을 반환합니다.
기본 템플릿이 없으면 `python manage.py seed_email_templates` 로 시드해주세요.

## 인증
- `Authorization: Bearer <staff_access_token>` (is_staff=True 유저만)

## 응답
`EmailTemplate` 객체 배열. 각 항목에는 `referenced_variables`(본문에서 실제 참조 중인 `{{변수}}`)와
`unknown_variables`(카탈로그에 없는 변수)가 함께 반환되므로 편집 직후 바로 린트 결과를 볼 수 있습니다.
        """,
        responses={
            200: EmailTemplateSerializer(many=True),
            401: OpenApiResponse(description="인증 누락/만료"),
            403: OpenApiResponse(description="관리자 권한 없음"),
        },
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class EmailTemplateDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = EmailTemplateSerializer
    queryset = EmailTemplate.objects.all()
    lookup_field = "key"

    @extend_schema(
        tags=["admin-emails"],
        summary="[관리자] 이메일 템플릿 단건 조회",
        description="키(예: `email_verification`)에 해당하는 템플릿 상세 + 사용 가능한 변수 카탈로그 반환.",
        responses={200: EmailTemplateSerializer, 404: OpenApiResponse(description="템플릿 없음")},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        tags=["admin-emails"],
        summary="[관리자] 이메일 템플릿 수정",
        description="""
`subject`, `html_body`, `text_body`, `from_name`, `is_active` 만 수정 가능.
`key`는 변경할 수 없습니다 (시스템 예약어).

저장 후 응답의 `unknown_variables` 배열이 비어있지 않다면, 문서화되지 않은 `{{변수}}` 가 포함된 것으로
해당 변수는 발송 시 치환되지 않고 원본 그대로 남습니다.
        """,
        responses={
            200: EmailTemplateSerializer,
            400: OpenApiResponse(description="유효성 검증 실패"),
            403: OpenApiResponse(description="관리자 권한 없음"),
            404: OpenApiResponse(description="템플릿 없음"),
        },
    )
    def patch(self, request, *args, **kwargs):
        return super().patch(request, *args, **kwargs)

    @extend_schema(exclude=True)
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)

    def perform_update(self, serializer):
        serializer.save(
            updated_by=self.request.user,
            available_variables=AVAILABLE_VARIABLES.get(serializer.instance.key, {}),
        )


class EmailTemplatePreviewView(generics.GenericAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = EmailTemplatePreviewSerializer

    @extend_schema(
        tags=["admin-emails"],
        summary="[관리자] 템플릿 미리보기 (발송 없음)",
        description="""
샘플 변수로 `{{변수}}`를 치환한 렌더링 결과를 반환합니다. 실제 발송은 하지 않습니다.

`context` 를 생략하면 카탈로그 기반 샘플 값이 자동 사용됩니다.
        """,
        request=EmailTemplatePreviewSerializer,
        responses={
            200: OpenApiResponse(
                description="렌더링 결과",
                response={
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string"},
                        "html_body": {"type": "string"},
                        "text_body": {"type": "string"},
                        "context_used": {"type": "object"},
                    },
                },
            ),
            404: OpenApiResponse(description="템플릿 없음"),
        },
    )
    def post(self, request, key):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            template = EmailTemplate.objects.get(key=key)
        except EmailTemplate.DoesNotExist:
            return Response({"detail": "템플릿을 찾을 수 없습니다."}, status=status.HTTP_404_NOT_FOUND)

        ctx = _sample_context(key)
        ctx.update(serializer.validated_data.get("context") or {})

        html = render_template(template.html_body, ctx)
        return Response(
            {
                "subject": render_template(template.subject, ctx),
                "html_body": html,
                "text_body": render_template(template.text_body or _strip_html(html), ctx),
                "context_used": ctx,
            }
        )


class EmailTemplateTestSendView(generics.GenericAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = EmailTemplateTestSendSerializer

    @extend_schema(
        tags=["admin-emails"],
        summary="[관리자] 테스트 이메일 발송",
        description="""
지정한 주소로 실제 Resend 발송을 수행합니다.  `EmailLog`에 기록되며 `status`가 `sent`/`failed` 로 업데이트됩니다.

**주의**: Resend는 `RESEND_FROM_EMAIL` 의 도메인이 대시보드에서 검증된 상태여야 발송됩니다.
        """,
        request=EmailTemplateTestSendSerializer,
        responses={
            202: OpenApiResponse(
                description="발송 큐잉 완료 — EmailLog id 반환",
                response={
                    "type": "object",
                    "properties": {"email_log_id": {"type": "integer"}},
                },
            ),
            400: OpenApiResponse(description="요청 바디 유효성 실패"),
            404: OpenApiResponse(description="템플릿 없음"),
        },
    )
    def post(self, request, key):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            EmailTemplate.objects.get(key=key, is_active=True)
        except EmailTemplate.DoesNotExist:
            return Response(
                {"detail": "템플릿이 없거나 비활성 상태입니다."}, status=status.HTTP_404_NOT_FOUND
            )

        ctx = _sample_context(key)
        ctx.update(serializer.validated_data.get("context") or {})

        from .services.sender import send_email

        log = send_email(key, serializer.validated_data["to_email"], ctx, user=request.user)
        return Response({"email_log_id": log.id}, status=status.HTTP_202_ACCEPTED)


class EmailLogListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = EmailLogSerializer
    queryset = EmailLog.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["status", "template_key"]
    search_fields = ["to_email", "subject", "provider_message_id"]
    ordering_fields = ["created_at", "sent_at"]
    ordering = ["-created_at"]

    @extend_schema(
        tags=["admin-emails"],
        summary="[관리자] 이메일 발송 로그 조회",
        description="필터: `status` (pending/sent/failed/bounced), `template_key`. 검색: `to_email`, `subject`, `provider_message_id`.",
        parameters=[
            OpenApiParameter("status", str, description="필터: pending/sent/failed/bounced"),
            OpenApiParameter("template_key", str, description="필터: 템플릿 키"),
            OpenApiParameter("search", str, description="to_email/subject/provider_message_id 검색"),
        ],
        responses={200: EmailLogSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class EmailLogDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = EmailLogDetailSerializer
    queryset = EmailLog.objects.all()

    @extend_schema(
        tags=["admin-emails"],
        summary="[관리자] 이메일 로그 상세 (렌더링 HTML 포함)",
        description="발송 당시 렌더링된 HTML/텍스트와 context 스냅샷을 포함한 전체 레코드.",
        responses={200: EmailLogDetailSerializer, 404: OpenApiResponse(description="로그 없음")},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
