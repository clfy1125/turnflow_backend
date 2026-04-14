from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_jobs", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="aijob",
            name="model_tier",
            field=models.CharField(
                choices=[("basic", "기본 모델"), ("pro", "프로 모델"), ("pro_plus", "프로 플러스 모델")],
                default="basic",
                max_length=20,
                verbose_name="모델 티어",
            ),
        ),
        migrations.AddField(
            model_name="aijob",
            name="token_cost",
            field=models.PositiveIntegerField(
                default=0,
                help_text="이 작업에 소모되는 토큰 수 (성공 시에만 차감)",
                verbose_name="토큰 비용",
            ),
        ),
    ]
