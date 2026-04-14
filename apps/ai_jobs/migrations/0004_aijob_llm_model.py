"""
Add llm_model field to AiJob for model selection (gemma / gpt5).
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_jobs", "0003_remove_aijob_model_tier_token_cost"),
    ]

    operations = [
        migrations.AddField(
            model_name="aijob",
            name="llm_model",
            field=models.CharField(
                choices=[("gemma", "Gemma (기본)"), ("gpt5", "GPT-5.4")],
                default="gemma",
                max_length=20,
                verbose_name="LLM 모델",
            ),
        ),
    ]
