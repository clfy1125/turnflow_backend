"""apps/admin_api/auth/views_manage.py — 2단계 인증 등록·백업코드·기기 관리.

## 등록 경로가 둘인 이유

1. **최초 등록** — 아직 어드민 토큰이 없다(2단계를 통과해야 나오는데 등록이 안 돼 있다).
   1단계 로그인이 403 `mfa_setup_required` 와 함께 준 `setup_token` 으로 인증한다.
   (이메일 기기 승인이 켜져 있으면 여기에 메일 코드가 더 붙는다 — 지금은 꺼져 있어
   `setup_token` 하나로 등록한다. :func:`apps.admin_api.auth.devices.needs_email_verification`)
2. **재등록** — 폰을 바꾼 경우. 어드민 토큰 + 비밀번호 + **현재 인증앱 코드**를 요구한다.
   탈취된 access 토큰만으로 인증앱을 공격자 폰으로 옮길 수 있으면 2단계가 무의미해진다.

두 경로 모두 `setup/` 이 새 `setup_token` 을 돌려주고 `confirm/` 이 그것을 소비한다.
등록 중인 시드는 pending 자리에만 있어, 중간에 이탈해도 기존 인증앱이 계속 살아 있다.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.admin_api.audit import log_admin_action
from apps.admin_api.models import AdminActionLog, AdminDevice, AdminMFADevice

from . import challenge as challenge_store
from . import devices as device_store
from . import totp as totp_service
from .emails import consume_device_code
from .http import auth_error, client_ip
from .serializers import (
    AdminBackupCodeRegenerateRequestSerializer,
    AdminMfaConfirmRequestSerializer,
    AdminMfaSetupRequestSerializer,
    AdminMfaStatusSerializer,
    admin_payload,
)
from .tokens import issue_admin_tokens

logger = logging.getLogger(__name__)
User = get_user_model()


def _device_row(device: AdminDevice, current_device_id: str = "") -> dict:
    return {
        "id": device.pk,
        "device_id": device.device_id,
        "label": device.label,
        "is_trusted": device.is_trusted,
        "is_current": bool(current_device_id) and device.device_id == current_device_id,
        "created_at": device.created_at,
        "last_seen_at": device.last_seen_at,
        "last_seen_ip": device.last_seen_ip,
        # 신뢰에는 만료를 두지 않는다 — 실질 상한은 refresh 수명(7일)이고, 그 뒤에는
        # 비밀번호와 인증앱을 다시 통과해야 한다. 만료를 따로 두면 이메일 코드만 늘어난다.
        "expires_at": None,
    }


def _current_device_id(request) -> str:
    """요청 토큰에 박힌 기기 ID (``did`` 클레임). 세션 인증·비어드민 토큰이면 빈 문자열."""
    payload = getattr(getattr(request, "auth", None), "payload", None)
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("did", "") or "")


class AdminMfaSetupView(GenericAPIView):
    """인증앱 등록 시작 — 시드 발급 + QR."""

    permission_classes = [AllowAny]  # setup_token 경로가 있어 직접 판정한다
    serializer_class = AdminMfaSetupRequestSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin_mfa"

    @extend_schema(
        tags=["admin-auth"],
        summary="[관리자] 인증앱 등록 시작 (QR 발급)",
        description="""
## 개요
TOTP 시드를 발급하고 인증앱이 읽을 QR 을 돌려줍니다. **이 시점에는 아직 등록되지 않습니다** —
`POST /admin/auth/mfa/confirm/` 으로 코드를 확인해야 완료됩니다.

## 인증 (경로 2개)
- **최초 등록**: 1단계 로그인이 403 `mfa_setup_required` 와 함께 준 `setup_token` 을 body 에.
  Authorization 헤더 불필요.
- **재등록**(폰 교체 등): `Authorization: Bearer <어드민 토큰>` + `password` + 현재 `code`.
  → 탈취된 토큰만으로 인증앱을 옮기지 못하게 하는 장치입니다.

## 요청 필드
- `setup_token` (최초 등록 시 필수)
- `password` (재등록 시 필수): 비밀번호 재확인
- `code` (재등록 시 필수): **현재** 인증앱 6자리

