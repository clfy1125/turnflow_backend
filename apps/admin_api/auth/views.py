"""apps/admin_api/auth/views.py — 어드민 2단계 로그인 (1단계 · 2단계 · 갱신).

계약: docs/frontend/ADMIN_AUTH_MFA_FRONTEND.md

## 응답 설계에서 지킨 두 가지

**계정 열거 방지.** 비밀번호 불일치·존재하지 않는 계정·스태프 아님을 **같은 401 같은 문구**로
답한다. 갈라놓으면 어드민 계정 목록을 밖에서 훑을 수 있다.

**마케팅 전용 계정은 이 관문을 그냥 통과시킨다.** 외주 계정에 인증앱 등록·기기 관리를
요구하는 건 현실적으로 관리가 안 된다(프론트 Q2 합의). 대신 그 계정은 애초에
마케팅 대시보드 외 모든 어드민 경로가 화이트리스트로 막혀 있다(apps/admin_api/roles.py).
프론트가 로그인 경로를 하나로 유지할 수 있도록 ``mfa_required: false`` + ``tokens`` 를
그대로 내려준다 — 4xx 로 돌려보내면 프론트가 재시도 분기를 갖게 된다.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.exceptions import TokenError

from apps.admin_api.audit import log_admin_action
from apps.admin_api.models import AdminActionLog
from apps.admin_api.roles import ROLE_FULL, admin_role
from apps.authentication.tokens import AppRefreshToken

from . import challenge as challenge_store
from . import devices as device_store
from . import totp as totp_service
from .emails import consume_device_code, send_device_code
from .http import auth_error, client_ip, mask_email
from .serializers import (
    AdminLoginRequestSerializer,
    AdminMfaVerifyRequestSerializer,
    AdminRefreshRequestSerializer,
    admin_payload,
)
from .tokens import is_admin_token, issue_admin_tokens, rotate_admin_refresh

logger = logging.getLogger(__name__)
User = get_user_model()

# 자격 실패는 원인을 구분하지 않는다 (계정 열거 방지).
_INVALID_CREDENTIALS = "이메일 또는 비밀번호가 올바르지 않습니다."


class AdminLoginView(GenericAPIView):
    """어드민 1단계 로그인 — 비밀번호 확인 후 2단계 티켓 발급."""

    permission_classes = [AllowAny]
    authentication_classes = []  # 로그인 자체는 기존 세션/토큰을 보지 않는다
    serializer_class = AdminLoginRequestSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin_login"

    @extend_schema(
        tags=["admin-auth"],
        summary="[관리자] 1단계 로그인 (비밀번호)",
        description="""
## 개요
관리자 콘솔 전용 로그인의 **1단계**입니다. 비밀번호를 확인하고 2단계(인증앱 코드)로 넘어갈
`challenge` 를 발급합니다. **이 단계에서는 토큰이 나오지 않습니다.**

일반 사용자 로그인(`POST /api/v1/auth/login/`)과 다른 엔드포인트입니다. 어드민 API 는 전
회원의 데이터를 워크스페이스 경계 없이 열기 때문에 관문을 분리했습니다.

## 사용 시나리오
- 관리자 콘솔 로그인 화면에서 이메일·비밀번호 제출 시.

## 인증
- **불필요** (공개 API). 기존 Authorization 헤더가 있어도 무시합니다.

## 요청 필드
- `email` (필수): 관리자 이메일
- `password` (필수): 계정 비밀번호
- `device_id` (선택): 클라이언트가 보관하는 기기 UUID. **없으면 서버가 발급해 응답에 담습니다**
  — 받은 값을 저장한 뒤 다음 로그인부터 그대로 보내세요(웹 localStorage / 앱 SecureStore).
- `device_label` (선택, 100자): 보안 화면 기기 목록에 표시할 이름

## 비즈니스 로직
- **신규 기기**(`device_verification_required: true`)에는 이메일로 6자리 승인 코드를 함께
  보냅니다. 2단계에서 인증앱 코드와 **둘 다** 필요합니다.
- **인증앱 미등록 계정**은 403 `mfa_setup_required` + `setup_token` 을 받습니다 →
  `POST /admin/auth/mfa/setup/` 으로 등록 화면을 띄우세요.
- **마케팅 전용 계정**(`admin_role="marketing_viewer"`)은 2단계가 없습니다 —
  `mfa_required: false` + `tokens` 가 바로 내려옵니다. 프론트는 응답에 `tokens` 가 있으면
  즉시 로그인 처리하면 됩니다(경로 분기 불필요).
- `challenge` 유효시간 300초, 코드 시도 5회 초과 시 티켓이 파기되어 처음부터 다시 해야 합니다.

