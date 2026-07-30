"""인스타 성장 리포트 시리얼라이저.

⚠️ `pdf_file.url`(공개 R2 URL) 을 절대 직렬화하지 않는다. 리포트에는 팔로워 댓글 원문이
   들어가므로 다운로드는 인증 엔드포인트(`GET /insta-reports/{id}/download/`)로만 제공한다.
"""

from __future__ import annotations

from rest_framework import serializers

from . import progress
from .models import InstagramReport


class ReportQuotaSerializer(serializers.Serializer):
    """이용 횟수 요약 (계정당 월 N회 · 연동 계정 수만큼 늘어남)."""

    per_account_limit = serializers.IntegerField(
        help_text="IG 계정 1개당 이번 달 생성 가능 횟수. -1 = 무제한(관리자)"
    )
    total_limit = serializers.IntegerField(
        help_text="연동된 활성 계정 수 × per_account_limit. -1 = 무제한"
    )
    total_used = serializers.IntegerField(help_text="이번 달 사용 횟수 합계(실패 건 제외)")
    total_remaining = serializers.IntegerField(help_text="남은 횟수 합계. -1 = 무제한")
    period_end = serializers.DateTimeField(
        help_text="이용 횟수가 초기화되는 시각(다음 달 1일 00:00 KST)"
    )


class ReportTargetSerializer(serializers.Serializer):
    """분석 팝업에 뿌릴 IG 계정 1개."""

    connection_id = serializers.UUIDField(help_text="POST 시 `connection_id` 로 그대로 전달")
    username = serializers.CharField(help_text="IG username (@ 없음)")
    name = serializers.CharField(help_text="IG 표시명 (예: 이지용 | 릴스 드래곤)")
    profile_picture_url = serializers.CharField(
        allow_blank=True, help_text="우리 스토리지에 캐싱된 안정 URL (없으면 빈 문자열)"
    )
    followers_count = serializers.IntegerField(
        allow_null=True, help_text="팔로워 수 (미확인 시 null)"
    )
    media_count = serializers.IntegerField(allow_null=True, help_text="게시물 수 (미확인 시 null)")
    display_line = serializers.CharField(
        help_text="그대로 렌더 가능한 한 줄 요약. 예: `@reels_drgn · 팔로워 98,293 · 게시물 672개`"
    )
    is_active = serializers.BooleanField(help_text="소프트 활성 여부. 비활성 계정은 생성 불가")
    can_generate = serializers.BooleanField(help_text="지금 분석 버튼을 누를 수 있는지")
    reason = serializers.CharField(
        allow_null=True,
        help_text=(
            "can_generate=false 사유 코드. "
            "`PLAN_REQUIRED`(프로 아님) · `QUOTA_EXCEEDED`(이번 달 사용) · "
            "`ALREADY_RUNNING`(생성 중) · `CONNECTION_INACTIVE` · `TOKEN_EXPIRED`"
        ),
    )
    reason_message = serializers.CharField(
        allow_blank=True, help_text="사유를 그대로 노출해도 되는 한국어 안내 문구"
    )
    used = serializers.IntegerField(help_text="이 계정이 이번 달 사용한 횟수")
    limit = serializers.IntegerField(help_text="이 계정의 이번 달 한도. -1 = 무제한")
    remaining = serializers.IntegerField(help_text="이 계정의 남은 횟수. -1 = 무제한")
    next_available_at = serializers.DateTimeField(
        allow_null=True, help_text="QUOTA_EXCEEDED 일 때 다시 가능한 시각(다음 달 1일 KST)"
    )
    running_report_id = serializers.UUIDField(
        allow_null=True, help_text="생성 중인 리포트가 있으면 그 id (폴링에 바로 사용)"
    )
    last_report = serializers.DictField(
        allow_null=True,
        help_text="가장 최근 완료 리포트 `{id, created_at, period_from, period_to}` (없으면 null)",
    )


class ReportTargetsResponseSerializer(serializers.Serializer):
    """`GET /insta-reports/targets/` 응답."""

    plan_required = serializers.CharField(help_text="이 기능에 필요한 플랜 이름 (`pro`)")
    has_feature = serializers.BooleanField(help_text="현재 사용자가 기능 보유 중인지")
    estimated_minutes = serializers.IntegerField(
        help_text="평균 생성 소요 시간(분). 안내 문구에 그대로 사용"
    )
    estimated_seconds = serializers.IntegerField(help_text="평균 생성 소요 시간(초)")
    quota = ReportQuotaSerializer()
    accounts = ReportTargetSerializer(many=True)


