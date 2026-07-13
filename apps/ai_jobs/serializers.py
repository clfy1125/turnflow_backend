from rest_framework import serializers

from apps.pages.models import Page, ReferenceCategory

from .models import AiJob, AiSourceImage


class AiJobCreateSerializer(serializers.Serializer):
    """POST /api/v1/ai/jobs/ 요청 바디."""

    slug = serializers.SlugField(
        max_length=120,
        required=False,
        allow_blank=True,
        default="",
        help_text="리메이크할 기존 페이지의 slug. 전달 시 해당 페이지의 현재 블록을 참고하여 AI가 리메이크합니다.",
    )
    apply_to_slug = serializers.SlugField(
        max_length=120,
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "새 페이지 생성 전용. 프론트가 미리 만들어 둔 빈 페이지의 slug. "
            "전달하면 작업 성공 시 백엔드가 result_json 을 이 페이지에 **자동 적용**한다 "
            "(별도 POST /api/v1/pages/ai/@{slug}/ 호출 불필요). "
            "리메이크(slug 전달) 시에는 무시된다 — 리메이크는 프론트가 롤백 UX와 함께 직접 적용."
        ),
    )
    concept = serializers.CharField(
        max_length=2000,
        help_text="페이지 컨셉 설명. 예: '제품 판매 랜딩 페이지', '밴드 프로필 페이지'",
    )
    model = serializers.ChoiceField(
        choices=AiJob.LlmModel.choices,
        default=AiJob.LlmModel.DEEPSEEK,
        required=False,
        help_text="사용할 AI 모델. `deepseek`(기본), `gemma`(자체 호스팅), `gpt5`(GPT-5.4, 개발 중)",
    )
    preserve_content = serializers.BooleanField(
        default=False,
        required=False,
        help_text=(
            "기존 텍스트 콘텐츠 보존 여부. "
            "False(기본): AI 가 컨셉에 맞게 자유롭게 다시 작성 (극적 변화). "
            "True: 표현은 다듬을 수 있지만 모든 의미·정보·줄바꿈/공백을 유지. "
            "기존에 없던 시각 속성(예: 테두리)도 임의로 추가하지 않음."
        ),
    )
    category = serializers.CharField(
        max_length=40,
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "페이지 카테고리 (새 페이지 생성 시 — **명시 권장**). 카테고리별 전용 레시피"
            "(섹션 구성·카피 톤·이미지 전략·디자인 변형)가 적용되어 품질이 크게 올라간다. "
            "허용값: `GET /api/v1/ai/categories/` 의 슬러그(`profile-link`/`digital-card`/`landing`/"
            "`portfolio`/`brochure`/`space-booking`/`group-buy`/`invitation`/`affiliate`/"
            "`commission`/`promotion`) 또는 내부 키(`profile`/`bizcard`/`rental`/`groupbuy`/`promo` 등). "
            "비우면 concept 문구에서 자동 추론. 리메이크(slug 전달)에서는 무시."
        ),
    )
    reference_page_slug = serializers.SlugField(
        max_length=120,
        required=False,
        allow_blank=True,
        default="",
        help_text=(
            "Few-shot 예시로 사용할 어드민이 큐레이션한 레퍼런스 페이지의 slug. "
            "전달 시 해당 페이지(is_reference=True, is_public=True)의 design_settings/블록 구조를 "
            "AI 에게 디자인 톤 참고 예시로 제공한다. "
            "비어 있으면 카테고리 기본 레퍼런스(예: invitation → @wedding)로 자동 폴백, "
            "그것도 없으면 기본 파일 예시."
        ),
    )
    image_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list,
        max_length=10,
        help_text=(
            "POST /api/v1/ai/source-images/ 로 먼저 업로드한 이미지 id 목록 (최대 10). "
            "전달하면 AI 가 라벨링 후 사용 가능한 이미지를 페이지에 배치한다. "
            "새 페이지 생성과 리메이크(full_restyle) 모두 지원 — 리메이크에선 기존 이미지는 "
            "보존되고 첨부 이미지가 새 블록(갤러리/쇼케이스)으로 추가된다. style_only 모드는 미지원."
        ),
    )

    def validate(self, data):
        cat = (data.get("category") or "").strip()
        if cat:
            from .services.category_profiles import normalize_category

            normalized = normalize_category(cat)
            if not normalized:
                raise serializers.ValidationError(
                    {
                        "category": (
                            "알 수 없는 카테고리입니다. GET /api/v1/ai/categories/ 의 "
                            "슬러그 중 하나를 보내세요."
                        )
                    }
                )
            data["category"] = normalized

        ref_slug = (data.get("reference_page_slug") or "").strip()
        if ref_slug:
            exists = Page.objects.filter(
                slug=ref_slug,
                is_reference=True,
                is_public=True,
                is_active=True,
            ).exists()
            if not exists:
                raise serializers.ValidationError(
                    {
                        "reference_page_slug": (
                            "레퍼런스 페이지를 찾을 수 없거나 활성/공개 상태가 아닙니다."
                        )
                    }
                )
        return data


