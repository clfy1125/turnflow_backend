"""어드민 회원 구독(요금제) 정합성 API 테스트.

대상 (모두 /api/v1/admin/, IsAdminUser):
  - GET   /admin/users/                 — subscription 블록 + ?plan= 구독 기준 필터
  - PATCH /admin/users/{id}/subscription/ — 결제 없이 구독 등급 강제 변경
  - GET   /admin/subscription-plans/    — 비활성 포함 전체 플랜
  - GET   /admin/metrics/overview/      — subscriptions.by_plan 동적 집계
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.admin_api.models import AdminActionLog
from apps.billing.models import (
    SubscriptionPlan,
    SubscriptionStatus,
    UserSubscription,
)

User = get_user_model()


# ─── 공통 픽스처 ─────────────────────────────────────────────


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def regular_user(db):
    return User.objects.create_user(email="regular@example.com", password="Pass1234!")


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(email="staff@example.com", password="Pass1234!", is_staff=True)


@pytest.fixture
def staff_client(client, staff_user):
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def regular_client(client, regular_user):
    client.force_authenticate(user=regular_user)
    return client


# 운영 DB 와 동일한 플랜 집합 (free/pro 활성, admin 비활성).
# 마이그레이션 시드와 충돌하지 않도록 get_or_create.


@pytest.fixture
def free_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="free",
        defaults={"display_name": "무료", "monthly_price": 0, "sort_order": 0},
    )
    return obj


@pytest.fixture
def pro_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="pro",
        defaults={"display_name": "프로", "monthly_price": 14900, "sort_order": 1},
    )
    return obj


@pytest.fixture
def admin_plan(db):
    obj, _ = SubscriptionPlan.objects.get_or_create(
        name="admin",
        defaults={
            "display_name": "관리자",
            "monthly_price": 18900,
            "sort_order": 2,
            "is_active": False,
        },
    )
    return obj


@pytest.fixture
def member(db):
    """구독을 붙일 평범한 회원 1명."""
    return User.objects.create_user(email="member@example.com", password="Pass1234!")


# ─── 권한 ────────────────────────────────────────────────────


class TestPermissions:
    def test_unauthenticated_cannot_patch_subscription(self, client, member, pro_plan):
        res = client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/", {"plan": "pro"}, format="json"
        )
        assert res.status_code == 401

    def test_regular_user_cannot_patch_subscription(self, regular_client, member, pro_plan):
        res = regular_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/", {"plan": "pro"}, format="json"
        )
        assert res.status_code == 403

    def test_regular_user_cannot_list_plans(self, regular_client):
        res = regular_client.get("/api/v1/admin/subscription-plans/")
        assert res.status_code == 403


# ─── 요청1: 목록 subscription 블록 + ?plan= 필터 ──────────────


class TestUserListSubscription:
    def test_list_exposes_real_subscription_block(self, staff_client, member, pro_plan):
        UserSubscription.objects.create(user=member, plan=pro_plan)

        res = staff_client.get("/api/v1/admin/users/")
        assert res.status_code == 200
        row = next(r for r in res.data["results"] if r["id"] == member.id)
        assert row["subscription"] == {
            "plan_name": "pro",
            "plan_display_name": "프로",
            "status": "active",
            "current_period_end": None,
        }

    def test_subscription_null_when_no_record(self, staff_client, member):
        res = staff_client.get("/api/v1/admin/users/")
        assert res.status_code == 200
        row = next(r for r in res.data["results"] if r["id"] == member.id)
        assert row["subscription"] is None

    def test_plan_filter_uses_subscription_plan_name(self, staff_client, free_plan, pro_plan):
        u_pro = User.objects.create_user(email="pro@example.com", password="Pass1234!")
        u_free = User.objects.create_user(email="free@example.com", password="Pass1234!")
        UserSubscription.objects.create(user=u_pro, plan=pro_plan)
        UserSubscription.objects.create(user=u_free, plan=free_plan)

        res = staff_client.get("/api/v1/admin/users/?plan=pro")
        assert res.status_code == 200
        emails = {r["email"] for r in res.data["results"]}
        assert "pro@example.com" in emails
        assert "free@example.com" not in emails


# ─── 요청2: 구독 강제 변경 PATCH ──────────────────────────────


class TestSubscriptionUpdate:
    def test_creates_subscription_when_none(self, staff_client, member, free_plan, pro_plan):
        assert not UserSubscription.objects.filter(user=member).exists()

        res = staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/", {"plan": "pro"}, format="json"
        )
        assert res.status_code == 200
        assert res.data["plan_name"] == "pro"
        assert res.data["status"] == "active"
        assert res.data["current_period_end"] is None

        sub = UserSubscription.objects.get(user=member)
        assert sub.plan_id == pro_plan.id
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.current_period_end is None

    def test_updates_existing_subscription(self, staff_client, member, free_plan, pro_plan):
        UserSubscription.objects.create(
            user=member, plan=free_plan, status=SubscriptionStatus.CANCELLED
        )
        res = staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/", {"plan": "pro"}, format="json"
        )
        assert res.status_code == 200
        sub = UserSubscription.objects.get(user=member)
        assert sub.plan_id == pro_plan.id
        assert sub.status == SubscriptionStatus.ACTIVE
        assert sub.cancelled_at is None

    def test_downgrade_off_pro_resets_extras_and_pending(
        self, staff_client, member, free_plan, pro_plan
    ):
        """pro→비-pro 수기 변경 시 추가계정 슬롯/예약을 리셋 — 허용량 부풀림 방지."""
        from apps.billing.subscription_utils import get_ig_account_allowance

        sub = UserSubscription.objects.create(
            user=member,
            plan=pro_plan,
            status=SubscriptionStatus.ACTIVE,
            extra_ig_accounts=2,
            pending_extra_ig_accounts=1,
        )

        res = staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/", {"plan": "free"}, format="json"
        )

        assert res.status_code == 200
        sub.refresh_from_db()
        assert sub.plan.name == "free"
        assert sub.extra_ig_accounts == 0
        assert sub.pending_extra_ig_accounts is None
        # 허용량이 1 로 정확히 재계산 (예전엔 1+2=3 으로 부풀었음)
        assert get_ig_account_allowance(member) == 1

    def test_accepts_plan_id(self, staff_client, member, free_plan, pro_plan):
        res = staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/",
            {"plan_id": str(pro_plan.id)},
            format="json",
        )
        assert res.status_code == 200
        assert res.data["plan_name"] == "pro"

    def test_can_assign_inactive_admin_plan(self, staff_client, member, free_plan, admin_plan):
        res = staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/", {"plan": "admin"}, format="json"
        )
        assert res.status_code == 200
        assert res.data["plan_name"] == "admin"

    def test_invalid_plan_name_returns_400(self, staff_client, member, free_plan):
        res = staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/",
            {"plan": "nope"},
            format="json",
        )
        assert res.status_code == 400

    def test_both_plan_and_plan_id_returns_400(self, staff_client, member, pro_plan):
        res = staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/",
            {"plan": "pro", "plan_id": str(pro_plan.id)},
            format="json",
        )
        assert res.status_code == 400

    def test_neither_plan_nor_plan_id_returns_400(self, staff_client, member):
        res = staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/", {}, format="json"
        )
        assert res.status_code == 400

    def test_unknown_user_returns_404(self, staff_client, pro_plan):
        res = staff_client.patch(
            "/api/v1/admin/users/999999/subscription/", {"plan": "pro"}, format="json"
        )
        assert res.status_code == 404

    def test_writes_audit_log(self, staff_client, member, free_plan, pro_plan):
        staff_client.patch(
            f"/api/v1/admin/users/{member.id}/subscription/", {"plan": "pro"}, format="json"
        )
        log = AdminActionLog.objects.filter(
            action=AdminActionLog.Action.USER_SUBSCRIPTION_UPDATE,
            target_id=str(member.id),
        ).first()
        assert log is not None
        assert log.changes["plan_name"]["after"] == "pro"


# ─── 요청3: 어드민 플랜 목록 별칭 ─────────────────────────────


class TestAdminSubscriptionPlanList:
    def test_returns_all_plans_including_inactive(
        self, staff_client, free_plan, pro_plan, admin_plan
    ):
        res = staff_client.get("/api/v1/admin/subscription-plans/")
        assert res.status_code == 200
        names = {p["name"] for p in res.data}
        assert {"free", "pro", "admin"} <= names
        admin_row = next(p for p in res.data if p["name"] == "admin")
        assert admin_row["is_active"] is False
        assert "monthly_price" in admin_row


# ─── 요청4: 대시보드 subscriptions.by_plan ────────────────────


class TestDashboardSubscriptions:
    def _pro_count(self, res):
        return next(
            row["count"] for row in res.data["subscriptions"]["by_plan"] if row["name"] == "pro"
        )

    def test_overview_includes_subscriptions_by_plan(
        self, staff_client, member, free_plan, pro_plan, admin_plan
    ):
        # 테스트 DB 에 기존 데이터가 있을 수 있으므로 절대값이 아닌 델타로 검증한다.
        before = staff_client.get("/api/v1/admin/metrics/overview/")
        assert before.status_code == 200
        assert "subscriptions" in before.data
        base_pro = self._pro_count(before)

        UserSubscription.objects.create(user=member, plan=pro_plan)

        after = staff_client.get("/api/v1/admin/metrics/overview/")
        by_plan = after.data["subscriptions"]["by_plan"]
        # 구조: 동적 리스트 [{name, display_name, count}].
        assert all({"name", "display_name", "count"} <= set(row) for row in by_plan)
        # pro 구독을 1건 추가했으므로 정확히 +1.
        assert self._pro_count(after) == base_pro + 1
        # 활성/비활성 플랜 모두 포함 (admin 은 is_active=False).
        names = {row["name"] for row in by_plan}
        assert {"free", "pro", "admin"} <= names
