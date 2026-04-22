"""
User-facing email endpoints:

- POST /api/v1/auth/email/send-verification/   (authenticated)
- POST /api/v1/auth/email/verify/              (public — token OR email+code)
- POST /api/v1/auth/password/reset-request/    (public)
- POST /api/v1/auth/password/reset-confirm/    (public)
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import EmailToken, EmailTokenPurpose
from .serializers import (
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    ResendVerificationSerializer,
    VerifyEmailRequestSerializer,
)
from .tasks import send_password_reset_email, send_verification_email

User = get_user_model()


def _client_ip(request) -> str | None:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class SendVerificationEmailView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ResendVerificationSerializer

    @extend_schema(
        tags=["auth"],
        summary="이메일 인증 코드 재전송",
        description=f"""
## 개요
현재 로그인한 사용자에게 이메일 인증 코드 + 인증 링크를 재발송합니다.
이미 인증된 계정이면 `409 Conflict`를 반환합니다.

## 인증
`Authorization: Bearer <access_token>`

## 동작
1. 기존 미사용 코드가 있더라도 새 코드를 발급합니다.
2. 비동기 큐(Celery) 에 적재하고 즉시 `202 Accepted` 로 응답.
3. 코드/링크 유효시간: **{settings.EMAIL_VERIFICATION_TTL_MINUTES}분**.

## 사용 예시
```javascript
await fetch('/api/v1/auth/email/send-verification/', {{
    method: 'POST',
    headers: {{ 'Authorization': `Bearer ${{token}}` }},
}});
```
        """,
        request=None,
        responses={
            202: OpenApiResponse(
                description="큐잉 완료",
                response={
                    "type": "object",
                    "properties": {
                        "detail": {"type": "string"},
                        "expires_minutes": {"type": "integer"},
                    },
                },
            ),
            401: OpenApiResponse(description="인증 누락/만료"),
            409: OpenApiResponse(description="이미 인증된 계정"),
        },
    )
    def post(self, request):
        user = request.user
        if user.is_email_verified:
            return Response(
                {"detail": "이미 인증된 이메일입니다."}, status=status.HTTP_409_CONFLICT
            )
        send_verification_email.delay(user.id)
        return Response(
            {
                "detail": "인증 메일을 발송했습니다.",
                "expires_minutes": settings.EMAIL_VERIFICATION_TTL_MINUTES,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class VerifyEmailView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = VerifyEmailRequestSerializer

    @extend_schema(
        tags=["auth"],
        summary="이메일 인증 확인",
        description="""
## 개요
아래 **둘 중 하나** 의 방법으로 이메일을 인증합니다.

1. **클릭 링크**: `{{ verification_url }}` 에서 받은 `token` 을 그대로 전달
2. **코드 입력**: 사용자가 받은 `email` + 6자리 `code` 조합

## 인증
공개 API — 인증 헤더 불필요 (token 자체가 증거 역할).

## 성공 시 동작
- 사용자의 `is_email_verified=True`, `email_verified_at=now()` 업데이트
- 토큰은 1회용으로 즉시 무효화됩니다.

