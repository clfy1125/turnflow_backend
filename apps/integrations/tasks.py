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

from .dm_exceptions import DMSendError, DMTransientError, exception_to_classification
from .models import (
    AutoDMCampaign,
    EventInbox,
    IGAccountConnection,
    SeenComment,
    SentDMLog,
    SpamCommentLog,
    SpamFilterConfig,
)
from .services import (
    CommentReplyPermanentError,
    InstagramCommentService,
    InstagramMediaService,
    InstagramMessagingService,
    InstagramOAuthService,
    SpamDetectionService,
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
    qs = IGAccountConnection.objects.filter(status=IGAccountConnection.Status.ACTIVE)
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
    """발송 직전 속도 제어. defer 해야 하면 (seconds, reason), 보내도 되면 None.

    - per-campaign 시간당 throttle(can_send_more): rolling 1h → 짧게 재평가.
    - per-IG-account Meta 안전속도 거버너(rate_governor): 750/hr Private Reply + 분당 버스트.
      초과 시 다음 윈도우(시각 경계)까지 defer → requeue 워커가 created_at 순으로 순차 재투입.
    드랍/실패가 아니라 항상 '지연'이다.
    """
    # ★ P4: Action Block 쿨다운 중이면 그 계정 모든 발송을 defer (Meta 로 보내지 않음 → 차단 연장 방지).
    from .rate_governor import action_block_cooldown_remaining

    ab_remaining = action_block_cooldown_remaining(str(ig_conn.external_account_id))
    if ab_remaining > 0:
        return (ab_remaining, "action_block_cooldown")

    if not campaign.can_send_more():
        # rolling 1h window — 슬롯이 비는 정확한 시각 계산은 비용 대비 이득이 적어
        # 5분 후 재평가(요구사항: 초과분은 드랍하지 않고 나중에 발송).
        return (300, "campaign_hourly_cap")

    if getattr(settings, "DM_GOVERNOR_ENABLED", True):
        from .rate_governor import check as _rate_check

        decision = _rate_check(
            ig_account_id=str(ig_conn.external_account_id),
            plan=_resolve_plan_name(campaign),
        )
        if not decision.allowed:
            # retry_after = 다음 시간/분 윈도우까지 초. 최소 30초 보장.
            return (max(int(decision.retry_after), 30), f"rate_governed:{decision.reason}")

    return None


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

    if cls.retriable:
        exp = min(log.retry_count, 10)
        backoff = min(60 * (2**exp), 3600)  # 상한 1h (쿼터는 시각경계로 풀림)
        log.next_retry_at = timezone.now() + timedelta(seconds=backoff)
        log.status = SentDMLog.Status.QUEUED
        log.save(update_fields=["retry_count", "next_retry_at", "status"])
        return {"status": "deferred", "reason": cls.reason, "retry_count": log.retry_count}

    # non-retriable → 종결
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
        ig_conn.mark_as_error(f"DM 발송 중 토큰/세션/권한 오류: {exc}")

    return {"status": cls.log_status, "reason": cls.reason}


# ===== 진입점 =====


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
)
def process_comment_and_send_dm(self, webhook_payload: dict):
    """
    댓글 웹훅 이벤트 처리.

    1) 스팸 검사 (스팸 필터 활성 시)
    2) 스팸이면 댓글 숨김 처리 후 종료
    3) 정상이면 매칭되는 활성 캠페인마다 DM 발송 큐에 enqueue
    """
    try:
        logger.debug(f"Processing comment webhook: {webhook_payload}")

        field = webhook_payload.get("field")
        value = webhook_payload.get("value", {})

        if field != "comments":
            return {"status": "skipped", "reason": f"Unsupported field: {field}"}

        comment_id = value.get("id")
        comment_text = value.get("text", "")
        parent_id = value.get("parent_id")  # 대댓글이면 부모 comment_id, top-level이면 빈 값
        from_user = value.get("from", {})
        from_user_id = from_user.get("id")
        from_username = from_user.get("username")
        media = value.get("media", {})
        media_id = media.get("id")

        if not all([comment_id, from_user_id, from_username, media_id]):
            logger.error(f"Missing required fields in webhook payload: {webhook_payload}")
            return {"status": "error", "reason": "Missing required fields"}

        # ★ 대댓글(답글) 가드:
        # 우리 시스템이 게시한 공개 답글이 다시 webhook 으로 들어오면 → DM 무한 루프.
        # 외부 사용자의 답글 역시 캠페인 트리거 대상이 아님 (top-level 댓글만 트리거).
        # parent_id 가 있으면 무조건 skip.
        if parent_id:
            logger.info(f"Skipping reply (대댓글): comment_id={comment_id} parent={parent_id}")
            return {"status": "skipped", "reason": "is_reply"}

        # ★ Self-comment 가드:
        # 비즈니스 본인이 자기 게시물에 댓글 → 자기 자신에게 DM 가는 루프 차단.
        # webhook entry.id 는 connected page 의 IG user id 와 동일.
        page_ig_user_id = str(webhook_payload.get("entry_id") or "")
        if page_ig_user_id and str(from_user_id) == page_ig_user_id:
            logger.info(
                f"Skipping self-comment DM: page={page_ig_user_id} "
                f"commented on own post (comment_id={comment_id})"
            )
            return {"status": "skipped", "reason": "self_comment"}

        # 1) 스팸 검사
        spam_result = _check_and_handle_spam(
            comment_id=comment_id,
            comment_text=comment_text,
            from_user_id=from_user_id,
            from_username=from_username,
            media_id=media_id,
            webhook_payload=webhook_payload,
        )
        if spam_result.get("is_spam"):
            return spam_result

        # 2) 활성 캠페인 매칭 (trigger_type + keyword 모두 평가)
        # webhook 의 entry.id 는 IG user id — 그 계정의 캠페인만 후보
        ig_user_id = str(webhook_payload.get("entry_id") or "")
        candidate_qs = _active_campaigns_for_account(ig_user_id)

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

        if not matched_campaigns:
            return {"status": "skipped", "reason": "No campaign matched (media/keyword)"}

        results = []
        for campaign in matched_campaigns:
            results.append(
                _enqueue_send_dm(
                    campaign=campaign,
                    comment_id=comment_id,
                    comment_text=comment_text,
                    from_user_id=from_user_id,
                    from_username=from_username,
                    webhook_payload=webhook_payload,
                )
            )

        return {"status": "queued", "results": results}

    except Exception as e:
        logger.exception(f"Error processing comment webhook: {e}")
        raise


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
        1. ig_conn.last_seen_media_id 가 비어있으면 → 첫 사용자 케이스, 무조건 attach
        2. media_id 가 last_seen_media_id 와 동일 → baseline 게시물, attach 안 함
        3. GET /v25.0/{media_id}?fields=timestamp 호출
           - timestamp > last_seen_media_at → 진짜 새 게시물, attach
           - timestamp <= last_seen_media_at → 옛날 게시물, attach 안 함
           - API 실패 → 안전하게 attach 안 함 (false negative > 잘못된 attach)

    Returns:
        attach 된 AutoDMCampaign 인스턴스 리스트 (refresh 된 상태)
    """
    if not unattached_campaigns or not webhook_media_id:
        return []

    # 모든 unattached 캠페인은 같은 IG 계정 소유 (호출자가 보장)
    ig_conn = unattached_campaigns[0].ig_connection

    # 룰 2: baseline 과 동일 미디어면 skip
    if ig_conn.last_seen_media_id and ig_conn.last_seen_media_id == webhook_media_id:
        return []

    # baseline 없으면 룰 1: 무조건 attach (첫 사용자)
    # baseline 있으면 timestamp 비교 필요
    if ig_conn.last_seen_media_id and ig_conn.last_seen_media_at:
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

        if media_ts <= ig_conn.last_seen_media_at:
            logger.info(
                f"next_media webhook attach: media={webhook_media_id} is older than "
                f"baseline ({media_ts.isoformat()} <= "
                f"{ig_conn.last_seen_media_at.isoformat()}) — skip"
            )
            return []
        new_media_at = media_ts
    else:
        # baseline 없음 → 첫 사용 케이스 (확실히 가장 최신 게시물로 간주)
        new_media_at = timezone.now()

    # ★ Attach: 모든 unattached next_media 캠페인에 media_id 부착 + 트리거 전환
    updated_count = AutoDMCampaign.objects.filter(
        id__in=[c.id for c in unattached_campaigns],
        media_id="",  # 동시성 안전: 이미 다른 webhook 이 attach 했으면 skip
    ).update(
        media_id=webhook_media_id,
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        updated_at=timezone.now(),
    )

    # baseline 갱신
    ig_conn.last_seen_media_id = webhook_media_id
    ig_conn.last_seen_media_at = new_media_at
    ig_conn.save(update_fields=["last_seen_media_id", "last_seen_media_at"])

    if updated_count:
        logger.info(
            f"next_media webhook attach: attached {updated_count} campaign(s) "
            f"on ig_conn={ig_conn.id} to media={webhook_media_id}"
        )

    # refresh 후 반환 (호출자가 즉시 매칭 사용)
    return list(
        AutoDMCampaign.objects.filter(
            id__in=[c.id for c in unattached_campaigns],
            media_id=webhook_media_id,
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
    # 1차 차단되지만, 다른 진입점(향후 추가)에서도 안전하게.
    if str(from_user_id) == str(ig_conn.external_account_id):
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "self_comment",
        }

    # ★ 동일 수신자 쿨다운(DM_RECIPIENT_COOLDOWN_SECONDS): 같은 사람이 단시간에 여러 댓글 달면
    # idempotency_key 는 comment_id 별로 다르므로 중복 방지 안 됨 → 별도 가드(계정 보호).
    cooldown_s = getattr(settings, "DM_RECIPIENT_COOLDOWN_SECONDS", 300)
    cooldown_cutoff = timezone.now() - timedelta(seconds=cooldown_s)
    recent_to_same_recipient = SentDMLog.objects.filter(
        campaign=campaign,
        recipient_user_id=from_user_id,
        created_at__gte=cooldown_cutoff,
    ).exists()
    if recent_to_same_recipient:
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": f"recipient_cooldown_{cooldown_s}s",
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

    # ★ 예약 발송 창 가드 (권위 있는 단일 체크포인트):
    # 모든 발송이 이 태스크를 거친다 — opening / reward(postback) / follow 재안내 /
    # reconcile 재큐 / 수동 재시도. enqueue 시점 가드를 통과한 뒤라도, 실행 시점에
    # 캠페인 활성 기간(window)이 끝났으면(또는 아직 시작 전이면) 여기서 확정 차단한다.
    # 상태(pause/complete)는 별도 관심사라 건드리지 않고, 오직 예약 창만 본다.
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
    """next_retry_at 이 도래한 defer(QUEUED) 건을 send_dm_task 로 순차(FIFO) 재투입.

    rate-limit/transient defer(item1) + 시간당 한도·계정 거버너 defer(item2) 공통 픽커다.
    created_at 오름차순으로 처리 = 가장 오래 기다린 건부터 → "다음 윈도우에 순차 발송".
    next_retry_at 이 없는 채 2분+ 정체된 QUEUED(초기 dispatch 유실)도 안전 재투입한다.

    동시 픽업은 select_for_update(skip_locked) + next_retry_at=None 마킹으로 방지하고,
    send_dm_task 진입 가드(status QUEUED/SUBMITTING)가 이중 발송을 막는다.

    Beat: 30초 주기.
    """
    now = timezone.now()
    stale_cutoff = now - timedelta(minutes=2)
    with transaction.atomic():
        rows = list(
            SentDMLog.objects.select_for_update(skip_locked=True)
            .filter(status=SentDMLog.Status.QUEUED)
            .filter(
                Q(next_retry_at__lte=now)
                | Q(next_retry_at__isnull=True, created_at__lte=stale_cutoff)
            )
            .order_by("created_at")
            .values_list("id", flat=True)[:200]
        )
        ids = list(rows)
        if ids:
            # 픽업 표식: next_retry_at 비워 다음 tick 중복 픽업 방지(재defer 시 send_dm_task 가 다시 채움).
            SentDMLog.objects.filter(id__in=ids).update(next_retry_at=None)

    for log_id in ids:
        send_dm_task.delay(str(log_id))

    if ids:
        logger.info(f"requeue_deferred_dms: requeued {len(ids)} deferred logs")
    return {"requeued": len(ids)}


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

    return {
        "total_queued": total,
        "window_risk": risk,
        "oldest_age_hours": round(oldest_age_h, 1),
    }


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
            # 이 계정의 모든 next_media 캠페인에 attach (한번에 같은 미디어로)
            updated = AutoDMCampaign.objects.filter(
                ig_connection_id=conn.id,
                trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
                status=AutoDMCampaign.Status.ACTIVE,
                media_id="",
            ).update(
                media_id=mid,
                media_url=media_obj.get("permalink") or None,
                trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
                updated_at=timezone.now(),
            )
            attached_for_account += updated
            attached_total += updated

            # 한 미디어에 attach 됐으면 나머지 next 캠페인은 더 없음 → loop 탈출
            if updated == 0:
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

            # 진짜 누락분 → 트리거 평가
            text = c.get("text") or ""
            matched = _matched_campaigns_for_comment(
                ig_user_id=conn.external_account_id,
                media_id=media_id,
                comment_text=text,
                now=now,
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
            id__in=conn_ids, status=IGAccountConnection.Status.ACTIVE
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
    """만료된 댓글 관측 장부(SeenComment) 삭제 (Beat: 매일).

    expires_at 가 지난 행을 배치 삭제. 멱등 — 다음 실행에 남은 만료분 계속 정리.
    """
    cutoff = timezone.now()
    deleted = 0
    while True:
        ids = list(
            SeenComment.objects.filter(expires_at__lt=cutoff).values_list("id", flat=True)[:5000]
        )
        if not ids:
            break
        n, _ = SeenComment.objects.filter(id__in=ids).delete()
        deleted += n
        if len(ids) < 5000:
            break
    if deleted:
        logger.info("cleanup_comment_ledger: deleted %s expired rows", deleted)
    return {"deleted": deleted}


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
def post_public_reply(self, log_id: str):
    """
    DM ACCEPTED 후 댓글에 공개 답글 게시 (v3.5 — 봇 검사 회피).

    동작:
      1. 템플릿 목록(public_reply_templates)에서 무작위로 1개 선택 — 다양성 확보
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

    # 활성 여부
    if not campaign.public_reply_enabled:
        return {"status": "skipped", "reason": "public reply disabled"}

    # 이미 게시했으면 skip
    if log.public_reply_id:
        return {"status": "skipped", "reason": "already replied"}

    # 템플릿 1개 선택 (list 우선, legacy fallback)
    template = campaign.pick_public_reply_template()
    if not template:
        return {"status": "skipped", "reason": "no template content"}

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
                "error": e.message,
                "code": e.code,
                "subcode": e.subcode,
            }
        )

        # ===== Circuit Breaker =====
        # 같은 IG 계정에서 10분 안에 영구 에러 3건 이상 누적되면
        # → 그 계정의 모든 캠페인 public_reply_enabled 자동 OFF.
        # 인스타 Action Block (code=1) 이 한 번 걸리면 그 시점부터의 모든 답글이
        # 같은 차단에 묶이는데, 그 사이에 계속 시도하면 차단 기간이 연장됨.
        # 자동 OFF 로 추가 시도를 막아 차단이 빨리 풀리게 한다.
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
                    for ev in (rl.verification_log or [])
                )
            )
            CB_THRESHOLD = 3
            if permanent_count >= CB_THRESHOLD:
                affected = AutoDMCampaign.objects.filter(
                    ig_connection=ig_conn,
                    public_reply_enabled=True,
                ).update(public_reply_enabled=False)
                logger.warning(
                    f"Circuit breaker tripped for {ig_conn.username}: "
                    f"{permanent_count} permanent errors in 10min "
                    f"→ disabled public_reply on {affected} campaign(s). "
                    f"Manual re-enable required after Meta restriction clears."
                )
                log.append_verification_log(
                    {
                        "path": "public_reply",
                        "result": "circuit_breaker_tripped",
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
    log.public_reply_id = reply_id
    log.public_reply_posted_at = timezone.now()
    log.save(update_fields=["public_reply_id", "public_reply_posted_at"])
    log.append_verification_log(
        {
            "path": "public_reply",
            "result": "posted",
            "reply_id": reply_id,
            "template_used": template[:50],
        }
    )
    return {"status": "posted", "reply_id": reply_id, "template": template[:50]}


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
    Follow-gate 통과 후 본 DM(reward) 을 사용자 IGSID 로 발송.

    24h 메시징 윈도우 내에서만 가능 (사용자가 우리에게 메시지 보낸 직후라 OK).
    """
    try:
        opening = SentDMLog.objects.select_related("campaign__ig_connection").get(id=opening_log_id)
    except SentDMLog.DoesNotExist:
        return {"status": "not_found"}

    campaign = opening.campaign

    # ★ 예약 발송 창 가드: 이 경로(키워드 답장 reward)는 send_dm_task 를 거치지 않고
    # 직접 발송하므로 별도로 창 검사. 활성 기간이 끝났으면 reward 도 발송하지 않는다.
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
    import hashlib

    idem = hashlib.sha256(f"reward:{opening.idempotency_key}".encode()).hexdigest()
    reward_log, created = SentDMLog.create_idempotent(
        idempotency_key=idem,
        campaign=campaign,
        comment_id=opening.comment_id,
        comment_text=opening.comment_text,
        recipient_user_id=opening.recipient_user_id,
        recipient_username=opening.recipient_username,
        message_sent=campaign.reward_message_template,
        status=SentDMLog.Status.SUBMITTING,
        submitted_at=timezone.now(),  # create 와 한 트랜잭션 — 별도 save 제거
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

    try:
        result = InstagramMessagingService.send_dm_via_user_id(
            ig_user_id=ig_conn.external_account_id,
            recipient_id=opening.recipient_user_id,
            message_text=campaign.reward_message_template,
            access_token=ig_conn.access_token,
            buttons=campaign.get_link_buttons(),
        )
    except DMSendError as e:
        # v3.9: rate-limit/transient 은 defer(QUEUED+next_retry_at) → requeue 워커가 send_dm_task 로
        # 재투입(reward 는 child 라 user_id 경로로 재발송). non-retriable 은 즉시 종결.
        # reward 는 항상 child(parent_log=opening) 이므로 _defer_or_fail 의 카운터 가드로 통계 제외.
        return _defer_or_fail(reward_log, campaign, ig_conn, e)

    reward_log.mark_accepted(
        message_id=result["message_id"],
        api_response=result.get("_raw") or result,
    )
    # reward 는 child → 캠페인 카운터 제외 (opening 이 이미 카운트됨).
    if reward_log.parent_log_id is None:
        campaign.increment_sent()
    verify_dm_delivery.apply_async(args=[str(reward_log.id)], countdown=600)

    return {
        "status": "accepted",
        "reward_log_id": str(reward_log.id),
        "message_id": result["message_id"],
    }


# ===== 스팸 처리 (기존 로직 보존) =====


def _check_and_handle_spam(
    comment_id: str,
    comment_text: str,
    from_user_id: str,
    from_username: str,
    media_id: str,
    webhook_payload: dict,
) -> dict:
    """스팸 검사 + 숨김 처리"""
    try:
        campaign = (
            AutoDMCampaign.objects.filter(media_id=media_id).select_related("ig_connection").first()
        )
        if not campaign:
            return {"is_spam": False, "spam_filter_processed": False}

        ig_connection = campaign.ig_connection

        try:
            spam_filter = SpamFilterConfig.objects.get(ig_connection=ig_connection)
        except SpamFilterConfig.DoesNotExist:
            return {"is_spam": False, "spam_filter_processed": False}

        if not spam_filter.is_active():
            return {
                "is_spam": False,
                "spam_filter_processed": False,
                "reason": "Spam filter inactive",
            }

        # ★ 플랜 런타임 게이트 — 스팸필터는 프로 전용. 다운그레이드 후에도 config 행은
        # 남아있으므로(재업그레이드 시 설정 복원) 여기서 즉시 무력화한다.
        # 플랜 조회 실패는 기능 꺼짐 취급(fail-closed) — 댓글이 필터를 안 타는 것뿐 파괴 없음.
        from apps.billing.subscription_utils import owner_has_feature

        if not owner_has_feature(ig_connection.workspace, "spam_filter"):
            return {
                "is_spam": False,
                "spam_filter_processed": False,
                "reason": "plan_not_allowed",
            }

        is_spam, reasons = SpamDetectionService.is_spam(
            text=comment_text,
            spam_keywords=spam_filter.spam_keywords,
            check_urls=spam_filter.block_urls,
        )

        if not is_spam:
            return {"is_spam": False, "spam_filter_processed": True}

        spam_log = SpamCommentLog.objects.create(
            spam_filter=spam_filter,
            comment_id=comment_id,
            comment_text=comment_text,
            commenter_user_id=from_user_id,
            commenter_username=from_username,
            media_id=media_id,
            spam_reasons=reasons,
            status=SpamCommentLog.Status.DETECTED,
            webhook_payload=webhook_payload,
        )
        spam_filter.increment_spam_detected()

        try:
            api_response = InstagramCommentService.hide_comment(
                comment_id=comment_id, access_token=ig_connection.access_token
            )
            spam_log.mark_as_hidden(api_response)
            spam_filter.increment_hidden()
            return {
                "is_spam": True,
                "status": "hidden",
                "spam_filter_processed": True,
                "spam_reasons": reasons,
                "spam_log_id": str(spam_log.id),
            }
        except Exception as hide_error:
            spam_log.mark_as_failed(str(hide_error))
            return {
                "is_spam": True,
                "status": "failed_to_hide",
                "spam_filter_processed": True,
                "spam_reasons": reasons,
                "error": str(hide_error),
                "spam_log_id": str(spam_log.id),
            }

    except Exception as e:
        logger.exception(f"Error in spam check: {e}")
        return {"is_spam": False, "spam_filter_processed": False, "error": str(e)}


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
            logger.exception("IG token refresh failed: conn=%s err=%s", conn.id, e)
            try:
                conn.error_message = f"refresh failed: {e}"[:500]
                conn.save(update_fields=["error_message", "updated_at"])
            except Exception:
                pass
            failed.append(
                {
                    "id": str(conn.id),
                    "username": conn.username or "(unknown)",
                    "error": str(e)[:200],
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
        return _enqueue_reward_dm(opening=opening, igsid=igsid)

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

    follow_passed = bool(is_follow)
    if is_follow is None:
        # Meta 가 필드를 안 내려준 경우 — 권한/잘못된 IGSID. 보수적으로 미통과 처리.
        follow_passed = False

    if follow_passed:
        return _enqueue_reward_dm(opening=opening, igsid=igsid)
    return _enqueue_follow_retry(opening=opening, igsid=igsid)


def _enqueue_reward_dm(*, opening: SentDMLog, igsid: str) -> dict:
    """Gate 통과 → reward DM 발송 큐에 enqueue."""
    campaign = opening.campaign
    reward_body = (campaign.reward_message_template or "").strip()
    if not reward_body:
        # 정책상 reward 비어있으면 게이트도 못 켜지지만 안전망
        opening.gate_status = SentDMLog.GateStatus.PASSED
        opening.save(update_fields=["gate_status", "sent_at"])
        return {"status": "passed_no_reward", "opening_log_id": str(opening.id)}

    # reward 는 새 idempotency_key (opening_log_id 를 시드로) — 같은 opening 에서
    # 두 번 PASSED 처리해도 같은 키가 나와 DB UNIQUE 로 중복 방지.
    import hashlib

    key_raw = f"reward:{campaign.id}:{opening.id}"
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
    # opening 도 PASSED 로 마킹 (reward dedup 이 이미 보장되므로 분리해도 무손실·멱등)
    SentDMLog.objects.filter(pk=opening.pk).update(gate_status=SentDMLog.GateStatus.PASSED)

    send_dm_task.delay(str(reward_log.id))
    return {
        "status": "reward_enqueued",
        "opening_log_id": str(opening.id),
        "reward_log_id": str(reward_log.id),
    }


def _enqueue_follow_retry(*, opening: SentDMLog, igsid: str) -> dict:
    """Gate 미통과 → 재안내 메시지 + 같은 quick_reply 재첨부.

    매 재시도마다 새 SentDMLog 가 생기지만, parent_log 로 opening 에 묶인다.
    opening 의 gate_status 는 PENDING 유지 (여전히 통과 대기).
    재시도 로그도 OPENING + PENDING 으로 만들어 send_dm_task 가 quick_reply 를 첨부하게 한다.
    """
    campaign = opening.campaign
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
