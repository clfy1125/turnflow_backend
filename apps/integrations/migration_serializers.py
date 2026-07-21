"""DM 캠페인 이전 API 시리얼라이저 (start / job 폴링 / 후보 / apply)."""

from __future__ import annotations

from rest_framework import serializers

from apps.ai_jobs.models import AiJob

from .models import AutoDMCampaign, DMCampaignCandidate, DMMigrationJob


class DMMigrationJobStartSerializer(serializers.Serializer):
    """분석 잡 시작 요청."""

    ig_connection_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="분석할 IG 계정 connection UUID. 미지정 시 워크스페이스의 첫 활성 연결.",
    )
    media_limit = serializers.IntegerField(
        required=False,
        default=50,
        min_value=10,
        max_value=100,
        help_text="분석할 최근 게시물 수 (10~100, 기본 50).",
    )
    force = serializers.BooleanField(
        required=False,
        default=False,
        help_text="True 면 24h 내 완료 결과가 있어도 새 분석을 강제(종료 1h 이내면 429).",
    )
    llm_model = serializers.ChoiceField(
        choices=AiJob.LlmModel.choices,
        required=False,
        default=AiJob.LlmModel.DEEPSEEK,
        help_text="분석에 쓸 LLM (기본 deepseek).",
    )


class DMMigrationJobSerializer(serializers.ModelSerializer):
    """잡 폴링 응답 — status/stage/progress + 카운터/에러/후보수."""

    counters = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()
    candidate_count = serializers.SerializerMethodField()

    class Meta:
        model = DMMigrationJob
        fields = [
            "id",
            "status",
            "stage",
            "progress",
            "message",
            "counters",
            "error",
            "candidate_count",
            "media_limit",
            "llm_model",
            "created_at",
            "started_at",
            "finished_at",
            "raw_expires_at",
            "raw_purged_at",
            "resume_at",
        ]

    def get_counters(self, obj) -> dict:
        return {
            "media_scanned": obj.media_scanned,
            "comments_collected": obj.comments_collected,
            "conversations_scanned": obj.conversations_scanned,
            "dm_messages_collected": obj.dm_messages_collected,
            "templates_found": obj.templates_found,
            "candidates_created": obj.candidates_created,
        }

    def get_error(self, obj) -> dict | None:
        if not obj.error_code and not obj.error_message:
            return None
        return {"code": obj.error_code, "message": obj.error_message}

    def get_candidate_count(self, obj) -> int:
        # prefetch/annotate 없으면 쿼리 1회 — 폴링 빈도 감안 허용(후보 수는 작다).
        return obj.candidates.count()


class DMCampaignCandidateSerializer(serializers.ModelSerializer):
    """후보 1건 (검수 UI). evidence_raw 는 7일 후 파기되면 null 로 직렬화된다."""

    job_id = serializers.UUIDField(read_only=True)
    applied_campaign_id = serializers.UUIDField(read_only=True, allow_null=True)

    class Meta:
        model = DMCampaignCandidate
        fields = [
            "id",
            "job_id",
            "status",
            "band",
            "media_id",
            "media_permalink",
            "media_caption_excerpt",
            "media_timestamp",
            "suggested_keywords",
            "suggested_keyword_mode",
            "confidence",
            "draft_name",
            "draft_description",
            "draft_opening_message",
            "draft_public_reply_templates",
            "follow_up_candidates",
            "matched_template",
            "evidence_aggregates",
            "evidence_raw",
            "applied_campaign_id",
            "applied_at",
            "dismissed_at",
            "created_at",
        ]


class CandidateApplyRequestSerializer(serializers.Serializer):
    """apply 시 선택 오버라이드 — 미지정 필드는 후보 초안값 사용."""

    name = serializers.CharField(required=False, max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    keyword_filter = serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=128), required=False
    )
    keyword_mode = serializers.ChoiceField(
        choices=AutoDMCampaign.KeywordMode.choices, required=False
    )
    opening_message_template = serializers.CharField(required=False, allow_blank=True)
    public_reply_enabled = serializers.BooleanField(required=False)
    public_reply_templates = serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=2200), required=False
    )
    media_id = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=255,
        help_text="template_only 후보를 특정 게시물에 붙일 때 필수.",
    )
