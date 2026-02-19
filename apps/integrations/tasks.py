"""
Celery tasks for Instagram integration
DM 자동발송 및 스팸 필터링 비동기 처리
"""

import logging
from celery import shared_task
from django.utils import timezone
from datetime import datetime

from .models import AutoDMCampaign, SentDMLog, IGAccountConnection, SpamFilterConfig, SpamCommentLog
from .services import InstagramMessagingService, SpamDetectionService, InstagramCommentService

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
)
def process_comment_and_send_dm(self, webhook_payload: dict):
    """
    Webhook에서 받은 댓글 데이터 처리:
    1. 스팸 검사 (스팸 필터 활성화된 경우)
    2. 스팸이면 댓글 숨김 처리
    3. 스팸이 아니면 DM 자동 발송 로직 실행

    Args:
        webhook_payload: Instagram webhook payload

    Returns:
        처리 결과 딕셔너리
    """
    try:
        logger.info(f"Processing comment webhook: {webhook_payload}")

        # Webhook 데이터 파싱
        field = webhook_payload.get("field")
        value = webhook_payload.get("value", {})

        if field != "comments":
            logger.warning(f"Unsupported webhook field: {field}")
            return {"status": "skipped", "reason": f"Unsupported field: {field}"}

        # 댓글 정보 추출
        comment_id = value.get("id")
        comment_text = value.get("text", "")
        parent_id = value.get("parent_id")  # 대댓글인 경우

        # 댓글 작성자 정보
        from_user = value.get("from", {})
        from_user_id = from_user.get("id")
        from_username = from_user.get("username")

        # 미디어 정보
        media = value.get("media", {})
        media_id = media.get("id")

        if not all([comment_id, from_user_id, from_username, media_id]):
            logger.error(f"Missing required fields in webhook payload: {webhook_payload}")
            return {"status": "error", "reason": "Missing required fields"}

        # === 1단계: 스팸 필터 검사 (활성화된 경우) ===
        spam_check_result = _check_and_handle_spam(
            comment_id=comment_id,
            comment_text=comment_text,
            from_user_id=from_user_id,
            from_username=from_username,
            media_id=media_id,
            webhook_payload=webhook_payload,
        )

        # 스팸으로 판정되면 DM 발송하지 않고 종료
        if spam_check_result.get("is_spam"):
            logger.info(f"Comment {comment_id} identified as spam, skipping DM")
            return spam_check_result

        # === 2단계: DM 자동발송 로직 ===
        # 해당 미디어에 대한 활성 캠페인 조회
        active_campaigns = AutoDMCampaign.objects.filter(
            media_id=media_id, status=AutoDMCampaign.Status.ACTIVE
        ).select_related("ig_connection")

        if not active_campaigns.exists():
            logger.info(f"No active campaign found for media_id: {media_id}")
            return {"status": "skipped", "reason": "No active campaign"}

        results = []
        for campaign in active_campaigns:
            result = _process_single_campaign(
                campaign=campaign,
                comment_id=comment_id,
                comment_text=comment_text,
                from_user_id=from_user_id,
                from_username=from_username,
                webhook_payload=webhook_payload,
            )
            results.append(result)

        return {"status": "completed", "processed_campaigns": len(results), "results": results}

    except Exception as e:
        logger.exception(f"Error processing comment webhook: {e}")
        raise


def _check_and_handle_spam(
    comment_id: str,
    comment_text: str,
    from_user_id: str,
    from_username: str,
    media_id: str,
    webhook_payload: dict,
) -> dict:
    """
    스팸 검사 및 처리

    Returns:
        {
            "is_spam": bool,
            "status": str,
            "spam_filter_processed": bool,
            ...
        }
    """
    try:
        # 미디어를 소유한 Instagram 계정 찾기
        # webhook의 media_id로 해당 계정 찾기 (실제로는 Instagram API로 조회해야 하지만,
        # 여기서는 active campaign이 있는 계정을 기준으로 함)
        campaigns = (
            AutoDMCampaign.objects.filter(media_id=media_id).select_related("ig_connection").first()
        )

        if not campaigns:
            # 캠페인이 없으면 스팸 필터도 적용 안 함
            return {"is_spam": False, "spam_filter_processed": False}

        ig_connection = campaigns.ig_connection

        # 스팸 필터 설정 조회
        try:
            spam_filter = SpamFilterConfig.objects.get(ig_connection=ig_connection)
        except SpamFilterConfig.DoesNotExist:
            # 스팸 필터 설정이 없으면 패스
            return {"is_spam": False, "spam_filter_processed": False}

        # 스팸 필터가 비활성화되어 있으면 패스
        if not spam_filter.is_active():
            return {
                "is_spam": False,
                "spam_filter_processed": False,
                "reason": "Spam filter inactive",
            }

        # 스팸 검사 수행
        is_spam, spam_reasons = SpamDetectionService.is_spam(
            text=comment_text,
            spam_keywords=spam_filter.spam_keywords,
            check_urls=spam_filter.block_urls,
        )

        if not is_spam:
            # 스팸이 아니면 정상 처리
            return {"is_spam": False, "spam_filter_processed": True}

        # === 스팸으로 판정됨 ===
        logger.info(f"Spam detected in comment {comment_id}: {spam_reasons}")

        # 스팸 로그 생성
        spam_log = SpamCommentLog.objects.create(
            spam_filter=spam_filter,
            comment_id=comment_id,
            comment_text=comment_text,
            commenter_user_id=from_user_id,
            commenter_username=from_username,
            media_id=media_id,
            spam_reasons=spam_reasons,
            status=SpamCommentLog.Status.DETECTED,
            webhook_payload=webhook_payload,
        )

        # 통계 업데이트
        spam_filter.increment_spam_detected()

        # 댓글 숨김 처리 시도
        try:
            api_response = InstagramCommentService.hide_comment(
                comment_id=comment_id, access_token=ig_connection.access_token
            )

            # 숨김 처리 성공
            spam_log.mark_as_hidden(api_response)
            spam_filter.increment_hidden()

            logger.info(f"Successfully hidden spam comment {comment_id}")

            return {
                "is_spam": True,
                "status": "hidden",
                "spam_filter_processed": True,
                "spam_reasons": spam_reasons,
                "spam_log_id": str(spam_log.id),
            }

        except Exception as hide_error:
            # 숨김 처리 실패
            error_msg = str(hide_error)
            spam_log.mark_as_failed(error_msg)

            logger.error(f"Failed to hide spam comment {comment_id}: {error_msg}")

            return {
                "is_spam": True,
                "status": "failed_to_hide",
                "spam_filter_processed": True,
                "spam_reasons": spam_reasons,
                "error": error_msg,
                "spam_log_id": str(spam_log.id),
            }

    except Exception as e:
        logger.exception(f"Error in spam check: {e}")
        return {"is_spam": False, "spam_filter_processed": False, "error": str(e)}


