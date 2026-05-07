"""Celery tasks — TikTok publish flow + comment moderation."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from apps.core.spam_detection import detect_spam, rule_config_from_model_attrs

from .models import (
    TikTokAccountConnection,
    TikTokCommentLog,
    TikTokSpamFilterConfig,
    TikTokVideoPost,
)
from .services import (
    MockTikTokProvider,
    TikTokAPIError,
    TikTokCommentService,
    TikTokContentPostingService,
    ensure_fresh_token,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=5, default_retry_delay=30)
def publish_video_task(self, post_id: str):
    """
    Drive a TikTokVideoPost from QUEUED → PUBLISHED.

    For PULL_FROM_URL we just call ``video/init/`` and let TikTok pull. For
    FILE_UPLOAD we'd ordinarily PUT chunks here too, but the MVP supports
    PULL_FROM_URL only end-to-end (FILE_UPLOAD just initializes and then
    relies on follow-up PUTs done by a separate uploader — out of MVP scope).
    """
    post = TikTokVideoPost.objects.select_related("connection").get(id=post_id)
    connection = post.connection

    # Refresh the token if it's near expiry.
    try:
        ensure_fresh_token(connection)
    except Exception as e:
        logger.warning("tiktok.publish: token refresh failed for post=%s: %s", post.id, e)

    post.status = TikTokVideoPost.Status.INITIATING
    post.initiated_at = timezone.now()
    post.save(update_fields=["status", "initiated_at", "updated_at"])

    try:
        if MockTikTokProvider.is_mock_mode():
            init_resp = MockTikTokProvider.init_publish(post)
        else:
            if post.source_type == TikTokVideoPost.SourceType.PULL_FROM_URL:
                init_resp = TikTokContentPostingService.init_pull_from_url(connection, post)
            else:
                init_resp = TikTokContentPostingService.init_file_upload(connection, post)

        publish_id = (init_resp.get("data") or {}).get("publish_id", "")
        upload_url = (init_resp.get("data") or {}).get("upload_url", "") or ""
        post.publish_id = publish_id
        post.upload_url = upload_url
        post.api_response = init_resp
        post.status = (
            TikTokVideoPost.Status.UPLOADING
            if post.source_type == TikTokVideoPost.SourceType.FILE_UPLOAD
            else TikTokVideoPost.Status.PROCESSING
        )
        post.next_check_at = timezone.now() + timedelta(seconds=10)
        post.save(
            update_fields=[
                "publish_id",
                "upload_url",
                "api_response",
                "status",
                "next_check_at",
                "updated_at",
            ]
        )

    except TikTokAPIError as e:
        logger.error("tiktok.publish failed for post=%s code=%s msg=%s", post.id, e.code, str(e))
        post.mark_failed(reason=str(e), response=e.response)
        return

    # Schedule a status poll.
    poll_publish_status_task.apply_async(args=[str(post.id)], countdown=10)


@shared_task(bind=True, max_retries=20, default_retry_delay=15)
def poll_publish_status_task(self, post_id: str):
    """Poll TikTok for publish status until PUBLISH_COMPLETE / FAILED / max retries."""
    post = TikTokVideoPost.objects.select_related("connection").get(id=post_id)
    if post.status in (TikTokVideoPost.Status.PUBLISHED, TikTokVideoPost.Status.FAILED):
        return

    connection = post.connection
    try:
        if MockTikTokProvider.is_mock_mode():
            data = MockTikTokProvider.fetch_status(post.publish_id, force_published=True)
        else:
            ensure_fresh_token(connection)
            data = TikTokContentPostingService.fetch_status(connection, post.publish_id)
    except TikTokAPIError as e:
        logger.warning("tiktok.poll: api error for post=%s: %s", post.id, e)
        if self.request.retries >= self.max_retries:
            post.mark_failed(reason=f"poll exceeded retries: {e}", response=e.response)
            return
        raise self.retry(exc=e)

    status_str = (data.get("status") or "").upper()
    if status_str == "PUBLISH_COMPLETE":
        ids = data.get("publicaly_available_post_id") or []
        post.mark_published(tiktok_video_id=(ids[0] if ids else ""), response=data)
        return
    if status_str in ("FAILED", "PUBLISH_FAILED"):
        post.mark_failed(reason=data.get("fail_reason", "TikTok reported FAILED"), response=data)
        return

    # Still processing — schedule another poll.
    if self.request.retries >= self.max_retries:
        post.mark_failed(reason="Polling exceeded retry budget", response=data)
        return
    raise self.retry(countdown=15, max_retries=self.max_retries)


# ─────────────────────────────────────────────────────────────────────────────
# Comment moderation tasks
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def fetch_and_screen_comments(connection_id: str, video_id: str):
    """
    Pull a video's comments via the (mocked / business) API, screen each one
    against the spam filter, and persist a TikTokCommentLog row per comment.
    """
    connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
    if not connection:
        logger.warning("tiktok.fetch_and_screen: connection not found id=%s", connection_id)
        return

    # Optional: per-connection spam filter config. If absent → only persistence,
    # no auto-moderation.
    config = TikTokSpamFilterConfig.objects.filter(connection=connection).first()
    rule_cfg = (
        rule_config_from_model_attrs(
            block_urls=config.block_urls,
            block_shortened_urls=config.block_shortened_urls,
            spam_keywords=config.spam_keywords or [],
            min_length=config.min_length,
            max_emoji_ratio=config.max_emoji_ratio,
            max_mentions=config.max_mentions,
            score_threshold=config.score_threshold,
        )
        if config and config.is_active()
        else None
    )

    try:
        payload = TikTokCommentService.list_comments(connection, video_id)
    except TikTokAPIError as e:
        logger.error("tiktok.fetch_and_screen: api error %s", e)
        return

    for entry in payload.get("comments", []):
        comment_id = entry.get("id")
        if not comment_id:
            continue
        log, _ = TikTokCommentLog.objects.get_or_create(
            connection=connection,
            external_comment_id=comment_id,
            defaults={
                "external_video_id": video_id,
                "commenter_external_id": (entry.get("user") or {}).get("open_id", ""),
                "commenter_username": (entry.get("user") or {}).get("display_name", ""),
                "text": entry.get("text", ""),
                "status": TikTokCommentLog.Status.PENDING,
            },
        )
        if log.status not in (
            TikTokCommentLog.Status.PENDING,
            TikTokCommentLog.Status.CLEAN,
        ):
            # Already screened or moderated — skip.
            continue

        if rule_cfg is None:
            log.mark_clean()
            continue

        verdict = detect_spam(log.text, config=rule_cfg)
        if not verdict.is_spam:
            log.mark_clean()
            continue

        if config and config.default_action == TikTokSpamFilterConfig.Action.HIDE:
            log.mark_detected(verdict.score, verdict.reasons)
            moderate_comment.delay(str(log.id), "hide")
        else:
            log.mark_review(verdict.score, verdict.reasons)
        # Stats counter (best-effort).
        if config:
            TikTokSpamFilterConfig.objects.filter(id=config.id).update(
                total_spam_detected=F("total_spam_detected") + 1,
            )


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def moderate_comment(self, log_id: str, action: str):
    """
    Apply a moderation ``action`` ("hide") to the TikTok comment.

    NOTE: TikTok's organic API doesn't allow deleting fan comments. ``delete``
    requests collapse to ``hide`` here as a safe fallback.
    """
    log = TikTokCommentLog.objects.select_related("connection").filter(id=log_id).first()
    if not log:
        return

    try:
        if action in ("hide", "delete"):
            response = TikTokCommentService.hide_comment(log.connection, log.external_comment_id)
        else:
            log.mark_failed(f"Unknown action: {action}")
            return
    except TikTokAPIError as e:
        logger.error("tiktok.moderate: api error %s", e)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        log.mark_failed(str(e), response=e.response)
        return

    log.mark_hidden(response=response)
    cfg = TikTokSpamFilterConfig.objects.filter(connection=log.connection).first()
    if cfg:
        TikTokSpamFilterConfig.objects.filter(id=cfg.id).update(
            total_hidden=F("total_hidden") + 1,
        )
