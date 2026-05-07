"""Celery tasks — YouTube upload flow + comment moderation."""

from __future__ import annotations

import logging
import os

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from apps.core.spam_detection import detect_spam, rule_config_from_model_attrs

from .models import (
    YouTubeAccountConnection,
    YouTubeCommentLog,
    YouTubeQuotaUsage,
    YouTubeSpamFilterConfig,
    YouTubeVideoPost,
)
from .services import (
    COMMENT_SET_MODERATION_QUOTA_COST,
    COMMENT_THREADS_LIST_QUOTA_COST,
    MockYouTubeProvider,
    VIDEOS_INSERT_QUOTA_COST,
    YouTubeAPIError,
    YouTubeCommentService,
    YouTubeQuotaExceeded,
    YouTubeUploadService,
    check_quota_or_raise,
    ensure_fresh_token,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def upload_video_task(self, post_id: str):
    """Drive a YouTubeVideoPost through QUEUED → UPLOADING → PUBLISHED."""
    post = YouTubeVideoPost.objects.select_related("connection").get(id=post_id)
    connection = post.connection

    post.status = YouTubeVideoPost.Status.UPLOADING
    post.started_at = timezone.now()
    if not post.video_size_bytes and post.video_file_path:
        try:
            post.video_size_bytes = os.path.getsize(post.video_file_path)
        except OSError:
            pass
    post.save(update_fields=["status", "started_at", "video_size_bytes", "updated_at"])

    # Pre-flight quota gate.
    try:
        check_quota_or_raise(VIDEOS_INSERT_QUOTA_COST)
    except YouTubeQuotaExceeded as e:
        logger.warning("youtube.upload: quota exceeded post=%s: %s", post.id, e)
        post.mark_failed(reason=str(e), response=e.response)
        return

    try:
        ensure_fresh_token(connection)
    except Exception as e:  # noqa: BLE001 — token refresh
        logger.warning("youtube.upload: token refresh failed post=%s: %s", post.id, e)

    try:
        if MockYouTubeProvider.is_mock_mode():
            response = MockYouTubeProvider.videos_insert(post)
        else:
            response = YouTubeUploadService.upload(connection, post)
    except YouTubeAPIError as e:
        logger.error(
            "youtube.upload failed post=%s code=%s msg=%s", post.id, e.code, str(e),
        )
        post.mark_failed(reason=str(e), response=e.response)
        return

    video_id = response.get("id") or ""
    if not video_id:
        post.mark_failed(reason="YouTube returned no video id", response=response)
        return

    post.quota_units_consumed = VIDEOS_INSERT_QUOTA_COST
    YouTubeQuotaUsage.add_units(VIDEOS_INSERT_QUOTA_COST)
    post.save(update_fields=["quota_units_consumed", "updated_at"])
    post.mark_published(video_id=video_id, response=response)


# ─────────────────────────────────────────────────────────────────────────────
# Comment moderation tasks
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def fetch_and_screen_comments(connection_id: str, video_id: str):
    """
    Pull a video's top-level comments, screen each against the spam filter,
    and persist a YouTubeCommentLog row per comment.

    Quota: 1 unit per call to commentThreads.list. We don't auto-paginate in
    the MVP; one page (up to 100 threads) per invocation.
    """
    connection = YouTubeAccountConnection.objects.filter(id=connection_id).first()
    if not connection:
        logger.warning("youtube.fetch_and_screen: connection not found id=%s", connection_id)
        return

    config = YouTubeSpamFilterConfig.objects.filter(connection=connection).first()
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
        check_quota_or_raise(COMMENT_THREADS_LIST_QUOTA_COST)
        ensure_fresh_token(connection)
        if MockYouTubeProvider.is_mock_mode():
            payload = MockYouTubeProvider.comment_threads_list(video_id)
        else:
            payload = YouTubeCommentService.list_threads_by_video(connection, video_id)
            YouTubeQuotaUsage.add_units(COMMENT_THREADS_LIST_QUOTA_COST)
    except (YouTubeAPIError, YouTubeQuotaExceeded) as e:
        logger.error("youtube.fetch_and_screen: %s", e)
        return

    for thread in payload.get("items", []):
        snippet = (thread.get("snippet") or {})
        top = (snippet.get("topLevelComment") or {})
        top_snippet = (top.get("snippet") or {})

        comment_id = top.get("id")
        if not comment_id:
            continue
        log, _ = YouTubeCommentLog.objects.get_or_create(
            connection=connection,
            external_comment_id=comment_id,
            defaults={
                "external_video_id": snippet.get("videoId", video_id),
                "external_thread_id": thread.get("id", ""),
                "commenter_channel_id": (top_snippet.get("authorChannelId") or {}).get("value", ""),
                "commenter_display_name": top_snippet.get("authorDisplayName", ""),
                "text": top_snippet.get("textDisplay", ""),
                "status": YouTubeCommentLog.Status.PENDING,
            },
        )
        if log.status not in (
            YouTubeCommentLog.Status.PENDING,
            YouTubeCommentLog.Status.CLEAN,
        ):
            continue

        if rule_cfg is None:
            log.mark_clean()
            continue

        verdict = detect_spam(log.text, config=rule_cfg)
        if not verdict.is_spam:
            log.mark_clean()
            continue

        log.mark_detected(verdict.score, verdict.reasons)
        action = (config.default_action if config else YouTubeSpamFilterConfig.Action.REVIEW)
        moderate_comment.delay(str(log.id), action)
        if config:
            YouTubeSpamFilterConfig.objects.filter(id=config.id).update(
                total_spam_detected=F("total_spam_detected") + 1,
            )


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def moderate_comment(self, log_id: str, action: str):
    """
    Apply ``setModerationStatus`` to a YouTube comment.

    ``action`` ∈ {"review", "reject"} — mapped to ``heldForReview`` /
    ``rejected`` respectively. Costs 50 quota units per call.
    """
    log = (
        YouTubeCommentLog.objects.select_related("connection")
        .filter(id=log_id)
        .first()
    )
    if not log:
        return

    moderation_status = (
        "heldForReview" if action == YouTubeSpamFilterConfig.Action.REVIEW else "rejected"
    )
    config = YouTubeSpamFilterConfig.objects.filter(connection=log.connection).first()
    ban_author = bool(config and config.ban_authors_on_reject and moderation_status == "rejected")

    try:
        check_quota_or_raise(COMMENT_SET_MODERATION_QUOTA_COST)
        ensure_fresh_token(log.connection)
        if MockYouTubeProvider.is_mock_mode():
            response = MockYouTubeProvider.set_moderation_status(
                [log.external_comment_id], moderation_status, ban_author,
            )
        else:
            response = YouTubeCommentService.set_moderation_status(
                log.connection,
                [log.external_comment_id],
                moderation_status,
                ban_author=ban_author,
            )
            YouTubeQuotaUsage.add_units(COMMENT_SET_MODERATION_QUOTA_COST)
    except (YouTubeAPIError, YouTubeQuotaExceeded) as e:
        logger.error("youtube.moderate: %s", e)
        if isinstance(e, YouTubeAPIError) and self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        log.mark_failed(str(e), response=getattr(e, "response", None))
        return

    if moderation_status == "heldForReview":
        log.mark_review(response=response)
    else:
        log.mark_rejected(response=response)

    if config:
        YouTubeSpamFilterConfig.objects.filter(id=config.id).update(
            total_moderated=F("total_moderated") + 1,
        )
