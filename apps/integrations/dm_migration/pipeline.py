"""DM 캠페인 이전 — 오케스트레이터.

단계 실행 + 체크포인트 재개(stage_data 키 존재 시 스킵) + 취소 + 레이트리밋 pause(재개
디스패치) + 자기발송 제외 + 후보(DMCampaignCandidate) 생성. Celery 태스크(tasks.py)가
``run_migration(job_id, redispatch=...)`` 를 호출한다.
"""

from __future__ import annotations

import difflib
import logging
import zlib
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.ai_jobs.services.dm_campaign_assistant import sample_replies

from ..models import AutoDMCampaign, DMCampaignCandidate, DMMigrationJob, SentDMLog
from . import analyze, collect, llm, recover
from .collect import (
    Budget,
    CollectContext,
    MigrationRateLimitPause,
    MigrationTokenError,
    RateLimiter,
)

logger = logging.getLogger(__name__)

RAW_RETENTION_DAYS = 7
MAX_RATE_PAUSES = 3
MAX_OUTBOUND = 500
CANDIDATE_CONFIDENCE_THRESHOLD = 0.50  # 확장 수집 대상(강한/불확실) 하한
OWN_FUZZY_RATIO = 0.92
MIN_COMMENTS = 8  # 이보다 적으면 표본이 안 나와 판정이 불가능하다
# 공개 답글(대댓글) 변주 개수 — AI 새 캠페인(suggest_campaign_fields)의 기본값과 맞춘다.
# 같은 문장을 계속 달면 인스타 스팸 탐지에 걸리므로 1개만 주던 예전 동작은 위험했다.
PUBLIC_REPLY_VARIANTS = 50


def _reply_variants(media_id: str, llm_draft: str | None) -> list[str]:
    """공개 답글 후보를 **여러 개** 만든다 (문구 다양화).

    첫 DM 본문은 원본을 그대로 살려야 해서 변주하면 안 되지만, 공개 답글은 "방금 DM
    보내드렸어요" 류의 정형 인사라 변주해도 의미가 안 바뀐다. 오히려 같은 문장을 수백 번
    달면 인스타 스팸 탐지에 걸리므로 **다양화가 안전 장치**다(시리얼라이저 안내도 3개 이상 권장).

    문구 풀은 AI 새 캠페인과 같은 소스(``sample_replies``)를 쓴다 — LLM 미사용이라 수 ms 다.
    LLM 초안이 있으면 맨 앞에 둬 게시물 맥락을 살리고, 나머지는 풀에서 채운다.
    시드를 media_id 로 고정해 **재분석해도 같은 제안**이 나오게 한다.
    """
    seed = zlib.crc32(media_id.encode("utf-8")) if media_id else None
    out = [t.strip() for t in (llm_draft,) if t and t.strip()]
    for t in sample_replies(PUBLIC_REPLY_VARIANTS, seed=seed):
        if t not in out:
            out.append(t)
    return out[:PUBLIC_REPLY_VARIANTS]


def _recovery_to_dict(r, media: dict) -> dict:
    """PostRecovery → stage_data 직렬화(체크포인트 재개용)."""
    return {
        "media_id": r.media_id,
        "permalink": media.get("permalink", "") or "",
        "caption": (media.get("caption") or "")[:300],
        "timestamp": media.get("timestamp", ""),
        "comments_count": media.get("comments_count", 0),
        "probed": r.probed,
        "trigger": r.trigger,
        "repetition": r.repetition,
        "signal": r.is_campaign_signal,
        "offer": r.offer,
        "gate": r.gate,
        "grade": r.grade,
        "score": r.score,
        "confirm_required": r.confirm_required,
        "drops": r.drops,
        "samples": r.samples,
        "keyword_hits": r.keyword_hits,
    }


_S = DMMigrationJob.Status
_ST = DMMigrationJob.Stage


class _Canceled(Exception):
    """사용자 취소 요청 감지."""


