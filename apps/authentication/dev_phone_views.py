"""dev 전용 — 휴대폰 테스트 계정 조회/재시드 + **인증번호 꺼내기**.

⭐ **인증번호 꺼내기가 이 파일의 존재 이유다.** dev 는 ``SMS_MOCK_MODE=True`` 라 문자가
실제로 나가지 않고, 인증번호는 평문으로 저장되지 않는다(HMAC). 그래서 프론트는
**발송까지만 되고 확인 단계를 영영 못 돈다.** 종전에는 "백엔드에 요청하세요"였는데,
그건 사람이 끼는 병목이라 프론트가 혼자 개발할 수 없다.

⭐ **평문을 저장하지 않는 설계는 그대로 둔다.** 이 엔드포인트는 평문을 "읽어오는" 게
아니라, 대기 중인 인증 행의 **해시를 알려진 값으로 덮어쓰고** 그 값을 돌려준다.
저장 모델(HMAC·평문 미보관)은 운영과 dev 가 완전히 같다 — dev 에서만 평문을 남기는
분기를 만들면 그 분기가 언젠가 운영으로 샌다.

⚠️ **게이트는 2중이다** — ``DEBUG=True`` **그리고** 설정 모듈이 prod 가 아닐 것.
   둘 중 하나라도 어긋나면 경로 자체가 404 다(``apps/home/dev_views.py`` 와 같은 규약).
   인증을 요구하지 않는 이유도 같다: 로그인 전 화면을 되돌릴 때도 써야 하고,
   비밀번호는 어차피 고정 공개값이다.
"""

from __future__ import annotations

import logging

from django.conf import settings
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from . import dev_phone_seed

logger = logging.getLogger(__name__)

#: 꺼내기 엔드포인트가 심어 주는 고정 인증번호. 프론트가 외우기 쉬우라고 고정값이다
#: (dev 전용이라 추측 가능해도 무해하다).
DEV_FIXED_CODE = "123456"


def _is_prod_settings() -> bool:
    return "prod" in (getattr(settings, "SETTINGS_MODULE", "") or "")


def _not_found() -> Response:
    return Response(
        {"success": False, "error": {"code": 404, "message": "Not found.", "details": {}}},
        status=status.HTTP_404_NOT_FOUND,
    )


def _dev_only():
    """운영에서는 존재 자체를 숨긴다 (404). 각 핸들러 안에서의 방어선."""
    if settings.DEBUG and not _is_prod_settings():
        return None
    return _not_found()


class DevOnlyMixin:
    """**모든 메서드**가 운영에서 404 가 되게 한다 — ``dispatch`` 에서 먼저 막는다.

    ⚠️ 핸들러(``get``/``post``) 안에서만 막으면 **DRF 가 메서드 검사를 먼저 해서**
       정의하지 않은 메서드에 ``405 Method Not Allowed`` 가 나간다. 405 는 "이 경로는
       존재한다" 는 신호라, 운영에서 dev 백도어의 위치를 알려주는 셈이다(실측 확인:
       GET 은 404 인데 POST 는 405 였다). 핸들러 안의 ``_dev_only()`` 는 그대로 둔다 —
       이 믹스인을 빠뜨린 뷰가 생겨도 데이터는 새지 않게.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def dispatch(self, request, *args, **kwargs):
        if not (settings.DEBUG and not _is_prod_settings()):
            resp = _not_found()
            resp.accepted_renderer = JSONRenderer()
            resp.accepted_media_type = "application/json"
            resp.renderer_context = {}
            return resp.render()
        return super().dispatch(request, *args, **kwargs)


def _error(message: str, code: str, http_status: int):
    return Response(
        {
            "detail": message,
            "code": code,
            "success": False,
            "error": {"code": http_status, "message": message, "details": {"code": code}},
        },
        status=http_status,
    )


class DevPhoneTestAccountView(DevOnlyMixin, APIView):
    """휴대폰 테스트 계정 목록 조회(GET) / 재시드(POST)."""

    @extend_schema(
        tags=["authentication"],
        summary="[dev] 휴대폰 테스트 계정 목록",
        description="""
## 개요
휴대폰 본인확인 화면을 상태별로 확인할 수 있는 **dev 전용 테스트 계정** 목록입니다.
각 계정의 *현재* 판정값(`phone_verification_required`, `reward`)을 **실제 판정 함수로
계산해** 함께 돌려줍니다 — 고정 주입이 아니므로 "목록에선 보이는데 API 응답은 다르다"가
생기지 않습니다.

## 사용 시나리오
프론트가 각 화면(강제 인증 / 보상 팝업 / 설정)을 만들 때 어떤 계정으로 로그인해야
그 상태가 보이는지 확인합니다.

