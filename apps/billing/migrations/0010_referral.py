import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0009_update_plan_features"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReferralCode",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "code",
                    models.CharField(
                        help_text="입력 시 대소문자 무시. 저장은 대문자로 정규화.",
                        max_length=50,
                        unique=True,
                        verbose_name="코드",
                    ),
                ),
                (
                    "description",
                    models.CharField(
                        blank=True, default="", max_length=200, verbose_name="설명"
                    ),
                ),
                (
                    "trial_days",
                    models.PositiveIntegerField(default=30, verbose_name="트라이얼 기간(일)"),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="활성")),
                (
                    "max_uses",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="null이면 무제한",
                        null=True,
                        verbose_name="최대 사용 횟수",
                    ),
                ),
                (
                    "current_uses",
                    models.PositiveIntegerField(default=0, verbose_name="현재 사용 횟수"),
                ),
                (
                    "valid_from",
                    models.DateTimeField(blank=True, null=True, verbose_name="사용 시작 시각"),
                ),
                (
                    "valid_until",
                    models.DateTimeField(blank=True, null=True, verbose_name="사용 종료 시각"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "target_plan",
                    models.ForeignKey(
                        help_text="레퍼럴 사용 시 일시 부여할 플랜 (보통 pro)",
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="referral_codes",
                        to="billing.subscriptionplan",
                        verbose_name="트라이얼 대상 플랜",
                    ),
                ),
            ],
            options={
                "verbose_name": "레퍼럴 코드",
                "verbose_name_plural": "레퍼럴 코드 목록",
                "db_table": "referral_codes",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ReferralRedemption",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("trial_started_at", models.DateTimeField(verbose_name="트라이얼 시작")),
                ("trial_ends_at", models.DateTimeField(verbose_name="트라이얼 종료")),
                (
                    "converted_to_paid",
                    models.BooleanField(
                        default=False,
                        help_text="트라이얼 후 정기결제 완료 시 True",
                        verbose_name="유료 전환 여부",
                    ),
                ),
                (
                    "converted_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="유료 전환 시각"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "referral_code",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="redemptions",
                        to="billing.referralcode",
                        verbose_name="레퍼럴 코드",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="referral_redemption",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="사용자",
                    ),
                ),
            ],
            options={
                "verbose_name": "레퍼럴 사용 이력",
                "verbose_name_plural": "레퍼럴 사용 이력 목록",
                "db_table": "referral_redemptions",
                "ordering": ["-created_at"],
            },
        ),
    ]
