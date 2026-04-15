"""
기존 유료 구독자의 PayApp 정기결제를 해지하는 커맨드.

플랜 가격 변경 후 기존 구독자들의 정기결제를 해지하고,
사용자가 재구독하면 새 가격이 적용되도록 합니다.

사용법:
  # 대상 확인 (dry-run, 기본값)
  python manage.py cancel_rebills

  # 실제 실행
  python manage.py cancel_rebills --execute
"""

from django.core.management.base import BaseCommand

from apps.billing.models import UserSubscription, SubscriptionStatus
from apps.billing.payapp_service import PayAppClient, PayAppError


class Command(BaseCommand):
    help = "기존 유료 구독자의 PayApp 정기결제를 해지합니다 (가격 변경 시 사용)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="실제로 해지를 실행합니다. 없으면 dry-run으로 대상만 표시합니다.",
        )

    def handle(self, *args, **options):
        execute = options["execute"]

        subs = UserSubscription.objects.filter(
            plan__name__in=["pro", "pro_plus"],
            status=SubscriptionStatus.ACTIVE,
            payapp_rebill_no__isnull=False,
        ).select_related("user", "plan")

        if not subs.exists():
            self.stdout.write(self.style.SUCCESS("해지 대상 정기결제가 없습니다."))
            return

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"해지 대상: {subs.count()}건")
        self.stdout.write(f"{'='*60}\n")

        for sub in subs:
            self.stdout.write(
                f"  {sub.user.email} | {sub.plan.display_name} | "
                f"rebill_no={sub.payapp_rebill_no} | "
                f"period_end={sub.current_period_end}"
            )

        if not execute:
            self.stdout.write(
                self.style.WARNING(
                    "\n[DRY-RUN] 실제 해지하려면 --execute 옵션을 추가하세요.\n"
                    "  python manage.py cancel_rebills --execute"
                )
            )
            return

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write("실행 시작")
        self.stdout.write(f"{'='*60}\n")

        success = 0
        failed = 0

        for sub in subs:
            try:
                PayAppClient.cancel_rebill(sub.payapp_rebill_no)
                old_rebill = sub.payapp_rebill_no
                sub.payapp_rebill_no = None
                sub.payapp_pay_url = None
                sub.save(update_fields=[
                    "payapp_rebill_no", "payapp_pay_url", "updated_at",
                ])
                success += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ {sub.user.email} — rebill_no={old_rebill} 해지 완료"
                    )
                )
            except PayAppError as e:
                failed += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"  ✗ {sub.user.email} — rebill_no={sub.payapp_rebill_no} 해지 실패: {e}"
                    )
                )

        self.stdout.write(f"\n{'='*60}")
        self.stdout.write(f"완료: 성공 {success}건, 실패 {failed}건")
        self.stdout.write(f"{'='*60}")
