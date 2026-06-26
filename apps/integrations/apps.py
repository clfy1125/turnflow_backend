"""
Integrations app configuration
"""

from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations"
    verbose_name = "Instagram Integrations"

    def ready(self):
        # P8: rate_governor fail-closed 의 기준점.
        # 프로세스 시작 시(=배포/재시작) 거버너 센티넬을 심어둔다. 이후 어느 check() 에서
        # 센티넬이 '사라진' 것을 보면 = Redis 가 (프로세스는 살아있는데) flush/재시작됐다는 뜻 →
        # 그 시각 경계까지 fail-closed. 반대로 콜드 스타트(배포)는 여기서 센티넬을 다시 심으므로
        # 발송이 차단되지 않는다(배포 때마다 1시간 멈추는 사고 방지).
        try:
            from django.core.cache import cache

            cache.set("dmrate:alive", 1, timeout=7 * 24 * 3600)
        except Exception:  # noqa: BLE001 - 캐시 미가용(일부 management 커맨드) 시 무시
            pass