## 응답 데이터
- `setup_token`: **새로 발급된 등록 티켓** — `confirm` 에 그대로 전달하세요 (TTL 300초)
- `otpauth_url`: 인증앱이 읽는 `otpauth://totp/...` URL
- `secret`: QR 을 못 읽을 때 손으로 입력할 base32 시드
- `qr_svg`: 그대로 렌더 가능한 인라인 SVG (프론트에 QR 라이브러리 불필요)
- `device_verification_required`: true 면 `confirm` 에 메일 코드(`email_code`)도 필요

## 주의사항
- 시드는 **확인 전까지 별도 자리(pending)** 에 보관됩니다. 중간에 창을 닫아도 기존 인증앱은
  그대로 동작합니다.
- 이 응답의 `secret` 은 비밀입니다 — 로깅·전송 로그에 남기지 마세요.
        """,
        request=AdminMfaSetupRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="시드 발급 성공",
                examples=[
                    OpenApiExample(
                        "성공",
                        value={
                            "setup_token": "eyJ...opaque",
                            "expires_in": 300,
                            "otpauth_url": "otpauth://totp/TurnFlow%20Admin:admin%40turnflow.ai.kr?secret=JBSWY3DPEHPK3PXP&issuer=TurnFlow%20Admin",
                            "secret": "JBSWY3DPEHPK3PXP",
                            "qr_svg": "<svg ...>",
                            "device_verification_required": False,
                        },
                        response_only=True,
                    )
                ],
            ),
            400: OpenApiResponse(description="`details.code = challenge_expired` / `invalid_code`"),
            401: OpenApiResponse(
                description="`details.code = invalid_credentials` (비밀번호 불일치)"
            ),
            403: OpenApiResponse(description="스태프가 아님"),
        },
    )
    def post(self, request):
        payload = self.get_serializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        user, device_id, needs_email, error = self._resolve(request, data)
        if error is not None:
            return error

        device = totp_service.start_enrollment(user)
        otpauth = totp_service.otpauth_url(user.email, device.pending_secret)
        token = challenge_store.create_challenge(
            user_id=user.pk, device_id=device_id, needs_email=needs_email
        )
        logger.info("[admin-mfa] 등록 시작 user=%s", user.email)
        return Response(
            {
                "setup_token": token,
                "expires_in": settings.ADMIN_MFA_CHALLENGE_TTL_SECONDS,
                "otpauth_url": otpauth,
                "secret": device.pending_secret,
                "qr_svg": totp_service.qr_svg(otpauth),
                "device_verification_required": needs_email,
            },
            status=status.HTTP_200_OK,
        )

    def _resolve(self, request, data):
        """(user, device_id, needs_email, error_response) — 두 인증 경로를 흡수한다."""
        setup_token = data.get("setup_token", "")
        if setup_token:
            ticket = challenge_store.load_challenge(setup_token)
            if ticket is None:
                return (
                    None,
                    "",
                    False,
                    auth_error(
                        status.HTTP_400_BAD_REQUEST,
                        "등록 시간이 만료되었습니다. 다시 로그인해 주세요.",
                        "challenge_expired",
                    ),
                )
            user = User.objects.filter(pk=ticket["user_id"], is_staff=True, is_active=True).first()
            if user is None:
                return (
                    None,
                    "",
                    False,
                    auth_error(
                        status.HTTP_400_BAD_REQUEST,
                        "등록 시간이 만료되었습니다. 다시 로그인해 주세요.",
                        "challenge_expired",
                    ),
                )
            # 부트스트랩 경로는 **미등록 계정 전용**이다. 이미 등록된 계정이 이 경로로 들어오면
            # 비밀번호만으로 인증앱을 갈아끼울 수 있게 된다.
            if totp_service.get_confirmed_device(user) is not None:
                return (
                    None,
                    "",
                    False,
                    auth_error(
                        status.HTTP_400_BAD_REQUEST,
                        "이미 등록된 계정입니다. 로그인 후 재등록해 주세요.",
                        "already_enrolled",
                    ),
                )
            challenge_store.consume_challenge(setup_token)
            return user, ticket["device_id"], bool(ticket.get("needs_email")), None

        # 재등록 경로
        user = request.user
        if not (user and user.is_authenticated and user.is_staff):
            return (
                None,
                "",
                False,
                auth_error(status.HTTP_403_FORBIDDEN, "관리자만 사용할 수 있습니다.", "not_staff"),
            )
        if not user.check_password(data.get("password", "")):
            return (
                None,
                "",
                False,
                auth_error(
                    status.HTTP_401_UNAUTHORIZED,
                    "비밀번호가 올바르지 않습니다.",
                    "invalid_credentials",
                ),
            )
        confirmed = totp_service.get_confirmed_device(user)
        if confirmed is not None and not totp_service.verify_totp(confirmed, data.get("code", "")):
            return (
                None,
                "",
                False,
                auth_error(
                    status.HTTP_400_BAD_REQUEST,
                    "현재 인증앱 코드가 올바르지 않습니다.",
                    "invalid_code",
                ),
            )
        return user, _current_device_id(request), False, None


class AdminMfaConfirmView(GenericAPIView):
    """등록 확인 — 새 시드로 만든 코드를 검증하고 백업코드·토큰을 발급."""

    permission_classes = [AllowAny]
    serializer_class = AdminMfaConfirmRequestSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin_mfa"

    @extend_schema(
        tags=["admin-auth"],
        summary="[관리자] 인증앱 등록 확인 (+ 백업코드 발급)",
        description="""
