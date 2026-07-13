from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("emails", "0003_rename_email_log_tmpl_status_idx_email_logs_templat_2ea189_idx_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="emailtemplate",
            name="from_name",
            field=models.CharField(
                blank=True,
                help_text="비워두면 settings.EMAIL_FROM_NAME 사용",
                max_length=100,
                verbose_name="발신자 이름 (override)",
            ),
        ),
    ]
