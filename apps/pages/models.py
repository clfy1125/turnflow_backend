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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "페이지"
        verbose_name_plural = "페이지 목록"

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
        verbose_name="파일",
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
        super().delete(*args, **kwargs)
