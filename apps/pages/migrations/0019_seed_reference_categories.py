from django.db import migrations


# (sort_order, slug, name, icon_emoji)
_SEED_CATEGORIES = [
    (1, "profile-link", "프로필 링크", "🌐"),
    (2, "digital-card", "디지털 명함", "💼"),
    (3, "landing", "랜딩/홈페이지", "🚀"),
    (4, "portfolio", "포트폴리오", "🎨"),
    (5, "brochure", "브로슈어/팜플렛", "📄"),
    (6, "space-booking", "공간 대여/예약", "📅"),
    (7, "group-buy", "공동 구매", "🛒"),
    (8, "invitation", "모바일 초대장", "💌"),
    (9, "affiliate", "제휴 마케팅", "🤝"),
    (10, "commission", "커미션/재능 판매", "✨"),
    (11, "promotion", "홍보/프로모션", "📣"),
]


def forward(apps, schema_editor):
    ReferenceCategory = apps.get_model("pages", "ReferenceCategory")
    for sort_order, slug, name, emoji in _SEED_CATEGORIES:
        ReferenceCategory.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "icon_emoji": emoji,
                "sort_order": sort_order,
                "is_active": True,
            },
        )


def reverse(apps, schema_editor):
    ReferenceCategory = apps.get_model("pages", "ReferenceCategory")
    ReferenceCategory.objects.filter(
        slug__in=[slug for _, slug, *_ in _SEED_CATEGORIES]
    ).delete()


class Migration(migrations.Migration):
    """초기 카테고리 11개 시드.

    update_or_create 를 써서 멱등성 확보 — 운영팀이 이미 손으로 만든 카테고리가
    있어도 슬러그가 같으면 sort_order/name/emoji 만 갱신, 페이지 매핑은 보존.
    """

    dependencies = [
        ("pages", "0018_page_reference_fields"),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=reverse),
    ]