class AiSourceImageSerializer(serializers.ModelSerializer):
    """POST /api/v1/ai/source-images/ 업로드 응답 (업로드된 이미지 1건)."""

    url = serializers.SerializerMethodField(
        help_text="업로드된 이미지 URL. (라벨링/배치 전 단계 — 표시·미리보기용)"
    )
    size_display = serializers.SerializerMethodField(
        help_text="사람이 읽기 좋은 파일 크기 (예: 320.5 KB)"
    )

    class Meta:
        model = AiSourceImage
        fields = [
            "id",
            "url",
            "mime_type",
            "size",
            "size_display",
            "width",
            "height",
            "original_name",
            "created_at",
        ]
        read_only_fields = fields

    def get_url(self, obj) -> str:
        request = self.context.get("request")
        if not obj.file:
            return ""
        if request:
            return request.build_absolute_uri(obj.file.url)
        return obj.file.url

    def get_size_display(self, obj) -> str:
        size = obj.size or 0
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"


class ReferenceCategorySerializer(serializers.ModelSerializer):
    """`GET /api/v1/ai/categories/` 응답."""

    reference_count = serializers.IntegerField(
        read_only=True,
        help_text="이 카테고리에 매핑된 활성 레퍼런스 페이지 수 (is_public + is_reference + snapshot=succeeded).",
    )

    class Meta:
        model = ReferenceCategory
        fields = [
            "slug",
            "name",
            "description",
            "icon_emoji",
            "icon_url",
            "sort_order",
            "reference_count",
        ]
        read_only_fields = fields


class ReferencePageListSerializer(serializers.ModelSerializer):
    """`GET /api/v1/ai/categories/{slug}/references/` 응답 항목."""

    reference_snapshot_url = serializers.SerializerMethodField()
    effective_title = serializers.SerializerMethodField()

    class Meta:
        model = Page
        fields = [
            "slug",
            "title",
            "effective_title",
            "reference_title",
            "reference_description",
            "reference_order",
            "reference_snapshot_url",
            "reference_snapshot_updated_at",
        ]
        read_only_fields = fields

    def get_reference_snapshot_url(self, obj: Page):
        if not obj.reference_snapshot:
            return None
        url = obj.reference_snapshot.url
        request = self.context.get("request")
        if request is not None and url.startswith("/"):
            return request.build_absolute_uri(url)
        return url

    def get_effective_title(self, obj: Page) -> str:
        return (obj.reference_title or "").strip() or obj.title


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


# ── SNS 게시물 카테고리 분류 ────────────────────────────────────


class ClassifyPostItemSerializer(serializers.Serializer):
    """ClassifyPosts 요청 내 게시물 한 건."""

    id = serializers.CharField(
        max_length=120,
        help_text="게시물 식별자 (응답 assignments.post_id 와 1:1 매칭). Apify shortCode 또는 임의 ID.",
    )
    caption = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="게시물 본문/캡션. 길어도 OK — 서버에서 적절히 잘라 LLM에 전달.",
    )
    hashtags = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        default=list,
        help_text="해시태그 (# 제외)",
    )
    type = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Image / Video / Sidecar 등 게시물 타입",
    )
    likes = serializers.IntegerField(required=False, default=0)
    comments = serializers.IntegerField(required=False, default=0)
    timestamp = serializers.CharField(required=False, allow_blank=True, default="")
    thumbnail_url = serializers.URLField(
        required=False,
        allow_blank=True,
        default="",
        help_text="썸네일 URL. (현재는 텍스트 기반 분류만 — 향후 비전 입력으로 확장 시 사용)",
    )


class ClassifyCategoryItemSerializer(serializers.Serializer):
    """기존 카테고리 한 건."""

    label = serializers.CharField(max_length=40)
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="LLM 이 의미 판단에 쓸 한 줄 설명",
    )


