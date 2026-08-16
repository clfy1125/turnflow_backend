"""apps/admin_api/auth/serializers.py — 어드민 2단계 로그인 요청/응답 스키마.

응답 시리얼라이저는 **문서(drf-spectacular)용**이다. 실제 응답은 뷰가 dict 로 만든다 —
로그인 응답은 분기(mfa_required / tokens)가 많아 시리얼라이저로 감싸면 오히려 읽기 어렵다.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.admin_api.serializers.identity import AdminMeSerializer


def admin_payload(user) -> dict:
    """로그인·등록 응답에 동봉하는 신원 — ``GET /admin/me/`` 와 **동일 스키마**.

    프론트가 로그인 직후 같은 정보를 다시 조회하며 로딩을 띄우지 않도록 함께 내려준다.
    두 곳이 같은 시리얼라이저를 쓰므로 필드가 갈라질 수 없다.
    """
    return AdminMeSerializer(user).data


class AdminLoginRequestSerializer(serializers.Serializer):
    """1단계 — 비밀번호."""

    email = serializers.EmailField(help_text="관리자 이메일")
    password = serializers.CharField(
        style={"input_type": "password"}, trim_whitespace=False, help_text="계정 비밀번호"
    )
    device_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,
        help_text="클라이언트가 보관하는 기기 UUID. 없으면 서버가 발급해 응답에 담는다.",
    )
    device_label = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=100,
        help_text="기기 표시명 (보안 화면 목록용). 예: '이재원 MacBook'",
    )


class AdminMfaVerifyRequestSerializer(serializers.Serializer):
    """2단계 — 인증앱 코드(또는 백업코드) + 신규 기기면 이메일 코드."""

    challenge = serializers.CharField(help_text="1단계 응답의 challenge (TTL 5분)")
    code = serializers.CharField(
        required=False, allow_blank=True, max_length=8, help_text="인증앱 6자리"
    )
    backup_code = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=20,
        help_text="백업코드 (ABCD-EFGH-JKLM). code 대신 사용 — 하이픈·대소문자 무관",
    )
    email_code = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=6,
        help_text="신규 기기(device_verification_required=true)일 때만 필수 — 메일로 받은 6자리",
    )
    remember_device = serializers.BooleanField(
        required=False,
        default=False,
        help_text="이 기기를 신뢰 등록. 다음부터 이메일 코드를 건너뛰고 refresh 수명이 길어진다.",
    )

    def validate(self, attrs):
        if not attrs.get("code") and not attrs.get("backup_code"):
            raise serializers.ValidationError(
                {"code": "인증앱 코드 또는 백업코드 중 하나는 필요합니다."}
            )
        return attrs


class AdminRefreshRequestSerializer(serializers.Serializer):
    refresh = serializers.CharField(help_text="어드민 refresh 토큰")


class AdminMfaSetupRequestSerializer(serializers.Serializer):
    """등록 시작 — 인증 경로가 둘이다.

    - 최초 등록(아직 어드민 토큰이 없음): 1단계 응답의 ``setup_token``
    - 재등록(이미 등록된 계정): 어드민 토큰 + ``password`` + 현재 ``code``
      → 탈취된 access 토큰만으로 인증앱을 공격자 폰으로 옮기지 못하게 한다.
    """

    setup_token = serializers.CharField(
        required=False, allow_blank=True, help_text="최초 등록 경로 (login 403 응답에 동봉)"
    )
    password = serializers.CharField(
        required=False,
        allow_blank=True,
        style={"input_type": "password"},
        trim_whitespace=False,
        help_text="재등록 경로 — 비밀번호 재확인",
    )
    code = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=8,
        help_text="재등록 경로 — 현재 인증앱 코드 (이미 등록된 계정만)",
    )


class AdminMfaConfirmRequestSerializer(serializers.Serializer):
    """등록 확인 — 새 시드로 만든 코드를 넣어 실제로 스캔됐는지 증명."""

    code = serializers.CharField(max_length=8, help_text="새로 등록한 인증앱의 6자리")
    setup_token = serializers.CharField(
        required=False, allow_blank=True, help_text="최초 등록 경로일 때 동봉"
    )
    email_code = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=6,
        help_text="신규 기기 최초 등록이면 필수 — 메일로 받은 6자리",
    )


class AdminBackupCodeRegenerateRequestSerializer(serializers.Serializer):
    """재발급 — 비밀번호 + 현재 코드. 기존 코드는 전부 폐기된다."""

    password = serializers.CharField(
        style={"input_type": "password"}, trim_whitespace=False, help_text="비밀번호 재확인"
    )
    code = serializers.CharField(max_length=8, help_text="현재 인증앱 6자리")


# ── 응답 (문서용) ─────────────────────────────────────────────────────────


class AdminTokensSerializer(serializers.Serializer):
    access = serializers.CharField(help_text="어드민 access 토큰 (2시간)")
    refresh = serializers.CharField(help_text="어드민 refresh 토큰 (신뢰 기기 7일 / 비신뢰 12시간)")


class AdminDeviceSerializer(serializers.Serializer):
    id = serializers.IntegerField(help_text="기기 행 PK (해제 API 의 경로 인자)")
    device_id = serializers.CharField(help_text="클라이언트 기기 UUID")
    label = serializers.CharField(allow_blank=True, help_text="기기 표시명")
    is_trusted = serializers.BooleanField(help_text="신뢰 등록 여부 (false=임시 세션)")
    is_current = serializers.BooleanField(help_text="지금 이 요청을 보낸 기기인지")
    created_at = serializers.DateTimeField(help_text="최초 로그인 시각")
    last_seen_at = serializers.DateTimeField(allow_null=True, help_text="마지막 접속 시각")
    last_seen_ip = serializers.CharField(allow_null=True, help_text="마지막 접속 IP")
    expires_at = serializers.DateTimeField(
        allow_null=True,
        help_text="신뢰 만료 시각. **항상 null** — 신뢰는 해제할 때까지 유지된다(설계).",
    )


class AdminMfaStatusSerializer(serializers.Serializer):
    enrolled = serializers.BooleanField(help_text="인증앱 등록 완료 여부")
    confirmed_at = serializers.DateTimeField(allow_null=True, help_text="등록 확인 시각")
    backup_codes_remaining = serializers.IntegerField(help_text="남은 백업코드 개수")
    backup_codes_low_threshold = serializers.IntegerField(
        help_text="이 개수 이하면 재발급을 권할 것 (서버가 정한 기준)"
    )
    last_login_at = serializers.DateTimeField(
        allow_null=True, help_text="계정 단위 마지막 로그인 시각"
    )
    trusted_devices = AdminDeviceSerializer(many=True, help_text="해제되지 않은 기기 목록")
