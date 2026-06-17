# Generated for DM 예약 발송 (활성 기간 한정 + 자동 종료)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0019_sentdmlog_recipient_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="autodmcampaign",
            name="scheduled_start_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "이 시각 이후부터 발송 시작 (status=active 여도 이 시각 전에는 발송하지 않음). "
                    "비우면 즉시 시작. ISO8601 타임존 포함 권장 (예: 2026-07-01T09:00:00+09:00)."
                ),
                verbose_name="예약 시작일시",
            ),
        ),
        migrations.AddField(
            model_name="autodmcampaign",
            name="scheduled_end_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "이 시각 이후 캠페인 자동 종료(status=completed, 발송 중단). "
                    "비우면 수동 종료 전까지 무기한. scheduled_start_at 이 있으면 그보다 미래여야 함."
                ),
                verbose_name="예약 종료일시",
            ),
        ),
    ]
