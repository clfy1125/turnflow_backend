"""
SentDMLog 99.9% 발송 보증 시스템 확장

- 새 상태(QUEUED/SUBMITTING/ACCEPTED/DELIVERED/READ + 분류된 실패) 추가
- 멱등성 키, meta_message_id, echo_mid, verified_via 등 검증 필드 추가
- 단계별 타임스탬프 (submitted/accepted/delivered/read)
- UNIQUE 제약: idempotency_key
- 기존 데이터의 idempotency_key는 sha256(id) 로 백필
"""

import hashlib

from django.db import migrations, models


def backfill_idempotency_key(apps, schema_editor):
    """기존 SentDMLog 행에 고유한 idempotency_key 백필"""
    SentDMLog = apps.get_model("integrations", "SentDMLog")
    for log in SentDMLog.objects.all().iterator():
        if not log.idempotency_key:
            log.idempotency_key = hashlib.sha256(
                f"legacy:{log.id}".encode("utf-8")
            ).hexdigest()
            log.save(update_fields=["idempotency_key"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0006_igoauthstate"),
    ]

    operations = [
        # 1. 새 필드 추가 (nullable/blank=True 우선)
        migrations.AddField(
            model_name="sentdmlog",
            name="idempotency_key",
            field=models.CharField(
                blank=True,
                default="",
                max_length=64,
                verbose_name="Idempotency Key",
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="meta_message_id",
            field=models.CharField(
                blank=True, default="", max_length=255, verbose_name="Meta Message ID"
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="echo_mid",
            field=models.CharField(
                blank=True, default="", max_length=255, verbose_name="Echo MID"
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="verified_via",
            field=models.CharField(
                blank=True,
                choices=[
                    ("echo", "is_echo 웹훅"),
                    ("conv_api", "Conversations API"),
                    ("both", "echo+conv_api"),
                ],
                default="",
                max_length=16,
                verbose_name="도착 확인 경로",
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="error_subcode",
            field=models.CharField(
                blank=True, max_length=50, verbose_name="에러 서브코드"
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="retry_count",
            field=models.IntegerField(default=0, verbose_name="재시도 횟수"),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="next_retry_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="다음 재시도 시각"
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="submitted_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="API 호출 시각"
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="accepted_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Meta 접수 시각"
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="delivered_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="도착 확인 시각"
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="read_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="읽음 시각"
            ),
        ),
        migrations.AddField(
            model_name="sentdmlog",
            name="verification_log",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="능동 조회/echo 매칭 등 검증 시도 이력",
                verbose_name="검증 로그",
            ),
        ),
        # 2. 상태 choices 확장 (legacy + 새 상태 모두 포함)
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
                    ("failed_api", "API 실패"),
                    ("failed_token", "토큰 만료"),
                    ("failed_window", "24h 윈도우 만료"),
                    ("failed_param", "파라미터 오류"),
                    ("failed_no_trace", "도착 미확인"),
                ],
                default="queued",
                max_length=20,
                verbose_name="상태",
            ),
        ),
        # 3. 기존 행에 idempotency_key 백필
        migrations.RunPython(backfill_idempotency_key, reverse_noop),
        # 4. idempotency_key를 unique 제약으로 승격
        migrations.AlterField(
            model_name="sentdmlog",
            name="idempotency_key",
            field=models.CharField(
                help_text="중복 발송 차단용 sha256 해시",
                max_length=64,
                unique=True,
                verbose_name="Idempotency Key",
            ),
        ),
        # 5. 새 인덱스 (Django 자동 명명 사용)
        migrations.AddIndex(
            model_name="sentdmlog",
            index=models.Index(
                fields=["status", "accepted_at"],
                name="sent_dm_log_status_98c2b3_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="sentdmlog",
            index=models.Index(
                fields=["meta_message_id"],
                name="sent_dm_log_meta_me_c79510_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="sentdmlog",
            index=models.Index(
                fields=["next_retry_at"],
                name="sent_dm_log_next_re_c46ef3_idx",
            ),
        ),
        # 6. sent_at verbose_name 변경 (legacy 표기)
        migrations.AlterField(
            model_name="sentdmlog",
            name="sent_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="발송일시(legacy)"
            ),
        ),
    ]
