"""AiJob 에 ``mode`` 필드 추가.

bio_remake 작업의 재설계 — 스타일 패치 + 콘텐츠 보존 방식. 빈 문자열은 LEGACY(구 전체 재생성)
호환용으로 두고, full_restyle / style_only 로 분기한다.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_jobs", "0006_aijob_external_import_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="aijob",
            name="mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "(구) 전체 재생성"),
                    ("full_restyle", "스타일 패치 + 구조 변경"),
                    ("style_only", "스타일 패치만 (콘텐츠 보존)"),
                ],
                default="",
                help_text=(
                    "bio_remake 작업 한정. 빈 문자열 = 구 방식(전체 재생성, 호환용). "
                    "full_restyle = 스타일/순서/추가삭제. style_only = 스타일만 패치."
                ),
                max_length=20,
                verbose_name="리뉴얼 모드",
            ),
        ),
    ]
