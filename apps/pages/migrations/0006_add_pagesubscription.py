from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("pages", "0005_add_contactinquiry"),
    ]

    operations = [
        migrations.CreateModel(
            name="PageSubscription",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "page",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="subscriptions",
                        to="pages.page",
                        verbose_name="페이지",
                    ),
                ),
                ("name", models.CharField(blank=True, default="", max_length=100, verbose_name="이름")),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("page_subscribe", "페이지 구독"),
                            ("newsletter", "뉴스레터"),
                            ("event", "이벤트 알림"),
                            ("other", "기타"),
                        ],
                        default="page_subscribe",
                        max_length=20,
                        verbose_name="분류",
                    ),
                ),
                ("email", models.EmailField(max_length=254, verbose_name="이메일")),
                ("phone", models.CharField(blank=True, default="", max_length=30, verbose_name="휴대폰번호")),
                ("agreed_to_terms", models.BooleanField(default=False, verbose_name="개인정보 수집 동의")),
                (
                    "memo",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="구독자에게 노출되지 않는 관리자 전용 메모입니다.",
                        verbose_name="관리자 메모",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="구독 일시")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "구독자",
                "verbose_name_plural": "구독자 목록",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="pagesubscription",
            index=models.Index(fields=["page", "created_at"], name="pages_pages_page_id_creat_idx"),
        ),
        migrations.AddIndex(
            model_name="pagesubscription",
            index=models.Index(fields=["page", "category"], name="pages_pages_page_id_categ_idx"),
        ),
    ]