def _process_single_campaign(
    campaign: AutoDMCampaign,
    comment_id: str,
    comment_text: str,
    from_user_id: str,
    from_username: str,
    webhook_payload: dict,
) -> dict:
    """
    단일 캠페인에 대한 DM 발송 처리

    Returns:
        처리 결과
    """
    try:
        # 1. 중복 체크 (이미 DM을 보낸 댓글인지) - 실험용으로 비활성화
        # existing_log = SentDMLog.objects.filter(campaign=campaign, comment_id=comment_id).first()

        # if existing_log:
        #     logger.info(f"DM already sent for comment {comment_id} in campaign {campaign.id}")
        #     return {"campaign_id": str(campaign.id), "status": "skipped", "reason": "Already sent"}

        # 2. 발송 제한 체크 (시간당 최대 발송 수)
        if not campaign.can_send_more():
            logger.warning(f"Campaign {campaign.id} reached hourly send limit")

            # 스킵 로그 생성
            SentDMLog.objects.create(
                campaign=campaign,
                comment_id=comment_id,
                comment_text=comment_text,
                recipient_user_id=from_user_id,
                recipient_username=from_username,
                message_sent=campaign.message_template,
                status=SentDMLog.Status.SKIPPED,
                error_message="Hourly send limit reached",
                webhook_payload=webhook_payload,
            )

            return {
                "campaign_id": str(campaign.id),
                "status": "skipped",
                "reason": "Hourly limit reached",
            }

        # 3. 로그 생성 (PENDING 상태)
        dm_log = SentDMLog.objects.create(
            campaign=campaign,
            comment_id=comment_id,
            comment_text=comment_text,
            recipient_user_id=from_user_id,
            recipient_username=from_username,
            message_sent=campaign.message_template,
            status=SentDMLog.Status.PENDING,
            webhook_payload=webhook_payload,
        )

        # 4. Instagram Connection 확인
        ig_connection = campaign.ig_connection
        if ig_connection.status != IGAccountConnection.Status.ACTIVE:
            error_msg = f"Instagram connection is not active: {ig_connection.status}"
            logger.error(error_msg)
            dm_log.mark_as_failed(error_msg)
            campaign.increment_failed()
            return {"campaign_id": str(campaign.id), "status": "failed", "reason": error_msg}

        # 5. DM 발송
        try:
            api_response = InstagramMessagingService.send_dm_via_comment(
                ig_user_id=ig_connection.external_account_id,
                comment_id=comment_id,
                message_text=campaign.message_template,
                access_token=ig_connection.access_token,
            )

            # 6. 성공 처리
            dm_log.mark_as_sent(api_response)
            campaign.increment_sent()

            logger.info(
                f"DM sent successfully: campaign={campaign.id}, "
                f"recipient={from_username}, comment={comment_id}"
            )

            return {
                "campaign_id": str(campaign.id),
                "status": "sent",
                "recipient": from_username,
                "message_id": api_response.get("message_id"),
            }

        except Exception as api_error:
            # 7. 실패 처리
            error_msg = str(api_error)
            error_code = getattr(api_error, "code", "")

            # API 응답에서 에러 정보 추출
            api_response_data = {}
            if hasattr(api_error, "response"):
                try:
                    api_response_data = api_error.response.json()
                    error_msg = api_response_data.get("error", {}).get("message", error_msg)
                    error_code = str(api_response_data.get("error", {}).get("code", error_code))
                except:
                    pass

            dm_log.mark_as_failed(error_msg, error_code, api_response_data)
            campaign.increment_failed()

            logger.error(
                f"DM send failed: campaign={campaign.id}, "
                f"recipient={from_username}, error={error_msg}"
            )

            return {
                "campaign_id": str(campaign.id),
                "status": "failed",
                "reason": error_msg,
                "error_code": error_code,
            }

    except Exception as e:
        logger.exception(f"Error in _process_single_campaign: {e}")
        return {
            "campaign_id": str(campaign.id) if campaign else "unknown",
            "status": "error",
            "reason": str(e),
        }
