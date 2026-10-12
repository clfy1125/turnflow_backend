"""어드민 캠페인 목록 — **페이지를 먼저 자르고 그 20건에만 집계**하는지 지킨다.

배경 (2026-10-13 prod 실측):
    ``GET /admin/auto-dm/campaigns/?page=1`` 이 **5.9초** 걸렸다. 응답은 20건·13KB 인데
    ``annotate_campaign_people`` 의 ``COUNT(DISTINCT recipient_user_id)`` 6개가 전 집합에
    걸려 Postgres 가 ``sent_dm_logs`` **433,415행을 정렬**했고, work_mem 을 넘겨 디스크에
    158MB 를 썼다(EXPLAIN: Incremental Sort 5,187ms / Peak Disk 167,144kB).

이 파일이 지키는 계약:
1. 기본 정렬에서는 집계가 **현재 페이지의 pk 로 제한**된다 (전 집합 집계 금지).
2. 그래도 응답 값은 전 집합 집계 경로(``?ordering=people_*``)와 **똑같다**.
3. 캠페인이 늘어도 쿼리 수가 늘지 않는다 (N+1 재발 방지).

⚠️ 파일명이 ``test_*`` 라 자동 수집되지만, 테스트 DB 가 dev DB 라
   **전역 카운트 단언은 금지** — 전부 우리가 만든 캠페인 스코프로 좁혔다.
"""

from __future__ import annotations

import re
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

User = get_user_model()
URL = "/api/v1/admin/auto-dm/campaigns/"


# ─── 픽스처 ──────────────────────────────────────────────────────────


@pytest.fixture
def staff_client(db):
    user = User.objects.create_user(
        email=f"staff-perf-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!", is_staff=True
    )
    c = APIClient(SERVER_NAME="127.0.0.1")
    c.force_authenticate(user=user)
    return c