## 주의사항
- 비밀번호 불일치 · 없는 계정 · 스태프 아님은 **모두 같은 401** 입니다(계정 열거 방지).
- 스로틀 5회/분(IP 기준) — 초과 시 429 `RATE_LIMITED`.

```bash
curl -X POST https://api.example.com/api/v1/admin/auth/login/ \\
  -H "Content-Type: application/json" \\
  -d '{"email":"admin@turnflow.ai.kr","password":"...","device_id":"3f2b-uuid"}'
```
        """,
        request=AdminLoginRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="1단계 통과 — 2단계로 진행하거나(mfa_required) 즉시 로그인(tokens)",
                examples=[
                    OpenApiExample(
                        "등록된 기기",
                        value={
                            "mfa_required": True,
                            "challenge": "eyJ...opaque",
                            "expires_in": 300,
                            "methods": ["totp", "backup_code"],
                            "device_id": "3f2b-uuid",
                            "device_trusted": True,
                            "device_verification_required": False,
                        },
                        response_only=True,
                    ),
                    OpenApiExample(
                        "신규 기기 (이메일 코드 발송됨)",
                        value={
                            "mfa_required": True,
                            "challenge": "eyJ...opaque",
                            "expires_in": 300,
                            "methods": ["totp", "email", "backup_code"],
                            "device_id": "9c81-uuid",
                            "device_trusted": False,
                            "device_verification_required": True,
                            "email_masked": "ad***in@turnflow.ai.kr",
                        },
                        response_only=True,
                    ),
                    OpenApiExample(
                        "마케팅 전용 계정 (2단계 없음)",
                        value={
                            "mfa_required": False,
                            "tokens": {"access": "eyJ...", "refresh": "eyJ..."},
                            "admin": {"admin_role": "marketing_viewer"},
                        },
                        response_only=True,
                    ),
                ],
            ),
            400: OpenApiResponse(description="필수 필드 누락 / 형식 오류"),
            401: OpenApiResponse(
                description="자격 실패 — `details.code = invalid_credentials` "
                "(비밀번호 불일치·없는 계정·비스태프 공통)"
            ),
            403: OpenApiResponse(
                description="인증앱 미등록 — `details.code = mfa_setup_required`, `setup_token` 동봉"
            ),
            429: OpenApiResponse(description="스로틀 5회/분 초과 — `code = RATE_LIMITED`"),
        },
    )
    def post(self, request):
        payload = self.get_serializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        ip = client_ip(request)

        user = authenticate(
            request, username=data["email"].strip().lower(), password=data["password"]
        )
        # is_staff 까지 여기서 거른다 — 비스태프에게 "비밀번호는 맞다"를 알려줄 이유가 없다.
        if user is None or not user.is_staff or not user.is_active:
            logger.warning("[admin-auth] 1단계 실패 email=%s ip=%s", data["email"][:64], ip)
            return auth_error(
                status.HTTP_401_UNAUTHORIZED, _INVALID_CREDENTIALS, "invalid_credentials"
            )

        # ── 마케팅 전용 계정: 2단계 없이 통과 (D-4) ──
        if admin_role(user) != ROLE_FULL:
            refresh = AppRefreshToken.for_user(user)
            logger.info("[admin-auth] 제한 역할 로그인 user=%s ip=%s", user.email, ip)
            return Response(
                {
                    "mfa_required": False,
                    "tokens": {"access": str(refresh.access_token), "refresh": str(refresh)},
                    "admin": admin_payload(user),
                },
                status=status.HTTP_200_OK,
            )

        device_id = device_store.normalize_device_id(data.get("device_id"))
        device, _ = device_store.get_or_create_device(user, device_id, data.get("device_label", ""))
        needs_email = device_store.needs_email_verification(device)

        # ── 인증앱 미등록: 등록 화면으로 유도 ──
        if totp_service.get_confirmed_device(user) is None:
            email_token_id = (
                send_device_code(user, device_label=device.label, request_ip=ip)
                if needs_email
                else None
            )
            setup_token = challenge_store.create_challenge(
                user_id=user.pk,
                device_id=device_id,
                needs_email=needs_email,
                email_token_id=email_token_id,
            )
            return auth_error(
                status.HTTP_403_FORBIDDEN,
                "2단계 인증(인증앱)을 먼저 등록해 주세요.",
                "mfa_setup_required",
                setup_token=setup_token,
                expires_in=settings.ADMIN_MFA_CHALLENGE_TTL_SECONDS,
                device_id=device_id,
                device_verification_required=needs_email,
                email_masked=mask_email(user.email) if needs_email else "",
            )

        email_token_id = (
            send_device_code(user, device_label=device.label, request_ip=ip)
            if needs_email
            else None
        )
        token = challenge_store.create_challenge(
            user_id=user.pk,
            device_id=device_id,
            needs_email=needs_email,
            email_token_id=email_token_id,
        )
        body = {
            "mfa_required": True,
            "challenge": token,
            "expires_in": settings.ADMIN_MFA_CHALLENGE_TTL_SECONDS,
            "methods": (
                ["totp", "email", "backup_code"] if needs_email else ["totp", "backup_code"]
            ),
            "device_id": device_id,
            "device_trusted": device.is_trusted,
            "device_verification_required": needs_email,
        }
        if needs_email:
            body["email_masked"] = mask_email(user.email)
        return Response(body, status=status.HTTP_200_OK)


class AdminMfaVerifyView(GenericAPIView):
    """어드민 2단계 — 코드 검증 후 어드민 토큰 발급."""

    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = AdminMfaVerifyRequestSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin_mfa"

    @extend_schema(
        tags=["admin-auth"],
        summary="[관리자] 2단계 인증 → 토큰 발급",
        description="""
