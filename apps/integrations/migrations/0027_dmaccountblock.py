from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0026_igconn_dedupe_unique"),
    ]

    operations = [
        migrations.CreateModel(
            name="DMAccountBlock",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "external_account_id",
                    models.CharField(
                        db_index=True, max_length=255, unique=True, verbose_name="Instagram Account ID"
                    ),
                ),
                (
                    "cooldown_until",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="쿨다운 종료 시각(이 시각까지 발송 차단)"
                    ),
                ),
                ("level", models.IntegerField(default=0, verbose_name="에스컬레이션 레벨(반복 차단 횟수)")),
                (
                    "last_tripped_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="마지막 트립 시각"),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "DM Account Block",
                "verbose_name_plural": "DM Account Blocks",
                "db_table": "dm_account_block",
            },
        ),
        migrations.AddIndex(
            model_name="dmaccountblock",
            index=models.Index(fields=["cooldown_until"], name="dm_acct_block_cooldown_idx"),
        ),
    ]