def _mk_conn():
    owner = User.objects.create_user(
        email=f"owner-perf-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )
    ws = Workspace.objects.create(name="w", slug=f"w-{uuid.uuid4().hex[:8]}", owner=owner)
    return IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username=f"perf_{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    )


def _mk_campaign(conn, name):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
        message_template="hi",
        name=name,
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _mk_log(campaign, recipient, status=SentDMLog.Status.DELIVERED):
    return SentDMLog.objects.create(
        campaign=campaign,
        comment_id=f"c_{uuid.uuid4().hex[:10]}",
        recipient_user_id=recipient,
        recipient_username="",
        message_sent="x",
        status=status,
        dm_kind=SentDMLog.DMKind.OPENING,
        idempotency_key=uuid.uuid4().hex,
    )


@pytest.fixture
def seeded(db):
    """캠페인 3개 + 수신자 여러 명. 목록/정렬 양쪽에서 같은 값이 나와야 한다."""
    conn = _mk_conn()
    tag = uuid.uuid4().hex[:8]
    camps = []
    for i in range(3):
        c = _mk_campaign(conn, f"perf-{tag}-{i}")
        for j in range(i + 1):  # 0번=1명, 1번=2명, 2번=3명
            _mk_log(c, f"r_{tag}_{i}_{j}")
        camps.append(c)
    return {"conn": conn, "campaigns": camps, "tag": tag}


def _rows_for(res, campaigns):
    by_id = {r["id"]: r for r in res.data["results"]}
    return {str(c.id): by_id.get(str(c.id)) for c in campaigns}


# ─── 1. 집계가 페이지로 제한되는가 ────────────────────────────────────


@pytest.mark.django_db
class TestPaginateBeforeAggregate:
    def test_default_ordering_scopes_aggregate_to_page(self, staff_client, seeded):
        """기본 정렬: ``COUNT(DISTINCT recipient_user_id)`` 쿼리가 pk 목록으로 좁혀져야 한다.

        전 집합에 걸리면 sent_dm_logs 전체가 정렬 대상이 된다 — 그게 5.9초의 원인이었다.
        """
        with CaptureQueriesContext(connection) as ctx:
            res = staff_client.get(URL, {"ig_connection_id": str(seeded["conn"].id)})
        assert res.status_code == 200

        distinct_qs = [
            q["sql"] for q in ctx.captured_queries if "COUNT(DISTINCT" in q["sql"].upper()
        ]
        assert distinct_qs, "집계 쿼리가 아예 없다 — people 값이 비었을 것"
        for sql in distinct_qs:
            assert re.search(r'"auto_dm_campaigns"\."id" IN \(', sql), (
                "집계가 전 집합에 걸렸다 (pk IN 절 없음). "
                "페이지를 먼저 자른 뒤 그 pk 로만 집계해야 한다.\n" + sql[:400]
            )

    def test_people_ordering_still_aggregates_everything(self, staff_client, seeded):
        """``?ordering=people_sent`` 은 집계값이 정렬 키라 전 집합 집계가 **정상**이다."""
        with CaptureQueriesContext(connection) as ctx:
            res = staff_client.get(
                URL, {"ig_connection_id": str(seeded["conn"].id), "ordering": "-people_sent"}
            )
        assert res.status_code == 200
        distinct_qs = [
            q["sql"] for q in ctx.captured_queries if "COUNT(DISTINCT" in q["sql"].upper()
        ]
        assert distinct_qs, "정렬을 위한 집계가 사라졌다"
        # 이 경로는 pk 제한이 없어도 된다 — 정렬하려면 전부 세야 하기 때문.
        assert any("ORDER BY" in sql.upper() for sql in distinct_qs)

    def test_query_count_does_not_grow_with_campaigns(self, staff_client, seeded):
        """캠페인이 늘어도 쿼리 수가 그대로여야 한다 (N+1 재발 방지)."""
        conn = seeded["conn"]
        reset_queries()
        with CaptureQueriesContext(connection) as before:
            staff_client.get(URL, {"ig_connection_id": str(conn.id)})

        tag = uuid.uuid4().hex[:8]
        for i in range(6):
            c = _mk_campaign(conn, f"extra-{tag}-{i}")
            _mk_log(c, f"x_{tag}_{i}")

        with CaptureQueriesContext(connection) as after:
            staff_client.get(URL, {"ig_connection_id": str(conn.id)})

        assert len(after.captured_queries) <= len(before.captured_queries) + 1, (
            f"캠페인 6개 추가에 쿼리가 {len(before.captured_queries)}→"
            f"{len(after.captured_queries)} 로 늘었다 — N+1 이다"
        )


# ─── 2. 값이 기존 경로와 같은가 ───────────────────────────────────────


@pytest.mark.django_db
class TestValuesUnchanged:
    def test_both_paths_agree(self, staff_client, seeded):
        """페이지 단위 집계와 전 집합 집계가 **같은 값**을 내야 한다."""
        conn_id = str(seeded["conn"].id)
        fast = staff_client.get(URL, {"ig_connection_id": conn_id})
        full = staff_client.get(URL, {"ig_connection_id": conn_id, "ordering": "-people_sent"})
        assert fast.status_code == full.status_code == 200

        a = _rows_for(fast, seeded["campaigns"])
        b = _rows_for(full, seeded["campaigns"])
        for cid, row_a in a.items():
            row_b = b[cid]
            assert row_a is not None and row_b is not None, f"{cid} 가 한쪽 응답에서 빠졌다"
            for key in ("people", "delivered_count", "delivery_rate", "total_sent"):
                assert row_a[key] == row_b[key], f"{cid}.{key}: {row_a[key]} != {row_b[key]}"

    def test_people_counts_are_real(self, staff_client, seeded):
        """수신자 수가 그대로 people.targets 로 나와야 한다 (집계가 조용히 0 이 되지 않게)."""
        res = staff_client.get(URL, {"ig_connection_id": str(seeded["conn"].id)})
        rows = _rows_for(res, seeded["campaigns"])
        for idx, camp in enumerate(seeded["campaigns"]):
            row = rows[str(camp.id)]
            assert row["people"]["targets"] == idx + 1, (
                f"{camp.name}: targets={row['people']['targets']} (기대 {idx + 1})"
            )

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"status": "active"},
            {"ordering": "-total_sent"},
            {"ordering": "created_at"},
            {"page": 1},
        ],
    )
    def test_filters_and_orderings_still_work(self, staff_client, seeded, params):
        """필터·정렬 조합이 500 없이 동작하고 people 블록이 채워져 있어야 한다."""
        params = {**params, "ig_connection_id": str(seeded["conn"].id)}
        res = staff_client.get(URL, params)
        assert res.status_code == 200, res.content[:300]
        for row in res.data["results"]:
            assert "people" in row and "targets" in row["people"]
