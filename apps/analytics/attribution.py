"""
가입 attribution 캡처 — 이메일 가입(serializer.create)과 Google 가입(view)의 유일한 진입점.

**어떤 예외도 밖으로 던지지 않는다** — attribution 저장 실패가 가입 자체를 막으면 안 된다.
프론트가 보낸 payload 는 신뢰하지 않고 여기서 전부 sanitize 한다 (길이 절단, UUID 파싱).
register serializer 쪽은 JSONField 로 느슨하게 받기만 하면 된다 —
잘못된 attribution 객체가 가입을 400 으로 깨뜨리는 일이 구조적으로 불가능하다.
"""

from __future__ import annotations

import logging
import uuid

from .channels import CH_UNKNOWN, derive_channel
from .models import SignupAttribution

logger = logging.getLogger(__name__)


def capture_signup_attribution(user, payload, signup_kind: str) -> None:
    """가입 attribution 저장 (멱등 — 이미 행이 있으면 덮어쓰지 않음).

    payload: dict | None — 프론트가 보낸 attribution 객체
      {visitor_id, utm_source, utm_medium, utm_campaign, utm_content, referrer, landing_path}
    signup_kind: "email" | "google" (models.SignupKind)

    페이로드가 아예 없으면(None/비-dict/빈 dict) channel="unknown" 으로 저장해
    "프론트 미연동 가입"과 "직접 유입(direct)"을 구분한다.
    """
    try:
        data = payload if isinstance(payload, dict) else {}

        def _s(key: str, max_len: int) -> str:
            value = data.get(key)
            return value.strip()[:max_len] if isinstance(value, str) else ""

        visitor_id = None
        if data.get("visitor_id"):
            try:
                visitor_id = uuid.UUID(str(data["visitor_id"]))
            except (ValueError, TypeError, AttributeError):
                visitor_id = None  # 깨진 visitor_id 는 버리고 나머지는 저장

        utm_source = _s("utm_source", 100)
        utm_medium = _s("utm_medium", 100)
        utm_campaign = _s("utm_campaign", 150)
        utm_content = _s("utm_content", 150)
        referrer = _s("referrer", 500)
        landing_path = _s("landing_path", 300)

        # 페이로드가 있으면 방문 기록과 동일한 derive_channel 로 파생, 없으면 unknown
        channel = derive_channel(utm_source, utm_medium, referrer) if data else CH_UNKNOWN

        SignupAttribution.objects.get_or_create(
            user=user,
            defaults={
                "visitor_id": visitor_id,
                "utm_source": utm_source,
                "utm_medium": utm_medium,
                "utm_campaign": utm_campaign,
                "utm_content": utm_content,
                "referrer": referrer,
                "landing_path": landing_path,
                "channel": channel,
                "signup_kind": signup_kind,
            },
        )
    except Exception:
        logger.exception("signup attribution capture failed user_id=%s", getattr(user, "id", None))
