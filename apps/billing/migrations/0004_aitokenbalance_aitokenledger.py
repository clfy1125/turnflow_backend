import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0003_seed_subscription_plans"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AiTokenBalance",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("balance", models.IntegerField(default=0, help_text="현재 사용 가능한 AI 토큰 수", verbose_name="토큰 잔액")),
                ("total_used", models.IntegerField(default=0, help_text="서비스 가입 이후 총 사용한 토큰 수", verbose_name="총 사용량")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="ai_token_balance", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "AI 토큰 잔액",
                "verbose_name_plural": "AI 토큰 잔액 목록",
                "db_table": "ai_token_balances",
            },
        ),
        migrations.CreateModel(
            name="AiTokenLedger",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("amount", models.IntegerField(help_text="양수=충전, 음수=차감", verbose_name="변동량")),
                ("balance_after", models.IntegerField(verbose_name="변동 후 잔액")),
                ("description", models.CharField(blank=True, default="", max_length=200, verbose_name="설명")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_token_ledger", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "AI 토큰 내역",
                "verbose_name_plural": "AI 토큰 내역 목록",
                "db_table": "ai_token_ledger",
                "ordering": ["-created_at"],
            },
        ),
    ]
