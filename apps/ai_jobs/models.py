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
        # 게시물(이미지+캡션) → AutoDM 캠페인 폼 초안. gemma-4 가 50개 답글 등 긴 출력을
        # 만드느라 수십 초가 걸려 동기 응답은 prod gunicorn 타임아웃에 걸린다 → AiJob 으로 처리.
        DM_CAMPAIGN_ASSIST = "dm_campaign_assist", "DM 캠페인 폼 자동완성"

    class LlmModel(models.TextChoices):
        GEMMA = "gemma", "Gemma (자체 H100, 폴백/오버플로우)"
        DEEPSEEK = "deepseek", "DeepSeek V4-Flash (외부 API, 기본)"
        GPT5 = "gpt5", "GPT-5.4"

    class Mode(models.TextChoices):
        # 빈 문자열(LEGACY)은 구 bio_remake 방식 — DB 에 이미 쌓인 result_json 호환용.
        LEGACY = "", "(구) 전체 재생성"
        FULL_RESTYLE = "full_restyle", "스타일 패치 + 구조 변경"
        STYLE_ONLY = "style_only", "스타일 패치만 (콘텐츠 보존)"

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
        # 사용자 업로드 이미지가 있는 새-생성 작업의 선행 단계.
        LABELING_IMAGES = "labeling_images", "이미지 분석"
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
        default=LlmModel.DEEPSEEK,
        verbose_name="LLM 모델",
    )
    mode = models.CharField(
        max_length=20,
        choices=Mode.choices,
        blank=True,
        default=Mode.LEGACY,
        verbose_name="리뉴얼 모드",
        help_text=(
            "bio_remake 작업 한정. 빈 문자열 = 구 방식(전체 재생성, 호환용). "
            "full_restyle = 스타일/순서/추가삭제. style_only = 스타일만 패치."
        ),
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


class AiSourceImage(models.Model):
    """AI 새-페이지 생성에 사용할 사용자 업로드 이미지.

    기존 ``pages.PageMedia`` 는 ``page`` FK 가 필수라 "아직 페이지가 없는" 새-생성
    시점에 쓸 수 없고, 멀티페이지 환경에서 무관한 페이지를 오염시킨다. 그래서 페이지에
    묶이지 않는 경량 모델을 따로 둔다.

    흐름:
        1) ``POST /api/v1/ai/source-images/`` 로 업로드 → ``job=None`` 으로 생성
        2) ``POST /api/v1/ai/jobs/`` 에 ``image_ids`` 전달 → 해당 AiJob 으로 연결(``job`` 채움)
        3) Celery 라벨링 단계가 비전 LLM 으로 ``usable``/``role``/``summary`` 등을 채움
    """

    class Role(models.TextChoices):
        # 페이지에 실제로 배치할 콘텐츠 이미지.
        CONTENT = "content", "콘텐츠(페이지 배치)"
        # 분위기/색감 참고만 하고 배치하지 않는 컨셉 이미지.
        CONCEPT = "concept", "컨셉(참고용)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_source_images",
        verbose_name="사용자",
    )
    job = models.ForeignKey(
        "AiJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_images",
        verbose_name="연결된 작업",
        help_text="업로드 시점엔 비어 있고, 생성 작업 생성 시 연결된다.",
    )

    # ── 파일 ──
    file = models.FileField(upload_to="ai_source_images/%Y/%m/", verbose_name="이미지")
    mime_type = models.CharField(max_length=100, blank=True, default="")
    size = models.PositiveIntegerField(default=0, verbose_name="크기(byte)")
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    original_name = models.CharField(max_length=500, blank=True, default="")
    # 동일 이미지 재라벨 방지용 콘텐츠 해시(sha256). 선택 최적화.
    content_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # ── 라벨링 결과 (라벨 캐시 겸용) ──
    labeled = models.BooleanField(default=False, verbose_name="라벨링 완료")
    usable = models.BooleanField(default=False, verbose_name="페이지 사용 가능")
    role = models.CharField(
        max_length=20, choices=Role.choices, blank=True, default="", verbose_name="용도"
    )
    summary = models.TextField(blank=True, default="", verbose_name="이미지 요약")
    suggested_use = models.CharField(max_length=200, blank=True, default="")
    quality_flags = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="품질 플래그",
        help_text="{blurry, low_res, has_text, nsfw}",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "AI 소스 이미지"
        verbose_name_plural = "AI 소스 이미지 목록"
        ordering = ["created_at"]  # 업로드 순서 보존 → {{user_image:N}} 인덱스 안정화
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self):
        return f"AiSourceImage({self.id}) {self.role or 'unlabeled'} ({self.user})"

    def delete(self, *args, **kwargs):
        """DB 삭제 시 스토리지 파일도 함께 제거 (PageMedia.delete 패턴)."""
        if self.file:
            self.file.delete(save=False)
        super().delete(*args, **kwargs)