## 에러
| 코드 | 원인 |
|------|------|
| 400 | token/code 형식 오류, 또는 만료/사용됨 |
| 404 | 이메일에 해당하는 사용자 없음 (code 방식만) |
        """,
        request=VerifyEmailRequestSerializer,
        examples=[
            OpenApiExample("토큰 방식", request_only=True, value={"token": "abcdef..."}),
            OpenApiExample(
                "코드 방식",
                request_only=True,
                value={"email": "user@example.com", "code": "482930"},
            ),
        ],
        responses={
            200: OpenApiResponse(
                description="인증 성공",
                response={
                    "type": "object",
                    "properties": {"is_email_verified": {"type": "boolean"}},
                },
            ),
            400: OpenApiResponse(description="토큰 만료/잘못됨"),
            404: OpenApiResponse(description="이메일 해당 사용자 없음"),
        },
    )
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        token_row = None
        if data.get("token"):
            token_row = EmailToken.consume(
                raw_token=data["token"], purpose=EmailTokenPurpose.EMAIL_VERIFY
            )
        else:
            try:
                user = User.objects.get(email=data["email"])
            except User.DoesNotExist:
                return Response(
                    {"detail": "해당 이메일의 사용자를 찾을 수 없습니다."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            token_row = EmailToken.consume(
                user=user, code=data["code"], purpose=EmailTokenPurpose.EMAIL_VERIFY
            )

        if not token_row:
            return Response(
                {"detail": "유효하지 않거나 만료된 인증 정보입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = token_row.user
        if not user.is_email_verified:
            user.is_email_verified = True
            user.email_verified_at = timezone.now()
            user.save(update_fields=["is_email_verified", "email_verified_at"])

        return Response({"is_email_verified": True})


class PasswordResetRequestView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = PasswordResetRequestSerializer

    @extend_schema(
        tags=["auth"],
        summary="비밀번호 재설정 요청",
        description=f"""
## 개요
입력한 이메일 주소로 비밀번호 재설정 링크/코드를 발송합니다.

## 보안
계정 존재 여부를 드러내지 않기 위해 **항상 202** 로 응답합니다. 존재하지 않는 이메일이라도 동일 응답.

## 유효 시간
{settings.PASSWORD_RESET_TTL_MINUTES}분

## 프론트 통합
```javascript
await fetch('/api/v1/auth/password/reset-request/', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{ email }}),
}});
```
        """,
        request=PasswordResetRequestSerializer,
        responses={
            202: OpenApiResponse(
                description="큐잉 완료 (계정 존재 여부와 관계 없이 동일 응답)",
                response={"type": "object", "properties": {"detail": {"type": "string"}}},
            ),
        },
    )
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        user = User.objects.filter(email=email, is_active=True).first()
        if user and user.has_usable_password():
            send_password_reset_email.delay(user.id)

        return Response(
            {"detail": "재설정 메일을 발송했습니다 (해당 계정이 존재하는 경우)."},
            status=status.HTTP_202_ACCEPTED,
        )


class PasswordResetConfirmView(generics.GenericAPIView):
    permission_classes = [AllowAny]
    serializer_class = PasswordResetConfirmSerializer

    @extend_schema(
        tags=["auth"],
        summary="비밀번호 재설정 확인",
        description="""
## 개요
메일에서 받은 `token` 과 새 비밀번호로 계정 비밀번호를 변경합니다.

## 동작
- 비밀번호 변경 성공 시 `is_email_verified=True` 자동 설정 (메일함 접근 가능 증거)
- 발급된 토큰은 1회용으로 즉시 무효화
- **모든 기존 refresh 토큰을 블랙리스트 처리** (다른 기기 강제 로그아웃)

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 토큰 만료/사용됨, 비밀번호 불일치 또는 정책 위반 |
        """,
        request=PasswordResetConfirmSerializer,
        responses={
            200: OpenApiResponse(
                description="재설정 완료",
                response={"type": "object", "properties": {"detail": {"type": "string"}}},
            ),
            400: OpenApiResponse(description="토큰/비밀번호 오류"),
        },
    )
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        token_row = EmailToken.consume(
            raw_token=data["token"], purpose=EmailTokenPurpose.PASSWORD_RESET
        )
        if not token_row:
            return Response(
                {"detail": "유효하지 않거나 만료된 토큰입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = token_row.user
        user.set_password(data["new_password"])
        if not user.is_email_verified:
            user.is_email_verified = True
            user.email_verified_at = timezone.now()
        user.save()

        # Blacklist all outstanding refresh tokens (force re-login everywhere else)
        try:
            from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
            from rest_framework_simplejwt.tokens import RefreshToken

            for out in OutstandingToken.objects.filter(user=user):
                try:
                    RefreshToken(out.token).blacklist()
                except Exception:
                    pass
        except ImportError:
            pass

        return Response({"detail": "비밀번호가 재설정되었습니다."})
