from django.db import migrations, models


class Migration(migrations.Migration):
    """AI 페이지 생성 시 사용자가 카테고리→레퍼런스 페이지를 선택하기 위한
    ``ReferenceCategory`` 모델을 도입한다. Page 와의 FK 연결 및 필드 확장은
    0018 마이그레이션에서 수행.
    """

    dependencies = [
        ("pages", "0016_pagesnapshot_unique_reason"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReferenceCategory",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        help_text="유저에게 노출. 예: '프로필 링크', '브로슈어/팜플렛'",
                        max_length=80,
                        verbose_name="카테고리 한글명",
                    ),
                ),
                (
                    "slug",
                    models.SlugField(
                        help_text="URL/API 경로에 사용. 소문자/하이픈만. 예: 'profile-link'",
                        max_length=50,
                        unique=True,
                        verbose_name="영문 슬러그",
                    ),
                ),
                (
                    "description",
                    models.TextField(blank=True, default="", verbose_name="설명"),
                ),
                (
                    "icon_emoji",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="예: '🎵'. icon_url 이 있으면 그게 우선.",
                        max_length=8,
                        verbose_name="아이콘 이모지",
                    ),
                ),
                (
                    "icon_url",
                    models.URLField(
                        blank=True,
                        default="",
                        max_length=512,
                        verbose_name="아이콘 이미지 URL",
                    ),
                ),
                (
                    "sort_order",
                    models.PositiveIntegerField(
                        db_index=True, default=0, verbose_name="정렬 순서 (ASC)"
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(
                        db_index=True, default=True, verbose_name="공개 API 노출 여부"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "AI 레퍼런스 카테고리",
                "verbose_name_plural": "AI 레퍼런스 카테고리 목록",
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.AddIndex(
            model_name="referencecategory",
            index=models.Index(
                fields=["is_active", "sort_order"],
                name="pages_refer_is_acti_5bfd3e_idx",
            ),
        ),
    ]
