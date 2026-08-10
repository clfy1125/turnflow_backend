"""카드 등록 시점에 받았던 결제 동의를 PaymentConsent 로 백필 (일회성, 2026-08-10).

배경: 결제 화면에 동의 체크(약관/개인정보/정기결제)가 이미 있었으나 **서버 저장이 없었다**.
`payment_consents` 테이블이 2026-08-10 에 생겼으므로 그 이전 사용자는 기록이 0건이다.
사용자가 실제로 누른 행위를 기록으로 복원한다.

원칙 — **아는 값만 넣고 모르는 값은 비운다**. 추정값을 넣으면 그 행이 증거로서 무효가 된다.
  consented_at              = billing_key_issued_at   (카드 등록 실제 시각, DB 값)
  plan_name / disclosed_*   = DB 실측값 (플랜 · 첫 결제일 · 청구액)
  copy_version              = legacy-checkout@backfill-2026-08-10
                              (당시 문구 버전을 모르므로 그 사실이 드러나는 값을 쓴다)
  ip / user_agent / session = 비움 (그 시점 요청 컨텍스트를 우리가 갖고 있지 않다)

실행:
    cd /opt/turnflow_backend && git pull origin main
    # 1) 미리보기 (아무것도 쓰지 않음)
    docker exec -i -e DRY=1 turnflow_instagram_web_dashboard \
        python manage.py shell < scripts/backfill_payment_consent.py
    # 2) 적용
    docker exec -i -e DRY=0 turnflow_instagram_web_dashboard \
        python manage.py shell < scripts/backfill_payment_consent.py

⚠️ 이 백필만으로는 27명의 첫 결제가 되지 않는다. 과금 게이트가 보는 필드는
   `UserSubscription.conversion_consent_at`(유료전환 2차 동의)이고 `kind=initial` 이 아니다.
   27명을 정상 결제시키려면 아래 둘 중 하나가 **추가로** 필요하다:
     (A) .env.production 에 CONVERSION_CONSENT_ENFORCE=False  ← 권장. 차단만 끄고 수집은 유지
     (B) 이 스크립트를 SET_CONVERSION=1 로 실행 → conversion_consent_at 도 카드 등록 시각으로 채움
   (B)는 게이트를 통과시키지만, 감사 기록상 '2차 동의'가 첫 결제보다 43~64일 앞선 것으로
   남는다 → 시행령 30일 창을 충족하지 못하며, 그 사실이 기록에 그대로 보인다.
   즉 (B)는 컴플라이언스를 만들어주지 않고 게이트만 연다. 같은 결과를 (A)가 더 정직하게 낸다.

멱등: 같은 user+kind 기록이 이미 있으면 건너뛴다. 여러 번 실행해도 중복이 생기지 않는다.
"""

import os

from django.db import transaction
from django.utils import timezone

from apps.billing.consent import trial_length_days
from apps.billing.models import ConsentKind, PaymentConsent, SubscriptionStatus, UserSubscription

DRY = os.environ.get("DRY", "1") == "1"
SET_CONVERSION = os.environ.get("SET_CONVERSION", "0") == "1"
COPY_VERSION = "legacy-checkout@backfill-2026-08-10"

targets = [
    s
    for s in UserSubscription.objects.filter(status=SubscriptionStatus.TRIALING).select_related(
        "plan", "user"
    )
    if s.has_billing_key and (trial_length_days(s) or 0) > 30.001
]
mode = "DRY-RUN" if DRY else "APPLY"
print(f"[{mode}] 대상 {len(targets)}명  (SET_CONVERSION={'ON' if SET_CONVERSION else 'off'})")

rows, skipped, no_ts = [], 0, 0
for s in targets:
    if not s.billing_key_issued_at:
        no_ts += 1
        print(f"  SKIP(카드등록시각 없음) {s.user.email}")
        continue
    if PaymentConsent.objects.filter(user_id=s.user_id, kind=ConsentKind.INITIAL).exists():
        skipped += 1
        continue
    rows.append(s)

print(f"넣을 것 {len(rows)}건 / 이미 있어 건너뜀 {skipped}건 / 시각없음 {no_ts}건")
for s in rows[:3]:
    print(f"  예시 {s.user.email}")
    print(f"    consented_at              = {s.billing_key_issued_at}")
    print(f"    plan_name                 = {s.plan.name}")
    print(f"    disclosed_first_charge_at = {timezone.localdate(s.current_period_end)}")
    print(f"    disclosed_amount          = {s.renewal_amount}")
    print(f"    copy_version              = {COPY_VERSION}")
    print("    agreed_terms/privacy/recurring = True/True/True")
    print("    ip / user_agent / session = (비움)")

if DRY:
    print("DRY-RUN — 아무것도 쓰지 않았습니다. 적용은 DRY=0.")
else:
    created = converted = 0
    with transaction.atomic():
        for s in rows:
            PaymentConsent.objects.create(
                user_id=s.user_id,
                subscription=s,
                kind=ConsentKind.INITIAL,
                plan_name=s.plan.name,
                disclosed_first_charge_at=timezone.localdate(s.current_period_end),
                disclosed_amount=s.renewal_amount,
                disclosed_recurring_cycle="monthly",
                payment_method_type="card",
                copy_version=COPY_VERSION,
                agreed_terms=True,
                agreed_privacy=True,
                agreed_recurring=True,
                ip_address=None,
                user_agent="",
                session_key="",
                consented_at=s.billing_key_issued_at,
            )
            created += 1
        if SET_CONVERSION:
            for s in targets:
                if s.conversion_consent_at is None and s.billing_key_issued_at:
                    converted += UserSubscription.objects.filter(
                        pk=s.pk, conversion_consent_at__isnull=True
                    ).update(conversion_consent_at=s.billing_key_issued_at)
    print(f"생성 {created}건 · conversion_consent_at 설정 {converted}건")
    print(f"전체 PaymentConsent 행 수 = {PaymentConsent.objects.count()}")
