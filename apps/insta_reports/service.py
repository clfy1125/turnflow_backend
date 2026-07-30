"""리포트 생성 엔진 — S1 수집 → S8 렌더 → PDF.

랩 `scripts/run_report.py` 의 오케스트레이션을 그대로 옮기고, 서버용으로 3가지를 더한다:
  · 단계별 진행률 기록(프론트 폴링용)
  · 영구 캐시 브릿지(DB ↔ 런 디렉터리) — 재분석 시 추출비 0
  · 산출물 업로드(PDF) + 원가·커버리지 영속화, 런 디렉터리는 항상 파기

⚠️ 실패 시 이용 횟수는 차감하지 않는다(`quota_consumed=False`). 사용자 귀책이 아니라
   외부 수집/모델 사정이 대부분이기 때문. 정책 변경 시 quota.py 문서도 함께 고칠 것.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from . import ig_profile, pdf, progress
from .models import ReportErrorCode, ReportStage, ReportStatus
from .pipeline import (
    aggregate,
    cache_store,
    collect_apify,
    collect_official,
    comments,
    config,
    extract,
    fake_mode,
    media,
    normalize,
    render,
    sampler,
    synthesize,
    verify_v3,
)
from .pipeline import feature_schema as fs
from .pipeline import metrics as metrics_mod
from .pipeline.costs import CostLedger

logger = logging.getLogger(__name__)

MIN_FEATURES_FOR_REPORT = 5  # 영상 피처가 이보다 적으면 v3 교차표가 의미를 못 가진다


class ReportFailure(Exception):
    """사용자에게 코드로 노출할 실패. 태스크가 잡아 report.mark_failed() 로 기록."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def fake_mode_enabled() -> bool:
    return bool(getattr(settings, "INSTA_REPORT_FAKE_MODE", False))


def _apify_cost_usd(post_count: int) -> float:
    unit = float(getattr(settings, "INSTA_REPORT_APIFY_USD_PER_POST", 0.0027) or 0)
    return round(unit * max(post_count, 0), 6)


