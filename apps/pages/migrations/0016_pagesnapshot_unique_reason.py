from django.db import migrations, models


def _cleanup_deprecated_reasons(apps, schema_editor):
    """이전 단계에서 잠시 사용됐던 ``restore`` / ``manual`` / ``ai_import_renewal``
    reason 의 스냅샷을 모두 삭제 — 새 unique 제약 (page, reason) 을 걸기 전에
    중복 가능성을 제거하고, 더 이상 사용하지 않는 reason 데이터를 정리한다.
    """
    PageSnapshot = apps.get_model("pages", "PageSnapshot")
    PageSnapshot.objects.filter(
        reason__in=["restore", "manual", "ai_import_renewal"]
    ).delete()


class Migration(migrations.Migration):
    """PageSnapshot.reason choices 갱신 + (page, reason) unique 추가.

    AI 편집 토글 (원본 ↔ 최신 작업물) 을 위해 페이지당 reason 별 최대 1건
    유지. ``AI_EDIT`` 는 원본 (한 번 생성), ``LATEST_AI_RESULT`` 는 매 AI
    호출 직후 upsert.
    """

    dependencies = [
        ("pages", "0015_add_pagesnapshot"),
    ]

    operations = [
        migrations.RunPython(
            _cleanup_deprecated_reasons,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="pagesnapshot",
            name="reason",
            field=models.CharField(
                choices=[
                    ("ai_edit", "AI 편집 직전 원본"),
                    ("latest_ai_result", "최신 AI 작업물"),
                ],
                help_text="어떤 작업 직전에 떠둔 스냅샷인지.",
                max_length=30,
                verbose_name="생성 사유",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="pagesnapshot",
            unique_together={("page", "reason")},
        ),
    ]