## 개요
`setup` 에서 받은 QR 을 인증앱에 등록한 뒤, 그 앱이 보여주는 6자리를 제출해 등록을 확정합니다.
성공하면 **백업코드 10개**와 **어드민 토큰**이 함께 발급되어 바로 콘솔로 진입할 수 있습니다.

## 인증
- 불필요 — `setup_token` 이 증거입니다.

## 요청 필드
- `setup_token` (필수): `setup` 응답값
- `code` (필수): 새로 등록한 인증앱의 6자리
- `email_code` (현재 미사용): `setup` 이 `device_verification_required: true` 를 준 경우에만
  필수. 이메일 기기 승인이 꺼져 있는 동안에는 항상 false 이므로 보내지 않아도 됩니다.

## 응답 데이터
- `backup_codes`: 10개. **이 응답에서만 1회 노출됩니다** — 서버는 해시만 보관하므로 다시 볼 수
  없습니다. 복사/다운로드 + "저장했습니다" 확인을 반드시 받으세요.
- `tokens`: 어드민 access/refresh
- `admin`: `GET /admin/me/` 와 동일 스키마

## 비즈니스 로직
- 등록을 마친 기기는 **자동으로 신뢰 등록**됩니다(refresh 7일).
- 재등록이면 **기존 백업코드는 전부 폐기**되고 새로 10개가 나갑니다.

