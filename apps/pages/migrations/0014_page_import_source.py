from django.db import migrations, models


class Migration(migrations.Migration):
    """Page 에 외부 임포트 추적 필드 4개 추가.

    인포크/리틀리/링크트리 페이지 복사 기능(`/api/v1/pages/ai/import-external/`)이
    페이지를 만들 때 출처를 기록하기 위한 필드. 자체 생성 페이지엔 영향 없음
    (전부 빈 문자열 / NULL 디폴트).
    """

    dependencies = [
        ("pages", "0013_page_is_active"),
    ]

    operations = [
        migrations.AddField(
            model_name="page",
            name="import_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "자체 생성"),
                    ("inpock", "인포크"),
                    ("litly", "리틀리"),
                    ("linktree", "링크트리"),
                ],
                default="",
                help_text="외부에서 임포트했다면 어떤 서비스인지. 자체 생성이면 빈 문자열.",
                max_length=20,
                verbose_name="외부 임포트 소스",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="import_source_slug",
            field=models.CharField(
                blank=True,
                default="",
                help_text="외부 서비스에서의 원본 slug (예: 'koreanwithmina').",
                max_length=255,
                verbose_name="원본 slug",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="import_source_url",
            field=models.URLField(
                blank=True,
                default="",
                max_length=512,
                verbose_name="원본 URL",
            ),
        ),
        migrations.AddField(
            model_name="page",
            name="imported_at",
            field=models.DateTimeField(
                blank=True,
                help_text="외부에서 임포트한 시점. 자체 생성 페이지면 NULL.",
                null=True,
                verbose_name="임포트 일시",
            ),
        ),
    ]