def generate(report) -> dict:
    """리포트 1건 생성. 성공 시 report 를 succeeded 로 갱신하고 요약 dict 반환.

    실패는 ``ReportFailure`` 로 올린다(태스크가 상태 기록·알림 담당).
    """
    connection = report.ig_connection
    if connection is None:
        raise ReportFailure(ReportErrorCode.INTERNAL, "연동 정보가 없습니다.")

    username = (connection.username or "").lstrip("@") or f"acct_{connection.external_account_id}"
    external_account_id = connection.external_account_id
    fake = fake_mode_enabled()

    report.status = ReportStatus.RUNNING
    report.started_at = timezone.now()
    report.save(update_fields=["status", "started_at", "updated_at"])

    t0 = time.monotonic()
    run_dir = Path(tempfile.mkdtemp(prefix=f"instarpt_{report.id.hex[:8]}_"))
    config.bind_run(run_dir)
    ledger = CostLedger(username)

    try:
        # ── S1 수집 ────────────────────────────────────────────────
        report.set_stage(
            ReportStage.COLLECTING,
            3,
            "게시물을 모으고 있어요",
            progress.stage_expected(ReportStage.COLLECTING),
        )
        collect_summary = _collect(report, connection, username, fake=fake)
        ledger.record_flat(
            "S1_collect",
            "apify",
            0.0 if fake else _apify_cost_usd(collect_summary.get("post_count", 0)),
            note=f"posts={collect_summary.get('post_count')}",
        )

        # ── S1' 정규화 + S2 지표 (1차: 샘플링 기준) ────────────────
        report.set_stage(
            ReportStage.METRICS,
            15,
            "숫자를 계산하고 있어요",
            progress.stage_expected(ReportStage.METRICS),
        )
        canon = normalize.build_canonical(username)
        if not canon.get("posts"):
            raise ReportFailure(ReportErrorCode.NO_POSTS, "정규화 결과에 게시물이 없습니다.")
        m = metrics_mod.build_metrics(canon)
        if m.get("insufficient"):
            reels = (m.get("coverage") or {}).get("reels_with_views", 0)
            raise ReportFailure(
                ReportErrorCode.NOT_ENOUGH_REELS,
                f"조회수 있는 릴스 {reels}개 < {config.MIN_REELS_FOR_REPORT}",
            )

        # ── S3 샘플러 + 미디어 다운로드 ────────────────────────────
        report.set_stage(
            ReportStage.PREPARING,
            20,
            "영상을 내려받고 있어요",
            progress.stage_expected(ReportStage.PREPARING),
        )
        sample = sampler.build_sample(canon)
        if not fake:
            official_doc = json.loads(
                (config.RAW_DIR / f"{username}.json").read_text(encoding="utf-8")
            )
            media_stats = media.download_for_run(
                official_doc,
                m,
                sample,
                on_progress=lambda d, n: _tick(
                    report, ReportStage.PREPARING, d, n, f"영상 준비 {d}/{n}"
                ),
            )
            logger.info("insta_report: media %s report=%s", media_stats, report.id)
            # 다운로드 결과가 canon 에 반영되도록 재정규화(경로 필드가 파일 존재 기준이라서).
            canon = normalize.build_canonical(username)
        m = metrics_mod.save_metrics(username, canon)

        # ── S4 피처 추출 ───────────────────────────────────────────
        report.set_stage(
            ReportStage.EXTRACTING,
            30,
            "영상을 분석하고 있어요",
            progress.stage_expected(ReportStage.EXTRACTING),
        )
        extraction = _extract(report, canon, sample, ledger, external_account_id, fake=fake)
        if len(extraction["features"]) < MIN_FEATURES_FOR_REPORT:
            raise ReportFailure(
                ReportErrorCode.EXTRACT_FAILED,
                f"성공 {len(extraction['features'])} / 실패 {len(extraction['failures'])} — "
                f"{list(extraction['failures'].values())[:2]}",
            )

        # ── S5 집계 + S4b 댓글 ─────────────────────────────────────
        report.set_stage(
            ReportStage.COMMENTS,
            65,
            "댓글을 분석하고 있어요",
            progress.stage_expected(ReportStage.COMMENTS),
        )
        agg = aggregate.build_aggregates(canon, m, extraction, sample)
        cstats, cfilter = _comments(
            canon, extraction, ledger, username, external_account_id, fake=fake
        )
        agg.update(aggregate.build_v3_extras(m, extraction, cstats, cfilter))
        aggregate.save_aggregates(username, agg)

        # ── S6 합성 + S7 검증 ─────────────────────────────────────
        report.set_stage(
            ReportStage.SYNTHESIZING,
            72,
            "인사이트를 쓰고 있어요",
            progress.stage_expected(ReportStage.SYNTHESIZING),
        )
        slots, gate_meta = _synthesize_and_verify(
            report, canon, m, agg, cstats, cfilter, ledger, username, fake=fake
        )

        # ── S8 렌더 + PDF ─────────────────────────────────────────
        report.set_stage(
            ReportStage.RENDERING,
            93,
            "리포트를 만들고 있어요",
            progress.stage_expected(ReportStage.RENDERING),
        )
        try:
            html_path = Path(render.render_report_v3(canon, m, agg, slots))
            html = html_path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            raise ReportFailure(ReportErrorCode.RENDER_FAILED, f"{type(e).__name__}: {e}") from e

        report.set_stage(
            ReportStage.EXPORTING,
            97,
            "PDF 로 만들고 있어요",
            progress.stage_expected(ReportStage.EXPORTING),
        )
        try:
            pdf_bytes = pdf.html_to_pdf(html)
        except Exception as e:  # noqa: BLE001
            raise ReportFailure(ReportErrorCode.PDF_FAILED, f"{type(e).__name__}: {e}") from e

        # ── 영속화 ────────────────────────────────────────────────
        cov = m.get("coverage") or {}
        cost = ledger.summary()
        report.pdf_file.save(f"{report.id}.pdf", ContentFile(pdf_bytes), save=False)
        report.pdf_bytes = len(pdf_bytes)
        report.metrics_json = m
        report.aggregates_json = agg
        report.slots_json = slots
        report.gate_meta = gate_meta
        report.posts_analyzed = int(cov.get("posts_analyzed") or 0)
        report.reels_with_views = int(cov.get("reels_with_views") or 0)
        report.videos_analyzed = int(agg.get("video_analyzed") or 0)
        report.comments_analyzed = int(cstats.get("n_analyzed") or 0)
        report.period_from = _as_date(cov.get("period_from"))
        report.period_to = _as_date(cov.get("period_to"))
        report.cost_usd = Decimal(str(cost.get("total_usd") or 0))
        report.tokens_json = cost
        report.elapsed_seconds = int(time.monotonic() - t0)
        report.status = ReportStatus.SUCCEEDED
        report.stage = ReportStage.DONE
        report.progress = 100
        report.message = "리포트가 완성됐어요"
        report.finished_at = timezone.now()
        report.save()

        logger.info(
            "insta_report: done report=%s user=%s cost=$%s elapsed=%ss videos=%s comments=%s",
            report.id,
            report.requested_by_id,
            report.cost_usd,
            report.elapsed_seconds,
            report.videos_analyzed,
            report.comments_analyzed,
        )
        return {"cost_usd": float(report.cost_usd), "elapsed_seconds": report.elapsed_seconds}
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# ── 단계 구현 ────────────────────────────────────────────────────────
def _collect(report, connection, username: str, *, fake: bool) -> dict:
    if fake:
        return fake_mode.write_sources(
            username,
            external_account_id=connection.external_account_id,
            followers=connection.followers_count or 12_345,
        )

    try:
        token = connection.access_token
    except Exception as e:  # noqa: BLE001 - 복호화 실패
        raise ReportFailure(ReportErrorCode.TOKEN_INVALID, f"{type(e).__name__}") from e
    if not token:
        raise ReportFailure(ReportErrorCode.TOKEN_INVALID, "액세스 토큰이 없습니다.")

    try:
        summary = collect_official.collect(username, connection.external_account_id, token)
    except collect_official.CollectError as e:
        code = ReportErrorCode.TOKEN_INVALID if e.token_invalid else ReportErrorCode.NO_POSTS
        raise ReportFailure(code, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise ReportFailure(ReportErrorCode.INTERNAL, f"{type(e).__name__}: {e}") from e

    # 팝업 표시용 통계 + 리포트 스냅샷 갱신 (수집 때 이미 받아 온 메타 재활용)
    ig_profile.apply_meta(
        connection,
        {
            "followers_count": summary.get("followers_count"),
            "media_count": summary.get("media_count"),
            "name": summary.get("name"),
            "biography": summary.get("biography"),
        },
    )
    report.followers_snapshot = summary.get("followers_count") or report.followers_snapshot
    report.media_count_snapshot = summary.get("media_count") or report.media_count_snapshot
    report.ig_name = summary.get("name") or report.ig_name
    report.save(
        update_fields=["followers_snapshot", "media_count_snapshot", "ig_name", "updated_at"]
    )

    try:
        apify_summary = collect_apify.collect(
            username,
            on_tick=lambda st, secs: _tick_message(
                report, ReportStage.COLLECTING, f"조회수를 모으고 있어요 ({secs}초)"
            ),
        )
    except collect_apify.ApifyError as e:
        raise ReportFailure(ReportErrorCode.VIEWS_UNAVAILABLE, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise ReportFailure(ReportErrorCode.VIEWS_UNAVAILABLE, f"{type(e).__name__}: {e}") from e

    return {**summary, "apify": apify_summary}


def _extract(
    report, canon: dict, sample: dict, ledger, external_account_id: str, *, fake: bool
) -> dict:
    if fake:
        return fake_mode.fake_extraction(canon, sample)

    shortcodes = [v["shortcode"] for v in sample.get("videos") or []]
    shortcodes += list(sample.get("light_images") or [])
    warmed = cache_store.warm_features(external_account_id, shortcodes)
    if warmed:
        logger.info(
            "insta_report: feature cache warm %s/%s report=%s", warmed, len(shortcodes), report.id
        )

    total = len(shortcodes) or 1

    def _progress(done, n, sc):  # noqa: ARG001 - shortcode 는 로깅용
        _tick(report, ReportStage.EXTRACTING, done, n or total, f"영상 분석 {done}/{n or total}")

    try:
        extraction = extract.extract_sample(canon, sample, ledger, progress=_progress)
    except Exception as e:  # noqa: BLE001
        raise ReportFailure(ReportErrorCode.EXTRACT_FAILED, f"{type(e).__name__}: {e}") from e
    finally:
        try:
            cache_store.flush_features(external_account_id)
        except Exception:  # noqa: BLE001 - 캐시 실패가 리포트를 막지 않게
            logger.warning("insta_report: feature cache flush failed report=%s", report.id)
    return extraction


def _comments(
    canon: dict, extraction: dict, ledger, username: str, external_account_id: str, *, fake: bool
) -> tuple[dict, dict]:
    triggers = comments.collect_triggers(canon, extraction["features"])
    cfilter = comments.filter_comments(canon, triggers)
    pool = cfilter["pool"]
    if fake:
        classes = fake_mode.fake_comment_classes(pool)
    else:
        version = comments.CLASSIFY_CACHE_VERSION
        cache_store.warm_comment_classes(external_account_id, username, version)
        try:
            classes = comments.classify_comments(pool, ledger, username)
        except Exception as e:  # noqa: BLE001 - 분류 실패는 리포트를 막지 않는다(전부 other)
            logger.warning(
                "insta_report: comment classify failed (%s) — other 처리", type(e).__name__
            )
            classes = {c["id"]: "other" for c in pool}
        else:
            try:
                cache_store.flush_comment_classes(external_account_id, username, version)
            except Exception:  # noqa: BLE001
                pass
    cstats = comments.comment_stats(pool, classes, canon)
    return cstats, cfilter


def _synthesize_and_verify(
    report,
    canon: dict,
    m: dict,
    agg: dict,
    cstats: dict,
    cfilter: dict,
    ledger,
    username: str,
    *,
    fake: bool,
) -> tuple[dict, dict]:
    if fake:
        # 가짜 모드는 AI 없이 폴백 슬롯(실코드)으로 렌더 계약을 검증한다.
        return verify_v3.fallback_slots_v3(m, agg), {"fake": True, "fallback_slots": ["ALL"]}

    synth_input = build_synth_input(canon, m, agg, cstats, cfilter, username)
    try:
        raw_slots = synthesize.synthesize(synth_input, ledger, v3=True)
    except Exception as e:  # noqa: BLE001 - 합성 실패는 폴백으로 계속(리포트는 나간다)
        logger.warning(
            "insta_report: synth failed report=%s (%s) — 폴백 슬롯", report.id, type(e).__name__
        )
        return (
            verify_v3.fallback_slots_v3(m, agg),
            {"error": f"{type(e).__name__}: {e}"[:300], "fallback_slots": ["ALL"]},
        )

    report.set_stage(
        ReportStage.VERIFYING,
        88,
        "숫자와 표현을 검수하고 있어요",
        progress.stage_expected(ReportStage.VERIFYING),
    )
    try:
        return verify_v3.run_gate_v3(
            raw_slots,
            m,
            agg,
            resynth_fn=lambda fb, keys: synthesize.synthesize(
                synth_input, ledger, fb, v3=True, only_slots=keys
            ),
            log=lambda msg: logger.info("insta_report gate[%s]: %s", report.id, msg),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "insta_report: gate failed report=%s (%s) — 폴백 슬롯", report.id, type(e).__name__
        )
        return (
            verify_v3.fallback_slots_v3(m, agg),
            {"error": f"{type(e).__name__}: {e}"[:300], "fallback_slots": ["ALL"]},
        )


def build_synth_input(
    canon: dict, m: dict, agg: dict, cstats: dict, cfilter: dict, username: str
) -> dict:
    """상위모델에 넣을 집계 JSON. (랩 run_report.py 와 동일 — 어려운 말은 사람말로 치환)

    ⚠️ AI 에는 분류 **키**(loss_aversion 등)를 절대 넣지 않는다. 그대로 리포트 문장에
       새어 나와 "결과제시형" 같은 알 수 없는 용어가 되기 때문.
    """

    def plainify_table(table, plain_map):
        out = []
        for c in table:
            out.append(
                {
                    "name": plain_map.get(c["key"], c["key"]),
                    "n": c["n"],
                    "median": c["median"],
                    "max": c["max"],
                    "low_sample": c["low_sample"],
                    "example_texts": [
                        e.get("hook_text") or e.get("opening_desc") or ""
                        for e in c.get("examples", [])
                    ][:2],
                }
            )
        return out

    quote_pool_slim = {
        cat: [{"quote_id": q["quote_id"], "text": q["text"][:100], "likes": q["likes"]} for q in qs]
        for cat, qs in cstats["quote_pool"].items()
    }
    low_feat_plain = [
        {
            "views": r["views"],
            "start_type": fs.HOOK_PLAIN_KO.get(r["hook_type"], r["hook_type"]),
            "screen_type": fs.OPENING_PLAIN_KO.get(r["opening"], r["opening"]),
            "first_words": r["hook_text"],
        }
        for r in agg["low_posts_features"]
    ]
    return {
        "account": {
            "username": username,
            "bio": (canon["account"].get("biography") or "")[:300],
            "followers": canon["account"].get("followers"),
        },
        "coverage": m["coverage"],
        "views_stats": m["views_stats"],
        "dist": m["dist"],
        "monthly": m["monthly"],
        "benchmark": m["benchmark"],
        "engagement": m["engagement"],
        "cta_caption": m["cta_caption"],
        "cta_keywords": m["cta_keywords"],
        "timing_dow": m["timing_dow"],
        "timing_hours": m["timing_hours"],
        "best_slot": m["best_slot"],
        "top_posts": [
            {
                k: p[k]
                for k in ("rank", "views", "likes", "comments", "date_kst", "title", "has_cta")
            }
            for p in m["top_posts"]
        ],
        "low_posts": m["low_posts"],
        "tips": m["tips"],
        "hook_table": plainify_table(agg["hook_table"], fs.HOOK_PLAIN_KO),
        "opening_table": plainify_table(agg["opening_table"], fs.OPENING_PLAIN_KO),
        "cta_video": agg["cta_video"],
        "value_2s": agg["value_2s"],
        "promotional": agg["promotional"],
        "good_hooks": agg["good_hooks"],
        "weak_hooks": agg["weak_hooks"],
        "low_posts_features": low_feat_plain,
        "topics_pool": agg["topics_pool"],
        "derived": agg["derived"],
        "top_posts_meta": [{**tm} for tm in agg["top_posts_meta"]],
        "comment_stats": {
            "counts": cstats["counts"],
            "pcts": cstats["pcts"],
            "n_analyzed": cstats["n_analyzed"],
            "save_mentions": cstats["save_mentions"],
            "motivations": cstats["motivations"],
            "quote_pool": quote_pool_slim,
            "dm_not_received_count": cstats.get("dm_not_received_count", 0),
            "dm_not_received_pct": cstats.get("dm_not_received_pct", 0),
            "insufficient": cstats.get("insufficient", False),
            "_note2": (
                "분석 가능한 댓글이 적어 팔로워 관련 주장은 단정하지 말고 '경향 참고' "
                "수준으로 쓰세요. fans_wants·motivation_descs 도 확신 어법 금지."
                if cstats.get("insufficient")
                else ""
            ),
            "_note": "dm_not_received 는 '약속한 자동 DM을 못 받았다'는 문의입니다. "
            "팔로워가 원하는 것이 아니라 발송 실패 신호이므로, 니즈로 해석하지 말고 "
            "'놓치고 있는 기회'에서 발송 점검을 제안하세요.",
        },
        "comment_filter": cfilter["stats"],
    }


# ── 진행률 유틸 ──────────────────────────────────────────────────────
def _tick(report, stage: str, done: int, total: int, message: str) -> None:
    """단계 내부 하위 진행률 갱신 (n/N)."""
    try:
        report.set_stage(stage, progress.interpolate(stage, done, total), message)
    except Exception:  # noqa: BLE001 - 진행률 실패가 생성을 막지 않게
        logger.debug("insta_report: progress tick failed report=%s", report.id)


def _tick_message(report, stage: str, message: str) -> None:
    try:
        start, _ = progress.stage_bounds(stage)
        report.set_stage(stage, max(report.progress, start), message)
    except Exception:  # noqa: BLE001
        pass


def _as_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None
