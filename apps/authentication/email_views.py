"""이메일 등록·인증 — ``POST /api/v1/auth/me/email/`` + ``.../email/verify/``.

⭐ **왜 필요한가**: Instagram Business Login 은 이메일을 주지 않아 IG 로 가입한 사용자는
``ig_<id>@ig.invalid`` 자리표시를 갖는다. 그 계정에는 **체험 종료 안내·연결 끊김 알림이
한 통도 못 간다** — 수익화 루프의 구멍이다. 이 두 엔드포인트가 그 구멍의 착지점이다.

⭐ **왜 ``EMAIL_VERIFY`` 를 재사용하지 않는가**: 저쪽은 "이미 내 계정에 달린 주소"를
확인하는 절차고, 이쪽은 "아직 내 계정에 없는 주소"를 소유 증명하는 절차다. 같은 purpose 를
쓰면 **가입 때 받은 인증 코드로 이메일을 바꿀 수 있다**. 그래서 purpose 를 분리했다.

⚠️ **범위 제한 — 자리표시 이메일 계정만.** 일반 "이메일 변경" 기능이 아니다.
   비밀번호가 있는 계정에 이걸 열면 세션 하나만 탈취해도 [이메일 변경 → 비밀번호 재설정]
   으로 계정을 통째로 가져갈 수 있다. 넓히려면 **현재 비밀번호 확인**을 함께 붙일 것.
   자리표시 계정은 애초에 이메일 기반 복구 경로가 없어 그 승격 경로 자체가 존재하지 않는다.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import BaseUserManager
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.emails.models import EmailToken, EmailTokenPurpose

from .serializers import UserSerializer

logger = logging.getLogger(__name__)
User = get_user_model()


def _error(message: str, code: str, http_status: int) -> Response:
    """``detail``/``code`` + §6 통일 envelope 동시 송출 (kakao/instagram 뷰와 같은 규칙)."""
    return Response(
        {
            "detail": message,
            "code": code,
            "success": False,
            "error": {"code": http_status, "message": message, "details": {"code": code}},
        },
        status=http_status,
    )


def _ttl_minutes() -> int:
    return int(getattr(settings, "EMAIL_CHANGE_TTL_MINUTES", 30))


class EmailRegisterRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="등록할 이메일 주소. 이 주소로 6자리 코드가 갑니다.")


class EmailRegisterVerifySerializer(serializers.Serializer):
    code = serializers.CharField(
        min_length=6, max_length=6, help_text="새 주소로 받은 6자리 숫자 코드"
    )


class EmailRegisterView(APIView):
    """이메일 등록 신청 — 새 주소로 6자리 코드를 보낸다."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "email_change"

    @extend_schema(
        tags=["authentication"],
        summary="이메일 등록 신청",
        description="""
## 개요
알림을 받을 이메일 주소를 등록합니다. 입력한 **새 주소로** 6자리 인증 코드를 보내고,
`POST /api/v1/auth/me/email/verify/` 에서 그 코드를 확인해야 실제로 반영됩니다.

## 사용 시나리오
인스타그램으로 가입한 사용자는 이메일이 없습니다(`user.email_is_placeholder === true`).
그 배너의 "이메일 등록" 버튼이 이 API 를 호출합니다.

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
1. **자리표시 이메일 계정만** 사용할 수 있습니다(`email_is_placeholder === true`).
   그 외에는 400 `EMAIL_CHANGE_NOT_ALLOWED` — 일반 "이메일 변경" 기능이 아닙니다.
2. 이미 다른 계정이 쓰는 주소면 409 `EMAIL_ALREADY_TAKEN`
3. 통과하면 `user.pending_email` 에 후보 주소를 담고, 그 주소로 코드 메일을 보냅니다.
   **이 시점에 `user.email` 은 바뀌지 않습니다** — 오타 한 번에 로그인 키가 아무도
   소유하지 않는 주소로 넘어가면 복구 경로가 사라지기 때문입니다.
4. 다시 신청하면 **이전 코드는 즉시 무효**가 됩니다(직전 주소로 받은 코드로 새 주소를
   확정해 버리는 것을 막습니다).

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `email` | ✅ | email | 등록할 주소. 이 주소로 코드가 발송됩니다 |

## 응답 (202)
```json
{ "detail": "인증 코드를 보냈습니다.", "pending_email": "me@example.com", "expires_minutes": 30 }
```

## 주의사항
- 스로틀 **5회/시간**(사용자 기준) — 메일 폭탄 방어입니다. 초과 시 429.
- 코드 유효시간은 기본 30분(`EMAIL_CHANGE_TTL_MINUTES`).
- 등록이 끝나기 전까지 `user.email` 과 `email_is_placeholder` 는 그대로입니다.
  진행 상태는 `user.pending_email` 로 확인하세요(`GET /auth/me/` 에 포함).

## 사용 예시
```javascript
await fetch('/api/v1/auth/me/email/', {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: 'me@example.com' }),
});
```
        """,
        request=EmailRegisterRequestSerializer,
        responses={
            202: OpenApiResponse(
                description="코드 발송됨",
                examples=[
                    OpenApiExample(
                        "발송됨",
                        value={
                            "detail": "인증 코드를 보냈습니다.",
                            "pending_email": "me@example.com",
                            "expires_minutes": 30,
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="자리표시 이메일 계정이 아니거나 형식 오류",
                examples=[
                    OpenApiExample(
                        "대상 아님",
                        value={
                            "detail": "이 계정은 이메일 등록 대상이 아닙니다.",
                            "code": "EMAIL_CHANGE_NOT_ALLOWED",
                        },
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            409: OpenApiResponse(
                description="이미 다른 계정이 쓰는 주소",
                examples=[
                    OpenApiExample(
                        "중복",
                        value={
                            "detail": "이미 사용 중인 이메일입니다.",
                            "code": "EMAIL_ALREADY_TAKEN",
                        },
                    )
                ],
            ),
            429: OpenApiResponse(description="요청이 너무 잦습니다 (사용자 기준 5/hour)"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def post(self, request):
        serializer = EmailRegisterRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # 도메인부만 소문자화 — Django 기본 규칙과 같게(로컬파트 대소문자는 보존).
        new_email = BaseUserManager.normalize_email(serializer.validated_data["email"]).strip()

        user = request.user
        if not user.email_is_placeholder:
            return _error(
                "이 계정은 이메일 등록 대상이 아닙니다.",
                "EMAIL_CHANGE_NOT_ALLOWED",
                status.HTTP_400_BAD_REQUEST,
            )
        if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
            return _error(
                "이미 사용 중인 이메일입니다.",
                "EMAIL_ALREADY_TAKEN",
                status.HTTP_409_CONFLICT,
            )

        ttl = _ttl_minutes()
        with transaction.atomic():
            # 이전 신청의 코드를 죽인다 — 살려 두면 **직전 주소로 받은 코드**로 방금 바꾼
            # 새 주소를 확정할 수 있다(토큰은 주소를 들고 있지 않고 pending_email 이 든다).
            EmailToken.objects.filter(
                user=user, purpose=EmailTokenPurpose.EMAIL_CHANGE, used_at__isnull=True
            ).update(used_at=timezone.now())
            user.pending_email = new_email
            user.save(update_fields=["pending_email"])
            token_row, _raw = EmailToken.issue(
                user=user, purpose=EmailTokenPurpose.EMAIL_CHANGE, ttl_minutes=ttl
            )

        from apps.emails.tasks import send_email_change_code

        transaction.on_commit(
            lambda: send_email_change_code.delay(user.id, new_email, token_row.code, ttl)
        )
        logger.info("email register requested: user=%s", user.id)
        return Response(
            {
                "detail": "인증 코드를 보냈습니다.",
                "pending_email": new_email,
                "expires_minutes": ttl,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class EmailRegisterVerifyView(APIView):
    """이메일 등록 확정 — 코드가 맞으면 ``pending_email`` 을 ``email`` 로 승격."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    # 6자리 코드 무차별 대입 방어 — 기존 이메일 인증과 같은 급.
    throttle_scope = "email_verify"

    @extend_schema(
        tags=["authentication"],
        summary="이메일 등록 확정",
        description="""
## 개요
`POST /api/v1/auth/me/email/` 로 신청한 주소에 도착한 **6자리 코드**를 확인해 등록을
확정합니다. 성공하면 `user.email` 이 새 주소로 바뀌고 `email_is_placeholder` 가
`false`, `is_email_verified` 가 `true` 가 됩니다.

## 사용 시나리오
"이메일 등록" 배너 → 주소 입력 → 코드 입력 화면에서 호출.

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
1. `pending_email` 이 없으면 400 `NO_PENDING_EMAIL` (신청부터 다시)
2. 코드가 틀리거나 만료면 400 `EMAIL_CODE_INVALID` (코드는 **1회용**)
3. 그 사이 다른 계정이 같은 주소를 가져갔으면 409 `EMAIL_ALREADY_TAKEN`
   → `pending_email` 을 비우고 다시 신청하게 합니다
4. 통과 → `email` 승격 + `is_email_verified=true` + `pending_email` 비움

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `code` | ✅ | string(6) | 새 주소로 받은 6자리 숫자 |

## 응답 (200)
갱신된 사용자 프로필 전체를 돌려줍니다(`GET /auth/me/` 와 같은 스키마).
```json
{ "detail": "이메일이 등록되었습니다.",
  "user": { "id": 1234, "email": "me@example.com",
            "email_is_placeholder": false, "is_email_verified": true, "pending_email": "" } }
```

## 주의사항
- 이 시점부터 체험 종료 안내·연결 끊김 알림 메일이 이 주소로 갑니다.
- 등록이 끝나면 `email_is_placeholder` 가 `false` 가 되어 **이 API 를 다시 쓸 수 없습니다**
  (400 `EMAIL_CHANGE_NOT_ALLOWED`). 일반적인 이메일 변경 기능은 아직 없습니다.
- 코드 입력 실패도 스로틀 대상입니다(IP 기준 10/min).

## 사용 예시
```javascript
const res = await fetch('/api/v1/auth/me/email/verify/', {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ code: '123456' }),
});
const { user } = await res.json();   // user.email_is_placeholder === false
```
        """,
        request=EmailRegisterVerifySerializer,
        responses={
            200: OpenApiResponse(
                response=UserSerializer,
                description="등록 완료",
                examples=[
                    OpenApiExample(
                        "완료",
                        value={
                            "detail": "이메일이 등록되었습니다.",
                            "user": {
                                "id": 1234,
                                "email": "me@example.com",
                                "email_is_placeholder": False,
                                "is_email_verified": True,
                                "pending_email": "",
                            },
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="대기 중인 신청이 없거나 코드가 유효하지 않음",
                examples=[
                    OpenApiExample(
                        "코드 불일치",
                        value={
                            "detail": "유효하지 않거나 만료된 인증 코드입니다.",
                            "code": "EMAIL_CODE_INVALID",
                        },
                    ),
                    OpenApiExample(
                        "신청 없음",
                        value={
                            "detail": "진행 중인 이메일 등록 신청이 없습니다.",
                            "code": "NO_PENDING_EMAIL",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            409: OpenApiResponse(
                description="그 사이 다른 계정이 같은 주소를 가져감 — 다시 신청 필요"
            ),
            429: OpenApiResponse(description="요청이 너무 잦습니다 (IP 기준 10/min)"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def post(self, request):
        serializer = EmailRegisterVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = serializer.validated_data["code"].strip()

        user = request.user
        pending = (user.pending_email or "").strip()
        if not pending:
            return _error(
                "진행 중인 이메일 등록 신청이 없습니다.",
                "NO_PENDING_EMAIL",
                status.HTTP_400_BAD_REQUEST,
            )

        token_row = EmailToken.consume(user=user, code=code, purpose=EmailTokenPurpose.EMAIL_CHANGE)
        if token_row is None:
            return _error(
                "유효하지 않거나 만료된 인증 코드입니다.",
                "EMAIL_CODE_INVALID",
                status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            # 신청과 확정 사이에 남이 가져갔을 수 있다 — 여기서 다시 본다.
            if User.objects.filter(email__iexact=pending).exclude(pk=user.pk).exists():
                user.pending_email = ""
                user.save(update_fields=["pending_email"])
                return _error(
                    "이미 사용 중인 이메일입니다. 다른 주소로 다시 시도해 주세요.",
                    "EMAIL_ALREADY_TAKEN",
                    status.HTTP_409_CONFLICT,
                )
            user.email = pending
            user.pending_email = ""
            user.is_email_verified = True
            user.email_verified_at = timezone.now()
            user.save(
                update_fields=[
                    "email",
                    "pending_email",
                    "is_email_verified",
                    "email_verified_at",
                ]
            )

        logger.info("email registered: user=%s", user.id)
        return Response(
            {"detail": "이메일이 등록되었습니다.", "user": UserSerializer(user).data},
            status=status.HTTP_200_OK,
        )
