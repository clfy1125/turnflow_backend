from django.db import migrations


def forward(apps, schema_editor):
    """불변식 `is_public ⟹ is_active` 로의 1회성 데이터 정정.

    두 필드가 과거 독립 관리돼, 다운그레이드 시 is_active 만 내려가고 is_public 이
    True 로 남은 페이지("슬롯 없는데 공개(서빙)되는" 페이지)가 존재한다.
    is_active=False 인데 is_public=True 인 페이지를 전부 찾아 is_public=False 로 내린다.
    """
    Page = apps.get_model("pages", "Page")
    Page.objects.filter(is_active=False, is_public=True).update(is_public=False)


def reverse(apps, schema_editor):
    # 어떤 페이지가 원래 공개였는지 복원할 근거가 없으므로 되돌리지 않는다(no-op).
    pass


class Migration(migrations.Migration):
    """is_active=False & is_public=True 페이지의 is_public 을 False 로 backfill.

    코드(free 다운그레이드 축소·page-activation POST)가 이후로는 비활성화 시
    is_public 을 함께 내리므로, 이 정정은 1회만 필요하다.
    """

    dependencies = [
        ("pages", "0022_pagesnapshot_history"),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=reverse),
    ]
