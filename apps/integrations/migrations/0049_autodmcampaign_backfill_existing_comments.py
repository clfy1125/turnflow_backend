"""캠페인 '기존 댓글 소급 발송' 필드 추가.

⚠️ 배포 안전장치 —
``backfill_started_at IS NULL`` 이 소급 발송 대기열이다(스캐너가 이걸로 미처리분을 고른다).
필드를 그냥 추가하면 **기존 활성 캠페인 전부가 즉시 대기열에 올라와** 배포 직후 한꺼번에
소급 발송이 터진다. 그래서 이 마이그레이션에서 기존 행을 전부 "이미 소급함" 으로 표시해
대기열에서 빼둔다. 신규 캠페인부터 적용되는 게 의도된 동작이다.
"""

from django.db import migrations, models
from django.utils import timezone


def mark_existing_as_done(apps, schema_editor):
    """기존 캠페인은 소급 대상이 아니다 — 락을 미리 채워 대기열에서 제외."""
    AutoDMCampaign = apps.get_model("integrations", "AutoDMCampaign")
    AutoDMCampaign.objects.filter(backfill_started_at__isnull=True).update(
        backfill_started_at=timezone.now(),
        backfill_stats={"skipped": "pre_existing_campaign"},
    )


def unmark(apps, schema_editor):
    """되돌리기 — 필드가 사라지므로 실질 no-op."""


class Migration(migrations.Migration):

    dependencies = [("integrations", "0048_autodmcampaign_thumbnail_source_url_and_more")]

    operations = [
        migrations.AddField(
            model_name="autodmcampaign",
            name="backfill_existing_comments",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "캠페인이 실제로 시작되는 시점에 이미 달려 있던 댓글에도 DM 을 발송한다. "
                    "범위는 게시물 업로드 시각부터이되 Private Reply 7일 창으로 잘린다. "
                    "1회만 실행되며(backfill_started_at 락), 일시중지 후 재개해도 반복되지 않는다."
                ),
                verbose_name="기존 댓글 소급 발송",
            ),
        ),
        migrations.AddField(
            model_name="autodmcampaign",
            name="backfill_started_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    "소급 발송을 시작한 시각. **NULL 인 행이 곧 대기열**이라 스캐너가 이걸로 "
                    "미처리분을 고른다 → 값이 차는 순간 1회성 락이 된다. 예약 캠페인은 예약 "
                    "시작 시각이 도래한 tick 에서 채워진다."
                ),
                verbose_name="소급 발송 실행 시각",
            ),
        ),
        migrations.AddField(
            model_name="autodmcampaign",
            name="backfill_stats",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="{scanned, enqueued, skipped, capped, floor, finished_at} — 프론트 표시용.",
                verbose_name="소급 발송 결과",
            ),
        ),
        migrations.RunPython(mark_existing_as_done, unmark),
    ]
