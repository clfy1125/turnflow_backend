"""인스타 성장 리포트 모델.

- ``InstagramReport``  : 잡 + 결과(PDF/집계). 진행률 폴링의 단일 소스.
- ``IGVideoFeature``   : Gemini 영상 피처 추출 캐시. 같은 계정 재분석 비용을 $0.27 → $0 으로.
- ``ReportAiCache``    : 댓글 분류 등 기타 AI 캐시(키-값).

리포트 산출물은 **자기완결 HTML 1개 파일**(썸네일 data-URI + Chart.js 인라인)이며,
**인증 다운로드 엔드포인트로만** 제공한다. 팔로워 댓글 원문이 들어가므로 공개 R2 URL 을
직렬화에 노출하지 않는다(``html_file.url`` 을 시리얼라이저에 넣지 말 것).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ReportStatus(models.TextChoices):
    QUEUED = "queued", "대기"
    RUNNING = "running", "진행 중"
    SUCCEEDED = "succeeded", "완료"
    FAILED = "failed", "실패"
    CANCELLED = "cancelled", "취소"


class ReportStage(models.TextChoices):
    """진행 단계. 라벨·가중치의 단일 소스는 ``progress.py`` (여기 값과 키가 일치해야 한다)."""

    QUEUED = "queued", "대기 중"
    COLLECTING = "collecting", "게시물 모으는 중"
    METRICS = "metrics", "숫자 계산 중"
    PREPARING = "preparing", "영상 내려받는 중"
    EXTRACTING = "extracting", "영상 분석 중"
    COMMENTS = "comments", "댓글 분석 중"
    SYNTHESIZING = "synthesizing", "인사이트 쓰는 중"
    VERIFYING = "verifying", "검수하는 중"
    RENDERING = "rendering", "리포트 만드는 중"
    EXPORTING = "exporting", "파일로 저장하는 중"
    DONE = "done", "완료"


class ReportErrorCode(models.TextChoices):
    """실패 사유. 프론트가 문구를 분기할 수 있게 코드로 고정한다."""

    VIEWS_UNAVAILABLE = "VIEWS_UNAVAILABLE", "조회수 수집 실패"
    NO_POSTS = "NO_POSTS", "게시물 없음"
    NOT_ENOUGH_REELS = "NOT_ENOUGH_REELS", "분석 가능한 릴스 부족"
    TOKEN_INVALID = "TOKEN_INVALID", "인스타 연결 만료"
    EXTRACT_FAILED = "EXTRACT_FAILED", "영상 분석 실패"
    SYNTH_FAILED = "SYNTH_FAILED", "인사이트 작성 실패"
    RENDER_FAILED = "RENDER_FAILED", "리포트 생성 실패"
    TIMEOUT = "TIMEOUT", "시간 초과"
    INTERNAL = "INTERNAL", "내부 오류"


# 실패 사유별 사용자 안내 문구 (프론트가 그대로 노출해도 되는 사람말).
ERROR_MESSAGES_KO = {
    ReportErrorCode.VIEWS_UNAVAILABLE: (
        "조회수 정보를 가져오지 못했어요. 잠시 후 다시 시도해 주세요. "
        "(이번 시도는 이용 횟수에서 차감되지 않았어요)"
    ),
    ReportErrorCode.NO_POSTS: "분석할 게시물을 찾지 못했어요. 게시물을 올린 뒤 다시 시도해 주세요.",
    ReportErrorCode.NOT_ENOUGH_REELS: (
        "조회수를 확인할 수 있는 릴스가 5개보다 적어 리포트를 만들 수 없어요. "
        "릴스가 더 쌓인 뒤에 다시 시도해 주세요."
    ),
    ReportErrorCode.TOKEN_INVALID: "인스타그램 연결이 만료됐어요. 계정을 다시 연결한 뒤 시도해 주세요.",
    ReportErrorCode.EXTRACT_FAILED: "영상 분석 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
    ReportErrorCode.SYNTH_FAILED: "리포트 문장을 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
    ReportErrorCode.RENDER_FAILED: "리포트를 만드는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.",
    ReportErrorCode.TIMEOUT: "생성 시간이 너무 오래 걸려 중단했어요. 잠시 후 다시 시도해 주세요.",
    ReportErrorCode.INTERNAL: "일시적인 오류로 리포트를 만들지 못했어요. 잠시 후 다시 시도해 주세요.",
}


def report_pdf_path(instance, filename: str) -> str:
    """**삭제 금지 — 0001_initial 이 참조한다.**

    산출물은 2026-08-03 에 PDF → HTML 로 바뀌었고(마이그 0002) 이 함수는 더 쓰이지 않는다.
    그래도 남겨두는 이유: `0001_initial` 은 이미 운영에 적용된 마이그레이션이고 Django 는
    마이그레이션 모듈을 임포트할 때 이 `upload_to` 참조를 해석한다 → 지우면 `migrate`
    (그리고 마이그레이션을 읽는 모든 커맨드)가 AttributeError 로 죽는다.
    """
    now = timezone.now()
    return f"insta_reports/{now:%Y/%m}/{instance.id}.pdf"


def report_html_path(instance, filename: str) -> str:
    """`insta_reports/2026/07/<uuid>.html` — 추측 불가 경로(그래도 노출은 인증 경유)."""
    now = timezone.now()
    return f"insta_reports/{now:%Y/%m}/{instance.id}.html"


class InstagramReport(models.Model):
    """리포트 생성 잡 1건 = 결과 1건.

    상태 전이: queued → running → (succeeded | failed | cancelled)
    진행률(progress/stage/message)은 Celery 태스크가 갱신하고 프론트는 3초 폴링으로 읽는다.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ── 소유·대상 ──
    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="insta_reports",
        verbose_name="워크스페이스",
    )
    ig_connection = models.ForeignKey(
        "integrations.IGAccountConnection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="insta_reports",
        verbose_name="대상 IG 연동",
        help_text="연동이 해제돼도 리포트는 남는다(아래 스냅샷 필드로 표시).",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="insta_reports",
        verbose_name="요청자",
    )

    # ── 요청 시점 계정 스냅샷 ──
    ig_username = models.CharField(
        max_length=255, blank=True, default="", verbose_name="IG username"
    )
    ig_name = models.CharField(max_length=255, blank=True, default="", verbose_name="IG 표시명")
    followers_snapshot = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="팔로워 수"
    )
    media_count_snapshot = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="게시물 수"
    )

    # ── 상태 ──
    status = models.CharField(
        max_length=20,
        choices=ReportStatus.choices,
        default=ReportStatus.QUEUED,
        db_index=True,
        verbose_name="상태",
    )
    stage = models.CharField(
        max_length=20,
        choices=ReportStage.choices,
        default=ReportStage.QUEUED,
        verbose_name="진행 단계",
    )
    progress = models.PositiveSmallIntegerField(default=0, verbose_name="진행률(%)")
    message = models.CharField(max_length=200, blank=True, default="", verbose_name="진행 메시지")
    # 프론트가 폴링 사이를 부드럽게 채울 수 있게(합성 단계는 서버 이벤트가 3~5분간 없다).
    stage_started_at = models.DateTimeField(null=True, blank=True, verbose_name="현재 단계 시작")
    stage_expected_seconds = models.PositiveIntegerField(
        default=0, verbose_name="현재 단계 예상 소요(초)"
    )

    # ── 결과 ──
    metrics_json = models.JSONField(null=True, blank=True, verbose_name="지표(S2)")
    aggregates_json = models.JSONField(null=True, blank=True, verbose_name="집계(S5)")
    slots_json = models.JSONField(null=True, blank=True, verbose_name="서술 슬롯(S6·S7)")
    gate_meta = models.JSONField(default=dict, blank=True, verbose_name="검증 게이트 로그")
    html_file = models.FileField(
        upload_to=report_html_path,
        blank=True,
        null=True,
        max_length=500,
        verbose_name="리포트 HTML",
        help_text="자기완결 HTML(썸네일 data-URI + Chart.js 인라인) — 인증 다운로드로만 제공",
    )
    html_bytes = models.PositiveIntegerField(default=0, verbose_name="HTML 크기(byte)")

    # ── 커버리지 요약 (목록/카드에 그대로 뿌림) ──
    posts_analyzed = models.PositiveIntegerField(default=0, verbose_name="분석 게시물 수")
    reels_with_views = models.PositiveIntegerField(default=0, verbose_name="조회수 있는 릴스 수")
    videos_analyzed = models.PositiveIntegerField(default=0, verbose_name="AI 영상 분석 수")
    comments_analyzed = models.PositiveIntegerField(default=0, verbose_name="분석 댓글 수")
    period_from = models.DateField(null=True, blank=True, verbose_name="분석 기간 시작")
    period_to = models.DateField(null=True, blank=True, verbose_name="분석 기간 종료")

    # ── 운영 ──
    cost_usd = models.DecimalField(
        max_digits=8, decimal_places=4, default=0, verbose_name="원가(USD)"
    )
    tokens_json = models.JSONField(default=dict, blank=True, verbose_name="토큰 원장")
    elapsed_seconds = models.PositiveIntegerField(default=0, verbose_name="소요(초)")
    error_code = models.CharField(
        max_length=32,
        choices=ReportErrorCode.choices,
        blank=True,
        default="",
        verbose_name="실패 코드",
    )
    error_message = models.TextField(blank=True, default="", verbose_name="실패 상세(내부)")
    celery_task_id = models.CharField(max_length=255, blank=True, default="")
    # 쿼터 차감 여부 — 실패(사용자 귀책 아님)는 차감하지 않는다.
    quota_consumed = models.BooleanField(default=True, db_index=True, verbose_name="이용 횟수 차감")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "insta_report"
        verbose_name = "인스타 성장 리포트"
        verbose_name_plural = "인스타 성장 리포트 목록"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "-created_at"]),
            models.Index(fields=["ig_connection", "-created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"@{self.ig_username} {self.status} ({self.created_at:%Y-%m-%d})"

    # ── 상태 헬퍼 ──
    @property
    def is_terminal(self) -> bool:
        return self.status in (ReportStatus.SUCCEEDED, ReportStatus.FAILED, ReportStatus.CANCELLED)

    def set_stage(
        self, stage: str, progress: int, message: str = "", expected_seconds: int | None = None
    ) -> None:
        """단계·진행률·메시지를 한 번에 갱신(태스크 전용)."""
        fields = ["stage", "progress", "message", "stage_started_at", "updated_at"]
        self.stage = stage
        self.progress = max(int(self.progress or 0), int(progress))  # 진행률 역행 금지
        self.message = message or self.message
        self.stage_started_at = timezone.now()
        if expected_seconds is not None:
            self.stage_expected_seconds = int(expected_seconds)
            fields.append("stage_expected_seconds")
        self.save(update_fields=fields)

    def mark_failed(self, code: str, detail: str = "", *, quota_consumed: bool = False) -> None:
        self.status = ReportStatus.FAILED
        self.error_code = code
        self.error_message = (detail or "")[:4000]
        self.quota_consumed = quota_consumed
        self.finished_at = timezone.now()
        if self.started_at:
            self.elapsed_seconds = int((self.finished_at - self.started_at).total_seconds())
        self.save(
            update_fields=[
                "status",
                "error_code",
                "error_message",
                "quota_consumed",
                "finished_at",
                "elapsed_seconds",
                "updated_at",
            ]
        )

    @property
    def error_message_ko(self) -> str:
        return ERROR_MESSAGES_KO.get(self.error_code, "") if self.error_code else ""


class IGVideoFeature(models.Model):
    """Gemini 영상 피처 추출 캐시 (영구).

    키는 (IG 계정 ID, shortcode, 스키마 버전). 계정 ID 를 키에 넣어 테넌트 간 공유를 원천 차단한다
    — 같은 shortcode 가 다른 계정에 뜰 일은 없지만 캐시는 보수적으로 격리한다.
    스키마 버전이 올라가면 자동으로 캐시 미스가 되어 재추출된다.
    """

    external_account_id = models.CharField(max_length=255, verbose_name="IG 계정 ID")
    shortcode = models.CharField(max_length=64, verbose_name="게시물 shortcode")
    schema_version = models.PositiveSmallIntegerField(verbose_name="피처 스키마 버전")
    features_json = models.JSONField(verbose_name="추출 피처")
    model_name = models.CharField(max_length=100, blank=True, default="")
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "insta_report_video_feature"
        verbose_name = "영상 피처 캐시"
        verbose_name_plural = "영상 피처 캐시 목록"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["external_account_id", "shortcode", "schema_version"],
                name="uq_igfeature_acct_sc_ver",
            )
        ]

    def __str__(self):
        return f"{self.shortcode}@v{self.schema_version}"


class ReportAiCache(models.Model):
    """댓글 분류 등 기타 AI 결과 캐시 (키-값). 90일 후 purge 대상."""

    cache_key = models.CharField(max_length=128, unique=True, verbose_name="캐시 키(sha256)")
    kind = models.CharField(max_length=40, db_index=True, verbose_name="종류")
    version = models.PositiveSmallIntegerField(default=1)
    payload_json = models.JSONField(verbose_name="캐시 값")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "insta_report_ai_cache"
        verbose_name = "리포트 AI 캐시"
        verbose_name_plural = "리포트 AI 캐시 목록"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind}:{self.cache_key[:12]}"
