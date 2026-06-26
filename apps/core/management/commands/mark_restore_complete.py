"""DR: SiteControl 권위 전환 (promote/demote).

  --promote : 이 서버(SITE_ID)를 active 로 승격 (active_site=SITE_ID, mode=live, restore_complete=True), epoch++.
              failover.sh 의 마지막 단계 — 복구·마이그레이션·dr_catchup 성공 후에만 호출.
  --demote  : 이 서버를 maintenance(passive)로 강등 (mode=maintenance, restore_complete=False), epoch++.

epoch 는 항상 +1 (펜싱). 전환 후 site_state 캐시를 즉시 무효화한다.
상세: DR_IMPLEMENTATION_PLAN.md §5.5, §8.2.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "DR SiteControl promote/demote (active_site flip + epoch++)."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--promote", action="store_true", help="이 서버를 active 로 승격")
        group.add_argument("--demote", action="store_true", help="이 서버를 maintenance 로 강등")
        parser.add_argument(
            "--site", default=None, help="active_site 로 지정할 SITE_ID (기본 settings.SITE_ID)"
        )
        parser.add_argument("--note", default="", help="SiteControl.note 에 남길 메모")

    def handle(self, *args, **opts):
        from apps.core.models import SiteControl
        from apps.core.site_control import invalidate_site_state_cache

        site = opts["site"] or getattr(settings, "SITE_ID", "colo")

        with transaction.atomic():
            sc, _ = SiteControl.objects.select_for_update().get_or_create(pk=1)
            sc.epoch = (sc.epoch or 0) + 1
            if opts["promote"]:
                sc.active_site = site
                sc.mode = SiteControl.Mode.LIVE
                sc.restore_complete = True
            else:  # demote
                sc.mode = SiteControl.Mode.MAINTENANCE
                sc.restore_complete = False
            if opts["note"]:
                sc.note = opts["note"]
            sc.save()

        invalidate_site_state_cache()

        msg = (
            f"SiteControl: active_site={sc.active_site} epoch={sc.epoch} "
            f"mode={sc.mode} restore_complete={sc.restore_complete}"
        )
        self.stdout.write(self.style.SUCCESS(msg))

        try:
            from apps.core.telegram import send_telegram_notification

            action = "PROMOTE" if opts["promote"] else "DEMOTE"
            send_telegram_notification(f"🔁 DR {action} — {msg}")
        except Exception:  # noqa: BLE001 — best-effort
            pass
