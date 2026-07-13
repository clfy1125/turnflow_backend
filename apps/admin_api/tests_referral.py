"""어드민 레퍼럴 코드 관리 API 테스트.

대상 (모두 /api/v1/admin/, IsAdminUser):
  - GET/POST         /admin/referral-codes/
  - GET/PATCH/DELETE /admin/referral-codes/{id}/
  - GET              /admin/referral-codes/{id}/redemptions/

테스트 DB 는 깨끗하지 않을 수 있으므로(다른 스위트/시드 잔재) 목록 단언은
"내가 만든 코드가 포함/제외" 방식(멤버십)으로 하고, 절대 카운트에 의존하지 않는다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.admin_api.models import AdminActionLog
from apps.billing.models import ReferralCode, ReferralRedemption, SubscriptionPlan

User = get_user_model()


# ─── 공통 픽스처 ─────────────────────────────────────────────


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def regular_user(db):
    return User.objects.create_user(email="regular-ref@example.com", password="Pass1234!")


@pytest.fixture
def staff_user(db):
    return User.objects.create_user(
        email="staff-ref@example.com", password="Pass1234!", is_staff=True
    )


@pytest.fixture
def staff_client(client, staff_user):
    client.force_authenticate(user=staff_user)
    return client


@pytest.fixture
def regular_client(client, regular_user):
    client.force_authenticate(user=regular_user)
    return client


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
    """운영용 비활성 플랜 — 트라이얼 대상으로 허용되면 안 된다."""
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


def _make_code(plan, code="TESTCODE", **kwargs):
    defaults = {"trial_days": 30, "target_plan": plan}
    defaults.update(kwargs)
    return ReferralCode.objects.create(code=code, **defaults)


def _redeem(code, email):
    user = User.objects.create_user(email=email, password="Pass1234!")
    now = timezone.now()
    return ReferralRedemption.objects.create(
        user=user,
        referral_code=code,
        trial_started_at=now,
        trial_ends_at=now + timedelta(days=code.trial_days),
    )


def _field_errors(res) -> dict:
    """검증 실패(400) 응답의 필드별 에러 dict.

    프로젝트 공통 예외 핸들러가 DRF ValidationError 를
    ``{"success": false, "error": {"code": 400, "message": ..., "details": {field: [...]}}}``
    로 감싼다(CLAUDE.md §6). 필드 단언은 그 details 를 본다.
    """
    return (res.data.get("error") or {}).get("details") or {}


# ─── 권한 ────────────────────────────────────────────────────


class TestPermissions:
    def test_unauthenticated_cannot_list(self, client):
        assert client.get("/api/v1/admin/referral-codes/").status_code == 401

    def test_regular_cannot_list(self, regular_client):
        assert regular_client.get("/api/v1/admin/referral-codes/").status_code == 403

    def test_regular_cannot_create(self, regular_client, pro_plan):
        res = regular_client.post(
            "/api/v1/admin/referral-codes/",
            {"code": "X1", "target_plan": str(pro_plan.id)},
            format="json",
        )
        assert res.status_code == 403


# ─── 생성 ────────────────────────────────────────────────────


class TestCreate:
    def test_create_success_normalizes_code(self, staff_client, pro_plan):
        res = staff_client.post(
            "/api/v1/admin/referral-codes/",
            {
                "code": "welcome2026",
                "target_plan": str(pro_plan.id),
                "trial_days": 45,
                "description": "웰컴",
                "max_uses": 100,
            },
            format="json",
        )
        assert res.status_code == 201, res.data
        assert res.data["code"] == "WELCOME2026"  # 대문자 정규화
        assert res.data["trial_days"] == 45
        assert res.data["target_plan"]["name"] == "pro"
        assert res.data["redemptions_count"] == 0
        assert res.data["converted_count"] == 0
        assert res.data["is_redeemable"] is True
        assert ReferralCode.objects.filter(code="WELCOME2026").exists()

    def test_create_defaults(self, staff_client, pro_plan):
        res = staff_client.post(
            "/api/v1/admin/referral-codes/",
            {"code": "DEFAULTS", "target_plan": str(pro_plan.id)},
            format="json",
        )
        assert res.status_code == 201
        assert res.data["trial_days"] == 30
        assert res.data["is_active"] is True
        assert res.data["max_uses"] is None

    def test_duplicate_code_case_insensitive_400(self, staff_client, pro_plan):
        _make_code(pro_plan, code="DUPLICATE")
        res = staff_client.post(
            "/api/v1/admin/referral-codes/",
            {"code": "duplicate", "target_plan": str(pro_plan.id)},
            format="json",
        )
        assert res.status_code == 400
        assert "code" in _field_errors(res)

    def test_bad_code_format_400(self, staff_client, pro_plan):
        res = staff_client.post(
            "/api/v1/admin/referral-codes/",
            {"code": "has space!", "target_plan": str(pro_plan.id)},
            format="json",
        )
        assert res.status_code == 400

    def test_invalid_plan_400(self, staff_client):
        res = staff_client.post(
            "/api/v1/admin/referral-codes/",
            {"code": "NOPLAN", "target_plan": "00000000-0000-0000-0000-000000000000"},
            format="json",
        )
        assert res.status_code == 400

    def test_rejects_inactive_plan(self, staff_client, admin_plan):
        """운영용 비활성 플랜(admin)으로는 트라이얼 코드를 만들 수 없다."""
        res = staff_client.post(
            "/api/v1/admin/referral-codes/",
            {"code": "ADMINTRIAL", "target_plan": str(admin_plan.id)},
            format="json",
        )
        assert res.status_code == 400
        assert "target_plan" in _field_errors(res)

    def test_rejects_free_plan(self, staff_client, free_plan):
        """무료 플랜으로 트라이얼을 부여하는 것은 무의미 — 거부."""
        res = staff_client.post(
            "/api/v1/admin/referral-codes/",
            {"code": "FREETRIAL", "target_plan": str(free_plan.id)},
            format="json",
        )
        assert res.status_code == 400
        assert "target_plan" in _field_errors(res)

    def test_valid_period_inverted_400(self, staff_client, pro_plan):
        now = timezone.now()
        res = staff_client.post(
            "/api/v1/admin/referral-codes/",
            {
                "code": "INVERTED",
                "target_plan": str(pro_plan.id),
                "valid_from": (now + timedelta(days=10)).isoformat(),
                "valid_until": now.isoformat(),
            },
            format="json",
        )
        assert res.status_code == 400
        assert "valid_until" in _field_errors(res)

    def test_create_writes_audit_log(self, staff_client, pro_plan):
        staff_client.post(
            "/api/v1/admin/referral-codes/",
            {"code": "AUDITED", "target_plan": str(pro_plan.id)},
            format="json",
        )
        log = AdminActionLog.objects.filter(
            action=AdminActionLog.Action.REFERRAL_CREATE, target_repr="AUDITED"
        ).first()
        assert log is not None
        assert log.changes["target_plan"]["after"] == "pro"


# ─── 목록 ────────────────────────────────────────────────────


class TestList:
    def test_list_includes_created_code_with_counts(self, staff_client, pro_plan):
        code = _make_code(pro_plan, code="LISTED", max_uses=10)
        _redeem(code, "r1@example.com")
        red2 = _redeem(code, "r2@example.com")
        red2.converted_to_paid = True
        red2.save(update_fields=["converted_to_paid"])

        res = staff_client.get("/api/v1/admin/referral-codes/")
        assert res.status_code == 200
        row = next(r for r in res.data["results"] if r["code"] == "LISTED")
        assert row["redemptions_count"] == 2
        assert row["converted_count"] == 1

    def test_filter_is_active(self, staff_client, pro_plan):
        _make_code(pro_plan, code="ACTIVEONE", is_active=True)
        _make_code(pro_plan, code="INACTIVEONE", is_active=False)

        res = staff_client.get("/api/v1/admin/referral-codes/?is_active=false")
        codes = {r["code"] for r in res.data["results"]}
        assert "INACTIVEONE" in codes
        assert "ACTIVEONE" not in codes

    def test_filter_target_plan(self, staff_client, pro_plan, free_plan):
        _make_code(pro_plan, code="PROCODE")
        _make_code(free_plan, code="FREECODE")

        res = staff_client.get(f"/api/v1/admin/referral-codes/?target_plan={pro_plan.id}")
        codes = {r["code"] for r in res.data["results"]}
        assert "PROCODE" in codes
        assert "FREECODE" not in codes

    def test_search_by_code(self, staff_client, pro_plan):
        _make_code(pro_plan, code="SEARCHABLE", description="찾아줘")
        res = staff_client.get("/api/v1/admin/referral-codes/?search=SEARCHAB")
        codes = {r["code"] for r in res.data["results"]}
        assert "SEARCHABLE" in codes


# ─── 상세 / 수정 ─────────────────────────────────────────────


class TestUpdate:
    def test_toggle_is_active(self, staff_client, pro_plan):
        code = _make_code(pro_plan, code="TOGGLE", is_active=True)
        res = staff_client.patch(
            f"/api/v1/admin/referral-codes/{code.id}/", {"is_active": False}, format="json"
        )
        assert res.status_code == 200
        assert res.data["is_active"] is False
        code.refresh_from_db()
        assert code.is_active is False

    def test_update_writes_audit_with_changes(self, staff_client, pro_plan):
        code = _make_code(pro_plan, code="CHANGEME", trial_days=30)
        staff_client.patch(
            f"/api/v1/admin/referral-codes/{code.id}/", {"trial_days": 60}, format="json"
        )
        log = AdminActionLog.objects.filter(
            action=AdminActionLog.Action.REFERRAL_UPDATE, target_id=str(code.id)
        ).first()
        assert log is not None
        assert log.changes["trial_days"] == {"before": 30, "after": 60}

    def test_max_uses_below_current_uses_400(self, staff_client, pro_plan):
        code = _make_code(pro_plan, code="TOOLOW", max_uses=10)
        _redeem(code, "u1@example.com")
        _redeem(code, "u2@example.com")
        ReferralCode.objects.filter(pk=code.pk).update(current_uses=2)

        res = staff_client.patch(
            f"/api/v1/admin/referral-codes/{code.id}/", {"max_uses": 1}, format="json"
        )
        assert res.status_code == 400
        assert "max_uses" in _field_errors(res)

    def test_update_duplicate_code_400(self, staff_client, pro_plan):
        _make_code(pro_plan, code="EXISTING")
        code = _make_code(pro_plan, code="RENAMEME")
        res = staff_client.patch(
            f"/api/v1/admin/referral-codes/{code.id}/", {"code": "existing"}, format="json"
        )
        assert res.status_code == 400

    def test_update_same_code_ok(self, staff_client, pro_plan):
        """자기 자신의 코드로 재저장은 중복이 아니어야 한다(exclude self)."""
        code = _make_code(pro_plan, code="SELFSAME")
        res = staff_client.patch(
            f"/api/v1/admin/referral-codes/{code.id}/",
            {"code": "selfsame", "description": "수정"},
            format="json",
        )
        assert res.status_code == 200
        assert res.data["description"] == "수정"

    def test_retrieve_404(self, staff_client):
        res = staff_client.get(
            "/api/v1/admin/referral-codes/00000000-0000-0000-0000-000000000000/"
        )
        assert res.status_code == 404


# ─── 삭제 ────────────────────────────────────────────────────


class TestDelete:
    def test_delete_unused_code_204(self, staff_client, pro_plan):
        code = _make_code(pro_plan, code="DELETEME")
        res = staff_client.delete(f"/api/v1/admin/referral-codes/{code.id}/")
        assert res.status_code == 204
        assert not ReferralCode.objects.filter(pk=code.pk).exists()
        assert AdminActionLog.objects.filter(
            action=AdminActionLog.Action.REFERRAL_DELETE, target_repr="DELETEME"
        ).exists()

    def test_delete_used_code_409(self, staff_client, pro_plan):
        code = _make_code(pro_plan, code="USEDCODE")
        _redeem(code, "keep@example.com")

        res = staff_client.delete(f"/api/v1/admin/referral-codes/{code.id}/")
        assert res.status_code == 409
        assert res.data["code"] == "referral_has_redemptions"
        # 코드는 보존되어야 한다
        assert ReferralCode.objects.filter(pk=code.pk).exists()


# ─── 사용 이력 ───────────────────────────────────────────────


class TestRedemptions:
    def test_lists_redemptions_for_code(self, staff_client, pro_plan):
        code = _make_code(pro_plan, code="WITHUSES")
        _redeem(code, "a@example.com")
        _redeem(code, "b@example.com")
        # 다른 코드의 사용은 섞이지 않아야 함
        other = _make_code(pro_plan, code="OTHERUSES")
        _redeem(other, "c@example.com")

        res = staff_client.get(f"/api/v1/admin/referral-codes/{code.id}/redemptions/")
        assert res.status_code == 200
        assert res.data["count"] == 2
        emails = {r["user_email"] for r in res.data["results"]}
        assert emails == {"a@example.com", "b@example.com"}

    def test_filter_converted(self, staff_client, pro_plan):
        code = _make_code(pro_plan, code="CONVFILTER")
        _redeem(code, "notconv@example.com")
        conv = _redeem(code, "conv@example.com")
        conv.converted_to_paid = True
        conv.converted_at = timezone.now()
        conv.save(update_fields=["converted_to_paid", "converted_at"])

        res = staff_client.get(
            f"/api/v1/admin/referral-codes/{code.id}/redemptions/?converted_to_paid=true"
        )
        assert res.status_code == 200
        emails = {r["user_email"] for r in res.data["results"]}
        assert emails == {"conv@example.com"}

    def test_redemptions_404_for_unknown_code(self, staff_client):
        res = staff_client.get(
            "/api/v1/admin/referral-codes/00000000-0000-0000-0000-000000000000/redemptions/"
        )
        assert res.status_code == 404
