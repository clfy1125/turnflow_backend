"""마케팅 채널 링크: UTM 상한 200·이름 512·완성 URL 2000 + 기존 행 NFC 백필 (2026-07-30).

한국어 UTM 대응 — 배경과 근거는 apps/analytics/migrations/0005 와 apps/analytics/utm.py
docstring 참고. 요점 3가지:

1. UTM 4필드 100 → **200**: 방문 기록(analytics 0005)과 같은 상한이어야 한다. 짧은 쪽이
   있으면 그 길이를 넘는 캠페인명은 링크 저장이 400 이 되고, 그 유입은 영구히
   '저장 안 된 링크(UTM)' 행으로 샌다.
2. name 255 → **512**: 프론트가 `캠페인 · 콘텐츠` 로 자동 조합하므로 200+3+200=403자.
3. url 1000 → **2000**: 한글은 퍼센트 인코딩으로 1글자가 9자가 된다(3바이트 × '%XX').
   200+200자 한글 UTM 이면 1000자를 쉽게 넘겨 INSERT 가 DataError(500) 로 죽는다
   (url 은 write 필드가 아니라 시리얼라이저 길이 검증을 타지 않았다). 상한을 올리고,
   그래도 넘는 경우는 시리얼라이저가 400 으로 막는다(serializers/marketing.py).

안전성: 전부 varchar 길이 **증가**(카탈로그 연산, 테이블 재작성 없음) + 소량 백필.
"""

from django.db import migrations, models


def _nfc_backfill(apps, schema_editor):
    """저장된 링크의 UTM 4필드를 NFC + 공백 표준형으로 정정 (달라지는 행만).

    링크 중복 검증(``utm_*__iexact``)은 DB 값끼리 비교하므로 저장값 자체가 표준형이어야
    "눈에 똑같은 조합"의 링크가 둘 생기는 것을 막을 수 있다.
    """
    from apps.analytics.utm import UTM_FIELDS, normalize_utm

    model = apps.get_model("admin_api", "MarketingChannelLink")
    changed = []
    for row in model.objects.only("id", *UTM_FIELDS).iterator(chunk_size=2000):
        dirty = False
        for field in UTM_FIELDS:
            current = getattr(row, field) or ""
            fixed = normalize_utm(current)
            if fixed != current:
                setattr(row, field, fixed)
                dirty = True
        if dirty:
            changed.append(row)
    if changed:
        model.objects.bulk_update(changed, UTM_FIELDS)


class Migration(migrations.Migration):

    dependencies = [
        ("admin_api", "0006_channel_link_exclude_and_longer_name"),
        # 백필이 analytics.utm 을 쓰지만 모델 의존은 없다. 상한 통일이 한 배포에서
        # 함께 적용되도록 순서만 고정한다.
        ("analytics", "0005_utm_length_200_and_nfc_backfill"),
    ]

    operations = [
        migrations.AlterField(
            model_name="marketingchannellink",
            name="name",
            field=models.CharField(max_length=512, verbose_name="링크 이름"),
        ),
        migrations.AlterField(
            model_name="marketingchannellink",
            name="utm_source",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AlterField(
            model_name="marketingchannellink",
            name="utm_medium",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AlterField(
            model_name="marketingchannellink",
            name="utm_campaign",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AlterField(
            model_name="marketingchannellink",
            name="utm_content",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AlterField(
            model_name="marketingchannellink",
            name="url",
            field=models.URLField(
                help_text=(
                    "base_url + utm 파라미터 조합 (서버 계산, 기존 동일 utm 키는 교체). "
                    "한글 UTM 은 퍼센트 인코딩으로 글자당 9자로 부풀기 때문에"
                    "(1글자=3바이트×'%XX') 2000자 상한을 넘을 수 있다 → "
                    "시리얼라이저가 400 으로 먼저 막는다"
                ),
                max_length=2000,
                verbose_name="완성 URL",
            ),
        ),
        migrations.RunPython(_nfc_backfill, migrations.RunPython.noop),
    ]
