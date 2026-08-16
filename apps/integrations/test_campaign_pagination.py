"""캠페인 목록 페이지네이션 + 「전체 선택」 일괄 처리 계약 테스트.

핵심 불변식: **`GET` 목록의 `count` == `all:true` 벌크가 처리하는 건수.**
이게 깨지면 사용자가 "300개 선택됨" 을 보고 눌렀는데 20개만 처리되는 사고가 난다
(페이지네이션 도입 시 가장 흔한 회귀라 여기서 고정한다).
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign
from apps.integrations.test_dm_migration import _client, _conn, _user, _ws

LIST_URL = "/api/v1/integrations/auto-dm-campaigns/"


def _campaign(conn, i, *, status=AutoDMCampaign.Status.PAUSED, source="", name=None):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        name=name or f"캠페인 {i:03d}",
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        media_id=f"m-{i:03d}",  # 게시물이 겹치면 재개가 중복으로 막힌다 → 전부 다르게
        status=status,
        source=source,
        message_template="본문",
    )


@pytest.fixture
def setup(db):
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    for i in range(45):
        _campaign(conn, i, source="dm_migration" if i % 3 == 0 else "")
    return _client(user), ws, conn


@pytest.mark.django_db
def test_list_stays_flat_without_page_and_becomes_envelope_with_page(setup):
    client, ws, _ = setup
    flat = client.get(f"{LIST_URL}?workspace_id={ws.id}").data
    assert isinstance(flat, list) and len(flat) == 45  # 기존 클라이언트 계약 유지

    env = client.get(f"{LIST_URL}?workspace_id={ws.id}&page=1&page_size=20").data
    assert env["count"] == 45
    assert len(env["results"]) == 20
    assert env["previous"] is None and "page=2" in env["next"]

    last = client.get(f"{LIST_URL}?workspace_id={ws.id}&page=3&page_size=20").data
    assert len(last["results"]) == 5 and last["next"] is None

    # 페이지를 넘겨도 항목이 겹치거나 빠지지 않는다(정렬이 안정적이어야 성립).
    seen = []
    for p in (1, 2, 3):
        seen += [
            r["id"] for r in client.get(f"{LIST_URL}?workspace_id={ws.id}&page={p}").data["results"]
        ]
    assert len(seen) == len(set(seen)) == 45


@pytest.mark.django_db
def test_page_size_is_clamped_instead_of_400(setup):
    client, ws, _ = setup
    assert len(client.get(f"{LIST_URL}?workspace_id={ws.id}&page_size=9999").data["results"]) == 45
    assert len(client.get(f"{LIST_URL}?workspace_id={ws.id}&page_size=0").data["results"]) == 1
    assert len(client.get(f"{LIST_URL}?workspace_id={ws.id}&page_size=abc").data["results"]) == 20


@pytest.mark.django_db
def test_bulk_all_covers_every_page_not_just_the_first(setup):
    """「전체 선택」 → 현재 페이지(20개)가 아니라 45개 전부가 처리돼야 한다."""
    client, ws, conn = setup
    listed = client.get(f"{LIST_URL}?workspace_id={ws.id}&page=1&page_size=20").data
    assert listed["count"] == 45 and len(listed["results"]) == 20

    r = client.post(f"{LIST_URL}bulk-resume/?workspace_id={ws.id}", {"all": True}, format="json")
    assert r.status_code == 200
    assert len(r.data["succeeded"]) == listed["count"] == 45, "화면의 count 와 처리 건수가 다르다"
    assert r.data["failed"] == []
    # dev DB 를 공유하므로 전역이 아니라 이 연결로 좁혀서 센다.
    mine = AutoDMCampaign.objects.filter(ig_connection=conn)
    assert mine.filter(status=AutoDMCampaign.Status.ACTIVE).count() == 45


@pytest.mark.django_db
def test_bulk_all_follows_the_same_filters_as_the_list(setup):
    """필터가 걸려 있으면 그 필터에 걸린 것만 대상 — 목록에 안 보이는 건 건드리지 않는다."""
    client, ws, conn = setup
    q = f"workspace_id={ws.id}&source=dm_migration"
    count = client.get(f"{LIST_URL}?{q}&page=1").data["count"]
    assert count == 15  # 45개 중 1/3

    r = client.post(f"{LIST_URL}bulk-resume/?{q}", {"all": True}, format="json")
    assert len(r.data["succeeded"]) == count
    mine = AutoDMCampaign.objects.filter(ig_connection=conn)
    assert mine.filter(status=AutoDMCampaign.Status.ACTIVE).count() == 15
    # 필터 밖(직접 만든 캠페인 30개)은 그대로 일시정지.
    assert mine.filter(source="", status=AutoDMCampaign.Status.PAUSED).count() == 30


@pytest.mark.django_db
def test_bulk_all_respects_search_and_exclude_ids(setup):
    client, ws, conn = setup
    _campaign(conn, 900, name="룩북 특별전")
    _campaign(conn, 901, name="룩북 재입고")
    q = f"workspace_id={ws.id}&search=룩북"
    rows = client.get(f"{LIST_URL}?{q}&page=1").data
    assert rows["count"] == 2
    keep = rows["results"][0]["id"]

    r = client.post(
        f"{LIST_URL}bulk-delete/?{q}", {"all": True, "exclude_ids": [keep]}, format="json"
    )
    assert len(r.data["succeeded"]) == 1
    assert AutoDMCampaign.objects.filter(id=keep).exists(), "제외한 항목이 지워졌다"


@pytest.mark.django_db
def test_bulk_all_refuses_when_over_the_cap(setup, monkeypatch):
    """상한을 넘으면 조용히 일부만 처리하지 말고 거부해야 한다."""
    from apps.integrations.views import AutoDMCampaignViewSet

    monkeypatch.setattr(AutoDMCampaignViewSet, "BULK_ALL_MAX", 10)
    client, ws, conn = setup
    r = client.post(f"{LIST_URL}bulk-resume/?workspace_id={ws.id}", {"all": True}, format="json")
    assert r.status_code == 400
    assert "too_many_targets" in str(r.data)
    # 아무것도 바뀌지 않았다 (한 건도 활성화되지 않음).
    mine = AutoDMCampaign.objects.filter(ig_connection=conn)
    assert mine.filter(status=AutoDMCampaign.Status.PAUSED).count() == 45


@pytest.mark.django_db
def test_bulk_requires_exactly_one_of_ids_or_all(setup):
    client, ws, conn = setup
    cid = str(AutoDMCampaign.objects.filter(ig_connection=conn).first().id)
    url = f"{LIST_URL}bulk-pause/?workspace_id={ws.id}"
    assert client.post(url, {}, format="json").status_code == 400
    assert client.post(url, {"all": True, "ids": [cid]}, format="json").status_code == 400
    assert client.post(url, {"ids": [cid]}, format="json").status_code == 200


@pytest.mark.django_db
def test_bulk_all_isolates_per_item_conflicts(setup):
    """같은 게시물에 활성 캠페인이 이미 있으면 그 건만 실패로 격리(전체 실패 아님)."""
    client, ws, conn = setup
    a = _campaign(conn, 800, status=AutoDMCampaign.Status.ACTIVE, name="선점")
    _campaign(conn, 801, status=AutoDMCampaign.Status.PAUSED, name="충돌")
    AutoDMCampaign.objects.filter(name="충돌").update(media_id=a.media_id)

    r = client.post(f"{LIST_URL}bulk-resume/?workspace_id={ws.id}", {"all": True}, format="json")
    assert r.status_code == 200
    reasons = {f["reason"] for f in r.data["failed"]}
    assert reasons == {"duplicate_active_campaign"}
    assert len(r.data["succeeded"]) == 46  # 47개 중 충돌 1건만 제외


@pytest.mark.django_db
def test_other_workspace_campaigns_are_never_touched(setup):
    """all=true 라도 남의 워크스페이스 캠페인은 대상이 아니다."""
    client, ws, _ = setup
    other_conn = _conn(_ws(_user()))
    other = _campaign(other_conn, 700, name="남의 캠페인")

    client.post(f"{LIST_URL}bulk-delete/?workspace_id={ws.id}", {"all": True}, format="json")
    other.refresh_from_db()
    assert other.status == AutoDMCampaign.Status.PAUSED
    assert timezone.now() is not None  # (import 사용 — 시각 비교는 여기선 불필요)
