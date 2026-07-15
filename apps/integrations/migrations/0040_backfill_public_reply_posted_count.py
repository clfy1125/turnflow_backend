"""public_reply_posted_count 백필 — 과거 성공 공개 답글 수로 카운터 초기화.

기존 캠페인(운영 중 ~10개)에도 public_reply_limit(기본 200)이 소급 적용되므로, 지금까지
게시된 성공 공개 답글 수를 세어 카운터를 맞춘다. 성공 공개 답글 = public_reply_id 가 세팅된
로그(복구 안내 답글은 recovery_reply_id 만 세팅하고 public_reply_id="" → 자동 제외되며,
이는 상한 집계 정의와 정확히 일치한다).

⚠️ 이미 200 건 이상 게시한 캠페인은 배포 직후 공개 답글이 멈춘다(제품 결정 — 소급 적용).
   배포 전 대상 캠페인 목록은 배포 체크리스트의 SQL 로 확인한다.

멱등 — 재실행해도 결과 동일(절대값으로 세팅). 역방향은 no-op.
"""

from django.db import migrations
from django.db.models import Count


def backfill(apps, schema_editor):
    AutoDMCampaign = apps.get_model("integrations", "AutoDMCampaign")
    SentDMLog = apps.get_model("integrations", "SentDMLog")
    counts = (
        SentDMLog.objects.exclude(public_reply_id="").values("campaign_id").annotate(n=Count("id"))
    )
    for row in counts:
        AutoDMCampaign.objects.filter(pk=row["campaign_id"]).update(
            public_reply_posted_count=row["n"]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0039_autodmcampaign_public_reply_limit_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
