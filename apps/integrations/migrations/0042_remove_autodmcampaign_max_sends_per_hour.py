from django.db import migrations


class Migration(migrations.Migration):
    """캠페인 시간당 한도(max_sends_per_hour) 컬럼 제거.

    v4.3(2026-07-09)부터 발송 속도는 계정 단위 스무스 페이서(dm_pacer)가 담당하며
    이 필드는 강제되지 않는 죽은 값이었다. API/어드민/프론트 노출까지 모두 제거하면서
    DB 컬럼도 함께 드롭한다. (PostgreSQL DROP COLUMN = 빠른 카탈로그 연산, 테이블 재작성 없음)
    """

    dependencies = [
        ("integrations", "0041_autodmcampaign_link_buttons"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="autodmcampaign",
            name="max_sends_per_hour",
        ),
    ]