def run_migration(job_id: str, *, redispatch=None) -> str:
    """마이그레이션 파이프라인 1회 실행(또는 체크포인트 재개). 최종 status 문자열 반환.

    redispatch(job_id: str, countdown: int) — 레이트리밋 pause 시 재개 디스패치 콜러블
    (celery task 가 apply_async 래핑 전달). None 이면(동기 테스트) pause 후 그대로 둔다.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    try:
        job = DMMigrationJob.objects.select_related("ig_connection").get(id=job_id)
    except DMMigrationJob.DoesNotExist:
        logger.warning("run_migration: job %s not found", job_id)
        return "missing"

    # 중복 실행 가드 — 다른 워커가 최근(60s 내) running 갱신 중이면 양보.
    if (
        job.status == _S.RUNNING
        and job.updated_at
        and (timezone.now() - job.updated_at) < timedelta(seconds=60)
    ):
        logger.info("run_migration: job %s appears active on another worker — skip", job_id)
        return "skipped"

    runner = _Runner(job, redispatch=redispatch)
    try:
        return runner.run()
    except SoftTimeLimitExceeded:
        logger.warning("run_migration: soft time limit — finalizing partial (job=%s)", job_id)
        runner.finalize(forced_partial=True, note="시간 제한으로 일부만 분석했습니다.")
        return job.status
    except _Canceled:
        runner.mark_canceled()
        return _S.CANCELED
    except MigrationTokenError as exc:
        # 사용자에게 보이는 문장에는 Graph 코드를 붙이지 않는다 — 프론트가 꼬리를 정규식으로
        # 잘라내고 있었다. 기술 코드는 error_code(머신 키)와 로그로 남긴다.
        logger.warning("DM이전 토큰 오류 (job=%s, graph_code=%s)", job_id, exc.code)
        runner.fail(
            "token_expired", "인스타 연결이 만료되었거나 권한이 없습니다. 계정을 다시 연결해주세요."
        )
        return _S.FAILED
    except MigrationRateLimitPause as exc:
        runner.pause(exc)
        return job.status
    except Exception as exc:  # noqa: BLE001 — 잡 단위 안전망
        logger.exception("run_migration failed (job=%s)", job_id)
        runner.fail("error", str(exc)[:500])
        return _S.FAILED


class _Runner:
    def __init__(self, job: DMMigrationJob, *, redispatch=None):
        self.job = job
        self.redispatch = redispatch
        self.conn = job.ig_connection
        self.ig = self.conn.external_account_id
        self.token = self.conn.access_token  # EncryptedTextField → 복호화
        self.mock = collect.is_mock(self.token)
        prev = job.api_budget_state or {}
        self.budget = Budget(
            caps=prev.get("caps") or dict(collect.DEFAULT_CAPS),
            made=dict(prev.get("made") or {}),
        )
        self.pacer = RateLimiter(enabled=not self.mock)
        # 취소 스냅샷: 워커 스레드는 DB 를 만지지 않아야 한다(스레드별 새 커넥션 = 테스트
        # 트랜잭션 밖 + 운영 커넥션 폭주). 스테이지 경계(메인 스레드)에서만 DB refresh 로
        # 갱신하고, ThreadPool 워커는 이 in-memory 스냅샷만 읽는다.
        self._cancel_snapshot = bool(job.cancel_requested)
        self.ctx = CollectContext(
            ig=self.ig,
            token=self.token,
            mock=self.mock,
            pacer=self.pacer,
            budget=self.budget,
            cancelled=lambda: self._cancel_snapshot,
        )
        self.sd = dict(job.stage_data or {})

    # ── 상태/저장 헬퍼 ──
    def _check_cancel(self):
        # 메인 스레드에서만 DB refresh — 스냅샷 갱신 후 취소면 중단.
        self.job.refresh_from_db(fields=["cancel_requested"])
        self._cancel_snapshot = bool(self.job.cancel_requested)
        if self._cancel_snapshot:
            raise _Canceled()

    def _budget_state(self) -> dict:
        st = dict(self.job.api_budget_state or {})
        st["caps"] = self.budget.caps
        st["made"] = self.budget.made
        return st

    def _persist(self, *, counter_fields=None):
        """stage_data + api_budget_state(+지정 카운터) 를 한 번에 저장."""
        self.job.stage_data = self.sd
        self.job.api_budget_state = self._budget_state()
        fields = ["stage_data", "api_budget_state", "updated_at"] + list(counter_fields or [])
        self.job.save(update_fields=fields)

    def _bump_llm(self, usage: dict):
        self.job.llm_calls = (self.job.llm_calls or 0) + int(usage.get("llm_calls", 0))
        self.job.llm_tokens_used = (self.job.llm_tokens_used or 0) + int(usage.get("llm_tokens", 0))

    # ── 메인 ──
    def run(self) -> str:
        job = self.job
        job.status = _S.RUNNING
        if not job.started_at:
            job.started_at = timezone.now()
        job.error_code = ""
        job.error_message = ""
        job.save(
            update_fields=["status", "started_at", "error_code", "error_message", "updated_at"]
        )

        self._stage_media()
        self._stage_estimate()
        self._stage_recover()
        self._stage_drafts()
        self.finalize()
        return job.status

    # ── 단계 1.5: 예상 소요 산출 (프론트가 진행바를 그릴 수 있게) ──
    def _stage_estimate(self):
        if self.job.estimated_seconds is not None:
            return
        self._check_cancel()
        self.job.set_stage(_ST.ESTIMATING, 8, "예상 시간을 계산하고 있습니다...")
        media = self.sd.get("media", [])
        targets = self._targets(media)
        est = recover.estimate_seconds(len(targets))
        self.job.estimated_seconds = est["seconds"]
        self.job.estimate_detail = dict(est, media_total=len(media))
        self.job.estimated_at = timezone.now()
        self.job.save(
            update_fields=["estimated_seconds", "estimate_detail", "estimated_at", "updated_at"]
        )

    def _targets(self, media: list) -> list:
        """분석 대상 게시물 — 댓글이 있고, **우리 캠페인이 걸려 있지 않은** 것.

        우리 캠페인이 도는 게시물을 넣으면 발신 DM 의 절반이 우리 것이라(실측 164/313)
        자기 DM 을 '타사 캠페인' 으로 오인해 자기증식 후보가 생긴다.
        """
        ours = set(
            AutoDMCampaign.objects.filter(ig_connection=self.conn)
            .exclude(media_id="")
            .values_list("media_id", flat=True)
        )
        return [
            m
            for m in media
            if (m.get("comments_count") or 0) >= MIN_COMMENTS and m.get("id") not in ours
        ]

    # ── 단계 2: 게시물별 정밀 복원 ──
    def _stage_recover(self):
        if "recoveries" in self.sd:
            return
        self._check_cancel()
        self.job.set_stage(_ST.COLLECTING_TARGETED_DMS, 15, "예전 DM을 찾고 있습니다...")
        targets = self._targets(self.sd.get("media", []))
        # 최신 게시물부터 — 결과가 나오는 대로 보여줄 수 있고, 수확도 최신 쪽이 높다.
        targets.sort(key=lambda m: m.get("timestamp", ""), reverse=True)

        mids, fps, tmpl_norms = _own_send_context(self.conn)
        is_own = recover.build_own_dm_matcher(mids, fps, tmpl_norms)

        out = []
        total = max(len(targets), 1)
        for i, media in enumerate(targets, 1):
            if self.budget.total_hit():
                break
            if i % 5 == 0 or i == total:
                self._check_cancel()
                self.job.set_stage(
                    _ST.COLLECTING_TARGETED_DMS,
                    15 + int(70 * i / total),
                    f"예전 DM을 찾고 있습니다... ({i}/{total})",
                )
            try:
                r = recover.recover_post(self.ctx, media, is_own_dm=is_own)
            except (MigrationTokenError, MigrationRateLimitPause):
                raise
            except Exception:  # noqa: BLE001 — 게시물 1건 실패가 잡을 죽이지 않게
                logger.exception("recover_post 실패 (media=%s)", media.get("id"))
                continue
            if not (r.found or r.is_campaign_signal):
                continue
            out.append(_recovery_to_dict(r, media))
        self.sd["recoveries"] = out
        self.job.candidates_created = 0
        self.job.dm_messages_collected = sum(
            (x["offer"] or {}).get("hits", 0) + (x["gate"] or {}).get("hits", 0) for x in out
        )
        self._persist(counter_fields=["dm_messages_collected"])

    # ── 단계 1: 미디어 ──
    def _stage_media(self):
        if "media" in self.sd:
            return
        self._check_cancel()
        self.job.set_stage(_ST.COLLECTING_MEDIA, 5, "게시물을 수집하고 있습니다...")
        media = collect.fetch_media(self.ctx, self.job.media_limit)
        self.sd["media"] = media
        self.job.media_scanned = len(media)
        self._persist(counter_fields=["media_scanned"])

    # ── 단계 3: 초안 생성 + 후보 저장 ──
    def _stage_drafts(self):
        if self.sd.get("drafts_done"):
            return
        self._check_cancel()
        self.job.set_stage(_ST.GENERATING_DRAFTS, 90, "캠페인 초안을 만들고 있습니다...")
        recs = self.sd.get("recoveries", [])
        media_by_id = {m.get("id"): m for m in self.sd.get("media", [])}
        existing = {
            c.media_id: c
            for c in AutoDMCampaign.objects.filter(ig_connection=self.conn).exclude(media_id="")
        }

        # LLM 은 '문구 다듬기' 에만 쓴다. 복원된 원문·링크·키워드는 관측값이라 건드리지 않는다.
        draft_inputs = [
            {
                "media_id": r["media_id"],
                "caption": r.get("caption", ""),
                "keywords": list((r.get("keyword_hits") or {}).keys())
                or ([r["trigger"]] if r.get("trigger") else []),
                "confidence": r.get("score", 0.5),
                "owner_reply_top": "",
                "template_text": (r.get("offer") or {}).get("text", ""),
                # 버튼이 붙으면 한도가 640자, 없으면 1000바이트 → 초안 길이 보장에 필요
                "has_button": bool((r.get("offer") or {}).get("url"))
                or bool((r.get("gate") or {}).get("is_gate")),
                "other_templates": [],
            }
            for r in recs
            if r.get("grade") in ("auto_draft", "needs_review")
        ]
        drafts, usage = llm.generate_drafts(draft_inputs, model_code=self.job.llm_model)
        self._bump_llm(usage)

        with transaction.atomic():
            self.job.candidates.all().delete()
            created = 0
            for r in recs:
                if r.get("grade") == "excluded" and not r.get("signal"):
                    continue
                created += 1
                self._create_candidate(r, media_by_id, drafts, existing)
            self.job.candidates_created = created
        self.sd["drafts_done"] = True
        self._persist(counter_fields=["candidates_created"])

    def _create_candidate(self, r: dict, media_by_id: dict, drafts: dict, existing: dict):
        mid = r["media_id"]
        media = media_by_id.get(mid, {})
        d = drafts.get(mid, {})
        offer = r.get("offer") or {}
        gate = r.get("gate") or {}
        trigger = r.get("trigger")
        keywords = d.get("keywords") or list((r.get("keyword_hits") or {}).keys())
        if not keywords and trigger:
            keywords = [trigger]
        exist = existing.get(mid)

        # ── 본문 길이 ──
        # 타사 도구는 **여러 통으로 쪼개** 보내서 한 통이 우리 한도를 넘는 원문이 나온다.
        # 우리도 게이트/오퍼를 각각 별도 DM 으로 복원하므로 한 통에 몰아넣을 일이 없고,
        # 그래도 길면 초안 생성 단계(llm._enforce_length)가 **한도 안에서 다시 써준다**.
        # 여기 fit 은 마지막 방어선일 뿐이라 사용자에게 '잘렸다' 고 알리지 않는다
        # (잘린 문장을 보여주느니 한도 안에서 말이 되는 문구를 주는 게 낫다).
        raw_opening = d.get("first_dm_draft") or offer.get("text") or ""
        has_button = bool(offer.get("url")) or bool(gate.get("is_gate"))
        opening, _ = analyze.fit_dm_text(raw_opening, has_button=has_button)
        gate_msg, _ = analyze.fit_dm_text(gate.get("text") or "", has_button=True)
        drops = list(r.get("drops") or [])
        DMCampaignCandidate.objects.create(
            job=self.job,
            ig_connection=self.conn,
            status=DMCampaignCandidate.Status.DETECTED,
            band=r.get("grade") or DMCampaignCandidate.Band.NEEDS_REVIEW,
            media_id=mid,
            media_permalink=r.get("permalink", ""),
            media_caption_excerpt=r.get("caption", "")[:300],
            media_timestamp=analyze.parse_graph_time(media.get("timestamp", "")),
            suggested_keywords=keywords[:5],
            suggested_keyword_mode=d.get("keyword_mode") or "any",
            confidence=r.get("score", 0.0),
            draft_name=d.get("name", ""),
            draft_description=d.get("description", ""),
            # 첫 DM 본문 — 오퍼 원문이 있으면 그것을, 없으면 LLM 초안을(포맷 한도에 맞춰 자름).
            draft_opening_message=opening,
            draft_public_reply_templates=_reply_variants(mid, d.get("public_reply_draft")),
            follow_up_candidates=[],
            # ── 정밀도 근거 ──
            support_hits=(offer or gate).get("hits", 0),
            support_probed=r.get("probed", 0),
            support_score=r.get("score", 0.0),
            # ── 산출물 1순위 ──
            offer_url=(offer.get("url") or "")[:1000],
            offer_button_label=(offer.get("label") or "")[:100],
            gate_detected=bool(gate.get("is_gate")),
            gate_message=gate_msg,
            gate_button_label=(gate.get("label") or "")[:100],
            confirm_required=bool(r.get("confirm_required")),
            transfer_drops=drops,
            matched_template={
                "source": "support",
                "support_hits": (offer or gate).get("hits", 0),
                "support_probed": r.get("probed", 0),
                "support_ratio": (offer or gate).get("ratio", 0),
                "first_sent_at": (r.get("samples") or [{}])[0].get("created_time", ""),
                "last_sent_at": (r.get("samples") or [{}])[-1].get("created_time", ""),
            },
            evidence_aggregates={
                "matched_comment_count": sum((r.get("keyword_hits") or {}).values()),
                "total_comment_count": r.get("comments_count", 0),
                "keyword_hit_counts": r.get("keyword_hits") or {},
                "repetition_ratio": r.get("repetition", 0),
                "dm_source": "targeted",
                "support_ratio": (offer or gate).get("ratio", 0),
                "has_existing_campaign": bool(exist),
                "existing_campaign_status": exist.status if exist else "",
            },
            evidence_raw={"sample_outbound_dms": r.get("samples", [])},
        )

    # ── 종결/상태 전이 ──
    def finalize(self, *, forced_partial: bool = False, note: str = ""):
        job = self.job
        now = timezone.now()
        partial = (
            forced_partial
            or bool(self.sd.get("failed_media_ids"))
            or bool(self.sd.get("dm_scope_missing"))
            or self.budget.total_hit()
        )
        job.status = _S.PARTIAL if partial else _S.READY
        job.stage = _ST.COMPLETED
        job.progress = 100
        job.finished_at = now
        job.raw_expires_at = now + timedelta(days=RAW_RETENTION_DAYS)
        job.message = note or (
            "일부만 분석했습니다 (일부 데이터 수집 실패)." if partial else "분석이 완료되었습니다."
        )
        job.stage_data = self.sd
        job.api_budget_state = self._budget_state()
        job.save()

    def pause(self, exc: MigrationRateLimitPause):
        job = self.job
        job.rate_limit_pauses = (job.rate_limit_pauses or 0) + 1
        st = self._budget_state()
        st.setdefault("throttle_events", []).append({"code": exc.code, "stage": job.stage})
        job.api_budget_state = st
        job.stage_data = self.sd  # 체크포인트 보존
        if job.rate_limit_pauses > MAX_RATE_PAUSES:
            job.save(
                update_fields=["rate_limit_pauses", "api_budget_state", "stage_data", "updated_at"]
            )
            self.finalize(forced_partial=True, note="레이트리밋이 반복되어 일부만 분석했습니다.")
            return
        countdown = 900 * (2 ** (job.rate_limit_pauses - 1))
        job.status = _S.PAUSED_RATE_LIMITED
        job.resume_at = timezone.now() + timedelta(seconds=countdown)
        job.message = "잠시 요청이 많아 대기 중입니다. 곧 자동으로 이어서 분석합니다."
        job.save(
            update_fields=[
                "status",
                "resume_at",
                "rate_limit_pauses",
                "message",
                "api_budget_state",
                "stage_data",
                "updated_at",
            ]
        )
        if self.redispatch:
            self.redispatch(str(job.id), countdown)

    def fail(self, code: str, message: str):
        job = self.job
        now = timezone.now()
        job.status = _S.FAILED
        job.error_code = code
        job.error_message = message
        job.finished_at = now
        job.raw_expires_at = now + timedelta(days=RAW_RETENTION_DAYS)
        job.message = "분석에 실패했습니다."
        job.stage_data = self.sd
        job.api_budget_state = self._budget_state()
        job.save()

    def mark_canceled(self):
        job = self.job
        now = timezone.now()
        job.status = _S.CANCELED
        job.finished_at = now
        job.raw_expires_at = now + timedelta(days=RAW_RETENTION_DAYS)
        job.message = "사용자가 분석을 취소했습니다."
        job.stage_data = self.sd
        job.api_budget_state = self._budget_state()
        job.save()


# ══════════════ 모듈 헬퍼 ══════════════


def _trim_comments(comments: list[dict], *, cap: int = 250, text_cap: int = 200) -> list[dict]:
    """원본 최소보관 — media당 250개·텍스트 200자 절단."""
    out = []
    for c in comments[:cap]:
        out.append(
            {
                "id": c.get("id"),
                "text": (c.get("text", "") or "")[:text_cap],
                "username": c.get("username", ""),
                "timestamp": c.get("timestamp", ""),
                "parent_id": c.get("parent_id"),
                "from": {"id": str((c.get("from") or {}).get("id") or "")},
            }
        )
    return out


def _template_meta(tmpl: dict | None) -> dict:
    if not tmpl:
        return {}
    return {
        "template_id": tmpl.get("template_id"),
        "cluster_size": tmpl.get("count", 0),
        "conversation_count": tmpl.get("conversation_count", 0),
        "variable_slots": tmpl.get("variable_slots", []),
        "first_sent_at": tmpl.get("first_sent_at", ""),
        "last_sent_at": tmpl.get("last_sent_at", ""),
    }


def _strip_urls(text: str) -> str:
    return analyze._URL_RE.sub("[링크]", text or "")


def _own_send_context(conn):
    """자기(TurnFlow) 발송 제외용 — SentDMLog mid/echo + 텍스트 지문 + 캠페인 템플릿 정규화."""
    mids: set = set()
    fps: set = set()
    tmpl_norms: list = []
    logs = SentDMLog.objects.filter(campaign__ig_connection=conn)
    for mid, echo, text in logs.values_list(
        "meta_message_id", "echo_mid", "message_sent"
    ).iterator():
        if mid:
            mids.add(mid)
        if echo:
            mids.add(echo)
        if text:
            fps.add(analyze.fingerprint(text))
    for camp in AutoDMCampaign.objects.filter(ig_connection=conn):
        texts = list(camp.opening_message_templates or [])
        for extra in (
            camp.opening_message_template,
            camp.message_template,
            camp.reward_message_template,
        ):
            if extra:
                texts.append(extra)
        for t in texts:
            if t:
                fps.add(analyze.fingerprint(t))
                tmpl_norms.append(analyze.placeholder_normalize(t))
    return mids, fps, tmpl_norms


def _is_own(msg: dict, mids: set, fps: set, tmpl_norms: list) -> bool:
    if msg.get("msg_id") and msg["msg_id"] in mids:
        return True
    if analyze.fingerprint(msg.get("text", "")) in fps:
        return True
    n = analyze.placeholder_normalize(msg.get("text", ""))
    if not n:
        return False
    for tn in tmpl_norms:
        if abs(len(n) - len(tn)) / max(len(n), len(tn), 1) > 0.30:
            continue
        if difflib.SequenceMatcher(None, n, tn).ratio() >= OWN_FUZZY_RATIO:
            return True
    return False
