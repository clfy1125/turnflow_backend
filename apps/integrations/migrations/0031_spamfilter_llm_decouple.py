"""스팸 필터 DM 캠페인 분리 + LLM(gemma) 재구축.

- SpamFilterConfig: auto_hide_enabled(기본 OFF), use_llm(기본 ON) 추가
- SpamCommentLog: CLEAN 상태 + confidence/spam_category/engine + hidden_at 인덱스
- SpamCommentLog: UNIQUE(spam_filter, comment_id) 멱등 제약 (중복 행 정리 후 추가)
"""

from django.db import migrations, models


def dedupe_spam_logs(apps, schema_editor):
    """UNIQUE(spam_filter, comment_id) 추가 전, 기존 중복 행 제거.

    현재(무제약) 코드가 같은 comment 를 여러 번 기록했을 수 있으므로,
    (spam_filter, comment_id) 그룹별로 최신 1건만 남기고 나머지 삭제. 멱등.
    """
    from django.db.models import Count

    SpamCommentLog = apps.get_model("integrations", "SpamCommentLog")

    dup_groups = (
        SpamCommentLog.objects.values("spam_filter_id", "comment_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    for grp in dup_groups:
        qs = SpamCommentLog.objects.filter(
            spam_filter_id=grp["spam_filter_id"], comment_id=grp["comment_id"]
        ).order_by("-created_at", "-id")
        keep_id = qs.values_list("id", flat=True).first()
        if keep_id is not None:
            qs.exclude(id=keep_id).delete()


def noop_reverse(apps, schema_editor):
    """역방향: 삭제된 중복 행은 복원 불가 — no-op."""


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0030_eventinbox_daily_partition"),
    ]

    operations = [
        # ── SpamFilterConfig: 토글 2개 ──
        migrations.AddField(
            model_name="spamfilterconfig",
            name="auto_hide_enabled",
            field=models.BooleanField(
                default=False,
                help_text="스팸 감지 시 자동으로 댓글을 숨김 처리 (off면 감지 기록만 → 수동 숨김 대기)",
                verbose_name="자동 숨김",
            ),
        ),
        migrations.AddField(
            model_name="spamfilterconfig",
            name="use_llm",
            field=models.BooleanField(
                default=True,
                help_text="off면 gemma LLM 없이 키워드/URL 규칙만으로 스팸 판정",
                verbose_name="LLM 판정 사용",
            ),
        ),
        # ── SpamCommentLog: 판정 결과 필드 ──
        migrations.AddField(
            model_name="spamcommentlog",
            name="confidence",
            field=models.FloatField(
                blank=True, help_text="0.0~1.0 (LLM 판정 시)", null=True, verbose_name="스팸 신뢰도"
            ),
        ),
        migrations.AddField(
            model_name="spamcommentlog",
            name="spam_category",
            field=models.CharField(
                blank=True,
                default="",
                help_text="rule/scam/adult/phishing/promo/abuse 등",
                max_length=32,
                verbose_name="스팸 분류",
            ),
        ),
        migrations.AddField(
            model_name="spamcommentlog",
            name="engine",
            field=models.CharField(
                blank=True,
                default="",
                help_text="rule / llm / llm_failopen / rule_trivial / rule_only 등",
                max_length=32,
                verbose_name="판정 엔진",
            ),
        ),
        # ── SpamCommentLog: CLEAN 상태 추가 (choices 변경) ──
        migrations.AlterField(
            model_name="spamcommentlog",
            name="status",
            field=models.CharField(
                choices=[
                    ("clean", "정상"),
                    ("detected", "감지됨"),
                    ("hidden", "숨김처리"),
                    ("failed", "처리실패"),
                ],
                default="detected",
                max_length=20,
                verbose_name="상태",
            ),
        ),
        # ── SpamCommentLog: hidden_at 인덱스 (대시보드 차단 집계용) ──
        migrations.AddIndex(
            model_name="spamcommentlog",
            index=models.Index(fields=["hidden_at"], name="spam_log_hidden_at_idx"),
        ),
        # ── 멱등 제약: 중복 정리 → UNIQUE ──
        migrations.RunPython(dedupe_spam_logs, noop_reverse),
        migrations.AddConstraint(
            model_name="spamcommentlog",
            constraint=models.UniqueConstraint(
                fields=["spam_filter", "comment_id"], name="uq_spam_log_filter_comment"
            ),
        ),
    ]
