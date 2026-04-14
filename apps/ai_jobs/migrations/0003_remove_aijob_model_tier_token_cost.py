"""
Remove model_tier and token_cost fields from AiJob.

구독 등급별 월 토큰 지급 방식으로 변경 — 모델 티어/건당 비용 개념 제거.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("ai_jobs", "0002_aijob_model_tier_token_cost"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="aijob",
            name="model_tier",
        ),
        migrations.RemoveField(
            model_name="aijob",
            name="token_cost",
        ),
    ]
