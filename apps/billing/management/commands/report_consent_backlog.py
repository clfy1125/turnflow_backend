"""유료전환 2차 동의 — **소급 대상 규모 산출** (프론트 요청서 §2-5).

새 고지 화면 이전에 가입해 동의 기록이 없는 체험자가 몇 명인지 세고, 처리 방식(개별 안내 vs
배치 + 유예)을 결정할 수 있게 분해해서 보여준다. **읽기 전용** — 아무것도 바꾸지 않는다.

    python manage.py report_consent_backlog
    python manage.py report_consent_backlog --since 2026-08-10       # 새 화면 배포일
    python manage.py report_consent_backlog --list                   # 이메일까지 출력

분해 축:
- ``coupon_over_30d``  체험 30일 초과(쿠폰 연장) + 미동의 → **기본 게이트 대상**.
  코드 배포만으로 첫 결제가 차단되므로 이 인원에게는 D-14/D-3 메일이 자동으로 나간다.
- ``legacy_30d``       체험 30일 이하 + ``initial`` 동의 기록도 없음 → 새 화면 이전 가입자.
  ``CONVERSION_CONSENT_REQUIRE_ALL_TRIALS`` 를 켜야 게이트에 들어온다(기본 꺼짐).
  **이 숫자를 보고 정책을 정하는 것이 이 명령의 목적이다.**
- ``consented``        이미 2차 동의를 받은 체험자 (참고용).
- ``no_card``          카드 미등록 체험자 — 자동 유료전환 자체가 없어 대상 아님.
"""

from __future__ import annotations

from datetime import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing.consent import CONSENT_WINDOW_DAYS, has_initial_consent, trial_length_days
from apps.billing.models import SubscriptionStatus, UserSubscription


class Command(BaseCommand):
    help = "유료전환 2차 동의 소급 대상 규모 산출 (읽기 전용)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            default=None,
            help="새 고지 화면 배포일 (YYYY-MM-DD). 이 날짜 **이전** 가입만 legacy 로 센다. "
            "미지정이면 가입일 필터 없음.",
        )
        parser.add_argument(
            "--list", action="store_true", help="대상 회원의 이메일·체험 종료일까지 출력"
        )

    def handle(self, *args, since: str | None = None, list: bool = False, **opts):
        show_list = list
        now = timezone.now()
        cutoff = None
        if since:
            # 배포일은 운영이 KST 로 말하는 날짜 → KST 자정 기준 (UTC 로 읽으면 9시간 밀린다)
            cutoff = timezone.make_aware(datetime.strptime(since, "%Y-%m-%d"))

        subs = (
            UserSubscription.objects.filter(status=SubscriptionStatus.TRIALING)
            .select_related("plan", "user")
            .order_by("current_period_end")
        )

        buckets: dict[str, list] = {
            "coupon_over_30d": [],
            "legacy_30d": [],
            "consented": [],
            "no_card": [],
            "ok_30d_consented_at_signup": [],
        }

        for sub in subs:
            if not sub.has_billing_key:
                buckets["no_card"].append(sub)
                continue
            if sub.conversion_consent_at is not None:
                buckets["consented"].append(sub)
                continue
            length = trial_length_days(sub) or 0
            if length > CONSENT_WINDOW_DAYS + (1 / 1440):
                buckets["coupon_over_30d"].append(sub)
                continue
            if has_initial_consent(sub):
                buckets["ok_30d_consented_at_signup"].append(sub)
                continue
            if cutoff is not None and sub.user.date_joined >= cutoff:
                # 새 화면 이후 가입인데 initial 기록이 없다 → 프론트 연결 누락 의심.
                buckets.setdefault("post_deploy_no_record", []).append(sub)
                continue
            buckets["legacy_30d"].append(sub)

        w = self.stdout.write
        w("")
        w(self.style.MIGRATE_HEADING("유료전환 2차 동의 — 소급 대상 규모"))
        w(f"  기준 시각        : {timezone.localtime(now).isoformat()}")
        w(f"  배포일(--since)  : {since or '(미지정)'}")
        w(f"  체험(trialing) 총 : {subs.count()}명")
        w("")
        labels = [
            (
                "coupon_over_30d",
                "① 30일 초과 체험 + 미동의 (배포만으로 게이트 대상)",
                self.style.WARNING,
            ),
            ("legacy_30d", "② 30일 체험 + 동의 기록 없음 (소급 정책 결정 대상)", self.style.ERROR),
            (
                "post_deploy_no_record",
                "③ 배포 이후 가입인데 initial 기록 없음 (프론트 연결 누락 의심)",
                self.style.ERROR,
            ),
            (
                "ok_30d_consented_at_signup",
                "④ 30일 체험 + 결제화면 동의 있음 (조치 불필요)",
                self.style.SUCCESS,
            ),
            ("consented", "⑤ 이미 2차 동의 완료", self.style.SUCCESS),
            ("no_card", "⑥ 카드 미등록 체험 (자동 전환 없음 — 대상 아님)", lambda s: s),
        ]
        for key, label, style in labels:
            rows = buckets.get(key) or []
            w(style(f"  {label}: {len(rows)}명"))
            if show_list and rows:
                for sub in rows:
                    days = trial_length_days(sub)
                    w(
                        f"      - {sub.user.email:<40} plan={sub.plan.name:<6} "
                        f"trial={days:.0f}일 종료={timezone.localdate(sub.current_period_end)} "
                        f"가입={timezone.localdate(sub.user.date_joined)}"
                    )
        w("")
        w("  다음 결정: ② 가 소수면 개별 안내(2차 동의 플로우 재사용), 다수면")
        w("            CONVERSION_CONSENT_REQUIRE_ALL_TRIALS=True + 전환 유예 검토.")
        w("")
