"""
Data migration: Grant 30 initial AI tokens to all existing users.
"""

from django.db import migrations


def grant_initial_tokens(apps, schema_editor):
    User = apps.get_model("authentication", "User")
    AiTokenBalance = apps.get_model("billing", "AiTokenBalance")
    AiTokenLedger = apps.get_model("billing", "AiTokenLedger")

    for user in User.objects.all():
        if not AiTokenBalance.objects.filter(user=user).exists():
            balance = AiTokenBalance.objects.create(user=user, balance=30)
            AiTokenLedger.objects.create(
                user=user,
                amount=30,
                balance_after=30,
                description="초기 토큰 지급",
            )


def reverse_grant(apps, schema_editor):
    AiTokenBalance = apps.get_model("billing", "AiTokenBalance")
    AiTokenLedger = apps.get_model("billing", "AiTokenLedger")
    AiTokenLedger.objects.filter(description="초기 토큰 지급").delete()
    AiTokenBalance.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0004_aitokenbalance_aitokenledger"),
        ("authentication", "0002_alter_user_managers"),
    ]

    operations = [
        migrations.RunPython(grant_initial_tokens, reverse_grant),
    ]
