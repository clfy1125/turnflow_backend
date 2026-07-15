"""WS-2 / §15.8 — EventInbox 일별 파티션 유지 + SentDMLog 배치 아카이브.

EventInbox 는 transient dedup 장부(36h 지난 마커 무가치)라 일별 RANGE 파티션 + 옛 파티션 DROP 으로
유계 관리한다(즉시 DROP, WAL≈0 — 일 570만 행에서도 batch DELETE 부하 없음).
SentDMLog 는 비파티션(전역 UNIQUE 유지)이라 시간 기준 배치 아카이브(현재 기본 비활성).

`integrations.maintain_partitions` Celery 태스크(일 1회 beat)가 아래를 호출한다:
  1) ensure_eventinbox_partitions  — 앞으로 N일치 파티션 '선생성'(행 도착 전에 있어야 DEFAULT 로 안 샘)
  2) drop_old_eventinbox_partitions — 보존일 초과 일별 파티션 DROP
  3) archive_old_sentdmlogs         — (옵션) 오래된 SentDMLog 배치 삭제 — R2 export 선행 전까지 비활성
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from django.db import connection, transaction

logger = logging.getLogger(__name__)

EVENTINBOX_TABLE = "webhook_event_inbox"
DEFAULT_PARTITION = f"{EVENTINBOX_TABLE}_default"


def _eventinbox_partition_name(d: date) -> str:
    return f"{EVENTINBOX_TABLE}_{d:%Y%m%d}"


def _child_partition_names() -> list[str]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "WHERE p.relname = %s",
            [EVENTINBOX_TABLE],
        )
        return [r[0] for r in cur.fetchall()]


def ensure_eventinbox_partitions(
    days_ahead: int | None = None, today: date | None = None
) -> list[str]:
    """오늘부터 days_ahead 일치 일별 파티션을 미리 생성(IF NOT EXISTS). 반환: 확인/생성한 파티션명.

    '선생성'이 핵심 — 행이 도착하기 전에 해당 일자 파티션이 있어야 DEFAULT 로 새지 않고
    DROP 가능한 일별 파티션에 정확히 적재되며, 추후 DEFAULT-overlap 에러도 없다.
    """
    if days_ahead is None:
        days_ahead = getattr(settings, "EVENTINBOX_PARTITION_DAYS_AHEAD", 14)
    today = today or date.today()
    names: list[str] = []
    failed: list[str] = []
    for i in range(0, days_ahead + 1):
        d = today + timedelta(days=i)
        nxt = d + timedelta(days=1)
        name = _eventinbox_partition_name(d)
        # 일자별로 격리 — 한 일자 실패(예: DEFAULT-overlap, 일시 락)가 나머지 일자 선생성을
        # 막지 않게 한다(중단 시 미래 파티션 미생성 → DEFAULT 누수 → wedge). atomic 으로 감싸
        # autocommit/중첩 트랜잭션 양쪽에서 안전하게 부분 실패만 롤백.
        try:
            with transaction.atomic(), connection.cursor() as cur:
                cur.execute(
                    f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF "{EVENTINBOX_TABLE}" '
                    f"FOR VALUES FROM (%s) TO (%s)",
                    [d.isoformat(), nxt.isoformat()],
                )
            names.append(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(name)
            logger.warning("ensure_eventinbox_partitions: %s 생성 실패: %s", name, exc)
    if failed:
        logger.warning(
            "ensure_eventinbox_partitions: %d개 일자 파티션 생성 실패 %s", len(failed), failed
        )
    return names


def drop_old_eventinbox_partitions(
    retention_days: int | None = None, today: date | None = None
) -> list[str]:
    """retention_days 보다 오래된 일별 파티션을 DROP(즉시, WAL≈0). 반환: 드롭한 파티션명.

    DEFAULT 파티션·비일별(YYYYMMDD 아닌) 파티션은 건드리지 않는다.
    DEFAULT 가 비어있지 않으면(=선생성 지연 신호) 경고 로깅.
    """
    if retention_days is None:
        retention_days = getattr(settings, "EVENTINBOX_PARTITION_RETENTION_DAYS", 7)
    today = today or date.today()
    cutoff = today - timedelta(days=retention_days)
    dropped: list[str] = []
    with connection.cursor() as cur:
        for relname in _child_partition_names():
            suffix = relname.rsplit("_", 1)[-1]
            if len(suffix) != 8 or not suffix.isdigit():
                continue  # DEFAULT 등 일별이 아닌 파티션 제외
            try:
                pdate = date(int(suffix[:4]), int(suffix[4:6]), int(suffix[6:8]))
            except ValueError:
                continue
            if pdate < cutoff:
                cur.execute(f'DROP TABLE IF EXISTS "{relname}"')
                dropped.append(relname)
        # DEFAULT 로 샌 행(파티션 선생성 지연 시) 중 보존 초과분을 정리 → DEFAULT 무한 비대화/wedge 방지.
        # EventInbox 는 transient(36h 지난 dedup 마커 무가치)라 보존일 초과 DEFAULT 행 삭제는 무해.
        cur.execute(
            f'DELETE FROM "{DEFAULT_PARTITION}" WHERE received_at < %s', [cutoff.isoformat()]
        )
        cur.execute(f'SELECT count(*) FROM "{DEFAULT_PARTITION}"')
        default_rows = cur.fetchone()[0]
        if default_rows:
            # 보존 이내인데도 DEFAULT 에 행이 있음 = 그 일자 파티션 선생성이 밀렸다는 신호.
            logger.warning(
                "EventInbox DEFAULT 파티션에 %d행 — 파티션 선생성 지연 의심(maintain_partitions/beat 점검)",
                default_rows,
            )
    return dropped


def archive_old_sentdmlogs(retention_days: int | None = None, batch_size: int = 5000) -> dict:
    """retention_days 보다 오래된 SentDMLog 를 배치 삭제. 0/None 이면 **비활성(기본)**.

    ⚠️ SentDMLog 는 업무기록(도착 증빙)이다. R2 export 미구현 상태에서 DELETE 만 하면 손실이므로
    기본 비활성(SENTDMLOG_ARCHIVE_RETENTION_DAYS=0). 활성화 전 반드시 R2 COPY/업로드 선행을 붙일 것(§15.8 (c)).

    ⚠️ 부수효과: 이 삭제는 campaign_stats.new_requester_timeseries 의 사람별 MIN(created_at)
    파생을 왜곡한다('전체 기간' 신규 요청자 차트 축소·재계산 오류). 활성화 전 절차는
    config/settings/base.py 의 SENTDMLOG_ARCHIVE_RETENTION_DAYS 주석(롤업 백필 등)을 반드시 따를 것.
    """
    if retention_days is None:
        retention_days = getattr(settings, "SENTDMLOG_ARCHIVE_RETENTION_DAYS", 0)
    if not retention_days:
        return {"enabled": False, "deleted": 0}

    # TODO(R2 export): 삭제 전 cutoff 이전 구간을 R2 로 COPY → 업로드 검증 → 그 다음에만 DELETE.
    from django.utils import timezone

    from apps.integrations.models import SentDMLog

    cutoff = timezone.now() - timedelta(days=retention_days)
    total = 0
    while True:
        ids = list(
            SentDMLog.objects.filter(created_at__lt=cutoff).values_list("id", flat=True)[
                :batch_size
            ]
        )
        if not ids:
            break
        SentDMLog.objects.filter(id__in=ids).delete()
        total += len(ids)
    return {"enabled": True, "deleted": total, "cutoff": cutoff.isoformat()}
