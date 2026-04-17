from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0007_payapp_migration"),
    ]

    operations = [
        migrations.AddField(
            model_name="usersubscription",
            name="page_activation_changed_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="하루 1회 제한용 — 마지막으로 페이지 활성화 조정한 시각",
                verbose_name="페이지 활성화 변경 일시",
            ),
        ),
        migrations.AddField(
            model_name="usersubscription",
            name="pro_activated_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="환불 7일 심사용 — 유료 플랜 첫 결제 완료 시각",
                verbose_name="유료 플랜 활성화 일시",
            ),
        ),
    ]
