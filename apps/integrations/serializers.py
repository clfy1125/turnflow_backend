"""
Instagram integration serializers
"""

from rest_framework import serializers
from .models import IGAccountConnection, AutoDMCampaign, SentDMLog, SpamFilterConfig, SpamCommentLog


class IGAccountConnectionSerializer(serializers.ModelSerializer):
    """Serializer for Instagram Account Connection"""

    workspace_id = serializers.UUIDField(source="workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    is_expired = serializers.SerializerMethodField()

    class Meta:
        model = IGAccountConnection
        fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "external_account_id",
            "username",
            "account_type",
            "token_expires_at",
            "scopes",
            "status",
            "last_verified_at",
            "error_message",
            "is_expired",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "external_account_id",
            "username",
            "account_type",
            "token_expires_at",
            "scopes",
            "status",
            "last_verified_at",
            "error_message",
            "created_at",
            "updated_at",
        ]

    def get_is_expired(self, obj):
        """Check if token is expired"""
        return obj.is_token_expired()


class ConnectionStartResponseSerializer(serializers.Serializer):
    """Response for connection start endpoint"""

    authorization_url = serializers.URLField()
    state = serializers.CharField()
    mode = serializers.CharField()


class ConnectionCallbackResponseSerializer(serializers.Serializer):
    """Response for connection callback endpoint"""

    success = serializers.BooleanField()
    message = serializers.CharField()
    connection = IGAccountConnectionSerializer(required=False)


class AutoDMCampaignSerializer(serializers.ModelSerializer):
    """Serializer for Auto DM Campaign"""

    ig_connection_id = serializers.UUIDField(source="ig_connection.id", read_only=True)
    ig_username = serializers.CharField(source="ig_connection.username", read_only=True)
    is_active = serializers.SerializerMethodField()
    can_send = serializers.SerializerMethodField()

    class Meta:
        model = AutoDMCampaign
        fields = [
            "id",
            "ig_connection_id",
            "ig_username",
            "media_id",
            "media_url",
            "name",
            "description",
            "message_template",
            "status",
            "max_sends_per_hour",
            "total_sent",
            "total_failed",
            "is_active",
            "can_send",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
        ]
        read_only_fields = [
            "id",
            "ig_connection_id",
            "ig_username",
            "total_sent",
            "total_failed",
            "is_active",
            "can_send",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
        ]

    def get_is_active(self, obj):
        return obj.is_active()

    def get_can_send(self, obj):
        return obj.can_send_more()


class AutoDMCampaignCreateSerializer(serializers.Serializer):
    """Serializer for creating Auto DM Campaign"""

    media_id = serializers.CharField(required=True, help_text="Instagram Media ID (게시물 ID)")
    media_url = serializers.URLField(
        required=False,
        allow_blank=True,
        allow_null=True,
        default=None,
        help_text="게시물 URL (선택사항, 비워두면 자동으로 null 처리)",
    )
    name = serializers.CharField(required=True, max_length=255, help_text="캠페인 이름")
    description = serializers.CharField(required=False, allow_blank=True, help_text="캠페인 설명")
    message_template = serializers.CharField(required=True, help_text="DM 메시지 템플릿")
    max_sends_per_hour = serializers.IntegerField(
        default=200, min_value=1, max_value=500, help_text="시간당 최대 발송 수 (기본값: 200)"
    )


class SentDMLogSerializer(serializers.ModelSerializer):
    """Serializer for Sent DM Log"""

    campaign_id = serializers.UUIDField(source="campaign.id", read_only=True)
    campaign_name = serializers.CharField(source="campaign.name", read_only=True)

    class Meta:
        model = SentDMLog
        fields = [
            "id",
            "campaign_id",
            "campaign_name",
            "comment_id",
            "comment_text",
            "recipient_user_id",
            "recipient_username",
            "message_sent",
            "status",
            "error_message",
            "error_code",
            "webhook_payload",
            "api_response",
            "created_at",
            "sent_at",
        ]
        read_only_fields = fields  # 모두 읽기 전용


class SpamFilterConfigSerializer(serializers.ModelSerializer):
    """스팸 필터 설정 Serializer"""

    ig_connection_id = serializers.UUIDField(source="ig_connection.id", read_only=True)
    ig_username = serializers.CharField(source="ig_connection.username", read_only=True)
    is_active = serializers.SerializerMethodField()

    class Meta:
        model = SpamFilterConfig
        fields = [
            "id",
            "ig_connection_id",
            "ig_username",
            "status",
            "spam_keywords",
            "block_urls",
            "total_spam_detected",
            "total_hidden",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "ig_connection_id",
            "ig_username",
            "total_spam_detected",
            "total_hidden",
            "created_at",
            "updated_at",
        ]

    def get_is_active(self, obj):
        """스팸 필터 활성화 여부"""
        return obj.is_active()


class SpamFilterConfigUpdateSerializer(serializers.ModelSerializer):
    """스팸 필터 설정 업데이트 Serializer"""

    class Meta:
        model = SpamFilterConfig
        fields = ["status", "spam_keywords", "block_urls"]

    def validate_spam_keywords(self, value):
        """스팸 키워드 검증"""
        if not isinstance(value, list):
            raise serializers.ValidationError("스팸 키워드는 리스트 형식이어야 합니다.")

        if len(value) > 100:
            raise serializers.ValidationError("스팸 키워드는 최대 100개까지 설정할 수 있습니다.")

        return value


class SpamCommentLogSerializer(serializers.ModelSerializer):
    """스팸 댓글 로그 Serializer"""

    spam_filter_id = serializers.UUIDField(source="spam_filter.id", read_only=True)
    ig_username = serializers.CharField(source="spam_filter.ig_connection.username", read_only=True)

    class Meta:
        model = SpamCommentLog
        fields = [
            "id",
            "spam_filter_id",
            "ig_username",
            "comment_id",
            "comment_text",
            "commenter_user_id",
            "commenter_username",
            "media_id",
            "spam_reasons",
            "status",
            "error_message",
            "webhook_payload",
            "api_response",
            "created_at",
            "hidden_at",
        ]
        read_only_fields = fields  # 모두 읽기 전용
