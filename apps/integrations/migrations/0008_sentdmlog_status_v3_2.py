"""
SentDMLog status choices 확장 (v3.2 — RATE_LIMITED 추가)

- failed_api 는 legacy alias 로 유지 (data 호환)
- rate_limited 신규 — Meta 4/17/32/613/368/1/2/5xx 매핑용
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0007_sentdmlog_99_9_guarantee"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sentdmlog",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "큐 대기"),
                    ("submitting", "API 호출 중"),
                    ("accepted", "Meta 접수됨"),
                    ("delivered", "도착 확인"),
                    ("read", "읽음 확인"),
                    ("pending", "대기중(legacy)"),
                    ("sent", "발송완료(legacy)"),
                    ("failed", "발송실패(legacy)"),
                    ("skipped", "건너뜀"),
                    ("failed_token", "토큰 만료/세션 무효"),
                    ("failed_window", "24h 윈도우 만료"),
                    ("failed_param", "파라미터 오류"),
                    ("rate_limited", "Meta 응답 대기 (지연)"),
                    ("failed_no_trace", "도착 미확인 (자가 점검 필요)"),
                    ("failed_api", "API 실패(legacy)"),
                ],
                default="queued",
                max_length=20,
                verbose_name="상태",
            ),
        ),
    ]
