# Generated for 실패 DM 복구(recovery) 기본 활성화 + 프로 전용 안내

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0034_autodmcampaign_recovery_keyword_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="autodmcampaign",
            name="recovery_reply_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "opening 비공개답글이 2534025(비팔로워 채널 미개설)로 실패하면 완전 실패로 두지 않고, "
                    "댓글에 '다시 보내드릴게요' 안내 대댓글을 남긴다. 사용자가 이 계정으로 DM 을 보내오면 "
                    "열린 24h 창으로 opening DM 을 재전송한다. **프로 전용**(dm_recovery 기능) — "
                    "미보유 플랜에서는 켜져 있어도 동작하지 않고 해당 DM 은 일반 실패로 종결된다."
                ),
                verbose_name="실패 DM 복구 사용",
            ),
        ),
    ]
