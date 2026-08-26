"""전환 이벤트 발사 헬퍼 — 가입/체험/결제 코드가 부르는 **유일한 진입점**.

여기 한 곳으로 모으는 이유: ``event_id`` 규약과 사용자 매칭 파라미터 조립이 세 군데로
흩어지면 한 곳만 바뀌어도 **중복 제거가 조용히 깨진다**(전환 2배 집계). 규약을 바꿔야
한다면 이 파일만 고치면 된다.

⚠️ 모든 함수는 **예외를 밖으로 던지지 않는다.** 광고 계측 실패가 가입·결제를 깨뜨리면
   안 된다 (attribution.capture_signup_attribution 과 같은 원칙).
"""

from __future__ import annotations

import logging

from django.utils import timezone

from .meta_capi import EVENT_COMPLETE_REGISTRATION, EVENT_PURCHASE, EVENT_START_TRIAL
from .tasks import dispatch_meta_capi

logger = logging.getLogger(__name__)


def client_meta_from_request(request) -> dict:
    """요청에서 IP·UA 를 뽑는다 (Meta 매칭 품질용).

    ⚠️ 원본 IP 는 **DB 에 저장하지 않는다** — 여기서 뽑아 태스크 인자로만 흘려보내고
       전송 후 버린다 (LandingVisit 이 ip_hash 만 남기는 것과 같은 원칙).
    ⚠️ ``REMOTE_ADDR`` 을 쓴다. ``X-Forwarded-For`` 를 직접 읽으면 위조 가능하고,
       프록시 신뢰 처리는 이미 미들웨어/`NUM_PROXIES` 계층의 몫이다.
    """
    if request is None:
        return {}
    try:
        return {
            "client_ip": request.META.get("REMOTE_ADDR", "") or "",
            "client_user_agent": (request.META.get("HTTP_USER_AGENT", "") or "")[:500],
        }
    except Exception:  # noqa: BLE001
        return {}


def _match_params(user) -> dict:
    """사용자의 Meta 매칭 파라미터 (이메일 + 회원ID + fbc/fbp).

    fbc/fbp 는 가입 시점에 SignupAttribution 에 저장해 둔 값이다 — 광고 클릭 순간에만
    얻을 수 있어서, 결제처럼 한참 뒤 일어나는 이벤트에서는 여기서 꺼내는 수밖에 없다.
    """
    params = {"email": getattr(user, "email", "") or "", "external_id": getattr(user, "id", None)}
    try:
        attr = getattr(user, "signup_attribution", None)
        if attr is not None:
            params["fbc"] = attr.fbc or ""
            params["fbp"] = attr.fbp or ""
    except Exception:  # noqa: BLE001
        # 귀속 행이 없거나 조회 실패 — 매칭 품질만 낮아지고 전송 자체는 유효하다
        pass
    return params


def track_signup(user, request=None) -> None:
    """CompleteRegistration — ``event_id = str(user.id)``.

    ⚠️ 프론트 픽셀이 쓰는 값과 **같아야** 한다 (배포본 확인: ``String(data.user.id)``).
    """
    try:
        dispatch_meta_capi(
            event_name=EVENT_COMPLETE_REGISTRATION,
            event_id=str(user.id),
            event_time=int(timezone.now().timestamp()),
            **_match_params(user),
            **client_meta_from_request(request),
        )
    except Exception:  # noqa: BLE001
        logger.exception("track_signup 실패 user_id=%s", getattr(user, "id", None))


def track_trial_started(subscription, request=None) -> None:
    """StartTrial — ``event_id = str(subscription.id)``.

    체험은 한 사람이 여러 번 시작할 수 있으므로 ``user.id`` 를 쓰면 두 번째 체험이 첫
    번째와 같은 id 가 되어 Meta 가 중복으로 지운다. 구독 id 를 쓴다.

    ⚠️ 2026-08-26 현재 **프론트 픽셀은 이 이벤트를 쏘지 않는다**(함수는 있으나 호출부
       없음). 프론트가 붙기 전까지는 서버 단독 집계이며, 붙을 때 같은 규약을 써야 한다.
    """
    try:
        user = subscription.user
        amount = getattr(subscription, "monthly_amount_snapshot", None)
        dispatch_meta_capi(
            event_name=EVENT_START_TRIAL,
            event_id=str(subscription.id),
            event_time=int(timezone.now().timestamp()),
            value=int(amount) if amount else 0,
            **_match_params(user),
            **client_meta_from_request(request),
        )
    except Exception:  # noqa: BLE001
        logger.exception("track_trial_started 실패 sub=%s", getattr(subscription, "id", None))


def track_purchase(payment, request=None) -> None:
    """Purchase — ``event_id = str(payment.id)`` (PaymentHistory UUID).

    ⚠️ 프론트 픽셀이 쓰는 값과 같다 (배포본 확인: ``{eventID: e.id}``).
    ⚠️ **월 갱신 결제에서는 부르지 않는다** (2026-08-26 제품 결정). 광고 최적화는 "이
       광고가 유료 고객을 만들었나"를 보는 것이라 첫 결제가 신호이고, 갱신까지 보내면
       같은 사람을 매달 다시 세어 ROAS 가 부풀려진다. 갱신 경로는 브라우저도 없어
       IP/UA 가 비고 매칭 품질도 낮다.
    """
    try:
        user = payment.user
        dispatch_meta_capi(
            event_name=EVENT_PURCHASE,
            event_id=str(payment.id),
            event_time=int(timezone.now().timestamp()),
            value=int(payment.amount or 0),
            **_match_params(user),
            **client_meta_from_request(request),
        )
    except Exception:  # noqa: BLE001
        logger.exception("track_purchase 실패 payment=%s", getattr(payment, "id", None))
