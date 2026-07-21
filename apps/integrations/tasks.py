"""
Instagram 통합 Celery 태스크.

99.9% DM 발송 보증 시스템의 비동기 컴포넌트:

- process_comment_and_send_dm: 댓글 웹훅 → 스팸 검사 → DM 발송 (진입점)
- send_dm_task:               단일 SentDMLog 행을 ACCEPTED까지 진행 (멱등)
- reconcile_accepted_dms:     ACCEPTED + 5분/35분 경과 건을 능동 조회로 검증
- reconcile_stuck_submitting: SUBMITTING 60초+ 정체 건을 안전 재시도
- dead_letter_alerter:        FAILED_TOKEN/FAILED_NO_TRACE 누적 알림
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .dm_exceptions import (
    DMSendError,
    DMTransientError,
    ErrorClassification,
    exception_to_classification,
)
from .models import (
    AutoDMCampaign,
    EventInbox,
    IGAccountConnection,
    SeenComment,
    SentDMLog,
    SpamCommentLog,
)
from .services import (
    CommentReplyPermanentError,
    InstagramCommentService,
    InstagramMediaService,
    InstagramMessagingService,
    InstagramOAuthService,
    MockInstagramProvider,
    scrub_secrets,
)

# ===== 웹훅 구독 재확정 (DR 컷오버 / Meta auto-disable 대비) =====
# Meta 는 콜백 엔드포인트가 반복 실패하면 계정별 웹훅 구독을 auto-disable 한다
# (엣지 장애·DR 컷오버 후 댓글 웹훅이 조용히 끊겨 캠페인이 무음이 되는 원인).
# 서버 이전 직후(startup.sh) + 주기적(beat 6h)으로 comments/messages 구독을 재확정한다.
REQUIRED_WEBHOOK_FIELDS = ("comments", "messages")


def _subscribed_field_names(sub_response: dict) -> set:
    """subscribed_apps GET 응답에서 구독 필드명 집합 추출(API 버전별 shape 방어)."""
    names = set()
    for app in (sub_response or {}).get("data", []) or []:
        for f in app.get("subscribed_fields", []) or []:
            if isinstance(f, dict) and f.get("name"):
                names.add(f["name"])
            elif isinstance(f, str):
                names.add(f)
    return names


def resubscribe_active_connections(check_only: bool = False) -> dict:
    """ACTIVE IG 연동 계정들의 웹훅 구독을 점검하고 필수 필드 누락 시 재구독.

    멱등·계정별 best-effort(한 계정 실패해도 나머지 진행). check_only=True 면 조회만.
    """
    summary = {
        "checked": 0,
        "ok": 0,
        "resubscribed": 0,
        "failed": 0,
        "skipped_expired": 0,
        "details": [],
    }
    now = timezone.now()
    qs = IGAccountConnection.objects.filter(
        status=IGAccountConnection.Status.ACTIVE, is_active=True
    )
    for conn in qs.iterator():
        igid = conn.external_account_id
        if not igid:
            continue
        if conn.token_expires_at and conn.token_expires_at <= now:
            summary["skipped_expired"] += 1
            continue
        summary["checked"] += 1
        try:
            token = conn.access_token
            sub = InstagramOAuthService.get_webhook_subscriptions(igid, token)
            missing = [f for f in REQUIRED_WEBHOOK_FIELDS if f not in _subscribed_field_names(sub)]
            if not missing:
                summary["ok"] += 1
                continue
            if check_only:
                summary["details"].append(f"{igid}: missing={missing} (check-only)")
                continue
            InstagramOAuthService.subscribe_to_webhooks(
                ig_user_id=igid,
                access_token=token,
                fields=",".join(REQUIRED_WEBHOOK_FIELDS),
            )
            summary["resubscribed"] += 1
            summary["details"].append(f"{igid}: resubscribed (was missing {missing})")
            logger.info("resubscribed webhooks ig=%s missing=%s", igid, missing)
        except Exception as e:  # noqa: BLE001 — 계정별 best-effort
            summary["failed"] += 1
            summary["details"].append(f"{igid}: ERROR {e!r}")
            logger.warning("resubscribe webhooks failed ig=%s: %s", igid, e)
    return summary


@shared_task
def resubscribe_all_webhooks(check_only: bool = False) -> dict:
    """주기(beat)/DR 훅. 활성 사이트에서만 실제 구독 변경. 재구독/실패 시 Telegram 경고."""
    from apps.core.site_control import is_active_site

    if not is_active_site():
        logger.info("resubscribe_all_webhooks: passive site — skip")
        return {"skipped": "passive_site"}
    result = resubscribe_active_connections(check_only=check_only)
    if result.get("resubscribed") or result.get("failed"):
        try:
            from apps.core.telegram import send_telegram_notification

            send_telegram_notification(
                "🔔 IG 웹훅 구독 점검: 재구독 %s · 실패 %s · 정상 %s/%s"
                % (result["resubscribed"], result["failed"], result["ok"], result["checked"])
            )
        except Exception:
            logger.exception("resubscribe_all_webhooks telegram 알림 실패 (non-fatal)")
    return result


# ===== 댓글 매칭 / 누락 보정 장부 공용 헬퍼 =====


def _active_campaigns_for_account(ig_user_id: str, now=None):
    """계정(IG user id)의 활성·예약창 내 캠페인 후보 queryset.

    웹훅 처리와 누락 보정 폴링이 동일한 후보 선정 규칙을 공유하도록 단일화.
    """
    qs = (
        AutoDMCampaign.objects.filter(status=AutoDMCampaign.Status.ACTIVE)
        .filter(AutoDMCampaign.schedule_window_q(now))
        .filter(ig_connection__is_active=True)  # 소프트 비활성 계정은 자동화 제외
        .select_related("ig_connection", "ig_connection__workspace")
    )
    if ig_user_id:
        qs = qs.filter(ig_connection__external_account_id=ig_user_id)
    return qs


def _matched_campaigns_for_comment(*, ig_user_id, media_id, comment_text, now=None):
    """이 댓글에 트리거되는(매체+키워드 매칭) 활성 캠페인 목록.

    웹훅 경로의 매칭 규칙과 동일 — 누락 보정 폴링이 재사용한다.
    next_media webhook-attach 같은 웹훅 전용 부수효과는 포함하지 않는다.
    """
    return [
        c
        for c in _active_campaigns_for_account(ig_user_id, now)
        if c.matches_media(media_id) and c.matches_keyword(comment_text)
    ]


def _record_seen_comment(
    *, ig_connection_id, comment_id: str, media_id: str, source: str, triggered: bool = False
) -> bool:
    """댓글 관측 장부(SeenComment)에 멱등 기록. 반환값은 신규 생성 여부(True=처음 봄).

    폴링은 반환값(False=이미 봄=앵커)으로 페이지네이션 종료를 판단한다.
    웹훅 경로는 반환값을 무시하고 호출부에서 예외를 삼켜 DM 흐름을 막지 않는다.
    """
    ttl_days = getattr(settings, "MISSED_COMMENT_LEDGER_TTL_DAYS", 10)
    _, created = SeenComment.objects.get_or_create(
        ig_connection_id=ig_connection_id,
        comment_id=comment_id,
        defaults={
            "media_id": media_id or "",
            "source": source,
            "triggered": triggered,
            "expires_at": timezone.now() + timedelta(days=ttl_days),
        },
    )
    return created


logger = logging.getLogger(__name__)

# P4: Action Block(정책 위반 일시 차단) 신호로 보는 Meta 에러 코드.
# 368 = "temporarily blocked for policies violations". 감지 시 계정 쿨다운(에스컬레이팅).
_ACTION_BLOCK_CODES = {368}


# ===== 발송 속도 제어 / defer 헬퍼 (item 1·2) =====


def _messaging_window(log) -> timedelta:
    """이 로그가 발송 가능한 메시징 윈도우.

    comment Private Reply 는 7일, user_id DM(story/reward) 는 24h.
    rate-limit/transient 로 계속 defer 되더라도 이 윈도우가 지나면 graceful 종결한다.
    """
    return timedelta(days=7) if log.comment_id else timedelta(hours=24)


def _resolve_plan_name(campaign) -> str:
    """캠페인 소유 워크스페이스의 요금제 이름(free/starter/pro/enterprise). 실패 시 free."""
    try:
        from apps.billing.subscription_utils import get_user_plan

        owner = campaign.ig_connection.workspace.owner
        return (get_user_plan(owner).name or "free").lower()
    except Exception:  # noqa: BLE001 - 요금제 조회 실패는 보수적으로 free 취급
        return "free"


def _rate_defer(log, campaign, ig_conn):
    """발송 직전 속도 제어 (v4.3 — 스무스 페이서). defer 필요 시 (seconds, reason), 발송 가능이면 None.

    게이트 순서:
      1) Action Block 쿨다운 — 계정 전체 정지 (차단 중 재시도 = 차단 연장 방지).
      2) dm_pacer — 계정×버킷 지터 슬롯 직렬화 (사설답장 3~7s / Send API 1~3s).
         슬롯 미도래면 그 시각까지 defer → requeue 워커가 슬롯 시각에 맞춰 재투입.
      3) rate_governor 시간당 백스톱(740) — 페이서 정상이면 절대 안 걸리는 최후 방어선.
    v4.3 에서 제거: 캠페인 200/hr(can_send_more) — 페이서가 계정 단위로 대체(필드 deprecated).
    드랍/실패가 아니라 항상 '지연'이다.
    """
    # ★ P4: Action Block 쿨다운 중이면 그 계정 모든 발송을 defer (Meta 로 보내지 않음 → 차단 연장 방지).
    from .rate_governor import action_block_cooldown_remaining

    ab_remaining = action_block_cooldown_remaining(str(ig_conn.external_account_id))
    if ab_remaining > 0:
        return (ab_remaining, "action_block_cooldown")

    if getattr(settings, "DM_PACER_ENABLED", True):
        from . import dm_pacer

        gate = dm_pacer.pacer_gate(str(ig_conn.external_account_id), log)
        if gate is not None:
            wait, bucket = gate
            return (wait, f"paced:{bucket}")

    if getattr(settings, "DM_GOVERNOR_ENABLED", True):
        from .rate_governor import check as _rate_check

        decision = _rate_check(
            ig_account_id=str(ig_conn.external_account_id),
            plan=_resolve_plan_name(campaign),
        )
        if not decision.allowed:
            # retry_after = 다음 시간 윈도우까지 초. 최소 30초 보장.
            return (max(int(decision.retry_after), 30), f"rate_governed:{decision.reason}")

    return None


# 토큰 라이브 확인 캐시 TTL(초): 진짜 토큰 사망 시 실패 배치가 /me 를 폭주시키지 않도록.
_TOKEN_DEAD_CHECK_TTL = 300


def _ig_token_confirmed_dead(ig_conn) -> bool:
    """라이브 GET /me 로 IG 토큰이 '확실히' 죽었는지 확인 (verify-before-brick).

    단발 수신자/권한 오류(예: code 200/2534066)나 일시적 190 으로 분류가 흔들려도,
    실제 토큰이 살아있으면 연결 전체를 error 로 브릭하지 않기 위한 최종 방어선.

    판정:
      - /me 2xx                      → 살아있음 → False (브릭 금지)
      - /me 4xx + OAuth 에러코드      → 진짜 사망 → True (mark_as_error 허용)
        (190 만료/회수, 102 세션, 104/2500 토큰 필요)
      - 네트워크/타임아웃/5xx/애매     → False (fail-safe: 브릭 금지)

    연결당 5분 1회만 확인(Redis 캐시). Mock 모드/자격증명 미설정이면 확인 불가 →
    보수적으로 True(=기존 동작 유지, 진짜 토큰오류일 때 브릭)로 폴백.
    """
    from django.core.cache import cache

    ck = f"ig_token_dead:{ig_conn.id}"
    cached = cache.get(ck)
    if cached is not None:
        return cached

    # 판정 자체는 InstagramOAuthService.verify_token 이 단일 소스.
    # 여기서는 캐시(5분 1회) + fail-safe(valid is False 일 때만 dead) 만 얹는다.
    # valid None(네트워크/애매)/True → dead=False (애매하면 브릭하지 않는다, 가용성 우선).
    dead = InstagramOAuthService.verify_token(ig_conn.access_token)["valid"] is False

    cache.set(ck, dead, _TOKEN_DEAD_CHECK_TTL)
    return dead


def _defer_or_fail(log, campaign, ig_conn, exc) -> dict:
    """발송 예외를 분류해 defer(재시도 대기) 또는 종결 처리하고 결과 dict 반환.

    - retriable(rate-limit/transient): **횟수 상한으로 죽이지 않고** QUEUED+next_retry_at 로 defer.
      → requeue_deferred_dms 워커가 재투입. 메시징 윈도우 만료(graceful 종결)는
        send_dm_task 진입부의 age 가드가 단일 지점에서 처리한다.
    - non-retriable(token/param/window/551·기타4xx): 즉시 종결.
      no_trace(551·기타4xx)는 '실패'가 아니라 '미확인' → increment_unconfirmed.
    parent_log 가 있는 child(reward/retry)는 통계 카운트에서 제외(opening/standalone 만).
    """
    cls = exception_to_classification(exc)
    log.retry_count = (log.retry_count or 0) + 1

    # ★ P4: Action Block(code 368 등) 감지 → 그 계정 발송을 에스컬레이팅 쿨다운으로 일시정지.
    # 368 은 retriable 로 분류돼 아래에서 defer 되지만, 추가로 계정 쿨다운을 걸어 _rate_defer 가
    # 그 계정의 모든 발송을 쿨다운 만료까지 Meta 로 보내지 않게 한다(차단 연장 방지·자동 재개).
    if getattr(exc, "code", None) in _ACTION_BLOCK_CODES:
        from .rate_governor import trip_action_block

        cooldown = trip_action_block(str(ig_conn.external_account_id))
        if cooldown > 0:  # 새 트립일 때만(중복 트립 무시) 로그·알림
            log.append_verification_log(
                {
                    "path": "dm_send",
                    "result": "action_block",
                    "code": exc.code,
                    "cooldown_s": cooldown,
                }
            )
            try:
                from apps.core.telegram import send_telegram_notification

                hrs = round(cooldown / 3600, 1)
                send_telegram_notification(
                    f"🚫 *DM Action Block 감지* — @{ig_conn.username} "
                    f"(code={exc.code})\n발송 {hrs}h 쿨다운 후 자동 재개. campaign=`{campaign.id}`"
                )
            except Exception:  # noqa: BLE001
                logger.exception("action_block telegram 알림 실패 (non-fatal)")

    if cls.retriable and log.retry_count < settings.DM_MAX_TRANSIENT_RETRIES:
        exp = min(log.retry_count, 10)
        backoff = min(60 * (2**exp), 3600)  # 상한 1h (쿼터는 시각경계로 풀림)
        log.next_retry_at = timezone.now() + timedelta(seconds=backoff)
        log.status = SentDMLog.Status.QUEUED
        log.save(update_fields=["retry_count", "next_retry_at", "status"])
        return {"status": "deferred", "reason": cls.reason, "retry_count": log.retry_count}

    # ★ 재시도 상한 소진(v3.4): 일시(transient)로 분류됐지만 상한을 넘도록 계속 실패 =
    # 사실상 영구(예: Meta code 1 로 오는 "댓글에 이미 답글 있음" 같은 조건)로 보고 종결한다.
    # 무한 defer 루프 + 백로그 경고 스팸을 막는 일반 안전망 — 진짜 일시 오류는 상한 훨씬 전에
    # 성공하므로 무영향, user_id 24h 윈도우와도 자연 정렬. FAILED_NO_TRACE(비-revivable)로 종결.
    if cls.retriable:
        logger.warning(
            "DM transient retries exhausted → terminate: log=%s retry=%s last=%s",
            log.id,
            log.retry_count,
            cls.reason,
        )
        cls = ErrorClassification(
            log_status=SentDMLog.Status.FAILED_NO_TRACE,
            retriable=False,
            reason=f"transient retries exhausted ({log.retry_count}); permanent-presumed ({cls.reason})",
        )

    # non-retriable → 종결
    # ★ verify-before-brick (v3.3): 단발 수신자/권한 오류로 연결 전체를 죽이지 않는다.
    # 토큰 오류(FAILED_TOKEN)로 분류됐더라도 라이브 /me 로 토큰이 실제 살아있으면
    # FAILED_NO_TRACE 로 강등하고 mark_as_error(연결 브릭)를 건너뛴다.
    # (과거: code=200 한 건이 연결을 error 로 브릭 → 이후 전 DM 이 pre-send 에서 정지)
    if cls.log_status == SentDMLog.Status.FAILED_TOKEN and not _ig_token_confirmed_dead(ig_conn):
        logger.warning(
            "DM token-error but token alive → downgrade to no_trace (no brick): "
            "conn=%s log=%s code=%s sub=%s",
            ig_conn.id,
            log.id,
            getattr(exc, "code", None),
            getattr(exc, "subcode", None),
        )
        cls = ErrorClassification(
            log_status=SentDMLog.Status.FAILED_NO_TRACE,
            retriable=False,
            reason=f"token still valid; per-recipient/permission error ({cls.reason})",
        )

    log.mark_failed(
        status=cls.log_status,
        error_message=str(exc),
        error_code=str(exc.code) if exc.code is not None else "",
        error_subcode=str(exc.subcode) if exc.subcode is not None else "",
        api_response=exc.api_response,
    )
    if log.parent_log_id is None:
        if cls.log_status == SentDMLog.Status.FAILED_NO_TRACE:
            campaign.increment_unconfirmed()
        else:
            campaign.increment_failed()

    if cls.log_status == SentDMLog.Status.FAILED_TOKEN:
        ig_conn.mark_as_error(f"DM 발송 중 토큰/세션 오류(라이브 확인됨): {exc}")

    return {"status": cls.log_status, "reason": cls.reason}


def _maybe_enter_recovery(log, campaign, exc) -> bool:
    """opening 비공개답글이 비팔로워 채널 미개설(code=100/subcode=2534025)로 **확정** 실패했고
    캠페인이 복구를 켰으면, 완전 실패로 종결하지 않고 RECOVERY_PENDING 로 전이하고
    안내 대댓글("DM이 숨겨진 요청/스팸함으로 갔어요 — 수락 후 다시 댓글")을 예약한다.

    v2(2026-07-14): 인바운드 DM 감지 폐기 — 사용자가 요청을 수락하고 **다시 댓글을 달면**
    일반 댓글→DM 경로가 재발송하고, 성공(ACCEPTED) 시 _flip_recovery_on_success 가 이전
    RECOVERY_PENDING 을 RECOVERY_DELIVERED 로 승격한다.

    반환 True → 복구 경로로 처리됨(호출부는 _defer_or_fail 을 타지 않음).
    조건 불충족(다른 subcode / reward·재안내 child / Story / 복구 비활성)이면
    False → 기존대로 FAILED_PARAM 종결(회귀 없음).

    '확정 실패'에만 댓글을 남기기 위한 가드 (prod 실측 2026-07-14 — 이중 댓글 버그):
      - 이 로그에 전달 흔적(accepted/echo/delivered/read)이 있으면 복구를 타지 않는다.
        revive(제자리 되살림) 재시도가 2534025 를 맞아도 원 DM 은 이미 전달됐을 수 있다.
      - 이 댓글에 이미 우리 답글(성공 공개답글 or 복구 답글)이 게시됐으면 추가 게시 금지.
      - 같은 (캠페인, 수신자)에 RECOVERY_PENDING 이 이미 있으면(=안내 댓글이 이미 나감)
        상태만 RECOVERY_PENDING 으로 두고 **댓글은 다시 달지 않는다** (재댓글이 또 실패한
        경우 — 수락 전에 다시 댓글만 단 사용자에게 같은 안내를 반복 게시하면 스팸).

    카운팅: 여기서는 total_failed 를 올리지 않는다(아직 '완전 실패' 아님).
    재댓글 발송 성공 시 flip(신규 로그가 increment_sent), TTL 만료 시 increment_failed 로 정산.
    """
    if not campaign.recovery_reply_enabled:
        return False
    # 프로 전용 런타임 게이트 (fail-closed). 미보유 플랜/조회 실패면 복구를 타지 않고
    # 기존대로 FAILED_PARAM 로 종결 → 회귀 없음. (spam_filter 게이트와 동일 정책)
    from apps.billing.subscription_utils import owner_has_feature

    try:
        workspace = campaign.ig_connection.workspace
    except Exception:  # noqa: BLE001 - 워크스페이스 조회 실패는 미보유 취급이 안전
        return False
    if not owner_has_feature(workspace, "dm_recovery"):
        return False
    # 정확히 2534025(비팔로워/채널 미개설)만. 삭제(33)/7일초과(2018292)/기타 code=100 은 제외.
    if getattr(exc, "code", None) != 100 or str(getattr(exc, "subcode", "") or "") != "2534025":
        return False
    # opening 첫 비공개답글만 (reward/재안내 child·Story·standalone 제외).
    if log.dm_kind != SentDMLog.DMKind.OPENING:
        return False
    if not log.comment_id or log.parent_log_id is not None:
        return False
    # ★ 확정 실패 가드: 전달 흔적이 있으면 '실패 확정' 이 아니다 (revive 재시도 경로 등).
    #   이미 전달됐을 수 있는 DM 에 "못 드렸어요" 계열 댓글을 달면 사용자 혼란 + 이중 댓글.
    if log.meta_message_id or log.echo_mid or log.accepted_at or log.delivered_at or log.read_at:
        return False
    # ★ 이 댓글에 이미 우리 답글이 달려 있으면(성공 공개답글/복구 답글) 추가 게시 금지.
    if log.public_reply_id or log.recovery_reply_id:
        return False

    # 같은 (캠페인, 수신자)에 이미 복구 대기가 있으면 안내 댓글은 1회로 충분 — 상태만 전이.
    # 매칭은 _recipient_match_q (웹훅 IGSID / 폴링 username 키 이원화 대응).
    _guided_q = _recipient_match_q(user_id=log.recipient_user_id, username=log.recipient_username)
    already_guided = _guided_q is not None and (
        SentDMLog.objects.filter(
            _guided_q,
            campaign=campaign,
            status=SentDMLog.Status.RECOVERY_PENDING,
        )
        .exclude(id=log.id)
        .exists()
    )

    log.status = SentDMLog.Status.RECOVERY_PENDING
    log.recovery_pending_at = timezone.now()
    log.error_code = str(getattr(exc, "code", "") or "")
    log.error_subcode = str(getattr(exc, "subcode", "") or "")
    log.error_message = str(exc)
    log.api_response = getattr(exc, "api_response", {}) or {}
    log.save(
        update_fields=[
            "status",
            "recovery_pending_at",
            "error_code",
            "error_subcode",
            "error_message",
            "api_response",
        ]
    )
    log.append_verification_log(
        {
            "path": "recovery",
            "result": "pending",
            "reason": "comment_not_eligible_2534025",
            "guide_reply": "skipped_duplicate" if already_guided else "scheduled",
        }
    )
    if not already_guided:
        # 안내 대댓글 게시 (best-effort, 성공답글과 페이싱/배치 공유, 서킷은 분리)
        import random as _r

        post_public_reply.apply_async(
            args=[str(log.id)], kwargs={"recovery": True}, countdown=_r.randint(5, 15)
        )
    return True


def _recipient_match_q(*, user_id: str = "", username: str = ""):
    """같은 수신자를 recipient 키 이원화를 넘어 매칭하는 Q.

    recipient_user_id 의 값 공간이 경로마다 다르다: 웹훅 경로 = IGSID, 폴링 경로 =
    username(comments edge 가 from.id 를 안 줄 때 폴백 — 2026-07-14 prod 실측).
    IGSID/username 어느 쪽 키로 저장됐든 매칭되도록 recipient_user_id 는 두 값 모두로,
    recipient_username 은 username 으로 본다(전부 인덱스 있는 필드).
    매칭 불가(둘 다 빈 값)면 None.
    """
    keys = [v for v in (str(user_id or "").strip(), str(username or "").strip()) if v]
    if not keys:
        return None
    q = Q(recipient_user_id__in=keys)
    uname = str(username or "").strip()
    if uname:
        q |= Q(recipient_username__iexact=uname)
    return q


def _flip_recovery_on_success(log, campaign) -> int:
    """어떤 발송이든 같은 (캠페인, 수신자)로 ACCEPTED 되면, 그 수신자의 이전
    RECOVERY_PENDING opening 들을 RECOVERY_DELIVERED(성공·종결)로 승격한다.

    v2 재댓글 복구의 성공 정산 지점: 사용자가 숨김함 요청을 수락하고 다시 댓글을 달아
    새 opening 이 성공하면, 실패로 대기 중이던 이전 건이 '복구 성공'으로 종결된다.
    (v1 인바운드 재전송 자식이 배포 전환기에 늦게 ACCEPTED 되는 경우도 recipient 가 같아
    이 경로가 자연 흡수한다.) 매칭은 _recipient_match_q — 웹훅(IGSID)/폴링(username)
    어느 경로로 만들어진 pending 이든 승격된다.

    카운팅: 여기서는 increment_sent 를 올리지 않는다 — 성공 집계는 방금 ACCEPTED 된
    신규 로그(top-level이면 호출부에서 이미 increment_sent)가 담당하고, 승격은 상태
    정산일 뿐이다(월 quota 는 (캠페인×수신자) 고유쌍 집계라 이중 소진 없음).
    반환값 = 승격한 로그 수.
    """
    match_q = _recipient_match_q(user_id=log.recipient_user_id, username=log.recipient_username)
    if match_q is None:
        return 0
    pendings = list(
        SentDMLog.objects.filter(
            match_q,
            campaign=campaign,
            status=SentDMLog.Status.RECOVERY_PENDING,
        ).exclude(id=log.id)
    )
    for parent in pendings:
        parent.status = SentDMLog.Status.RECOVERY_DELIVERED
        parent.save(update_fields=["status"])
        parent.append_verification_log(
            {"path": "recovery", "result": "recovered_by_new_send", "new_log_id": str(log.id)}
        )
    return len(pendings)


# ===== 진입점 =====


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
)
def process_comment_and_send_dm(self, webhook_payload: dict):
    """
    댓글 웹훅 이벤트 처리 (DM 캠페인 전용).

    매칭되는 활성 캠페인마다 DM 발송 큐에 enqueue 한다.
    스팸 검사는 이 태스크와 **독립**된 ``run_spam_filter_check`` 가 담당한다(웹훅에서 병렬 디스패치).
    """
    try:
        logger.debug(f"Processing comment webhook: {webhook_payload}")

        field = webhook_payload.get("field")
        value = (
            webhook_payload.get("value") or {}
        )  # "value": null 방어 (get 기본값은 명시 null 미보정)

        if field != "comments":
            return {"status": "skipped", "reason": f"Unsupported field: {field}"}

        comment_id = value.get("id")
        # "text": null 방어 — TextField(null=False) 라 None 이 create_idempotent 까지 가면
        # NOT NULL IntegrityError 로 정상 매칭 댓글의 DM 이 유실된다(미디어/스티커 전용 댓글 등).
        comment_text = str(value.get("text") or "")
        parent_id = value.get("parent_id")  # 대댓글이면 부모 comment_id, top-level이면 빈 값
        from_user = value.get("from") or {}  # 키가 null 로 오는 payload 방어
        from_user_id = str(from_user.get("id") or "")
        from_username = str(from_user.get("username") or "")
        media = value.get("media") or {}
        media_id = str(media.get("id") or "")
        page_ig_user_id = str(webhook_payload.get("entry_id") or "")

        # 필수: comment_id + entry_id + 신원키(id/username 중 하나).
        # from.username / media.id 결측은 허용 — 복구 재댓글 라우팅은 media 없이도,
        # 신원키 한쪽만으로도 동작한다(_recipient_match_q 이원화). 예전 all([...]) 게이트는
        # 이런 payload 를 복구 라우팅 도달 전에 탈락시켜 재댓글을 유실시켰다.
        # entry_id 필수는 테넌트 가드 — 없으면 _active_campaigns_for_account("") 가
        # 전 계정을 스캔한다.
        if not comment_id or not page_ig_user_id or not (from_user_id or from_username):
            logger.error(f"Missing required fields in webhook payload: {webhook_payload}")
            return {"status": "error", "reason": "Missing required fields"}

        # ★ Self-comment 가드:
        # 비즈니스 본인이 자기 게시물에 댓글 → 자기 자신에게 DM 가는 루프 차단.
        # webhook entry.id 는 connected page 의 IG user id 와 동일.
        # (대댓글 가드보다 먼저 — 우리 공개/복구 답글이 어떤 분기로도 새지 않게.)
        if from_user_id and from_user_id == page_ig_user_id:
            logger.info(
                f"Skipping self-comment DM: page={page_ig_user_id} "
                f"commented on own post (comment_id={comment_id})"
            )
            return {"status": "skipped", "reason": "self_comment"}

        # ★ 대댓글(답글) 가드:
        # 우리 시스템이 게시한 공개 답글이 다시 webhook 으로 들어오면 → DM 무한 루프.
        # 외부 사용자의 답글 역시 캠페인 트리거 대상이 아님 (top-level 댓글만 트리거).
        # 예외: RECOVERY_PENDING 보유 사용자의 답글은 복구 재댓글로 라우팅 —
        #   복구 안내가 사용자 댓글의 '답글'로 달리므로, 사용자의 가장 자연스러운 응답
        #   (스레드 답글)이 여기로 들어온다. 그 외 답글은 기존대로 무조건 skip.
        if parent_id:
            routed = _maybe_route_recovery_recomment(
                page_ig_user_id=page_ig_user_id,
                from_user_id=from_user_id,
                from_username=from_username,
                comment_id=comment_id,
                comment_text=comment_text,
                media_id=media_id,
                source="webhook_reply",
            )
            if routed:
                return {"status": "queued", "reason": "recovery_recomment_reply", "routed": routed}
            logger.info(f"Skipping reply (대댓글): comment_id={comment_id} parent={parent_id}")
            return {"status": "skipped", "reason": "is_reply"}

        # ★ media 결측 payload: 일반 매칭은 돌릴 수 없다 — matches_media 가 ANY_MEDIA 에서
        #   매체 불문 True 라 오발송 위험. SeenComment/next_media attach 도 media 가 필요.
        #   복구 재댓글 라우팅만 시도한다 (media 미상이므로 SPECIFIC_MEDIA pending 은
        #   matches_media 스코핑이 fail-closed, ANY_MEDIA 만 라우팅 가능).
        if not media_id:
            routed = _maybe_route_recovery_recomment(
                page_ig_user_id=page_ig_user_id,
                from_user_id=from_user_id,
                from_username=from_username,
                comment_id=comment_id,
                comment_text=comment_text,
                media_id="",
                source="webhook",
            )
            if routed:
                return {"status": "queued", "reason": "recovery_recomment", "routed": routed}
            return {"status": "skipped", "reason": "no_media_id"}

        # 활성 캠페인 매칭 (trigger_type + keyword 모두 평가)
        # (스팸 검사는 run_spam_filter_check 가 독립적으로 처리 — 여기서 하지 않는다)
        # webhook 의 entry.id 는 IG user id — 그 계정의 캠페인만 후보
        candidate_qs = _active_campaigns_for_account(page_ig_user_id)

        # trigger_type 평가 + 누락 보정 장부(SeenComment) 기록 대상 수집:
        #   - matched_campaigns: 매체+키워드 매칭 → DM enqueue 대상
        #   - seen_conn_ids: 이 media 를 폴링하게 될 specific_media(=attach된 next_media 포함)
        #     캠페인의 connection. keyword 매칭과 무관 — 폴링 앵커가 "모든 댓글"을 알아야 하므로.
        matched_campaigns = []
        seen_conn_ids = set()
        for c in candidate_qs:
            if (
                c.trigger_type == AutoDMCampaign.TriggerType.SPECIFIC_MEDIA
                and c.media_id == media_id
            ):
                seen_conn_ids.add(c.ig_connection_id)
            if c.matches_media(media_id) and c.matches_keyword(comment_text):
                matched_campaigns.append(c)

        # ★ 누락 보정 장부 기록: 웹훅 payload(value.id / value.media.id)만으로 생성 — 별도 Meta API 불필요.
        # 폴링이 나중에 이 댓글을 "이미 봤다"(앵커)고 인식하게 한다. 기록 실패는 DM 을 막지 않는다.
        for conn_id in seen_conn_ids:
            try:
                _record_seen_comment(
                    ig_connection_id=conn_id,
                    comment_id=comment_id,
                    media_id=media_id,
                    source=SeenComment.Source.WEBHOOK,
                    triggered=bool(matched_campaigns),
                )
            except Exception:
                logger.exception(
                    "SeenComment 기록 실패: comment_id=%s conn=%s", comment_id, conn_id
                )

        # ★ v3.6 — next_media webhook-based attach
        # 매칭된 캠페인이 없거나 next_media (media_id="") 캠페인이 있으면
        # 이 webhook 의 media.id 가 baseline 이후의 "새 게시물"인지 검증 후 attach.
        unattached_next = list(
            candidate_qs.filter(
                trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
                media_id="",
            )
        )
        if unattached_next:
            attached_now = _maybe_attach_next_media_from_webhook(
                unattached_campaigns=unattached_next,
                webhook_media_id=media_id,
                comment_text=comment_text,
            )
            if attached_now:
                # attach 된 캠페인을 매칭 목록에 추가 (중복 방지)
                existing_ids = {c.id for c in matched_campaigns}
                for c in attached_now:
                    if c.id not in existing_ids and c.matches_keyword(comment_text):
                        matched_campaigns.append(c)

        # ★ 복구 재댓글 라우팅(키워드 비적용): RECOVERY_PENDING 보유 사용자의 top-level
        #   재댓글이 캠페인 키워드와 불일치해도("수락했어요" 등) 복구 재발송은 진행돼야 한다.
        #   일반 매칭과 같은 캠페인이 겹치면 comment_id 단위 멱등키가 중복을 흡수한다.
        recovery_routed = _maybe_route_recovery_recomment(
            page_ig_user_id=page_ig_user_id,
            from_user_id=from_user_id,
            from_username=from_username,
            comment_id=comment_id,
            comment_text=comment_text,
            media_id=media_id,
            source="webhook",
        )

        if not matched_campaigns:
            if recovery_routed:
                return {
                    "status": "queued",
                    "reason": "recovery_recomment",
                    "routed": recovery_routed,
                }
            return {"status": "skipped", "reason": "No campaign matched (media/keyword)"}

        results = []
        for campaign in matched_campaigns:
            results.append(
                _enqueue_send_dm(
                    campaign=campaign,
                    comment_id=comment_id,
                    comment_text=comment_text,
                    # from.id 결측 payload 는 username 폴백 (recipient_user_id 가 NOT NULL
                    # CharField — 폴링 경로의 recipient_key 폴백과 동일 관례)
                    from_user_id=from_user_id or from_username,
                    from_username=from_username,
                    webhook_payload=webhook_payload,
                )
            )

        return {"status": "queued", "results": results, "recovery_routed": recovery_routed}

    except Exception as e:
        logger.exception(f"Error processing comment webhook: {e}")
        raise


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
)
def run_spam_filter_check(self, webhook_payload: dict):
    """댓글 웹훅 → 계정 전체 스팸 필터 (DM 캠페인과 **독립**).

    ``process_comment_and_send_dm`` 과 별개 태스크로 병렬 실행된다. 캠페인 유무와 무관하게
    entry_id(=IG 계정)로 ``SpamFilterConfig`` 를 **직접** 찾아, 활성+프로면 하이브리드(규칙+gemma)
    로 판정한다. auto_hide 켜져 있으면 Meta API 로 숨기고, 아니면 감지만 기록(수동 숨김 대기).

    멱등: ``UNIQUE(spam_filter, comment_id)`` 에 get_or_create 로 경합 → 최초 1회만 분류/숨김.
    잠정 상태는 CLEAN(통계 제외·안전) — 스팸 확정 시에만 DETECTED/HIDDEN 으로 승격한다.
    LLM 판정은 fail-open(불확실하면 숨기지 않음)이라 워커 크래시가 없으면 항상 종결 상태에 도달.
    """
    try:
        field = webhook_payload.get("field")
        value = webhook_payload.get("value", {})
        if field != "comments":
            return {"status": "skipped", "reason": f"unsupported_field:{field}"}

        comment_id = value.get("id")
        comment_text = value.get("text", "") or ""
        from_user = value.get("from", {})
        from_user_id = from_user.get("id")
        from_username = from_user.get("username")
        media = value.get("media", {})
        media_id = media.get("id")
        entry_id = str(webhook_payload.get("entry_id") or "")

        if not all([comment_id, from_user_id, entry_id]):
            return {"status": "error", "reason": "missing_fields"}

        # ★ self 가드: 본인(비즈니스) 댓글·본인이 게시한 공개 답글은 검사 안 함.
        # webhook entry.id == 연결된 IG 계정 user id. 우리 답글도 이 계정에서 나오므로 함께 걸러진다.
        # (parent_id 가 있어도 self 가 아니면 외부 답글 → 검사 진행. 결정 3)
        if str(from_user_id) == entry_id:
            return {"status": "skipped", "reason": "self_comment"}

        # ★ 계정 직접 조회 — 캠페인 우회(결합 해제 핵심). 캠페인 없는 게시물의 댓글도 검사된다.
        # 같은 IG 계정(external_account_id)이 여러 워크스페이스에 연결될 수 있으므로(테스트/공유)
        # 단일 .first() 가 아니라 **활성 연결 전부**를 훑어, 활성+프로 필터가 걸린 연결마다 적용한다.
        # (DM 경로의 _active_campaigns_for_account 도 같은 계정의 모든 연결을 훑는 것과 일관)
        conns = list(
            IGAccountConnection.objects.filter(
                external_account_id=entry_id,
                status=IGAccountConnection.Status.ACTIVE,
                is_active=True,  # 소프트 비활성 계정은 스팸필터 대상 제외
            ).select_related("workspace")
        )
        if not conns:
            return {"status": "skipped", "reason": "no_active_connection"}

        from apps.billing.subscription_utils import owner_has_feature

        results = []
        had_active_filter = False
        plan_blocked = False
        for conn in conns:
            spam_filter = getattr(conn, "spam_filter", None)
            if spam_filter is None or not spam_filter.is_active():
                continue
            had_active_filter = True
            # 플랜 런타임 게이트 (fail-closed) — 스팸필터는 프로 전용.
            if not owner_has_feature(conn.workspace, "spam_filter"):
                plan_blocked = True
                continue
            try:
                results.append(
                    _run_spam_for_connection(
                        conn,
                        spam_filter,
                        comment_id=comment_id,
                        comment_text=comment_text,
                        from_user_id=from_user_id,
                        from_username=from_username,
                        media_id=media_id,
                        webhook_payload=webhook_payload,
                    )
                )
            except Exception:
                # 한 연결의 처리 실패가 다른 연결을 막지 않게 격리(멱등이라 재시도 안전).
                logger.exception("spam 처리 실패: conn=%s comment=%s", conn.id, comment_id)

        if not results:
            # 활성 필터는 있었으나 전부 플랜 게이트로 막힌 경우와, 애초에 활성 필터가 없던 경우 구분.
            if had_active_filter and plan_blocked:
                return {"status": "skipped", "reason": "plan_not_allowed"}
            return {"status": "skipped", "reason": "filter_inactive"}
        return {"status": "processed", "results": results}

    except Exception as e:
        logger.exception("run_spam_filter_check error: %s", e)
        raise


def _comment_triggers_active_campaign(conn, *, media_id: str, comment_text: str) -> bool:
    """이 댓글이 계정의 **활성 auto-DM 캠페인을 실제로 발동**시키는지(media+keyword 매칭).

    발동 조건은 발송 경로(``_process_comment_and_send_dm``)와 동일하게
    ``matches_media(media_id) and matches_keyword(comment_text)`` 로 맞춘다.

    이런 댓글은 사용자가 **원해서 유치한** 트리거 댓글이므로 스팸 분류에서 제외해야 한다.
    (트리거 키워드 "가이드🔥"·"비밀코드"·"풀버전"·"스킬"·"클로드(ㅋㄹㄷ)" 등이 gemma 에
     promo/adult/scam 으로 오분류돼, DM 을 정상 받은 팬 댓글이 스팸으로 감지되던 회귀 방지 —
     2026-07-21 3dragon_pd: detected 36건 중 최소 10건이 실제 DM 발송(read/delivered)된 댓글.)
    """
    from .models import AutoDMCampaign

    campaigns = AutoDMCampaign.objects.filter(
        ig_connection=conn, status=AutoDMCampaign.Status.ACTIVE
    )
    for campaign in campaigns:
        if campaign.matches_media(media_id) and campaign.matches_keyword(comment_text):
            return True
    return False


def _run_spam_for_connection(
    conn,
    spam_filter,
    *,
    comment_id: str,
    comment_text: str,
    from_user_id: str,
    from_username: str,
    media_id: str,
    webhook_payload: dict,
) -> dict:
    """단일 (연결, 스팸필터)에 대해 멱등 claim → 하이브리드 판정 → auto_hide 처리.

    ``run_spam_filter_check`` 가 계정의 활성 필터 연결마다 호출한다.
    """
    from .spam_classifier import classify_comment

    # ── 멱등 claim: (spam_filter, comment_id) UNIQUE 에 경합 → 최초 1회만 진행 ──
    # 잠정 상태 CLEAN: 크래시로 중단돼도 오탐 감지로 집계되지 않는다(안전).
    log, created = SpamCommentLog.objects.get_or_create(
        spam_filter=spam_filter,
        comment_id=comment_id,
        defaults={
            "comment_text": comment_text,
            "commenter_user_id": str(from_user_id),
            "commenter_username": from_username or "",
            "media_id": media_id or "",
            "status": SpamCommentLog.Status.CLEAN,
            "spam_reasons": [],
        },
    )
    if not created:
        # 동일 comment 재도착 → 재분류·재숨김 없이 단락
        return {
            "status": "skipped",
            "reason": "already_processed",
            "conn_id": str(conn.id),
            "spam_log_id": str(log.id),
            "prior_status": log.status,
        }

    # ── ★ 캠페인 트리거 댓글 면제 (규칙/LLM 판정보다 우선) ──
    # 이 댓글이 활성 캠페인을 발동시키면(=사용자가 원한 댓글) 스팸 분류를 건너뛰고 CLEAN 유지.
    if _comment_triggers_active_campaign(conn, media_id=media_id, comment_text=comment_text):
        log.engine = "campaign_trigger_exempt"
        log.save(update_fields=["engine"])
        return {
            "status": "clean",
            "engine": "campaign_trigger_exempt",
            "conn_id": str(conn.id),
            "spam_log_id": str(log.id),
        }

    # ── 하이브리드 판정 (규칙 즉시차단 + 애매하면 gemma, fail-open) ──
    verdict = classify_comment(
        comment_text,
        spam_keywords=spam_filter.spam_keywords,
        block_urls=spam_filter.block_urls,
        use_llm=getattr(spam_filter, "use_llm", True),
    )

    if not verdict.is_spam:
        # 잠정 CLEAN 유지 (TTL 정리 대상) — 판정 메타만 기록
        log.confidence = verdict.confidence
        log.spam_category = verdict.category or ""
        log.engine = verdict.engine
        log.save(update_fields=["confidence", "spam_category", "engine"])
        return {
            "status": "clean",
            "engine": verdict.engine,
            "conn_id": str(conn.id),
            "spam_log_id": str(log.id),
        }

    # ── 스팸 확정 → DETECTED 로 승격 + 원본 페이로드 보존(감사) ──
    log.status = SpamCommentLog.Status.DETECTED
    log.spam_reasons = verdict.reasons
    log.confidence = verdict.confidence
    log.spam_category = verdict.category or ""
    log.engine = verdict.engine
    log.webhook_payload = webhook_payload
    log.save(
        update_fields=[
            "status",
            "spam_reasons",
            "confidence",
            "spam_category",
            "engine",
            "webhook_payload",
        ]
    )
    spam_filter.increment_spam_detected()

    # auto_hide off → 감지만 기록(유저가 수동 숨김)
    if not spam_filter.auto_hide_enabled:
        return {
            "status": "detected",
            "engine": verdict.engine,
            "conn_id": str(conn.id),
            "spam_log_id": str(log.id),
        }

    # mock 토큰 — dev 에서 Meta 미호출(기존 hide_comment 는 mock 분기가 없음)
    if MockInstagramProvider.is_mock_token(conn.access_token):
        log.mark_as_hidden({"mock": True})
        spam_filter.increment_hidden()
        return {"status": "hidden_mock", "conn_id": str(conn.id), "spam_log_id": str(log.id)}

    try:
        api_response = InstagramCommentService.hide_comment(
            comment_id=comment_id, access_token=conn.access_token
        )
        log.mark_as_hidden(api_response)
        spam_filter.increment_hidden()
        return {"status": "hidden", "conn_id": str(conn.id), "spam_log_id": str(log.id)}
    except Exception as hide_error:
        # 숨김 실패는 재분류 없이 FAILED 기록만(재시도 시 already_processed 로 단락).
        # 유저가 모더레이션 API 로 수동 재숨김 가능.
        log.mark_as_failed(scrub_secrets(str(hide_error)))
        return {
            "status": "failed_to_hide",
            "conn_id": str(conn.id),
            "spam_log_id": str(log.id),
            "error": scrub_secrets(str(hide_error)),
        }


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
)
def process_story_reply_and_send_dm(self, payload: dict):
    """
    Story 답장 이벤트 처리 (v3.7).

    Story 답장은 댓글이 아니라 messages webhook 으로 옴. payload 구조:
        {
            "page_ig_user_id": "...",   # 우리 비즈니스 IG ID
            "sender_user_id":  "...",   # 답장 보낸 사용자 IGSID
            "sender_username": "...",   # (선택)
            "story_id":        "...",   # 답장 대상 Story ID
            "message_mid":     "...",   # 메시지 ID (idempotency 용)
            "message_text":    "...",   # 답장 본문
            "entry_time":      <int>,   # webhook timestamp
        }

    매칭 룰: trigger_type=STORY_REPLY 이고 media_id=story_id 인 활성 캠페인.
    keyword_filter 도 동일하게 평가.
    """
    page_ig_user_id = str(payload.get("page_ig_user_id") or "")
    sender_user_id = str(payload.get("sender_user_id") or "")
    sender_username = str(payload.get("sender_username") or "")
    story_id = str(payload.get("story_id") or "")
    message_mid = str(payload.get("message_mid") or "")
    message_text = payload.get("message_text") or ""

    if not (page_ig_user_id and sender_user_id and story_id and message_mid):
        return {"status": "skipped", "reason": "missing required fields"}

    # ★ Self-message 가드: 자기 자신의 메시지 무시
    if sender_user_id == page_ig_user_id:
        return {"status": "skipped", "reason": "self_story_reply"}

    candidate_qs = (
        AutoDMCampaign.objects.filter(
            status=AutoDMCampaign.Status.ACTIVE,
            trigger_type=AutoDMCampaign.TriggerType.STORY_REPLY,
            ig_connection__external_account_id=page_ig_user_id,
            ig_connection__is_active=True,  # 소프트 비활성 계정 제외
        )
        # 예약 발송: 활성 기간(window) 안에 있는 캠페인만 후보
        .filter(AutoDMCampaign.schedule_window_q()).select_related(
            "ig_connection", "ig_connection__workspace"
        )
    )

    matched_campaigns = []
    for c in candidate_qs:
        if c.matches_story(story_id) and c.matches_keyword(message_text):
            matched_campaigns.append(c)

    if not matched_campaigns:
        return {"status": "skipped", "reason": "no story_reply campaign matched"}

    results = []
    for campaign in matched_campaigns:
        results.append(
            _enqueue_send_dm_for_story_reply(
                campaign=campaign,
                story_id=story_id,
                message_mid=message_mid,
                message_text=message_text,
                sender_user_id=sender_user_id,
                sender_username=sender_username,
                payload=payload,
            )
        )

    return {"status": "queued", "results": results}


def _enqueue_send_dm_for_story_reply(
    *,
    campaign: AutoDMCampaign,
    story_id: str,
    message_mid: str,
    message_text: str,
    sender_user_id: str,
    sender_username: str,
    payload: dict,
) -> dict:
    """
    Story 답장 매칭 후 SentDMLog INSERT + send_dm_task 큐 등록.

    idempotency_key = sha256(workspace : ig_user : message_mid : campaign)
        → 같은 답장(같은 mid) 에 대해 동일 캠페인 중복 발송 차단.
    """
    ig_conn = campaign.ig_connection

    # ★ 예약 발송 창 가드 (TOCTOU 안전망)
    if not campaign.is_runnable_now():
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "outside_schedule_window",
        }

    # Self-DM 가드
    if str(sender_user_id) == str(ig_conn.external_account_id):
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "self_story_reply",
        }

    # 수신자 쿨다운 (동일 사용자가 연속 답장하는 케이스 방어; DM_RECIPIENT_COOLDOWN_SECONDS)
    cooldown_s = getattr(settings, "DM_RECIPIENT_COOLDOWN_SECONDS", 300)
    cooldown_cutoff = timezone.now() - timedelta(seconds=cooldown_s)
    recent = SentDMLog.objects.filter(
        campaign=campaign,
        recipient_user_id=sender_user_id,
        created_at__gte=cooldown_cutoff,
    ).exists()
    if recent:
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": f"recipient_cooldown_{cooldown_s}s",
        }

    idempotency_key = InstagramMessagingService.build_idempotency_key(
        workspace_id=ig_conn.workspace_id,
        ig_user_id=ig_conn.external_account_id,
        comment_id=message_mid,  # story 답장은 message_mid 를 trigger ID 로 사용
        campaign_id=campaign.id,
    )

    # 시간당 한도·계정 거버너는 send_dm_task 단일 지점에서 평가(초과 시 드랍 아닌 defer).
    log, created = SentDMLog.create_idempotent(
        idempotency_key=idempotency_key,
        campaign=campaign,
        comment_id="",  # ★ Story 답장은 comment_id 없음 (send_dm_task 가 user_id 분기 판단)
        comment_text=message_text,
        recipient_user_id=sender_user_id,
        recipient_username=sender_username,
        message_sent=campaign.get_opening_message(),
        status=SentDMLog.Status.QUEUED,
        webhook_payload=payload,
        dm_kind=SentDMLog.DMKind.STANDALONE,
        gate_status=SentDMLog.GateStatus.NONE,
    )
    if not created:
        return {
            "campaign_id": str(campaign.id),
            "status": "duplicate",
            "log_id": str(log.id) if log else None,
        }

    send_dm_task.delay(str(log.id))
    return {
        "campaign_id": str(campaign.id),
        "status": "enqueued",
        "log_id": str(log.id),
        "trigger": "story_reply",
    }


@shared_task
def resolve_recipient_usernames_for_campaign(campaign_id: str, igsids: list) -> dict:
    """수신자 목록 열람 시 트리거되는 지연(lazy) username 해석 (best-effort).

    Story 답장 트리거 캠페인은 messaging 웹훅에 username 이 없어 SentDMLog.recipient_username
    이 빈 값으로 저장된다. 프론트가 수신자 목록/상세를 실제로 열람할 때만(verification_views)
    이 태스크를 fire-and-forget 로 enqueue 해, IG User Profile API 로 핸들을 해석하고 빈 로그에
    채운다 — 아무도 안 보면 API 호출 0.

    - IGSID→username 은 InstagramMessagingService.resolve_username 이 캐시(양성 7d/음성 1h).
    - consent 윈도우(~24h) 밖이면 해석 실패 → 빈 값 유지(응답 계층에서 user_{igsid} 폴백 표기).
    - 발송 파이프라인과 완전 독립 — 실패해도 조용히 skip, 아무것도 막지 않는다.
    """
    if not campaign_id or not igsids:
        return {"status": "noop"}

    try:
        campaign = AutoDMCampaign.objects.select_related("ig_connection").get(id=campaign_id)
    except (AutoDMCampaign.DoesNotExist, ValueError, TypeError):
        return {"status": "campaign_not_found"}

    ig_conn = campaign.ig_connection
    if not ig_conn or ig_conn.status != IGAccountConnection.Status.ACTIVE:
        return {"status": "ig_not_active"}

    token = ig_conn.access_token
    resolved = 0
    for igsid in {str(x) for x in igsids if x}:
        username = InstagramMessagingService.resolve_username(igsid, token)
        if not username:
            continue
        # 빈(미해석) 로그에 한해서만 채움 — 이미 실제 핸들이 있으면 건드리지 않는다.
        updated = SentDMLog.objects.filter(
            campaign_id=campaign.id,
            recipient_user_id=igsid,
            recipient_username="",
        ).update(recipient_username=username)
        if updated:
            resolved += 1

    return {"status": "done", "resolved": resolved, "requested": len(igsids)}


def _maybe_attach_next_media_from_webhook(
    *,
    unattached_campaigns: list,
    webhook_media_id: str,
    comment_text: str,
) -> list:
    """
    Webhook 으로 받은 댓글의 media_id 가 "캠페인 생성 이후의 새 게시물"인지
    검증한 뒤, 맞으면 next_media 캠페인들에 즉시 attach (v3.6).

    검증 룰:
        1. media_id 가 last_seen_media_id 와 동일 → baseline 게시물, attach 안 함
        2. GET /v25.0/{media_id}?fields=timestamp 호출로 게시 시각 확보
           - API 실패/무응답 → 안전하게 attach 안 함 (false negative > 잘못된 attach)
        3. baseline(last_seen_media_at) 이 있으면 그보다 새 게시물만 후보
        4. ★ 가장 중요 — 게시 시각이 **각 캠페인 생성 시각 이후**여야 attach.
           "다음 새 게시물"은 캠페인을 만든 뒤 올라온 게시물을 뜻하므로, 생성 전부터
           존재하던 (baseline 보다는 새롭지만) 게시물엔 붙지 않는다. 이 가드는
           ``attach_next_media_single_active(media_published_at=...)`` 가 후보별로 적용한다.
           (baseline 은 specific_media 캠페인으로는 전진하지 않아 며칠씩 뒤처질 수 있어,
            baseline 비교만으로는 이미 존재하던 최신 게시물을 "다음 게시물"로 오인한다.)

    Returns:
        attach 된 AutoDMCampaign 인스턴스 리스트 (refresh 된 상태)
    """
    if not unattached_campaigns or not webhook_media_id:
        return []

    # 모든 unattached 캠페인은 같은 IG 계정 소유 (호출자가 보장)
    ig_conn = unattached_campaigns[0].ig_connection

    # 룰 1: baseline 과 동일 미디어면 skip
    if ig_conn.last_seen_media_id and ig_conn.last_seen_media_id == webhook_media_id:
        return []

    # 룰 2: 이 게시물의 실제 게시 시각 확보 (baseline 비교 + '캠페인 생성 이후' 가드 공용).
    #   실패하면 안전하게 skip — 잘못된 attach 보다 놓치는 편이 낫다.
    try:
        media_ts = InstagramMediaService.get_media_timestamp(
            media_id=webhook_media_id,
            access_token=ig_conn.access_token,
        )
    except Exception as e:
        logger.warning(
            f"next_media webhook attach: timestamp fetch failed for "
            f"media={webhook_media_id}: {e}"
        )
        return []
    if media_ts is None:
        logger.warning(
            f"next_media webhook attach: no timestamp for media={webhook_media_id} "
            "(API returned no data or 404) — skip"
        )
        return []

    # 룰 3: baseline 보다 오래된 게시물이면 skip
    if ig_conn.last_seen_media_at and media_ts <= ig_conn.last_seen_media_at:
        logger.info(
            f"next_media webhook attach: media={webhook_media_id} is older than "
            f"baseline ({media_ts.isoformat()} <= "
            f"{ig_conn.last_seen_media_at.isoformat()}) — skip"
        )
        return []
    new_media_at = media_ts

    # ★ Attach: '한 게시물 = 활성 캠페인 1개' 불변식 + '진짜 다음 게시물' 가드 유지.
    #   media_published_at 로 후보 중 생성 시각이 이 게시물보다 이른(=이 게시물이 생성 이후) 것만
    #   attach 되고, 나머지는 대기 유지. (예전엔 baseline 만 비교해 이미 존재하던 최신 게시물에
    #   중복 attach 됐고 댓글당 opening DM 이 중복 발송돼 Meta code 1 무한 재시도로 이어졌다.)
    result = AutoDMCampaign.attach_next_media_single_active(
        ig_connection_id=ig_conn.id,
        candidate_ids=[c.id for c in unattached_campaigns],
        media_id=webhook_media_id,
        media_published_at=media_ts,
    )

    # baseline 갱신 (관측된 최신 게시물로 전진 — attach 여부와 무관)
    ig_conn.last_seen_media_id = webhook_media_id
    ig_conn.last_seen_media_at = new_media_at
    ig_conn.save(update_fields=["last_seen_media_id", "last_seen_media_at"])

    if result["attached"] or result["paused"]:
        logger.info(
            "next_media webhook attach: ig_conn=%s media=%s attached=%s paused_dup=%s",
            ig_conn.id,
            webhook_media_id,
            result["attached"],
            result["paused"],
        )

    # refresh 후 반환 (호출자가 즉시 매칭 사용) — attach 된 1개만
    return list(
        AutoDMCampaign.objects.filter(
            id__in=result["attached"],
        ).select_related("ig_connection")
    )


def _enqueue_send_dm(
    *,
    campaign: AutoDMCampaign,
    comment_id: str,
    comment_text: str,
    from_user_id: str,
    from_username: str,
    webhook_payload: dict,
) -> dict:
    """SentDMLog 행을 멱등하게 INSERT하고 send_dm_task 큐 등록.

    캠페인 설정에 따라:
      - opening DM 본문 = campaign.get_opening_message() (follow_gate 안내 자동 첨부)
      - dm_kind = OPENING(gate 사용 시) / STANDALONE
      - gate_status = PENDING(gate 사용 시) / NONE
    """
    ig_conn = campaign.ig_connection

    # ★ 예약 발송 창 가드 (TOCTOU 안전망): 후보 선정 이후 종료 시각이 지났을 수 있으므로
    # 큐 적재 직전에 한 번 더 확인. 창 밖이면 로그도 남기지 않고 조용히 skip.
    if not campaign.is_runnable_now():
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "outside_schedule_window",
        }

    # ★ Self-DM 가드 (이중 안전망):
    # 캠페인 owner = 댓글 작성자면 skip. _process_comment_and_send_dm 에서
    # 1차 차단되지만, 다른 진입점에서도 안전하게.
    # ⚠️ poll_missed_comments 는 comments edge 가 from.id 를 안 줘서 from_user_id 에
    # **username** 이 들어온다 → IGSID 비교만으로는 못 거른다(2026-07-14 prod 실측:
    # 자기 공개답글에 셀프 DM 50건). username 도 함께 비교한다.
    own_username = (getattr(ig_conn, "username", "") or "").strip().lower()
    if str(from_user_id) == str(ig_conn.external_account_id) or (
        own_username
        and own_username
        in {str(from_user_id or "").strip().lower(), str(from_username or "").strip().lower()}
    ):
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "self_comment",
        }

    # ★ 동일 수신자 쿨다운(DM_RECIPIENT_COOLDOWN_SECONDS): 같은 사람이 단시간에 여러 댓글 달면
    # idempotency_key 는 comment_id 별로 다르므로 중복 방지 안 됨 → 별도 가드(계정 보호).
    # 예외(좁게): **안내 댓글이 실제 게시된**(recovery_reply_id 있음) RECOVERY_PENDING 만
    # 쿨다운 모수에서 제외 — 복구 안내("수락 후 다시 댓글")를 보고 사용자가 5분 내에 재댓글을
    # 다는 것이 정상 흐름이므로. 미게시 pending(중복 실패의 silent 전이)까지 면제하면 채널이
    # 계속 닫힌 유저의 재댓글마다 실패 시도가 무한 반복된다(페이서 소모·실패통계 증폭) —
    # 게시된 안내는 수신자당 1회뿐이라 면제도 실질 1건으로 캡된다.
    cooldown_s = getattr(settings, "DM_RECIPIENT_COOLDOWN_SECONDS", 300)
    cooldown_cutoff = timezone.now() - timedelta(seconds=cooldown_s)
    # 매칭은 _recipient_match_q — recipient 키 이원화(웹훅 IGSID / 폴링·from 결측 username 폴백)를
    # 넘어야 한다. recipient_user_id 정확일치만 보면 같은 사람의 로그가 IGSID/username 두 키공간에
    # 갈려(예: c1 은 웹훅 IGSID 발송, c2 는 폴링 username 발송) 서로의 최근 발송을 못 봐 쿨다운이
    # 우회된다(_flip_recovery_on_success / _maybe_route_recovery_recomment 와 동일 헬퍼로 통일).
    _cooldown_match_q = _recipient_match_q(user_id=from_user_id, username=from_username)
    recent_to_same_recipient = _cooldown_match_q is not None and (
        SentDMLog.objects.filter(
            _cooldown_match_q,
            campaign=campaign,
            created_at__gte=cooldown_cutoff,
        )
        .exclude(Q(status=SentDMLog.Status.RECOVERY_PENDING) & ~Q(recovery_reply_id=""))
        .exists()
    )
    if recent_to_same_recipient:
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": f"recipient_cooldown_{cooldown_s}s",
        }

    # ★ 한 댓글 = 비공개답글(Private Reply) 1회 (Meta 제약). 다른 캠페인이 같은 댓글에 이미
    #   opening/standalone DM 을 발송 중이거나 성공했으면 중복 발송을 막는다. '한 게시물 = 활성
    #   캠페인 1개' 가드가 우회돼 같은 게시물에 활성 캠페인이 2개로 새는 경우에도, code 1
    #   ("이미 답글 있음") 무한 재시도로 백로그가 부풀지 않게 하는 최종 안전망이다.
    #   실패(failed_*)·skipped·recovery_pending 로그는 슬롯을 점유하지 않으므로(재시도 여지)
    #   점유 상태에서 제외한다.
    if comment_id:
        _slot_occupying = (
            SentDMLog.Status.QUEUED,
            SentDMLog.Status.SUBMITTING,
            SentDMLog.Status.PENDING,
            SentDMLog.Status.SENT,
            SentDMLog.Status.ACCEPTED,
            SentDMLog.Status.DELIVERED,
            SentDMLog.Status.READ,
            SentDMLog.Status.RECOVERY_DELIVERED,
        )
        already_claimed = (
            SentDMLog.objects.filter(comment_id=comment_id, status__in=_slot_occupying)
            .exclude(campaign=campaign)
            .exists()
        )
        if already_claimed:
            return {
                "campaign_id": str(campaign.id),
                "status": "skipped",
                "reason": "duplicate_comment_private_reply",
            }

    idempotency_key = InstagramMessagingService.build_idempotency_key(
        workspace_id=ig_conn.workspace_id,
        ig_user_id=ig_conn.external_account_id,
        comment_id=comment_id,
        campaign_id=campaign.id,
    )

    # 발송할 본문 + 분류 결정
    # v3.8: Follow-gate 재활성 (is_user_follow_business 기반 silent verify).
    # reward_message_template 가 비어 있으면 게이트 의미 없음 → STANDALONE 으로 fallback.
    message_body = campaign.get_opening_message()
    if campaign.follow_gate_enabled and (campaign.reward_message_template or "").strip():
        dm_kind = SentDMLog.DMKind.OPENING
        gate_status = SentDMLog.GateStatus.PENDING
    else:
        dm_kind = SentDMLog.DMKind.STANDALONE
        gate_status = SentDMLog.GateStatus.NONE

    # 시간당 발송 제한·계정 거버너는 send_dm_task 진입 시 단일 지점에서 평가하며,
    # 초과 시 드랍하지 않고 defer(QUEUED+next_retry_at) 한다 (requeue 워커가 순차 재투입).
    # → 여기서는 항상 QUEUED 로 적재하고 발송 판단은 send_dm_task 에 위임.
    log, created = SentDMLog.create_idempotent(
        idempotency_key=idempotency_key,
        campaign=campaign,
        comment_id=comment_id,
        comment_text=comment_text,
        recipient_user_id=from_user_id,
        recipient_username=from_username,
        message_sent=message_body,
        status=SentDMLog.Status.QUEUED,
        webhook_payload=webhook_payload,
        dm_kind=dm_kind,
        gate_status=gate_status,
    )
    if not created:
        return {
            "campaign_id": str(campaign.id),
            "status": "duplicate",
            "log_id": str(log.id) if log else None,
        }

    send_dm_task.delay(str(log.id))
    return {
        "campaign_id": str(campaign.id),
        "status": "enqueued",
        "log_id": str(log.id),
        "dm_kind": dm_kind,
        "gate_status": gate_status,
    }


# ===== 발송 태스크 (상태머신) =====


@shared_task(bind=True, max_retries=5)
def send_dm_task(self, log_id: str):
    """
    단일 SentDMLog 를 SUBMITTING → ACCEPTED 까지 진행.

    멱등성: 이미 ACCEPTED 이상이면 즉시 반환.
    재시도: transient 에러만 백오프 재시도.
    """
    try:
        log = SentDMLog.objects.select_related(
            "campaign",
            "campaign__ig_connection",
            "campaign__ig_connection__workspace__owner",
        ).get(id=log_id)
    except SentDMLog.DoesNotExist:
        logger.warning(f"SentDMLog {log_id} not found")
        return {"status": "not_found"}

    # 이미 처리됨
    if log.status not in (SentDMLog.Status.QUEUED, SentDMLog.Status.SUBMITTING):
        return {"status": "skipped", "reason": f"already {log.status}"}

    campaign = log.campaign
    ig_conn = campaign.ig_connection

    # ★ Self-DM 최후 방어선: 수신자가 계정 자신이면 발송하지 않는다.
    # 적재 시점 가드(_enqueue_send_dm)를 우회하는 경로(requeue_deferred_dms 재투입,
    # SKIPPED revive, 이미 적재된 사고 백로그 — 2026-07-14 prod 셀프 DM 50건)까지
    # 여기서 확정 차단. revive 돼도 다시 이 가드에 걸리므로 재발 불가.
    _own_uname = (ig_conn.username or "").strip().lower()
    if (str(log.recipient_user_id or "") == str(ig_conn.external_account_id)) or (
        _own_uname
        and _own_uname
        in {
            str(log.recipient_user_id or "").strip().lower(),
            str(log.recipient_username or "").strip().lower(),
        }
    ):
        log.mark_skipped("self recipient (account itself)")
        return {"status": "skipped", "reason": "self_recipient"}

    # ★ 캠페인 상태 가드 (v4.3 Fix 1 — 일시중지가 백로그를 실제로 멈추게):
    # 모든 발송이 이 태스크를 거친다 — opening / reward(postback) / follow 재안내 /
    # reconcile 재큐 / 수동 재시도. 대기(QUEUED) 중이던 건이라도 실행 시점에 캠페인이
    # PAUSED/COMPLETED/INACTIVE 면 발송하지 않고 SKIPPED 로 종결한다(REVIVABLE → 재개 시 되살림).
    # (이전엔 예약 창만 봐서 일시중지해도 기존 백로그가 계속 나가는 버그가 있었다.)
    if not campaign.is_active():
        log.mark_skipped(f"Campaign not active (status={campaign.status})")
        return {"status": "skipped", "reason": "campaign_not_active"}

    # ★ 계정 소프트 비활성 가드: is_active=False 계정은 발송에서 제외(캠페인이 우연히
    #   ACTIVE 라도 확정 차단). 활성 계정 초과 축소/재선택 시 in-flight DM 을 여기서 종결.
    if not ig_conn.is_active:
        log.mark_skipped("IG account deactivated")
        return {"status": "skipped", "reason": "ig_account_inactive"}

    # ★ 예약 발송 창 가드 (권위 있는 단일 체크포인트):
    # ACTIVE 여도 예약 창 밖(시작 전/종료 후)이면 여기서 확정 차단한다.
    if not campaign.is_within_schedule():
        log.mark_skipped("Campaign outside active schedule window")
        return {"status": "skipped", "reason": "outside_schedule_window"}

    if ig_conn.status != IGAccountConnection.Status.ACTIVE:
        log.mark_failed(
            status=SentDMLog.Status.FAILED_TOKEN,
            error_message=f"IG connection not active: {ig_conn.status}",
        )
        if log.parent_log_id is None:
            campaign.increment_failed()
        return {"status": "failed_token"}

    # ★ 메시징 윈도우 만료 가드 (graceful 종결의 단일 지점):
    # rate-limit/한도 초과로 계속 defer 되더라도 comment 7일 / user_id 24h 가 지나면
    # Meta 가 무조건 거부하므로 FAILED_WINDOW 로 종결한다(= rate-limit 실패가 아닌 윈도우 만료).
    age = timezone.now() - log.created_at
    if age >= _messaging_window(log):
        log.mark_failed(
            status=SentDMLog.Status.FAILED_WINDOW,
            error_message="Messaging window expired while waiting for send capacity",
        )
        if log.parent_log_id is None:
            campaign.increment_failed()
        return {"status": "failed_window", "reason": "window_expired"}

    # ★ 플랜 월간 DM 한도 가드 (free/basic 200건 — pro/admin 은 -1이라 COUNT 자체 생략):
    # 모든 발송 경로(opening/reward/revive/requeue)가 이 태스크를 지나므로 우회 불가.
    # 한도 초과는 SKIPPED 종결 — REVIVABLE 이므로 업그레이드 후 retry_failed 로 되살릴 수
    # 있다(월경계 defer 는 메시징 윈도우와 충돌해 큐만 부풀리므로 채택하지 않음).
    # 집계 실패는 fail-open(발송 허용) — DM 무손실 원칙.
    from apps.billing.dm_limits import check_dm_quota, notify_quota_reached_once

    owner = ig_conn.workspace.owner
    quota_ok, quota_used, quota_limit = check_dm_quota(owner)
    if not quota_ok:
        notify_quota_reached_once(owner, quota_used, quota_limit)
        log.mark_skipped("monthly_dm_limit_reached")
        return {
            "status": "skipped",
            "reason": "monthly_dm_limit",
            "used": quota_used,
            "limit": quota_limit,
        }

    # ★ 발송 속도 제어 (재진입 포함): 캠페인 시간당 한도/계정 거버너 초과 시
    # 드랍·실패가 아니라 defer(QUEUED+next_retry_at) → requeue 워커가 순차 재투입.
    defer = _rate_defer(log, campaign, ig_conn)
    if defer is not None:
        retry_after, reason = defer
        log.next_retry_at = timezone.now() + timedelta(seconds=retry_after)
        log.status = SentDMLog.Status.QUEUED
        log.save(update_fields=["next_retry_at", "status"])
        return {"status": "deferred", "reason": reason, "retry_after": retry_after}

    log.mark_submitting()

    # Follow-gate: opening DM + PENDING 이면 generic template 버튼 첨부.
    # 사용자가 버튼 클릭 시 webhook 으로 postback payload = "fg:{log_id}" 가 돌아온다.
    # generic template 으로 보내야 인스타 앱에서 "메시지 박스 안에 버튼" 형태로 보임.
    buttons = None
    if log.dm_kind == SentDMLog.DMKind.OPENING and log.gate_status == SentDMLog.GateStatus.PENDING:
        # follow-gate opening/재안내 DM: postback 버튼 (클릭 시 webhook 으로 fg:{log_id} 회신)
        buttons = [
            {
                "type": "postback",
                "title": campaign.get_follow_gate_button_label(),
                "payload": f"fg:{log.id}",
            }
        ]
    elif log.dm_kind in (SentDMLog.DMKind.STANDALONE, SentDMLog.DMKind.REWARD):
        # 콘텐츠 전달 DM(단순 DM / reward): 설정된 링크 버튼(web_url)을 첨부.
        # → 단순 DM 발송 · 버튼클릭 즉시 reward · 팔로우 검증 후 reward 모두 링크 버튼이 붙는다.
        buttons = campaign.get_link_buttons()

    try:
        # 라우팅 규칙:
        #  - 첫 DM(opening/standalone, parent_log 없음 + comment_id 있음) → Private Reply.
        #    (Meta: 댓글당 Private Reply 1회.)
        #  - child(reward/follow 재안내, parent_log 있음) 또는 Story 답장(comment_id 없음)
        #    → user_id 기반 DM(24h 윈도우). child 는 comment_id 를 물려받아도 두 번째
        #    Private Reply 를 보내면 안 되므로 반드시 user_id 경로.
        if log.comment_id and log.parent_log_id is None:
            result = InstagramMessagingService.send_dm_via_comment(
                ig_user_id=ig_conn.external_account_id,
                comment_id=log.comment_id,
                message_text=log.message_sent,
                access_token=ig_conn.access_token,
                buttons=buttons,
            )
        else:
            result = InstagramMessagingService.send_dm_via_user_id(
                ig_user_id=ig_conn.external_account_id,
                recipient_id=log.recipient_user_id,
                message_text=log.message_sent,
                access_token=ig_conn.access_token,
                buttons=buttons,
            )
    except DMSendError as e:
        # ★ P6 (C4): Meta 가 '이미 전달했을 수 있는데 실패처럼 응답' 하는 모호한 케이스는
        # blind 재발송하면 중복(오프닝 DM 2개 함정). 재발송 전 Conversations 조회로 '최근 발송
        # 흔적' 확인 후 분기. 대상: (a) DMAnomalyError(200-no-mid) (b) code 1/2("An unexpected
        # error…retry" — Meta 가 처리·전달하고도 반환하는 사례 잦음, 2026-07-01 실측).
        # 명시적 rate-limit(4/17/32/368/613)은 '요청 거부' 라 전달 없음 → 검증 없이 정상 defer.
        from .dm_exceptions import DMAnomalyError

        maybe_delivered = isinstance(e, DMAnomalyError) or e.code in (1, 2)
        if maybe_delivered:
            _tag = "anomaly" if isinstance(e, DMAnomalyError) else f"err_code_{e.code}"
            recent = InstagramMessagingService.has_recent_message_to_recipient(
                ig_user_id=ig_conn.external_account_id,
                recipient_id=log.recipient_user_id,
                access_token=ig_conn.access_token,
                since_seconds=900,
            )
            if recent is True:
                # 발송 확인됨 → 재발송 금지, 도착 확정 처리(conv_api 로 확인).
                log.mark_accepted(
                    message_id="", api_response={"maybe_delivered": _tag, "recent": True}
                )
                if log.parent_log_id is None:
                    campaign.increment_sent()
                # 접수 확정 = 복구 성공 정산 대상 ('어떤 발송이든 같은 수신자 접수 시 승격' 계약)
                _flip_recovery_on_success(log, campaign)
                log.mark_delivered(via=SentDMLog.VerifiedVia.CONV_API)
                log.append_verification_log({"path": _tag, "result": "confirmed_sent"})
                return {"status": "delivered", "via": f"{_tag}_conv_api"}
            if recent is None and isinstance(e, DMAnomalyError):
                # 200-no-mid + 확인 불가 → 중복 방지 우선: 재발송 안 함('미확인' 분리 집계).
                # (code 1/2 는 여기서 종결하지 않고 아래 defer+retry 로 흘린다 — 무손실 우선.
                #  code 1/2 는 "please retry" 시맨틱이라 미전달 가능성이 크고, API 체크 자체가
                #  타임아웃/5xx 로 None 을 낼 수 있어 여기서 FAILED 로 종결하면 유실 위험.
                #  reconcile_stuck_submitting 의 recent=None→requeue 와 대칭을 맞춘다.)
                log.mark_failed(
                    status=SentDMLog.Status.FAILED_NO_TRACE,
                    error_message="200 OK without message_id; delivery unconfirmed (no resend)",
                    api_response=e.api_response,
                )
                if log.parent_log_id is None:
                    campaign.increment_unconfirmed()
                log.append_verification_log({"path": _tag, "result": "unconfirmed_no_resend"})
                return {"status": "failed_no_trace", "reason": f"{_tag}_unconfirmed"}
            # recent is False(정말 안 감) 또는 code 1/2 + recent None(확인불가) → defer+retry(무손실).
            # ⚠️ 잔여 한계: 전달됐는데 Conversations 인덱싱 지연으로 recent=False 면 재시도 시 중복 가능
            #   (기존 동작과 동일 = 회귀 아님). 완전 제거는 '재시도 직전 재확인' 후속 개선 필요.
        # ★ 실패 DM 복구: opening 비공개답글이 비팔로워 채널 미개설(code=100/subcode=2534025)로
        #   확정 실패했고 캠페인이 복구를 켰다면, 완전 실패로 종결하지 않고 RECOVERY_PENDING +
        #   "숨김함 수락 후 재댓글" 안내 대댓글을 예약한다. 조건 불충족이면 아래 기존 종결 경로로.
        if _maybe_enter_recovery(log, campaign, e):
            return {"status": "recovery_pending", "log_id": str(log.id)}
        # v3.9: rate-limit/transient 은 횟수 상한으로 종결하지 않고 defer.
        # 윈도우 만료(graceful 종결)는 위 진입부 age 가드가 단일 지점에서 담당.
        # no_trace(551·기타4xx)는 실패가 아닌 '미확인'으로 분리 집계.
        return _defer_or_fail(log, campaign, ig_conn, e)

    # 성공 — ACCEPTED 진입
    log.mark_accepted(
        message_id=result["message_id"],
        api_response=result.get("_raw") or result,
    )
    # v3.8: child DM 은 카운트 제외 (위 increment_failed 와 같은 정책)
    if log.parent_log_id is None:
        campaign.increment_sent()

    # ★ 같은 수신자의 발송이 접수되면 이전 RECOVERY_PENDING 을 RECOVERY_DELIVERED 로 승격
    #   (재댓글 복구의 성공 정산 — v2).
    _flip_recovery_on_success(log, campaign)

    # 10분 후 첫 능동 검증 예약 (echo가 먼저 오면 skip됨; v3.9: 쿼터 절약 위해 5→10분)
    verify_dm_delivery.apply_async(args=[str(log.id)], countdown=600)

    # public_reply_enabled 면 댓글에 공개 답글도 게시 (best-effort)
    # v3.5: public_reply_templates(list) 또는 legacy public_reply_template 중 하나라도 있으면 OK
    # v3.7: Story 답장 캠페인 (comment_id 없음) 은 공개 답글 불가능 → skip
    has_reply_content = bool(
        (campaign.public_reply_templates or []) or (campaign.public_reply_template or "").strip()
    )
    if (
        campaign.public_reply_enabled
        and has_reply_content
        and log.comment_id  # Story 답장은 comment_id 없음 → 공개 답글 skip
        and log.dm_kind != SentDMLog.DMKind.REWARD
        and not campaign.public_reply_limit_reached()  # 상한 도달 시 무의미한 태스크 미적재(best-effort)
    ):
        # 5~15초 지터를 enqueue 시점에서 적용 (Instagram 봇 검사 회피)
        import random as _r

        post_public_reply.apply_async(
            args=[str(log.id)],
            countdown=_r.randint(5, 15),
        )

    return {
        "status": "accepted",
        "log_id": str(log.id),
        "message_id": result["message_id"],
    }


# ===== 능동 검증 (단건) =====


@shared_task(bind=True, max_retries=3)
def verify_dm_delivery(self, log_id: str):
    """
    GET /{message_id} 로 단건 도착 검증 (v3.2 — 5분/35분 2단계).

    호출 시점:
        1) ACCEPTED 5분 후 (send_dm_task 가 예약)
        2) reconcile_accepted_dms 워커가 누락 건에 재호출

    35분 cutoff:
        ACCEPTED 후 35분이 지나도 echo·Conv API 모두 미발견이면
        FAILED_NO_TRACE 로 종결 (프론트는 자가 점검 체크리스트 노출).
    """
    try:
        log = SentDMLog.objects.select_related("campaign__ig_connection").get(id=log_id)
    except SentDMLog.DoesNotExist:
        return {"status": "not_found"}

    # 이미 도착 확정 / 종결 상태면 skip
    if log.is_delivered() or log.is_terminal():
        return {"status": "skipped", "reason": f"already {log.status}"}

    if log.status != SentDMLog.Status.ACCEPTED or not log.meta_message_id:
        return {"status": "skipped", "reason": "not in ACCEPTED with message_id"}

    ig_conn = log.campaign.ig_connection

    try:
        message = InstagramMessagingService.fetch_message(
            message_id=log.meta_message_id,
            access_token=ig_conn.access_token,
        )
    except DMTransientError as e:
        log.append_verification_log(
            {"path": "conv_api", "result": "transient_error", "error": str(e)}
        )
        raise self.retry(exc=e, countdown=120) from e
    except DMSendError as e:
        log.append_verification_log({"path": "conv_api", "result": "api_error", "error": str(e)})
        return {"status": "api_error", "error": str(e)}

    if message is not None:
        log.append_verification_log(
            {"path": "conv_api", "result": "found", "message_id": message.get("id")}
        )
        log.mark_delivered(via=SentDMLog.VerifiedVia.CONV_API)
        return {"status": "delivered", "via": "conv_api"}

    # 미발견 — 35분 cutoff 검사
    age = timezone.now() - (log.accepted_at or log.created_at)
    log.append_verification_log(
        {"path": "conv_api", "result": "not_found", "age_seconds": age.total_seconds()}
    )

    if age.total_seconds() < 35 * 60:
        # 첫 검증(10분 시점) — 35분 시점까지 한 번만 더 예약 (v3.9: sparse 폴링 = GET 약 2회).
        remaining = max(60, int(35 * 60 - age.total_seconds()))
        # next_retry_at 기록 → reconcile_accepted_dms 가 예약 살아있는 건은 재큐하지 않도록 게이트.
        log.next_retry_at = timezone.now() + timedelta(seconds=remaining)
        log.save(update_fields=["next_retry_at"])
        verify_dm_delivery.apply_async(args=[log_id], countdown=remaining)
        return {"status": "deferred", "next_check_in_seconds": remaining}

    # 35분+ — 도착 확인 실패 종결 (자가 점검 체크리스트 영역)
    log.mark_failed(
        status=SentDMLog.Status.FAILED_NO_TRACE,
        error_message="No echo and not found in Conversations API after 35 minutes",
    )
    # v3.9: no_trace 는 '실패'가 아니라 '미확인' → 전용 카운터(success_rate/total_failed 와 분리).
    log.campaign.increment_unconfirmed()
    return {"status": "failed_no_trace"}


# ===== Beat 워커 =====


@shared_task
def reconcile_accepted_dms():
    """
    ACCEPTED 상태로 10분 이상 머무는 건들을 능동 검증 (예약 유실 고아만).

    v3.9: 매분 무조건 재큐하던 방식은 stuck 1건당 GET ~30회 쿼터 낭비를 유발했다.
    verify_dm_delivery 가 다음 검증을 next_retry_at 으로 예약하므로, 여기서는 예약이
    없거나(워커 재시작/메시지 유실) 2분 이상 지난 '고아'만 재가동한다(cutoff 5→10분).

    Beat: 1분 주기.
    """
    now = timezone.now()
    cutoff = now - timedelta(minutes=10)
    grace = now - timedelta(minutes=2)
    queryset = (
        SentDMLog.objects.filter(
            status=SentDMLog.Status.ACCEPTED,
            accepted_at__lte=cutoff,
        )
        .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=grace))
        .values_list("id", flat=True)[:200]
    )

    count = 0
    for log_id in queryset:
        verify_dm_delivery.delay(str(log_id))
        count += 1

    if count:
        logger.info(f"reconcile_accepted_dms: queued {count} verifications")
    return {"queued": count}


@shared_task
def reconcile_stuck_submitting():
    """
    SUBMITTING 상태로 60초 이상 정체된 건을 큐로 되돌림(워커 크래시 복구).

    ★ P6 (F2): Meta accept 직후 mark_accepted 전에 크래시했다면 그냥 재발송 시 중복.
    재큐 전에 Conversations 조회로 '이미 보냈는지' 확인 — 확인되면(recent=True) 재발송 대신
    도착 확정 처리한다. 확인 불가/없음(None/False)이면 기존대로 재큐(무손실 우선).

    Beat: 30초 주기.
    """
    cutoff = timezone.now() - timedelta(seconds=60)
    qs = SentDMLog.objects.select_related("campaign__ig_connection").filter(
        status=SentDMLog.Status.SUBMITTING,
        submitted_at__lte=cutoff,
    )

    requeued = 0
    confirmed = 0
    for log in qs[:100]:
        ig_conn = log.campaign.ig_connection
        recent = None
        try:
            recent = InstagramMessagingService.has_recent_message_to_recipient(
                ig_user_id=ig_conn.external_account_id,
                recipient_id=log.recipient_user_id,
                access_token=ig_conn.access_token,
                since_seconds=900,
            )
        except Exception:  # noqa: BLE001 - 조회 실패는 보수적으로 재큐(무손실 우선)
            recent = None

        if recent is True:
            # 이미 발송됨 → 재발송 금지, 도착 확정 처리.
            log.mark_accepted(message_id="", api_response={"stuck_recovery": True, "recent": True})
            if log.parent_log_id is None:
                log.campaign.increment_sent()
            # 접수 확정 = 복구 성공 정산 대상 ('어떤 발송이든 같은 수신자 접수 시 승격' 계약)
            _flip_recovery_on_success(log, log.campaign)
            log.mark_delivered(via=SentDMLog.VerifiedVia.CONV_API)
            log.append_verification_log({"path": "stuck_recovery", "result": "confirmed_sent"})
            confirmed += 1
            continue

        # recent in (False, None) → 발송 흔적 없음/확인 불가 → 재큐(기존 동작, 무손실 우선).
        log.status = SentDMLog.Status.QUEUED
        log.save(update_fields=["status"])
        send_dm_task.delay(str(log.id))
        requeued += 1

    if requeued or confirmed:
        logger.warning(
            "reconcile_stuck_submitting: requeued %s, confirmed_already_sent %s",
            requeued,
            confirmed,
        )
    return {"requeued": requeued, "confirmed": confirmed}


@shared_task
def requeue_deferred_dms():
    """next_retry_at 이 도래(+look-ahead)한 defer(QUEUED) 건을 send_dm_task 로 재투입.

    rate-limit/transient defer(item1) + 페이서 슬롯/백스톱 defer(item2) 공통 픽커다.
    created_at 오름차순으로 처리 = 가장 오래 기다린 건부터.
    next_retry_at 이 없는 채 2분+ 정체된 QUEUED(초기 dispatch 유실)도 안전 재투입한다.

    v4.3 — 슬롯 스태거: beat 주기(30s)가 페이서 간격(3~7s)보다 굵어서 "도래분 일괄 발사"
    하면 슬롯 간격이 뭉개진다(버스트 = 봇 지문). 그래서 look-ahead 창(now+35s) 안의 건까지
    미리 픽업하되, 각 건을 **countdown = 슬롯까지 남은 초**로 예약 발사한다 → 워커가
    슬롯 시각에 정확히 실행. 재진입한 태스크는 dm_pacer 의 claimed 플래그로 재클레임 없이
    통과한다(포인터 이중 전진 방지).

    동시 픽업은 select_for_update(skip_locked) + next_retry_at=None 마킹으로 방지하고,
    send_dm_task 진입 가드(status QUEUED/SUBMITTING)가 이중 발송을 막는다.

    Beat: 30초 주기.
    """
    now = timezone.now()
    stale_cutoff = now - timedelta(minutes=2)
    lookahead = now + timedelta(seconds=35)
    with transaction.atomic():
        rows = list(
            SentDMLog.objects.select_for_update(skip_locked=True)
            .filter(status=SentDMLog.Status.QUEUED)
            .filter(
                Q(next_retry_at__lte=lookahead)
                | Q(next_retry_at__isnull=True, created_at__lte=stale_cutoff)
            )
            .order_by("created_at")
            .values_list("id", "next_retry_at")[:200]
        )
        ids = [r[0] for r in rows]
        if ids:
            # 픽업 표식: next_retry_at 비워 다음 tick 중복 픽업 방지(재defer 시 send_dm_task 가 다시 채움).
            SentDMLog.objects.filter(id__in=ids).update(next_retry_at=None)

    for log_id, retry_at in rows:
        countdown = max(0.0, (retry_at - now).total_seconds()) if retry_at else 0.0
        if countdown > 0.5:
            send_dm_task.apply_async(args=[str(log_id)], countdown=countdown)
        else:
            send_dm_task.delay(str(log_id))

    if ids:
        logger.info(f"requeue_deferred_dms: requeued {len(ids)} deferred logs")
    return {"requeued": len(ids)}


@shared_task
def reconcile_pacer_pointers():
    """페이서 포인터 자가치유 (v4.3 Fix 2) — 삭제/일시중지로 생긴 '빈 슬롯 홀' 회수.

    캠페인 삭제(CASCADE 로 QUEUED 소멸)·일시중지(QUEUED→SKIPPED)로 대기 건이 사라져도
    계정 페이서 포인터는 단조 증가라 앞선 채 남는다 → 새 DM 이 유휴 시간을 기다리는 문제.
    각 계정×버킷 포인터를 '아직 대기중인 마지막 예약 슬롯(max next_retry_at)'까지만 당겨
    그 뒤의 phantom 예약을 회수한다. 실제 예약 뒤로는 안 당기고(충돌 방지), now 아래로도
    안 내려간다(과속 불가). slack(기본 300s)으로 클레임 write-lag 오탐을 흡수한다.

    한계: 계정 내 여러 캠페인이 시간상 섞인 상태에서 일부만 삭제하면 '중간 홀'은 남는다
    (이미 클레임된 잔존 DM 은 자기 슬롯을 유지) — 계정이 잠시 저속 발송할 뿐 무손실.
    tail(가장 흔한 케이스: 백로그를 통째로 삭제/중지)은 완전 회수된다.

    Beat: 60초. Redis 전용(LocMem 이면 no-op).
    """
    from django.db.models import Max

    from . import dm_pacer

    now = timezone.now()
    now_ts = now.timestamp()
    reclaimed: dict[str, float] = {}
    for bucket, acct in dm_pacer.iter_active_pointers():
        latest = (
            SentDMLog.objects.filter(
                campaign__ig_connection__external_account_id=acct,
                status=SentDMLog.Status.QUEUED,
            )
            .filter(dm_pacer.bucket_q(bucket))
            .aggregate(m=Max("next_retry_at"))["m"]
        )
        floor_ts = max(now_ts, latest.timestamp() if latest else 0.0)
        secs = dm_pacer.reclaim_pointer(acct, bucket, floor_ts)
        if secs > 0:
            reclaimed[f"{bucket}:{acct}"] = round(secs, 1)
    if reclaimed:
        logger.info("reconcile_pacer_pointers: reclaimed phantom slots %s", reclaimed)
    return {"reclaimed": reclaimed}


@shared_task
def dead_letter_alerter():
    """
    최근 10분 내 발생한 FAILED_TOKEN/FAILED_NO_TRACE 누적 시 알림.

    Beat: 10분 주기. 운영자가 토큰 만료/도달 안되는 캠페인을 즉시 인지하도록.
    """
    window_start = timezone.now() - timedelta(minutes=10)

    token_failures = SentDMLog.objects.filter(
        status=SentDMLog.Status.FAILED_TOKEN,
        created_at__gte=window_start,
    ).count()

    no_trace = SentDMLog.objects.filter(
        status=SentDMLog.Status.FAILED_NO_TRACE,
        created_at__gte=window_start,
    ).count()

    if token_failures > 0 or no_trace > 0:
        logger.error(
            "DM dead-letter alert: "
            f"FAILED_TOKEN={token_failures}, FAILED_NO_TRACE={no_trace} (last 10min)"
        )
        # P9: 로그뿐 아니라 Telegram 으로도 즉시 통지 (운영자 인지).
        try:
            from apps.core.telegram import send_telegram_notification

            send_telegram_notification(
                "⚠️ *DM dead-letter* (최근 10분)\n"
                f"FAILED_TOKEN: *{token_failures}* (재연동 필요)\n"
                f"FAILED_NO_TRACE: *{no_trace}* (도착 미확인)"
            )
        except Exception:  # noqa: BLE001
            logger.exception("dead_letter_alerter telegram 알림 실패 (non-fatal)")

    return {"failed_token": token_failures, "failed_no_trace": no_trace}


@shared_task
def dm_backlog_alert():
    """발송 백로그 위험 시 Telegram 경고 (P7 — E1 손실 예방). Beat: 30분 주기.

    QUEUED(대기) 중 메시징 윈도우(7d/24h) 만료가 임박(기본 6h 이내)한 건이 있거나,
    가장 오래된 QUEUED 가 임계(기본 2h)를 넘으면 알림 → 윈도우 만료 손실 전 운영자 인지.
    """
    now = timezone.now()
    risk_hours = getattr(settings, "DM_BACKLOG_RISK_HOURS", 6)
    oldest_alert_hours = getattr(settings, "DM_BACKLOG_OLDEST_ALERT_HOURS", 2)

    queued = SentDMLog.objects.filter(status=SentDMLog.Status.QUEUED)
    total = queued.count()
    if not total:
        return {"total_queued": 0}

    oldest = queued.order_by("created_at").values_list("created_at", flat=True).first()
    oldest_age_h = (now - oldest).total_seconds() / 3600 if oldest else 0

    risk_cut = timedelta(hours=risk_hours)
    risk = 0
    for cid, created in queued.order_by("created_at").values_list("comment_id", "created_at")[
        :5000
    ]:
        window = timedelta(days=7) if cid else timedelta(hours=24)
        if (created + window) - now <= risk_cut:
            risk += 1

    if risk > 0 or oldest_age_h >= oldest_alert_hours:
        try:
            from apps.core.telegram import send_telegram_notification

            send_telegram_notification(
                "📨 *DM 백로그 경고*\n"
                f"대기(QUEUED): *{total}*\n"
                f"윈도우 만료 임박({risk_hours}h 내): *{risk}*\n"
                f"최오래 대기: *{oldest_age_h:.1f}h*"
            )
        except Exception:  # noqa: BLE001
            logger.exception("dm_backlog_alert telegram 실패 (non-fatal)")


@shared_task
def dm_infra_health_alert():
    """론칭 하드닝(#6) — 'CPU/메모리/재시작 신호에 안 잡히고 조용히 터지는' 인프라 신호를 Telegram 노출.

    감시 3종(임계 초과분만 한 번에 묶어 알림 → 스팸 방지, 실패는 비치명적):
      1) Redis 사용량 > maxmemory 임계(기본 70%) — noeviction 이라 8G 도달 시 write 거부 →
         rate_governor fail-closed 로 전 계정 DM 정지(조용한 장애).
      2) 브로커 큐(LLEN) 적체 — 워커 stall/CF tick 정지 시 큐가 쌓임(dm_backlog_alert 는 DB 레벨만 봄).
      3) 밀린 deferred DM 나이 — requeue 파이프라인(워커/CF tick) 정지 신호.
    """
    from .queue_health import oldest_due_deferred_dm_age_s, queue_depths

    problems: list[str] = []

    # 1) Redis 메모리 (noeviction → 임계 초과 시 write 거부 = 전 계정 DM freeze)
    try:
        import redis as _redis

        c = _redis.from_url(settings.CELERY_BROKER_URL)
        try:
            info = c.info("memory")
            used = int(info.get("used_memory", 0))
            maxm = int(info.get("maxmemory", 0))
            if maxm > 0:
                pct = round(used / maxm * 100, 1)
                if pct >= getattr(settings, "REDIS_MEM_ALERT_PCT", 70):
                    problems.append(
                        f"Redis 메모리 {pct}% ({used >> 20}MiB/{maxm >> 20}MiB) — noeviction write 거부 임박"
                    )
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        logger.debug("dm_infra_health_alert: redis mem check failed", exc_info=True)

    # 2) 큐 적체
    depths = queue_depths()
    depth_cut = getattr(settings, "QUEUE_DEPTH_ALERT", 5000)
    hot = {q: n for q, n in depths.items() if n >= depth_cut}
    if hot:
        problems.append(
            "큐 적체: " + ", ".join(f"{q}={n}" for q, n in sorted(hot.items(), key=lambda x: -x[1]))
        )

    # 3) deferred DM 밀림 (requeue 파이프라인 정지)
    age = oldest_due_deferred_dm_age_s()
    if age is not None and age >= getattr(settings, "DEFERRED_DM_ALERT_SECONDS", 3600):
        problems.append(f"deferred DM 밀림 {age // 60}분 — requeue 파이프라인 점검(CF tick/워커)")

    if problems:
        try:
            from apps.core.telegram import send_telegram_notification

            send_telegram_notification("🩺 *인프라 헬스 경고*\n- " + "\n- ".join(problems))
        except Exception:  # noqa: BLE001
            logger.exception("dm_infra_health_alert telegram 실패 (non-fatal)")
    return {"problems": problems, "queue_depths": depths}


@shared_task
def expire_gate_pending():
    """
    Opening DM ACCEPTED 후 24시간 내 사용자 답장이 없으면 gate_status=EXPIRED.

    Beat: 1시간 주기.
    """
    cutoff = timezone.now() - timedelta(hours=24)
    qs = SentDMLog.objects.filter(
        gate_status=SentDMLog.GateStatus.PENDING,
        accepted_at__lte=cutoff,
    )
    count = 0
    for log in qs[:200]:
        log.gate_status = SentDMLog.GateStatus.EXPIRED
        log.save(update_fields=["gate_status"])
        count += 1
    if count:
        logger.info(f"expire_gate_pending: marked {count} as EXPIRED")
    return {"expired": count}


# ===== Phase 2: next_media 폴링 =====


def _parse_iso_timestamp(value: str):
    """Meta가 보내는 ISO8601 timestamp ('2026-05-07T03:14:15+0000') 파싱"""
    from datetime import datetime as _dt

    if not value:
        return None
    # Meta는 종종 "+0000" (콜론 없음) 형식으로 보내므로 보정
    v = value.replace("+0000", "+00:00").replace("Z", "+00:00")
    try:
        return _dt.fromisoformat(v)
    except ValueError:
        return None


@shared_task
def snapshot_baseline_for_account(ig_connection_id: str):
    """
    next_media 캠페인이 처음 만들어졌거나 last_seen 이 비어있는 IG 계정에 대해,
    "현재 최신 게시물" 을 baseline 으로 기록.

    이렇게 해야 과거 게시물에 attach 되지 않고, 진짜 "다음 게시물"부터 트리거된다.
    """
    try:
        conn = IGAccountConnection.objects.get(id=ig_connection_id)
    except IGAccountConnection.DoesNotExist:
        return {"status": "not_found"}

    if conn.last_seen_media_id:
        return {"status": "already_snapshotted"}
    if conn.status != IGAccountConnection.Status.ACTIVE:
        return {"status": "skipped", "reason": "ig connection not active"}

    try:
        media_list = InstagramMediaService.list_recent_media(
            ig_user_id=conn.external_account_id,
            access_token=conn.access_token,
            limit=1,
        )
    except Exception as e:
        logger.warning(f"snapshot_baseline_for_account: API failed: {e}")
        return {"status": "api_error", "error": str(e)}

    if not media_list:
        # 게시물이 하나도 없는 신규 계정 — 빈 sentinel 로 표시
        conn.last_polled_at = timezone.now()
        conn.save(update_fields=["last_polled_at"])
        return {"status": "no_media"}

    latest = media_list[0]
    conn.last_seen_media_id = str(latest.get("id") or "")
    conn.last_seen_media_at = _parse_iso_timestamp(latest.get("timestamp"))
    conn.last_polled_at = timezone.now()
    conn.save(
        update_fields=[
            "last_seen_media_id",
            "last_seen_media_at",
            "last_polled_at",
        ]
    )
    return {"status": "ok", "baseline_media_id": conn.last_seen_media_id}


@shared_task
def poll_new_media_for_next_campaigns():
    """
    next_media 트리거 캠페인이 있는 IG 계정에 대해 신규 게시물 발견 시
    캠페인의 media_id 자동 attach + trigger_type 을 specific_media 로 전환.

    Beat: 5분 주기.

    동작:
      1. 활성 next_media 캠페인을 가진 ACTIVE IG 계정만 후보
      2. 각 계정의 최근 게시물 5건 조회
      3. last_seen_media_id 보다 timestamp 가 더 새로운 미디어가 있으면:
         - 그 중 가장 오래된(=새 미디어 중 첫번째) 미디어를 골라
         - 해당 계정의 모든 next_media 캠페인에 attach
         - last_seen_* 업데이트
      4. last_seen 이 비어있으면 baseline 만 기록 (기존 게시물에 attach 안 함)
    """
    from .models import AutoDMCampaign

    next_campaigns_qs = (
        AutoDMCampaign.objects.filter(
            trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
            status=AutoDMCampaign.Status.ACTIVE,
            media_id="",
        )
        .values_list("ig_connection_id", flat=True)
        .distinct()
    )

    ig_connection_ids = list(next_campaigns_qs)
    if not ig_connection_ids:
        return {"polled": 0, "attached": 0}

    polled = 0
    attached_total = 0
    skipped_recent = 0

    # 폴링 가드: 같은 계정을 3분 이내에 다시 폴링하지 않도록 (Beat 중복 fire 방어)
    min_interval = timedelta(minutes=3)
    now = timezone.now()

    for conn in IGAccountConnection.objects.filter(
        id__in=ig_connection_ids,
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    ):
        if conn.last_polled_at and (now - conn.last_polled_at) < min_interval:
            skipped_recent += 1
            continue

        polled += 1
        try:
            media_list = InstagramMediaService.list_recent_media(
                ig_user_id=conn.external_account_id,
                access_token=conn.access_token,
                limit=5,
            )
        except Exception as e:
            logger.warning(f"poll_new_media: API failed for ig_conn={conn.id}: {e}")
            continue

        conn.last_polled_at = timezone.now()

        if not media_list:
            conn.save(update_fields=["last_polled_at"])
            continue

        # baseline 미설정 → 현재 최신 미디어를 baseline 으로만 기록
        if not conn.last_seen_media_id:
            latest = media_list[0]
            conn.last_seen_media_id = str(latest.get("id") or "")
            conn.last_seen_media_at = _parse_iso_timestamp(latest.get("timestamp"))
            conn.save(
                update_fields=[
                    "last_seen_media_id",
                    "last_seen_media_at",
                    "last_polled_at",
                ]
            )
            continue

        # baseline 보다 새로운 미디어 추출 (timestamp 또는 id 비교)
        baseline_ts = conn.last_seen_media_at
        baseline_id = conn.last_seen_media_id

        new_medias = []
        for m in media_list:
            mid = str(m.get("id") or "")
            mts = _parse_iso_timestamp(m.get("timestamp"))
            if not mid or mid == baseline_id:
                continue
            if baseline_ts and mts and mts <= baseline_ts:
                continue
            new_medias.append((mts, mid, m))

        if not new_medias:
            conn.save(update_fields=["last_polled_at"])
            continue

        # 가장 오래된 새 미디어부터 처리 (= 진짜 "next" 부터)
        new_medias.sort(key=lambda x: (x[0] or timezone.now(),))

        attached_for_account = 0
        for _mts, mid, media_obj in new_medias:
            # '한 게시물 = 활성 캠페인 1개' 불변식 유지 — 1개만 attach, 나머지 후보는 자동 일시정지.
            candidate_ids = list(
                AutoDMCampaign.objects.filter(
                    ig_connection_id=conn.id,
                    trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
                    status=AutoDMCampaign.Status.ACTIVE,
                    media_id="",
                ).values_list("id", flat=True)
            )
            result = AutoDMCampaign.attach_next_media_single_active(
                ig_connection_id=conn.id,
                candidate_ids=candidate_ids,
                media_id=mid,
                media_url=media_obj.get("permalink") or None,
                media_published_at=_mts,
            )
            n_attached = len(result["attached"])
            attached_for_account += n_attached
            attached_total += n_attached
            if result["paused"]:
                logger.info(
                    "poll_new_media: paused %s duplicate next_media campaign(s) "
                    "on ig_conn=%s media=%s",
                    len(result["paused"]),
                    conn.id,
                    mid,
                )

            # 첫 새 미디어에 1개 attach(+나머지 pause)했으면 남은 후보가 없음 → loop 탈출
            if n_attached == 0:
                break

            # last_seen 갱신은 마지막(=가장 최신) 미디어로
        latest_new = new_medias[-1][2]
        conn.last_seen_media_id = str(latest_new.get("id") or "")
        conn.last_seen_media_at = _parse_iso_timestamp(latest_new.get("timestamp"))
        conn.save(
            update_fields=[
                "last_seen_media_id",
                "last_seen_media_at",
                "last_polled_at",
            ]
        )

        if attached_for_account:
            logger.info(
                f"poll_new_media: attached {attached_for_account} next_media "
                f"campaigns on ig_conn={conn.id} to media={mid}"
            )

    return {
        "polled": polled,
        "attached": attached_total,
        "skipped_recent": skipped_recent,
    }


@shared_task
def check_polling_anomalies():
    """
    next_media 폴링 운영 모니터링 (v3.4).

    Beat: 1시간 주기. 다음 이상 상황 발견 시 logger.warning:
        - 활성 next_media 캠페인이 있는데 한 번도 폴링되지 않은 IG 계정
        - 활성 next_media 캠페인이 있는데 마지막 폴링이 15분 이상 전인 IG 계정

    Beat 가 죽었거나, 워커가 task 를 받지 못하거나, 토큰 만료 등의
    원인으로 폴링이 안 돌고 있는지 빠르게 인지하기 위함.
    """
    from .models import AutoDMCampaign

    now = timezone.now()
    stale_threshold = timedelta(minutes=15)

    # 활성 next_media 캠페인 보유 IG 계정 (media_id 비어있는 것만 = 아직 attach 안됨)
    pending_ig_ids = (
        AutoDMCampaign.objects.filter(
            trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
            status=AutoDMCampaign.Status.ACTIVE,
            media_id="",
        )
        .values_list("ig_connection_id", flat=True)
        .distinct()
    )

    if not pending_ig_ids:
        return {"checked": 0, "never_polled": 0, "stale": 0}

    never_polled = []
    stale = []

    for conn in IGAccountConnection.objects.filter(
        id__in=list(pending_ig_ids),
        status=IGAccountConnection.Status.ACTIVE,
        is_active=True,
    ).only("id", "external_account_id", "username", "last_polled_at"):
        if conn.last_polled_at is None:
            never_polled.append(conn)
        elif (now - conn.last_polled_at) > stale_threshold:
            stale.append(conn)

    if never_polled:
        ids = ", ".join(str(c.id) for c in never_polled[:10])
        logger.warning(
            f"check_polling_anomalies: {len(never_polled)} ig_conn never polled "
            f"despite active next_media campaigns: [{ids}]"
        )
    if stale:
        items = ", ".join(f"{c.id}(last={c.last_polled_at.isoformat()})" for c in stale[:10])
        logger.warning(f"check_polling_anomalies: {len(stale)} ig_conn stale (>15min): [{items}]")

    return {
        "checked": len(pending_ig_ids),
        "never_polled": len(never_polled),
        "stale": len(stale),
    }


# ===== 예약 발송: 활성 기간 자동 종료 =====


@shared_task
def enforce_campaign_schedules():
    """예약 발송 캠페인의 자동 종료 처리 (Beat: 1분 주기).

    scheduled_end_at 이 지난 ACTIVE 캠페인을 COMPLETED 로 전환하고 ended_at 을 기록한다.
    멱등 — 이미 COMPLETED/PAUSED 면 대상 아님.

    시작(scheduled_start_at) 게이팅은 발송 경로의 ``schedule_window_q`` 가 담당하므로
    여기서는 별도 상태 전이를 하지 않는다 (시작 전 캠페인은 ACTIVE 그대로 두되 발송만 안 됨).
    이 분리 덕분에 Beat 가 잠시 죽어도 "시작 전인데 발송됨" 같은 오발송은 발생하지 않는다.
    """
    now = timezone.now()
    ended = AutoDMCampaign.objects.filter(
        status=AutoDMCampaign.Status.ACTIVE,
        scheduled_end_at__isnull=False,
        scheduled_end_at__lte=now,
    ).update(
        status=AutoDMCampaign.Status.COMPLETED,
        ended_at=now,
        updated_at=now,
    )
    if ended:
        logger.info(f"enforce_campaign_schedules: auto-completed {ended} campaign(s)")
    return {"auto_completed": ended}


# ===== 댓글 웹훅 누락 보정 (polling) =====


def _poll_one_media(conn: IGAccountConnection, media_id: str, now) -> dict:
    """한 (connection, media) 의 최근 댓글을 newest-first 로 훑어 웹훅 누락분을 보정 발송.

    종료 조건(먼저 만나는 것):
      1) 앵커 — 이미 장부(SeenComment)에 있는 댓글 → 그보다 오래된 건 모두 관측됨.
      2) 7일 창(window_floor) 밖 댓글 — Private Reply 불가(어차피 Meta code=100) → 중단.
      3) 댓글 소진(paging_after 없음).
      4) 폭주 방지 상한(MAX_PAGES) — 도달 시 backfill gap 가능성 → 경고.

    각 누락분은 기존 ``_enqueue_send_dm`` 로 enqueue → idempotency_key 가 웹훅과의
    중복을 막고, rate_governor 가 발송량을 throttle 한다.
    """
    token = conn.access_token
    window_floor = now - timedelta(days=getattr(settings, "PRIVATE_REPLY_WINDOW_DAYS", 7))
    page_size = getattr(settings, "MISSED_COMMENT_POLL_PAGE_SIZE", 50)
    max_pages = getattr(settings, "MISSED_COMMENT_POLL_MAX_PAGES", 20)
    own_username = (conn.username or "").strip().lower()

    misses = 0
    scanned = 0
    after = None
    pages = 0

    while pages < max_pages:
        resp = InstagramMediaService.list_media_comments(
            media_id, token, limit=page_size, after=after
        )
        comments = resp.get("data") or []
        if not comments:
            return {"misses": misses, "scanned": scanned, "stop": "empty"}

        for c in comments:
            cid = c.get("id")
            if not cid:
                continue
            scanned += 1
            ts = _parse_iso_timestamp(c.get("timestamp"))

            # (2) 7일 창 밖 → newest-first 이므로 이후도 전부 밖 → 종료
            if ts is not None and ts < window_floor:
                return {"misses": misses, "scanned": scanned, "stop": "window_floor"}

            # (1) 장부 멱등 기록 — 이미 있으면 앵커
            created = _record_seen_comment(
                ig_connection_id=conn.id,
                comment_id=cid,
                media_id=media_id,
                source=SeenComment.Source.POLL,
            )
            if not created:
                return {"misses": misses, "scanned": scanned, "stop": "anchor"}

            # ★ 웹훅 경로와 동일한 2대 가드 — 누락 시 셀프 DM 자기증식 루프
            #   (2026-07-14 prod 실측: 우리가 단 공개답글을 매시간 주워 자기 자신에게
            #    opening DM 50건 → 그 DM 의 공개답글을 다음 폴링이 또 주움).
            # (a) self-comment 스킵(대댓글 가드보다 먼저 — 우리 답글이 어떤 분기로도 안 새게):
            #     계정 본인 작성 댓글. comments edge 의 from.id(요청 필드) + username
            #     (케이스 무시, from 누락 응답 대비) 이중 비교.
            c_from_id = str((c.get("from") or {}).get("id") or "")
            c_username = str(c.get("username") or "").strip().lower()
            if (c_from_id and c_from_id == str(conn.external_account_id)) or (
                own_username and c_username == own_username
            ):
                continue
            text = c.get("text") or ""
            # (b) 대댓글(답글) 스킵: top-level 만 캠페인 트리거. 단 RECOVERY_PENDING 보유
            #     사용자의 답글은 복구 재댓글로 라우팅(웹훅 경로와 동일 예외).
            if c.get("parent_id"):
                _maybe_route_recovery_recomment(
                    page_ig_user_id=str(conn.external_account_id),
                    from_user_id=c_from_id,
                    from_username=str(c.get("username") or ""),
                    comment_id=cid,
                    comment_text=text,
                    media_id=media_id,
                    source="poll_reply",
                )
                continue

            # 진짜 누락분 → 트리거 평가
            matched = _matched_campaigns_for_comment(
                ig_user_id=conn.external_account_id,
                media_id=media_id,
                comment_text=text,
                now=now,
            )
            # ★ 복구 재댓글 라우팅(키워드 비적용) — 웹훅 경로와 동일 예외.
            _maybe_route_recovery_recomment(
                page_ig_user_id=str(conn.external_account_id),
                from_user_id=c_from_id,
                from_username=str(c.get("username") or ""),
                comment_id=cid,
                comment_text=text,
                media_id=media_id,
                source="poll",
            )
            if not matched:
                continue

            # 수신자 식별: 댓글 edge 는 from.id 를 잘 주지 않으므로 username/comment_id 로 대체
            # (실제 발송은 comment_id Private Reply 라 수신자 id 불필요. 쿨다운/통계용).
            recipient_key = (c.get("from") or {}).get("id") or c.get("username") or cid
            uname = c.get("username") or ""
            enq_payload = {
                "source": "poll_missed_comments",
                "media_id": media_id,
                "comment_id": cid,
                "comment_ts": c.get("timestamp"),
            }
            any_enqueued = False
            for camp in matched:
                # per-campaign baseline: 캠페인 시작 전 댓글은 보정 발송하지 않음
                # (오래된 게시물에 캠페인을 새로 켰을 때 기존 댓글 대량 발송 방지).
                eff_start = camp.started_at or camp.scheduled_start_at or camp.created_at
                if ts is not None and eff_start is not None and ts < eff_start:
                    continue
                _enqueue_send_dm(
                    campaign=camp,
                    comment_id=cid,
                    comment_text=text,
                    from_user_id=recipient_key,
                    from_username=uname,
                    webhook_payload=enq_payload,
                )
                any_enqueued = True

            if any_enqueued:
                misses += 1
                try:
                    SeenComment.objects.filter(ig_connection_id=conn.id, comment_id=cid).update(
                        triggered=True
                    )
                except Exception:
                    pass

        after = resp.get("paging_after")
        pages += 1
        if not after:
            return {"misses": misses, "scanned": scanned, "stop": "end_of_comments"}

    # MAX_PAGES 소진했는데 앵커/창 미도달 → backfill gap 가능성 경고
    logger.warning(
        "poll_missed_comments: budget exhausted without anchor — possible gap "
        "(conn=%s media=%s scanned=%s)",
        conn.id,
        media_id,
        scanned,
    )
    try:
        from apps.core.telegram import send_telegram_notification

        send_telegram_notification(
            f"⚠️ 댓글 누락 보정 폴링 budget 소진 (gap 가능): "
            f"media={media_id} scanned={scanned}p={max_pages}"
        )
    except Exception:
        pass
    return {"misses": misses, "scanned": scanned, "stop": "budget_exhausted"}


@shared_task(name="integrations.poll_missed_comments")
def poll_missed_comments():
    """댓글 웹훅 누락 보정 (Beat: 1시간 주기).

    활성·예약창 내 specific_media(=attach된 next_media 포함) 캠페인이 붙은
    (connection, media) 마다 최근 댓글을 재조회해 웹훅 누락분을 보정 발송한다.
    ANY_MEDIA / STORY_REPLY 는 v1 대상 아님(전자는 비용, 후자는 댓글 아님).
    """
    if not getattr(settings, "MISSED_COMMENT_POLL_ENABLED", True):
        return {"enabled": False}

    now = timezone.now()
    max_targets = getattr(settings, "MISSED_COMMENT_POLL_MAX_TARGETS", 1000)

    targets = list(
        AutoDMCampaign.objects.filter(status=AutoDMCampaign.Status.ACTIVE)
        .filter(AutoDMCampaign.schedule_window_q(now))
        .filter(trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA)
        .exclude(media_id="")
        .values_list("ig_connection_id", "media_id")
        .distinct()[:max_targets]
    )
    if not targets:
        return {"targets": 0, "polled": 0, "misses": 0}

    conn_ids = {t[0] for t in targets}
    conns = {
        c.id: c
        for c in IGAccountConnection.objects.filter(
            id__in=conn_ids, status=IGAccountConnection.Status.ACTIVE, is_active=True
        )
    }

    polled = 0
    total_misses = 0
    for conn_id, media_id in targets:
        conn = conns.get(conn_id)
        if conn is None:
            continue
        try:
            result = _poll_one_media(conn, media_id, now)
            total_misses += result.get("misses", 0)
            polled += 1
        except Exception:
            logger.exception("poll_missed_comments: 실패 conn=%s media=%s", conn_id, media_id)

    if total_misses:
        logger.info("poll_missed_comments: polled=%s misses(enqueued)=%s", polled, total_misses)
    return {"targets": len(targets), "polled": polled, "misses": total_misses}


@shared_task(name="integrations.cleanup_comment_ledger")
def cleanup_comment_ledger():
    """만료된 관측/멱등 장부 정리 (Beat: 매일).

    - SeenComment: ``expires_at`` 이 지난 행(웹훅 누락 보정 앵커).
    - SpamCommentLog(status=CLEAN): 스팸 아님으로 판정돼 멱등용으로만 남긴 장부 행 — 48h 경과분.
      (스팸 행 detected/hidden/failed 는 통계·감사 위해 보존한다.)
    배치 삭제. 멱등 — 다음 실행에 남은 만료분 계속 정리.
    """
    now = timezone.now()

    deleted = 0
    while True:
        ids = list(
            SeenComment.objects.filter(expires_at__lt=now).values_list("id", flat=True)[:5000]
        )
        if not ids:
            break
        n, _ = SeenComment.objects.filter(id__in=ids).delete()
        deleted += n
        if len(ids) < 5000:
            break

    # CLEAN 스팸 로그 TTL 정리 (멱등 장부라 오래 보관 불필요)
    clean_cutoff = now - timedelta(hours=48)
    deleted_clean = 0
    while True:
        ids = list(
            SpamCommentLog.objects.filter(
                status=SpamCommentLog.Status.CLEAN, created_at__lt=clean_cutoff
            ).values_list("id", flat=True)[:5000]
        )
        if not ids:
            break
        n, _ = SpamCommentLog.objects.filter(id__in=ids).delete()
        deleted_clean += n
        if len(ids) < 5000:
            break

    if deleted or deleted_clean:
        logger.info(
            "cleanup_comment_ledger: deleted %s SeenComment, %s CLEAN spam logs",
            deleted,
            deleted_clean,
        )
    return {"deleted": deleted, "deleted_clean_spam_logs": deleted_clean}


@shared_task(name="integrations.maintain_partitions")
def maintain_partitions():
    """EventInbox 일별 파티션 유지 + (옵션)SentDMLog 배치 아카이브 (Beat: 매일).

    1) 앞으로 N일치 파티션 선생성(행 도착 전에 있어야 DEFAULT 로 안 샘),
    2) 보존일 초과 일별 파티션 DROP(즉시, WAL≈0),
    3) (옵션) SentDMLog 오래된 행 배치 아카이브 — R2 export 선행 전까지 비활성. (§15.8)
    """
    from apps.integrations import partition_maintenance as pm

    # 각 단계를 독립 격리 — ensure 가 실패해도 drop_old(디스크 회수 + DEFAULT 경고/정리)는 반드시 돌게.
    result: dict = {}
    try:
        result["ensured"] = len(pm.ensure_eventinbox_partitions())
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintain_partitions: ensure 실패: %s", exc)
        result["ensure_error"] = str(exc)
    try:
        result["dropped"] = pm.drop_old_eventinbox_partitions()
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintain_partitions: drop_old 실패: %s", exc)
        result["drop_error"] = str(exc)
    try:
        result["archived"] = pm.archive_old_sentdmlogs()
    except Exception as exc:  # noqa: BLE001
        logger.exception("maintain_partitions: archive 실패: %s", exc)
        result["archive_error"] = str(exc)
    logger.info("maintain_partitions: %s", result)
    return result


# ===== 공개 답글 + Follow-gate =====


@shared_task(bind=True, max_retries=10)
def post_public_reply(self, log_id: str, recovery: bool = False):
    """
    댓글에 공개 답글(대댓글) 게시 (v3.5 — 봇 검사 회피).

    두 가지 모드:
      - 일반(recovery=False): DM ACCEPTED 후 "DM 보내드렸어요" 성공 답글.
      - 복구(recovery=True): opening 비공개답글이 2534025(비팔로워 채널 미개설)로 확정 실패했을 때
        "숨겨진 요청함 확인·수락 후 다시 댓글" 안내 답글. 템플릿·enable 플래그·기록 필드·
        서킷브레이커 키가 분리되지만, 배치/쿨다운/페이싱(계정 단위 대댓글 예산)은 성공 답글과 공유한다.

    동작:
      1. 템플릿 목록에서 무작위로 1개 선택 — 다양성 확보
      2. 같은 IG 계정 기준 batch_size 만큼 최근에 게시했으면 batch_pause_seconds 만큼
         대기 후 재시도 — 짧은 시간 안에 대량 답글 방지 (Instagram 봇 차단 회피)
      3. 매 답글마다 5~15초 지터 적용 — 일정한 간격 패턴 회피

    Best-effort — 실패해도 DM 흐름엔 영향 없음.
    """
    try:
        log = SentDMLog.objects.select_related("campaign__ig_connection").get(id=log_id)
    except SentDMLog.DoesNotExist:
        return {"status": "not_found"}

    campaign = log.campaign
    feature = "recovery" if recovery else "public_reply"

    # 활성 여부 / 이미 게시 여부 / 템플릿 — 모드별 소스 분기
    if recovery:
        enabled = campaign.recovery_reply_enabled
        already = bool(log.recovery_reply_id)
        template = campaign.pick_recovery_reply_template()
    else:
        enabled = campaign.public_reply_enabled
        already = bool(log.public_reply_id)
        template = campaign.pick_public_reply_template()

    if not enabled:
        return {"status": "skipped", "reason": f"{feature} disabled"}
    if already:
        return {"status": "skipped", "reason": "already replied"}
    if not template:
        return {"status": "skipped", "reason": "no template content"}
    # ★ 복구 안내는 게시 직전 상태 재확인 — 예약(5~15s+배치 재시도) 사이에 재댓글 발송이
    #   성공해 RECOVERY_DELIVERED 로 승격됐다면, '못 드렸어요' 계열 안내는 이제 거짓 → 게시 취소.
    if recovery and log.status != SentDMLog.Status.RECOVERY_PENDING:
        return {"status": "skipped", "reason": f"no_longer_pending({log.status})"}

    # ★ 캠페인별 성공 공개 답글 상한 — 복구 안내(recovery)는 항상 예외(차단·집계 제외).
    #   배치 스로틀·API 호출 전에 검사해 상한 도달 건이 COUNT 쿼리/retry 슬롯을 낭비하지
    #   않게 한다. 로그는 failed 로 만들지 않는다(DM 은 이미 정상 발송, 대댓글은 부가 기능).
    if not recovery and campaign.public_reply_limit_reached():
        log.append_verification_log(
            {
                "path": "public_reply",
                "result": "limit_skipped",
                "limit": campaign.public_reply_limit,
                "posted_count": campaign.public_reply_posted_count,
            }
        )
        return {"status": "skipped", "reason": "public_reply_limit_reached"}

    ig_conn = campaign.ig_connection

    # ★ 배치 카운트 체크 — 같은 IG 계정에서 최근 N건 이상 게시했으면 일시 정지
    batch_window = max(30, campaign.public_reply_batch_pause_seconds)
    cutoff = timezone.now() - timedelta(seconds=batch_window)
    recent_replies = SentDMLog.objects.filter(
        campaign__ig_connection=ig_conn,
        public_reply_posted_at__gte=cutoff,
    ).count()

    if recent_replies >= max(1, campaign.public_reply_batch_size):
        delay = campaign.public_reply_batch_pause_seconds
        log.append_verification_log(
            {
                "path": "public_reply",
                "result": "batch_paused",
                "recent_replies": recent_replies,
                "batch_size": campaign.public_reply_batch_size,
                "delay_seconds": delay,
            }
        )
        try:
            raise self.retry(countdown=delay)
        except self.MaxRetriesExceededError:
            log.append_verification_log(
                {"path": "public_reply", "result": "abandoned_after_batch_pause"}
            )
            return {"status": "abandoned", "reason": "max retries after batch pause"}

    # (지터는 enqueue 시점에 이미 적용됨 — send_dm_task 참조)

    # 실제 답글 게시
    try:
        result = InstagramCommentService.post_reply(
            comment_id=log.comment_id,
            message=template,
            access_token=ig_conn.access_token,
        )
    except CommentReplyPermanentError as e:
        # 댓글 삭제, 7일 초과, 권한 없음, 토큰 만료, Action Block — 재시도 의미 없음
        logger.info(
            f"post_public_reply permanent error log={log_id} code={e.code}/{e.subcode}: {e.message}"
        )
        log.append_verification_log(
            {
                "path": "public_reply",
                "result": "abandoned_permanent",
                "feature": feature,  # 'public_reply' | 'recovery' — 서킷 분리용
                "error": e.message,
                "code": e.code,
                "subcode": e.subcode,
            }
        )

        # ===== Circuit Breaker (feature 별로 분리) =====
        # 같은 IG 계정에서 10분 안에 같은 feature 영구 에러 3건 이상 누적되면
        # → 그 feature 의 플래그를 계정 전체에서 자동 OFF.
        # 인스타 Action Block (code=1) 이 걸리면 계속 시도할수록 차단 기간이 연장되므로,
        # 자동 OFF 로 추가 시도를 막아 차단이 빨리 풀리게 한다.
        # ★ 복구(recovery) 답글은 별도 서킷 — 비팔로워(스팸성) 대상이라 영구에러가 잦은데,
        #   공유 서킷이면 정상 성공답글(public_reply)까지 꺼버리므로 feature 로 분리한다.
        try:
            ig_conn = log.campaign.ig_connection
            cutoff = timezone.now() - timedelta(minutes=10)
            recent_logs = SentDMLog.objects.filter(
                campaign__ig_connection=ig_conn,
                created_at__gte=cutoff,
            ).only("verification_log")
            permanent_count = sum(
                1
                for rl in recent_logs
                if any(
                    (ev or {}).get("result") == "abandoned_permanent"
                    # 레거시 엔트리(feature 키 없음)는 public_reply 로 간주
                    and (ev or {}).get("feature", "public_reply") == feature
                    for ev in (rl.verification_log or [])
                )
            )
            CB_THRESHOLD = 3
            if permanent_count >= CB_THRESHOLD:
                flag = "recovery_reply_enabled" if recovery else "public_reply_enabled"
                affected = AutoDMCampaign.objects.filter(
                    ig_connection=ig_conn,
                    **{flag: True},
                ).update(**{flag: False})
                logger.warning(
                    f"Circuit breaker ({feature}) tripped for {ig_conn.username}: "
                    f"{permanent_count} permanent errors in 10min "
                    f"→ disabled {flag} on {affected} campaign(s). "
                    f"Manual re-enable required after Meta restriction clears."
                )
                log.append_verification_log(
                    {
                        "path": "public_reply",
                        "result": "circuit_breaker_tripped",
                        "feature": feature,
                        "recent_permanent": permanent_count,
                        "affected_campaigns": affected,
                        "ig_account": ig_conn.username,
                    }
                )
        except Exception:
            logger.exception("circuit breaker check failed (non-fatal)")

        return {"status": "abandoned", "reason": "permanent", "code": e.code}
    except Exception as e:
        logger.warning(f"post_public_reply failed for log={log_id}: {e}; will retry")
        try:
            raise self.retry(exc=e, countdown=60) from e
        except self.MaxRetriesExceededError:
            log.append_verification_log(
                {"path": "public_reply", "result": "failed", "error": str(e)}
            )
            return {"status": "failed", "error": str(e)}

    reply_id = (result or {}).get("id", "")
    now = timezone.now()
    if recovery:
        # 복구 안내 답글: id 는 별도 필드. public_reply_posted_at 도 찍어 성공답글과
        # '대댓글 배치 예산(batch window)'을 공유(계정 단위 대댓글 속도 보호).
        log.recovery_reply_id = reply_id
        log.public_reply_posted_at = now
        log.save(update_fields=["recovery_reply_id", "public_reply_posted_at"])
    else:
        log.public_reply_id = reply_id
        log.public_reply_posted_at = now
        log.save(update_fields=["public_reply_id", "public_reply_posted_at"])
        # 성공 공개 답글 누계 증가 (원자적). 복구는 상한 집계 제외 → 여기서만 증가.
        campaign.increment_public_reply_posted()
    log.append_verification_log(
        {
            "path": "public_reply",
            "result": "posted",
            "feature": feature,
            "reply_id": reply_id,
            "template_used": template[:50],
        }
    )
    return {"status": "posted", "reply_id": reply_id, "feature": feature, "template": template[:50]}


@shared_task
def handle_inbound_message_for_gate(
    *,
    page_ig_user_id: str,
    sender_user_id: str,
    message_text: str,
    message_mid: str = "",
):
    """
    Inbound 메시지(사용자 → 우리)를 받았을 때 Follow-gate 통과 여부 평가.

    매칭 조건:
        - 동일 ig_user_id 의 SentDMLog 중 sender 와 같은 recipient 가
          gate_status=PENDING 이고 dm_kind=OPENING 인 가장 최근 항목
        - campaign.matches_gate_keyword(message_text) 가 True

    매칭 시:
        - opening 로그의 gate_status = PASSED 로 전환
        - reward DM 을 send_reward_dm 으로 발송 큐 등록
    """
    if not (page_ig_user_id and sender_user_id):
        return {"status": "skipped", "reason": "missing ids"}

    # ★ Self-message 가드: 비즈니스 owner 본인이 자기 자신에게 메시지를 보낸
    # 케이스. (실제 페이지 본인은 outbound 가 echo 로 처리되지만, 테스트 환경에서
    # 동일 계정 inbound 가 잘못 들어올 수 있어 안전망 추가.)
    if str(sender_user_id) == str(page_ig_user_id):
        return {"status": "skipped", "reason": "self_message"}

    candidates = (
        SentDMLog.objects.select_related("campaign__ig_connection")
        .filter(
            recipient_user_id=sender_user_id,
            gate_status=SentDMLog.GateStatus.PENDING,
            dm_kind=SentDMLog.DMKind.OPENING,
            campaign__ig_connection__external_account_id=page_ig_user_id,
        )
        .order_by("-accepted_at", "-created_at")
    )

    matched_count = 0
    for opening_log in candidates[:5]:
        campaign = opening_log.campaign
        if not campaign.follow_gate_enabled:
            continue
        if not campaign.matches_gate_keyword(message_text):
            continue

        # gate 통과 — opening 로그 상태 전이
        opening_log.gate_status = SentDMLog.GateStatus.PASSED
        opening_log.save(update_fields=["gate_status"])
        opening_log.append_verification_log(
            {
                "path": "gate",
                "result": "passed",
                "trigger_text": (message_text or "")[:60],
                "trigger_mid": message_mid,
            }
        )

        # reward DM 발송 큐 등록
        send_reward_dm.delay(str(opening_log.id))
        matched_count += 1

    return {"status": "ok", "matched": matched_count}


@shared_task(bind=True, max_retries=5)
def send_reward_dm(self, opening_log_id: str):
    """
    Follow-gate(키워드 답장) 통과 후 본 DM(reward) 을 발송 **큐에 등록**.

    v4.3 — BYPASS 수정: 이전에는 이 태스크가 send_dm_via_user_id 를 직접 호출해
    페이서/백스톱/월쿼터를 전부 우회했다(2026-07-09 감사에서 적발). 이제 형제 헬퍼
    (_enqueue_reward_dm)와 동일하게 QUEUED 로그를 만들고 send_dm_task 에 위임한다 —
    모든 발송이 단일 초크포인트(send_dm_task → _rate_defer)를 지난다.
    24h 메시징 윈도우 내에서만 가능 (사용자가 우리에게 메시지 보낸 직후라 OK).
    """
    try:
        opening = SentDMLog.objects.select_related("campaign__ig_connection").get(id=opening_log_id)
    except SentDMLog.DoesNotExist:
        return {"status": "not_found"}

    campaign = opening.campaign

    # 빠른 가드(권위 체크는 send_dm_task 진입부가 다시 수행 — 여기선 무의미한 큐잉만 회피).
    if not campaign.is_within_schedule():
        return {"status": "skipped", "reason": "outside_schedule_window"}

    if not campaign.reward_message_template:
        logger.warning(f"send_reward_dm: campaign {campaign.id} has no reward template; skip")
        return {"status": "skipped", "reason": "no reward template"}

    ig_conn = campaign.ig_connection
    if ig_conn.status != IGAccountConnection.Status.ACTIVE:
        return {"status": "failed_token", "reason": "ig connection not active"}

    # 멱등성 키 — opening 의 idempotency_key 를 시드로 sha256(64자 고정).
    # 접미사 접합(":reward")은 varchar(64) 를 초과(71자)해 DataError 를 내므로 금지 — 형제 헬퍼
    # (_enqueue_reward_dm/_enqueue_follow_retry)와 동일하게 항상 해시화한다.
    # ★ 키 유지: 과거 직접발송 시절과 같은 파생식 → 재배포 전후 중복 발송 불가.
    import hashlib

    idem = hashlib.sha256(f"reward:{opening.idempotency_key}".encode()).hexdigest()
    reward_log, created = SentDMLog.create_idempotent(
        idempotency_key=idem,
        campaign=campaign,
        # comment_id 는 비운다: reward 는 user_id(24h 윈도우) 발송이므로 _messaging_window 가
        # 24h 로 잡히게 한다(comment_id 를 물려받으면 7일로 오판). 라우팅은 parent_log 만으로도
        # user_id 경로가 보장되지만 윈도우 계산까지 정확히 맞춘다 (_enqueue_reward_dm 과 동일).
        comment_id="",
        comment_text="",
        recipient_user_id=opening.recipient_user_id,
        recipient_username=opening.recipient_username,
        message_sent=campaign.reward_message_template,
        status=SentDMLog.Status.QUEUED,
        webhook_payload=opening.webhook_payload,
        dm_kind=SentDMLog.DMKind.REWARD,
        gate_status=SentDMLog.GateStatus.NONE,
        parent_log=opening,
    )
    if not created:
        return {
            "status": "duplicate",
            "reward_log_id": str(reward_log.id) if reward_log else None,
        }

    send_dm_task.delay(str(reward_log.id))
    return {
        "status": "enqueued",
        "reward_log_id": str(reward_log.id),
    }


# ═════════════════════════════════════════════════════════════
# 실패 DM 복구 (recovery): 확정 실패 → 안내 대댓글 → 재댓글이 일반 경로로 재발송
# ═════════════════════════════════════════════════════════════
# v1 의 인바운드 DM 감지 태스크(process_inbound_recovery_dm)는 폐기됐다(2026-07-14).
# 'DM 먼저 받기'를 꺼둔 사용자에게 동작하지 않았고, 재발송 트리거는 재댓글(일반
# 댓글→DM 경로)로 대체됐다. 성공 정산은 _flip_recovery_on_success, 만료는 아래 스윕.


def _maybe_route_recovery_recomment(
    *,
    page_ig_user_id: str,
    from_user_id: str,
    from_username: str,
    comment_id: str,
    comment_text: str,
    media_id: str,
    source: str,
) -> int:
    """RECOVERY_PENDING 보유 사용자의 재댓글을 복구 재발송으로 라우팅.

    일반 캠페인 매칭이 놓치는 두 재댓글 형태를 구제한다(적대적 리뷰 발견):
      1. **스레드 답글** — 안내가 사용자 댓글의 답글로 달리므로 사용자의 가장 자연스러운
         응답은 그 스레드 답글인데, 일반 경로는 parent_id 댓글을 무조건 스킵한다.
      2. **키워드 불일치 재댓글** — "수락했어요" 처럼 캠페인 키워드가 없는 재댓글.
    사용자는 캠페인 트리거가 아니라 우리 안내에 응답한 것이므로 키워드 필터를 적용하지
    않는다. RECOVERY_PENDING 이 있는 사용자에게만 동작(오남용 불가 — 이미 정당하게
    트리거됐던 사용자다). 매칭은 _recipient_match_q(IGSID/username 키 이원화 대응).

    **게시물 스코핑(media_id)**: 재댓글이 달린 media 가 캠페인의 매체 범위와 일치해야
    라우팅한다(campaign.matches_media). 같은 계정의 **다른 게시물**에 단 댓글로 복구가
    발동하는 과대 트리거 방지(2026-07-14 스코핑 조임). SPECIFIC_MEDIA 는 캠페인이 명시한
    게시물만, ANY_MEDIA 는 캠페인 의미대로 모든 게시물 통과. media 미상(빈 값)이면
    SPECIFIC_MEDIA 는 fail-closed — 다른 게시물일 가능성을 배제할 수 없으므로.

    발송은 기존 _enqueue_send_dm 초크포인트를 그대로 통과(자기가드/쿨다운/멱등) —
    일반 매칭과 같은 캠페인이 겹치면 idempotency_key(comment_id 단위)가 중복을 흡수한다.
    반환값 = enqueue 된 캠페인 수.
    """
    page = str(page_ig_user_id or "")
    if not page or not comment_id:
        return 0
    # self 방어 (poller 는 from.id 가 없을 수 있어 username 도 호출부 가드에 의존하지만,
    # IGSID 가 오는 웹훅 경로는 여기서도 확실히 끊는다)
    if from_user_id and str(from_user_id) == page:
        return 0
    match_q = _recipient_match_q(user_id=from_user_id, username=from_username)
    if match_q is None:
        return 0

    pendings = (
        SentDMLog.objects.select_related("campaign__ig_connection")
        .filter(
            match_q,
            status=SentDMLog.Status.RECOVERY_PENDING,
            parent_log__isnull=True,
            campaign__ig_connection__external_account_id=page,
        )
        .order_by("-recovery_pending_at")
    )

    from apps.billing.subscription_utils import owner_has_feature

    now = timezone.now()
    routed = 0
    seen_campaigns: set = set()
    for pending in pendings[:10]:  # 방어적 상한 (정상적으론 수신자당 소수)
        campaign = pending.campaign
        if campaign.id in seen_campaigns:
            continue
        seen_campaigns.add(campaign.id)
        # TTL 지난 건 재발송하지 않음 — 만료 정산은 스윕이 담당(중복 정산 방지).
        ttl = campaign.recovery_ttl_seconds or 604800
        if (
            pending.recovery_pending_at
            and (now - pending.recovery_pending_at).total_seconds() >= ttl
        ):
            continue
        if campaign.status != AutoDMCampaign.Status.ACTIVE or not campaign.recovery_reply_enabled:
            continue
        # 게시물 스코핑: 캠페인이 명시한 media 의 재댓글만 (docstring 참고)
        if not campaign.matches_media(media_id):
            continue
        # 프로 전용 게이트 재확인 (진입 후 다운그레이드 방어, fail-closed)
        try:
            if not owner_has_feature(campaign.ig_connection.workspace, "dm_recovery"):
                continue
        except Exception:  # noqa: BLE001 - 판단 불가 시 미보유 취급
            continue
        res = _enqueue_send_dm(
            campaign=campaign,
            comment_id=comment_id,
            comment_text=comment_text,
            from_user_id=from_user_id or from_username,
            from_username=from_username,
            webhook_payload={
                "source": source,
                "recovery_recomment": True,
                "pending_log_id": str(pending.id),
                "comment_id": comment_id,
            },
        )
        if res.get("status") == "enqueued":
            pending.append_verification_log(
                {
                    "path": "recovery",
                    "result": "recomment_routed",
                    "new_log_id": res.get("log_id"),
                    "source": source,
                }
            )
            routed += 1
    return routed


@shared_task(name="integrations.poll_recovery_recomments")
def poll_recovery_recomments():
    """RECOVERY_PENDING 스레드의 답글을 재조회해 웹훅 유실분 재댓글을 보정 라우팅 (hourly).

    복구 안내는 사용자 댓글의 '답글'로 달리므로 가장 자연스러운 응답은 그 스레드 답글인데,
    comments 웹훅이 유실되면(Meta 구독 auto-disable 전례) 이를 영영 못 본다 —
    미디어 폴링(poll_missed_comments)의 comments edge 는 문서상 top-level only 라
    답글 관측이 보장되지 않고(섞여 오는 건 undocumented 실측), ANY_MEDIA 캠페인은
    아예 미디어 폴링 대상도 아니다. 여기서는 안내 대댓글이 **실제 게시된**
    RECOVERY_PENDING 의 원 댓글(comment_id = 스레드 루트)의 replies edge 를 직접
    조회한다. IG 답글은 2단계 평탄화라 안내 댓글에 단 답글도 루트의 replies 로 온다.

    중복 안전: 라우팅이 기존 _enqueue_send_dm 초크포인트를 통과하므로 comment_id 단위
    idempotency_key + 수신자 쿨다운이 웹훅/반복 폴링과의 중복을 흡수한다.
    ⚠️ SeenComment 에는 기록하지 않는다 — 답글을 기록하면 _poll_one_media 의 anchor
    판정(created=False → 페이지네이션 중단)을 오염시켜 진짜 누락 top-level 댓글을 숨긴다.
    """
    if not getattr(settings, "RECOVERY_RECOMMENT_POLL_ENABLED", True):
        return {"enabled": False}

    now = timezone.now()
    cap = getattr(settings, "RECOVERY_RECOMMENT_POLL_MAX_THREADS", 200)
    # 후보: 안내가 실제 게시된(recovery_reply_id 有) 살아있는 pending 만.
    # 캠페인/연동 게이트는 여기서 1차로 좁히고, TTL·프로 게이트는 라우팅과 동일하게
    # 개별 재판정한다. 후보 상한(cap)이 곧 스레드(=API 호출) 상한의 상계.
    # ⚠️ 정렬이 oldest-first 결정적이라, 동시에 살아있는 pending 이 cap 을 넘으면 최신
    #    pending 은 오래된 것들이 성공/TTL 만료로 빠질 때까지 폴링에서 밀릴 수 있다
    #    (웹훅 정상 시 무해 — 이 태스크가 필요한 웹훅 유실 대량 상황에서만 발현, minor).
    #    cap 도달 시 아래에서 경고해 운영자가 RECOVERY_RECOMMENT_POLL_MAX_THREADS 를 올릴 수 있게 한다.
    candidates = list(
        SentDMLog.objects.select_related("campaign__ig_connection")
        .filter(
            status=SentDMLog.Status.RECOVERY_PENDING,
            parent_log__isnull=True,
            recovery_pending_at__isnull=False,
            campaign__status=AutoDMCampaign.Status.ACTIVE,
            campaign__recovery_reply_enabled=True,
            campaign__ig_connection__status=IGAccountConnection.Status.ACTIVE,
            campaign__ig_connection__is_active=True,
        )
        .exclude(recovery_reply_id="")
        .exclude(comment_id="")
        .order_by("recovery_pending_at")[:cap]
    )
    if len(candidates) >= cap:
        logger.warning(
            "poll_recovery_recomments: 후보가 cap(%s)에 도달 — 최신 RECOVERY_PENDING 이 "
            "이번 런에서 폴링되지 않을 수 있음(웹훅 유실 대량 상황). 만료 스윕 백로그 확인 + "
            "RECOVERY_RECOMMENT_POLL_MAX_THREADS 상향 검토.",
            cap,
        )

    # (connection, 루트 comment_id) 스레드 단위로 그룹핑 — 같은 루트를 공유하는
    # 복수 캠페인 pending 이 있어도 replies fetch 는 스레드당 1회.
    threads: dict = {}
    for log in candidates:
        campaign = log.campaign
        ttl = campaign.recovery_ttl_seconds or 604800
        if (now - log.recovery_pending_at).total_seconds() >= ttl:
            continue  # 만료 정산은 handle_recovery_pending_expiry 담당
        threads.setdefault((campaign.ig_connection_id, log.comment_id), []).append(log)

    fetched = 0
    routed = 0
    for (_conn_id, root_comment_id), pendings in threads.items():
        conn = pendings[0].campaign.ig_connection
        own_igsid = str(conn.external_account_id or "")
        own_username = (conn.username or "").strip().lower()
        # 우리 안내 대댓글 id 집합 — 캠페인마다 각자 안내를 달 수 있으므로 집합으로.
        guide_reply_ids = {p.recovery_reply_id for p in pendings if p.recovery_reply_id}
        # 스레드의 media = 원 게시물. 스코핑 라우팅에 전달 (SPECIFIC_MEDIA 캠페인의
        # media_id — 답글은 원 게시물 위이므로 이 값이 곧 답글의 media 다).
        thread_media_id = next((p.campaign.media_id for p in pendings if p.campaign.media_id), "")
        floor_ts = min(p.recovery_pending_at for p in pendings)

        try:
            resp = InstagramMediaService.list_comment_replies(root_comment_id, conn.access_token)
        except Exception:  # noqa: BLE001 - best-effort 폴링, 스레드 단위 격리
            logger.exception("poll_recovery_recomments: replies 조회 실패 root=%s", root_comment_id)
            continue
        fetched += 1
        if resp.get("paging_after"):
            logger.warning(
                "poll_recovery_recomments: replies >1페이지 — 첫 페이지만 처리 (root=%s)",
                root_comment_id,
            )
        for r in resp.get("data") or []:
            rid = r.get("id")
            if not rid or rid in guide_reply_ids:
                continue
            r_from_id = str((r.get("from") or {}).get("id") or "")
            r_username = str(r.get("username") or "")
            # self 답글(우리 안내 등) 스킵 — _poll_one_media 와 동일한 이중 비교
            if (r_from_id and r_from_id == own_igsid) or (
                own_username and r_username.strip().lower() == own_username
            ):
                continue
            if not r_from_id and not r_username:
                continue  # 신원 없음 — recipient 매칭 불가
            # 안내(=pending 진입) 이전 답글은 우리 안내에 대한 응답이 아님.
            # timestamp 파싱 실패 시엔 통과(fail-open) — 라우팅 쪽 가드가 재판정.
            r_ts = _parse_iso_timestamp(r.get("timestamp"))
            if r_ts is not None and r_ts < floor_ts:
                continue
            routed += _maybe_route_recovery_recomment(
                page_ig_user_id=own_igsid,
                from_user_id=r_from_id,
                from_username=r_username,
                comment_id=rid,
                comment_text=r.get("text") or "",
                media_id=thread_media_id,
                source="recovery_poll",
            )
    return {
        "candidates": len(candidates),
        "threads": len(threads),
        "fetched": fetched,
        "routed": routed,
    }


@shared_task(name="integrations.handle_recovery_pending_expiry")
def handle_recovery_pending_expiry():
    """RECOVERY_PENDING 이 캠페인 recovery_ttl_seconds 를 넘기면 RECOVERY_EXPIRED 로 만료.

    좀비 로우 방지 스윕(hourly). ttl 은 캠페인별이라 후보를 좁힌 뒤 개별 판정한다.
    만료는 '완전 실패' 정산 → opening(부모) 은 increment_failed 로 집계.
    """
    now = timezone.now()
    candidates = list(
        SentDMLog.objects.select_related("campaign")
        .filter(
            status=SentDMLog.Status.RECOVERY_PENDING,
            recovery_pending_at__isnull=False,
        )
        .order_by("recovery_pending_at")[:2000]
    )
    expired = 0
    for log in candidates:
        campaign = log.campaign
        ttl = (campaign.recovery_ttl_seconds if campaign else 0) or 604800
        if (now - log.recovery_pending_at).total_seconds() < ttl:
            continue
        log.status = SentDMLog.Status.RECOVERY_EXPIRED
        log.save(update_fields=["status"])
        log.append_verification_log({"path": "recovery", "result": "expired"})
        if log.parent_log_id is None and campaign:
            campaign.increment_failed()
        expired += 1
    return {"scanned": len(candidates), "expired": expired}


# ═════════════════════════════════════════════════════════════
# IG Long-lived Token 자동 갱신 (KST 09:00 daily)
# ═════════════════════════════════════════════════════════════


@shared_task(bind=True, max_retries=1)
def refresh_ig_tokens_pending_expiry(self):
    """만료까지 14일 미만 남은 ACTIVE IG 연동의 long-lived token 을 갱신.

    Meta 정책: ig_refresh_token 호출 시 새로 60일짜리 토큰 발급 (권한 그대로).
    활성 사용자라면 사실상 영구 유지 — 60일 이상 미사용 계정만 자연 만료.
    실행 후 Telegram 으로 성공/실패 요약 push (best-effort).
    """
    from apps.core.telegram import send_telegram_notification

    cutoff = timezone.now() + timedelta(days=14)
    candidates = list(
        IGAccountConnection.objects.filter(
            status=IGAccountConnection.Status.ACTIVE,
            token_expires_at__isnull=False,
            token_expires_at__lte=cutoff,
        )
    )

    succeeded: list[dict] = []
    failed: list[dict] = []

    for conn in candidates:
        try:
            result = InstagramOAuthService.refresh_long_lived_token(conn.access_token)
            new_token = result.get("access_token")
            if not new_token:
                raise ValueError(f"refresh response missing access_token: {result}")
            expires_in = int(result.get("expires_in", 60 * 24 * 3600))  # default 60일

            conn.access_token = new_token  # EncryptedTextField 자동 암호화
            conn.token_expires_at = timezone.now() + timedelta(seconds=expires_in)
            conn.last_verified_at = timezone.now()
            conn.error_message = ""
            conn.save(
                update_fields=[
                    "_encrypted_access_token",
                    "token_expires_at",
                    "last_verified_at",
                    "error_message",
                    "updated_at",
                ]
            )
            succeeded.append(
                {
                    "id": str(conn.id),
                    "username": conn.username or "(unknown)",
                    "new_expires_at": conn.token_expires_at.strftime("%Y-%m-%d"),
                }
            )
            logger.info(
                "IG token refreshed: conn=%s user=@%s new_exp=%s",
                conn.id,
                conn.username,
                conn.token_expires_at,
            )
            # ★ P2: 토큰 복구 직후, 토큰 만료로 종결된(FAILED_TOKEN) 건을 윈도우 내라면 되살림.
            # 배포·일시 토큰오류로 죽었던 발송이 토큰 복구와 동시에 자동 재발송된다.
            revive_failed_token_logs.delay(str(conn.id))
        except Exception as e:
            # H-9: 예외 문자열에 토큰 URL 이 섞일 수 있으므로 로그·DB 저장 전 마스킹.
            safe_err = scrub_secrets(str(e))
            logger.exception("IG token refresh failed: conn=%s err=%s", conn.id, safe_err)
            try:
                conn.error_message = f"refresh failed: {safe_err}"[:500]
                conn.save(update_fields=["error_message", "updated_at"])
            except Exception:
                pass
            failed.append(
                {
                    "id": str(conn.id),
                    "username": conn.username or "(unknown)",
                    "error": safe_err[:200],
                }
            )

    # 6h 주기로 자주 도므로, 처리할 후보가 없으면 Telegram 알림을 생략(노이즈 방지).
    # 후보가 있었던 실행(성공/실패 포함)만 요약 push.
    if candidates:
        message = _build_token_refresh_summary(
            checked=len(candidates), succeeded=succeeded, failed=failed
        )
        send_telegram_notification(message)

    return {
        "checked": len(candidates),
        "succeeded": len(succeeded),
        "failed": len(failed),
    }


@shared_task
def revive_failed_token_logs(ig_connection_id: str):
    """토큰 복구(갱신/재연동) 후, 해당 연결의 FAILED_TOKEN 로그를 윈도우 내라면 되살림 (P2).

    제자리 되살림(SentDMLog.revive)이라 같은 idempotency_key 를 재사용 → 중복 발송 불가.
    윈도우(comment 7d / user_id 24h)를 넘긴 건은 revive 가 알아서 거부한다.
    """
    revived = 0
    scanned = 0
    qs = (
        SentDMLog.objects.filter(
            campaign__ig_connection_id=ig_connection_id,
            status=SentDMLog.Status.FAILED_TOKEN,
        )
        .select_related("campaign")
        .order_by("created_at")[:500]
    )
    for log in qs:
        scanned += 1
        try:
            if log.revive(reason="token_recovered"):
                revived += 1
        except Exception:
            logger.exception("revive_failed_token_logs: revive 실패 log=%s", log.id)
    if revived:
        logger.info(
            "revive_failed_token_logs: revived %s/%s logs for conn=%s",
            revived,
            scanned,
            ig_connection_id,
        )
    return {"revived": revived, "scanned": scanned}


def _build_token_refresh_summary(*, checked: int, succeeded: list, failed: list) -> str:
    """Telegram Markdown 요약 메시지 빌드."""
    now_kst = timezone.localtime().strftime("%Y-%m-%d %H:%M KST")
    lines = [
        f"🔄 *IG Token Refresh* — {now_kst}",
        "",
        f"대상: *{checked}*개 (만료 D-14 이내)",
        f"✅ 성공: *{len(succeeded)}*",
        f"❌ 실패: *{len(failed)}*",
    ]
    if succeeded:
        lines.append("")
        lines.append("*Refreshed*")
        for s in succeeded[:20]:
            lines.append(f"• @{s['username']} → 다음 만료 `{s['new_expires_at']}`")
        if len(succeeded) > 20:
            lines.append(f"… 외 {len(succeeded) - 20}개")
    if failed:
        lines.append("")
        lines.append("*Failed* (재OAuth 안내 필요)")
        for f in failed[:20]:
            lines.append(f"• @{f['username']} — `{f['error']}`")
        if len(failed) > 20:
            lines.append(f"… 외 {len(failed) - 20}개")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════
# Follow-gate (v3.8): postback 수신 → IG Profile API 로 팔로우 확인 → 분기
# ═════════════════════════════════════════════════════════════

_GATE_CHAIN_MAX_HOPS = 10


def _gate_chain(log: SentDMLog) -> list[SentDMLog]:
    """클릭된 로그부터 parent_log 를 따라 플로우 루트까지의 체인 (clicked → … → root).

    재안내(retry) DM 은 자신의 id 로 새 quick_reply 버튼을 달고 나가므로, postback 이
    참조하는 로그는 루트 opening 이 아니라 체인 중간 노드일 수 있다. 게이트 통과 마킹과
    reward 멱등 판정을 클릭된 노드 기준으로만 하면 루트가 PENDING 으로 남아
    (= 기본 로그 API 의 follow_passed / 전환 통계가 영구 '미전환') 루트 버튼 재클릭 시
    reward 중복 발송 구멍도 생긴다 — 반드시 체인 전체 기준으로 판정한다.
    """
    chain = [log]
    cur = log
    for _ in range(_GATE_CHAIN_MAX_HOPS):
        if cur.parent_log_id is None:
            break
        cur = cur.parent_log
        chain.append(cur)
    return chain


def _gate_flow_ids(root: SentDMLog) -> list:
    """루트 opening 에서 시작하는 플로우 전체(루트 + 모든 자손 로그)의 pk 목록.

    reward/재안내는 '클릭된 노드'에 붙으므로 루트의 직계 자식이 아닐 수 있다 —
    reward 중복 존재 검사와 재안내 스로틀은 상향 체인이 아니라 이 플로우 전체로 한다.
    """
    ids = [root.pk]
    frontier = [root.pk]
    for _ in range(_GATE_CHAIN_MAX_HOPS):
        children = list(
            SentDMLog.objects.filter(parent_log_id__in=frontier).values_list("pk", flat=True)
        )
        if not children:
            break
        ids.extend(children)
        frontier = children
    return ids


@shared_task(bind=True, max_retries=3)
def process_follow_gate_postback(
    self, opening_log_id: str, igsid: str, recipient_account_id: str = ""
):
    """
    사용자가 opening DM 의 'follow_check' quick_reply 버튼을 눌렀을 때 호출.

    흐름:
        1) opening SentDMLog 조회 + 게이트 상태 검증 (멱등성)
        2) IG Profile API: is_user_follow_business 호출
        3) True  → reward DM 발송 (새 child SentDMLog, dm_kind=REWARD, parent_log 연결)
                   opening log 의 gate_status 를 PASSED 로 마킹
        4) False → 재안내 메시지 발송 (새 child SentDMLog, dm_kind=STANDALONE,
                   parent_log 연결, quick_reply 재첨부) — opening 은 PENDING 유지

    멱등성/스팸 방지:
        - opening log 가 이미 PASSED 면 즉시 skip
        - 같은 (campaign, igsid) 30초 쿨다운: 사용자가 미친 듯이 눌러도 한 번만 처리
    """
    try:
        opening = SentDMLog.objects.select_related("campaign", "campaign__ig_connection").get(
            id=opening_log_id
        )
    except SentDMLog.DoesNotExist:
        logger.warning("follow-gate: opening log %s not found", opening_log_id)
        return {"status": "not_found"}

    campaign = opening.campaign
    ig_conn = campaign.ig_connection

    # ★ 이 postback 이 opening 을 보낸 '바로 그 계정' 에 도착한 것인지 검증.
    #    멀티계정 환경에서 동일 payload("fg:{opening_id}") postback 이 다른 연결계정에도 도달하면,
    #    엉뚱한 igsid 로 reward 를 보내고 게이트를 선점(PASSED)해 정작 수신자는 reward 를 못 받는다.
    #    (recipient_account_id 미전달 시 — 구버전 큐 태스크 — 는 검사 생략해 하위호환.)
    if recipient_account_id and recipient_account_id != (ig_conn.external_account_id or ""):
        return {
            "status": "skipped",
            "reason": "account_mismatch",
            "opening_log_id": str(opening.id),
            "recipient_account_id": recipient_account_id,
        }

    # 이미 게이트 통과한 opening 이면 추가 처리 안 함
    if opening.gate_status == SentDMLog.GateStatus.PASSED:
        return {"status": "already_passed", "opening_log_id": str(opening.id)}

    # 게이트 미사용 캠페인의 log 가 잘못 라우팅된 경우
    if opening.dm_kind != SentDMLog.DMKind.OPENING:
        return {"status": "skipped", "reason": "not_opening_dm"}

    # 재안내 버튼 postback 은 체인 중간 노드를 참조한다 — 루트가 이미 통과한 플로우면 skip
    # (재안내로 통과한 뒤 원래 opening 버튼을 다시 누르는 경우의 reward 중복 방지).
    chain = _gate_chain(opening)
    if chain[-1].gate_status == SentDMLog.GateStatus.PASSED:
        return {"status": "already_passed", "opening_log_id": str(opening.id)}

    # 30초 쿨다운 — 사용자가 버튼을 연타해도 한 번만 IG API 호출
    cooldown_cutoff = timezone.now() - timedelta(seconds=30)
    recent_followup = SentDMLog.objects.filter(
        parent_log=opening,
        created_at__gte=cooldown_cutoff,
    ).exists()
    if recent_followup:
        return {"status": "skipped", "reason": "cooldown_30s"}

    if ig_conn.status != IGAccountConnection.Status.ACTIVE:
        return {"status": "skipped", "reason": "ig_not_active"}

    # button-only 모드: 팔로우 검증을 건너뛰고 즉시 reward 발송.
    # 위의 PASSED 멱등성(:gate_status)/OPENING dm_kind/30s 쿨다운/ig_conn ACTIVE 가드를
    # 모두 통과한 뒤이므로, gate_verify_follow=false 면 check_user_follow_business
    # 호출 없이 바로 reward 큐에 넣는다 (연타·중복 방어는 위 가드가 동일하게 적용됨).
    if not campaign.gate_verify_follow:
        return _enqueue_reward_dm(opening=opening, igsid=igsid, chain=chain)

    # IG Profile API 호출
    try:
        is_follow = InstagramMessagingService.check_user_follow_business(
            igsid=igsid,
            access_token=ig_conn.access_token,
        )
    except DMSendError as e:
        cls = exception_to_classification(e)
        if cls.retriable and self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=30) from e
        logger.warning("follow-gate: profile check failed (terminal) log=%s err=%s", opening.id, e)
        return {"status": "failed", "reason": str(e)}

    # 판정 근거를 영구 기록 — "팔로우 했는데 미통과" 분쟁 시 Meta 가 False 를 준 것인지
    # 필드 누락(None → 보수적 미통과)인지 사후 구분이 불가능했던 관측성 갭 보완.
    opening.append_verification_log({"path": "follow_check", "igsid": igsid, "result": is_follow})
    logger.info(
        "follow-gate: check campaign=%s log=%s igsid=%s is_follow=%r",
        campaign.id,
        opening.id,
        igsid,
        is_follow,
    )

    follow_passed = bool(is_follow)
    if is_follow is None:
        # Meta 가 필드를 안 내려준 경우 — 권한/잘못된 IGSID. 보수적으로 미통과 처리.
        follow_passed = False

    if follow_passed:
        return _enqueue_reward_dm(opening=opening, igsid=igsid, chain=chain)
    return _enqueue_follow_retry(opening=opening, igsid=igsid, chain=chain)


def _enqueue_reward_dm(
    *, opening: SentDMLog, igsid: str, chain: list[SentDMLog] | None = None
) -> dict:
    """Gate 통과 → reward DM 발송 큐에 enqueue. 체인 전체(루트 포함)를 PASSED 로 마킹."""
    campaign = opening.campaign
    chain = chain or _gate_chain(opening)
    root = chain[-1]
    flow_ids = _gate_flow_ids(root)

    def _mark_flow_passed():
        # 루트만 PENDING 으로 남으면 기본 로그 API 의 follow_passed / 전환 통계가
        # 영구 '미전환' 으로 잘못 표시된다 — 클릭된 노드가 아니라 플로우의
        # opening 계열(루트+재안내) 전체를 마킹.
        SentDMLog.objects.filter(pk__in=flow_ids, dm_kind=SentDMLog.DMKind.OPENING).update(
            gate_status=SentDMLog.GateStatus.PASSED
        )

    reward_body = (campaign.reward_message_template or "").strip()
    if not reward_body:
        # 정책상 reward 비어있으면 게이트도 못 켜지지만 안전망
        _mark_flow_passed()
        return {"status": "passed_no_reward", "opening_log_id": str(opening.id)}

    # 플로우 단위 중복 방어 1: 플로우 어느 노드에든 이미 reward 자식이 있으면 재발송 금지.
    # (기존 데이터는 reward 멱등키가 '클릭된 노드' 기준이라, 아래 루트 기준 키만으로는
    #  재안내로 통과한 과거 플로우의 reward 를 못 잡는다 — 루트 버튼 재클릭 시 중복 구멍.
    #  reward 는 재안내 노드에 붙어 있을 수 있어 상향 체인이 아닌 플로우 전체로 검사.)
    existing = SentDMLog.objects.filter(
        dm_kind=SentDMLog.DMKind.REWARD, parent_log_id__in=flow_ids
    ).first()
    if existing is not None:
        _mark_flow_passed()
        return {
            "status": "duplicate_reward",
            "opening_log_id": str(opening.id),
            "reward_log_id": str(existing.id),
        }

    # 플로우 단위 중복 방어 2: 멱등키를 클릭된 노드가 아닌 '루트' 기준으로 — 체인의
    # 서로 다른 버튼(원 opening / 재안내)을 눌러도 같은 키 → DB UNIQUE 로 한 번만 발송.
    # (재안내 없는 플로우는 루트 == 클릭된 노드라 기존 키 체계와 동일.)
    import hashlib

    key_raw = f"reward:{campaign.id}:{root.id}"
    idem = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()

    reward_log, created = SentDMLog.create_idempotent(
        idempotency_key=idem,
        campaign=campaign,
        comment_id="",  # reward 는 24h 윈도우 내 user_id 발송
        comment_text="",
        recipient_user_id=igsid,
        recipient_username=opening.recipient_username,
        message_sent=reward_body,
        status=SentDMLog.Status.QUEUED,
        dm_kind=SentDMLog.DMKind.REWARD,
        gate_status=SentDMLog.GateStatus.PASSED,
        parent_log=opening,
    )
    if not created:
        return {
            "status": "duplicate_reward",
            "opening_log_id": str(opening.id),
            "reward_log_id": str(reward_log.id) if reward_log else None,
        }
    # 플로우 전체 PASSED 마킹 (reward dedup 이 이미 보장되므로 분리해도 무손실·멱등)
    _mark_flow_passed()

    send_dm_task.delay(str(reward_log.id))
    return {
        "status": "reward_enqueued",
        "opening_log_id": str(opening.id),
        "reward_log_id": str(reward_log.id),
    }


def _enqueue_follow_retry(
    *, opening: SentDMLog, igsid: str, chain: list[SentDMLog] | None = None
) -> dict:
    """Gate 미통과 → 재안내 메시지 + 같은 quick_reply 재첨부.

    매 재시도마다 새 SentDMLog 가 생기지만, parent_log 로 opening 에 묶인다.
    opening 의 gate_status 는 PENDING 유지 (여전히 통과 대기).
    재시도 로그도 OPENING + PENDING 으로 만들어 send_dm_task 가 quick_reply 를 첨부하게 한다.
    """
    campaign = opening.campaign
    chain = chain or _gate_chain(opening)
    flow_ids = _gate_flow_ids(chain[-1])

    # 플로우 전체 30초 재안내 스로틀 — 재안내마다 자기 id 의 새 버튼이 생기므로
    # postback 핸들러의 '클릭된 노드의 자식' 쿨다운만으로는 최신 재안내 버튼 연타를
    # 못 막는다(새 노드는 자식이 없음). 통과(reward) 경로는 스로틀하지 않는다.
    cooldown_cutoff = timezone.now() - timedelta(seconds=30)
    recent_retry = SentDMLog.objects.filter(
        parent_log_id__in=flow_ids,
        dm_kind=SentDMLog.DMKind.OPENING,
        created_at__gte=cooldown_cutoff,
    ).exists()
    if recent_retry:
        return {
            "status": "skipped",
            "reason": "retry_cooldown",
            "opening_log_id": str(opening.id),
        }

    retry_body = campaign.get_follow_gate_retry_message()

    # 멱등성 키: 같은 opening 의 재시도들은 시각까지 포함해 매번 다르게.
    # (사용자가 의도적으로 여러 번 누르는 시나리오 = 별도 발송)
    import hashlib

    key_raw = f"retry:{campaign.id}:{opening.id}:{timezone.now().timestamp():.0f}"
    idem = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()

    retry_log, created = SentDMLog.create_idempotent(
        idempotency_key=idem,
        campaign=campaign,
        comment_id="",  # 재안내도 user_id 기반 (24h 윈도우 사용자가 방금 상호작용함)
        comment_text="",
        recipient_user_id=igsid,
        recipient_username=opening.recipient_username,
        message_sent=retry_body,
        status=SentDMLog.Status.QUEUED,
        dm_kind=SentDMLog.DMKind.OPENING,  # quick_reply 다시 붙도록
        gate_status=SentDMLog.GateStatus.PENDING,
        parent_log=opening,
    )
    if not created:
        return {
            "status": "duplicate_retry",
            "opening_log_id": str(opening.id),
            "retry_log_id": str(retry_log.id) if retry_log else None,
        }

    send_dm_task.delay(str(retry_log.id))
    return {
        "status": "retry_enqueued",
        "opening_log_id": str(opening.id),
        "retry_log_id": str(retry_log.id),
    }


# ============================================================================
# 프로필 사진 캐싱 (R2/로컬 default_storage 에 사본 보관)
# ============================================================================


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def sync_ig_profile_picture(self, connection_id: str) -> dict:
    """IG 프로필 사진을 최신화하여 R2/로컬 default_storage 에 캐싱.

    동작:
        1) IGAccountConnection 조회 (status=ACTIVE 만)
        2) IG /me API 호출 → 최신 profile_picture_url + name + username 획득
        3) source URL 이 기존과 같으면 synced_at 만 갱신 후 종료 (dedup)
        4) 다르면 fetch_and_store_profile_image() 로 다운로드/정제/저장
        5) 4개 필드 + name + username 갱신
        6) 401/403 → connection.mark_as_error + status 표시

    Returns:
        {"status": "updated"|"unchanged"|"skipped"|"failed", "connection_id": ..., ...}
    """
    from .models import IGAccountConnection
    from .profile_image import ProfileImageFetchError, fetch_and_store_profile_image
    from .services import InstagramOAuthService, MockInstagramProvider

    try:
        conn = IGAccountConnection.objects.get(id=connection_id)
    except IGAccountConnection.DoesNotExist:
        logger.warning("sync_ig_profile_picture: connection not found id=%s", connection_id)
        return {
            "status": "skipped",
            "reason": "connection_not_found",
            "connection_id": connection_id,
        }

    if conn.status != IGAccountConnection.Status.ACTIVE:
        logger.info(
            "sync_ig_profile_picture: skip non-active conn=%s status=%s", conn.id, conn.status
        )
        return {
            "status": "skipped",
            "reason": f"status_{conn.status}",
            "connection_id": str(conn.id),
        }

    token = conn.access_token
    if not token:
        logger.warning("sync_ig_profile_picture: no access_token conn=%s", conn.id)
        return {"status": "skipped", "reason": "no_token", "connection_id": str(conn.id)}

    # Mock 토큰이면 mock account info 사용
    try:
        if MockInstagramProvider.is_mock_token(token):
            account_info = MockInstagramProvider.get_mock_account_info(token)
        else:
            account_info = InstagramOAuthService.get_account_info(token)
    except Exception as e:  # noqa: BLE001
        # 401/403 → 토큰 만료/회수 가능성 → status 갱신
        msg = str(e)
        if "401" in msg or "403" in msg or "OAuthException" in msg:
            conn.mark_as_error(f"profile sync token error: {msg[:200]}")
        logger.warning(
            "sync_ig_profile_picture: get_account_info failed conn=%s err=%s", conn.id, e
        )
        try:
            self.retry(exc=e)
        except self.MaxRetriesExceededError:
            pass
        return {
            "status": "failed",
            "reason": "get_account_info_failed",
            "connection_id": str(conn.id),
        }

    remote_url = (account_info.get("profile_picture_url") or "").strip()
    new_name = (account_info.get("name") or "").strip()
    new_username = (account_info.get("username") or "").strip()

    # name/username 은 항상 최신화 (사진 변화와 무관)
    name_changed = bool(new_name) and new_name != conn.name
    username_changed = bool(new_username) and new_username != conn.username

    # 소스 URL 동일하면 다운로드 생략
    if remote_url and remote_url == conn.profile_picture_source_url and conn.profile_picture_url:
        update_fields = ["profile_picture_synced_at", "updated_at"]
        conn.profile_picture_synced_at = timezone.now()
        if name_changed:
            conn.name = new_name
            update_fields.append("name")
        if username_changed:
            conn.username = new_username
            update_fields.append("username")
        conn.save(update_fields=update_fields)
        return {
            "status": "unchanged",
            "connection_id": str(conn.id),
            "profile_picture_url": conn.profile_picture_url,
        }

    # 소스 URL 없으면 (mock 등) 사진은 비워두고 메타만 갱신
    if not remote_url:
        update_fields = ["profile_picture_synced_at", "updated_at"]
        conn.profile_picture_synced_at = timezone.now()
        if name_changed:
            conn.name = new_name
            update_fields.append("name")
        if username_changed:
            conn.username = new_username
            update_fields.append("username")
        conn.save(update_fields=update_fields)
        return {
            "status": "skipped",
            "reason": "no_remote_url",
            "connection_id": str(conn.id),
        }

    # 다운로드/저장
    try:
        new_cached_url = fetch_and_store_profile_image(remote_url, conn.external_account_id)
    except ProfileImageFetchError as e:
        logger.warning("sync_ig_profile_picture: fetch failed conn=%s err=%s", conn.id, e)
        # 메타라도 갱신
        update_fields = ["profile_picture_synced_at", "updated_at"]
        conn.profile_picture_synced_at = timezone.now()
        if name_changed:
            conn.name = new_name
            update_fields.append("name")
        if username_changed:
            conn.username = new_username
            update_fields.append("username")
        conn.save(update_fields=update_fields)
        return {"status": "failed", "reason": f"fetch_error: {e}", "connection_id": str(conn.id)}

    # 정상 갱신
    conn.profile_picture_url = new_cached_url
    conn.profile_picture_source_url = remote_url
    conn.profile_picture_synced_at = timezone.now()
    update_fields = [
        "profile_picture_url",
        "profile_picture_source_url",
        "profile_picture_synced_at",
        "updated_at",
    ]
    if name_changed:
        conn.name = new_name
        update_fields.append("name")
    if username_changed:
        conn.username = new_username
        update_fields.append("username")
    conn.save(update_fields=update_fields)

    logger.info("sync_ig_profile_picture: updated conn=%s url=%s", conn.id, new_cached_url)
    return {
        "status": "updated",
        "connection_id": str(conn.id),
        "profile_picture_url": new_cached_url,
    }


# ===== P2d: 웹훅 메시징 이벤트 비동기 처리 (echo→DELIVERED / read→READ) =====
#
# 기존엔 instagram_webhook 가 _process_messaging_events 안에서 mark_delivered/mark_read 를
# row-lock·멱등성 없이 INLINE 으로 수행 → Meta 재전송 시 동시 UPDATE 레이스.
# 이제 webhook 핸들러는 EventInbox 에 멱등 INSERT 만 하고 200 을 즉시 반환하며,
# 실제 UPDATE 는 이 태스크가 select_for_update() 로 직렬화해서 처리한다.


def _apply_echo_delivered(*, mid: str, page_ig_user_id: str, recipient_user_id: str) -> int:
    """is_echo mid → SentDMLog DELIVERED 승격 (select_for_update 로 레이스 제거)."""
    matched = 0
    with transaction.atomic():
        qs = (
            SentDMLog.objects.select_for_update()
            .filter(meta_message_id=mid)
            .select_related("campaign__ig_connection")
        )
        logs = list(qs)
        if not logs and recipient_user_id:
            # mid 가 echo 단계에서 다르게 발급될 수 있어 recipient + 최근 ACCEPTED 로 fallback.
            # dm_log_recipient_status_idx (0019) 가 이 쿼리를 인덱스 스캔으로 처리.
            logs = list(
                SentDMLog.objects.select_for_update()
                .filter(
                    recipient_user_id=recipient_user_id,
                    status=SentDMLog.Status.ACCEPTED,
                    campaign__ig_connection__external_account_id=page_ig_user_id,
                )
                .order_by("-accepted_at")[:1]
            )
        for log in logs:
            if log.status == SentDMLog.Status.READ:
                continue
            log.append_verification_log({"path": "echo", "result": "matched", "mid": mid})
            log.mark_delivered(via=SentDMLog.VerifiedVia.ECHO, mid=mid)
            matched += 1
    return matched


def _apply_read(*, mid: str) -> int:
    """messaging_seen mid → SentDMLog READ 승격 (select_for_update)."""
    matched = 0
    with transaction.atomic():
        logs = list(SentDMLog.objects.select_for_update().filter(meta_message_id=mid))
        if not logs:
            logs = list(SentDMLog.objects.select_for_update().filter(echo_mid=mid))
        for log in logs:
            log.mark_read()
            matched += 1
    return matched


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 5, "countdown": 10},
    retry_backoff=True,
)
def process_messaging_event(self, event_key: str):
    """EventInbox 1건을 소비해 SentDMLog 상태를 갱신 (멱등).

    EventInbox.event_key 가 이미 UNIQUE 로 중복을 막고, 여기서 processed_at 으로 재처리도 막는다.
    실패하면 재시도되며, EventInbox 행은 남아 있어 reconcile 워커의 능동 검증이 안전망이 된다.
    """
    # EventInbox 는 파티션 테이블에서 per-partition UNIQUE(event_key, received_at) 라, 동시/자정경계
    # 재전송 시 같은 event_key 가 2행 생길 수 있다(무해 — 적용은 멱등, 중복 enqueue 는 아래 processed_at
    # 게이트가 흡수, 중복 행은 파티션 DROP 으로 청소). .get() 은 MultipleObjectsReturned 로 죽으므로
    # 가장 오래된 1건만 처리한다.
    evt = EventInbox.objects.filter(event_key=event_key).order_by("received_at").first()
    if evt is None:
        logger.warning("process_messaging_event: missing EventInbox %s", event_key)
        return {"status": "missing"}

    if evt.processed_at:
        return {"status": "already_processed"}

    data = evt.payload or {}
    mid = data.get("mid") or ""
    matched = 0
    if evt.event_type == EventInbox.EVENT_ECHO and mid:
        matched = _apply_echo_delivered(
            mid=mid,
            page_ig_user_id=data.get("page_ig_user_id", ""),
            recipient_user_id=data.get("recipient_user_id", ""),
        )
    elif evt.event_type == EventInbox.EVENT_READ and mid:
        matched = _apply_read(mid=mid)

    evt.processed_at = timezone.now()
    evt.save(update_fields=["processed_at"])
    logger.info("process_messaging_event %s: matched=%s", event_key, matched)
    return {"status": "ok", "matched": matched}