## 개요
1단계에서 받은 `challenge` 와 인증앱 코드를 제출해 **어드민 전용 토큰**을 받습니다.

## 인증
- **불필요** — `challenge` 자체가 1단계 통과 증거입니다.

## 요청 필드
- `challenge` (필수): 1단계 응답값 (TTL 300초)
- `code` (택1): 인증앱 6자리
- `backup_code` (택1): 백업코드 `ABCD-EFGH-JKLM` (하이픈·대소문자 무관). `code` 대신 사용
- `email_code` (조건부 필수): 1단계가 `device_verification_required: true` 였다면 필수
- `remember_device` (선택, 기본 false): 이 기기를 신뢰 등록

## 비즈니스 로직
- **어드민 토큰의 수명**: access 2시간 / refresh 12시간. `remember_device: true` 로 신뢰
  등록한 기기는 **refresh 7일**입니다.
- 신뢰 등록된 기기는 다음 로그인부터 이메일 코드를 건너뜁니다.
- 인증앱 코드는 **1회용**입니다 — 같은 30초 창의 코드를 다시 넣으면 실패합니다(재사용 방지).
- 백업코드는 소모됩니다. 남은 개수는 `GET /admin/auth/mfa/status/` 에서 확인하세요.
- 성공 시 `admin` 필드로 `GET /admin/me/` 와 동일한 신원을 함께 내려줍니다.

## 주의사항
- 코드 5회 실패 시 `challenge` 가 파기됩니다 → `challenge_expired` 를 받으면 **로그인 화면으로
  되돌리세요** (같은 challenge 로 재시도 불가).
- 스로틀 10회/분(IP 기준).
        """,
        request=AdminMfaVerifyRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="인증 성공 — 어드민 토큰 발급",
                examples=[
                    OpenApiExample(
                        "성공",
                        value={
                            "tokens": {"access": "eyJ...", "refresh": "eyJ..."},
                            "admin": {
                                "id": 2,
                                "email": "admin@turnflow.ai.kr",
                                "is_staff": True,
                                "admin_role": "full",
                            },
                            "device_id": "9c81-uuid",
                            "device_trusted": True,
                        },
                        response_only=True,
                    )
                ],
            ),
            400: OpenApiResponse(
                description="`details.code` = `invalid_code` / `invalid_email_code` / "
                "`challenge_expired` / `backup_code_used`"
            ),
            429: OpenApiResponse(description="스로틀 10회/분 초과"),
        },
    )
    def post(self, request):
        payload = self.get_serializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        ip = client_ip(request)

        ticket = challenge_store.load_challenge(data["challenge"])
        if ticket is None:
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "인증 시간이 만료되었습니다. 다시 로그인해 주세요.",
                "challenge_expired",
            )

        user = User.objects.filter(pk=ticket["user_id"], is_staff=True, is_active=True).first()
        if user is None:
            challenge_store.consume_challenge(data["challenge"])
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "인증 시간이 만료되었습니다. 다시 로그인해 주세요.",
                "challenge_expired",
            )

        # 1) 이메일 기기 승인 코드 (신규 기기일 때만)
        if ticket.get("needs_email"):
            if not consume_device_code(user, data.get("email_code", "")):
                alive = challenge_store.register_attempt(data["challenge"])
                return auth_error(
                    status.HTTP_400_BAD_REQUEST,
                    "메일로 받은 코드가 올바르지 않습니다.",
                    "invalid_email_code" if alive else "challenge_expired",
                )

        # 2) 인증앱 또는 백업코드
        method = totp_service.verify_second_factor(
            user, code=data.get("code", ""), backup_code=data.get("backup_code", "")
        )
        if method is None:
            alive = challenge_store.register_attempt(data["challenge"])
            logger.warning("[admin-auth] 2단계 실패 user=%s ip=%s", user.email, ip)
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "코드가 올바르지 않습니다.",
                "invalid_code" if alive else "challenge_expired",
            )

        challenge_store.consume_challenge(data["challenge"])

        device, _ = device_store.get_or_create_device(user, ticket["device_id"])
        amr = ["pwd", method]
        if ticket.get("needs_email"):
            amr.append("email")
        # 이메일 승인을 통과했고 사용자가 원하면 신뢰 등록. 이메일 승인 없이(이미 신뢰 기기)
        # 들어온 경우에도 remember_device 는 무해하다 — 이미 신뢰 상태다.
        if data.get("remember_device") or device.is_trusted:
            device_store.trust_device(device, ip)
        else:
            device.touch(ip)

        tokens = issue_admin_tokens(
            user, device_id=device.device_id, amr=amr, trusted=device.is_trusted
        )
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        log_admin_action(
            request=request,
            actor=user,
            action=AdminActionLog.Action.ADMIN_LOGIN,
            target_type="admin",
            target_id=user.pk,
            target_repr=user.email,
            changes={"amr": amr, "device_id": device.device_id, "trusted": device.is_trusted},
        )
        logger.info("[admin-auth] 로그인 성공 user=%s amr=%s ip=%s", user.email, amr, ip)
        return Response(
            {
                "tokens": tokens,
                "admin": admin_payload(user),
                "device_id": device.device_id,
                "device_trusted": device.is_trusted,
            },
            status=status.HTTP_200_OK,
        )


class AdminTokenRefreshView(GenericAPIView):
    """어드민 refresh 회전 — 일반 refresh 로는 어드민 토큰을 만들 수 없다."""

    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = AdminRefreshRequestSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin_mfa"

    @extend_schema(
        tags=["admin-auth"],
        summary="[관리자] 어드민 토큰 갱신",
        description="""
