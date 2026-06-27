"""WS-2 / §15.8 — EventInbox 일별 파티션 유지 테스트.

검증:
  - 마이그레이션 0030 후 webhook_event_inbox 가 파티션 테이블 + DEFAULT 파티션 보유
  - ensure_eventinbox_partitions: 미래 파티션 선생성 + 재실행 멱등
  - get_or_create(event_key) 가 파티션 테이블에서 정상 적재 + 중복 흡수
  - drop_old_eventinbox_partitions: 보존 초과 일별 파티션만 DROP(최신·DEFAULT 보존)

NOTE(pytest-tests-prefix): tests_*.py 는 자동수집 안 됨 → 파일 경로 명시 실행.
"""

import uuid
from datetime import date, timedelta

import pytest
from django.db import connection

from apps.integrations import partition_maintenance as pm
from apps.integrations.models import EventInbox


def _child_partitions() -> list[str]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = %s",
            [pm.EVENTINBOX_TABLE],
        )
        return [r[0] for r in cur.fetchall()]


@pytest.mark.django_db
class TestEventInboxPartitions:
    def test_table_is_partitioned_with_default(self):
        # 부모는 파티션 테이블(relkind='p'), DEFAULT 파티션 존재
        with connection.cursor() as cur:
            cur.execute("SELECT relkind FROM pg_class WHERE relname = %s", [pm.EVENTINBOX_TABLE])
            assert cur.fetchone()[0] == "p"
        assert pm.DEFAULT_PARTITION in _child_partitions()

    def test_ensure_creates_future_partitions_idempotent(self):
        today = date(2031, 6, 1)
        names1 = pm.ensure_eventinbox_partitions(days_ahead=3, today=today)
        names2 = pm.ensure_eventinbox_partitions(days_ahead=3, today=today)  # 재실행 무에러
        assert names1 == names2 and len(names1) == 4
        parts = _child_partitions()
        for n in names1:
            assert n in parts

    def test_get_or_create_routes_and_dedups(self):
        pm.ensure_eventinbox_partitions()  # 오늘 파티션 보장
        key = f"echo:{uuid.uuid4().hex}"
        o1, c1 = EventInbox.objects.get_or_create(event_key=key, defaults={"event_type": "echo"})
        o2, c2 = EventInbox.objects.get_or_create(event_key=key, defaults={"event_type": "echo"})
        assert c1 is True and c2 is False
        assert o1.pk == o2.pk
        assert EventInbox.objects.filter(event_key=key).count() == 1

    def test_drop_old_removes_only_aged_daily_partitions(self):
        today = date.today()
        old = today - timedelta(days=30)
        pm.ensure_eventinbox_partitions(days_ahead=0, today=old)  # 오래된 파티션 1개
        assert pm._eventinbox_partition_name(old) in _child_partitions()

        dropped = pm.drop_old_eventinbox_partitions(retention_days=7, today=today)
        assert pm._eventinbox_partition_name(old) in dropped
        after = _child_partitions()
        assert pm._eventinbox_partition_name(old) not in after  # 오래된 건 제거
        assert pm.DEFAULT_PARTITION in after  # DEFAULT 는 보존
