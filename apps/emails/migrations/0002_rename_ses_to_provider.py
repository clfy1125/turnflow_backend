from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("emails", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="emaillog",
            old_name="ses_message_id",
            new_name="provider_message_id",
        ),
        migrations.AlterField(
            model_name="emailtemplate",
            name="from_name",
            field=models.CharField(
                blank=True,
                help_text="비워두면 settings.RESEND_FROM_NAME 사용",
                max_length=100,
                verbose_name="발신자 이름 (override)",
            ),
        ),
    ]
