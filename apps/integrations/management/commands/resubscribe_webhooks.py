"""연동된(ACTIVE) IG 계정들의 웹훅 구독(comments,messages)을 점검/재확정한다.

용도:
- DR 서버 이전(failover) 직후 즉시 재구독 (startup.sh 가 호출) — Meta auto-disable 복구.
- 운영자 수동 점검/복구.

주기 실행은 Celery beat `integrations-resubscribe-webhooks`(6h) 가 담당한다.
이 명령은 활성 사이트 게이트 없이 직접 실행한다(DR/수동 목적).
"""

from django.core.management.base import BaseCommand

from apps.integrations.tasks import resubscribe_active_connections


class Command(BaseCommand):
    help = "연동된(ACTIVE) IG 계정들의 웹훅 구독(comments,messages)을 점검/재확정한다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="구독 변경 없이 현재 상태만 점검(재구독 안 함).",
        )

    def handle(self, *args, **opts):
        res = resubscribe_active_connections(check_only=opts["check_only"])
        self.stdout.write(
            self.style.SUCCESS(
                f"checked={res['checked']} ok={res['ok']} "
                f"resubscribed={res['resubscribed']} failed={res['failed']} "
                f"skipped_expired={res['skipped_expired']}"
            )
        )
        for d in res["details"]:
            self.stdout.write("  - " + d)
