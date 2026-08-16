"""apps/admin_api/auth/emails.py — 신규 기기 승인 코드 메일.

코드 저장·소비는 :class:`apps.emails.models.EmailToken` 을 그대로 쓴다. 6자리 코드 + TTL +
1회용 + 원자적 사용처리가 이미 구현돼 있고 회원 인증에서 검증된 코드다 — 어드민용으로
같은 것을 다시 만들 이유가 없다.

메일에는 **링크를 넣지 않는다.** 링크 클릭으로 기기가 승인되면 메일함 접근만으로 2단계를
통과하게 된다. 사람이 코드를 읽어 로그인 화면에 옮겨 적어야, 메일함을 본 사람과 로그인
화면 앞에 있는 사람이 같다는 근거가 생긴다.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.emails.constants import TEMPLATE_ADMIN_DEVICE_CODE
from apps.emails.models import EmailToken, EmailTokenPurpose

logger = logging.getLogger(__name__)


def send_device_code(user, *, device_label: str = "", request_ip: str | None = None) -> int | None:
    """기기 승인 코드 발급 + 메일 큐잉. 반환값은 ``EmailToken`` PK (challenge 에 물린다).

    메일 발송 실패가 로그인 흐름을 500 으로 만들지 않도록 삼킨다 — 코드는 이미 발급됐고,
    사용자는 재시도하면 된다. 실패는 로그로 남는다.
    """
    row, _ = EmailToken.issue(
        user=user,
        purpose=EmailTokenPurpose.ADMIN_DEVICE,
        ttl_minutes=settings.ADMIN_DEVICE_CODE_TTL_MINUTES,
        request_ip=request_ip,
    )
    try:
        from apps.emails.services.sender import send_email

        send_email(
            TEMPLATE_ADMIN_DEVICE_CODE,
            user.email,
            {
                "full_name": user.full_name or user.email.split("@")[0],
                "device_code": row.code,
                "expires_minutes": str(settings.ADMIN_DEVICE_CODE_TTL_MINUTES),
                "device_label": device_label or "(이름 없음)",
                "request_ip": request_ip or "(알 수 없음)",
            },
            user=user,
        )
    except Exception:  # noqa: BLE001 — 메일 실패가 로그인 응답을 깨지 않는다
        logger.exception("[admin-mfa] 기기 승인 코드 메일 발송 실패 user=%s", user.pk)
    return row.pk


def consume_device_code(user, code: str) -> bool:
    """기기 승인 코드 검증(1회용).

    ``EmailToken.consume`` 이 ``used_at`` 을 원자적으로 찍으므로 같은 코드의 재사용은
    자연히 막힌다.
    """
    code = (code or "").strip()
    if not code.isdigit() or len(code) != 6:
        return False
    row = EmailToken.consume(user=user, code=code, purpose=EmailTokenPurpose.ADMIN_DEVICE)
    return row is not None
