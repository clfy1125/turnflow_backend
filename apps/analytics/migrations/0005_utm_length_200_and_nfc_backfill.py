"""UTM 4필드 상한 200 통일 + 기존 행 NFC 표준형 백필 (한국어 UTM 대응, 2026-07-30).

왜:
- 방문(LandingVisit)/가입(SignupAttribution)은 campaign·content 를 150 으로 받았는데
  어드민 저장 링크(MarketingChannelLink)는 100 이었다 → 101~150자 캠페인명은 방문은
  기록되지만 링크를 저장할 수 없어(400) 영구히 '저장 안 된 링크(UTM)' 로 샌다.
  세 테이블을 **200 으로 통일**한다.
- macOS/iOS 복붙 한글은 NFD(자모 분해)로 저장될 수 있다. 화면상 동일하지만 다른
  문자열이라 대시보드의 4-튜플 매칭이 조용히 실패한다. 앞으로는 저장 시 정규화되며
  (analytics.utm.normalize_utm), 이 마이그레이션이 **기존 행을 NFC 로 되돌린다**.
  (읽기 시점에도 정규화하므로 백필 없이도 매칭은 되지만, 링크 중복 검증(iexact)은
  DB 값끼리 비교하므로 저장값 자체가 표준형이어야 한다.)

안전성: varchar 길이 **증가**는 PostgreSQL 에서 카탈로그만 바꾸는 연산(테이블 재작성/
ACCESS EXCLUSIVE 장기 락 없음). 백필은 값이 실제로 달라지는 행만 UPDATE 한다.
"""

from django.db import migrations, models


def _nfc_backfill(apps, schema_editor):
    """UTM 4필드를 NFC + 공백 표준형으로 정정 (달라지는 행만)."""
    from apps.analytics.utm import UTM_FIELDS, normalize_utm

    for model_name in ("LandingVisit", "SignupAttribution"):
        model = apps.get_model("analytics", model_name)
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
            if len(changed) >= 500:
                model.objects.bulk_update(changed, UTM_FIELDS)
                changed = []
        if changed:
            model.objects.bulk_update(changed, UTM_FIELDS)


class Migration(migrations.Migration):

    dependencies = [
        ("analytics", "0004_cancellationevent_offer_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="landingvisit",
            name="utm_source",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="utm_source"),
        ),
        migrations.AlterField(
            model_name="landingvisit",
            name="utm_medium",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="utm_medium"),
        ),
        migrations.AlterField(
            model_name="landingvisit",
            name="utm_campaign",
            field=models.CharField(
                blank=True, default="", max_length=200, verbose_name="utm_campaign"
            ),
        ),
        migrations.AlterField(
            model_name="landingvisit",
            name="utm_content",
            field=models.CharField(
                blank=True, default="", max_length=200, verbose_name="utm_content"
            ),
        ),
        migrations.AlterField(
            model_name="signupattribution",
            name="utm_source",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="utm_source"),
        ),
        migrations.AlterField(
            model_name="signupattribution",
            name="utm_medium",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="utm_medium"),
        ),
        migrations.AlterField(
            model_name="signupattribution",
            name="utm_campaign",
            field=models.CharField(
                blank=True, default="", max_length=200, verbose_name="utm_campaign"
            ),
        ),
        migrations.AlterField(
            model_name="signupattribution",
            name="utm_content",
            field=models.CharField(
                blank=True, default="", max_length=200, verbose_name="utm_content"
            ),
        ),
        # 되돌릴 필요가 없다(정규화는 표시상 동일한 값으로의 정정) → reverse 는 no-op.
        migrations.RunPython(_nfc_backfill, migrations.RunPython.noop),
    ]
