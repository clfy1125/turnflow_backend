# Generated for ig profile picture caching feature

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0016_autodmcampaign_follow_gate_retry_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="igaccountconnection",
            name="name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="IG /me 응답의 name 필드 (사람이 읽는 표시명)",
                max_length=255,
                verbose_name="Display Name",
            ),
        ),
        migrations.AddField(
            model_name="igaccountconnection",
            name="profile_picture_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="R2/로컬에 캐싱된 안정 URL — 프론트에 노출",
                max_length=1024,
                verbose_name="Cached Profile Picture URL",
            ),
        ),
        migrations.AddField(
            model_name="igaccountconnection",
            name="profile_picture_source_url",
            field=models.TextField(
                blank=True,
                default="",
                help_text="IG /me 가 준 원본 URL — 변경 감지/디버그용 (내부)",
                verbose_name="IG Source Profile Picture URL",
            ),
        ),
        migrations.AddField(
            model_name="igaccountconnection",
            name="profile_picture_synced_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Profile Picture Last Synced At",
            ),
        ),
    ]
