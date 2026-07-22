"""DM 캠페인 이전 — 오케스트레이터.

단계 실행 + 체크포인트 재개(stage_data 키 존재 시 스킵) + 취소 + 레이트리밋 pause(재개
디스패치) + 자기발송 제외 + 후보(DMCampaignCandidate) 생성. Celery 태스크(tasks.py)가
``run_migration(job_id, redispatch=...)`` 를 호출한다.
"""

from __future__ import annotations

import difflib
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ..models import AutoDMCampaign, DMCampaignCandidate, DMMigrationJob, SentDMLog
from . import analyze, collect, llm
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
        runner.fail("token_expired", f"IG 토큰 오류로 분석을 중단했습니다 (code={exc.code}).")
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
        self._stage_comments_first()
        self._stage_classify_and_expand()
        self._stage_targeted_dms()
        self._stage_conversations()
        self._stage_cluster()
        self._stage_match()
        self._stage_drafts()
        self.finalize()
        return job.status

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

    # ── 단계 2: 댓글 1차 ──
    def _stage_comments_first(self):
        if "comments" in self.sd:
            return
        self._check_cancel()
        self.job.set_stage(_ST.COLLECTING_COMMENTS, 12, "댓글을 수집하고 있습니다...")
        media = self.sd.get("media", [])
        first, failed = collect.fetch_comments_first_pass(self.ctx, media)
        # media별 텍스트 200자 절단 + media당 250개 캡(원본 최소보관).
        comments = {}
        after = {}
        for mid, v in first.items():
            comments[mid] = _trim_comments(v["comments"])
            after[mid] = v["paging_after"]
        self.sd["comments"] = comments
        self.sd["comments_after"] = after
        self.sd["failed_media_ids"] = failed
        self.job.comments_collected = sum(len(c) for c in comments.values())
        self._persist(counter_fields=["comments_collected"])

    # ── 단계 3: 분류 + 후보 댓글 확장 ──
    def _stage_classify_and_expand(self):
        if "verdicts" in self.sd:
            return
        self._check_cancel()
        self.job.set_stage(_ST.CLASSIFYING_POSTS, 35, "게시물을 분류하고 있습니다...")
        media_by_id = {m.get("id"): m for m in self.sd.get("media", [])}
        comments = self.sd.get("comments", {})
        evidence = {
            mid: analyze.comment_evidence(
                media=media_by_id.get(mid, {}), comments=cs, own_account_id=self.ig
            )
            for mid, cs in comments.items()
        }
        verdicts, usage = llm.classify_posts(list(evidence.values()), model_code=self.job.llm_model)
        self._bump_llm(usage)

        # 강한/불확실 후보만 댓글 확장(포화 기반 조기 종료).
        cand_meta = []
        for v in verdicts:
            if v.get("is_campaign") or v.get("confidence", 0) >= CANDIDATE_CONFIDENCE_THRESHOLD:
                mid = v["media_id"]
                known = {
                    analyze.normalize_comment(c.get("text", "")) for c in comments.get(mid, [])
                }
                known.discard("")
                cand_meta.append(
                    {
                        "media_id": mid,
                        "after": self.sd.get("comments_after", {}).get(mid),
                        "comments": list(comments.get(mid, [])),
                        "known_norms": known,
                        "keywords": v.get("keywords", []),
                    }
                )
        collect.fetch_comments_expand(self.ctx, cand_meta)
        for c in cand_meta:
            comments[c["media_id"]] = _trim_comments(c["comments"])
            evidence[c["media_id"]] = analyze.comment_evidence(
                media=media_by_id.get(c["media_id"], {}),
                comments=comments[c["media_id"]],
                own_account_id=self.ig,
            )

        self.sd["comments"] = comments
        self.sd["evidence"] = evidence
        self.sd["verdicts"] = verdicts
        self.job.comments_collected = sum(len(c) for c in comments.values())
        self._persist(counter_fields=["comments_collected"])

    # ── 단계 3.5: 타겟 DM 복원 (게시물 댓글러 → 그가 받은 발신 DM) ──
    def _stage_targeted_dms(self):
        if "targeted_dms" in self.sd:
            return
        self._check_cancel()
        self.job.set_stage(_ST.COLLECTING_TARGETED_DMS, 48, "타겟 DM 을 복원하고 있습니다...")
        verdicts = self.sd.get("verdicts", [])
        # 후보(캠페인 판정 or 불확실) 게시물의 **초기(오래된) 댓글러** 를 잡는다.
        # 캠페인 시점 참여자가 DM 을 받았고, 그들은 댓글 목록 tail 에 있다(실측 38% vs 1.7%).
        cand_media = [
            v["media_id"]
            for v in verdicts
            if v.get("is_campaign") or v.get("confidence", 0) >= CANDIDATE_CONFIDENCE_THRESHOLD
        ]
        media_commenters = collect.fetch_oldest_commenters(self.ctx, cand_media)
        raw = collect.fetch_targeted_dms(self.ctx, media_commenters)
        # 자기발송(우리 캠페인 DM)·노이즈(게이트/인사) 제외 → 외부 툴/수동 발신만 남긴다.
        mids, fps, tmpl_norms = _own_send_context(self.conn)
        cleaned = {}
        for mid, rec in raw.items():
            keep = [
                m
                for m in rec
                if not _is_own(m, mids, fps, tmpl_norms)
                and not analyze.is_noise_dm(analyze.placeholder_normalize(m.get("text", "")))
            ]
            if keep:
                cleaned[mid] = keep
        self.sd["targeted_dms"] = cleaned
        self.job.dm_messages_collected = (self.job.dm_messages_collected or 0) + sum(
            len(v) for v in cleaned.values()
        )
        self._persist(counter_fields=["dm_messages_collected"])

    # ── 단계 4: DM 대화 + 자기발송 제외 ──
    def _stage_conversations(self):
        if "outbound_dms" in self.sd:
            return
        self._check_cancel()
        self.job.set_stage(_ST.COLLECTING_DM_CONVERSATIONS, 55, "DM 기록을 수집하고 있습니다...")
        conv = collect.fetch_conversations(self.ctx)
        mids, fps, tmpl_norms = _own_send_context(self.conn)
        filtered = []
        excluded = 0
        for m in conv["outbound"]:
            if _is_own(m, mids, fps, tmpl_norms):
                excluded += 1
                continue
            filtered.append(m)
            if len(filtered) >= MAX_OUTBOUND:
                break
        self.sd["outbound_dms"] = filtered
        self.sd["dm_scope_missing"] = conv["scope_missing"]
        self.sd["own_sends_excluded"] = excluded
        self.job.conversations_scanned = conv["conversations_scanned"]
        self.job.dm_messages_collected = len(filtered)
        self._persist(counter_fields=["conversations_scanned", "dm_messages_collected"])

    # ── 단계 5: 템플릿 군집화 ──
    def _stage_cluster(self):
        if "templates" in self.sd:
            return
        self._check_cancel()
        self.job.set_stage(_ST.CLUSTERING_DM_TEMPLATES, 70, "DM 패턴을 분석하고 있습니다...")
        outbound = self.sd.get("outbound_dms", [])
        templates = analyze.cluster_templates(outbound)
        verify, usage = llm.verify_templates(templates, model_code=self.job.llm_model)
        self._bump_llm(usage)
        templates = [
            t
            for t in templates
            if verify.get(t["template_id"], {}).get("is_campaign_template", True)
        ]
        self.sd["templates"] = templates
        self.job.templates_found = len(templates)
        self._persist(counter_fields=["templates_found"])

    # ── 단계 6: 매칭 ──
    def _stage_match(self):
        if "matches" in self.sd:
            return
        self._check_cancel()
        self.job.set_stage(_ST.MATCHING_CAMPAIGNS, 82, "게시물과 DM을 매칭하고 있습니다...")
        media_by_id = {m.get("id"): m for m in self.sd.get("media", [])}
        comments = self.sd.get("comments", {})
        evidence = self.sd.get("evidence", {})
        templates = self.sd.get("templates", [])
        verdicts = self.sd.get("verdicts", [])

        prelim = []
        uncertain = []
        for v in verdicts:
            if not v.get("is_campaign"):
                continue
            mid = v["media_id"]
            media = media_by_id.get(mid, {})
            cs = comments.get(mid, [])
            hits = analyze.keyword_hit_counts(cs, v.get("keywords", []))
            ev = evidence.get(mid) or analyze.comment_evidence(
                media=media, comments=cs, own_account_id=self.ig
            )
            cand = {
                "media_id": mid,
                "timestamp": analyze.parse_graph_time(media.get("timestamp", "")),
                "keywords": v.get("keywords", []),
                "comment_days": ev.get("comment_days", []),
                "keyword_comment_count": sum(hits.values()),
            }
            best = analyze.match_candidate(cand, templates)
            prelim.append((v, ev, hits, best))
            if best and 0.35 <= best["python_score"] <= 0.75:
                uncertain.append(
                    {
                        "media_id": mid,
                        "template_id": best["template"]["template_id"],
                        "caption": ev.get("caption_excerpt", ""),
                        "keywords": v.get("keywords", []),
                        "template_text": best["template"]["representative"],
                    }
                )
        fits, usage = llm.judge_fit(uncertain, model_code=self.job.llm_model)
        self._bump_llm(usage)

        targeted = self.sd.get("targeted_dms", {})
        matches = []
        for v, _ev, hits, best in prelim:
            mid = v["media_id"]
            # 타겟 복원 DM 이 있으면 최우선 — 게시물에 직접 연결된 실제 발신 DM.
            opening = analyze.pick_recovered_opening(targeted.get(mid) or [])
            if opening:
                # 강함 = URL(자료 링크) 포함 or 2명 이상에게 동일 발송 → 자동화 템플릿 확실.
                # 약함(수동 1회성 메시지)은 그대로 LLM 에 먹이면 환각 위험 → 초안은 캡션/키워드로
                # 생성하되, 복원 원문은 근거로 보존한다.
                strong = opening["has_url"] or opening["recipients"] >= 2
                matches.append(
                    {
                        "media_id": mid,
                        "band": "auto_draft" if strong else "needs_review",
                        "final_score": 0.9 if strong else 0.55,
                        "confidence": v.get("confidence", 0),
                        "keywords": v.get("keywords", []),
                        "keyword_hit_counts": hits,
                        "template_id": f"tg_{mid}",
                        "template_source": "targeted" if strong else "targeted_weak",
                        "template_text": opening["representative"] if strong else "",
                        "matched_template": {
                            "template_id": f"tg_{mid}",
                            "source": "targeted",
                            "cluster_size": opening["count"],
                            "conversation_count": opening["recipients"],
                            "has_url": opening["has_url"],
                            "representative": opening["representative"],
                        },
                        "recovered_samples": [
                            {"text": d.get("text", ""), "created_time": d.get("created_time", "")}
                            for d in (targeted.get(mid) or [])[:3]
                        ],
                        "signals": {
                            "source": "targeted",
                            "recipients": opening["recipients"],
                            "has_url": opening["has_url"],
                        },
                    }
                )
                continue
            # 폴백: 전역 템플릿 매칭(시간/볼륨/키워드 + 불확실 밴드 LLM 적합도).
            if best:
                final = best["python_score"]
                key = (mid, best["template"]["template_id"])
                if key in fits:
                    final = 0.6 * best["python_score"] + 0.4 * fits[key]
                band = analyze.score_band(final, v.get("confidence", 0))
                tmpl = best["template"]
            else:
                final, band, tmpl = 0.0, "excluded", None
            matches.append(
                {
                    "media_id": mid,
                    "band": band,
                    "final_score": round(final, 3),
                    "confidence": v.get("confidence", 0),
                    "keywords": v.get("keywords", []),
                    "keyword_hit_counts": hits,
                    "template_id": tmpl["template_id"] if tmpl else None,
                    "template_source": "global",
                    "template_text": tmpl["representative"] if tmpl else "",
                    "matched_template": _template_meta(tmpl),
                    "signals": (
                        {
                            k: best[k]
                            for k in (
                                "time_score",
                                "volume_score",
                                "keyword_in_template",
                                "sends_in_window",
                            )
                        }
                        if best
                        else {}
                    ),
                }
            )
        self.sd["matches"] = matches
        self._persist()

    # ── 단계 7: 초안 생성 + 후보 저장 ──
    def _stage_drafts(self):
        if self.sd.get("drafts_done"):
            return
        self._check_cancel()
        self.job.set_stage(_ST.GENERATING_DRAFTS, 90, "캠페인 초안을 생성하고 있습니다...")
        media_by_id = {m.get("id"): m for m in self.sd.get("media", [])}
        evidence = self.sd.get("evidence", {})
        templates = self.sd.get("templates", [])
        matches = self.sd.get("matches", [])
        own_excluded = self.sd.get("own_sends_excluded", 0)
        existing_media = set(
            AutoDMCampaign.objects.filter(ig_connection=self.conn)
            .exclude(media_id="")
            .values_list("media_id", flat=True)
        )

        # 초안 입력 (media-bound: auto_draft/needs_review). template_text 는 매치가 이미 결정
        # (타겟 복원본 or 전역 템플릿 대표). 전역 템플릿 사용분은 template_only 중복 방지에 기록.
        draft_inputs = []
        used_global_templates = set()
        for mt in matches:
            if mt["band"] not in ("auto_draft", "needs_review"):
                continue
            mid = mt["media_id"]
            ev = evidence.get(mid, {})
            if mt.get("template_source") == "global" and mt.get("template_id"):
                used_global_templates.add(mt["template_id"])
            draft_inputs.append(
                {
                    "media_id": mid,
                    "caption": ev.get("caption_excerpt", ""),
                    "keywords": mt.get("keywords", []),
                    "confidence": mt.get("confidence", 0.6),
                    "owner_reply_top": ev.get("owner_reply_top", ""),
                    "template_text": mt.get("template_text", ""),
                    "other_templates": [],
                }
            )
        drafts, usage = llm.generate_drafts(draft_inputs, model_code=self.job.llm_model)
        self._bump_llm(usage)

        # 재실행 멱등: 이 잡의 기존 후보를 지우고 새로 만든다(READY 전이라 apply 안 됨).
        with transaction.atomic():
            self.job.candidates.all().delete()
            created = 0
            for mt in matches:
                if mt["band"] not in ("auto_draft", "needs_review"):
                    continue
                created += 1
                self._create_media_candidate(
                    mt, media_by_id, evidence, drafts, existing_media, own_excluded
                )
            # template_only: 매칭에 안 쓰인 전역 min-support 템플릿
            for t in templates:
                if t["template_id"] in used_global_templates:
                    continue
                created += 1
                self._create_template_only_candidate(t, own_excluded)
            self.job.candidates_created = created

        self.sd["drafts_done"] = True
        self._persist(counter_fields=["candidates_created"])

    def _create_media_candidate(
        self, mt, media_by_id, evidence, drafts, existing_media, own_excluded
    ):
        mid = mt["media_id"]
        media = media_by_id.get(mid, {})
        ev = evidence.get(mid, {})
        tm = mt.get("matched_template") or {}
        rep = mt.get("template_text", "")
        d = drafts.get(mid, {})
        # 타겟 복원본이면 실제 발신 DM 샘플을, 아니면 전역 템플릿 대표를 근거 원본으로.
        sample_dms = mt.get("recovered_samples") or (
            [{"text": rep, "created_time": tm.get("last_sent_at", "")}] if rep else []
        )
        DMCampaignCandidate.objects.create(
            job=self.job,
            ig_connection=self.conn,
            status=DMCampaignCandidate.Status.DETECTED,
            band=mt["band"],
            media_id=mid,
            media_permalink=media.get("permalink", "") or "",
            media_caption_excerpt=(media.get("caption", "") or "")[:300],
            media_timestamp=analyze.parse_graph_time(media.get("timestamp", "")),
            suggested_keywords=d.get("keywords") or mt.get("keywords", []),
            suggested_keyword_mode=d.get("keyword_mode") or "any",
            confidence=mt.get("final_score", 0.0),
            draft_name=d.get("name", ""),
            draft_description=d.get("description", ""),
            draft_opening_message=d.get("first_dm_draft", ""),
            draft_public_reply_templates=(
                [d["public_reply_draft"]] if d.get("public_reply_draft") else []
            ),
            follow_up_candidates=[
                {
                    "text": t,
                    "confidence": mt.get("confidence", 0.5),
                    "source_template_id": mt.get("template_id"),
                    "cluster_size": tm.get("cluster_size") or tm.get("count", 0),
                }
                for t in (d.get("followup_candidates") or [])
            ],
            matched_template=tm,
            evidence_aggregates={
                "matched_comment_count": sum((mt.get("keyword_hit_counts") or {}).values()),
                "total_comment_count": ev.get("comments_analyzed", 0),
                "keyword_hit_counts": mt.get("keyword_hit_counts", {}),
                "account_replied_publicly": ev.get("account_replied_publicly", False),
                "dm_source": mt.get("template_source", ""),
                "dm_recovered_recipients": (mt.get("signals") or {}).get("recipients", 0),
                "dm_burst_overlap_ratio": (mt.get("signals") or {}).get("time_score", 0.0),
                "time_window": [tm.get("first_sent_at", ""), tm.get("last_sent_at", "")],
                "has_existing_campaign": mid in existing_media,
                "own_sends_excluded": own_excluded,
            },
            evidence_raw={
                "sample_comments": ev.get("sample_comments", []),
                "sample_outbound_dms": sample_dms,
                "template_representative_text": rep,
            },
        )

    def _create_template_only_candidate(self, t, own_excluded):
        DMCampaignCandidate.objects.create(
            job=self.job,
            ig_connection=self.conn,
            status=DMCampaignCandidate.Status.DETECTED,
            band=DMCampaignCandidate.Band.TEMPLATE_ONLY,
            media_id="",
            confidence=0.4,
            draft_opening_message=_strip_urls(t["representative"]),
            matched_template=_template_meta(t),
            evidence_aggregates={
                "cluster_size": t.get("count", 0),
                "conversation_count": t.get("conversation_count", 0),
                "time_window": [t.get("first_sent_at", ""), t.get("last_sent_at", "")],
                "own_sends_excluded": own_excluded,
            },
            evidence_raw={
                "sample_outbound_dms": [
                    {"text": t["representative"], "created_time": t.get("last_sent_at", "")}
                ],
                "template_representative_text": t.get("representative", ""),
            },
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
