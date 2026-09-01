"""그랜드파더링 스냅샷을 **현재 판매가까지 내린다** (인하분 소급 적용).

가격을 내리면 신규 가입자는 새 가격이지만, 기존 구독자는
``UserSubscription.monthly_amount_snapshot`` 에 옛 가격이 고정돼 있어 계속 비싸게
결제된다(그랜드파더링은 인상 방어용 장치라 인하는 자동 반영되지 않는다).
이 명령이 그 스냅샷을 현재 판매가로 내려, **다음 갱신부터** 새 가격이 적용되게 한다.

    python manage.py lower_grandfathered_price                 # 미리보기 (기본, 쓰기 없음)
    python manage.py lower_grandfathered_price --apply         # 실제 반영
    python manage.py lower_grandfathered_price --plan pro --apply
    python manage.py lower_grandfathered_price --email a@b.com --apply

안전 규약:

- **내리기만 한다.** 스냅샷 > 현재가 인 행만 손대고, 스냅샷 < 현재가(진짜 프로모
  그랜드파더링)는 절대 올리지 않는다. 그래서 몇 번 돌려도 결과가 같다(멱등).
- 갱신 과금 태스크(``charge_subscription``)와 **같은 방식으로 행을 잠근다**
  (``select_for_update(of=("self",))``). pending_plan 이 nullable FK(OUTER JOIN)라
  조인행은 잠글 수 없으므로 구독 행만 잠근다.
- 이미 PENDING 주문이 떠 있으면(승인 진행 중) **그 행은 건너뛴다** — 주문 금액은
  생성 시점에 확정되므로 스냅샷만 바꾸면 원장과 청구가 어긋난다. 그 건은 해당
  주기가 끝난 뒤 다시 돌리면 된다.
- 과거 ``PaymentHistory`` · ``PaymentConsent.disclosed_amount`` 는 그 시점의 사실
  기록이므로 손대지 않는다.
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F, Q

from apps.billing.models import PaymentHistory, PaymentStatus, UserSubscription

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "가격 인하분을 기존 구독자 스냅샷에 소급 적용 (내리기만, 멱등)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="실제로 반영한다. 없으면 미리보기만 하고 아무것도 쓰지 않는다.",
        )
        parser.add_argument(
            "--plan",
            default=None,
            help="플랜 이름으로 한정 (예: pro). 미지정이면 전 플랜.",
        )
        parser.add_argument(
            "--email",
            action="append",
            default=None,
            help="특정 회원만 (여러 번 지정 가능). 미지정이면 대상 전원.",
        )

    def handle(self, *args, apply=False, plan=None, email=None, **opts):
        w = self.stdout.write

        # 스냅샷이 현재 판매가보다 비싼 행만. 예약 플랜(pending)도 같은 기준으로 본다.
        qs = (
            UserSubscription.objects.filter(
                Q(monthly_amount_snapshot__gt=F("plan__monthly_price"))
                | Q(pending_amount_snapshot__gt=F("pending_plan__monthly_price"))
            )
            .select_related("plan", "pending_plan", "user")
            .order_by("current_period_end")
        )
        if plan:
            qs = qs.filter(Q(plan__name=plan) | Q(pending_plan__name=plan))
        if email:
            qs = qs.filter(user__email__in=email)

        targets = list(qs)
        w("")
        w(self.style.MIGRATE_HEADING("그랜드파더링 스냅샷 인하 소급"))
        w(f"  모드          : {'APPLY (쓰기)' if apply else 'DRY-RUN (미리보기)'}")
        w(f"  플랜 한정     : {plan or '(전체)'}")
        w(f"  회원 한정     : {', '.join(email) if email else '(전체)'}")
        w(f"  대상          : {len(targets)}건")
        w("")

        if not targets:
            w(self.style.SUCCESS("  내릴 것이 없습니다 (모든 스냅샷이 현재 판매가 이하)."))
            w("")
            return

        if email:
            missing = set(email) - {s.user.email for s in targets}
            if missing:
                w(self.style.WARNING(f"  ⚠️ 대상이 아닌 이메일: {', '.join(sorted(missing))}"))
                w("")

        changed = 0
        skipped = 0
        delta_total = 0

        for sub in targets:
            plan_price = sub.plan.monthly_price
            snap = sub.monthly_amount_snapshot
            p_snap = sub.pending_amount_snapshot
            p_price = sub.pending_plan.monthly_price if sub.pending_plan_id else None

            before_amount = sub.renewal_amount
            label = f"{sub.user.email:<38} plan={sub.plan.name:<6} status={sub.status:<10}"

            # 승인 진행 중 주문이 있으면 만지지 않는다 — 원장과 청구가 어긋난다.
            pending_order = PaymentHistory.objects.filter(
                subscription=sub, status=PaymentStatus.PENDING
            ).exists()
            if pending_order:
                skipped += 1
                w(
                    self.style.WARNING(
                        f"  SKIP  {label} — PENDING 주문 진행 중 (다음 주기에 재실행)"
                    )
                )
                continue

            new_snap = min(snap, plan_price) if snap is not None else None
            new_p_snap = (
                min(p_snap, p_price) if (p_snap is not None and p_price is not None) else p_snap
            )

            parts = []
            if new_snap != snap:
                parts.append(f"snapshot {snap:,}→{new_snap:,}")
            if new_p_snap != p_snap:
                parts.append(f"pending({sub.pending_plan.name}) {p_snap:,}→{new_p_snap:,}")
            if not parts:
                skipped += 1
                continue

            if not apply:
                # 미리보기용 청구액 재계산 (저장 없음)
                sub.monthly_amount_snapshot = new_snap
                sub.pending_amount_snapshot = new_p_snap
                after_amount = sub.renewal_amount
                delta_total += before_amount - after_amount
                changed += 1
                w(
                    f"  WOULD {label} {' / '.join(parts)}  "
                    f"청구액 {before_amount:,}→{after_amount:,}원"
                )
                continue

            with transaction.atomic():
                locked = (
                    UserSubscription.objects.select_for_update(of=("self",))
                    .select_related("plan", "pending_plan", "user")
                    .get(pk=sub.pk)
                )
                # 락 획득 후 재검증 — 그 사이 다른 경로(플랜 변경 등)가 바꿨을 수 있다.
                fields = []
                if (
                    locked.monthly_amount_snapshot is not None
                    and locked.monthly_amount_snapshot > locked.plan.monthly_price
                ):
                    locked.monthly_amount_snapshot = locked.plan.monthly_price
                    fields.append("monthly_amount_snapshot")
                if (
                    locked.pending_plan_id
                    and locked.pending_amount_snapshot is not None
                    and locked.pending_amount_snapshot > locked.pending_plan.monthly_price
                ):
                    locked.pending_amount_snapshot = locked.pending_plan.monthly_price
                    fields.append("pending_amount_snapshot")
                if not fields:
                    skipped += 1
                    w(self.style.WARNING(f"  SKIP  {label} — 락 후 재검증에서 대상 아님"))
                    continue
                locked.save(update_fields=fields)
                after_amount = locked.renewal_amount

            delta_total += before_amount - after_amount
            changed += 1
            logger.info(
                "그랜드파더링 인하 소급: user=%s plan=%s %s 청구액 %d→%d",
                sub.user.email,
                sub.plan.name,
                " / ".join(parts),
                before_amount,
                after_amount,
            )
            w(
                self.style.SUCCESS(
                    f"  DONE  {label} {' / '.join(parts)}  "
                    f"청구액 {before_amount:,}→{after_amount:,}원"
                )
            )

        w("")
        w(f"  {'반영' if apply else '반영 예정'}: {changed}건 / 건너뜀: {skipped}건")
        w(f"  월 청구액 합계 감소: {delta_total:,}원")
        if not apply:
            w("")
            w(self.style.WARNING("  ※ DRY-RUN 입니다. 실제 반영은 --apply 를 붙이세요."))
        w("")
