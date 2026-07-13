"""IG 계정 활성화 조정 엔드포인트 테스트.

GET/POST /billing/ig-account-activation/ — 허용량 초과 시 활성 계정 재선택.
비활성 처리 시 해당 계정의 활성 캠페인 PAUSE, in-flight DM SKIP 확인.
더러운 테스트 DB 대응: 이메일/slug/외부ID 는 uuid 로 유일화.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.billing.subscription_utils import count_active_ig_connections, ensure_subscription
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email=f"igact-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


@pytest.fixture
def workspace(db, user):
    return Workspace.objects.create(name="WS", slug=f"ws-{uuid.uuid4().hex[:8]}", owner=user)


def _conn(ws, i):
    return IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:8]}",
        username=f"u{i}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    )


@pytest.mark.django_db
class TestIGAccountActivation:
    def _client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_get_flags_adjustment_when_over_allowance(self, user, workspace):
        # 무료 플랜 허용량 1, 활성 연동 2 → 조정 필요
        _conn(workspace, 0)
        _conn(workspace, 1)
        ensure_subscription(user)

        res = self._client(user).get(reverse("billing:ig-account-activation"))

        assert res.status_code == 200
        data = res.json()
        assert data["max_ig_accounts"] == 1
        assert data["total_accounts"] == 2
        assert data["active_accounts"] == 2
        assert data["needs_activation_adjustment"] is True
        assert len(data["accounts"]) == 2
        assert {"id", "username", "is_active", "status", "workspace_name"} <= set(
            data["accounts"][0].keys()
        )

    def test_post_selects_active_and_deactivates_rest(self, user, workspace):
        c0 = _conn(workspace, 0)
        c1 = _conn(workspace, 1)
        ensure_subscription(user)

        res = self._client(user).post(
            reverse("billing:ig-account-activation"),
            {"active_account_ids": [str(c0.id)]},
            format="json",
        )

        assert res.status_code == 200
        data = res.json()
        assert data["active_accounts"] == 1
        assert data["needs_activation_adjustment"] is False
        c0.refresh_from_db()
        c1.refresh_from_db()
        assert c0.is_active is True
        assert c1.is_active is False  # 소프트 비활성 (연결/토큰 보존)
        assert c1.status == IGAccountConnection.Status.ACTIVE

    def test_post_deactivation_pauses_campaign_and_skips_dm(self, user, workspace):
        c0 = _conn(workspace, 0)
        c1 = _conn(workspace, 1)
        camp = AutoDMCampaign.objects.create(
            ig_connection=c1,
            trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
            name="c1 campaign",
            message_template="hi",
            status=AutoDMCampaign.Status.ACTIVE,
        )
        log = SentDMLog.objects.create(
            campaign=camp,
            recipient_user_id="rcpt_1",
            comment_id=f"c_{uuid.uuid4().hex[:8]}",
            idempotency_key=uuid.uuid4().hex,
            status=SentDMLog.Status.QUEUED,
        )
        ensure_subscription(user)

        self._client(user).post(
            reverse("billing:ig-account-activation"),
            {"active_account_ids": [str(c0.id)]},
            format="json",
        )

        camp.refresh_from_db()
        log.refresh_from_db()
        assert camp.status == AutoDMCampaign.Status.PAUSED
        assert log.status == SentDMLog.Status.SKIPPED

    def test_post_rejects_over_allowance(self, user, workspace):
        c0 = _conn(workspace, 0)
        c1 = _conn(workspace, 1)
        ensure_subscription(user)

        res = self._client(user).post(
            reverse("billing:ig-account-activation"),
            {"active_account_ids": [str(c0.id), str(c1.id)]},  # 허용량 1 초과
            format="json",
        )
        assert res.status_code == 400

    def test_post_rejects_foreign_account_id(self, user, workspace):
        _conn(workspace, 0)
        ensure_subscription(user)

        res = self._client(user).post(
            reverse("billing:ig-account-activation"),
            {"active_account_ids": [str(uuid.uuid4())]},
            format="json",
        )
        assert res.status_code == 400

    def test_post_clears_review_flag(self, user, workspace):
        c0 = _conn(workspace, 0)
        sub = ensure_subscription(user)
        sub.ig_activation_review_needed = True
        sub.save(update_fields=["ig_activation_review_needed"])

        self._client(user).post(
            reverse("billing:ig-account-activation"),
            {"active_account_ids": [str(c0.id)]},
            format="json",
        )

        sub.refresh_from_db()
        assert sub.ig_activation_review_needed is False


@pytest.mark.django_db
class TestActiveConnectionCount:
    def test_count_excludes_soft_inactive(self, user, workspace):
        c0 = _conn(workspace, 0)
        _conn(workspace, 1)
        assert count_active_ig_connections(user) == 2

        c0.deactivate()
        assert count_active_ig_connections(user) == 1  # 비활성은 슬롯을 비움
