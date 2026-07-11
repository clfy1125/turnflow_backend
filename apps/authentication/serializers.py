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

    class Meta:
        model = User
        fields = ["email", "full_name", "password", "password_confirm", "attribution"]
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
        validated_data.pop("password_confirm")
        user = User.objects.create_user(
            email=validated_data["email"],
            full_name=validated_data.get("full_name", ""),
            password=validated_data["password"],
        )
        # 가입 유입 attribution 저장 — 절대 예외를 던지지 않아 가입을 막지 않는다.
        from apps.analytics.attribution import capture_signup_attribution

        capture_signup_attribution(user, attribution, signup_kind="email")
        return user


class UserSerializer(serializers.ModelSerializer):
    """Serializer for user profile"""

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "full_name",
            "is_email_verified",
            "email_verified_at",
            "date_joined",
            "last_login",
        ]
        read_only_fields = [
            "id",
            "is_email_verified",
            "email_verified_at",
            "date_joined",
            "last_login",
        ]


class UserUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating user profile"""

    class Meta:
        model = User
        fields = ["full_name"]


class TokenSerializer(serializers.Serializer):
    """Serializer for JWT tokens"""

    refresh = serializers.CharField()
    access = serializers.CharField()


class AuthResponseSerializer(serializers.Serializer):
    """Serializer for authentication response (login/register)"""

    user = UserSerializer()
    tokens = TokenSerializer()


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