## 인증
**필요 없습니다.** 운영에서는 경로 자체가 404 입니다(`DEBUG=True` + 비-prod 설정에서만 동작).

## 비즈니스 로직
- `exists: false` 면 아직 안 만든 계정입니다 → `POST .../reseed/` 를 먼저 호출하세요.
- `phone_required_configured: false` 면 서버에 `PHONE_REQUIRED_SINCE` 가 비어 있어
  **P01·P06(필수 계정)이 재현되지 않습니다**(전원이 '기존 회원'으로 판정). 백엔드에 요청하세요.

## 응답 (200)
```json
{
  "phone_required_configured": true,
  "fixed_code_hint": "POST /api/v1/auth/dev/phone-code/ 로 인증번호를 심고 받아가세요",
  "accounts": [
    { "code": "P01", "title": "신규 가입자 — 인증 강제", "email": "p01-required@test.turnflow.link",
      "password": "Test1234!", "exists": true,
      "state": { "phone": "", "phone_verified": false,
                 "phone_verification_required": true,
                 "reward": { "kind": "none", "reason": "new_signup", "days": 0 } } }
  ]
}
```

## 주의사항
- 계정 비밀번호는 전부 `Test1234!` 입니다.
- 시드는 **문자를 보내지 않습니다**(발송 상한·비용을 건드리지 않음).

## 사용 예시
```bash
curl https://dev-api.turnflow.link/api/v1/auth/dev/phone-test-accounts/
```
        """,
        responses={
            200: OpenApiResponse(description="계정 목록"),
            400: OpenApiResponse(description="해당 없음"),
            401: OpenApiResponse(description="해당 없음"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="운영 환경 — 경로가 존재하지 않습니다"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def get(self, request):
        blocked = _dev_only()
        if blocked:
            return blocked
        return Response(
            {
                "phone_required_configured": dev_phone_seed.required_since_ok(),
                "fixed_code_hint": (
                    "인증번호는 POST /api/v1/auth/dev/phone-code/ {email} 로 심고 받아가세요 "
                    f"(항상 {DEV_FIXED_CODE})"
                ),
                "accounts": dev_phone_seed.list_all(),
            }
        )


class DevPhoneTestAccountReseedView(DevOnlyMixin, APIView):
    """테스트 계정을 처음 상태로 되돌린다."""

    @extend_schema(
        tags=["authentication"],
        summary="[dev] 휴대폰 테스트 계정 재시드",
        description="""
## 개요
테스트 계정 6개를 **처음 상태로 되돌립니다**(없으면 만듭니다). 인증을 끝내 버렸거나
번호를 지워서 그 화면을 다시 못 보게 됐을 때 호출하세요.

## 사용 시나리오
"P01 로 인증을 끝냈더니 강제 화면이 안 뜬다" → 재시드하면 다시 미인증 상태가 됩니다.

## 인증
**필요 없습니다.** 운영에서는 404.

## 비즈니스 로직
계정별로 번호·인증이력·보상지급기록·구독상태를 전부 지우고 다시 만듭니다.
**문자는 보내지 않습니다.**

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `codes` | | string[] | 되돌릴 코드만 지정 (예: `["P01"]`). 생략하면 전체 |

## 응답 (200)
`GET` 과 같은 모양의 `accounts` 배열을 돌려줍니다.

## 주의사항
- 되돌린 직후 `state` 를 함께 돌려주므로 별도 `GET` 이 필요 없습니다.

## 사용 예시
```bash
curl -X POST https://dev-api.turnflow.link/api/v1/auth/dev/phone-test-accounts/reseed/ \\
  -H 'Content-Type: application/json' -d '{"codes":["P01"]}'
```
        """,
        request=None,
        responses={
            200: OpenApiResponse(description="재시드 완료"),
            400: OpenApiResponse(description="알 수 없는 코드"),
            401: OpenApiResponse(description="해당 없음"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="운영 환경 — 경로가 존재하지 않습니다"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def post(self, request):
        blocked = _dev_only()
        if blocked:
            return blocked

        codes = request.data.get("codes") or dev_phone_seed.ALL_CODES
        if isinstance(codes, str):
            codes = [codes]
        unknown = [c for c in codes if c not in dev_phone_seed.BY_CODE]
        if unknown:
            return _error(
                f"알 수 없는 코드: {', '.join(unknown)} (사용 가능: {', '.join(dev_phone_seed.ALL_CODES)})",
                "UNKNOWN_CODE",
                status.HTTP_400_BAD_REQUEST,
            )

        accounts = [dev_phone_seed.seed_one(dev_phone_seed.BY_CODE[c]) for c in codes]
        logger.info("dev phone test accounts reseeded: %s", ",".join(codes))
        return Response(
            {
                "phone_required_configured": dev_phone_seed.required_since_ok(),
                "accounts": accounts,
            }
        )


class DevPhoneCodeView(DevOnlyMixin, APIView):
    """대기 중인 인증번호를 **알려진 값으로 심고** 돌려준다."""

    @extend_schema(
        tags=["authentication"],
        summary="[dev] 인증번호 꺼내기",
        description="""
