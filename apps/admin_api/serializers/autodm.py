"""apps/admin_api/serializers/autodm.py — 자동 DM 모니터링(도메인 F) 시리얼라이저.

``/api/v1/admin/auto-dm/`` 및 관련 백오피스 엔드포인트에서 사용하는 cross-workspace
(전역) 읽기 전용 시리얼라이저 모음. 모든 접근은 ``IsAdminUser``(is_staff=True) 권한으로만
허용되며, 워크스페이스 경계를 넘어 전체 캠페인/DM 로그/IG 연동을 조회한다.

보안:
- IG ``access_token`` 등 비밀값은 절대 직렬화하지 않는다. 토큰은 상태/만료/마지막 검증 시각만 노출.

원본 도메인 모델: ``apps.integrations.models`` (IGAccountConnection / AutoDMCampaign / SentDMLog).
캠페인/계정 통계는 ``apps.integrations.serializers.DMVerificationStatsSerializer`` 와 동일한
집계 dict 형태(키 셋)를 따른다.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.admin_api.dm_error_catalog import describe_for_log
from apps.admin_api.dm_policy_rollup import followup_not_sent, opening_not_sent
from apps.integrations.campaign_stats import (
    build_dm_stats,
    compute_campaign_enrichment,
    people_from_annotations,
)
from apps.integrations.dm_status_groups import GROUP_DISPLAY
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.services import is_instagram_permalink

# ===== 공용 mini 시리얼라이저 =====


class _OwnerSerializer(serializers.Serializer):
    """워크스페이스 소유자(User) 요약 — id/email 만 노출."""

    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)

    class Meta:
        # 다른 admin 도메인의 동명 _OwnerSerializer 와 OpenAPI 컴포넌트 충돌 방지.
        ref_name = "AdminAutoDMOwner"


class _WorkspaceMiniSerializer(serializers.Serializer):
    """워크스페이스 요약 — id/name."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)

    class Meta:
        ref_name = "AdminAutoDMWorkspaceMini"


