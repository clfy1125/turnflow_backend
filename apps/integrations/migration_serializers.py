"""DM 캠페인 이전 API 시리얼라이저 (start / job 폴링 / 후보 / apply)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.ai_jobs.models import AiJob

from .models import AutoDMCampaign, DMCampaignCandidate, DMMigrationJob

# 실제로 채워지는 transfer.drops 코드 — 관측으로 판정 가능한 것만 넣는다.
# (프론트가 들고 있는 12종 중 나머지는 원본 DM 만 봐서는 알 수 없어 **절대 안 내려간다**.)
TRANSFER_DROP_CODES = (
    "attachment_image",  # 원본 DM 에 사진 첨부가 있었다
    "attachment_video",
    "attachment_file",
    "carousel",  # 첨부 2장 이상(넘겨보는 카드)
    "message_sequence",  # 게이트는 찾았는데 뒤따르는 본 DM 을 못 찾음 = 여러 통 구조의 일부 유실
)


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
        help_text=(
            "True 면 **7일 캐시**(완료 결과 재사용)를 무시하고 새 분석을 강제한다. "
            "직전 분석 종료 후 **6시간** 이내면 429(쿨다운). "
            "force 없이 그냥 호출하면 거부되지 않고 200 + reused=true 로 캐시 결과가 온다."
        ),
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
    estimate = serializers.SerializerMethodField()
    # DB 는 smallint 라 스키마에 max 32767 이 노출됐다. 실제 범위는 0~100 이므로 명시한다.
    progress = serializers.IntegerField(
        read_only=True,
        min_value=0,
        max_value=100,
        help_text="0~100. 단조 증가한다(되돌아가지 않음).",
    )

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
            "estimate",
            "trigger_source",
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

    def get_estimate(self, obj) -> dict | None:
        """예상 소요 — 1단계(estimating)가 끝나면 채워진다.

        프론트는 이 값이 들어온 순간부터 "약 N분 걸려요" + 진행바를 그릴 수 있다.
        `ready` 가 되기 전엔 null 일 수 있으므로 null 체크 필수.
        """
        if obj.estimated_seconds is None:
            return None
        d = obj.estimate_detail or {}
        return {
            "seconds": obj.estimated_seconds,
            "seconds_max": d.get("seconds_max"),
            "media_with_comments": d.get("media_with_comments"),
            "computed_at": obj.estimated_at.isoformat() if obj.estimated_at else None,
        }

    def get_error(self, obj) -> dict | None:
        """실패 사유. ``code`` 는 머신 키, ``message`` 는 **그대로 보여줘도 되는 사용자 문구**.

        실제로 나올 수 있는 ``code`` 는 두 가지뿐이다.
          - ``token_expired`` — IG 연결 만료/권한 없음(재연결 CTA 를 띄우면 된다). 종결성 실패.
          - ``error`` — 그 외 예기치 못한 실패. 재시도 가능.
        (``token_invalid``·``connection_inactive`` 는 이 잡에서 쓰지 않는다 — 비활성 연결은
        잡을 만들기 전에 400 으로 막힌다.)
        """
        if not obj.error_code and not obj.error_message:
            return None
        return {"code": obj.error_code, "message": obj.error_message}

    def get_candidate_count(self, obj) -> int:
        # prefetch/annotate 없으면 쿼리 1회 — 폴링 빈도 감안 허용(후보 수는 작다).
        return obj.candidates.count()


class DMMigrationJobStartResponseSerializer(serializers.Serializer):
    """``POST jobs/`` 응답 — 잡 객체를 **봉투**에 담아 준다(스키마가 잡 객체 단독이라 어긋나 있었다)."""

    reused = serializers.BooleanField(
        help_text="true = 새로 돌리지 않고 기존/캐시 잡을 그대로 준 것(HTTP 200). false = 새 잡(201)."
    )
    job = DMMigrationJobSerializer()


class OfferSerializer(serializers.Serializer):
    """복원한 자료 링크. ⚠️ 라벨 키는 `label` 이 아니라 **`button_label`** 이다."""

    url = serializers.CharField(
        allow_blank=True, help_text="확인/수정본 우선. 못 찾았으면 빈 문자열."
    )
    button_label = serializers.CharField(allow_blank=True, help_text="원본 버튼 글자.")
    confirmed = serializers.BooleanField(help_text="사용자가 '이 링크 맞아요' 를 눌렀는지.")
    edited = serializers.BooleanField(help_text="사용자가 링크를 고쳤는지.")


class SupportSerializer(serializers.Serializer):
    """근거 강도 — 같은 게시물 댓글러 몇 명이 같은 DM 을 받았는가."""

    hits = serializers.IntegerField(help_text="같은 DM 을 받은 사람 수.")
    probed = serializers.IntegerField(help_text="확인해 본 댓글러 수.")
    score = serializers.FloatField(help_text="Wilson 신뢰하한(0~1). 0.60 이상이 auto_draft.")


class TransferDropSerializer(serializers.Serializer):
    code = serializers.ChoiceField(choices=TRANSFER_DROP_CODES)
    count = serializers.IntegerField(required=False)


class TransferSerializer(serializers.Serializer):
    coverage = serializers.ChoiceField(choices=("full", "partial"))
    drops = TransferDropSerializer(many=True)


class ExistingCampaignSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    status = serializers.CharField()
    source = serializers.CharField(allow_blank=True)


class DMCampaignCandidateSerializer(serializers.ModelSerializer):
    """후보 1건 (검수 UI). evidence_raw 는 7일 후 파기되면 null 로 직렬화된다."""

    job_id = serializers.UUIDField(read_only=True)
    applied_campaign_id = serializers.UUIDField(read_only=True, allow_null=True)
    offer = serializers.SerializerMethodField()
    support = serializers.SerializerMethodField()
    transfer = serializers.SerializerMethodField()
    existing_campaign = serializers.SerializerMethodField()

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
            "offer",
            "support",
            "transfer",
            "existing_campaign",
            "confirm_required",
            "confirmed_at",
            "gate_detected",
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

    # 목록(view=list)에서는 큰 필드를 빼 응답을 줄인다.
    _HEAVY = ("evidence_raw", "evidence_aggregates", "follow_up_candidates", "matched_template")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if self.context.get("light"):
            for k in self._HEAVY:
                data.pop(k, None)
        return data

    @extend_schema_field(OfferSerializer)
    def get_offer(self, obj) -> dict:
        """인플루언서가 보내려던 **자료 링크** — 이 기능의 산출물 1순위."""
        return {
            "url": obj.confirmed_url or obj.offer_url,
            "button_label": obj.offer_button_label,
            "confirmed": bool(obj.confirmed_at),
            "edited": bool(obj.confirmed_url and obj.confirmed_url != obj.offer_url),
        }

    @extend_schema_field(SupportSerializer)
    def get_support(self, obj) -> dict:
        """근거의 강도. 같은 게시물 댓글러 여러 명이 **같은 DM 을 받았을수록** 확실하다.

        1~2명이면 다른 게시물 캠페인에서 흘러든 것일 확률이 높다(실측 86%).
        """
        return {
            "hits": obj.support_hits,
            "probed": obj.support_probed,
            "score": obj.support_score,
        }

    @extend_schema_field(TransferSerializer)
    def get_transfer(self, obj) -> dict:
        """못 옮긴 것. 실제로 채워지는 코드는 ``TRANSFER_DROP_CODES`` 5종뿐이다."""
        drops = obj.transfer_drops or []
        return {"coverage": "partial" if drops else "full", "drops": drops}

    @extend_schema_field(ExistingCampaignSerializer)
    def get_existing_campaign(self, obj) -> dict | None:
        """같은 게시물에 이미 우리 캠페인이 있으면 알려준다(중복 생성 방지).

        분석 단계에서 이런 게시물은 아예 제외하지만, 분석 후에 사용자가 캠페인을 만들었을 수
        있으므로 응답 시점에 한 번 더 확인한다.
        """
        if not obj.media_id:
            return None
        from .models import AutoDMCampaign

        c = (
            AutoDMCampaign.objects.filter(
                ig_connection_id=obj.ig_connection_id, media_id=obj.media_id
            )
            .exclude(id=obj.applied_campaign_id)
            .order_by("-created_at")
            .first()
        )
        if not c:
            return None
        return {"id": str(c.id), "name": c.name, "status": c.status, "source": c.source}


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
    link_button_url = serializers.URLField(
        required=False,
        allow_blank=True,
        max_length=1000,
        help_text="첫 DM 에 붙일 링크 버튼 주소. 미지정이면 확정/복원된 오퍼 링크를 쓴다.",
    )
    link_button_label = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=20,
        help_text="링크 버튼 문구(20자). 미지정이면 복원된 버튼 문구 또는 '자료 받기'.",
    )
    link_buttons = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        help_text='[{"url":"https://...","label":"자료 받기"}] 최대 3개. 지정하면 위 두 필드보다 우선.',
    )
    follow_gate_enabled = serializers.BooleanField(
        required=False,
        help_text="미지정이면 원본에서 팔로우 확인 단계가 감지된 경우 자동으로 켠다.",
    )
    media_id = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=255,
        help_text="template_only 후보를 특정 게시물에 붙일 때 필수.",
    )


class PaginatedCandidateSerializer(serializers.Serializer):
    """``candidates/`` 응답은 **봉투**다 (스키마가 순수 배열로 잘못 선언돼 있었다).

    ⚠️ 캠페인 목록(`auto-dm-campaigns`)과 반대다 — 그쪽은 `page` 를 줘야 봉투가 되고,
    여기는 **항상** 봉투다.
    """

    count = serializers.IntegerField(help_text="필터에 걸린 전체 후보 수.")
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = DMCampaignCandidateSerializer(many=True)


class CandidateSummarySerializer(serializers.Serializer):
    """``candidates/summary/`` 응답 — 필터 칩 개수 + 날짜 범위."""

    total = serializers.IntegerField()
    by_band = serializers.DictField(child=serializers.IntegerField())
    by_status = serializers.DictField(child=serializers.IntegerField())
    needs_confirm = serializers.IntegerField()
    with_offer_url = serializers.IntegerField()
    media_date_range = serializers.DictField(child=serializers.CharField(allow_null=True))


class BulkApplyRequestSerializer(serializers.Serializer):
    """apply-all 요청 — 어느 밴드를 한 번에 적용할지."""

    bands = serializers.ListField(
        child=serializers.ChoiceField(choices=["auto_draft", "needs_review"]),
        required=False,
        allow_empty=False,
        help_text=(
            "기본 ['auto_draft'] — 근거가 충분한 후보만. 'needs_review' 를 넣으면 "
            "링크 확인이 필요한 후보까지 함께 만든다(사용자가 나중에 링크를 고칠 수 있다). "
            "'template_only' 는 게시물을 특정할 수 없어 여기서 못 쓴다."
        ),
    )


class CandidateConfirmRequestSerializer(serializers.Serializer):
    """후보의 **자료 링크 확인/수정** 요청.

    표본이 적은 후보는 복원한 링크가 다른 캠페인 것일 수 있어 인플루언서 확인을 받는다.
    화면에는 링크 하나만 보여주고 "이 링크가 맞나요?" 만 물으면 된다.
    """

    url = serializers.URLField(
        required=False,
        allow_blank=True,  # ← 스키마상 format:uri 지만 **빈 문자열은 허용**된다(아래 참조)
        max_length=1000,
        help_text=(
            "맞으면 생략(복원된 링크를 그대로 확정). 다르면 올바른 링크를 보내면 교체된다.\n"
            "**빈 문자열 `\"\"` 은 유효한 값**이며 '링크 없음' 으로 확정한다 — "
            "`format: uri` 와 모순돼 보이지만 `allow_blank` 라 서버는 받는다. "
            "링크를 지우는 의도가 아니라면 아예 **필드를 빼고** 보내는 쪽이 안전하다."
        ),
    )
    correct = serializers.BooleanField(
        required=False,
        default=True,
        help_text="false 면 이 후보를 무시(dismiss)한다 — 캠페인이 아니었다는 뜻.",
    )