## 주의사항
- 코드가 틀리면 등록은 완료되지 않고 기존 인증앱(있다면)이 그대로 유지됩니다.
        """,
        request=AdminMfaConfirmRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="등록 완료 — 백업코드 1회 노출 + 토큰 발급",
                examples=[
                    OpenApiExample(
                        "성공",
                        value={
                            "backup_codes": ["ABCD-EFGH-JKLM", "..."],
                            "tokens": {"access": "eyJ...", "refresh": "eyJ..."},
                            "admin": {"id": 2, "email": "admin@turnflow.ai.kr", "is_staff": True},
                            "device_id": "9c81-uuid",
                            "device_trusted": True,
                        },
                        response_only=True,
                    )
                ],
            ),
            400: OpenApiResponse(
                description="`details.code` = `invalid_code` / `invalid_email_code` / "
                "`challenge_expired` / `setup_not_started`"
            ),
        },
    )
    def post(self, request):
        payload = self.get_serializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        ip = client_ip(request)

        ticket = challenge_store.load_challenge(data.get("setup_token", ""))
        if ticket is None:
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "등록 시간이 만료되었습니다. 처음부터 다시 시도해 주세요.",
                "challenge_expired",
            )
        user = User.objects.filter(pk=ticket["user_id"], is_staff=True, is_active=True).first()
        if user is None:
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "등록 시간이 만료되었습니다. 처음부터 다시 시도해 주세요.",
                "challenge_expired",
            )

        device = AdminMFADevice.objects.filter(user=user).first()
        if device is None or not device.pending_secret:
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "등록이 시작되지 않았습니다. QR 발급부터 다시 시도해 주세요.",
                "setup_not_started",
            )

        if ticket.get("needs_email") and not consume_device_code(user, data.get("email_code", "")):
            alive = challenge_store.register_attempt(data["setup_token"])
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "메일로 받은 코드가 올바르지 않습니다.",
                "invalid_email_code" if alive else "challenge_expired",
            )

        if not totp_service.verify_pending_totp(device, data["code"]):
            alive = challenge_store.register_attempt(data["setup_token"])
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "코드가 올바르지 않습니다.",
                "invalid_code" if alive else "challenge_expired",
            )

        totp_service.complete_enrollment(device)
        backup_codes = totp_service.issue_backup_codes(user)
        challenge_store.consume_challenge(data["setup_token"])

        admin_device, _ = device_store.get_or_create_device(user, ticket["device_id"])
        # 등록을 마친 기기는 신뢰한다 — 방금 비밀번호·인증앱(·메일)을 모두 통과했다.
        device_store.trust_device(admin_device, ip)

        amr = ["pwd", "totp"] + (["email"] if ticket.get("needs_email") else [])
        tokens = issue_admin_tokens(user, device_id=admin_device.device_id, amr=amr, trusted=True)
        log_admin_action(
            request=request,
            actor=user,
            action=AdminActionLog.Action.ADMIN_MFA_ENROLLED,
            target_type="admin",
            target_id=user.pk,
            target_repr=user.email,
            changes={"device_id": admin_device.device_id},
        )
        logger.info("[admin-mfa] 등록 완료 user=%s", user.email)
        return Response(
            {
                "backup_codes": backup_codes,
                "tokens": tokens,
                "admin": admin_payload(user),
                "device_id": admin_device.device_id,
                "device_trusted": True,
            },
            status=status.HTTP_200_OK,
        )


class AdminMfaStatusView(APIView):
    """등록 상태 + 기기 목록 (보안 설정 화면)."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["admin-auth"],
        summary="[관리자] 2단계 인증 상태·기기 목록",
        description="""
## 개요
보안 설정 화면이 그리는 값 — 인증앱 등록 여부, 남은 백업코드, 로그인한 기기 목록입니다.

## 인증
- `Authorization: Bearer <어드민 토큰>` (is_staff=True). 본인 것만 조회됩니다.

## 응답 데이터
- `enrolled` / `confirmed_at`: 인증앱 등록 여부와 시각
- `backup_codes_remaining`: 남은 1회용 코드 수
- `backup_codes_low_threshold`: **서버가 정한 재발급 권고 기준**(기본 3). 프론트가 자체
  기준을 하드코딩하지 말고 이 값과 비교하세요 — 기준이 바뀌어도 배포가 필요 없습니다.
- `last_login_at`: 계정 단위 마지막 로그인 시각
- `trusted_devices[]`: 해제되지 않은 기기. `is_trusted=false` 는 신뢰 등록 없이 들어온
  임시 세션입니다. `expires_at` 은 **항상 null** — 신뢰는 해제할 때까지 유지됩니다.
  `is_current` 로 "지금 이 기기"를 표시하세요(자기 자신을 해제하면 로그아웃됩니다).

## 주의사항
- 마케팅 전용 계정은 2단계 대상이 아니므로 `enrolled=false` 로 조회됩니다.
        """,
        responses={
            200: OpenApiResponse(response=AdminMfaStatusSerializer, description="조회 성공"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="스태프 아님"),
        },
    )
    def get(self, request):
        user = request.user
        confirmed = totp_service.get_confirmed_device(user)
        current = _current_device_id(request)
        rows = [
            _device_row(d, current)
            for d in device_store.active_devices(user).order_by("-last_seen_at", "-created_at")
        ]
        return Response(
            {
                "enrolled": confirmed is not None,
                "confirmed_at": confirmed.confirmed_at if confirmed else None,
                "backup_codes_remaining": totp_service.backup_codes_remaining(user),
                "backup_codes_low_threshold": settings.ADMIN_BACKUP_CODE_LOW_THRESHOLD,
                "last_login_at": user.last_login,
                "trusted_devices": rows,
            },
            status=status.HTTP_200_OK,
        )


class AdminBackupCodeRegenerateView(GenericAPIView):
    """백업코드 재발급 — 기존 코드는 전부 폐기."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminBackupCodeRegenerateRequestSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "admin_mfa"

    @extend_schema(
        tags=["admin-auth"],
        summary="[관리자] 백업코드 재발급",
        description="""
## 개요
1회용 백업코드 10개를 새로 발급합니다. **기존 코드는 전부 폐기**됩니다.

