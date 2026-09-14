"""
Serializers for authentication
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

User = get_user_model()


class UserRegistrationSerializer(serializers.ModelSerializer):
    """Serializer for user registration"""

    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
        style={"input_type": "password"},
    )
    password_confirm = serializers.CharField(
        write_only=True, required=True, style={"input_type": "password"}
    )
    # 가입 유입 attribution — 의도적으로 느슨한 JSONField (nested serializer 아님):
    # 어떤 JSON 형태든 통과시키고 sanitize 는 capture_signup_attribution 이 담당한다.
    # → 잘못된 attribution 객체가 가입을 400 으로 깨뜨리는 일이 구조적으로 불가능.
    attribution = serializers.JSONField(
        required=False,
        write_only=True,
        help_text=(
            "선택 — 가입 유입 attribution 객체 {visitor_id, utm_source, utm_medium, "
            "utm_campaign, utm_content, referrer, landing_path}"
        ),
    )
    # 마케팅(광고성) 수신 동의 — 정보통신망법상 별도 동의. 미체크(False) 기본.
    # True 로 가입하면 동의 시각(marketing_opt_in_at)까지 함께 기록한다.
    marketing_opt_in = serializers.BooleanField(
        required=False,
        default=False,
        help_text="선택 — 마케팅(광고성) 정보 수신 동의 여부. 기본 False.",
    )

    class Meta:
        model = User
        fields = [
            "email",
            "full_name",
            "password",
            "password_confirm",
            "attribution",
            "marketing_opt_in",
        ]
        extra_kwargs = {
            "full_name": {"required": False},
        }

    def validate(self, attrs):
        """Validate password confirmation"""
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password": "Password fields didn't match."})
        return attrs

    def create(self, validated_data):
        """Create user with encrypted password"""
        attribution = validated_data.pop("attribution", None)
        marketing_opt_in = validated_data.pop("marketing_opt_in", False)
        validated_data.pop("password_confirm")
        # 동의한 경우에만 동의 시각을 기록해 단일 INSERT 로 생성한다.
        extra_fields = {}
        if marketing_opt_in:
            from django.utils import timezone

            extra_fields["marketing_opt_in"] = True
            extra_fields["marketing_opt_in_at"] = timezone.now()
        user = User.objects.create_user(
            email=validated_data["email"],
            full_name=validated_data.get("full_name", ""),
            password=validated_data["password"],
            **extra_fields,
        )
        # 가입 유입 attribution 저장 — 절대 예외를 던지지 않아 가입을 막지 않는다.
        from apps.analytics.attribution import capture_signup_attribution

        capture_signup_attribution(user, attribution, signup_kind="email")

        # Meta 전환 API — attribution **저장 뒤에** 불러야 fbc/fbp 를 함께 실어 보낸다.
        # 순서를 바꾸면 매칭 파라미터가 빈 채로 나간다(전송은 되지만 매칭률이 떨어진다).
        from apps.analytics.conversions import track_signup

        track_signup(user, request=self.context.get("request"))
        return user


class UserSerializer(serializers.ModelSerializer):
    """Serializer for user profile"""

    email_is_placeholder = serializers.BooleanField(
        read_only=True,
        help_text=(
            "이메일이 **우리가 지어낸 자리표시**인가. Instagram Business Login 은 이메일을 "
            "주지 않아 IG 가입자에게는 `ig_<id>@ig.invalid` 를 발급한다. true 면 이 사용자에게 "
            "메일을 보낼 수 없으므로 프론트는 '알림 받을 이메일 등록' 을 유도할 것."
        ),
    )

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "email_is_placeholder",
            "pending_email",
            "full_name",
            "is_email_verified",
            "email_verified_at",
            "date_joined",
            "last_login",
            "marketing_opt_in",
            "marketing_opt_in_at",
        ]
        read_only_fields = [
            "id",
            "email_is_placeholder",
            "pending_email",
            "is_email_verified",
            "email_verified_at",
            "date_joined",
            "last_login",
            # 동의 자체(marketing_opt_in)는 이 출력용 시리얼라이저로 수정하지 않는다
            # (수정은 UserUpdateSerializer). 동의 시각은 서버가 파생하므로 항상 read-only.
            "marketing_opt_in",
            "marketing_opt_in_at",
        ]


class UserUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating user profile"""

    class Meta:
        model = User
        fields = ["full_name", "marketing_opt_in"]

    def update(self, instance, validated_data):
        """full_name/marketing_opt_in 갱신.

        marketing_opt_in 이 바뀌면 동의 시각(marketing_opt_in_at)을 서버가 파생한다:
        - False→True: 지금 시각을 동의 시각으로 기록
        - True→False: 동의 철회 → 동의 시각 제거
        (수신거부 링크·설정 토글이 모두 이 한 필드로 수렴한다.)
        """
        from django.utils import timezone

        if "marketing_opt_in" in validated_data:
            new_val = validated_data["marketing_opt_in"]
            if new_val and not instance.marketing_opt_in:
                instance.marketing_opt_in_at = timezone.now()
            elif not new_val:
                instance.marketing_opt_in_at = None
        return super().update(instance, validated_data)


