"""IGOAuthState 에 return_to / opener_origin 추가 (iOS OAuth 같은-탭 복귀 + postMessage 오리진 고정).

두 필드 모두 **검증된 값만** 저장된다(허용목록 완전일치) — apps/integrations/oauth_return.py.
기존 행/기존 팝업 플로우는 빈 문자열이므로 동작이 바뀌지 않는다(순수 추가, 되돌리기 안전).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0046_igaccountconnection_biography_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="igoauthstate",
            name="return_to",
            field=models.URLField(
                blank=True, default="", max_length=500, verbose_name="복귀 URL(검증됨)"
            ),
        ),
        migrations.AddField(
            model_name="igoauthstate",
            name="opener_origin",
            field=models.CharField(
                blank=True, default="", max_length=255, verbose_name="opener origin(검증됨)"
            ),
        ),
    ]
