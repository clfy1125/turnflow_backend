"""dev 재현: CS #d34572b3 — 프로 체험 중 / 계정 3개 / 2개로 축소 예약됨.

prod suecap1@gmail.com 상태를 그대로 복제한다(개인정보는 복사하지 않음 — 계정명·프로필사진 제외).
멱등: 다시 돌리면 같은 상태로 덮어쓴다.
"""

import datetime
import uuid

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.text import slugify

EMAIL = "ig-reduce@test.com"
PASSWORD = "Test1234!"

U = get_user_model()
now = timezone.now()

# ── 1. 유저 ────────────────────────────────────────────────────────────────
user, created = U.objects.get_or_create(
    email=EMAIL,
    defaults={"full_name": "계정축소 테스트", "is_active": True},
)
user.full_name = "계정축소 테스트"
user.is_active = True
if hasattr(user, "is_email_verified"):
    user.is_email_verified = True
user.set_password(PASSWORD)
user.save()
print(f"USER {'created' if created else 'updated'}: {user.email} (id={user.id})")

# ── 2. 워크스페이스 ─────────────────────────────────────────────────────────
from apps.workspace.models import Membership, Workspace  # noqa: E402

ws = Workspace.objects.filter(owner=user).first()
if not ws:
    ws = Workspace.objects.create(
        name="계정축소 테스트",
        slug=f"ig-reduce-{uuid.uuid4().hex[:8]}",
        owner=user,
    )
Membership.objects.get_or_create(workspace=ws, user=user, defaults={"role": Membership.Role.OWNER})
print(f"WORKSPACE: {ws.id} {ws.name}")

# ── 3. 구독: 프로 / 체험중 / extra=2, pending=1 / 7일 뒤 첫 결제 ──────────────
from apps.billing.models import SubscriptionPlan, SubscriptionStatus, UserSubscription  # noqa: E402

pro = SubscriptionPlan.objects.get(name="pro")
period_end = (now + datetime.timedelta(days=7)).replace(hour=16, minute=1, second=0, microsecond=0)
period_start = period_end - datetime.timedelta(days=44)  # 44일 쿠폰 체험 (prod 와 동일)

sub, _ = UserSubscription.objects.get_or_create(user=user, defaults={"plan": pro})
sub.plan = pro
sub.status = SubscriptionStatus.TRIALING
sub.current_period_start = period_start
sub.current_period_end = period_end
sub.monthly_amount_snapshot = 14900
sub.extra_ig_accounts = 2  # 지금 허용량 = 1(기본) + 2 = 3개
sub.pending_extra_ig_accounts = 1  # ← 축소 예약: 갱신일부터 1+1 = 2개
sub.pending_plan = None
sub.pending_amount_snapshot = None
sub.trial_used_at = period_start
sub.trial_plan = pro
sub.card_company = "신한"
sub.card_number_masked = "45184445****364*"
sub.billing_key_issued_at = period_start
if not sub.toss_customer_key:
    sub.toss_customer_key = f"tf_{uuid.uuid4().hex}"
# 실제 결제가 절대 일어나면 안 되므로 빌링키는 비워 둔다(갱신 시 토스 호출 불가 → 실패로 끝남).
sub.save()
print(
    f"SUBSCRIPTION: plan=pro status={sub.status} extra={sub.extra_ig_accounts} "
    f"pending={sub.pending_extra_ig_accounts} period_end={sub.current_period_end}"
)

# ── 4. IG 연동 3개 (연동일 차이 = 자동조정 순서를 만든다) ─────────────────────
from apps.integrations.models import AutoDMCampaign, IGAccountConnection  # noqa: E402

SEED = [
    # (username, 표시명, 연동일 오프셋(일), 캠페인 상태)
    ("old_news_bot.kr", "", -38, "active"),  # 가장 먼저 연동 = 고객이 지우고 싶어하는 계정
    ("main.brand", "메인 브랜드 계정", -18, "active"),
    ("sub.brand.skin", "서브 브랜드 계정", -13, "active"),
]

conns = []
for idx, (uname, disp, day_offset, camp_status) in enumerate(SEED):
    ext_id = f"1784100000000{idx:04d}"
    conn, made = IGAccountConnection.objects.get_or_create(
        workspace=ws,
        external_account_id=ext_id,
        defaults={"username": uname},
    )
    conn.username = uname
    conn.name = disp
    conn.account_type = "BUSINESS"
    conn.access_token = f"DEVFAKE-{ext_id}"  # 가짜 토큰 — Graph 호출은 실패한다(UI 확인용)
    conn.token_expires_at = now + datetime.timedelta(days=60)
    conn.scopes = ["instagram_business_basic", "instagram_business_manage_messages"]
    conn.status = IGAccountConnection.Status.ACTIVE
    conn.is_active = True
    conn.last_verified_at = now
    conn.token_dead_strikes = 0
    conn.token_dead_first_seen_at = None
    conn.followers_count = 1200 + idx * 3400
    conn.media_count = 40 + idx * 15
    conn.save()
    # created_at 은 auto_now_add 라 update() 로만 덮어쓸 수 있다
    IGAccountConnection.objects.filter(pk=conn.pk).update(
        created_at=now + datetime.timedelta(days=day_offset)
    )
    conn.refresh_from_db()
    conns.append(conn)

    camp, _ = AutoDMCampaign.objects.get_or_create(
        ig_connection=conn,
        name=f"[테스트] {uname} 자동DM",
        defaults={
            "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            "media_id": f"1784199999{idx:06d}",
        },
    )
    camp.message_template = "안녕하세요! 신청해주셔서 감사합니다 🙌"
    camp.keyword_filter = ["신청"]
    camp.status = camp_status
    camp.save()
    print(f"  IG {conn.username:20s} created_at={conn.created_at:%Y-%m-%d} campaign={camp.status}")

# ── 5. 검증: 실제 API 가 뭐라고 답하는지 ────────────────────────────────────
from apps.billing.subscription_utils import (  # noqa: E402
    count_active_ig_connections,
    get_ig_account_allowance,
)

print("\n=== 검증 ===")
print("  현재 허용량 =", get_ig_account_allowance(user))
print("  현재 활성 계정 =", count_active_ig_connections(user))

from apps.billing import tasks as bt  # noqa: E402

print("  다음 결제 예정액 =", bt._renewal_amount_for(sub))

allowance_after = 1 + sub.pending_extra_ig_accounts
active_sorted = list(
    IGAccountConnection.objects.filter(
        workspace__owner=user, status="active", is_active=True
    ).order_by("created_at")
)
print("  갱신 후 허용량 =", allowance_after)
print("  → 남는 계정:", [c.username for c in active_sorted[:allowance_after]])
print("  → 자동 비활성:", [c.username for c in active_sorted[allowance_after:]])

print(f"\n로그인: {EMAIL} / {PASSWORD}")
print(f"워크스페이스: {ws.id}")
for c in conns:
    print(f"  connection_id {c.id}  @{c.username}")
