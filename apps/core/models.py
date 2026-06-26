"""Core models — DR(재해복구) 컨트롤플레인.

- ``SiteControl``  : 어느 서버(colo/office/azure)가 '권위(write/Celery/scheduler 가능)' 인지의 단일 진실.
- ``ScheduledJob`` : 외부 Cron(``/api/v1/internal/scheduler/tick``)이 참조하는 due-job 테이블.
                     기존 ``CELERY_BEAT_SCHEDULE`` 의 단일 장애점(celery_beat)을 대체한다.

설계 상세: ``DR_IMPLEMENTATION_PLAN.md`` §5(SiteControl/health), §6(ScheduledJob/tick).
"""

from __future__ import annotations

from datetime import UTC, timedelta

from django.db import models
from django.utils import timezone


class SiteControl(models.Model):
    """DR 권위 사이트 락 (싱글톤, pk=1).

    ``active_site == settings.SITE_ID`` 인 서버만 write / Celery / scheduler tick 이 허용된다.
    ``epoch`` 는 펜싱 토큰 — 모든 권위 전환마다 +1 하여, 되살아난 과거 active 서버가 자신이
    stale 임을 감지하고 스스로 passive 化하게 한다(split-brain 방지).
    """

    class Mode(models.TextChoices):
        LIVE = "live", "Live"
        MAINTENANCE = "maintenance", "Maintenance"

    active_site = models.CharField(max_length=32, default="colo", verbose_name="권위 사이트")
    epoch = models.BigIntegerField(default=1, verbose_name="펜싱 epoch")
    mode = models.CharField(
        max_length=16, choices=Mode.choices, default=Mode.LIVE, verbose_name="모드"
    )
    restore_complete = models.BooleanField(default=True, verbose_name="복구 완료")
    note = models.CharField(max_length=255, blank=True, default="", verbose_name="비고")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="갱신 시각")

    class Meta:
        db_table = "site_control"
        verbose_name = "Site Control"
        verbose_name_plural = "Site Control"

    def __str__(self) -> str:
        return f"active={self.active_site} epoch={self.epoch} mode={self.mode}"

    def save(self, *args, **kwargs):
        self.pk = 1  # 싱글톤 강제
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> SiteControl:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ScheduledJob(models.Model):
    """외부 Cron(/scheduler/tick)이 참조하는 due-job (celery_beat 대체).

    ``next_due_at`` 이 '지금이 그 시각인가' 의 권위 필드. tick 이
    ``select_for_update(skip_locked=True)`` 로 due 행을 잠그고 ``next_due_at`` 을 전진시킨 뒤
    Celery 로 enqueue 한다 → 동시 tick(CF Cron + GCS) 에도 '윈도우당 정확히 1회' 가
    DB 불변식으로 보장된다.
    """

    key = models.CharField(max_length=128, unique=True, verbose_name="잡 키")
    task = models.CharField(max_length=255, verbose_name="Celery 태스크명")
    interval_seconds = models.PositiveIntegerField(null=True, blank=True, verbose_name="주기(초)")
    # cron 형 잡(빈 문자열 = '*'). interval_seconds 가 있으면 그쪽이 우선.
    cron_minute = models.CharField(max_length=64, blank=True, default="")
    cron_hour = models.CharField(max_length=64, blank=True, default="")
    cron_day_of_week = models.CharField(max_length=64, blank=True, default="")
    queue = models.CharField(
        max_length=64, blank=True, default="", verbose_name="큐(빈값=route-by-name)"
    )
    enabled = models.BooleanField(default=True, verbose_name="활성")
    next_due_at = models.DateTimeField(db_index=True, verbose_name="다음 실행 예정 시각")
    last_run_at = models.DateTimeField(null=True, blank=True, verbose_name="마지막 실행")
    last_status = models.CharField(max_length=32, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "core_scheduled_job"
        verbose_name = "Scheduled Job"
        verbose_name_plural = "Scheduled Jobs"
        ordering = ["key"]
        indexes = [models.Index(fields=["enabled", "next_due_at"], name="core_sched_enabled_due")]

    def __str__(self) -> str:
        return f"{self.key} (next={self.next_due_at:%Y-%m-%d %H:%M})"

    def compute_next_due(self, *, after=None):
        """다음 due 시각 계산. interval 우선, 아니면 cron. tz-aware(UTC) 반환.

        놓친 윈도우를 몰아치지 않도록 항상 ``after``(기본 now) 이후 '다음' 1회만 잡는다.
        cron 은 **settings.TIME_ZONE(Asia/Seoul)** 기준으로 결정적 계산한다(Celery crontab 의
        tz 바인딩이 신뢰되지 않아 직접 계산 — 계획서 §14-3 해소).
        지원 grammar: cron_minute=정수, cron_hour="*" | "*/N" | 정수, day_of_week="" 또는 "*".
        """
        base = after or timezone.now()
        if self.interval_seconds:
            return base + timedelta(seconds=self.interval_seconds)
        return self._next_cron(base)

    def _next_cron(self, base):
        import zoneinfo

        from django.conf import settings

        tz = zoneinfo.ZoneInfo(getattr(settings, "TIME_ZONE", "UTC") or "UTC")
        local = base.astimezone(tz)

        minute = int(self.cron_minute) if self.cron_minute.strip().isdigit() else 0
        hour_spec = (self.cron_hour or "*").strip()
        if hour_spec == "*" or hour_spec == "":
            allowed_hours = set(range(24))
        elif hour_spec.startswith("*/"):
            step = int(hour_spec[2:])
            allowed_hours = set(range(0, 24, max(step, 1)))
        elif hour_spec.isdigit():
            allowed_hours = {int(hour_spec)}
        else:
            # 미지원 grammar → 안전하게 1시간 뒤 재평가(다음 tick 이 다시 계산)
            return base + timedelta(hours=1)

        candidate = local.replace(minute=minute, second=0, microsecond=0)
        for _ in range(24 * 8):  # 최대 8일 lookahead (day_of_week 미사용이라 충분)
            if candidate > local and candidate.hour in allowed_hours:
                return candidate.astimezone(UTC)
            candidate = candidate + timedelta(hours=1)
        return base + timedelta(hours=1)  # 이론상 도달 안 함