class ClassifyArtistContextSerializer(serializers.Serializer):
    """작가 컨텍스트 (선택)."""

    name = serializers.CharField(required=False, allow_blank=True, default="")
    category = serializers.CharField(required=False, allow_blank=True, default="")
    genre = serializers.CharField(required=False, allow_blank=True, default="")
    bio = serializers.CharField(required=False, allow_blank=True, default="")


class ClassifyPostsRequestSerializer(serializers.Serializer):
    """POST /api/v1/ai/classify-posts/ 요청 바디."""

    posts = ClassifyPostItemSerializer(
        many=True,
        min_length=1,
        max_length=20,
        help_text="분류할 게시물 배치. 1~20개. 속도-품질 균형은 6~9 권장.",
    )
    existing_categories = ClassifyCategoryItemSerializer(
        many=True,
        required=False,
        default=list,
        help_text="이미 정해진 카테고리 목록. 첫 호출에서는 빈 배열.",
    )
    artist_context = ClassifyArtistContextSerializer(
        required=False,
        default=dict,
        help_text="작가 메타 (LLM 이 톤/장르 판단에 활용).",
    )
    max_categories = serializers.IntegerField(
        required=False,
        default=6,
        min_value=1,
        max_value=12,
        help_text="한 페이지가 가질 카테고리 총 상한. 기본 6.",
    )
    model = serializers.ChoiceField(
        choices=AiJob.LlmModel.choices,
        default=AiJob.LlmModel.DEEPSEEK,
        required=False,
        help_text="LLM 모델. 기본 deepseek (외부 API). gemma(자체 호스팅, 무료)도 선택 가능.",
    )
    max_tokens = serializers.IntegerField(
        required=False,
        default=2500,
        min_value=256,
        max_value=8000,
    )
    temperature = serializers.FloatField(
        required=False,
        default=0.1,
        min_value=0.0,
        max_value=2.0,
        help_text="결정성 높이려고 기본 0.1.",
    )
    use_vision = serializers.BooleanField(
        required=False,
        default=True,
        help_text=(
            "True 이면 각 post.thumbnail_url 을 ``image_url`` 멀티모달 블록으로 함께 보내,"
            " LLM 이 이미지 안의 한국어 제목을 읽고 카테고리/제목을 판단한다."
            " False 면 텍스트(캡션/태그)만으로 분류."
        ),
    )


class ClassifyAssignmentSerializer(serializers.Serializer):
    post_id = serializers.CharField()
    category_label = serializers.CharField()
    is_new_category = serializers.BooleanField()
    suggested_title = serializers.CharField()
    title_source = serializers.ChoiceField(
        choices=["image", "caption", "fallback"],
        help_text=(
            "제목 출처. image=이미지 안 텍스트 사용, caption=캡션에서 추출,"
            " fallback=카테고리+번호 자동."
        ),
        required=False,
        default="caption",
    )


class ClassifyNewCategorySerializer(serializers.Serializer):
    label = serializers.CharField()
    description = serializers.CharField(allow_blank=True)


class ClassifyPostsResponseSerializer(serializers.Serializer):
    model = serializers.CharField()
    elapsed_seconds = serializers.FloatField()
    assignments = ClassifyAssignmentSerializer(many=True)
    new_categories = ClassifyNewCategorySerializer(many=True)
    usage = AiLlmTryUsageSerializer()


# ── AutoDM 캠페인 폼-작성 도움 (AI suggest) ─────────────────────


