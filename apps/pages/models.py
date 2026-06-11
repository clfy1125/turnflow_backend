import uuid

from django.conf import settings
from django.db import models
from django.utils.text import slugify


def _generate_unique_slug(username: str) -> str:
    """username 기반으로 unique slug 생성. 충돌 시 숫자 suffix 추가."""
    base = slugify(username) or "page"
    slug = base
    counter = 1
    while Page.objects.filter(slug=slug).exists():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


class Page(models.Model):
    """사용자당 여러 개 생성 가능한 공개 가능한 블록형 페이지."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pages",
        verbose_name="소유자",
    )
    slug = models.SlugField(
        max_length=120,
        unique=True,
        verbose_name="공개 URL slug",
        help_text="공개 URL 식별자. 사용자명 변경 시 자동 갱신되지 않습니다.",
    )
    title = models.CharField(max_length=255, blank=True, default="", verbose_name="페이지 제목")
    is_public = models.BooleanField(default=False, verbose_name="공개 여부")
    data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="페이지 설정 데이터",
        help_text="프론트엔드가 자유롭게 저장하는 페이지 설정 (테마, 배경색, 폰트 등). 서버는 구조를 강제하지 않습니다.",
    )
    custom_css = models.TextField(
        blank=True,
        default="",
        verbose_name="커스텀 CSS",
        help_text="사용자가 자유롭게 작성하는 CSS. 공개 페이지 렌더링 시 <style> 태그로 주입됩니다.",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="활성 상태",
        help_text="비활성 페이지는 공개 URL로 접근 불가. 다운그레이드 시 플랜 한도에 맞춰 비활성화됩니다.",
    )

    # 외부 임포트 추적 — 인포크/리틀리/링크트리에서 가져온 페이지의 출처를 기록.
    # 자체 생성 페이지는 모두 빈 문자열. 어드민 추적 / 같은 URL 재임포트 감지 /
    # 컨버터 정확도 분석용. 페이지를 만든 후 수동 편집해도 출처는 그대로 보존.
    class ImportSource(models.TextChoices):
        NONE = "", "자체 생성"
        INPOCK = "inpock", "인포크"
        LITLY = "litly", "리틀리"
        LINKTREE = "linktree", "링크트리"

    import_source = models.CharField(
        max_length=20,
        blank=True,
        default="",
        choices=ImportSource.choices,
        verbose_name="외부 임포트 소스",
        help_text="외부에서 임포트했다면 어떤 서비스인지. 자체 생성이면 빈 문자열.",
    )
    import_source_slug = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="원본 slug",
        help_text="외부 서비스에서의 원본 slug (예: 'koreanwithmina').",
    )
    import_source_url = models.URLField(
        max_length=512,
        blank=True,
        default="",
        verbose_name="원본 URL",
    )
    imported_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="임포트 일시",
        help_text="외부에서 임포트한 시점. 자체 생성 페이지면 NULL.",
    )

    # ── AI Few-shot 레퍼런스 메타 ─────────────────────────────
    # 어드민이 큐레이션한 공개 페이지를 카테고리별로 분류하면,
    # 사용자가 AI 페이지 생성 시 카테고리→레퍼런스 선택을 거쳐
    # 해당 페이지의 데이터(title/data/blocks/custom_css)가 LLM Few-shot 예시로 사용된다.
    is_reference = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name="AI 레퍼런스 대상",
        help_text="True 이면 카테고리 내 AI 레퍼런스 후보로 노출. 어드민만 토글 가능.",
    )
    reference_category = models.ForeignKey(
        "pages.ReferenceCategory",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reference_pages",
        verbose_name="레퍼런스 카테고리",
    )
    reference_order = models.PositiveIntegerField(
        default=0,
        verbose_name="카테고리 내 정렬 순서",
    )
    reference_title = models.CharField(
        max_length=120,
        blank=True,
        default="",
        verbose_name="레퍼런스 표시명",
        help_text="비어 있으면 page.title 사용.",
    )
    reference_description = models.TextField(
        blank=True,
        default="",
        verbose_name="레퍼런스 설명",
        help_text="이 페이지가 어떤 스타일/용도인지 사용자 안내용 한두 줄.",
    )
    reference_snapshot = models.ImageField(
        upload_to="pages/snapshots/%Y/%m/",
        null=True,
        blank=True,
        verbose_name="모바일 미리보기 스냅샷",
        help_text="Playwright Headless 캡쳐 → WebP. R2/로컬 STORAGES default 사용.",
    )
    reference_snapshot_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="스냅샷 마지막 갱신 시각",
    )
    reference_snapshot_job_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="스냅샷 캡쳐 작업 ID",
        help_text="Celery task id. 폴링용. 빈 문자열이면 진행 중 아님.",
    )

    class SnapshotStatus(models.TextChoices):
        NONE = "", "-"
        PENDING = "pending", "대기 중"
        RUNNING = "running", "진행 중"
        SUCCEEDED = "succeeded", "완료"
        FAILED = "failed", "실패"

    reference_snapshot_status = models.CharField(
        max_length=20,
        blank=True,
        default="",
        choices=SnapshotStatus.choices,
        verbose_name="스냅샷 상태",
    )
    reference_snapshot_error = models.TextField(
        blank=True,
        default="",
        verbose_name="스냅샷 실패 메시지",
    )

    # ── AI 스냅샷 활성 슬롯 포인터 ────────────────────────────
    # 라이브 페이지가 "지금 어느 스냅샷 상태와 동일한지"를 가리키는 포인터.
    #   - AI 1-shot 편집 직후 → latest_ai_result 스냅샷
    #   - 스냅샷 복원 직후    → 복원에 사용한 스냅샷
    #   - 사용자가 블록/디자인을 직접 편집해 저장 → NULL (라이브가 어느 슬롯과도 불일치)
    # GET .../snapshots/ 의 is_current 계산에 사용. 가리키던 스냅샷이 지워지면
    # on_delete=SET_NULL 로 자동 NULL. PageSnapshot 은 같은 파일 하단에 정의돼
    # 있어 문자열 참조로 forward reference.
    current_snapshot = models.ForeignKey(
        "pages.PageSnapshot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="현재 활성 스냅샷",
        help_text=(
            "라이브 페이지가 현재 일치하는 스냅샷. AI 편집/복원 직후 설정되고, "
            "사용자가 직접 편집하면 NULL 로 초기화된다."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "페이지"
        verbose_name_plural = "페이지 목록"
        indexes = [
            models.Index(
                fields=["is_reference", "reference_category", "reference_order"],
                name="page_ref_browse_idx",
            ),
        ]

    def __str__(self):
        return f"{self.slug} ({self.user})"

    @classmethod
    def get_or_create_for_user(cls, user):
        """유저의 첫 번째 Page 반환. 없으면 자동 생성."""
        page = cls.objects.filter(user=user).order_by("created_at").first()
        if page:
            return page, False
        slug = _generate_unique_slug(user.username)
        return cls.objects.create(user=user, slug=slug, is_public=False), True

    def detach_snapshot_pointer(self):
        """사용자가 블록/디자인을 직접 편집해 라이브가 어느 스냅샷과도 일치하지
        않게 됐을 때 활성 슬롯 포인터를 해제한다.

        AI 편집/복원 외의 모든 일반 편집 경로(블록 생성/수정/삭제/재정렬, 페이지
        메타·CSS 수정)에서 호출한다. 이미 NULL 이면 DB 쓰기 없이 즉시 반환하므로
        AI 스냅샷이 없는 대다수 페이지에서는 사실상 무비용이다.
        """
        if self.current_snapshot_id is not None:
            self.current_snapshot_id = None
            self.save(update_fields=["current_snapshot", "updated_at"])


class Block(models.Model):
    """페이지 안에 배치되는 블록 단위 컨텐츠."""

    class BlockType(models.TextChoices):
        PROFILE = "profile", "프로필"
        CONTACT = "contact", "연락처"
        SINGLE_LINK = "single_link", "단일 링크"

    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="blocks",
        verbose_name="페이지",
    )
    type = models.CharField(
        max_length=50,
        choices=BlockType.choices,
        verbose_name="블록 타입",
    )
    order = models.PositiveIntegerField(default=0, verbose_name="표시 순서")
    is_enabled = models.BooleanField(default=True, verbose_name="노출 여부")
    data = models.JSONField(default=dict, verbose_name="블록 데이터")
    custom_css = models.TextField(
        blank=True,
        default="",
        verbose_name="커스텀 CSS",
        help_text="블록에 적용할 커스텀 CSS. 공개 페이지 렌더링 시 해당 블록 영역에 주입.",
    )

    # ── 예약 설정 ──────────────────────────────────────────
    schedule_enabled = models.BooleanField(
        default=False,
        verbose_name="예약 설정 활성화",
        help_text="True이면 publish_at/hide_at 기준으로 공개 여부를 자동 제어합니다.",
    )
    publish_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="공개 시작 일시",
        help_text="이 시각 이후부터 공개 페이지에 노출됩니다. (schedule_enabled=True일 때만 적용)",
    )
    hide_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="숨김 시작 일시",
        help_text="이 시각 이후부터 공개 페이지에서 숨겨집니다. (schedule_enabled=True일 때만 적용)",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "블록"
        verbose_name_plural = "블록 목록"
        ordering = ["order"]
        # 같은 페이지 내 order 중복 금지
        unique_together = [("page", "order")]
        indexes = [
            models.Index(fields=["page", "type"]),
        ]

    def __str__(self):
        return f"{self.page.slug} / {self.type} (order={self.order})"

    def get_next_order(self):
        """현재 페이지의 마지막 order + 1 반환."""
        last = Block.objects.filter(page=self.page).order_by("-order").first()
        return (last.order + 1) if last else 1


# ─────────────────────────────────────────────────────────────
# 통계 모델
# ─────────────────────────────────────────────────────────────


class PageView(models.Model):
    """공개 페이지 조회 이벤트. 방문자가 페이지를 열 때 1건 기록."""

    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="views",
        verbose_name="페이지",
    )
    viewed_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="조회 일시")
    referer = models.CharField(
        max_length=500,
        blank=True,
        default="",
        verbose_name="유입 채널 URL",
        help_text="HTTP Referer 헤더 원문. 집계 시 도메인으로 파싱됩니다.",
    )
    country = models.CharField(
        max_length=2,
        blank=True,
        default="",
        verbose_name="유입 국가",
        help_text="ISO 3166-1 alpha-2 코드 (예: KR, US). Cloudflare CF-IPCountry 헤더 기반.",
    )
    ip_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="IP 해시",
        help_text="SHA-256(IP). 개인정보 보호를 위해 원본 IP는 저장하지 않습니다.",
    )

    class Meta:
        verbose_name = "페이지 조회"
        verbose_name_plural = "페이지 조회 목록"
        indexes = [
            models.Index(fields=["page", "viewed_at"]),
            models.Index(fields=["page", "country"]),
        ]


class BlockClick(models.Model):
    """블록 클릭 이벤트. 방문자가 링크/연락처 블록을 클릭할 때 1건 기록."""

    block = models.ForeignKey(
        Block,
        on_delete=models.CASCADE,
        related_name="clicks",
        verbose_name="블록",
    )
    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="block_clicks",
        verbose_name="페이지",
        help_text="집계 쿼리 최적화를 위해 비정규화 저장.",
    )
    link_id = models.CharField(
        max_length=200,
        blank=True,
        default="",
        verbose_name="서브링크 ID",
        help_text="social 블록의 플랫폼 키(instagram, youtube 등), group_link의 개별 링크 ID. 빈 문자열이면 블록 단위 클릭.",
    )
    clicked_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="클릭 일시")
    referer = models.CharField(max_length=500, blank=True, default="", verbose_name="유입 채널 URL")
    country = models.CharField(max_length=2, blank=True, default="", verbose_name="유입 국가")
    ip_hash = models.CharField(max_length=64, blank=True, default="", verbose_name="IP 해시")

    class Meta:
        verbose_name = "블록 클릭"
        verbose_name_plural = "블록 클릭 목록"
        indexes = [
            models.Index(fields=["page", "clicked_at"]),
            models.Index(fields=["block", "clicked_at"]),
            models.Index(fields=["block", "link_id", "clicked_at"]),
        ]


# ─────────────────────────────────────────────────────────────
# 문의 모델
# ─────────────────────────────────────────────────────────────


class ContactInquiry(models.Model):
    """페이지 방문자가 페이지 관리자에게 보내는 문의."""

    class Category(models.TextChoices):
        GENERAL = "general", "일반 문의"
        BUSINESS = "business", "비즈니스 협업"
        SUPPORT = "support", "고객 지원"
        OTHER = "other", "기타"

    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="inquiries",
        verbose_name="페이지",
    )

    # ── 방문자가 입력하는 정보 ─────────────────────────────
    name = models.CharField(max_length=100, verbose_name="보낸 사람")
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.GENERAL,
        verbose_name="분류",
    )
    email = models.EmailField(blank=True, default="", verbose_name="이메일")
    phone = models.CharField(max_length=30, verbose_name="휴대폰번호")
    subject = models.CharField(max_length=255, verbose_name="문의 제목")
    content = models.TextField(blank=True, default="", verbose_name="문의 내용")
    agreed_to_terms = models.BooleanField(
        default=False,
        verbose_name="이용약관 및 개인정보 처리방침 동의",
    )

    # ── 페이지 관리자 전용 ─────────────────────────────────
    memo = models.TextField(
        blank=True,
        default="",
        verbose_name="관리자 메모",
        help_text="작성 내용은 확인용으로, 문의 고객에게 전달되지 않습니다.",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="문의 일시")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "문의"
        verbose_name_plural = "문의 목록"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["page", "created_at"]),
            models.Index(fields=["page", "category"]),
        ]

    def __str__(self):
        return f"[{self.get_category_display()}] {self.subject} — {self.name} ({self.created_at:%Y-%m-%d})"


# ─────────────────────────────────────────────────────────────
# 구독 모델
# ─────────────────────────────────────────────────────────────


class PageSubscription(models.Model):
    """페이지 방문자가 페이지 관리자의 구독 폼을 통해 등록하는 구독자."""

    class Category(models.TextChoices):
        PAGE_SUBSCRIBE = "page_subscribe", "페이지 구독"
        NEWSLETTER = "newsletter", "뉴스레터"
        EVENT = "event", "이벤트 알림"
        OTHER = "other", "기타"

    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        verbose_name="페이지",
    )

    # ── 방문자가 입력하는 정보 ─────────────────────────────
    name = models.CharField(max_length=100, blank=True, default="", verbose_name="이름")
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.PAGE_SUBSCRIBE,
        verbose_name="분류",
    )
    email = models.EmailField(verbose_name="이메일")
    phone = models.CharField(max_length=30, blank=True, default="", verbose_name="휴대폰번호")
    agreed_to_terms = models.BooleanField(
        default=False,
        verbose_name="개인정보 수집 동의",
    )

    # ── 페이지 관리자 전용 ─────────────────────────────────
    memo = models.TextField(
        blank=True,
        default="",
        verbose_name="관리자 메모",
        help_text="구독자에게 노출되지 않는 관리자 전용 메모입니다.",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="구독 일시")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "구독자"
        verbose_name_plural = "구독자 목록"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["page", "created_at"]),
            models.Index(fields=["page", "category"]),
        ]

    def __str__(self):
        return f"[{self.get_category_display()}] {self.email} ({self.created_at:%Y-%m-%d})"


# ─────────────────────────────────────────────────────────────
# 미디어 파일 모델
# ─────────────────────────────────────────────────────────────


class PageMedia(models.Model):
    """페이지 관리자가 업로드한 이미지/파일. block.data 의 URL 필드에서 참조."""

    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="media_files",
        verbose_name="페이지",
    )
    file = models.FileField(
        upload_to="pages/%Y/%m/",
        verbose_name="완성(크롭) 파일",
        help_text="편집(크롭) 완료된 최종 이미지. 블록 렌더링에 사용.",
    )
    original_file = models.FileField(
        upload_to="pages/originals/%Y/%m/",
        null=True,
        blank=True,
        verbose_name="편집 전 원본 파일",
        help_text="크롭/편집하기 전의 원본 이미지. 재편집 시 이 파일로 편집기를 복원합니다.",
    )
    crop_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="크롭 파라미터",
        help_text=(
            "이미지 편집 파라미터. "
            "{x, y, width, height, aspect_ratio, locked, rotation, original_width, original_height}. "
            "빈 dict이면 (기존 이미지) 프론트에서 전체 영역(최대 크롭)으로 간주합니다."
        ),
    )
    original_name = models.CharField(max_length=500, verbose_name="원본 파일명")
    mime_type = models.CharField(max_length=100, verbose_name="MIME 타입")
    size = models.PositiveIntegerField(verbose_name="파일 크기 (bytes)")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="업로드 일시")

    class Meta:
        verbose_name = "미디어 파일"
        verbose_name_plural = "미디어 파일 목록"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["page", "created_at"]),
        ]

    def __str__(self):
        return f"{self.page.slug} / {self.original_name}"

    def delete(self, *args, **kwargs):
        """DB 레코드 삭제 시 스토리지 파일도 함께 제거."""
        if self.file:
            self.file.delete(save=False)
        if self.original_file:
            self.original_file.delete(save=False)
        super().delete(*args, **kwargs)


# ─────────────────────────────────────────────────────────────
# 페이지 스냅샷 (AI 편집 롤백용)
# ─────────────────────────────────────────────────────────────


class PageSnapshot(models.Model):
    """AI 편집/복원 이력을 담는 페이지 변경 기록 (bounded history).

    페이지당 여러 건이 시간순으로 쌓이며, ``apps.pages.aiviews`` 의 헬퍼들이
    다음 시점마다 한 건씩 INSERT 한다 (덮어쓰기 X):
      - ``AI_EDIT`` — AI 첫 호출 직전의 원본. 페이지당 한 번만 생성되고 영구 유지
        (트리밍 대상에서 제외 = 항상 맨 처음으로 되돌릴 수 있는 앵커).
      - ``AI_RESULT`` — AI 편집(1-shot 적용) 직후의 작업물. AI 적용마다 새로 쌓인다.
      - ``RESTORE`` — 스냅샷 복원 직전의 라이브 상태. 복원으로 덮어쓰기 전에 보관해
        "롤백의 롤백" 을 가능하게 한다.

    보관 한도는 ``aiviews.MAX_SNAPSHOTS_PER_PAGE`` (기본 10) — 초과 시 오래된 것부터
    삭제하되 원본(``AI_EDIT``)과 현재 활성 스냅샷(``Page.current_snapshot``)은 보존.
    페이지 삭제 시 CASCADE.

    ``LATEST_AI_RESULT`` 는 0022 이전 데이터의 reason 값으로 남아 있을 수 있어
    choices 에 유지한다 (신규 생성은 ``AI_RESULT`` 사용).
    """

    class Reason(models.TextChoices):
        AI_EDIT = "ai_edit", "AI 편집 직전 원본"
        AI_RESULT = "ai_result", "AI 작업물"
        RESTORE = "restore", "복원 직전 상태"
        # 레거시 — 0022 이전에 생성된 단일 슬롯 작업물. 신규 생성 금지, 표시/복원만.
        LATEST_AI_RESULT = "latest_ai_result", "AI 작업물"

    page = models.ForeignKey(
        Page,
        on_delete=models.CASCADE,
        related_name="snapshots",
        verbose_name="페이지",
    )
    reason = models.CharField(
        max_length=30,
        choices=Reason.choices,
        verbose_name="생성 사유",
        help_text="어떤 작업 직전에 떠둔 스냅샷인지.",
    )
    snapshot = models.JSONField(
        verbose_name="스냅샷 데이터",
        help_text=(
            "페이지 + 블록 전체 상태. 스키마: "
            '{"page": {title, is_public, data, custom_css}, '
            '"blocks": [{id, type, order, is_enabled, data, custom_css, '
            "schedule_enabled, publish_at, hide_at}, ...]}"
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name="생성자",
        help_text="스냅샷을 발생시킨 사용자 (페이지 소유자와 다를 수 있음).",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "페이지 스냅샷"
        verbose_name_plural = "페이지 스냅샷 목록"
        ordering = ["-created_at"]
        # bounded history — 페이지당 여러 건이 시간순으로 쌓인다. (reason 별 unique 아님:
        # 0016 에서 넣었던 (page, reason) unique 는 0022 에서 제거 = 이력 보관 가능.)
        indexes = [
            models.Index(fields=["page", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.page.slug} / {self.get_reason_display()} @ {self.created_at:%Y-%m-%d %H:%M}"


# ─────────────────────────────────────────────────────────────
# AI 레퍼런스 카테고리
# ─────────────────────────────────────────────────────────────


class ReferenceCategory(models.Model):
    """AI 페이지 생성 시 사용자가 선택할 수 있는 레퍼런스 카테고리.

    어드민이 동적으로 CRUD. is_active=False 면 공개 API에서 제외.
    Page.reference_category 가 이 모델을 가리키며, 카테고리 삭제 시
    Page.reference_category=NULL (페이지는 보존).
    """

    name = models.CharField(
        max_length=80,
        verbose_name="카테고리 한글명",
        help_text="유저에게 노출. 예: '프로필 링크', '브로슈어/팜플렛'",
    )
    slug = models.SlugField(
        max_length=50,
        unique=True,
        verbose_name="영문 슬러그",
        help_text="URL/API 경로에 사용. 소문자/하이픈만. 예: 'profile-link'",
    )
    description = models.TextField(blank=True, default="", verbose_name="설명")
    icon_emoji = models.CharField(
        max_length=8,
        blank=True,
        default="",
        verbose_name="아이콘 이모지",
        help_text="예: '🎵'. icon_url 이 있으면 그게 우선.",
    )
    icon_url = models.URLField(
        max_length=512,
        blank=True,
        default="",
        verbose_name="아이콘 이미지 URL",
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        db_index=True,
        verbose_name="정렬 순서 (ASC)",
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name="공개 API 노출 여부",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "AI 레퍼런스 카테고리"
        verbose_name_plural = "AI 레퍼런스 카테고리 목록"
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["is_active", "sort_order"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.slug})"
