"""DRF serializers for the YouTube integration app."""

from rest_framework import serializers

from .models import (
    YouTubeAccountConnection,
    YouTubeCommentLog,
    YouTubeSpamFilterConfig,
    YouTubeVideoPost,
)


class YouTubeAccountConnectionSerializer(serializers.ModelSerializer):
    workspace_id = serializers.UUIDField(source="workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)
    is_expired = serializers.BooleanField(source="is_token_expired", read_only=True)

    class Meta:
        model = YouTubeAccountConnection
        fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "external_account_id",
            "channel_title",
            "channel_thumbnail_url",
            "google_email",
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


class YouTubeVideoPostSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="connection.id", read_only=True)
    workspace_id = serializers.UUIDField(source="connection.workspace.id", read_only=True)

    class Meta:
        model = YouTubeVideoPost
        fields = [
            "id",
            "connection_id",
            "workspace_id",
            "title",
            "description",
            "tags",
            "category_id",
            "privacy_status",
            "made_for_kids",
            "video_file_path",
            "video_size_bytes",
            "youtube_video_id",
            "status",
            "fail_reason",
            "quota_units_consumed",
            "created_at",
            "updated_at",
            "started_at",
            "published_at",
        ]
        read_only_fields = [
            "id",
            "connection_id",
            "workspace_id",
            "youtube_video_id",
            "status",
            "fail_reason",
            "quota_units_consumed",
            "created_at",
            "updated_at",
            "started_at",
            "published_at",
        ]


class YouTubeVideoUploadRequestSerializer(serializers.Serializer):
    """Input for ``POST /api/v1/youtube/videos/``."""

    connection_id = serializers.UUIDField(
        help_text="대상 YouTubeAccountConnection ID. 워크스페이스 멤버여야 함.",
    )
    title = serializers.CharField(
        max_length=100, help_text="영상 제목 (YouTube 100자 제한)",
    )
    description = serializers.CharField(
        required=False, allow_blank=True, default="",
        help_text="영상 설명. URL/멘션 포함 가능.",
    )
    tags = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False, default=list,
        help_text="태그 배열. YouTube 검색 가중치에 사용.",
    )
    category_id = serializers.CharField(
        required=False, default="22",
        help_text=(
            "YouTube category ID. 기본 22 (People & Blogs). "
            "전체 목록은 youtube.videoCategories.list 참고."
        ),
    )
    privacy_status = serializers.ChoiceField(
        choices=YouTubeVideoPost.PrivacyStatus.choices,
        default=YouTubeVideoPost.PrivacyStatus.PRIVATE,
        help_text="public / unlisted / private. 검수 통과 전에는 private 권장.",
    )
    made_for_kids = serializers.BooleanField(
        default=False,
        help_text="COPPA 대상 여부. 잘못 표기하면 채널 제재 위험.",
    )
    video_file_path = serializers.CharField(
        max_length=500,
        help_text="서버 측 영상 파일 경로 (절대경로 또는 MEDIA_ROOT 기반).",
    )
    video_size_bytes = serializers.IntegerField(
        required=False, default=0, min_value=0,
        help_text="(선택) 통계용. 미지정 시 task가 직접 stat() 으로 계산.",
    )


class YouTubeSpamFilterConfigSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="connection.id", read_only=True)

    class Meta:
        model = YouTubeSpamFilterConfig
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
            "ban_authors_on_reject",
            "total_spam_detected",
            "total_moderated",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "connection_id", "total_spam_detected", "total_moderated",
            "created_at", "updated_at",
        ]


class YouTubeCommentLogSerializer(serializers.ModelSerializer):
    connection_id = serializers.UUIDField(source="connection.id", read_only=True)

    class Meta:
        model = YouTubeCommentLog
        fields = [
            "id",
            "connection_id",
            "external_video_id",
            "external_thread_id",
            "external_comment_id",
            "commenter_channel_id",
            "commenter_display_name",
            "text",
            "status",
            "score",
            "reasons",
            "error_message",
            "created_at",
            "moderated_at",
        ]
        read_only_fields = fields
