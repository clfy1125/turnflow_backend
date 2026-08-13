"""어드민 프론트 요청서 19차 회귀 테스트 — DM-19 (`user_view`).

이 라운드가 막는 것은 하나다: **"고객이 보는 화면"이라는 이름을 달고 거짓을 말하는 것.**

어드민 로그 상세는 운영자 어휘(`error_*`)로 그린다. 거기에 고객이 실제로 보고 있는
문장(`user_view`)을 나란히 붙였다. CS 가 고객과 다른 언어로 응대하던 문제를 없애려는
것인데, 그 값이 유저 콘솔과 조금이라도 갈리면 **없는 것보다 나쁘다** — CS 가 그 문구를
근거로 응대하기 때문이다.

그래서 여기서 고정하는 것은 전부 항등이다:

  ① `user_view` == 유저 API 의 `frontend_action` (description 제외) — 값이 같아야 한다
  ② 사본이 아니라 **호출**이어야 한다 — 서버 문구를 고치면 어드민도 함께 바뀐다
  ③ `description` 은 빼되 나머지 8키는 빠짐없이 — 프론트가 그릴 칸이 비면 안 된다
  ④ 목록(`/logs/`)·수신자(`/recipients/`)에는 **넣지 않는다**(요청서 명시)

⚠️ 테스트 DB 는 dev DB 라 더럽다(test-db-not-clean). 여기 테스트는 전부 자기가 만든
   로그 1건만 보므로 전역 집계 격리는 필요 없다.

실행:
    docker compose exec web pytest apps/admin_api/tests_admin_19th_round.py
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.integrations.dm_frontend_actions import build_frontend_action
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.integrations.serializers import SentDMLogSerializer
from apps.workspace.models import Workspace

DETAIL_URL = "/api/v1/admin/auto-dm/logs/{}/"
LIST_URL = "/api/v1/admin/auto-dm/logs/"
RECIPIENTS_URL = "/api/v1/admin/auto-dm/recipients/"

pytestmark = pytest.mark.django_db

# `user_view` 가 반드시 담아야 하는 키 — 프론트가 그리는 칸과 1:1.
USER_VIEW_KEYS = {
    "user_reason",
    "severity",
    "type",
    "title",
    "cause",
    "next_step",
    "checklist",
    "cta",
}


@pytest.fixture
def staff_client(db):
    user = get_user_model().objects.create_user(
        email=f"staff19-{uuid.uuid4().hex[:8]}@t.dev", password="x", is_staff=True
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _mk_campaign():
    owner = get_user_model().objects.create_user(
        email=f"own19-{uuid.uuid4().hex[:8]}@t.dev", password="x"
    )
    ws = Workspace.objects.create(name="w", slug=f"w19-{uuid.uuid4().hex[:8]}", owner=owner)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=uuid.uuid4().hex[:12],
        username=f"ig{uuid.uuid4().hex[:6]}",
        status=IGAccountConnection.Status.ACTIVE,
    )
    return AutoDMCampaign.objects.create(
        ig_connection=conn, name=f"c19-{uuid.uuid4().hex[:6]}", media_id="m1"
    )


def _log(camp, status, *, code="", subcode="", msg=""):
    return SentDMLog.objects.create(
        campaign=camp,
        recipient_user_id=f"r{uuid.uuid4().hex[:8]}",
        recipient_username="buyer",
        status=status,
        dm_kind=SentDMLog.DMKind.OPENING,
        error_code=code,
        error_subcode=subcode,
        error_message=msg,
        idempotency_key=uuid.uuid4().hex,
    )


def _detail(client, log):
    res = client.get(DETAIL_URL.format(log.id))
    assert res.status_code == 200, res.data
    return res.data


# 요청서가 배경으로 든 네 줄 — 운영자 어휘와 고객 어휘가 갈리는 실제 조합.
# (status, code, subcode, message, 기대 user_reason)
DIVERGENT_CASES = [
    (SentDMLog.Status.SKIPPED, "", "", "유령 오프닝 정리", "ghost_opening_cleanup"),
    (SentDMLog.Status.FAILED_TOKEN, "190", "", "", "connection_lost"),
    (SentDMLog.Status.FAILED_PARAM, "100", "2534025", "", "hidden_request"),
    (SentDMLog.Status.FAILED_NO_TRACE, "", "", "", "delivery_unconfirmed"),
]


class TestUserViewMatchesUserConsole:
    """① 어드민이 보여주는 '고객 화면'이 실제 고객 화면과 같아야 한다."""

    @pytest.mark.parametrize("status,code,subcode,msg,expected_reason", DIVERGENT_CASES)
    def test_matches_user_api_exactly(
        self, staff_client, status, code, subcode, msg, expected_reason
    ):
        camp = _mk_campaign()
        log = _log(camp, status, code=code, subcode=subcode, msg=msg)

        admin_view = _detail(staff_client, log)["user_view"]
        # 고객이 실제로 받는 값 — 유저 API 가 쓰는 바로 그 시리얼라이저.
        user_action = SentDMLogSerializer(log).data["frontend_action"]

        assert admin_view["user_reason"] == expected_reason
        for key in USER_VIEW_KEYS:
            assert admin_view[key] == user_action[key], f"{expected_reason}.{key} 불일치"

    def test_is_a_call_not_a_copy(self, staff_client, monkeypatch):
        """② 사본이면 서버 문구를 고쳐도 어드민만 옛 문구로 남는다.

        문구 함수를 갈아끼웠을 때 어드민 응답이 **따라 바뀌면** 호출이고,
        안 바뀌면 어딘가에 사본이 있다는 뜻이다.
        """
        camp = _mk_campaign()
        log = _log(camp, SentDMLog.Status.FAILED_TOKEN, code="190")

        sentinel = "★교체된문구★"
        real = build_frontend_action

        def _patched(*args, **kwargs):
            return {**real(*args, **kwargs), "title": sentinel}

        monkeypatch.setattr("apps.integrations.dm_frontend_actions.build_frontend_action", _patched)
        assert _detail(staff_client, log)["user_view"]["title"] == sentinel


class TestUserViewShape:
    """③ 프론트가 그릴 칸이 비지 않아야 한다."""

    def test_has_all_keys_and_no_description(self, staff_client):
        camp = _mk_campaign()
        log = _log(camp, SentDMLog.Status.FAILED_PARAM, code="100", subcode="2534025")

        view = _detail(staff_client, log)["user_view"]
        assert set(view) == USER_VIEW_KEYS
        # 요청서: "description 은 필요 없습니다. 주셔도 쓰지 않습니다."
        assert "description" not in view

    @pytest.mark.parametrize(
        "status",
        [
            SentDMLog.Status.DELIVERED,
            SentDMLog.Status.READ,
            SentDMLog.Status.QUEUED,
        ],
    )
    def test_present_on_non_error_logs(self, staff_client, status):
        """성공·읽음·대기 로그에도 나와야 한다 — 고객은 그 로그에도 같은 모달을 본다."""
        camp = _mk_campaign()
        view = _detail(staff_client, _log(camp, status))["user_view"]
        assert view["title"], f"{status} 의 제목이 비었다"
        assert set(view) == USER_VIEW_KEYS

    def test_admin_vocabulary_still_intact(self, staff_client):
        """어드민 자신의 어휘(`error_*`)를 밀어내지 않았는지.

        `user_view` 는 **덧붙이는 것**이지 대체가 아니다. 같은 로그에서 두 어휘가
        실제로 다르다는 것까지 확인한다(그 차이가 이 기능의 존재 이유다).
        """
        camp = _mk_campaign()
        log = _log(camp, SentDMLog.Status.SKIPPED, msg="유령 오프닝 정리")

        data = _detail(staff_client, log)
        assert data["error_title"], "운영자 어휘가 사라졌다"
        assert data["user_view"]["title"] == "중복 발송을 방지했어요"
        assert data["error_title"] != data["user_view"]["title"]


class TestNotOnListEndpoints:
    """④ 목록에는 넣지 않는다 — 한 행에 두 어휘가 섞이면 표가 어느 쪽 말인지 알 수 없다."""

    def test_absent_from_log_list(self, staff_client):
        camp = _mk_campaign()
        _log(camp, SentDMLog.Status.FAILED_TOKEN, code="190")

        res = staff_client.get(LIST_URL, {"campaign_id": str(camp.id)})
        assert res.status_code == 200
        assert res.data["results"], "테스트 로그가 목록에 없다"
        for row in res.data["results"]:
            assert "user_view" not in row

    def test_absent_from_recipients(self, staff_client):
        camp = _mk_campaign()
        _log(camp, SentDMLog.Status.FAILED_TOKEN, code="190")

        res = staff_client.get(RECIPIENTS_URL, {"campaign_id": str(camp.id)})
        assert res.status_code == 200
        for row in res.data["results"]:
            assert "user_view" not in row


class TestPermissions:
    def test_requires_staff(self, db):
        camp = _mk_campaign()
        log = _log(camp, SentDMLog.Status.FAILED_TOKEN, code="190")

        client = APIClient()
        client.force_authenticate(
            user=get_user_model().objects.create_user(
                email=f"plain19-{uuid.uuid4().hex[:8]}@t.dev", password="x"
            )
        )
        assert client.get(DETAIL_URL.format(log.id)).status_code == 403
