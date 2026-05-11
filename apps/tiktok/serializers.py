"""DRF serializers for the TikTok integration app (Business API)."""

from rest_framework import serializers

from .models import (
    TikTokAccountConnection,
    TikTokBlockedWord,
    TikTokCommentLog,
    TikTokSpamFilterConfig,
)


class TikTokAccountConnectionSerializer(serializers.ModelSerializer):
    workspace_id = serializers.UUIDField(source="workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    is_expired = serializers.BooleanField(source="is_token_expired", read_only=True)

    class Meta:
        model = TikTokAccountConnection
        fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "external_account_id",  # advertiser_id
            "bc_id",
            "advertiser_name",
            "scopes",
            "status",
            "token_expires_at",
            "is_expired",
            "last_verified_at",
            "error_message",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class ConnectionStartResponseSerializer(serializers.Serializer):
    authorization_url = serializers.URLField()
    state = serializers.CharField()
    mode = serializers.ChoiceField(choices=["mock", "production"])


# ─────────────────────────────────────────────────────────────────────────────
# Spam filter config
# ─────────────────────────────────────────────────────────────────────────────

class TikTokSpamFilterConfigSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="connection.id", read_only=True)

    class Meta:
        model = TikTokSpamFilterConfig
        fields = [
            "id",
            "connection_id",
            "status",
            "spam_keywords",
            "block_urls",
            "block_shortened_urls",
            "min_length",
            "max_emoji_ratio",
            "max_mentions",
            "score_threshold",
            "default_action",
            "total_spam_detected",
            "total_hidden",
            "total_deleted",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "connection_id",
            "total_spam_detected",
            "total_hidden",
            "total_deleted",
            "created_at",
            "updated_at",
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Comment log
# ─────────────────────────────────────────────────────────────────────────────

class TikTokCommentLogSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="connection.id", read_only=True)

    class Meta:
        model = TikTokCommentLog
        fields = [
            "id",
            "connection_id",
            "advertiser_id",
            "ad_id",
            "creative_id",
            "parent_comment_id",
            "external_comment_id",
            "commenter_external_id",
            "commenter_username",
            "text",
            "status",
            "score",
            "reasons",
            "error_message",
            "created_at",
            "moderated_at",
        ]
        read_only_fields = fields


# ─────────────────────────────────────────────────────────────────────────────
# Action request bodies
# ─────────────────────────────────────────────────────────────────────────────

class CommentFetchRequestSerializer(serializers.Serializer):
    connection_id = serializers.UUIDField(
        help_text="대상 TikTokAccountConnection ID (advertiser).",
    )
    ad_id = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="특정 광고만 가져오려면 지정. 비우면 advertiser 전체.",
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(
        required=False, default=20, min_value=1, max_value=100,
    )


class CommentReplyRequestSerializer(serializers.Serializer):
    text = serializers.CharField(
        max_length=500,
        help_text="답글 본문. TikTok 광고 댓글 답글 제한 내",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Blocked words
# ─────────────────────────────────────────────────────────────────────────────

class TikTokBlockedWordSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="connection.id", read_only=True)

    class Meta:
        model = TikTokBlockedWord
        fields = [
            "id",
            "connection_id",
            "word",
            "external_id",
            "is_synced",
            "last_synced_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "connection_id", "external_id",
            "is_synced", "last_synced_at",
            "created_at", "updated_at",
        ]


class BlockedWordsBulkRequestSerializer(serializers.Serializer):
    connection_id = serializers.UUIDField()
    words = serializers.ListField(
        child=serializers.CharField(max_length=255),
        min_length=1, max_length=200,
        help_text="TikTok 측에 신규 등록할 차단 단어 배열.",
    )


class BlockedWordsCheckRequestSerializer(serializers.Serializer):
    connection_id = serializers.UUIDField()
    words = serializers.ListField(
        child=serializers.CharField(max_length=255),
        min_length=1, max_length=200,
        help_text="확인할 단어 배열. TikTok 측 차단 등록 여부를 반환.",
    )
