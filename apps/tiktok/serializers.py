"""DRF serializers for the TikTok integration app."""

from rest_framework import serializers

from .models import (
    TikTokAccountConnection,
    TikTokCommentLog,
    TikTokSpamFilterConfig,
    TikTokVideoPost,
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
            "external_account_id",
            "union_id",
            "username",
            "avatar_url",
            "scopes",
            "status",
            "is_audited",
            "token_expires_at",
            "refresh_token_expires_at",
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


class TikTokVideoPostSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="connection.id", read_only=True)
    workspace_id = serializers.UUIDField(source="connection.workspace.id", read_only=True)

    class Meta:
        model = TikTokVideoPost
        fields = [
            "id",
            "connection_id",
            "workspace_id",
            "caption",
            "source_type",
            "video_url",
            "video_size_bytes",
            "requested_privacy",
            "effective_privacy",
            "disable_duet",
            "disable_comment",
            "disable_stitch",
            "publish_id",
            "tiktok_video_id",
            "status",
            "fail_reason",
            "retry_count",
            "created_at",
            "updated_at",
            "initiated_at",
            "uploaded_at",
            "published_at",
        ]
        read_only_fields = [
            "id",
            "connection_id",
            "workspace_id",
            "effective_privacy",
            "publish_id",
            "tiktok_video_id",
            "status",
            "fail_reason",
            "retry_count",
            "created_at",
            "updated_at",
            "initiated_at",
            "uploaded_at",
            "published_at",
        ]


class TikTokVideoPublishRequestSerializer(serializers.Serializer):
    """Input for ``POST /api/v1/tiktok/videos/``."""

    connection_id = serializers.UUIDField(
        help_text="대상 TikTokAccountConnection ID. 워크스페이스 멤버여야 함.",
    )
    caption = serializers.CharField(
        max_length=2200, allow_blank=True, default="",
        help_text="TikTok 캡션. 해시태그/멘션 포함 가능. 최대 2200자.",
    )
    source_type = serializers.ChoiceField(
        choices=TikTokVideoPost.SourceType.choices,
        default=TikTokVideoPost.SourceType.PULL_FROM_URL,
        help_text="PULL_FROM_URL(검증된 도메인 URL을 TikTok이 fetch) 또는 FILE_UPLOAD(서버에서 직접 업로드).",
    )
    video_url = serializers.URLField(
        required=False, allow_blank=True, default="",
        help_text="source_type=PULL_FROM_URL 일 때 필수. TikTok 앱 대시보드에 등록된 도메인이어야 함.",
    )
    video_size_bytes = serializers.IntegerField(
        required=False, default=0, min_value=0,
        help_text="source_type=FILE_UPLOAD 일 때 필수. 청크 분할 계산에 사용.",
    )
    video_file_path = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="source_type=FILE_UPLOAD 일 때 서버 측 파일 경로 (MEDIA_ROOT 기준 가능).",
    )
    requested_privacy = serializers.ChoiceField(
        choices=TikTokVideoPost.Privacy.choices,
        default=TikTokVideoPost.Privacy.SELF_ONLY,
        help_text=(
            "사용자가 원하는 공개 범위. 미감사 클라이언트는 무엇을 보내든 SELF_ONLY로 강제됨."
        ),
    )
    disable_duet = serializers.BooleanField(default=False)
    disable_comment = serializers.BooleanField(default=False)
    disable_stitch = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if attrs["source_type"] == TikTokVideoPost.SourceType.PULL_FROM_URL:
            if not attrs.get("video_url"):
                raise serializers.ValidationError(
                    {"video_url": "PULL_FROM_URL source_type requires video_url."}
                )
        else:  # FILE_UPLOAD
            if not attrs.get("video_size_bytes"):
                raise serializers.ValidationError(
                    {"video_size_bytes": "FILE_UPLOAD source_type requires video_size_bytes."}
                )
        return attrs


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
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "connection_id", "total_spam_detected", "total_hidden",
            "created_at", "updated_at",
        ]


class TikTokCommentLogSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="connection.id", read_only=True)

    class Meta:
        model = TikTokCommentLog
        fields = [
            "id",
            "connection_id",
            "external_video_id",
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
