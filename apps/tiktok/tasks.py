"""Celery tasks — TikTok Business API comment moderation."""

from __future__ import annotations

import logging

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from apps.core.spam_detection import detect_spam, rule_config_from_model_attrs

from .models import (
    TikTokAccountConnection,
    TikTokBlockedWord,
    TikTokCommentLog,
    TikTokSpamFilterConfig,
)
from .services import (
    MockTikTokProvider,
    TikTokAdCommentService,
    TikTokAPIError,
    TikTokBlockedWordService,
    ensure_fresh_token,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Comment fetch + screen
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def fetch_and_screen_comments(
    connection_id: str,
    *,
    ad_id: str = "",
    page: int = 1,
    page_size: int = 20,
):
    """
    Pull a page of ad comments and screen each one against the configured
    spam filter. Detected spam is auto-moderated according to the filter's
    default_action.
    """
    connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
    if not connection:
        logger.warning("tiktok.fetch_and_screen: connection not found id=%s", connection_id)
        return

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
        ensure_fresh_token(connection)
        if MockTikTokProvider.is_mock_mode():
            payload = MockTikTokProvider.list_comments(connection.external_account_id)
        else:
            filtering = {"ad_id": ad_id} if ad_id else None
            payload = TikTokAdCommentService.list(
                connection,
                filtering=filtering,
                page=page,
                page_size=page_size,
            )
    except TikTokAPIError as e:
        logger.error("tiktok.fetch_and_screen: api error %s", e)
        return

    for entry in payload.get("list", []):
        comment_id = entry.get("comment_id") or entry.get("id")
        if not comment_id:
            continue
        log, _ = TikTokCommentLog.objects.get_or_create(
            connection=connection,
            external_comment_id=comment_id,
            defaults={
                "advertiser_id": connection.external_account_id,
                "ad_id": entry.get("ad_id", ""),
                "creative_id": entry.get("creative_id", ""),
                "parent_comment_id": entry.get("parent_comment_id", ""),
                "commenter_external_id": entry.get("user_id", ""),
                "commenter_username": entry.get("username", ""),
                "text": entry.get("text", ""),
                "status": TikTokCommentLog.Status.PENDING,
            },
        )
        if log.status not in (
            TikTokCommentLog.Status.PENDING,
            TikTokCommentLog.Status.CLEAN,
        ):
            continue  # already moderated; skip re-screening

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
        elif config and config.default_action == TikTokSpamFilterConfig.Action.DELETE:
            log.mark_detected(verdict.score, verdict.reasons)
            moderate_comment.delay(str(log.id), "delete")
        else:
            log.mark_review(verdict.score, verdict.reasons)

        if config:
            TikTokSpamFilterConfig.objects.filter(id=config.id).update(
                total_spam_detected=F("total_spam_detected") + 1,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Apply a moderation action to a single comment
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def moderate_comment(self, log_id: str, action: str):
    """
    ``action`` ∈ {"hide", "show", "delete"}.

    "delete" requires the comment to belong to the authorized brand
    (TikTok does not allow deleting fan/viewer comments on someone else's
    feed). When the API rejects deletion we fall through to "hide" as a
    safe fallback.
    """
    log = (
        TikTokCommentLog.objects.select_related("connection")
        .filter(id=log_id)
        .first()
    )
    if not log:
        return

    connection = log.connection
    try:
        ensure_fresh_token(connection)
        if MockTikTokProvider.is_mock_mode():
            if action == "delete":
                response = MockTikTokProvider.delete_comments([log.external_comment_id])
            else:
                response = MockTikTokProvider.set_comment_status(
                    [log.external_comment_id], "HIDE" if action == "hide" else "SHOW",
                )
        else:
            if action == "delete":
                response = TikTokAdCommentService.delete(
                    connection, comment_ids=[log.external_comment_id],
                )
            else:
                response = TikTokAdCommentService.set_status(
                    connection,
                    comment_ids=[log.external_comment_id],
                    action="HIDE" if action == "hide" else "SHOW",
                )
    except TikTokAPIError as e:
        logger.error("tiktok.moderate: api error %s", e)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        log.mark_failed(str(e), response=e.response)
        return

    config = TikTokSpamFilterConfig.objects.filter(connection=connection).first()
    if action == "delete":
        log.mark_deleted(response=response)
        if config:
            TikTokSpamFilterConfig.objects.filter(id=config.id).update(
                total_deleted=F("total_deleted") + 1,
            )
    else:
        log.mark_hidden(response=response)
        if config:
            TikTokSpamFilterConfig.objects.filter(id=config.id).update(
                total_hidden=F("total_hidden") + 1,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Blocked-words sync
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def sync_blocked_words_from_tiktok(connection_id: str):
    """
    Pull the authoritative blocked-word list from TikTok and reconcile our
    local mirror (``TikTokBlockedWord`` rows).
    """
    connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
    if not connection:
        return

    try:
        ensure_fresh_token(connection)
        if MockTikTokProvider.is_mock_mode():
            data = MockTikTokProvider.list_blocked_words()
        else:
            data = TikTokBlockedWordService.list(connection)
    except TikTokAPIError as e:
        logger.error("tiktok.sync_blocked_words: %s", e)
        return

    now = timezone.now()
    remote_words = {}
    for entry in data.get("list", []):
        w = entry.get("word")
        if not w:
            continue
        remote_words[w] = entry.get("id", "")

    # Upsert remote entries.
    for word, external_id in remote_words.items():
        TikTokBlockedWord.objects.update_or_create(
            connection=connection,
            word=word,
            defaults={
                "external_id": external_id,
                "is_synced": True,
                "last_synced_at": now,
            },
        )

    # Anything in local cache no longer in remote → mark as not synced so the
    # UI can surface drift, but never delete (user might be mid-edit).
    TikTokBlockedWord.objects.filter(connection=connection).exclude(
        word__in=list(remote_words.keys())
    ).update(is_synced=False)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def push_blocked_words_to_tiktok(self, connection_id: str, words: list):
    """Push a batch of blocked words to TikTok and persist results locally."""
    connection = TikTokAccountConnection.objects.filter(id=connection_id).first()
    if not connection:
        return

    try:
        ensure_fresh_token(connection)
        if MockTikTokProvider.is_mock_mode():
            data = MockTikTokProvider.create_blocked_words(words)
        else:
            data = TikTokBlockedWordService.create(connection, words=words)
    except TikTokAPIError as e:
        logger.error("tiktok.push_blocked_words: %s", e)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e)
        return

    now = timezone.now()
    for entry in data.get("created", []) or [
        {"id": "", "word": w} for w in words
    ]:
        TikTokBlockedWord.objects.update_or_create(
            connection=connection,
            word=entry.get("word") or "",
            defaults={
                "external_id": entry.get("id", ""),
                "is_synced": True,
                "last_synced_at": now,
            },
        )
