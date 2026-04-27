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
        choices=AiJob.LlmModel.choices,
        default=AiJob.LlmModel.GEMMA,
        required=False,
        help_text="사용할 AI 모델. `gemma`(기본), `gpt5`(GPT-5.4, 개발 중)",
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
            "model_name",
            "error_message",
            "created_at",
            "finished_at",
        ]
        read_only_fields = fields


class PageAiJobHistoryItemSerializer(serializers.ModelSerializer):
    """특정 페이지에 연결된 AI 작업 이력 목록 항목.

    `AiJobListSerializer` 와 달리 롤백 UI에 필요한 두 필드를 추가로 노출한다.
      - `concept`       : 작업 생성 시 사용자가 입력한 컨셉 (프롬프트 요약용)
      - `can_rollback`  : 이 작업물로 페이지를 롤백 가능한지 여부
                          (`status == succeeded` AND `result_json is not null`)
    """

    concept = serializers.SerializerMethodField()
    can_rollback = serializers.SerializerMethodField()

    class Meta:
        model = AiJob
        fields = [
            "id",
            "status",
            "stage",
            "progress",
            "message",
            "job_type",
            "llm_model",
            "model_name",
            "concept",
            "can_rollback",
            "error_message",
            "created_at",
            "started_at",
            "finished_at",
        ]
        read_only_fields = fields

    def get_concept(self, obj: AiJob) -> str:
        payload = obj.input_payload or {}
        return payload.get("concept", "") if isinstance(payload, dict) else ""

    def get_can_rollback(self, obj: AiJob) -> bool:
        return obj.status == AiJob.Status.SUCCEEDED and obj.result_json is not None


class AiJobRollbackResponseSerializer(serializers.Serializer):
    """POST /api/v1/ai/jobs/{id}/rollback/ 응답 바디."""

    job_id = serializers.UUIDField(help_text="롤백에 사용된 AiJob ID")
    page_slug = serializers.CharField(help_text="롤백 적용된 페이지 slug")
    applied_at = serializers.DateTimeField(help_text="롤백 적용 시각 (서버 시간)")
    detail = serializers.CharField(help_text="사람이 읽을 수 있는 결과 메시지")


# ── 실험용 동기 LLM 호출 (DeepSeek 검증) ──────────────────────


class AiLlmTryRequestSerializer(serializers.Serializer):
    """POST /api/v1/ai/test/llm/ 요청 바디."""

    concept = serializers.CharField(
        max_length=2000,
        help_text="페이지 컨셉. 실제 build_prompts 와 동일한 파이프라인을 탄다.",
    )
    slug = serializers.SlugField(
        max_length=120,
        required=False,
        allow_blank=True,
        default="",
        help_text="리메이크 모드로 테스트하려면 본인 소유 페이지 slug 전달.",
    )
    model = serializers.ChoiceField(
        choices=AiJob.LlmModel.choices,
        default=AiJob.LlmModel.DEEPSEEK,
        required=False,
        help_text="기본값 deepseek. gemma 와도 비교 가능.",
    )
    max_tokens = serializers.IntegerField(
        required=False,
        default=8000,
        min_value=128,
        max_value=16000,
    )
    temperature = serializers.FloatField(
        required=False,
        default=0.2,
        min_value=0.0,
        max_value=2.0,
    )


class AiLlmTryUsageSerializer(serializers.Serializer):
    prompt_tokens = serializers.IntegerField()
    completion_tokens = serializers.IntegerField()
    total_tokens = serializers.IntegerField()
    cache_hit_tokens = serializers.IntegerField(help_text="캐시에서 재사용된 입력 토큰")
    cache_miss_tokens = serializers.IntegerField(help_text="새로 계산된 입력 토큰")
    estimated_cost_usd = serializers.FloatField(help_text="가격표 기반 추정 비용 (USD)")


class AiLlmTryResponseSerializer(serializers.Serializer):
    model = serializers.CharField(help_text="실제 호출된 LiteLLM 모델명")
    elapsed_seconds = serializers.FloatField(help_text="LLM 호출 소요 시간")
    content = serializers.CharField(help_text="LLM 원본 텍스트 응답")
    parsed_json = serializers.JSONField(
        help_text="content 에서 JSON 추출 성공 시 dict, 실패 시 null"
    )
    parse_error = serializers.CharField(
        allow_null=True,
        allow_blank=True,
        help_text="JSON 파싱 실패 시 에러 메시지",
    )
    usage = AiLlmTryUsageSerializer()
    prompt_preview = serializers.DictField(
        child=serializers.CharField(),
        help_text="실제 모델에 보낸 프롬프트 ({system, user_head, user_tail}). 디버깅용.",
    )
