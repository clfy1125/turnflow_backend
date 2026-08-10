"""결제 플로우 검증용 dev 테스트 계정 시드 (프론트 전달용).

`GET /billing/subscription/preview/` 의 3가지 시나리오 + 동의 기록 경로를 각각 재현할 수
있는 계정을 만든다. **dev 전용** — prod 에서 실행하면 즉시 중단한다(실사용자 DB 오염 방지).

    python manage.py seed_billing_test_accounts
    python manage.py seed_billing_test_accounts --reset   # 상태를 처음으로 되돌린다

계정은 매번 같은 이메일/비밀번호로 **멱등** 생성된다 — 프론트가 문서에 적어두고 계속 쓸 수
있어야 하므로 랜덤 이메일을 쓰지 않는다.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.billing.models import PaymentConsent, SubscriptionPlan, SubscriptionStatus
from apps.billing.subscription_utils import ensure_subscription

User = get_user_model()

PASSWORD = "TestPass1234!"

# (이메일, 라벨, 시나리오 설명)
ACCOUNTS = [
    ("billing-new@test.com", "무료 · 체험 미사용", "preview → scenario=trial (30일 체험 시작)"),
    ("billing-coupon@test.com", "무료 · 쿠폰 적용 대상", "preview?referral_code=... → 44일 체험"),
    (
        "billing-used@test.com",
        "체험 소진(trial_used_at)",
        "preview → scenario=charge_now (즉시 결제)",
    ),
    (
        "billing-trialing@test.com",
        "프로 체험 중 + 카드",
        "preview → scenario=attach_only (기간 불변)",
    ),
    ("billing-basic@test.com", "무료 · 베이직 구매용", "preview?plan_name=basic → charge_now"),
]


class Command(BaseCommand):
    help = "결제 플로우 검증용 dev 테스트 계정 시드 (dev 전용)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset", action="store_true", help="구독 상태·동의 기록을 초기 상태로 되돌린다"
        )

    def handle(self, *args, reset: bool = False, **opts):
        # prod 안전장치 — 실사용자 DB 에 테스트 계정을 만들지 않는다.
        if not settings.DEBUG and "prod" in (settings.SETTINGS_MODULE or ""):
            raise CommandError("prod 에서는 실행할 수 없습니다 (dev 전용 명령).")

        free = SubscriptionPlan.objects.get(name="free")
        pro = SubscriptionPlan.objects.get(name="pro")
        now = timezone.now()
        w = self.stdout.write

        for email, label, scenario in ACCOUNTS:
            user, created = User.objects.get_or_create(
                email=email,
                defaults={"full_name": label, "is_email_verified": True},
            )
            user.set_password(PASSWORD)
            user.is_email_verified = True
            user.save(update_fields=["password", "is_email_verified"])

            sub = ensure_subscription(user)
            if reset or created:
                PaymentConsent.objects.filter(user=user).delete()
                sub.plan = free
                sub.status = SubscriptionStatus.ACTIVE
                sub.current_period_start = now
                sub.current_period_end = None
                sub.monthly_amount_snapshot = None
                sub.extra_ig_accounts = 0
                sub.trial_used_at = None
                sub.trial_plan = None
                sub.cancelled_during_trial_at = None
                sub.conversion_consent_at = None
                sub.clear_billing_key()

                if email == "billing-used@test.com":
                    # 체험을 이미 써서 재구독은 즉시 결제가 되는 상태
                    sub.trial_used_at = now - timezone.timedelta(days=60)
                    sub.trial_plan = pro
                elif email == "billing-trialing@test.com":
                    # 프로 체험 중 + 카드 등록됨 (attach_only / 체험 중 UX 확인용)
                    sub.plan = pro
                    sub.trial_plan = pro
                    sub.status = SubscriptionStatus.TRIALING
                    sub.current_period_start = now
                    sub.current_period_end = now + timezone.timedelta(days=30)
                    sub.monthly_amount_snapshot = pro.monthly_price
                    sub.trial_used_at = now
                    sub.set_billing_key(
                        "bk_dev_test_trialing", card_company="현대", card_number="433012******123*"
                    )
                sub.save()

            state = f"{sub.plan.name}/{sub.status}"
            card = "카드있음" if sub.has_billing_key else "카드없음"
            w(
                f"  {'+' if created else '='} {email:<28} [{state:<16}] {card:<7} "
                f"{'(reset)' if reset and not created else ''}"
            )
            w(f"      └ {scenario}")

        w("")
        w(self.style.SUCCESS(f"완료. 비밀번호는 전부 '{PASSWORD}'"))
        w("  로그인: POST /api/v1/auth/login/  {email, password}")
        w("  ⚠️ 끝슬래시 필수")
