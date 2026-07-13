"""소프트 비활성(is_active=False) IG 계정이 자동화에서 제외되는지 검증.

DM 발송 후보 선정(_active_campaigns_for_account)이 비활성 계정을 건너뛰는지 확인 —
이게 걸리면 웹훅/폴링 경로 전반이 함께 차단된다.
테스트 간 페이서 버킷 공유를 피하려고 external_account_id 는 uuid 로 유일화한다.
"""

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.integrations.models import AutoDMCampaign, IGAccountConnection
from apps.integrations.tasks import _active_campaigns_for_account
from apps.workspace.models import Workspace

User = get_user_model()


@pytest.fixture
def conn(db):
    user = User.objects.create_user(
        email=f"enf-{uuid.uuid4().hex[:8]}@example.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="WS", slug=f"ws-{uuid.uuid4().hex[:8]}", owner=user)
    c = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="enfuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    )
    AutoDMCampaign.objects.create(
        ig_connection=c,
        trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
        name="enf campaign",
        message_template="hi",
        status=AutoDMCampaign.Status.ACTIVE,
    )
    return c


@pytest.mark.django_db
def test_active_account_yields_campaign(conn):
    qs = _active_campaigns_for_account(conn.external_account_id)
    assert qs.count() == 1


@pytest.mark.django_db
def test_soft_inactive_account_excluded(conn):
    conn.is_active = False
    conn.save(update_fields=["is_active"])

    qs = _active_campaigns_for_account(conn.external_account_id)
    assert qs.count() == 0  # 비활성 계정은 DM 후보에서 제외
