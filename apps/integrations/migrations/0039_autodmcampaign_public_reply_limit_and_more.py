# Generated for public reply limit (Feature A)

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0038_clear_v1_recovery_templates"),
    ]

    operations = [
        migrations.AddField(
            model_name="autodmcampaign",
            name="public_reply_limit",
            field=models.IntegerField(
                default=200,
                help_text=(
                    "이 캠페인이 게시하는 성공 공개 답글(대댓글)의 누적 상한. 도달하면 이후 공개 "
                    "답글을 더 게시하지 않는다(DM 발송 자체엔 영향 없음). 0 = 무제한. "
                    "복구 안내 대댓글(recovery)은 이 상한의 집계·차단 대상에서 제외된다."
                ),
                validators=[django.core.validators.MinValueValidator(0)],
                verbose_name="공개 답글 최대 게시 수",
            ),
        ),
        migrations.AddField(
            model_name="autodmcampaign",
            name="public_reply_posted_count",
            field=models.IntegerField(
                default=0,
                help_text=(
                    "성공적으로 게시된 공개 답글(대댓글) 누적 수 — public_reply_limit 판정 기준. "
                    "복구 안내 대댓글은 포함하지 않는다. 원자적으로 증가한다."
                ),
                verbose_name="공개 답글 게시 누계",
            ),
        ),
    ]
