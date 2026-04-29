import uuid

from django.conf import settings
from django.db import models


class AiJob(models.Model):
    """AI 페이지 생성 작업. Celery 비동기 처리 후 결과 저장."""

    class JobType(models.TextChoices):
        BIO_REMAKE = "bio_remake", "바이오 리메이크"
        THEME_GENERATION = "theme_generation", "테마 생성"
        COPY_GENERATION = "copy_generation", "카피 생성"
        # 외부 서비스(인포크/리틀리/링크트리) 페이지를 비동기로 가져오기.
        # 이미지 재업로드 옵션이 있어 LLM 호출 없이도 분 단위 작업이 됨 → AiJob 으로 처리.
        EXTERNAL_IMPORT = "external_import", "외부 페이지 가져오기"

    class LlmModel(models.TextChoices):
        GEMMA = "gemma", "Gemma (자체 H100, 기본)"
        DEEPSEEK = "deepseek", "DeepSeek V4-Flash (외부 API, 폴백/오버플로우)"
        GPT5 = "gpt5", "GPT-5.4"

    # LlmModel → 실제 LiteLLM 모델명 (litellm-config.yaml 의 model_name 과 일치해야 함)
    LLM_MODEL_MAP = {
        "gemma": "gemma-4",
        "deepseek": "deepseek",
        "gpt5": "gpt-5.4",
    }

    # AI 작업 1건당 고정 토큰 비용
    TOKEN_COST = 1

    class Status(models.TextChoices):
        QUEUED = "queued", "대기"
        RUNNING = "running", "진행 중"
        SUCCEEDED = "succeeded", "완료"
        FAILED = "failed", "실패"

    class Stage(models.TextChoices):
        QUEUED = "queued", "대기"
        # LLM 파이프라인 (BIO_REMAKE / THEME_GENERATION / COPY_GENERATION) 단계
        PREPARING_PROMPT = "preparing_prompt", "프롬프트 준비"
        CALLING_MODEL = "calling_model", "모델 호출"
        PARSING_RESPONSE = "parsing_response", "응답 파싱"
        RESOLVING_IMAGES = "resolving_images", "이미지 검색"
        # 외부 임포트 파이프라인 (EXTERNAL_IMPORT) 단계
        FETCHING_SOURCE = "fetching_source", "원본 페이지 다운로드"
        CONVERTING = "converting", "블록 변환"
        REUPLOADING_IMAGES = "reuploading_images", "이미지 재업로드"
        CREATING_PAGE = "creating_page", "페이지 생성"
        COMPLETED = "completed", "완료"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_jobs",
        verbose_name="사용자",
    )
    page = models.ForeignKey(
        "pages.Page",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_jobs",
        verbose_name="대상 페이지",
    )
    job_type = models.CharField(
        max_length=30,
        choices=JobType.choices,
        default=JobType.BIO_REMAKE,
        verbose_name="작업 유형",
    )
    llm_model = models.CharField(
        max_length=20,
        choices=LlmModel.choices,
        default=LlmModel.GEMMA,
        verbose_name="LLM 모델",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
        verbose_name="상태",
    )
    stage = models.CharField(
        max_length=30,
        choices=Stage.choices,
        default=Stage.QUEUED,
        verbose_name="진행 단계",
    )
    progress = models.PositiveSmallIntegerField(
        default=0,
        verbose_name="진행률 (%)",
        help_text="0~100",
    )
    message = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="진행 메시지",
    )

    # ── 입력 ──
    input_payload = models.JSONField(
        default=dict,
        verbose_name="사용자 입력",
        help_text="프론트에서 전달한 컨셉, 스타일, 참고 자료 등",
    )
    resolved_prompt = models.TextField(
        blank=True,
        default="",
        verbose_name="조립된 프롬프트",
        help_text="실제 LLM에 전달된 전체 프롬프트 (디버깅/분석용)",
    )

    # ── 모델 정보 ──
    model_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="사용 모델",
    )

    # ── 결과 ──
    result_json = models.JSONField(
        null=True,
        blank=True,
        verbose_name="생성 결과 JSON",
        help_text="LLM이 생성한 페이지 JSON (blocks 포함)",
    )
    error_message = models.TextField(
        blank=True,
        default="",
        verbose_name="에러 메시지",
    )

    # ── 타임스탬프 ──
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="실행 시작")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="실행 종료")

    class Meta:
        verbose_name = "AI 작업"
        verbose_name_plural = "AI 작업 목록"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.job_type} / {self.status} ({self.user})"

    def set_stage(self, stage: str, progress: int, message: str = ""):
        """단계·진행률·메시지를 한 번에 업데이트."""
        self.stage = stage
        self.progress = progress
        if message:
            self.message = message
        self.save(update_fields=["stage", "progress", "message", "updated_at"])
