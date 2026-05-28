"""Telegram 운영 알림 헬퍼.

토큰 refresh / dead-letter 누적 / 배치 실패 등 운영자가 즉시 알아야 할 이벤트를
지정된 Telegram 봇으로 푸시한다.

설정:
    settings.TELEGRAM_BOT_TOKEN — `12345:ABC...` 형태의 봇 토큰
    settings.TELEGRAM_CHAT_ID   — 알림 받을 채널/그룹/개인 chat id

둘 중 하나라도 비어있으면 no-op (개발/로컬 환경에서 안전).
실패는 절대 예외로 던지지 않는다 — best-effort.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
_DEFAULT_TIMEOUT = 10


def send_telegram_notification(
    message: str,
    *,
    parse_mode: str = "Markdown",
    disable_web_page_preview: bool = True,
) -> bool:
    """Telegram 메시지 1건 전송 (best-effort).

    Returns:
        True  — 전송 성공
        False — 키 미설정 / 네트워크 오류 / Telegram API 거부 (조용히 실패)
    """
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "") or ""
    if not token or not chat_id:
        logger.debug("telegram skip: TOKEN/CHAT_ID empty")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message[:4000],  # Telegram 메시지 한도 4096자, 안전 마진
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    try:
        r = requests.post(url, json=payload, timeout=_DEFAULT_TIMEOUT)
        if r.ok:
            return True
        logger.warning(
            "telegram send failed: status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return False
    except requests.RequestException as e:
        logger.warning("telegram send exception: %s", e)
        return False