## 개요
어드민 access 토큰(2시간)이 만료되면 이 엔드포인트로 갱신합니다.

## 인증
- 불필요 — body 의 `refresh` 자체가 증거입니다.

## 요청 필드
- `refresh` (필수): 어드민 refresh 토큰

## 비즈니스 로직
- **회전**됩니다(`ROTATE_REFRESH_TOKENS`). 응답의 새 `refresh` 를 반드시 저장하세요 —
  방금 쓴 refresh 는 즉시 블랙리스트에 올라 다음 갱신이 401 이 됩니다.
- 토큰에 박힌 기기가 **보안 화면에서 해제**됐으면 갱신이 막힙니다(`device_revoked`).
- 일반 사용자 토큰(`POST /api/v1/auth/login/` 발급분)을 넣으면 400 `not_admin_token` 입니다.

## 주의사항
- ⚠️ 기존 `POST /api/v1/auth/token/refresh/` 로는 어드민 토큰을 갱신할 수 없습니다.
        """,
        request=AdminRefreshRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="갱신 성공 (refresh 회전됨)",
                examples=[
                    OpenApiExample(
                        "성공", value={"access": "eyJ...", "refresh": "eyJ..."}, response_only=True
                    )
                ],
            ),
            400: OpenApiResponse(description="`details.code = not_admin_token`"),
            401: OpenApiResponse(
                description="`details.code` = `token_expired` / `device_revoked` / `user_inactive`"
            ),
        },
    )
    def post(self, request):
        payload = self.get_serializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            token, user_id, device_id = rotate_admin_refresh(payload.validated_data["refresh"])
        except TokenError:
            return auth_error(
                status.HTTP_401_UNAUTHORIZED,
                "로그인이 만료되었습니다. 다시 로그인해 주세요.",
                "token_expired",
            )

        if not is_admin_token(token.payload):
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "관리자 토큰이 아닙니다. 다시 로그인해 주세요.",
                "not_admin_token",
            )

        user = User.objects.filter(pk=user_id, is_staff=True, is_active=True).first()
        if user is None:
            return auth_error(
                status.HTTP_401_UNAUTHORIZED,
                "사용할 수 없는 계정입니다. 다시 로그인해 주세요.",
                "user_inactive",
            )

        device = device_store.find_live_device(user_id, device_id)
        if device_id and device is None:
            logger.warning(
                "[admin-auth] 해제된 기기의 갱신 차단 user=%s device=%s", user.email, device_id[:8]
            )
            return auth_error(
                status.HTTP_401_UNAUTHORIZED,
                "이 기기의 로그인이 해제되었습니다. 다시 로그인해 주세요.",
                "device_revoked",
            )

        if device:
            device.touch(client_ip(request))
        tokens = issue_admin_tokens(
            user,
            device_id=device_id,
            amr=list(token.payload.get("amr", ["pwd"])),
            trusted=bool(device and device.is_trusted),
        )
        return Response(tokens, status=status.HTTP_200_OK)