class TokenSerializer(serializers.Serializer):
    """Serializer for JWT tokens"""

    refresh = serializers.CharField()
    access = serializers.CharField()


class AuthResponseSerializer(serializers.Serializer):
    """Serializer for authentication response (login/register)"""

    user = UserSerializer()
    tokens = TokenSerializer()


class GoogleAuthResponseSerializer(AuthResponseSerializer):
    """Google 로그인 응답 — 가입/로그인이 **같은 엔드포인트**라 구분 플래그가 붙는다.

    ``is_new_user`` 를 login/register 의 공용 응답(AuthResponseSerializer)에 넣지 않는
    이유: 그 두 엔드포인트는 이 필드를 반환하지 않으므로 스키마에 넣으면 문서가 거짓이
    된다 (register 는 항상 신규, login 은 항상 기존이라 애초에 필요도 없다).
    """

    is_new_user = serializers.BooleanField(
        help_text=(
            "이번 요청으로 계정이 새로 생성되었는가. "
            "Meta 픽셀 CompleteRegistration 등 **가입 전환 이벤트는 반드시 이 값으로 분기**할 것 "
            "— date_joined 로 추정하면 누락/중복 발사된다."
        )
    )


class AccountDeleteSerializer(serializers.Serializer):
    """회원 탈퇴 요청 시리얼라이저. 비밀번호 확인 필수."""

    password = serializers.CharField(
        required=True,
        write_only=True,
        style={"input_type": "password"},
        help_text="본인 확인을 위한 현재 비밀번호",
    )

    def validate_password(self, value):
        user = self.context.get("user")
        if user and not user.check_password(value):
            raise serializers.ValidationError("비밀번호가 올바르지 않습니다.")
        return value


class GoogleLoginSerializer(serializers.Serializer):
    """Google OAuth 로그인 요청 시리얼라이저."""

    token = serializers.CharField(
        required=True,
        help_text="프론트엔드에서 Google 로그인 후 받은 ID Token",
    )
    attribution = serializers.JSONField(
        required=False,
        help_text="선택 — 가입 유입 attribution 객체 (신규 가입 시에만 저장)",
    )
    marketing_opt_in = serializers.BooleanField(
        required=False,
        default=False,
        help_text="선택 — 마케팅 수신 동의 (신규 가입 시에만 반영). 기본 False.",
    )


