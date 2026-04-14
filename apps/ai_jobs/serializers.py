from rest_framework import serializers

from .models import AiJob


class AiJobCreateSerializer(serializers.Serializer):
    """POST /api/v1/ai/jobs/ 요청 바디."""

    page_id = serializers.IntegerField(
        help_text="AI 결과를 적용할 Page ID. (선택 — 생략 시 결과만 생성, 적용은 별도)",
        required=False,
        allow_null=True,
        default=None,
    )
    slug = serializers.SlugField(
        max_length=120,
        required=False,
        allow_blank=True,
        default="",
        help_text="리메이크할 기존 페이지의 slug. 전달 시 해당 페이지의 현재 블록을 참고하여 AI가 리메이크합니다.",
    )
    job_type = serializers.ChoiceField(
        choices=AiJob.JobType.choices,
        default=AiJob.JobType.BIO_REMAKE,
        help_text="작업 유형. 현재는 `bio_remake`만 지원.",
    )
    concept = serializers.CharField(
        max_length=2000,
        help_text="페이지 컨셉 설명. 예: '제품 판매 랜딩 페이지', '밴드 프로필 페이지'",
    )
    style = serializers.CharField(
        max_length=1000,
        required=False,
        allow_blank=True,
        default="",
        help_text="원하는 디자인 스타일. 예: '다크 모드, 미니멀', '밝고 귀여운 느낌'",
    )
    reference_text = serializers.CharField(
        max_length=5000,
        required=False,
        allow_blank=True,
        default="",
        help_text="참고 텍스트 (브랜드 소개, 상품 목록 등). AI가 콘텐츠 작성에 활용.",
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
            "model_name",
            "result_json",
            "error_message",
            "created_at",
            "started_at",
            "finished_at",
        ]
        read_only_fields = fields


class AiJobListSerializer(serializers.ModelSerializer):
    """GET /api/v1/ai/jobs/ 목록 응답 (result_json 제외하여 가볍게)."""

    class Meta:
        model = AiJob
        fields = [
            "id",
            "status",
            "stage",
            "progress",
            "message",
            "job_type",
            "model_name",
            "error_message",
            "created_at",
            "finished_at",
        ]
        read_only_fields = fields
