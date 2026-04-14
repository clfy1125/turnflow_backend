# Generated manually for PayApp migration

import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0006_add_ai_tokens_monthly_to_plans"),
    ]

    operations = [
        # ── SubscriptionPlan: Remove yearly_price ──
        migrations.RemoveField(
            model_name="subscriptionplan",
            name="yearly_price",
        ),
        # ── UserSubscription: Remove toss fields + billing_cycle ──
        migrations.RemoveField(
            model_name="usersubscription",
            name="toss_customer_key",
        ),
        migrations.RemoveField(
            model_name="usersubscription",
            name="toss_billing_key",
        ),
        migrations.RemoveField(
            model_name="usersubscription",
            name="toss_subscription_id",
        ),
        migrations.RemoveField(
            model_name="usersubscription",
            name="billing_cycle",
        ),
        # ── UserSubscription: Add PayApp fields ──
        migrations.AddField(
            model_name="usersubscription",
            name="payapp_rebill_no",
            field=models.CharField(
                blank=True,
                help_text="rebillRegist 응답의 rebill_no",
                max_length=100,
                null=True,
                verbose_name="PayApp 정기결제 등록번호",
            ),
        ),
        migrations.AddField(
            model_name="usersubscription",
            name="payapp_rebill_expire",
            field=models.DateField(
                blank=True,
                help_text="rebillExpire로 설정한 만료일",
                null=True,
                verbose_name="PayApp 정기결제 만료일",
            ),
        ),
        migrations.AddField(
            model_name="usersubscription",
            name="payapp_pay_url",
            field=models.URLField(
                blank=True,
                help_text="최초 결제 시 프론트가 리다이렉트할 URL",
                max_length=500,
                null=True,
                verbose_name="PayApp 결제 URL",
            ),
        ),
        # ── PaymentHistory: Remove toss fields ──
        migrations.RemoveField(
            model_name="paymenthistory",
            name="toss_payment_key",
        ),
        migrations.RemoveField(
            model_name="paymenthistory",
            name="toss_order_id",
        ),
        # ── PaymentHistory: Add PayApp fields ──
        migrations.AddField(
            model_name="paymenthistory",
            name="payapp_mul_no",
            field=models.CharField(
                blank=True,
                help_text="PayApp mul_no — 멱등 키",
                max_length=100,
                null=True,
                unique=True,
                verbose_name="PayApp 결제요청번호",
            ),
        ),
        migrations.AddField(
            model_name="paymenthistory",
            name="payapp_rebill_no",
            field=models.CharField(
                blank=True,
                max_length=100,
                null=True,
                verbose_name="PayApp 정기결제 등록번호",
            ),
        ),
        migrations.AddField(
            model_name="paymenthistory",
            name="receipt_url",
            field=models.URLField(
                blank=True,
                help_text="PayApp csturl — 카드 결제 시 영수증 URL",
                max_length=500,
                null=True,
                verbose_name="매출전표 URL",
            ),
        ),
        migrations.AddField(
            model_name="paymenthistory",
            name="pay_type_display",
            field=models.CharField(
                blank=True,
                help_text="예: 신용카드, 휴대전화, 카카오페이 등",
                max_length=50,
                null=True,
                verbose_name="결제수단 표시명",
            ),
        ),
        # ── PayAppWebhookLog 모델 생성 ──
        migrations.CreateModel(
            name="PayAppWebhookLog",
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
                    "webhook_type",
                    models.CharField(
                        choices=[
                            ("feedback", "Feedback (결제통보)"),
                            ("fail", "Fail (정기결제 실패)"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "mul_no",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=100,
                        null=True,
                        verbose_name="결제요청번호",
                    ),
                ),
                (
                    "rebill_no",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        max_length=100,
                        null=True,
                        verbose_name="정기결제 등록번호",
                    ),
                ),
                (
                    "pay_state",
                    models.CharField(max_length=10, verbose_name="결제요청 상태"),
                ),
                (
                    "raw_data",
                    models.JSONField(default=dict, verbose_name="수신 데이터 원본"),
                ),
                ("processed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "payapp_webhook_logs",
                "ordering": ["-created_at"],
                "verbose_name": "PayApp 웹훅 로그",
                "verbose_name_plural": "PayApp 웹훅 로그 목록",
                "unique_together": {("mul_no", "pay_state")},
            },
        ),
    ]
