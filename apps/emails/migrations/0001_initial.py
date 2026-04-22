from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("authentication", "0003_user_email_verification"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "key",
                    models.CharField(
                        choices=[
                            ("email_verification", "email_verification"),
                            ("password_reset", "password_reset"),
                            ("welcome", "welcome"),
                            ("onboarding_day_3", "onboarding_day_3"),
                            ("onboarding_day_7", "onboarding_day_7"),
                            ("onboarding_day_14", "onboarding_day_14"),
                        ],
                        max_length=64,
                        unique=True,
                        verbose_name="Template Key",
                    ),
                ),
                ("subject", models.CharField(max_length=255, verbose_name="제목 (subject line)")),
                ("html_body", models.TextField(verbose_name="HTML 본문")),
                (
                    "text_body",
                    models.TextField(
                        blank=True,
                        help_text="순수 텍스트 fallback. 비워두면 HTML에서 자동 추출.",
                        verbose_name="텍스트 본문",
                    ),
                ),
                (
                    "from_name",
                    models.CharField(
                        blank=True,
                        help_text="비워두면 settings.AWS_SES_FROM_NAME 사용",
                        max_length=100,
                        verbose_name="발신자 이름 (override)",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="활성화")),
                (
                    "available_variables",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="이 템플릿에서 사용 가능한 {{변수}} 목록 (자동 채움)",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_email_templates",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Email Template",
                "verbose_name_plural": "Email Templates",
                "db_table": "email_templates",
                "ordering": ["key"],
            },
        ),
        migrations.CreateModel(
            name="EmailToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "purpose",
                    models.CharField(
                        choices=[
                            ("email_verify", "Email Verification"),
                            ("password_reset", "Password Reset"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "code",
                    models.CharField(
                        help_text="6-digit numeric (plaintext, short TTL)", max_length=6
                    ),
                ),
                (
                    "token_hash",
                    models.CharField(
                        help_text="sha256(token) — token itself never stored",
                        max_length=64,
                        unique=True,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("request_ip", models.GenericIPAddressField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="email_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Email Token",
                "verbose_name_plural": "Email Tokens",
                "db_table": "email_tokens",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="emailtoken",
            index=models.Index(
                fields=["user", "purpose", "used_at"],
                name="email_token_user_purpose_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="emailtoken",
            index=models.Index(fields=["token_hash"], name="email_token_hash_idx"),
        ),
        migrations.CreateModel(
            name="EmailLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("template_key", models.CharField(db_index=True, max_length=64)),
                ("to_email", models.EmailField(max_length=254)),
                ("from_email", models.EmailField(max_length=254)),
                ("subject", models.CharField(max_length=255)),
                ("rendered_html", models.TextField(blank=True)),
                ("rendered_text", models.TextField(blank=True)),
                ("context_snapshot", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                            ("bounced", "Bounced"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("ses_message_id", models.CharField(blank=True, db_index=True, max_length=255)),
                ("error_message", models.TextField(blank=True)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                (
                    "template",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="logs",
                        to="emails.emailtemplate",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="email_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Email Log",
                "verbose_name_plural": "Email Logs",
                "db_table": "email_logs",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="emaillog",
            index=models.Index(
                fields=["template_key", "status"], name="email_log_tmpl_status_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="emaillog",
            index=models.Index(
                fields=["to_email", "created_at"], name="email_log_to_created_idx"
            ),
        ),
        migrations.CreateModel(
            name="OnboardingSchedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("template_key", models.CharField(max_length=64)),
                ("scheduled_for", models.DateTimeField(db_index=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="onboarding_schedules",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "email_onboarding_schedules",
                "ordering": ["scheduled_for"],
                "unique_together": {("user", "template_key")},
            },
        ),
        migrations.AddIndex(
            model_name="onboardingschedule",
            index=models.Index(
                fields=["scheduled_for", "sent_at", "cancelled_at"],
                name="email_onboarding_sched_idx",
            ),
        ),
    ]