class KakaoLoginSerializer(serializers.Serializer):
    """카카오 로그인 요청.

    두 경로 중 **하나만** 채워 보낸다:
      · 웹 표준 경로 — ``code`` + ``redirect_uri`` (인가 코드를 서버가 교환)
      · 네이티브 경로 — ``access_token`` (카카오 SDK 가 이미 토큰을 들고 있는 경우)

    ``code`` 를 권장한다. 액세스 토큰이 브라우저를 거치지 않고, 클라이언트 시크릿이
    서버에만 남기 때문이다.
    """

    code = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="카카오 인가 코드. authorize 리다이렉트 쿼리스트링의 `code` 값.",
    )
    redirect_uri = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "authorize 요청에 사용한 redirect_uri 와 **완전히 동일한 값**. "
            "code 를 보낼 때 필수 — 한 글자라도 다르면 카카오가 교환을 거절한다."
        ),
    )
    access_token = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="카카오 액세스 토큰(네이티브 SDK 경로). code 대신 보낸다.",
    )
    attribution = serializers.JSONField(
        required=False,
        help_text="선택 — 가입 유입 attribution 객체 (신규 가입 시에만 저장)",
    )
    marketing_opt_in = serializers.BooleanField(
        required=False,
        default=False,
        help_text="선택 — 마케팅 수신 동의 (신규 가입 시에만 반영). 기본 False.",
    )

    def validate(self, attrs):
        code = (attrs.get("code") or "").strip()
        access_token = (attrs.get("access_token") or "").strip()

        if not code and not access_token:
            raise serializers.ValidationError(
                {"code": "code 또는 access_token 중 하나는 반드시 필요합니다."}
            )
        # 둘 다 오면 code 를 쓴다. 조용히 고르지 않고 거절하는 이유: 프론트가 두 경로를
        # 섞어 보내고 있다는 뜻이라, 어느 쪽이 실제로 검증됐는지 아무도 모르게 된다.
        if code and access_token:
            raise serializers.ValidationError(
                {"code": "code 와 access_token 은 동시에 보낼 수 없습니다."}
            )
        if code and not (attrs.get("redirect_uri") or "").strip():
            raise serializers.ValidationError(
                {"redirect_uri": "code 를 보낼 때는 redirect_uri 가 필수입니다."}
            )

        attrs["code"] = code
        attrs["access_token"] = access_token
        return attrs


class KakaoAuthResponseSerializer(AuthResponseSerializer):
    """카카오 로그인 응답 — 구글과 동일하게 가입/로그인이 같은 엔드포인트라 구분 플래그가 붙는다."""

    is_new_user = serializers.BooleanField(
        help_text=(
            "이번 요청으로 계정이 새로 생성되었는가. "
            "Meta 픽셀 CompleteRegistration 등 **가입 전환 이벤트는 반드시 이 값으로 분기**할 것."
        )
    )


class InstagramLoginSerializer(serializers.Serializer):
    """인스타그램 로그인/가입 요청 (인가 코드 교환).

    ⚠️ ``redirect_uri`` 를 **받지 않는다**. ``GET /auth/instagram/start/`` 가 검증해
    저장해 둔 값을 쓴다 — 클라이언트가 다시 보낸 값을 쓰면 authorize 때와 한 글자만
    달라도 Meta 가 교환을 거절하는데, 그 실패는 원인을 찾기가 매우 어렵다.
    (카카오는 프론트가 authorize 를 직접 만들어서 받을 수밖에 없지만, 인스타는 authorize
     URL 도 우리가 만들어 주므로 서버가 양쪽 값을 모두 소유할 수 있다.)
    """

    code = serializers.CharField(
        help_text="인스타그램 인가 코드. 콜백 쿼리스트링의 `code` 값 (1회용)."
    )
    state = serializers.CharField(
        help_text="`GET /auth/instagram/start/` 에서 받은 state. **1회용·10분**."
    )
    attribution = serializers.JSONField(
        required=False,
        help_text="선택 — 가입 유입 attribution 객체 (신규 가입 시에만 저장)",
    )
    marketing_opt_in = serializers.BooleanField(
        required=False,
        default=False,
        help_text="선택 — 마케팅 수신 동의 (신규 가입 시에만 반영). 기본 False.",
    )

    def validate(self, attrs):
        attrs["code"] = (attrs.get("code") or "").strip()
        attrs["state"] = (attrs.get("state") or "").strip()
        if not attrs["code"]:
            raise serializers.ValidationError({"code": "code 는 필수입니다."})
        if not attrs["state"]:
            raise serializers.ValidationError({"state": "state 는 필수입니다."})
        return attrs