## 인증
- `Authorization: Bearer <어드민 토큰>` + 비밀번호 + 현재 인증앱 코드.
  → 탈취된 토큰만으로 복구 수단을 새로 뽑아가지 못하게 하는 장치입니다.

## 요청 필드
- `password` (필수): 비밀번호 재확인
- `code` (필수): 현재 인증앱 6자리

## 응답 데이터
- `backup_codes`: 10개. **이 응답에서만 1회 노출됩니다.**

## 주의사항
- 남은 개수가 `backup_codes_low_threshold`(기본 3) 이하일 때 재발급을 권하세요.
- 재발급 즉시 옛 코드는 무효입니다 — 종이에 적어 둔 것이 있으면 버리도록 안내하세요.
        """,
        request=AdminBackupCodeRegenerateRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="재발급 완료 (1회 노출)",
                examples=[
                    OpenApiExample(
                        "성공",
                        value={"backup_codes": ["ABCD-EFGH-JKLM", "..."], "remaining": 10},
                        response_only=True,
                    )
                ],
            ),
            400: OpenApiResponse(description="`details.code` = `invalid_code` / `not_enrolled`"),
            401: OpenApiResponse(description="`details.code = invalid_credentials`"),
        },
    )
    def post(self, request):
        payload = self.get_serializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        user = request.user

        if not user.check_password(data["password"]):
            return auth_error(
                status.HTTP_401_UNAUTHORIZED,
                "비밀번호가 올바르지 않습니다.",
                "invalid_credentials",
            )
        confirmed = totp_service.get_confirmed_device(user)
        if confirmed is None:
            return auth_error(
                status.HTTP_400_BAD_REQUEST,
                "먼저 인증앱을 등록해 주세요.",
                "not_enrolled",
            )
        if not totp_service.verify_totp(confirmed, data["code"]):
            return auth_error(
                status.HTTP_400_BAD_REQUEST, "코드가 올바르지 않습니다.", "invalid_code"
            )

        codes = totp_service.issue_backup_codes(user)
        log_admin_action(
            request=request,
            action=AdminActionLog.Action.ADMIN_BACKUP_CODES_REGENERATED,
            target_type="admin",
            target_id=user.pk,
            target_repr=user.email,
        )
        return Response({"backup_codes": codes, "remaining": len(codes)}, status=status.HTTP_200_OK)


class AdminDeviceRevokeView(APIView):
    """기기 신뢰 해제 — 본인 기기만."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["admin-auth"],
        summary="[관리자] 기기 신뢰 해제",
        description="""
## 개요
보안 설정 화면의 기기 목록에서 특정 기기를 해제합니다.

## 인증
- `Authorization: Bearer <어드민 토큰>`. **본인 기기만** 해제할 수 있습니다(남의 기기 → 404).

## 비즈니스 로직
- 해제된 기기는 **다음 토큰 갱신부터 막힙니다**(`/admin/auth/refresh/` → 401 `device_revoked`).
- 이미 발급된 access 토큰은 만료(최대 2시간)까지 유효합니다. 즉시 끊어야 하는 상황이라면
  기기 문제가 아니라 계정 문제이므로 계정 비활성화를 쓰세요.
- 다음 로그인 때 그 기기는 신규 기기로 취급됩니다(비밀번호 + 인증앱을 다시 통과해야 합니다).

## 주의사항
- `is_current: true` 인 기기를 해제하면 **본인이 로그아웃**됩니다 — 확인 모달을 띄우세요.
        """,
        request=None,
        responses={
            204: OpenApiResponse(description="해제 완료"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="스태프 아님"),
            404: OpenApiResponse(description="본인 기기가 아니거나 이미 해제됨"),
        },
    )
    def delete(self, request, pk: int):
        device = AdminDevice.objects.filter(
            pk=pk, user=request.user, revoked_at__isnull=True
        ).first()
        if device is None:
            return auth_error(
                status.HTTP_404_NOT_FOUND, "기기를 찾을 수 없습니다.", "device_not_found"
            )
        device_store.revoke_device(device)
        log_admin_action(
            request=request,
            action=AdminActionLog.Action.ADMIN_DEVICE_REVOKED,
            target_type="admin_device",
            target_id=device.pk,
            target_repr=device.label or device.device_id,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