class ReportCreateSerializer(serializers.Serializer):
    """리포트 생성 요청 본문."""

    connection_id = serializers.UUIDField(
        help_text="분석할 IG 연동 id. `GET /insta-reports/targets/` 의 `connection_id`"
    )


class ReportStepSerializer(serializers.Serializer):
    key = serializers.CharField(help_text="단계 키 (collecting/extracting/...)")
    label = serializers.CharField(help_text="사람이 읽는 단계 이름 (그대로 노출 가능)")
    status = serializers.CharField(help_text="`done` | `active` | `pending` | `failed`")
    detail = serializers.CharField(
        allow_blank=True, help_text="진행 중 세부 메시지 (예: 영상 분석 12/30)"
    )
    progress_start = serializers.IntegerField(help_text="이 단계가 차지하는 진행률 시작(%)")
    progress_end = serializers.IntegerField(help_text="이 단계가 차지하는 진행률 끝(%)")
    expected_seconds = serializers.IntegerField(help_text="이 단계 평균 소요(초)")


class ReportSerializer(serializers.ModelSerializer):
    """리포트 상태 + 결과 (폴링 응답)."""

    stage_label = serializers.SerializerMethodField(help_text="현재 단계의 사람말 라벨")
    steps = serializers.SerializerMethodField(help_text="단계별 진행 상태 배열 (체크리스트 렌더용)")
    eta_seconds = serializers.SerializerMethodField(
        help_text="남은 예상 시간(초). 완료·실패 시 null"
    )
    error_message = serializers.CharField(
        source="error_message_ko",
        read_only=True,
        help_text="실패 시 사용자에게 보여 줄 한국어 문구 (성공 시 빈 문자열)",
    )
    pdf_download_url = serializers.SerializerMethodField(
        help_text="완료 시 PDF 다운로드 경로(인증 필요). 미완료면 null"
    )
    pdf_ready = serializers.SerializerMethodField(help_text="PDF 준비 완료 여부")
    account = serializers.SerializerMethodField(help_text="분석 대상 계정 스냅샷")

    class Meta:
        model = InstagramReport
        fields = [
            "id",
            "status",
            "stage",
            "stage_label",
            "progress",
            "message",
            "steps",
            "eta_seconds",
            "stage_started_at",
            "stage_expected_seconds",
            "account",
            "posts_analyzed",
            "reels_with_views",
            "videos_analyzed",
            "comments_analyzed",
            "period_from",
            "period_to",
            "pdf_ready",
            "pdf_download_url",
            "pdf_bytes",
            "error_code",
            "error_message",
            "created_at",
            "started_at",
            "finished_at",
            "elapsed_seconds",
        ]
        read_only_fields = fields

    def get_stage_label(self, obj) -> str:
        return progress.stage_label(obj.stage)

    def get_steps(self, obj) -> list:
        return progress.steps_payload(obj)

    def get_eta_seconds(self, obj) -> int | None:
        return progress.eta_seconds(obj)

    def get_pdf_ready(self, obj) -> bool:
        return bool(obj.pdf_file)

    def get_pdf_download_url(self, obj) -> str | None:
        if not obj.pdf_file:
            return None
        return f"/api/v1/insta-reports/{obj.id}/download/"

    def get_account(self, obj) -> dict:
        return {
            "connection_id": str(obj.ig_connection_id) if obj.ig_connection_id else None,
            "username": obj.ig_username,
            "name": obj.ig_name,
            "followers_count": obj.followers_snapshot,
            "media_count": obj.media_count_snapshot,
        }


class ReportListItemSerializer(serializers.ModelSerializer):
    """히스토리 목록 행 (가벼운 필드만)."""

    pdf_ready = serializers.SerializerMethodField()
    pdf_download_url = serializers.SerializerMethodField()
    error_message = serializers.CharField(source="error_message_ko", read_only=True)

    class Meta:
        model = InstagramReport
        fields = [
            "id",
            "status",
            "progress",
            "ig_username",
            "ig_name",
            "posts_analyzed",
            "reels_with_views",
            "videos_analyzed",
            "comments_analyzed",
            "period_from",
            "period_to",
            "pdf_ready",
            "pdf_download_url",
            "pdf_bytes",
            "error_code",
            "error_message",
            "created_at",
            "finished_at",
        ]
        read_only_fields = fields

    def get_pdf_ready(self, obj) -> bool:
        return bool(obj.pdf_file)

    def get_pdf_download_url(self, obj) -> str | None:
        if not obj.pdf_file:
            return None
        return f"/api/v1/insta-reports/{obj.id}/download/"
