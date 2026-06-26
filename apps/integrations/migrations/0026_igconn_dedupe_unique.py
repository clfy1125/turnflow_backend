"""P5 — IGAccountConnection 재연동 고아화 방지.

(workspace, external_account_id) 중복 행을 정리(캠페인/SeenComment/SpamFilterConfig 를
canonical 행으로 repoint 후 스테일 삭제)하고, 동일 쌍에 UNIQUE 제약을 추가한다.

CASCADE FK 이므로 반드시 'FK repoint → 스테일 connection 삭제' 순서로 처리한다.
"""

from django.db import migrations, models
from django.db.models import Count


def dedupe_connections(apps, schema_editor):
    IGAccountConnection = apps.get_model("integrations", "IGAccountConnection")
    AutoDMCampaign = apps.get_model("integrations", "AutoDMCampaign")
    SeenComment = apps.get_model("integrations", "SeenComment")
    SpamFilterConfig = apps.get_model("integrations", "SpamFilterConfig")

    dup_groups = (
        IGAccountConnection.objects.values("workspace_id", "external_account_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )

    for grp in dup_groups:
        rows = list(
            IGAccountConnection.objects.filter(
                workspace_id=grp["workspace_id"],
                external_account_id=grp["external_account_id"],
            ).order_by("-created_at")
        )
        if len(rows) < 2:
            continue
        # canonical = ACTIVE 중 최신, 없으면 그냥 최신.
        canonical = next((r for r in rows if r.status == "active"), rows[0])
        stale_ids = [r.id for r in rows if r.id != canonical.id]
        if not stale_ids:
            continue

        # 1) 캠페인 repoint (SentDMLog 는 campaign FK 로 자동 따라감).
        AutoDMCampaign.objects.filter(ig_connection_id__in=stale_ids).update(
            ig_connection_id=canonical.id
        )

        # 2) SeenComment repoint — UNIQUE(ig_connection, comment_id) 충돌 시 삭제.
        for sc in SeenComment.objects.filter(ig_connection_id__in=stale_ids):
            clash = SeenComment.objects.filter(
                ig_connection_id=canonical.id, comment_id=sc.comment_id
            ).exists()
            if clash:
                sc.delete()
            else:
                sc.ig_connection_id = canonical.id
                sc.save(update_fields=["ig_connection_id"])

        # 3) SpamFilterConfig (OneToOne) — canonical 에 없으면 1개 이전, 나머지는 삭제.
        canon_has_spam = SpamFilterConfig.objects.filter(
            ig_connection_id=canonical.id
        ).exists()
        for sf in SpamFilterConfig.objects.filter(ig_connection_id__in=stale_ids):
            if not canon_has_spam:
                sf.ig_connection_id = canonical.id
                sf.save(update_fields=["ig_connection_id"])
                canon_has_spam = True
            else:
                sf.delete()

        # 4) FK 모두 옮긴 뒤 스테일 connection 삭제.
        IGAccountConnection.objects.filter(id__in=stale_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0025_seencomment_seencomment_uq_seen_comment_conn_comment"),
    ]

    operations = [
        migrations.RunPython(dedupe_connections, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="igaccountconnection",
            constraint=models.UniqueConstraint(
                fields=["workspace", "external_account_id"],
                name="uq_igconn_ws_account",
            ),
        ),
    ]
