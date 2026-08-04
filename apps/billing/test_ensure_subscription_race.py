"""`ensure_subscription` 경쟁 조건 회귀 테스트 (2026-08-05 실장애).

## 무슨 일이 있었나
prod 에서 `GET /api/v1/billing/my-subscription/` 이 **500** 을 냈다:

    UserSubscription.DoesNotExist            ← get() 실패
      ↓ except 블록에서 objects.create()
    psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
      "user_subscriptions_user_id_key"  DETAIL: Key (user_id)=(137) already exists.

구독 행이 아직 없는 **신규 사용자**가 앱을 처음 열 때, SPA 가 같은 엔드포인트를 두 번
호출하면서 두 요청이 모두 `DoesNotExist` 를 받고 모두 INSERT 를 시도했다
(실제 UA: iOS Instagram 인앱 브라우저). 마케팅 유입으로 신규 가입이 몰리면 재발한다.

## 왜 `get_or_create` 여야 하나
Django 의 `get_or_create` 는 INSERT 를 `transaction.atomic` 으로 감싸고 `IntegrityError` 가
나면 **다시 `get()`** 한다. 직접 try/except 로 흉내내면 ① 재조회가 없어 500 이 나거나
② atomic 없이 INSERT 가 실패해 바깥 트랜잭션이 오염된다.

## ⚠️ 이 테스트를 고칠 때 반드시 지킬 것
경쟁 창은 **UserSubscription 조회를 첫 2회 실패**시켜 재현한다:
  1회차 = `user.subscription` 역참조 디스크립터가 내부적으로 부르는 `get()`
  2회차 = `ensure_subscription` 이 직접 부르는 조회
하나만 실패시키면 2회차가 행을 찾아 **조기 반환**하므로 INSERT 에 도달하지 못하고,
테스트가 옛 구현에서도 통과해 무의미해진다(실제로 그 실수를 했다).
검증법: 옛 구현으로 되돌려 이 파일을 돌리면 **실패**해야 한다.

실행: pytest apps/billing/test_ensure_subscription_race.py
"""

import uuid

import pytest
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.billing.models import UserSubscription
from apps.billing.subscription_utils import ensure_subscription, get_free_plan

RACE_WINDOW_FAILS = 2


def _make_user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email=f"race_{uuid.uuid4().hex[:10]}@example.com",
        password="pw12345!",
        full_name="Race",
    )


def _install_race_window(monkeypatch):
    """UserSubscription 조회를 첫 2회 실패시켜 '경쟁 상대가 아직 커밋 전' 상황을 만든다."""
    original_get = QuerySet.get
    state = {"fails": 0}

    def flaky_get(self, *args, **kwargs):
        if self.model is UserSubscription and state["fails"] < RACE_WINDOW_FAILS:
            state["fails"] += 1
            raise UserSubscription.DoesNotExist("simulated race window")
        return original_get(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "get", flaky_get)
    return state


@pytest.mark.django_db
def test_creates_free_subscription_when_absent():
    """기본 동작 — 구독이 없으면 free 로 만든다."""
    user = _make_user()
    assert not UserSubscription.objects.filter(user=user).exists()

    sub = ensure_subscription(user)

    assert sub.user_id == user.id
    assert sub.plan.name == "free"
    assert UserSubscription.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_returns_existing_without_duplicate():
    """이미 있으면 그것을 돌려주고 새로 만들지 않는다."""
    user = _make_user()
    existing = UserSubscription.objects.create(
        user=user, plan=get_free_plan(), current_period_start=timezone.now()
    )
    user.refresh_from_db()

    sub = ensure_subscription(user)

    assert sub.pk == existing.pk
    assert UserSubscription.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_no_integrity_error_when_row_appears_mid_flight(monkeypatch):
    """★ 회귀 테스트 — 옛 구현이라면 UniqueViolation 으로 죽는 지점."""
    user = _make_user()
    existing = UserSubscription.objects.create(
        user=user, plan=get_free_plan(), current_period_start=timezone.now()
    )
    user._state.fields_cache.pop("subscription", None)
    state = _install_race_window(monkeypatch)

    sub = ensure_subscription(user)

    assert state["fails"] == RACE_WINDOW_FAILS, "경쟁 창이 재현되지 않았다 — 테스트가 무의미"
    assert sub.pk == existing.pk, "기존 행을 돌려줘야 한다"
    assert UserSubscription.objects.filter(user=user).count() == 1, "중복 생성됨"


@pytest.mark.django_db
def test_my_subscription_endpoint_survives_the_race(monkeypatch):
    """엔드포인트 레벨 — 같은 상황에서 500 이 아니라 200 이어야 한다."""
    from rest_framework.test import APIClient

    user = _make_user()
    UserSubscription.objects.create(
        user=user, plan=get_free_plan(), current_period_start=timezone.now()
    )
    user._state.fields_cache.pop("subscription", None)
    state = _install_race_window(monkeypatch)

    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get("/api/v1/billing/my-subscription/")

    assert state["fails"] == RACE_WINDOW_FAILS, "경쟁 창이 재현되지 않았다 — 테스트가 무의미"
    assert res.status_code == 200, f"500 회귀: {res.status_code} {res.content[:300]}"
    assert UserSubscription.objects.filter(user=user).count() == 1
