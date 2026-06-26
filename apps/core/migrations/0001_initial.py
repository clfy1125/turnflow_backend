from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="SiteControl",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("active_site", models.CharField(default="colo", max_length=32, verbose_name="권위 사이트")),
                ("epoch", models.BigIntegerField(default=1, verbose_name="펜싱 epoch")),
                (
                    "mode",
                    models.CharField(
                        choices=[("live", "Live"), ("maintenance", "Maintenance")],
                        default="live",
                        max_length=16,
                        verbose_name="모드",
                    ),
                ),
                ("restore_complete", models.BooleanField(default=True, verbose_name="복구 완료")),
                ("note", models.CharField(blank=True, default="", max_length=255, verbose_name="비고")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="갱신 시각")),
            ],
            options={
                "verbose_name": "Site Control",
                "verbose_name_plural": "Site Control",
                "db_table": "site_control",
            },
        ),
        migrations.CreateModel(
            name="ScheduledJob",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("key", models.CharField(max_length=128, unique=True, verbose_name="잡 키")),
                ("task", models.CharField(max_length=255, verbose_name="Celery 태스크명")),
                (
                    "interval_seconds",
                    models.PositiveIntegerField(blank=True, null=True, verbose_name="주기(초)"),
                ),
                ("cron_minute", models.CharField(blank=True, default="", max_length=64)),
                ("cron_hour", models.CharField(blank=True, default="", max_length=64)),
                ("cron_day_of_week", models.CharField(blank=True, default="", max_length=64)),
                (
                    "queue",
                    models.CharField(
                        blank=True, default="", max_length=64, verbose_name="큐(빈값=route-by-name)"
                    ),
                ),
                ("enabled", models.BooleanField(default=True, verbose_name="활성")),
                ("next_due_at", models.DateTimeField(db_index=True, verbose_name="다음 실행 예정 시각")),
                ("last_run_at", models.DateTimeField(blank=True, null=True, verbose_name="마지막 실행")),
                ("last_status", models.CharField(blank=True, default="", max_length=32)),
                ("last_error", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Scheduled Job",
                "verbose_name_plural": "Scheduled Jobs",
                "db_table": "core_scheduled_job",
                "ordering": ["key"],
            },
        ),
        migrations.AddIndex(
            model_name="scheduledjob",
            index=models.Index(fields=["enabled", "next_due_at"], name="core_sched_enabled_due"),
        ),
    ]
