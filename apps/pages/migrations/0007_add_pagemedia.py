from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("pages", "0006_add_pagesubscription"),
    ]

    operations = [
        migrations.CreateModel(
            name="PageMedia",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "page",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="media_files",
                        to="pages.page",
                        verbose_name="페이지",
                    ),
                ),
                ("file", models.FileField(upload_to="pages/%Y/%m/", verbose_name="파일")),
                ("original_name", models.CharField(max_length=500, verbose_name="원본 파일명")),
                ("mime_type", models.CharField(max_length=100, verbose_name="MIME 타입")),
                ("size", models.PositiveIntegerField(verbose_name="파일 크기 (bytes)")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="업로드 일시")),
            ],
            options={
                "verbose_name": "미디어 파일",
                "verbose_name_plural": "미디어 파일 목록",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="pagemedia",
            index=models.Index(fields=["page", "created_at"], name="pages_media_page_created_idx"),
        ),
    ]
