from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0012_free_plan_custom_css"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptionplan",
            name="list_price",
            field=models.IntegerField(
                default=0,
                help_text="정가 (원). 할인 표시용 — monthly_price보다 크면 할인 판매 중",
            ),
        ),
        migrations.AlterField(
            model_name="subscriptionplan",
            name="monthly_price",
            field=models.IntegerField(default=0, help_text="월 요금 (원) — 현재 판매가"),
        ),
    ]
