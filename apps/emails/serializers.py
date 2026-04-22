"""
Serializers for admin CRUD + user-facing verify/reset endpoints.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .constants import AVAILABLE_VARIABLES, TEMPLATE_KEYS
from .models import EmailLog, EmailTemplate
from .services.renderer import find_variables

User = get_user_model()


# ------------------------- Admin ------------------------- #


class EmailTemplateSerializer(serializers.ModelSerializer):
    referenced_variables = serializers.SerializerMethodField()
    unknown_variables = serializers.SerializerMethodField()

    class Meta:
        model = EmailTemplate
        fields = [
            "id",
            "key",
            "subject",
            "html_body",
            "text_body",
            "from_name",
            "is_active",
            "available_variables",
            "referenced_variables",
            "unknown_variables",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "key",
            "available_variables",
            "referenced_variables",
            "unknown_variables",
            "created_at",
            "updated_at",
        ]

    def _referenced(self, obj: EmailTemplate) -> set[str]:
        return (
            find_variables(obj.subject)
            | find_variables(obj.html_body)
            | find_variables(obj.text_body)
        )

    def get_referenced_variables(self, obj: EmailTemplate) -> list[str]:
        return sorted(self._referenced(obj))

    def get_unknown_variables(self, obj: EmailTemplate) -> list[str]:
        """Variables used in the template that are NOT in the documented catalogue."""
        allowed = set(AVAILABLE_VARIABLES.get(obj.key, {}).keys())
        return sorted(self._referenced(obj) - allowed)


class EmailTemplatePreviewSerializer(serializers.Serializer):
    context = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
        help_text="`{{변수}}` 치환에 사용할 값. 비워두면 샘플 값으로 자동 생성됩니다.",
    )


class EmailTemplateTestSendSerializer(serializers.Serializer):
    to_email = serializers.EmailField(help_text="테스트 발송 대상 이메일")
    context = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        required=False,
        help_text="템플릿 변수 값. 비워두면 샘플 값 사용",
    )


class EmailLogSerializer(serializers.ModelSerializer):
    user_email = serializers.CharField(source="user.email", read_only=True, default=None)

    class Meta:
        model = EmailLog
        fields = [
            "id",
            "user",
            "user_email",
            "template",
            "template_key",
            "to_email",
            "from_email",
            "subject",
            "status",
            "attempts",
            "provider_message_id",
            "error_message",
            "created_at",
            "sent_at",
        ]
        read_only_fields = fields


class EmailLogDetailSerializer(EmailLogSerializer):
    class Meta(EmailLogSerializer.Meta):
        fields = EmailLogSerializer.Meta.fields + [
            "rendered_html",
            "rendered_text",
            "context_snapshot",
        ]
        read_only_fields = fields


# --------------------- User-facing --------------------- #


class VerifyEmailRequestSerializer(serializers.Serializer):
    """Accept either a raw URL token or an (email, code) pair."""

    token = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False)
    code = serializers.RegexField(regex=r"^\d{6}$", required=False)

    def validate(self, attrs):
        if not attrs.get("token") and not (attrs.get("email") and attrs.get("code")):
            raise serializers.ValidationError(
                "token 또는 (email + code) 중 하나는 반드시 포함되어야 합니다."
            )
        return attrs


class ResendVerificationSerializer(serializers.Serializer):
    pass  # authenticated; no body needed


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    new_password = serializers.CharField(
        write_only=True, required=True, validators=[validate_password]
    )
    new_password_confirm = serializers.CharField(write_only=True, required=True)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError({"new_password_confirm": "비밀번호가 일치하지 않습니다."})
        return attrs
