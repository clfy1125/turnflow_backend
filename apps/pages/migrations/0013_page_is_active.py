from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pages", "0012_add_original_file_crop_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="page",
            name="is_active",
            field=models.BooleanField(
                default=True,
                help_text="비활성 페이지는 공개 URL로 접근 불가. 다운그레이드 시 플랜 한도에 맞춰 비활성화됩니다.",
                verbose_name="활성 상태",
            ),
        ),
    ]