class AutoDMCampaignAiSuggestRequestSerializer(serializers.Serializer):
    """POST /api/v1/integrations/auto-dm-campaigns/ai-suggest/ 요청 바디.

    게시물(이미지+캡션)을 gemma-4 로 분석해 AutoDM 캠페인 폼 초안을 생성한다.
    프론트가 게시물 목록에서 이미 받은 caption/image_url 을 그대로 넘기는 것을 권장
    (mock 모드 dev 에서도 동작, Graph 재조회 불필요). caption/image_url 둘 다 비어 있으면
    media_id 로 백엔드가 Graph API 조회를 시도한다(mock 모드/연결 없음 시 400).
    """

    media_id = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="게시물 IG media id (참고용 echo + caption/image 미제공 시 Graph 조회 키).",
    )
    ig_connection_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Graph 조회에 사용할 IG connection UUID. 미지정 시 첫 활성 connection.",
    )
    caption = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="게시물 캡션. 제공 시 Graph 조회를 건너뛴다 (권장).",
    )
    image_url = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="게시물 이미지/썸네일 URL. 제공 시 백엔드가 받아 base64 로 비전 입력에 사용.",
    )
    media_type = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="IMAGE / VIDEO / CAROUSEL_ALBUM 등 (참고용 echo).",
    )
    business_type = serializers.CharField(
        required=False, allow_blank=True, default="", help_text="업종 힌트 (선택)."
    )
    campaign_goal = serializers.CharField(
        required=False, allow_blank=True, default="", help_text="캠페인 목적 힌트 (선택)."
    )
    tone = serializers.CharField(
        required=False, allow_blank=True, default="", help_text="원하는 말투 힌트 (선택)."
    )
    link_url = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="보낼 링크 (선택). link_button 으로 제안됨 — 주면 그 URL, 안 주면 예시 URL(교체용). 본문엔 안 들어감.",
    )
    include_follow_gate = serializers.BooleanField(
        required=False, default=True, help_text="팔로우 게이트 문구도 생성할지 (기본 true)."
    )
    reply_variant_count = serializers.IntegerField(
        required=False,
        default=50,
        min_value=1,
        max_value=50,
        help_text="공개 답글 변형 개수 (기본 50, 1~50). 코드 풀에서 즉시 추출(LLM 미사용).",
    )

    def validate(self, attrs):
        has_context = any(
            (attrs.get(k) or "").strip() for k in ("caption", "image_url", "media_id")
        )
        if not has_context:
            raise serializers.ValidationError(
                "caption, image_url, media_id 중 최소 하나는 필요합니다."
            )
        return attrs


class AutoDMCampaignAiSuggestJobSerializer(serializers.Serializer):
    """POST .../ai-suggest/ 의 202 응답 — 생성 작업이 큐에 등록됨.

    gemma-4 가 답글 변형 등 긴 출력을 만드느라 수십 초가 걸려 동기 응답은 prod 타임아웃에
    걸린다. 그래서 작업을 큐에 넣고 job_id 를 즉시 반환하며, 프론트는 poll_url 을 1~2초
    간격으로 폴링해 status=succeeded 가 되면 result_json.suggestion 을 폼에 채운다.
    """

    job_id = serializers.UUIDField(help_text="생성 작업 ID")
    status = serializers.CharField(help_text="작업 상태 (queued)")
    poll_url = serializers.CharField(help_text="작업 상태 폴링 URL (GET)")
    message = serializers.CharField(help_text="안내 메시지")


class DmOpeningDiversifyRequestSerializer(serializers.Serializer):
    """POST /api/v1/integrations/auto-dm-campaigns/diversify-opening/ 요청 바디.

    사용자가 폼에서 확정한 오프닝 DM 1개를 넘기면, gemma-4 가 톤·의미를 유지한 채 표현만 바꾼
    변형 count 개를 생성한다(스팸 탐지 회피용 오프닝 다양화). 비동기(AiJob) — 202 로 job_id 를
    돌려주고, 프론트는 poll_url 을 폴링해 result_json.variants 를 캠페인 opening 변형 풀로 쓴다.
    """

    opening_message = serializers.CharField(
        max_length=2000,
        help_text=(
            "다양화할 원문 오프닝 DM. 이 문구의 톤·의미를 기준으로 변형이 생성된다. "
            "실제 오프닝(버튼 640자 / 일반 1000바이트)보다 넉넉한 2000자까지 허용(과도한 입력 방어)."
        ),
    )
    count = serializers.IntegerField(
        required=False,
        default=10,
        min_value=2,
        max_value=30,
        help_text="생성할 변형 개수 (기본 10, 2~30). 많을수록 생성 시간 증가(30개 ≈ 25초).",
    )
    tone = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="참고 톤 힌트 (선택). 비우면 원문 톤을 그대로 유지.",
    )


class DmOpeningDiversifyJobSerializer(serializers.Serializer):
    """POST .../diversify-opening/ 의 202 응답 — 변형 생성 작업이 큐에 등록됨.

    프론트는 poll_url 을 1~2초 간격 폴링해 status=succeeded 가 되면 result_json.variants
    (문자열 배열)를 캠페인 opening 변형 풀로 쓴다.
    """

    job_id = serializers.UUIDField(help_text="변형 생성 작업 ID")
    status = serializers.CharField(help_text="작업 상태 (queued)")
    poll_url = serializers.CharField(help_text="작업 상태 폴링 URL (GET)")
    requested_count = serializers.IntegerField(help_text="요청한 변형 개수 (2~30 클램프 후)")
    message = serializers.CharField(help_text="안내 메시지")
