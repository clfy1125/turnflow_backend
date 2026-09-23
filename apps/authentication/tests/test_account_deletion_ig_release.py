"""탈퇴 확정 시 IG 연동 점유 해제 (CS #baf92c82).

지키려는 것 — 실제 사고에서 나왔다:

  계정을 갈아타려고 옛 계정을 탈퇴한 사용자가, 새 계정에서 같은 인스타그램 계정을
  연동하지 못하고 **7일 내내 막힌다.** 우리 탈퇴는 즉시 삭제가 아니라 유예 7일
  소프트 삭제인데, 그동안 `IGAccountConnection` 행이 `status=active` 로 살아 있고
  점유 판정이 `status != REVOKED` 이기 때문이다. 고객은 시킨 대로 탈퇴까지 했는데
  `ALREADY_CONNECTED_ELSEWHERE` 만 반복해서 만난다(실측: 15분간 19회 시도, 전부 차단).

  → `confirm_deletion` 이 소유 IG 연동을 `disconnect()` 해서 점유를 **즉시** 푼다.

⚠️ 테스트 DB 는 dev DB 를 그대로 쓴다(conftest.py) → 전역 카운트 단언 금지, 이메일은 uuid.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.authentication import account_deletion as ad
from apps.emails.models import EmailToken, EmailTokenPurpose
from apps.integrations.models import IGAccountConnection
from apps.integrations.services import InstagramOAuthService
from apps.workspace.models import Membership, Workspace

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_graph_calls(monkeypatch):
    """웹훅 구독 해제는 Graph 호출 — 테스트에서 실제로 나가면 안 된다."""
    calls: list[str] = []

    def _fake(ig_user_id, access_token):  # noqa: ANN001
        calls.append(ig_user_id)
        return True

    monkeypatch.setattr(InstagramOAuthService, "unsubscribe_webhooks", staticmethod(_fake))
    return calls


def _user(prefix="igrel"):
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4().hex[:12]}@test.com", password="pw-Str0ng!"
    )


def _ws(user):
    ws = Workspace.objects.create(
        name="igrel-ws", slug=f"igrel-{uuid.uuid4().hex[:10]}", owner=user
    )
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws, ext_id=None, status=IGAccountConnection.Status.ACTIVE):
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=ext_id or f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=status,
        is_active=status == IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token"
    conn.save()
    return conn


def _delete_token(user) -> str:
    _, raw = EmailToken.issue(user=user, purpose=EmailTokenPurpose.ACCOUNT_DELETE, ttl_minutes=30)
    return raw


def test_confirm_deletion_releases_ig_for_another_workspace():
    """★ 본체: 탈퇴 확정 직후 그 IG 를 다른 워크스페이스가 연동할 수 있어야 한다."""
    leaver = _user("leaver")
    old_ws = _ws(leaver)
    ext_id = f"ig_{uuid.uuid4().hex[:12]}"
    _conn(old_ws, ext_id=ext_id)

    newcomer = _user("newcomer")
    new_ws = _ws(newcomer)

    # 탈퇴 전에는 점유 중이라 새 워크스페이스가 막힌다 (이 사고의 재현).
    assert IGAccountConnection.find_conflicting_connection(ext_id, new_ws) is not None

    result = ad.confirm_deletion(raw_token=_delete_token(leaver))

    assert result["instagram_disconnected"] == 1
    # 유예 중이라 계정·연동 행은 아직 살아 있지만, 점유는 풀려 있어야 한다.
    leaver.refresh_from_db()
    assert leaver.is_active is False
    assert leaver.deletion_scheduled_at is not None
    assert IGAccountConnection.find_conflicting_connection(ext_id, new_ws) is None


def test_confirm_deletion_revokes_and_wipes_token(_no_graph_calls):
    """점유 해제는 REVOKED + 토큰 폐기 + 웹훅 구독 해제까지 간다."""
    leaver = _user()
    ws = _ws(leaver)
    conn = _conn(ws)

    ad.confirm_deletion(raw_token=_delete_token(leaver))

    conn.refresh_from_db()
    assert conn.status == IGAccountConnection.Status.REVOKED
    assert conn.is_active is False
    assert (conn.access_token or "") == ""
    # 주인 없는 연동으로 댓글 웹훅이 계속 날아오면 안 된다.
    assert conn.external_account_id in _no_graph_calls


def test_already_revoked_connection_is_not_touched(_no_graph_calls):
    """이미 REVOKED 인 연동은 점유하지 않으므로 건드리지 않는다(불필요한 Graph 호출 금지)."""
    leaver = _user()
    ws = _ws(leaver)
    _conn(ws, status=IGAccountConnection.Status.REVOKED)

    result = ad.confirm_deletion(raw_token=_delete_token(leaver))

    assert result["instagram_disconnected"] == 0
    assert _no_graph_calls == []


def test_soft_deactivated_connection_is_also_released():
    """소프트 비활성(is_active=False)도 점유로 치므로 함께 풀어야 한다."""
    leaver = _user()
    ws = _ws(leaver)
    ext_id = f"ig_{uuid.uuid4().hex[:12]}"
    conn = _conn(ws, ext_id=ext_id)
    conn.deactivate(reason="test")
    assert conn.status == IGAccountConnection.Status.ACTIVE  # 점유는 유지된 상태

    other_ws = _ws(_user("other"))
    assert IGAccountConnection.find_conflicting_connection(ext_id, other_ws) is not None

    ad.confirm_deletion(raw_token=_delete_token(leaver))

    assert IGAccountConnection.find_conflicting_connection(ext_id, other_ws) is None


def test_disconnect_failure_does_not_block_deletion(monkeypatch):
    """연동 해제가 터져도 탈퇴는 확정된다 — 탈퇴를 막는 쪽이 더 나쁘다."""
    leaver = _user()
    ws = _ws(leaver)
    _conn(ws)

    def _boom(self, reason="user_requested"):  # noqa: ANN001
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(IGAccountConnection, "disconnect", _boom)

    result = ad.confirm_deletion(raw_token=_delete_token(leaver))

    assert result["instagram_disconnected"] == 0
    leaver.refresh_from_db()
    assert leaver.is_active is False
    assert leaver.deletion_scheduled_at is not None


def test_restore_reports_reconnect_required():
    """복구해도 IG 는 안 돌아온다 — 화면이 재연동을 안내할 수 있어야 한다."""
    leaver = _user()
    ws = _ws(leaver)
    _conn(ws)

    ad.confirm_deletion(raw_token=_delete_token(leaver))
    leaver.refresh_from_db()
    out = ad.restore_account(user=leaver)

    assert out["instagram_reconnect_required"] is True
    assert out["subscription_restored"] is False
