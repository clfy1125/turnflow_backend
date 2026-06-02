from django.db import migrations, models


class Migration(migrations.Migration):
    """Page 모델에 AI Few-shot 레퍼런스용 필드 9개 + 카테고리 FK + 복합 인덱스를 추가.

    모든 필드는 nullable/default 설정이라 기존 row 에 안전하게 추가됨.
    PG 16 metadata-only DDL.
    """

    dependencies = [
        ("pages", "0017_create_reference_category"),
    ]

    operations = [
        migrations.AddField(
            model_name="page",
            name="is_reference",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="True 이면 카테고리 내 AI 레퍼런스 후보로 노출. 어드민만 토글 가능.",
                verbose_name="AI 레퍼런스 대상",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="reference_pages",
                to="pages.referencecategory",
                verbose_name="레퍼런스 카테고리",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_order",
            field=models.PositiveIntegerField(
                default=0, verbose_name="카테고리 내 정렬 순서"
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_title",
            field=models.CharField(
                blank=True,
                default="",
                help_text="비어 있으면 page.title 사용.",
                max_length=120,
                verbose_name="레퍼런스 표시명",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_description",
            field=models.TextField(
                blank=True,
                default="",
                help_text="이 페이지가 어떤 스타일/용도인지 사용자 안내용 한두 줄.",
                verbose_name="레퍼런스 설명",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_snapshot",
            field=models.ImageField(
                blank=True,
                help_text="Playwright Headless 캡쳐 → WebP. R2/로컬 STORAGES default 사용.",
                null=True,
                upload_to="pages/snapshots/%Y/%m/",
                verbose_name="모바일 미리보기 스냅샷",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_snapshot_updated_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="스냅샷 마지막 갱신 시각"
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_snapshot_job_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Celery task id. 폴링용. 빈 문자열이면 진행 중 아님.",
                max_length=64,
                verbose_name="스냅샷 캡쳐 작업 ID",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_snapshot_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "-"),
                    ("pending", "대기 중"),
                    ("running", "진행 중"),
                    ("succeeded", "완료"),
                    ("failed", "실패"),
                ],
                default="",
                max_length=20,
                verbose_name="스냅샷 상태",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="reference_snapshot_error",
            field=models.TextField(
                blank=True, default="", verbose_name="스냅샷 실패 메시지"
            ),
        ),
        migrations.AddIndex(
            model_name="page",
            index=models.Index(
                fields=["is_reference", "reference_category", "reference_order"],
                name="page_ref_browse_idx",
            ),
        ),
    ]
