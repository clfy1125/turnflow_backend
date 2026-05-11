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
from django.db import IntegrityError, transaction
from django.utils import timezone

from .dm_exceptions import (
    DMSendError,
    DMTransientError,
    exception_to_classification,
)
from .models import (
    AutoDMCampaign,
    IGAccountConnection,
    SentDMLog,
    SpamCommentLog,
    SpamFilterConfig,
)
from .services import (
    InstagramCommentService,
    InstagramMediaService,
    InstagramMessagingService,
    SpamDetectionService,
)

logger = logging.getLogger(__name__)


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
            logger.info(
                f"Skipping reply (대댓글): comment_id={comment_id} parent={parent_id}"
            )
            return {"status": "skipped", "reason": "is_reply"}

        # ★ Self-comment 가드:
        # 비즈니스 본인이 자기 게시물에 댓글 → 자기 자신에게 DM 가는 루프 차단.
        # webhook entry.id 는 connected page 의 IG user id 와 동일.
        page_ig_user_id = str(webhook_payload.get("entry_id") or "")
        if (
            page_ig_user_id
            and str(from_user_id) == page_ig_user_id
        ):
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
        candidate_qs = (
            AutoDMCampaign.objects
            .filter(status=AutoDMCampaign.Status.ACTIVE)
            .select_related("ig_connection", "ig_connection__workspace")
        )
        if ig_user_id:
            candidate_qs = candidate_qs.filter(
                ig_connection__external_account_id=ig_user_id
            )

        # trigger_type 평가:
        #   specific_media (or attached next_media): media_id 일치
        #   any_media: 모두 통과
        matched_campaigns = []
        for c in candidate_qs:
            if c.matches_media(media_id) and c.matches_keyword(comment_text):
                matched_campaigns.append(c)

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
    sender_user_id  = str(payload.get("sender_user_id") or "")
    sender_username = str(payload.get("sender_username") or "")
    story_id        = str(payload.get("story_id") or "")
    message_mid     = str(payload.get("message_mid") or "")
    message_text    = payload.get("message_text") or ""

    if not (page_ig_user_id and sender_user_id and story_id and message_mid):
        return {"status": "skipped", "reason": "missing required fields"}

    # ★ Self-message 가드: 자기 자신의 메시지 무시
    if sender_user_id == page_ig_user_id:
        return {"status": "skipped", "reason": "self_story_reply"}

    candidate_qs = (
        AutoDMCampaign.objects
        .filter(
            status=AutoDMCampaign.Status.ACTIVE,
            trigger_type=AutoDMCampaign.TriggerType.STORY_REPLY,
            ig_connection__external_account_id=page_ig_user_id,
        )
        .select_related("ig_connection", "ig_connection__workspace")
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

    # Self-DM 가드
    if str(sender_user_id) == str(ig_conn.external_account_id):
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "self_story_reply",
        }

    # 수신자 60초 쿨다운 (동일 사용자가 연속 답장하는 케이스 방어)
    cooldown_cutoff = timezone.now() - timedelta(seconds=60)
    recent = SentDMLog.objects.filter(
        campaign=campaign,
        recipient_user_id=sender_user_id,
        created_at__gte=cooldown_cutoff,
    ).exists()
    if recent:
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "recipient_cooldown_60s",
        }

    idempotency_key = InstagramMessagingService.build_idempotency_key(
        workspace_id=ig_conn.workspace_id,
        ig_user_id=ig_conn.external_account_id,
        comment_id=message_mid,  # story 답장은 message_mid 를 trigger ID 로 사용
        campaign_id=campaign.id,
    )

    if not campaign.can_send_more():
        try:
            SentDMLog.objects.create(
                campaign=campaign,
                comment_id="",  # story 답장은 comment_id 없음
                comment_text=message_text,
                recipient_user_id=sender_user_id,
                recipient_username=sender_username,
                message_sent=campaign.get_opening_message(),
                status=SentDMLog.Status.SKIPPED,
                error_message="Hourly send limit reached",
                idempotency_key=idempotency_key,
                webhook_payload=payload,
                dm_kind=SentDMLog.DMKind.STANDALONE,
                gate_status=SentDMLog.GateStatus.NONE,
            )
        except IntegrityError:
            pass
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "Hourly limit reached",
        }

    try:
        with transaction.atomic():
            log = SentDMLog.objects.create(
                campaign=campaign,
                comment_id="",  # ★ Story 답장은 comment_id 없음 (send_dm_task 가 user_id 분기 판단)
                comment_text=message_text,
                recipient_user_id=sender_user_id,
                recipient_username=sender_username,
                message_sent=campaign.get_opening_message(),
                status=SentDMLog.Status.QUEUED,
                idempotency_key=idempotency_key,
                webhook_payload=payload,
                dm_kind=SentDMLog.DMKind.STANDALONE,
                gate_status=SentDMLog.GateStatus.NONE,
            )
    except IntegrityError:
        existing = SentDMLog.objects.filter(idempotency_key=idempotency_key).first()
        return {
            "campaign_id": str(campaign.id),
            "status": "duplicate",
            "log_id": str(existing.id) if existing else None,
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
    if (
        ig_conn.last_seen_media_id
        and ig_conn.last_seen_media_id == webhook_media_id
    ):
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
    ig_conn.save(
        update_fields=["last_seen_media_id", "last_seen_media_at"]
    )

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

    # ★ Self-DM 가드 (이중 안전망):
    # 캠페인 owner = 댓글 작성자면 skip. _process_comment_and_send_dm 에서
    # 1차 차단되지만, 다른 진입점(향후 추가)에서도 안전하게.
    if str(from_user_id) == str(ig_conn.external_account_id):
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "self_comment",
        }

    # ★ 동일 수신자 60초 쿨다운: 같은 사람이 단시간에 여러 댓글 달면
    # idempotency_key 는 comment_id 별로 다르므로 중복 방지 안 됨 → 별도 가드.
    cooldown_cutoff = timezone.now() - timedelta(seconds=60)
    recent_to_same_recipient = SentDMLog.objects.filter(
        campaign=campaign,
        recipient_user_id=from_user_id,
        created_at__gte=cooldown_cutoff,
    ).exists()
    if recent_to_same_recipient:
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "recipient_cooldown_60s",
        }

    idempotency_key = InstagramMessagingService.build_idempotency_key(
        workspace_id=ig_conn.workspace_id,
        ig_user_id=ig_conn.external_account_id,
        comment_id=comment_id,
        campaign_id=campaign.id,
    )

    # 발송할 본문 + 분류 결정
    # v3.5: Follow-gate 는 deprecated (Meta 한계로 검증 불가). 모든 DM 은 STANDALONE.
    message_body = campaign.get_opening_message()
    dm_kind = SentDMLog.DMKind.STANDALONE
    gate_status = SentDMLog.GateStatus.NONE

    # 시간당 발송 제한 체크
    if not campaign.can_send_more():
        try:
            SentDMLog.objects.create(
                campaign=campaign,
                comment_id=comment_id,
                comment_text=comment_text,
                recipient_user_id=from_user_id,
                recipient_username=from_username,
                message_sent=message_body,
                status=SentDMLog.Status.SKIPPED,
                error_message="Hourly send limit reached",
                idempotency_key=idempotency_key,
                webhook_payload=webhook_payload,
                dm_kind=dm_kind,
                gate_status=SentDMLog.GateStatus.NONE,
            )
        except IntegrityError:
            pass  # 이미 같은 키 존재 → 무시
        return {
            "campaign_id": str(campaign.id),
            "status": "skipped",
            "reason": "Hourly limit reached",
        }

    try:
        with transaction.atomic():
            log = SentDMLog.objects.create(
                campaign=campaign,
                comment_id=comment_id,
                comment_text=comment_text,
                recipient_user_id=from_user_id,
                recipient_username=from_username,
                message_sent=message_body,
                status=SentDMLog.Status.QUEUED,
                idempotency_key=idempotency_key,
                webhook_payload=webhook_payload,
                dm_kind=dm_kind,
                gate_status=gate_status,
            )
    except IntegrityError:
        existing = SentDMLog.objects.filter(idempotency_key=idempotency_key).first()
        return {
            "campaign_id": str(campaign.id),
            "status": "duplicate",
            "log_id": str(existing.id) if existing else None,
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
            "campaign", "campaign__ig_connection"
        ).get(id=log_id)
    except SentDMLog.DoesNotExist:
        logger.warning(f"SentDMLog {log_id} not found")
        return {"status": "not_found"}

    # 이미 처리됨
    if log.status not in (SentDMLog.Status.QUEUED, SentDMLog.Status.SUBMITTING):
        return {"status": "skipped", "reason": f"already {log.status}"}

    campaign = log.campaign
    ig_conn = campaign.ig_connection

    if ig_conn.status != IGAccountConnection.Status.ACTIVE:
        log.mark_failed(
            status=SentDMLog.Status.FAILED_TOKEN,
            error_message=f"IG connection not active: {ig_conn.status}",
        )
        campaign.increment_failed()
        return {"status": "failed_token"}

    log.mark_submitting()

    try:
        # ★ Story 답장 캠페인 (comment_id 없음) → user_id 기반 DM (24h 윈도우)
        # 그 외 (comment 트리거) → Private Reply via comment_id
        if log.comment_id:
            result = InstagramMessagingService.send_dm_via_comment(
                ig_user_id=ig_conn.external_account_id,
                comment_id=log.comment_id,
                message_text=log.message_sent,
                access_token=ig_conn.access_token,
            )
        else:
            result = InstagramMessagingService.send_dm_via_user_id(
                ig_user_id=ig_conn.external_account_id,
                recipient_id=log.recipient_user_id,
                message_text=log.message_sent,
                access_token=ig_conn.access_token,
            )
    except DMSendError as e:
        cls = exception_to_classification(e)
        log.retry_count += 1

        # v3.2: retriable인 transient (RATE_LIMITED)만 재시도
        if cls.retriable and self.request.retries < self.max_retries:
            backoff = min(60 * (2 ** self.request.retries), 60 * 60 * 6)  # 최대 6h
            log.next_retry_at = timezone.now() + timedelta(seconds=backoff)
            # RATE_LIMITED 상태로 표시 후 재시도 큐에 들어가도록 QUEUED로 회귀
            log.status = SentDMLog.Status.QUEUED
            log.save(update_fields=["retry_count", "next_retry_at", "status"])
            raise self.retry(exc=e, countdown=backoff) from e

        # 모든 FAILED_* (FAILED_TOKEN/FAILED_PARAM/FAILED_WINDOW/FAILED_NO_TRACE)
        # 또는 RATE_LIMITED 한도 초과 → 즉시 Dead Letter (재시도 중단)
        log.mark_failed(
            status=cls.log_status,
            error_message=str(e),
            error_code=str(e.code) if e.code is not None else "",
            error_subcode=str(e.subcode) if e.subcode is not None else "",
            api_response=e.api_response,
        )
        campaign.increment_failed()

        if cls.log_status == SentDMLog.Status.FAILED_TOKEN:
            ig_conn.mark_as_error(f"DM 발송 중 토큰/세션/권한 오류: {e}")

        return {"status": cls.log_status, "reason": cls.reason}

    # 성공 — ACCEPTED 진입
    log.mark_accepted(
        message_id=result["message_id"],
        api_response=result.get("_raw") or result,
    )
    campaign.increment_sent()

    # 5분 후 첫 능동 검증 예약 (echo가 먼저 오면 skip됨)
    verify_dm_delivery.apply_async(
        args=[str(log.id)], countdown=300
    )

    # public_reply_enabled 면 댓글에 공개 답글도 게시 (best-effort)
    # v3.5: public_reply_templates(list) 또는 legacy public_reply_template 중 하나라도 있으면 OK
    # v3.7: Story 답장 캠페인 (comment_id 없음) 은 공개 답글 불가능 → skip
    has_reply_content = bool(
        (campaign.public_reply_templates or [])
        or (campaign.public_reply_template or "").strip()
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
        log = SentDMLog.objects.select_related(
            "campaign__ig_connection"
        ).get(id=log_id)
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
        log.append_verification_log(
            {"path": "conv_api", "result": "api_error", "error": str(e)}
        )
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
        # 첫 검증(5분 시점) — 35분 시점까지 한 번 더 예약
        remaining = max(60, int(35 * 60 - age.total_seconds()))
        verify_dm_delivery.apply_async(args=[log_id], countdown=remaining)
        return {"status": "deferred", "next_check_in_seconds": remaining}

    # 35분+ — 도착 확인 실패 종결 (자가 점검 체크리스트 영역)
    log.mark_failed(
        status=SentDMLog.Status.FAILED_NO_TRACE,
        error_message="No echo and not found in Conversations API after 35 minutes",
    )
    log.campaign.increment_failed()
    return {"status": "failed_no_trace"}


# ===== Beat 워커 =====


@shared_task
def reconcile_accepted_dms():
    """
    ACCEPTED 상태로 5분 이상 머무는 건들을 능동 검증.

    Beat: 1분 주기.
    """
    cutoff = timezone.now() - timedelta(minutes=5)
    queryset = SentDMLog.objects.filter(
        status=SentDMLog.Status.ACCEPTED,
        accepted_at__lte=cutoff,
    ).values_list("id", flat=True)[:200]

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
    SUBMITTING 상태로 60초 이상 정체된 건을 큐로 되돌림.

    Beat: 30초 주기.
    """
    cutoff = timezone.now() - timedelta(seconds=60)
    qs = SentDMLog.objects.filter(
        status=SentDMLog.Status.SUBMITTING,
        submitted_at__lte=cutoff,
    )

    count = 0
    for log in qs[:100]:
        log.status = SentDMLog.Status.QUEUED
        log.save(update_fields=["status"])
        send_dm_task.delay(str(log.id))
        count += 1

    if count:
        logger.warning(f"reconcile_stuck_submitting: requeued {count} stuck logs")
    return {"requeued": count}


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

    return {"failed_token": token_failures, "failed_no_trace": no_trace}


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

    next_campaigns_qs = AutoDMCampaign.objects.filter(
        trigger_type=AutoDMCampaign.TriggerType.NEXT_MEDIA,
        status=AutoDMCampaign.Status.ACTIVE,
        media_id="",
    ).values_list("ig_connection_id", flat=True).distinct()

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
            logger.warning(
                f"poll_new_media: API failed for ig_conn={conn.id}: {e}"
            )
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
        conn.last_seen_media_at = _parse_iso_timestamp(
            latest_new.get("timestamp")
        )
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
        AutoDMCampaign.objects
        .filter(
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
        items = ", ".join(
            f"{c.id}(last={c.last_polled_at.isoformat()})" for c in stale[:10]
        )
        logger.warning(
            f"check_polling_anomalies: {len(stale)} ig_conn stale (>15min): [{items}]"
        )

    return {
        "checked": len(pending_ig_ids),
        "never_polled": len(never_polled),
        "stale": len(stale),
    }


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
    except Exception as e:
        logger.warning(
            f"post_public_reply failed for log={log_id}: {e}; will retry"
        )
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
        SentDMLog.objects
        .select_related("campaign__ig_connection")
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
        opening = SentDMLog.objects.select_related(
            "campaign__ig_connection"
        ).get(id=opening_log_id)
    except SentDMLog.DoesNotExist:
        return {"status": "not_found"}

    campaign = opening.campaign
    if not campaign.reward_message_template:
        logger.warning(
            f"send_reward_dm: campaign {campaign.id} has no reward template; skip"
        )
        return {"status": "skipped", "reason": "no reward template"}

    ig_conn = campaign.ig_connection
    if ig_conn.status != IGAccountConnection.Status.ACTIVE:
        return {"status": "failed_token", "reason": "ig connection not active"}

    # 멱등성 키 — opening 의 idempotency_key 에 ":reward" 접미사
    idem = f"{opening.idempotency_key}:reward"
    try:
        with transaction.atomic():
            reward_log = SentDMLog.objects.create(
                campaign=campaign,
                comment_id=opening.comment_id,
                comment_text=opening.comment_text,
                recipient_user_id=opening.recipient_user_id,
                recipient_username=opening.recipient_username,
                message_sent=campaign.reward_message_template,
                status=SentDMLog.Status.SUBMITTING,
                idempotency_key=idem,
                webhook_payload=opening.webhook_payload,
                dm_kind=SentDMLog.DMKind.REWARD,
                gate_status=SentDMLog.GateStatus.NONE,
                parent_log=opening,
            )
            reward_log.submitted_at = timezone.now()
            reward_log.save(update_fields=["submitted_at"])
    except IntegrityError:
        existing = SentDMLog.objects.filter(idempotency_key=idem).first()
        return {
            "status": "duplicate",
            "reward_log_id": str(existing.id) if existing else None,
        }

    try:
        result = InstagramMessagingService.send_dm_via_user_id(
            ig_user_id=ig_conn.external_account_id,
            recipient_id=opening.recipient_user_id,
            message_text=campaign.reward_message_template,
            access_token=ig_conn.access_token,
        )
    except DMSendError as e:
        cls_info = exception_to_classification(e)
        if cls_info.retriable and self.request.retries < self.max_retries:
            backoff = min(60 * (2 ** self.request.retries), 60 * 60)
            reward_log.next_retry_at = timezone.now() + timedelta(seconds=backoff)
            reward_log.status = SentDMLog.Status.QUEUED
            reward_log.save(update_fields=["next_retry_at", "status"])
            raise self.retry(exc=e, countdown=backoff) from e

        reward_log.mark_failed(
            status=cls_info.log_status,
            error_message=str(e),
            error_code=str(e.code) if e.code is not None else "",
            error_subcode=str(e.subcode) if e.subcode is not None else "",
            api_response=e.api_response,
        )
        campaign.increment_failed()
        return {"status": cls_info.log_status, "reason": cls_info.reason}

    reward_log.mark_accepted(
        message_id=result["message_id"],
        api_response=result.get("_raw") or result,
    )
    campaign.increment_sent()
    verify_dm_delivery.apply_async(args=[str(reward_log.id)], countdown=300)

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
            AutoDMCampaign.objects.filter(media_id=media_id)
            .select_related("ig_connection")
            .first()
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
