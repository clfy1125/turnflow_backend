"""AutoDMCampaign.total_unconfirmed — FAILED_NO_TRACE 전용 카운터.

도착 미확인(FAILED_NO_TRACE)은 '실패'가 아니라 '미확인'이므로
total_failed / success_rate 와 분리 집계한다.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0022_autodmcampaign_link_button_label_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="autodmcampaign",
            name="total_unconfirmed",
            field=models.IntegerField(
                default=0,
                help_text=(
                    "FAILED_NO_TRACE (200 접수됐으나 35분 내 도착 미확인). "
                    "'실패'가 아니라 '미확인' 이므로 total_failed / success_rate 와 분리 집계."
                ),
                verbose_name="총 도착미확인 수",
            ),
        ),
    ]