## 개요
`POST /api/v1/auth/me/phone/` 로 발송한 뒤, **그 계정의 인증번호를 `123456` 으로 심고**
돌려줍니다. dev 는 문자가 실제로 나가지 않으므로 이 호출 없이는 확인 단계를 돌 수 없습니다.

## 사용 시나리오
```
① POST /auth/me/phone/            { phone: "010-1234-5678" }   → 202
② POST /auth/dev/phone-code/      { email: "p01-...@..." }     → { code: "123456" }
③ POST /auth/me/phone/verify/     { code: "123456" }           → 200
```
②는 자동화 테스트에서도 그대로 씁니다.

## 인증
**필요 없습니다.** 운영에서는 404.

## 비즈니스 로직
- 그 계정의 **살아있는**(미확인·미만료·미무효) 인증 행을 찾아 `code_hash` 를
  `123456` 의 해시로 덮어씁니다. **평문을 저장하지 않는 구조는 그대로입니다** —
  dev 전용 저장 분기를 만들지 않았습니다.
- 대기 중인 인증이 없으면 400 `NO_PENDING_PHONE_VERIFICATION` → ①부터 다시 하세요.
- 만료(3분)됐으면 역시 400 입니다. 발송 후 바로 호출하세요.

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `email` | ✅ | string | 대상 계정 이메일 |

## 응답 (200)
```json
{ "email": "p01-required@test.turnflow.link", "code": "123456",
  "phone": "010-****-5678", "expires_at": "2026-10-12T20:03:11+09:00" }
```

## 주의사항
- 같은 계정에 발송을 여러 번 했다면 **가장 최근 1건**에만 심습니다(이전 건은 이미 무효).
- 이 엔드포인트는 쿨다운·일일 상한을 **건드리지 않습니다**(문자를 보내지 않으므로).

## 사용 예시
```bash
curl -X POST https://dev-api.turnflow.link/api/v1/auth/dev/phone-code/ \\
  -H 'Content-Type: application/json' -d '{"email":"p01-required@test.turnflow.link"}'
```
        """,
        request=None,
        responses={
            200: OpenApiResponse(
                description="인증번호",
                examples=[
                    OpenApiExample("성공", value={"code": "123456", "phone": "010-****-5678"})
                ],
            ),
            400: OpenApiResponse(description="대기 중인 인증이 없거나 만료됨"),
            401: OpenApiResponse(description="해당 없음"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="운영 환경이거나 그 이메일의 계정이 없음"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
        parameters=[
            OpenApiParameter(
                "email", str, OpenApiParameter.QUERY, description="대상 계정 이메일(쿼리로도 가능)"
            )
        ],
    )
    def post(self, request):
        blocked = _dev_only()
        if blocked:
            return blocked

        from django.contrib.auth import get_user_model

        from .models import PhoneVerification
        from .phone import hash_code, mask_phone

        email = (request.data.get("email") or request.query_params.get("email") or "").strip()
        if not email:
            return _error("email 이 필요합니다.", "EMAIL_REQUIRED", status.HTTP_400_BAD_REQUEST)

        user = get_user_model().objects.filter(email__iexact=email).first()
        if user is None:
            return _error(
                f"'{email}' 계정을 찾을 수 없습니다.", "USER_NOT_FOUND", status.HTTP_404_NOT_FOUND
            )

        from django.utils import timezone

        row = (
            PhoneVerification.objects.filter(
                user=user,
                verified_at__isnull=True,
                invalidated_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created_at")
            .first()
        )
        if row is None:
            return _error(
                "대기 중인 인증이 없습니다. POST /api/v1/auth/me/phone/ 로 먼저 발송하세요.",
                "NO_PENDING_PHONE_VERIFICATION",
                status.HTTP_400_BAD_REQUEST,
            )

        # 평문을 저장하는 게 아니라 **해시를 알려진 값으로 바꾼다** — 저장 모델은 그대로.
        row.code_hash = hash_code(row.phone, DEV_FIXED_CODE)
        row.save(update_fields=["code_hash"])
        logger.info("dev phone code planted for %s", email)

        return Response(
            {
                "email": user.email,
                "code": DEV_FIXED_CODE,
                "phone": mask_phone(row.phone),
                "expires_at": row.expires_at.isoformat(),
            }
        )
