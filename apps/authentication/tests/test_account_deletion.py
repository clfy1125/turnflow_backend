"""웹 단독 회원탈퇴 (turnflow.link/delete-account) 테스트.

Google Play 계정 삭제 정책 대응 경로. 여기서 지키려는 것:
  1. **열거 방지** — 가입/미가입 응답이 같아야 한다
  2. **스로틀 scope 생존** — 이 프로젝트 스로틀은 fail-open 이라 scope 이름이
     settings 와 뷰에서 어긋나면 예외 없이 조용히 꺼진다
  3. **구독 중에도 탈퇴가 진행된다** — 기존 me/delete/ 는 409 로 막는데, 이 경로가
     막히면 Google Play 정책 위반이다 (앱으로 돌려보내게 된다)
  4. **동의 강제** — 두 동의 없이는 삭제되지 않는다
  5. **유예 후 파기** — 유예 전에는 파기되지 않고, 만료 후에는 파기된다

⚠️ 테스트 DB 는 dev DB 를 그대로 쓴다(conftest.py 참고) → 절대 카운트 단언 금지,
   이메일은 uuid 로 유일하게 만든다.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication import account_deletion as ad
from apps.emails.models import EmailToken, EmailTokenPurpose

User = get_user_model()

pytestmark = pytest.mark.django_db


def _email() -> str:
    return f"del-{uuid.uuid4().hex[:12]}@test.com"


def _make_user(**extra) -> "User":
    user = User.objects.create_user(email=_email(), password="pw-Str0ng!", **extra)
    return user


def _issue_delete_token(user) -> str:
    _, raw = EmailToken.issue(
        user=user, purpose=EmailTokenPurpose.ACCOUNT_DELETE, ttl_minutes=30
    )
    return raw


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    """스로틀 버킷을 테스트에서 비켜 간다.

    버킷은 **dev Redis 에 그대로 살아 있어** 테스트 간에 누적된다 (이 파일만 돌리면
    통과하고 전체 스위트에서 429 로 죽는 형태로 드러난다).

    ⚠️ `cache.clear()` 로 지우면 안 된다 — 테스트는 dev 캐시를 공유하고, 전체 flush 는
    DM 페이서를 날려 dev 의 DM 발송을 최대 1시간 세운다. 그래서 캐시는 손대지 않고
    뷰의 throttle_classes 만 비운다.

    scope 이름이 settings 와 일치하는지는 test_throttle_scopes_are_registered 가
    `throttle_scope` 속성으로 따로 검증한다 — 그 테스트는 요청을 보내지 않으므로
    이 픽스처의 영향을 받지 않는다.
    """
    from apps.authentication import deletion_views as dv

    for view in (
        dv.AccountDeletionRequestView,
        dv.AccountDeletionVerifyView,
        dv.AccountDeletionConfirmView,
        dv.AccountDeletionRestoreView,
    ):
        monkeypatch.setattr(view, "throttle_classes", [])


@pytest.fixture(autouse=True)
def _no_real_emails(monkeypatch):
    """메일 발송 태스크를 눌러 둔다 — 테스트가 dev 브로커로 새는 것을 막는다."""
    sent: list[tuple] = []
    monkeypatch.setattr(
        "apps.emails.tasks.send_account_deletion_email.delay",
        lambda *a, **k: sent.append(("verify", a, k)),
    )
    monkeypatch.setattr(
        "apps.emails.tasks.send_account_deletion_confirmed_email.delay",
        lambda *a, **k: sent.append(("confirmed", a, k)),
    )
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# 스로틀 scope 생존 — fail-open 이라 조용히 꺼지는 것을 여기서 막는다
# ─────────────────────────────────────────────────────────────────────────────
def test_throttle_scopes_are_registered():
    """뷰가 선언한 scope 가 settings 에 실제로 있는지.

    DRF ScopedRateThrottle 은 등록되지 않은 scope 를 만나면 예외 없이 **통과시킨다**.
    그래서 오타 하나로 공개 메일 발송 경로의 스로틀이 사라져도 아무도 모른다.
    """
    from django.conf import settings

    from apps.authentication import deletion_views as dv

    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    for view in (
        dv.AccountDeletionRequestView,
        dv.AccountDeletionVerifyView,
        dv.AccountDeletionConfirmView,
        dv.AccountDeletionRestoreView,
    ):
        scope = view.throttle_scope
        assert scope in rates, (
            f"{view.__name__}.throttle_scope={scope!r} 가 DEFAULT_THROTTLE_RATES 에 없다 "
            "— 스로틀이 조용히 꺼진 상태다"
        )
        assert rates[scope], f"{scope} 의 rate 가 비어 있다"


# ─────────────────────────────────────────────────────────────────────────────
# 열거 방지
# ─────────────────────────────────────────────────────────────────────────────
def test_request_response_identical_for_unknown_and_known_email(client):
    """가입/미가입 응답이 완전히 같아야 한다."""
    url = reverse("authentication:account-deletion-request")
    user = _make_user()

    known = client.post(url, {"email": user.email}, format="json")
    unknown = client.post(url, {"email": _email()}, format="json")

    assert known.status_code == unknown.status_code == 200
    assert known.data == unknown.data, "가입 여부에 따라 응답이 갈리면 열거 취약점이다"


def test_request_skips_staff_accounts(_no_real_emails):
    """운영자 계정은 메일 한 통으로 사라지지 않는다."""
    staff = _make_user(is_staff=True)
    assert ad.request_deletion(email=staff.email) is False
    assert not _no_real_emails


def test_request_skips_already_pending(_no_real_emails):
    user = _make_user()
    user.deletion_scheduled_at = timezone.now() + timezone.timedelta(days=7)
    user.save(update_fields=["deletion_scheduled_at"])
    assert ad.request_deletion(email=user.email) is False


# ─────────────────────────────────────────────────────────────────────────────
# verify — 토큰을 소비하지 않는다
# ─────────────────────────────────────────────────────────────────────────────
def test_verify_does_not_consume_token():
    """확인 화면을 띄운 뒤에도 토큰이 살아 있어야 한다.

    여기서 consume 하면 화면만 보고 그만둔 사용자, 그리고 링크를 미리 여는 메일
    클라이언트·보안 스캐너 때문에 토큰이 타 버린다.
    """
    user = _make_user()
    raw = _issue_delete_token(user)

    first = ad.describe_pending(raw_token=raw)
    assert first["email_masked"].endswith("@test.com")
    assert "@" in first["email_masked"]
    assert user.email not in first["email_masked"], "전체 이메일을 노출하면 안 된다"

    # 두 번째 조회도 성공해야 한다 = 소비되지 않았다
    ad.describe_pending(raw_token=raw)

    # 그리고 확정은 여전히 가능하다
    result = ad.confirm_deletion(raw_token=raw)
    assert result["purge_at"]


def test_confirm_consumes_token_single_use():
    user = _make_user()
    raw = _issue_delete_token(user)

    ad.confirm_deletion(raw_token=raw)

    with pytest.raises(ad.DeletionError) as exc:
        ad.confirm_deletion(raw_token=raw)
    assert exc.value.code == "invalid_token"


def test_verify_rejects_unknown_token():
    with pytest.raises(ad.DeletionError) as exc:
        ad.describe_pending(raw_token="nope-not-a-real-token")
    assert exc.value.code == "invalid_token"


# ─────────────────────────────────────────────────────────────────────────────
# 동의 강제 — 서버가 막는다
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "payload",
    [
        {"agree_permanent": True, "agree_no_refund": False},
        {"agree_permanent": False, "agree_no_refund": True},
        {"agree_permanent": False, "agree_no_refund": False},
    ],
)
def test_confirm_requires_both_agreements(client, payload):
    """프론트 체크박스만 믿으면 '무환불 소멸을 고지했다'는 증거가 남지 않는다."""
    user = _make_user()
    raw = _issue_delete_token(user)

    res = client.post(
        reverse("authentication:account-deletion-confirm"),
        {"token": raw, **payload},
        format="json",
    )
    assert res.status_code == 400

    user.refresh_from_db()
    assert user.deletion_scheduled_at is None, "동의 없이 탈퇴가 접수됐다"


def test_confirm_endpoint_succeeds_with_both_agreements(client):
    user = _make_user()
    raw = _issue_delete_token(user)

    res = client.post(
        reverse("authentication:account-deletion-confirm"),
        {"token": raw, "agree_permanent": True, "agree_no_refund": True},
        format="json",
    )
    assert res.status_code == 200, res.data
    assert res.data["purge_at"]
    # 고지문이 응답에 함께 실려야 한다 (프론트가 결과 화면에 그린다)
    assert res.data["legal_retention"], "법정 보존 고지가 응답에 없다"

    user.refresh_from_db()
    assert user.is_active is False
    assert user.is_pending_deletion


# ─────────────────────────────────────────────────────────────────────────────
# 유료 구독 중에도 진행돼야 한다 (Google Play 정책의 핵심)
# ─────────────────────────────────────────────────────────────────────────────
def test_confirm_cancels_paid_subscription_instead_of_blocking():
    """기존 me/delete/ 는 409 로 막지만, 이 경로는 해지하고 진행해야 한다.

    막으면 사용자를 앱으로 돌려보내게 되고 그게 정책 위반이다.
    """
    from apps.billing.models import SubscriptionPlan, SubscriptionStatus, UserSubscription

    plan = SubscriptionPlan.objects.filter(name="pro").first()
    if plan is None:
        pytest.skip("pro 플랜 시드가 없는 환경")

    user = _make_user()
    sub = UserSubscription.objects.create(
        user=user,
        plan=plan,
        status=SubscriptionStatus.ACTIVE,
        current_period_end=timezone.now() + timezone.timedelta(days=20),
    )

    raw = _issue_delete_token(user)
    result = ad.confirm_deletion(raw_token=raw)

    assert result["cancelled_subscription"] is True
    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.CANCELLED

    user.refresh_from_db()
    assert user.is_pending_deletion


def test_confirm_clears_pause_schedule():
    """정지 예약을 남기면 handle_pause_expiry 가 탈퇴 계정을 되살려 과금한다."""
    from apps.billing.models import SubscriptionPlan, SubscriptionStatus, UserSubscription

    plan = SubscriptionPlan.objects.filter(name="pro").first()
    if plan is None:
        pytest.skip("pro 플랜 시드가 없는 환경")

    user = _make_user()
    sub = UserSubscription.objects.create(
        user=user,
        plan=plan,
        status=SubscriptionStatus.PAUSED,
        pause_ends_at=timezone.now() + timezone.timedelta(days=30),
        paused_months=1,
    )

    ad.confirm_deletion(raw_token=_issue_delete_token(user))

    sub.refresh_from_db()
    assert sub.status == SubscriptionStatus.CANCELLED
    assert sub.pause_ends_at is None, "자동 재개 예약이 남아 있으면 탈퇴 계정에 과금된다"
    assert sub.paused_months is None


# ─────────────────────────────────────────────────────────────────────────────
# 로그인 시 복구 안내
# ─────────────────────────────────────────────────────────────────────────────
def test_login_on_pending_account_returns_recovery_hint(client):
    """복구 메일을 잃은 사용자에게 로그인 시도가 유일한 창구다."""
    user = _make_user()
    ad.confirm_deletion(raw_token=_issue_delete_token(user))

    res = client.post(
        reverse("authentication:login"),
        {"email": user.email, "password": "pw-Str0ng!"},
        format="json",
    )
    assert res.status_code == 409, res.data
    assert res.data["error"]["details"]["code"] == "account_deletion_pending"
    assert res.data["error"]["details"]["purge_at"]


def test_login_with_wrong_password_does_not_reveal_pending_state(client):
    """비밀번호를 모르는 제3자에게 '탈퇴 진행 중'을 알려주면 열거 취약점이다."""
    user = _make_user()
    ad.confirm_deletion(raw_token=_issue_delete_token(user))

    res = client.post(
        reverse("authentication:login"),
        {"email": user.email, "password": "wrong-password"},
        format="json",
    )
    assert res.status_code == 401
    assert "account_deletion_pending" not in str(res.data)


# ─────────────────────────────────────────────────────────────────────────────
# 복구
# ─────────────────────────────────────────────────────────────────────────────
def test_restore_reactivates_but_not_subscription(client):
    user = _make_user()
    ad.confirm_deletion(raw_token=_issue_delete_token(user))

    _, restore_raw = EmailToken.issue(
        user=user, purpose=EmailTokenPurpose.ACCOUNT_RESTORE, ttl_minutes=60
    )
    res = client.post(
        reverse("authentication:account-deletion-restore"),
        {"token": restore_raw},
        format="json",
    )
    assert res.status_code == 200, res.data
    # 구독은 되살아나지 않는다 — 카드가 이미 삭제됐으므로 이 값이 true 면 거짓 안내다
    assert res.data["subscription_restored"] is False

    user.refresh_from_db()
    assert user.is_active is True
    assert user.deletion_scheduled_at is None
    assert user.deletion_requested_at is None


def test_restore_token_is_single_use(client):
    user = _make_user()
    ad.confirm_deletion(raw_token=_issue_delete_token(user))
    _, restore_raw = EmailToken.issue(
        user=user, purpose=EmailTokenPurpose.ACCOUNT_RESTORE, ttl_minutes=60
    )
    url = reverse("authentication:account-deletion-restore")

    assert client.post(url, {"token": restore_raw}, format="json").status_code == 200
    assert client.post(url, {"token": restore_raw}, format="json").status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# 파기 — 유예 전/후
# ─────────────────────────────────────────────────────────────────────────────
def test_purge_skips_accounts_still_in_grace():
    from apps.authentication.tasks import purge_deleted_accounts

    user = _make_user()
    ad.confirm_deletion(raw_token=_issue_delete_token(user))

    purge_deleted_accounts()

    assert User.objects.filter(pk=user.pk).exists(), "유예 중인 계정이 파기됐다"


def test_purge_deletes_after_grace_expires():
    from apps.authentication.tasks import purge_deleted_accounts

    user = _make_user()
    ad.confirm_deletion(raw_token=_issue_delete_token(user))

    # 유예를 과거로 밀어 만료시킨다
    User.objects.filter(pk=user.pk).update(
        deletion_scheduled_at=timezone.now() - timezone.timedelta(minutes=1)
    )

    purge_deleted_accounts()

    assert not User.objects.filter(pk=user.pk).exists(), "유예 만료 계정이 파기되지 않았다"


def test_purge_skips_staff_even_if_scheduled():
    """권한이 나중에 부여된 계정 방어 — 운영자는 이 경로로 사라지지 않는다."""
    from apps.authentication.tasks import purge_deleted_accounts

    user = _make_user()
    ad.confirm_deletion(raw_token=_issue_delete_token(user))
    User.objects.filter(pk=user.pk).update(
        is_staff=True,
        deletion_scheduled_at=timezone.now() - timezone.timedelta(minutes=1),
    )

    purge_deleted_accounts()

    assert User.objects.filter(pk=user.pk).exists()


def test_purge_deletes_owned_workspace_first():
    """Workspace.owner 가 PROTECT 라 순서를 틀리면 ProtectedError 로 영원히 실패한다."""
    from apps.authentication.tasks import purge_deleted_accounts
    from apps.workspace.models import Workspace

    user = _make_user()
    ws = Workspace.objects.create(
        name=f"ws-{uuid.uuid4().hex[:8]}", slug=f"ws-{uuid.uuid4().hex[:8]}", owner=user
    )
    ad.confirm_deletion(raw_token=_issue_delete_token(user))
    User.objects.filter(pk=user.pk).update(
        deletion_scheduled_at=timezone.now() - timezone.timedelta(minutes=1)
    )

    summary = purge_deleted_accounts()

    assert not User.objects.filter(pk=user.pk).exists(), f"파기 실패: {summary}"
    assert not Workspace.objects.filter(pk=ws.pk).exists()


# ─────────────────────────────────────────────────────────────────────────────
# 고지문
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_endpoint_discloses_legal_retention(client):
    """'7일 후 모두 삭제' 는 사실이 아니다 — 보존 항목·근거·기간이 나와야 한다."""
    res = client.get(reverse("authentication:account-deletion-policy"))
    assert res.status_code == 200
    assert res.data["grace_days"] >= 1
    assert res.data["deleted_items"]

    retention = res.data["legal_retention"]
    assert retention, "법정 보존 고지가 비어 있다"
    for row in retention:
        assert row["item"] and row["basis"] and row["period"]
    # 결제 기록 5년 보존(전자상거래법)이 빠지면 허위 고지가 된다
    assert any("5년" == r["period"] for r in retention)
