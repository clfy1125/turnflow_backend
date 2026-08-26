"""
analytics Celery tasks — 랜딩 방문 데이터 유지보수.

스케줄(config/settings/base.py CELERY_BEAT_SCHEDULE):
1. cleanup_landing_visits — 매일 KST 03:30, 보존기간(기본 180일) 초과 LandingVisit 배치 삭제.

SignupAttribution 은 TTL 없음 (사용자당 1행 업무 기록, user 삭제 시 CASCADE).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# 한 번에 지우는 pk 청크 크기 — 거대 단일 DELETE(락/WAL 폭증) 방지
_CHUNK_SIZE = 10_000


@shared_task(name="analytics.cleanup_landing_visits")
def cleanup_landing_visits() -> dict:
    """LANDING_VISIT_RETENTION_DAYS(기본 180일) 초과 LandingVisit 배치 삭제 (10k 청크, pk 커서).

    반환: {"retention_days", "deleted"}
    멱등: 이미 지워진 행은 다음 차수에 조회되지 않는다.
    """
    from .models import LandingVisit

    retention_days = settings.LANDING_VISIT_RETENTION_DAYS
    cutoff = timezone.now() - timedelta(days=retention_days)

    total_deleted = 0
    while True:
        pks = list(
            LandingVisit.objects.filter(created_at__lt=cutoff).values_list("pk", flat=True)[
                :_CHUNK_SIZE
            ]
        )
        if not pks:
            break
        deleted, _ = LandingVisit.objects.filter(pk__in=pks).delete()
        total_deleted += deleted

    summary = {"retention_days": retention_days, "deleted": total_deleted}
    logger.info("cleanup_landing_visits: %s", summary)
    return summary


# ──────────────────────────────────────────────────────────────
# Meta 전환 API(CAPI) — 서버발 전환 이벤트
# ──────────────────────────────────────────────────────────────
#
# 왜 Celery 인가: 가입·결제 뷰에서 외부 API 를 동기 호출하면 Meta 가 느릴 때 우리 가입이
# 같이 느려진다(CLAUDE.md §5-3). 계측은 절대 본 기능을 붙잡으면 안 된다.
#
# ⚠️ Meta 는 event_time 이 **7일보다 오래되면 배치 전체를 거부**한다. 재시도 백오프가
#    그 창을 넘기면 조용히 전부 버려지므로 _MAX_EVENT_AGE_SECONDS 로 잘라낸다.

_META_CAPI_MAX_RETRIES = 4
_MAX_EVENT_AGE_SECONDS = 6 * 24 * 3600  # Meta 상한 7일에서 하루 여유


@shared_task(
    name="analytics.send_meta_capi_event",
    bind=True,
    max_retries=_META_CAPI_MAX_RETRIES,
    default_retry_delay=60,
    acks_late=True,
)
def send_meta_capi_event(
    self,
    *,
    event_name: str,
    event_id: str,
    event_time: int,
    email: str = "",
    phone: str = "",
    external_id=None,
    fbc: str = "",
    fbp: str = "",
    client_ip: str = "",
    client_user_agent: str = "",
    event_source_url: str = "",
    value: int | None = None,
    currency: str = "KRW",
) -> dict:
    """전환 이벤트 1건을 Meta 로 전송한다.

    **이벤트는 1건씩 보낸다** — Meta 는 배치 중 하나만 잘못돼도 배치 전체를 거부하므로,
    묶어 보내면 한 건의 결함이 다른 전환까지 날린다.

    ``event_id`` 는 브라우저 픽셀과 **반드시 같은 값**이어야 중복 제거된다
    (규약은 apps/analytics/meta_capi.py docstring).

    ``client_ip`` 는 요청에서 뽑아 인자로만 받는다 — 원본 IP 를 DB 에 저장하지 않기 위해서다.
    """
    from . import meta_capi

    if not meta_capi.is_enabled():
        return {"skipped": "disabled"}

    age = timezone.now().timestamp() - int(event_time)
    if age > _MAX_EVENT_AGE_SECONDS:
        logger.warning(
            "meta_capi 이벤트가 너무 오래됨(버림): name=%s id=%s age=%.0fh",
            event_name,
            event_id,
            age / 3600,
        )
        return {"skipped": "too_old", "age_seconds": int(age)}

    custom_data = None
    if value is not None:
        custom_data = {"currency": currency, "value": str(value)}

    try:
        event = meta_capi.build_event(
            event_name=event_name,
            event_id=event_id,
            event_time=event_time,
            user_data=meta_capi.build_user_data(
                email=email,
                phone=phone,
                external_id=external_id,
                fbc=fbc,
                fbp=fbp,
                client_ip=client_ip,
                client_user_agent=client_user_agent,
            ),
            event_source_url=event_source_url,
            custom_data=custom_data,
        )
    except ValueError as exc:
        # 지원 목록 밖 이벤트 — 재시도해도 같으니 즉시 포기
        logger.error("meta_capi 이벤트 구성 실패: %s", exc)
        return {"skipped": "invalid", "error": str(exc)}

    result = meta_capi.send_events([event])
    if result["ok"]:
        return {"sent": event_name, "event_id": event_id}

    # 4xx 는 payload 문제라 재시도해도 같다 — 429(레이트리밋)만 예외로 재시도한다.
    status = result.get("status")
    if status is not None and 400 <= status < 500 and status != 429:
        logger.error(
            "meta_capi 영구 실패(재시도 안 함): name=%s status=%s body=%s",
            event_name,
            status,
            result.get("body"),
        )
        return {"failed": event_name, "status": status}

    raise self.retry(countdown=60 * (2**self.request.retries))


def dispatch_meta_capi(**kwargs) -> None:
    """CAPI 전송 예약 — **어떤 예외도 호출측으로 던지지 않는다**.

    가입·결제 코드가 이 함수를 부른다. 광고 계측 실패가 가입이나 결제를 깨뜨리면 안 된다.
    비활성 상태면 태스크를 아예 던지지 않는다(큐 낭비 방지).
    """
    from . import meta_capi

    if not meta_capi.is_enabled():
        return
    try:
        send_meta_capi_event.delay(**kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("meta_capi 태스크 예약 실패: %s", kwargs.get("event_name"))
