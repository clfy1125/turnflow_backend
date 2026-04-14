from rest_framework import serializers

from .models import AiJob


class AiJobCreateSerializer(serializers.Serializer):
    """POST /api/v1/ai/jobs/ 요청 바디."""

    slug = serializers.SlugField(
        max_length=120,
        required=False,
        allow_blank=True,
        default="",
        help_text="리메이크할 기존 페이지의 slug. 전달 시 해당 페이지의 현재 블록을 참고하여 AI가 리메이크합니다.",
    )
    concept = serializers.CharField(
        max_length=2000,
        help_text="페이지 컨셉 설명. 예: '제품 판매 랜딩 페이지', '밴드 프로필 페이지'",
    )
    model = serializers.ChoiceField(
        choices=AiJob.ModelTier.choices,
        default=AiJob.ModelTier.BASIC,
        help_text="AI 모델 티어. `basic`(3토큰), `pro`(100토큰, 개발중), `pro_plus`(500토큰, 개발중)",
    )


class AiJobSerializer(serializers.ModelSerializer):
    """GET /api/v1/ai/jobs/{id}/ 응답."""

    class Meta:
        model = AiJob
        fields = [
            "id",
            "status",
            "stage",
            "progress",
            "message",
            "job_type",
            "model_tier",
            "token_cost",
            "model_name",
            "result_json",
            "error_message",
            "created_at",
            "started_at",
            "finished_at",
        ]
        read_only_fields = fields


class AiJobListSerializer(serializers.ModelSerializer):
    """내 작업 목록 (결과 JSON 제외)."""

    class Meta:
        model = AiJob
        fields = [
            "id",
            "status",
            "stage",
            "progress",
            "message",
            "job_type",
            "model_tier",
            "token_cost",
            "model_name",
            "error_message",
            "created_at",
            "finished_at",
        ]
        read_only_fields = fields
