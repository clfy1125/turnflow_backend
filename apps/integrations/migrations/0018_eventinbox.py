# P2b — webhook idempotency ledger (additive table; old code ignores it → backward compatible).
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0017_ig_profile_picture"),
    ]

    operations = [
        migrations.CreateModel(
            name="EventInbox",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("event_key", models.CharField(max_length=255, unique=True, verbose_name="이벤트 키")),
                ("event_type", models.CharField(max_length=32, verbose_name="이벤트 타입")),
                ("payload", models.JSONField(blank=True, default=dict, verbose_name="이벤트 페이로드")),
                ("received_at", models.DateTimeField(auto_now_add=True, verbose_name="수신 시각")),
                ("processed_at", models.DateTimeField(blank=True, null=True, verbose_name="처리 완료 시각")),
            ],
            options={
                "verbose_name": "Webhook Event Inbox",
                "verbose_name_plural": "Webhook Event Inbox",
                "db_table": "webhook_event_inbox",
            },
        ),
        migrations.AddIndex(
            model_name="eventinbox",
            index=models.Index(fields=["processed_at"], name="webhook_eve_process_idx"),
        ),
        migrations.AddIndex(
            model_name="eventinbox",
            index=models.Index(fields=["received_at"], name="webhook_eve_receive_idx"),
        ),
    ]
