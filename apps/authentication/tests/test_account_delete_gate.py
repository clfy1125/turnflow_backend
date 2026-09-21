"""앱 내 회원탈퇴(`DELETE /auth/me/delete/`)의 구독 차단 게이트.

여기서 지키려는 것 — 전부 실제 사고(CS #247995c5)에서 나왔다:

  1. **카드 없는 무료 체험은 탈퇴를 막지 않는다.** 가입 1초 뒤 자동 지급되는 프로 30일
     (`trial_kind="auto"`, 빌링키 없음)이 탈퇴를 봉쇄하면, 잘못 만든 계정을 지우려는
     사용자가 **원하지도 않은 구독 해지**를 먼저 하게 된다(이탈 지표까지 오염된다).
  2. **카드가 등록된 유료 구독은 여전히 막는다.** 잔여 유료기간 소멸 + 빌링키 고아는
     카드가 있을 때만 생기는 문제이고, 그 동의는 공개 웹 경로가 따로 받는다.
  3. **409 는 사유가 화면에 닿아야 한다.** 이 뷰는 DRF 예외 핸들러를 우회하므로
     `detail` 과 envelope 를 동시에 실어 보낸다. 프론트 탈퇴 모달이 401 만 분기하고
     나머지를 generic 문구로 뭉개고 있어(2026-09-22 배포본), 한쪽만 담으면 조용히 사라진다.
  4. **막을 때도 빠져나갈 길을 준다** — `web_deletion_url`(공개 탈퇴 경로).

⚠️ 테스트 DB 는 dev DB 를 그대로 쓴다(conftest.py) → 카운트 단언 금지, 이메일은 uuid.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

User = get_user_model()

pytestmark = pytest.mark.django_db


def _email() -> str:
    return f"deletegate-{uuid.uuid4().hex[:12]}@test.com"


def _user():
    return User.objects.create_user(email=_email(), password="pw-Str0ng!")


def _pro_plan():
    from apps.billing.models import SubscriptionPlan

    plan = SubscriptionPlan.objects.filter(name="pro").first()
    if plan is None:
        pytest.skip("pro 플랜 시드가 없는 환경")
    return plan


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


URL = "/api/v1/auth/me/delete/"


def test_cardless_auto_trial_can_delete():
    """카드 없는 자동 프로 체험은 탈퇴를 막지 않는다 (CS #247995c5 의 직접 원인)."""
    from apps.billing.models import SubscriptionStatus, UserSubscription

    user = _user()
    user_id = user.pk
    UserSubscription.objects.create(
        user=user,
        plan=_pro_plan(),
        status=SubscriptionStatus.TRIALING,
        current_period_end=timezone.now() + timezone.timedelta(days=30),
        trial_used_at=timezone.now(),
        trial_kind="auto",
        # 빌링키 없음 — 카드 미등록 체험
    )

    res = _client(user).delete(URL)

    assert res.status_code == 204, res.data
    assert not User.objects.filter(pk=user_id).exists()


def test_card_backed_subscription_still_blocked():
    """카드가 등록된 유료 구독은 여전히 막는다 — 잔여기간/빌링키 정리가 필요하다."""
    from apps.billing.models import SubscriptionStatus, UserSubscription

    user = _user()
    sub = UserSubscription.objects.create(
        user=user,
        plan=_pro_plan(),
        status=SubscriptionStatus.ACTIVE,
        current_period_end=timezone.now() + timezone.timedelta(days=20),
    )
    sub.toss_billing_key = "bk_test_dummy"  # 암호화 setter
    sub.save(update_fields=["_encrypted_toss_billing_key"])
    assert sub.has_billing_key

    res = _client(user).delete(URL)

    assert res.status_code == 409
    assert res.data["error"]["details"]["code"] == "active_subscription"
    assert User.objects.filter(pk=user.pk).exists()


def test_block_response_carries_reason_in_both_shapes():
    """`detail` 과 envelope 를 동시에 — 프론트가 어느 쪽을 읽든 사유가 보여야 한다."""
    from apps.billing.models import SubscriptionStatus, UserSubscription

    user = _user()
    sub = UserSubscription.objects.create(
        user=user,
        plan=_pro_plan(),
        status=SubscriptionStatus.ACTIVE,
        current_period_end=timezone.now() + timezone.timedelta(days=20),
    )
    sub.toss_billing_key = "bk_test_dummy"
    sub.save(update_fields=["_encrypted_toss_billing_key"])

    res = _client(user).delete(URL)

    assert res.status_code == 409
    assert res.data["detail"], "detail 이 비면 프론트 기존 코드가 사유를 못 읽는다"
    assert res.data["detail"] == res.data["error"]["message"], "두 경로의 문구가 갈리면 안 된다"
    assert res.data["error"]["details"]["web_deletion_url"].endswith("/delete-account")


def test_free_plan_can_delete():
    """무료 플랜은 구독이 있어도 막지 않는다 (회귀 방지)."""
    from apps.billing.models import SubscriptionPlan, SubscriptionStatus, UserSubscription

    free = SubscriptionPlan.objects.filter(name="free").first()
    if free is None:
        pytest.skip("free 플랜 시드가 없는 환경")

    user = _user()
    user_id = user.pk
    UserSubscription.objects.create(user=user, plan=free, status=SubscriptionStatus.ACTIVE)

    res = _client(user).delete(URL)

    assert res.status_code == 204, res.data
    assert not User.objects.filter(pk=user_id).exists()


def test_no_subscription_can_delete():
    """구독 레코드 자체가 없는 계정 (회귀 방지)."""
    user = _user()
    user_id = user.pk

    res = _client(user).delete(URL)

    assert res.status_code == 204, res.data
    assert not User.objects.filter(pk=user_id).exists()
