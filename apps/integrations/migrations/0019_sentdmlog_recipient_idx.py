# P2a — recipient_user_id composite index on sent_dm_logs for the echo-match fallback query.
# Built with CREATE INDEX CONCURRENTLY (no write lock) → migration must be non-atomic.
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    # CONCURRENTLY cannot run inside a transaction block.
    atomic = False

    dependencies = [
        ("integrations", "0018_eventinbox"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="sentdmlog",
            index=models.Index(
                fields=["recipient_user_id", "status", "-accepted_at"],
                name="dm_log_recipient_status_idx",
            ),
        ),
    ]
