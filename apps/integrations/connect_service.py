"""IG 연동 행 생성/갱신 후의 **공통 후처리** — 연동 콜백과 인스타 로그인이 공유한다.

왜 모으는가: 연동 직후 돌려야 하는 백그라운드 작업이 다섯 가지인데(웹훅 구독, 인사이트
부트스트랩, DM 이전 선분석, 프로필 사진 캐싱, 토큰오류 발송 되살림), 이게 뷰 안에 인라인으로
있으면 **두 번째 진입점(인스타 로그인)이 생기는 순간 한쪽만 빠진다.** 실제로 빠지면 증상이
전부 "조용한 결함"이다 — 웹훅을 안 붙이면 댓글이 영영 안 들어오고(무음), 프로필 사진을 안
받아 오면 IG CDN 서명이 만료돼 나중에 깨진다.

⚠️ 전부 **best-effort** 다. 하나라도 실패했다고 연동 자체를 실패로 돌리면 안 된다 —
   토큰은 이미 발급됐고, 사용자는 화면에서 "연동 실패"를 보지만 실제로는 연동된 상태가 된다.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def subscribe_webhooks(instagram_account_id: str, access_token: str) -> None:
    """계정별 웹훅 구독 (댓글·메시지). 실패해도 예외를 던지지 않는다.

    ⚠️ 이게 빠지면 **댓글 웹훅이 한 건도 안 들어온다** — 자동 DM 이 통째로 죽는데
       화면에는 아무 오류도 안 뜬다(memory: webhook-subscription-auto-disable).
    """
    from .services import InstagramOAuthService

    try:
        result = InstagramOAuthService.subscribe_to_webhooks(
            ig_user_id=instagram_account_id,
            access_token=access_token,
            fields="comments,messages",
        )
        logger.debug("Webhook subscription result for %s: %s", instagram_account_id, result)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to subscribe webhooks for %s: %s", instagram_account_id, exc)


def post_connect_fanout(connection) -> None:
    """연동 행이 저장된 **뒤** 돌리는 백그라운드 작업 묶음 (웹훅 구독은 별도).

    - 인사이트 부트스트랩: 프론트가 따로 sync 를 부르지 않아도 즉시 데이터가 생기게
    - DM 이전 선분석: 계정당 수 분~수십 분이라 사용자가 열어보기 전에 미리 돌린다(7일 캐시)
    - 프로필 사진 캐싱: IG CDN URL 은 서명된 일시 URL 이라 그대로 두면 나중에 깨진다
    - 토큰오류 발송 되살림: 재연동이면 FAILED_TOKEN 으로 막혔던 DM 을 창 안에서 되살린다
      (신규 연동이면 대상이 없어 no-op — 안전)
    """
    conn_id = str(connection.id)

    try:
        from apps.insights.tasks import bootstrap_account

        bootstrap_account.delay(conn_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to enqueue insights bootstrap (non-fatal): %s", exc)

    try:
        from .tasks import prewarm_dm_migration

        prewarm_dm_migration.delay(conn_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to enqueue dm-migration prewarm (non-fatal): %s", exc)

    try:
        from .tasks import sync_ig_profile_picture

        sync_ig_profile_picture.delay(conn_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to enqueue profile sync (non-fatal): %s", exc)

    try:
        from .tasks import revive_failed_token_logs

        revive_failed_token_logs.delay(conn_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to enqueue reconnect revive (non-fatal): %s", exc)

    logger.info("post_connect_fanout enqueued for connection=%s", conn_id)


def attach_connection(workspace, profile) -> tuple[object | None, str]:
    """인스타 **로그인** 경로의 연동 행 생성/갱신. 반환 ``(connection, "")`` 또는 ``(None, 사유)``.

    ⭐ 연동 콜백(``InstagramIntegrationViewSet.connect_callback``)의 게이트를 그대로 가져오되
       **순서만 로그인 상황에 맞춘다**:
         - 전역 유일성(하나의 IG 계정 = 하나의 워크스페이스): 로그인에서는 이 검사가
           **애초에 걸릴 수 없다** — 다른 워크스페이스가 이미 그 계정을 쓰고 있으면 우리는
           그쪽 owner 로 **로그인시키기** 때문이다(뷰가 먼저 판정한다). 그래도 방어로 둔다.
         - 플랜 한도: 새 사용자는 무료(또는 자동 체험 프로) + 활성 0 이라 항상 통과한다.
           그래도 검사한다 — 기존 사용자가 IG 로 로그인하며 **다른** 계정을 붙일 수 있다.

    ``profile`` 은 ``apps.authentication.instagram.InstagramProfile``.
    """
    from django.utils import timezone

    from apps.billing.subscription_utils import (
        count_active_ig_connections,
        get_ig_account_allowance,
    )

    from .models import IGAccountConnection
    from .services import InstagramOAuthService

    connection = (
        IGAccountConnection.objects.filter(
            workspace=workspace,
            external_account_id=profile.user_id,
        )
        .order_by("-created_at")
        .first()
    )
    is_new_row = connection is None

    if is_new_row:
        conflict = IGAccountConnection.find_conflicting_connection(profile.user_id, workspace)
        if conflict is not None:
            logger.warning(
                "instagram login: 연동 차단(글로벌 중복) ig=%s ws=%s conflict_ws=%s",
                profile.user_id,
                workspace.id,
                conflict.workspace_id,
            )
            return None, "ALREADY_CONNECTED_ELSEWHERE"
        connection = IGAccountConnection(
            workspace=workspace,
            external_account_id=profile.user_id,
        )

    allowance = get_ig_account_allowance(workspace.owner)
    current_active = count_active_ig_connections(workspace.owner)
    takes_new_slot = is_new_row or connection.status != IGAccountConnection.Status.ACTIVE
    if takes_new_slot and allowance != -1 and current_active >= allowance:
        logger.warning(
            "instagram login: 연동 차단(플랜 한도) ws=%s allowance=%s",
            workspace.id,
            allowance,
        )
        return None, "PLAN_LIMIT_EXCEEDED"

    connection.username = profile.username or profile.name or ""
    connection.account_type = profile.account_type or "BUSINESS"
    connection.access_token = profile.access_token
    connection.token_expires_at = profile.token_expires_at
    connection.scopes = InstagramOAuthService.REQUIRED_SCOPES
    connection.status = IGAccountConnection.Status.ACTIVE
    connection.last_verified_at = timezone.now()
    connection.error_message = ""
    # 새 토큰을 받았으므로 이전 '사망' 흔적을 지운다 — 안 지우면 재연동한 계정이 스트라이크를
    # 이어받아 다음 사망 확정이 부당하게 빨라진다.
    connection.token_dead_strikes = 0
    connection.token_dead_first_seen_at = None
    connection.reconnect_reason = ""
    if not connection.is_active and (allowance == -1 or current_active < allowance):
        connection.is_active = True
    connection.save()

    subscribe_webhooks(profile.user_id, profile.access_token)
    post_connect_fanout(connection)
    return connection, ""
