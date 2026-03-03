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
    """사용자당 1개의 공개 가능한 블록형 페이지."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="page",
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "페이지"
        verbose_name_plural = "페이지 목록"

    def __str__(self):
        return f"{self.slug} ({self.user})"

    @classmethod
    def get_or_create_for_user(cls, user):
        """유저의 Page 반환. 없으면 자동 생성."""
        try:
            return cls.objects.get(user=user), False
        except cls.DoesNotExist:
            slug = _generate_unique_slug(user.username)
            page = cls.objects.create(user=user, slug=slug, is_public=False)
            return page, True


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
