from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """PageSnapshot 추가 — AI 페이지 편집 직전 상태를 JSON 한 덩어리로 보관.

    ``AiPageEditView`` 가 ``page.blocks.all().delete()`` 후 재생성하는 구조라
    이전엔 롤백 불가능했음. 이 모델이 변경 직전 페이지+블록 전체를 JSON 으로
    떠놓고, ``/api/v1/pages/ai/@{slug}/snapshots/{id}/restore/`` 로 복원한다.
    """

    dependencies = [
        ("pages", "0014_page_import_source"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PageSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("ai_edit", "AI 1-shot 편집"),
                            ("ai_import_renewal", "외부 재임포트"),
                            ("restore", "롤백 직전 안전 스냅샷"),
                            ("manual", "수동 저장"),
                        ],
                        help_text="어떤 작업 직전에 떠둔 스냅샷인지.",
                        max_length=30,
                        verbose_name="생성 사유",
                    ),
                ),
                (
                    "snapshot",
                    models.JSONField(
                        help_text=(
                            "페이지 + 블록 전체 상태. 스키마: "
                            "{\"page\": {title, is_public, data, custom_css}, "
                            "\"blocks\": [{id, type, order, is_enabled, data, custom_css, "
                            "schedule_enabled, publish_at, hide_at}, ...]}"
                        ),
                        verbose_name="스냅샷 데이터",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="스냅샷을 발생시킨 사용자 (페이지 소유자와 다를 수 있음).",
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="생성자",
                    ),
                ),
                (
                    "page",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="snapshots",
                        to="pages.page",
                        verbose_name="페이지",
                    ),
                ),
            ],
            options={
                "verbose_name": "페이지 스냅샷",
                "verbose_name_plural": "페이지 스냅샷 목록",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["page", "-created_at"],
                        name="pages_pages_page_id_b8f1c4_idx",
                    ),
                ],
            },
        ),
    ]