class _CampaignMiniSerializer(serializers.Serializer):
    """캠페인 요약 — id/name (DM 로그 목록의 nested 표시용)."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)

    class Meta:
        ref_name = "AdminAutoDMCampaignMini"


# ===== 통계 집계 헬퍼 =====


def _build_stats(queryset) -> dict:
    """``SentDMLog`` 쿼리셋 → ``DMVerificationStatsSerializer`` 형태의 통계 dict.

    DM-1-a: 어드민 전용 사본을 폐기하고 **유저 콘솔과 같은 함수**
    (:func:`apps.integrations.campaign_stats.build_dm_stats`) 를 호출한다.
    사본 시절에는 이벤트 단위 필드만 있고 ``unique_*``(사람 단위)가 통째로 빠져 있어서
    같은 캠페인의 숫자가 어드민/유저 화면에서 달랐다. 필드를 여기 복사하지 말 것.

    DM-7: 여기에만 ``not_sent``(사람 단위 policy 분해)를 얹는다 — `policy`(확인해야함/정상)는
    **운영자 어휘**라 유저 콘솔 응답에 실리면 안 되므로 공용 함수가 아니라 이 어댑터에서 붙인다.
    """
    stats = build_dm_stats(queryset)
    stats["not_sent"] = opening_not_sent(queryset)
    # 후속 축은 build_dm_stats 가 만든 follow_up 블록 **안**에 같은 모양으로 넣는다.
    if isinstance(stats.get("follow_up"), dict):
        stats["follow_up"]["not_sent"] = followup_not_sent(queryset)
    return stats


# ===== 캠페인 =====


class AdminCampaignPeopleSerializer(serializers.Serializer):
    """캠페인 1건의 **사람(인원) 단위** 요약 (DM-1-b).

    상세(`stats`)의 ``unique_*`` 와 **같은 정의·같은 계산**(campaign_stats) — 목록의
    ``people.sent`` 와 상세의 ``unique_sent`` 는 항상 일치한다.
    ``targets == sent + waiting + failed`` 항등이 성립한다.
    """

    targets = serializers.IntegerField(help_text="전체 대상 인원 (unique_targets — 루트 DM 기준)")
    sent = serializers.IntegerField(help_text="실제 발송된 인원 (unique_sent — Meta 접수 이상)")
    waiting = serializers.IntegerField(help_text="발송 대기/발송 중 인원 (unique_waiting)")
    failed = serializers.IntegerField(help_text="아무것도 받지 못한 인원 (unique_failed)")
    unconfirmed = serializers.IntegerField(
        help_text="발송됐으나 도착 미확인 인원 (unique_unconfirmed)"
    )
    hidden_spam = serializers.IntegerField(
        help_text="숨겨진 요청·스팸함으로 간 인원 (unique_hidden_spam — failed 의 부분집합)"
    )
    needs_attention = serializers.IntegerField(
        help_text="숨김함 제외 '확인 필요' 인원 (unique_needs_attention_excl_hidden). "
        "목록의 '발송 실패 인원' 컬럼은 이 값을 쓴다"
    )
    sent_rate = serializers.FloatField(
        help_text="sent / targets (0~1, unique_sent_rate — 헤드라인용). targets 0 이면 0.0"
    )

    class Meta:
        ref_name = "AdminCampaignPeople"


class AdminCampaignListSerializer(serializers.ModelSerializer):
    """캠페인 목록 (cross-workspace) — 운영 모니터링용 요약."""

    ig_username = serializers.CharField(
        source="ig_connection.username",
        read_only=True,
        help_text="이 캠페인이 연결된 IG 계정 username.",
    )
    owner = _OwnerSerializer(
        source="ig_connection.workspace.owner",
        read_only=True,
        help_text="IG 계정이 속한 워크스페이스의 소유자(User).",
    )
    people = serializers.SerializerMethodField(
        help_text=(
            "사람(인원) 단위 요약 — 상세 stats 의 unique_* 와 동일 정의 (DM-1-b). "
            "목록 카드의 '발송 인원'은 people.sent, '발송 실패 인원'은 people.needs_attention. "
            "total_sent/total_failed 는 **발송 이벤트** 수라 follow-gate 캠페인에서 "
            "인원보다 크다(1명 = 오프닝+리워드 2건)."
        )
    )
    delivered_count = serializers.SerializerMethodField(
        help_text="확정 도착 DM 건수 (delivered+read+recovery_delivered) — 이벤트 단위."
    )
    delivery_rate = serializers.SerializerMethodField(
        help_text=(
            "(delivered+read+recovery_delivered) / (accepted 진입 건) — 0~1. "
            "⚠️ 하드실패가 분모에서 빠지므로 100% 로 부풀 수 있다. 헤드라인에는 "
            "people.sent_rate 를 쓸 것."
        )
    )
    last_sent_at = serializers.SerializerMethodField(
        help_text="이 캠페인의 마지막 DM 로그 생성 시각 (로그 0건이면 null)."
    )

    class Meta:
        model = AutoDMCampaign
        fields = [
            "id",
            "name",
            "ig_username",
            "owner",
            "status",
            "trigger_type",
            "people",
            "delivered_count",
            "delivery_rate",
            "last_sent_at",
            # 하위호환(폴백용) — 발송 **이벤트** 단위 비정규화 카운터. 제거 예정 없음.
            "total_sent",
            "total_failed",
            "total_unconfirmed",
            "created_at",
            "started_at",
        ]
        read_only_fields = fields

    @extend_schema_field(AdminCampaignPeopleSerializer)
    def get_people(self, obj: AutoDMCampaign) -> dict | None:
        return people_from_annotations(obj)

    def _enrichment(self, obj: AutoDMCampaign) -> dict:
        # annotate 된 값을 재사용 (추가 쿼리 0) — 미annotate 인스턴스는 즉석 집계 폴백.
        cached = getattr(obj, "_admin_enrichment", None)
        if cached is None:
            cached = compute_campaign_enrichment(obj)
            obj._admin_enrichment = cached
        return cached

    def get_delivered_count(self, obj: AutoDMCampaign) -> int:
        return self._enrichment(obj)["delivered_count"]

    def get_delivery_rate(self, obj: AutoDMCampaign) -> float:
        return self._enrichment(obj)["delivery_rate"]

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_last_sent_at(self, obj: AutoDMCampaign):
        return self._enrichment(obj)["last_sent_at"]


class AdminCampaignDetailSerializer(serializers.ModelSerializer):
    """캠페인 상세 (cross-workspace) — 전체 설정 + 누적 발송 통계."""

    ig_connection_id = serializers.UUIDField(
        source="ig_connection.id",
        read_only=True,
        help_text="연결된 IGAccountConnection PK.",
    )
    ig_username = serializers.CharField(source="ig_connection.username", read_only=True)
    owner = _OwnerSerializer(source="ig_connection.workspace.owner", read_only=True)
    media_permalink = serializers.SerializerMethodField(
        help_text=(
            "인스타그램 게시물 영구 링크 — media_url 이 instagram.com permalink 면 그 값, "
            "아니면 빈 문자열. specific_media 캠페인은 생성 시 permalink 를 백필하지만(best-effort), "
            "레거시/조회 실패 캠페인은 빈 값일 수 있음(프론트는 이때 media_id 만 표시). "
            "media_url 은 CDN 이미지 URL 등 permalink 가 아닐 수 있어 이 필드로 분리."
        )
    )
    stats = serializers.SerializerMethodField(
        help_text=(
            "이 캠페인의 dm_logs 를 DMVerificationStatsSerializer 와 동일한 형태로 집계한 dict "
            "(delivery_rate/read_rate/gate_passthrough_rate 포함). "
            "어드민 전용으로 두 블록이 더 붙는다 (DM-7):\n"
            "- `not_sent` — 오프닝 축 '발송 안 됨' 인원의 분류·사유 분해\n"
            "- `follow_up.not_sent` — 후속 DM 축 같은 것\n"
            "각 블록: `{total, investigate, normal, breakdown[{reason, policy, policy_display, "
            "title, people}]}`. 계약: `investigate + normal == total == Σ breakdown[].people`, "
            "breakdown 은 🔴 먼저·인원 많은 순 정렬. `reason` 은 문구가 바뀌어도 고정인 머신 키로, "
            "그대로 `GET /admin/auto-dm/recipients/?dm_axis=<축>&error_reason=<reason>` 에 실으면 "
            "그 인원이 목록에 뜬다 (DM-13/14)."
        )
    )

    class Meta:
        model = AutoDMCampaign
        fields = [
            "id",
            "name",
            "description",
            "ig_connection_id",
            "ig_username",
            "owner",
            "status",
            "trigger_type",
            "media_id",
            "media_url",
            "media_permalink",
            "keyword_filter",
            "keyword_mode",
            "message_template",
            "opening_message_template",
            "public_reply_enabled",
            "public_reply_template",
            "public_reply_templates",
            "public_reply_batch_size",
            "public_reply_batch_pause_seconds",
            "public_reply_limit",
            "public_reply_posted_count",
            "follow_gate_enabled",
            "gate_verify_follow",
            "follow_gate_prompt",
            "follow_gate_button_label",
            "follow_gate_retry_message",
            "reward_message_template",
            "gate_trigger_keywords",
            "link_button_url",
            "link_button_label",
            "link_buttons",
            "total_sent",
            "total_failed",
            "total_unconfirmed",
            "created_at",
            "updated_at",
            "started_at",
            "ended_at",
            "stats",
        ]
        read_only_fields = fields

    def get_stats(self, obj: AutoDMCampaign) -> dict:
        return _build_stats(obj.dm_logs.all())

    def get_media_permalink(self, obj: AutoDMCampaign) -> str:
        url = obj.media_url or ""
        return url if is_instagram_permalink(url) else ""


# ===== DM 로그 =====


class AdminDMLogListSerializer(serializers.ModelSerializer):
    """DM 발송 로그 목록 (cross-workspace) — 요약."""

    campaign = _CampaignMiniSerializer(read_only=True, help_text="이 로그가 속한 캠페인 (id/name).")
    flow_role = serializers.SerializerMethodField(
        help_text=(
            "플로우 내 역할 (표시용) — opening/retry/reward/standalone. "
            "재안내(retry)는 quick_reply 재첨부를 위해 dm_kind=opening 으로 저장되므로 "
            "dm_kind 만으로는 오프닝과 구분되지 않는다(parent_log 유무로 판정). "
            "수신자 타임라인에서 '왜 이 사람에게 여러 건이 갔나'를 라벨링하는 데 사용."
        )
    )

    error_policy = serializers.SerializerMethodField(
        help_text=(
            "**분류 (2026-07-31)** — `investigate`(🔴 조사 필요) | `normal`(⚪ 자동 처리). "
            "행 색·필터를 이 값 하나로 그린다. 오류가 아닌 행(delivered/read 등)은 normal. "
            "`?error_policy=` 로 서버 필터 가능(상한 없음)."
        )
    )
    error_reason = serializers.SerializerMethodField(
        help_text=(
            "DM-14 — 사유 머신 키(예: `window_after_close`). 문구가 바뀌어도 고정이라 "
            "'이 행과 같은 사유 보기'를 `?error_reason=` 로 그대로 만들 수 있다. "
            "운영 대시보드 `failure_breakdown[].reason` · `skipped_breakdown[].reason` 과 "
            "같은 네임스페이스. 오류·건너뜀이 아닌 행은 빈 문자열."
        )
    )
    error_title = serializers.SerializerMethodField(
        help_text=(
            "DM-2 — 오류의 짧은 한국어 라벨 (예: '숨겨진 요청 · 스팸함 유입'). 서버 사전"
            "(dm_error_catalog)이 (code, subcode, status) 로 판정한다. 오류가 아니거나 "
            "사전에 없으면 빈 문자열 → 프론트 로컬 사전 폴백. "
            "원인·조치 전문은 목록에 자리가 없어 상세 응답에만 있다."
        )
    )

    class Meta:
        model = SentDMLog
        fields = [
            "id",
            "campaign",
            # 수신자 행(recipients 목록)과 로그를 잇는 키 — recipients → 이 필드로 exact 필터.
            "recipient_user_id",
            "recipient_username",
            "status",
            "dm_kind",
            "flow_role",
            "gate_status",
            "error_code",
            # DM-2 — (code, subcode) 정확 일치가 사전 판정 1순위라 subcode 가 필요하다
            # (100/2534025 숨김함=복구 대상 vs 100/2534022 윈도우 만료=정상 실패는 조치가 정반대).
            "error_subcode",
            "error_title",
            "error_reason",
            "error_policy",
            "created_at",
            "delivered_at",
        ]
        read_only_fields = fields

    def _described(self, obj: SentDMLog) -> dict:
        cached = getattr(obj, "_described_error", None)
        if cached is None:
            # error_message 를 함께 넘긴다 — 건너뜀(skipped)은 사유 컬럼이 없어 원문으로
            # 사유가 갈리기 때문(dm_error_catalog.classify).
            cached = describe_for_log(
                obj.error_code, obj.error_subcode, obj.status, obj.error_message
            )
            obj._described_error = cached
        return cached

    def get_error_reason(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_reason"]

    def get_error_title(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_title"]

    def get_error_policy(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_policy"]

    @extend_schema_field(
        serializers.ChoiceField(choices=["opening", "retry", "reward", "standalone"])
    )
    def get_flow_role(self, obj: SentDMLog) -> str:
        # 단일 소스: apps.integrations.serializers.SentDMLogSerializer.get_flow_role 와 동일 판정.
        if obj.dm_kind == SentDMLog.DMKind.REWARD:
            return "reward"
        if obj.dm_kind == SentDMLog.DMKind.OPENING:
            return "retry" if obj.parent_log_id is not None else "opening"
        return "standalone"


class AdminDMLogDetailSerializer(serializers.ModelSerializer):
    """DM 발송 로그 상세 (cross-workspace) — 디버깅/검증 이력 + 오류 원인·조치 (DM-2).

    운영 대시보드 `failure_breakdown` 과 **같은 서버 사전**(dm_error_catalog)을 쓰므로,
    로그 1건을 열었을 때 대시보드로 돌아가 코드를 대조할 필요가 없다. 새 코드가 나와도
    사전이 서버에 있어 프론트 배포가 필요 없다.
    필드명에 ``error_`` 접두를 둔 이유 — 상세는 평면 객체라 다른 필드와 섞이기 때문
    (대시보드의 title/cause/action 은 breakdown 행 안이라 접두가 없다).
    """

    campaign = _CampaignMiniSerializer(read_only=True)
    error_title = serializers.SerializerMethodField(
        help_text="짧은 한국어 라벨 (예: '토큰 만료 · 무효'). 사전에 없으면 빈 문자열."
    )
    error_cause = serializers.SerializerMethodField(
        help_text="왜 발생했는가 (1~2문장 한국어). 없으면 빈 문자열."
    )
    error_action = serializers.SerializerMethodField(
        help_text="운영자가 무엇을 해야 하는가. 없으면 빈 문자열."
    )
    recoverable = serializers.SerializerMethodField(
        help_text=(
            "복구/재검증 경로가 있는 실패인가 — 재발송/재검증 버튼 노출 판단용. "
            "failed_no_trace(재검증)·recovery_*·failed_param@2534025(숨김채널 복구)=true."
        )
    )
    error_policy = serializers.SerializerMethodField(
        help_text=(
            "**분류 (2026-07-31)** — `investigate`(🔴 조사 필요) | `normal`(⚪ 자동 처리). "
            "운영 대시보드 failure_breakdown.policy 와 같은 어휘·같은 사전이다. "
            "오류가 아닌 상태(delivered/read 등)는 normal."
        )
    )
    error_policy_display = serializers.SerializerMethodField(
        help_text="분류 한국어 표시명 ('조사 필요' / '자동 처리')."
    )
    error_reason = serializers.SerializerMethodField(
        help_text=(
            "DM-14 — 사유 머신 키(예: `window_after_close`). `?error_reason=` 로 "
            "'같은 사유 전체 보기'를 만들 때 쓴다. 오류·건너뜀이 아니면 빈 문자열."
        )
    )

    class Meta:
        model = SentDMLog
        fields = [
            "id",
            "campaign",
            "recipient_username",
            "status",
            "dm_kind",
            "gate_status",
            "error_code",
            # DM-2 — 사전 판정 1순위가 (code, subcode) 정확 일치라 subcode 없이는
            # 프론트에서 사전을 붙일 수도 없었다 (모델엔 원래 있던 값).
            "error_subcode",
            "error_title",
            "error_cause",
            "error_action",
            "error_reason",
            "error_policy",
            "error_policy_display",
            "recoverable",
            "created_at",
            "delivered_at",
            # 상세 전용
            "comment_id",
            "comment_text",
            "message_sent",
            "error_message",
            "verified_via",
            "retry_count",
            "verification_log",
        ]
        read_only_fields = fields

    def _described(self, obj: SentDMLog) -> dict:
        cached = getattr(obj, "_described_error", None)
        if cached is None:
            # error_message 를 함께 넘긴다 — 건너뜀(skipped)은 사유 컬럼이 없어 원문으로
            # 사유가 갈리기 때문(dm_error_catalog.classify).
            cached = describe_for_log(
                obj.error_code, obj.error_subcode, obj.status, obj.error_message
            )
            obj._described_error = cached
        return cached

    def get_error_reason(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_reason"]

    def get_error_title(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_title"]

    def get_error_cause(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_cause"]

    def get_error_action(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_action"]

    def get_error_policy(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_policy"]

    def get_error_policy_display(self, obj: SentDMLog) -> str:
        return self._described(obj)["error_policy_display"]

    def get_recoverable(self, obj: SentDMLog) -> bool:
        return self._described(obj)["recoverable"]


# ===== DM 수신자(사람) 단위 롤업 =====


class AdminDMRecipientSerializer(serializers.Serializer):
    """수신자(사람) 1명 단위 롤업 — (campaign, recipient_user_id) 그룹당 1행.

    사용자용 ``DMRecipientRollupSerializer`` (integrations) 와 **동일한 status_group 판정**을
    따르되, 어드민 교차-워크스페이스 식별용 nested(campaign/ig/owner)와 오프닝/상호작용
    카운트를 추가한다. 개별 발송 이벤트는 ``GET /admin/auto-dm/logs/?recipient_user_id=`` 로
    펼친다(타임라인).

    행 키는 ``recipient_user_id`` 우선, 비어 있으면(스토리 답장 초기 등) 표시상
    ``recipient_username`` 폴백 — 사용자용 프론트의 ``recipient_user_id || recipient_username``
    폴백과 동일하다.
    """

    recipient_user_id = serializers.CharField(
        allow_blank=True,
        help_text="수신자 Instagram ID (묶음 키). 비어 있을 수 있음(스토리 답장 초기).",
    )
    recipient_username = serializers.CharField(
        allow_blank=True,
        help_text="수신자 username (최신값 best-effort, 미해석 시 user_{IGSID} 폴백).",
    )
    campaign = _CampaignMiniSerializer(help_text="이 수신자가 속한 캠페인 (id/name).")
    ig_connection_id = serializers.UUIDField(help_text="캠페인이 연결된 IGAccountConnection PK.")
    ig_username = serializers.CharField(allow_blank=True, help_text="IG 계정 username.")
    owner = _OwnerSerializer(help_text="캠페인이 속한 워크스페이스 소유자(User).")
    status_group = serializers.ChoiceField(
        choices=list(GROUP_DISPLAY.keys()),
        help_text=(
            "이 수신자의 코스 상태 그룹 — 사용자용 recipients 와 동일 어휘·판정 "
            "(waiting/sent/read/hidden_spam/attention). ?status_group= 로 서버 필터 가능."
        ),
    )
    status_group_display = serializers.CharField(
        help_text="status_group 한국어 표시명 (대기중/전송됨/읽음/숨겨진 요청 · 스팸/확인 필요)."
    )
    sent = serializers.BooleanField(help_text="DM 이 실제 발송됨 (Meta 접수 이상)")
    delivered = serializers.BooleanField(help_text="도착 확인됨 (delivered/read)")
    read = serializers.BooleanField(help_text="읽음 확인됨")
    needs_attention = serializers.BooleanField(
        help_text=(
            "조치 필요(status_group == attention)인가. success-aware — 발송/도착/읽음/복구 성공이 "
            "하나라도 있으면 false. 숨겨진 요청·스팸은 별도 그룹이므로 여기서 false."
        )
    )
    dm_count = serializers.IntegerField(
        help_text="이 사람에게 나간 총 DM 이벤트 수 (오프닝+상호작용+재시도)"
    )
    opening_count = serializers.IntegerField(
        help_text="오프닝 DM 수 (dm_kind ∈ {opening, standalone} — 트리거로 나간 첫 DM)."
    )
    interaction_count = serializers.IntegerField(
        help_text="상호작용 DM 수 (dm_kind = reward — 오프닝 이후 같은 DM창 후속 발송)."
    )
    latest_status = serializers.CharField(
        allow_blank=True, help_text="가장 최근 발송의 SentDMLog.status (배지 보조용)."
    )
    latest_followup_status = serializers.CharField(
        allow_blank=True,
        help_text=(
            "DM-8 — **마지막 후속 DM**(dm_kind=reward)의 status. 후속을 받은 적이 없으면 빈 문자열. "
            "캠페인 상세의 후속 DM 표(stats.follow_up)와 **같은 규칙**(사람별 마지막 1건)이라 "
            "표의 인원과 목록 행이 어긋나지 않는다. `latest_status`(전체 로그 기준)로는 "
            "오프닝이 최신인 사람의 후속 상태를 알 수 없다."
        ),
    )
    error_title = serializers.CharField(
        allow_blank=True,
        help_text=(
            "이 사람의 오류 사유 라벨 (서버 사전 dm_error_catalog 판정). "
            "**DM-16 (2026-07-31) — 기준을 '가장 최근 로그' → '가장 최근 실패·정체 로그'로 "
            "바꿨다.** `error_policy` 와 근거 로그가 같아졌으므로, `?error_policy=investigate` "
            "로 필터한 목록에 사유가 빈 행이 섞이지 않는다(과거 실패 후 결국 성공한 사람). "
            "실패 이력이 아예 없으면 `error_policy` 와 함께 빈 문자열이다. "
            "원인·조치 전문은 `GET /admin/auto-dm/logs/{id}/` 상세에 있다."
        ),
    )
    error_reason = serializers.CharField(
        allow_blank=True,
        help_text=(
            "DM-14 — 이 사람의 사유 머신 키. `error_title` 과 **같은 로그** 기준이며 "
            "`stats.not_sent.breakdown[].reason` 과 같은 값이라, 카드의 사유 칩을 눌러 "
            "`?error_reason=` 로 드릴다운했을 때 이 열이 전부 그 사유다. 없으면 빈 문자열."
        ),
    )
    error_policy = serializers.CharField(
        allow_blank=True,
        help_text=(
            "DM-8 — 이 사람의 분류: `investigate`(🔴 조사 필요) | `normal`(⚪ 자동 처리) | "
            '`""`(실패·정체 로그가 없어 분류할 사유 자체가 없음). '
            "판정은 **가장 최근 실패·정체 로그** 1건 기준으로, 캠페인 상세의 "
            "`stats.not_sent` 분해와 **같은 규칙**이다(카드 인원 = 필터 결과). "
            "`?dm_axis=` 를 주면 그 축의 대표 로그 기준으로 바뀐다(DM-13). "
            "`?error_policy=` 로 서버 필터 가능 — **상한 없음**(DM-15)."
        ),
    )
    first_sent_at = serializers.DateTimeField(
        allow_null=True, help_text="이 수신자의 첫 DM 로그 생성(발송 시작) 시각."
    )
    last_activity_at = serializers.DateTimeField(allow_null=True, help_text="마지막 활동 시각.")

    class Meta:
        ref_name = "AdminDMRecipient"


# ===== IG 연동 =====


class AdminIGConnectionListSerializer(serializers.ModelSerializer):
    """IG 계정 연동 목록 (cross-workspace).

    보안: ``access_token`` 등 비밀값은 절대 노출하지 않는다. 토큰은 상태/만료/마지막
    검증 시각만 제공한다.
    """

    workspace = _WorkspaceMiniSerializer(
        read_only=True, help_text="이 IG 계정이 속한 워크스페이스 (id/name)."
    )
    owner = _OwnerSerializer(
        source="workspace.owner",
        read_only=True,
        help_text="워크스페이스 소유자(User).",
    )
    campaigns_count = serializers.SerializerMethodField(
        help_text="이 계정에 연결된 자동 DM 캠페인 총 수 (dm_campaigns)."
    )
    recent_delivery_rate_24h = serializers.SerializerMethodField(
        help_text=(
            "최근 24시간 ACCEPTED 진입 건 중 DELIVERED+READ 비율 (0~1). " "집계 대상 없으면 null."
        )
    )

    class Meta:
        model = IGAccountConnection
        fields = [
            "id",
            "username",
            "workspace",
            "owner",
            "status",
            "token_expires_at",
            "last_verified_at",
            "error_message",
            "campaigns_count",
            "recent_delivery_rate_24h",
        ]
        read_only_fields = fields

    def get_campaigns_count(self, obj: IGAccountConnection) -> int:
        return obj.dm_campaigns.count()

    def get_recent_delivery_rate_24h(self, obj: IGAccountConnection) -> float | None:
        since = timezone.now() - timedelta(hours=24)
        agg = SentDMLog.objects.filter(
            campaign__ig_connection=obj, created_at__gte=since
        ).aggregate(
            accepted=Count("id", filter=Q(status="accepted")),
            delivered=Count("id", filter=Q(status="delivered")),
            read=Count("id", filter=Q(status="read")),
            no_trace=Count("id", filter=Q(status="failed_no_trace")),
        )
        denom = agg["accepted"] + agg["delivered"] + agg["read"] + agg["no_trace"]
        num = agg["delivered"] + agg["read"]
        return round(num / denom, 4) if denom else None
