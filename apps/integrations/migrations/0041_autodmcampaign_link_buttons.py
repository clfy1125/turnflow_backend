# Generated for multiple link buttons (Feature B)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0040_backfill_public_reply_posted_count"),
    ]

    operations = [
        migrations.AddField(
            model_name="autodmcampaign",
            name="link_buttons",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "발송 DM 카드에 붙일 web_url 링크 버튼 목록. 형식: "
                    '[{"url": "https://...", "label": "자세히 보기"}] (최대 3개 — Meta button '
                    "template 한도). 비어있지 않으면 legacy link_button_url/link_button_label 보다 "
                    "우선한다. label 은 각 20자, url 은 http/https 만 유효(초과/무효 항목은 무시)."
                ),
                verbose_name="링크 버튼 목록 (최대 3개)",
            ),
        ),
    ]
