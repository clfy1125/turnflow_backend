"""리포트 생성 엔진 — S1 수집 → S8 렌더(자기완결 HTML).

랩 `scripts/run_report.py` 의 오케스트레이션을 그대로 옮기고, 서버용으로 3가지를 더한다:
  · 단계별 진행률 기록(프론트 폴링용)
  · 영구 캐시 브릿지(DB ↔ 런 디렉터리) — 재분석 시 추출비 0
  · 산출물 업로드(자기완결 HTML) + 원가·커버리지 영속화, 런 디렉터리는 항상 파기

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

from . import ig_profile, progress
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


# ── 가짜 모드 페이싱 ─────────────────────────────────────────────────
# 가짜 모드는 실제 일을 안 하니 2~3초면 끝난다 → 진행률이 0에서 100으로 튀어 프론트가
# 퍼센트/단계 UX 를 검증할 수 없다. 그래서 **실제 단계 비중대로** 총 N초에 걸쳐 흐르게 한다
# (수집 11% · 영상분석 33% · 합성 24% … = 프로덕션과 같은 모양, 100배 빠르게).
_PACED_STAGES = [s for s in progress.STAGES if s["key"] != ReportStage.QUEUED]
_PACED_TOTAL_EXPECTED = sum(s["expected"] for s in _PACED_STAGES) or 1


# 페이싱으로 흡수할 수 없는 실작업 예약분(초). 마지막 단계(렌더+저장)는 가짜 모드에서도 1초 남짓
# 걸리고 그 시점엔 이미 마감 시각이 지나 있어 뒤로 튀어나온다 → 총 소요에서 미리 빼 둔다.
# (PDF 변환을 쓰던 시절엔 4초였다. HTML 저장으로 바뀌어 크게 줄었다.)
_FAKE_TAIL_RESERVE_SECONDS = 1.2


def fake_delay_seconds() -> float:
    return float(getattr(settings, "INSTA_REPORT_FAKE_DELAY_SECONDS", 10) or 0)


def _fake_paced_budget() -> float:
    """진행률 애니메이션에 쓸 대기 예산 = 목표 총 소요 − 실작업 예약분."""
    total = fake_delay_seconds()
    if total <= 0:
        return 0.0
    return max(total - _FAKE_TAIL_RESERVE_SECONDS, total * 0.3)


def fake_time_scale() -> float:
    """가짜 모드에서 '실제 예상 소요' 를 축소하는 배율 (꺼져 있으면 1.0).

    프론트가 `stage_expected_seconds`/`eta_seconds` 로 진행률을 보간하므로, 이 값도 같이
    줄여 주지 않으면 서버는 3초에 끝났는데 클라이언트 보간은 6분짜리로 기어간다.
    """
    if not fake_mode_enabled():
        return 1.0
    return max(fake_delay_seconds(), 0.0) / _PACED_TOTAL_EXPECTED


def _expected(stage: str) -> int:
    """이 단계의 예상 소요(초) — 가짜 모드면 축소 배율을 적용한다(최소 1초)."""
    raw = progress.stage_expected(stage) * fake_time_scale()
    return max(int(round(raw)), 1)


class _FakePacer:
    """단계 종료 시점을 절대 시각으로 맞춰 대기 — 실작업 시간을 흡수해 총 소요를 고정한다."""

    def __init__(self, total_seconds: float):
        self.total = max(total_seconds, 0.0)
        self.t0 = time.monotonic()
        self._span: dict[str, tuple[float, float]] = {}
        acc = 0
        for s in _PACED_STAGES:
            start = acc / _PACED_TOTAL_EXPECTED
            acc += s["expected"]
            self._span[s["key"]] = (start, acc / _PACED_TOTAL_EXPECTED)

    def wait(self, stage: str, ratio: float = 1.0) -> None:
        """`stage` 의 ratio(0~1) 지점까지 대기. 이미 지났으면 즉시 반환."""
        start, end = self._span.get(stage, (0.0, 1.0))
        frac = start + (end - start) * min(max(ratio, 0.0), 1.0)
        remaining = (self.t0 + self.total * frac) - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)


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
    # 가짜 모드에서만 동작 — 진행률이 실제와 같은 비중으로 흐르게 대기시킨다(프론트 UX 검증용).
    pacer = _FakePacer(_fake_paced_budget()) if fake else None
    run_dir = Path(tempfile.mkdtemp(prefix=f"instarpt_{report.id.hex[:8]}_"))
    config.bind_run(run_dir)
    ledger = CostLedger(username)

    try:
        # ── S1 수집 ────────────────────────────────────────────────
        report.set_stage(
            ReportStage.COLLECTING,
            3,
            "게시물을 모으고 있어요",
            _expected(ReportStage.COLLECTING),
        )
        collect_summary = _collect(report, connection, username, fake=fake)
        ledger.record_flat(
            "S1_collect",
            "apify",
            0.0 if fake else _apify_cost_usd(collect_summary.get("post_count", 0)),
            note=f"posts={collect_summary.get('post_count')}",
        )
        if pacer:
            pacer.wait(ReportStage.COLLECTING)

        # ── S1' 정규화 + S2 지표 (1차: 샘플링 기준) ────────────────
        report.set_stage(
            ReportStage.METRICS,
            15,
            "숫자를 계산하고 있어요",
            _expected(ReportStage.METRICS),
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
        if pacer:
            pacer.wait(ReportStage.METRICS)

        # ── S3 샘플러 + 미디어 다운로드 ────────────────────────────
        report.set_stage(
            ReportStage.PREPARING,
            20,
            "영상을 내려받고 있어요",
            _expected(ReportStage.PREPARING),
        )
        sample = sampler.build_sample(canon)
        if not fake:
            official_doc = json.loads(
                (config.RAW_DIR / f"{username}.json").read_text(encoding="utf-8")
            )
            # Graph `media_url` 이 빈 릴스의 영상 URL 은 Apify 응답에서 가져온다
            # (media.py 상단 주의사항 — 안 넘기면 결손분이 그대로 유실된다).
            media_stats = media.download_for_run(
                official_doc,
                m,
                sample,
                apify_doc=_read_apify_doc(username),
                on_progress=lambda d, n: _tick(
                    report, ReportStage.PREPARING, d, n, f"영상 준비 {d}/{n}"
                ),
            )
            logger.info("insta_report: media %s report=%s", media_stats, report.id)
            # 다운로드 결과가 canon 에 반영되도록 재정규화(경로 필드가 파일 존재 기준이라서).
            canon = normalize.build_canonical(username)
        m = metrics_mod.save_metrics(username, canon)
        if pacer:
            pacer.wait(ReportStage.PREPARING)

        # ── S4 피처 추출 ───────────────────────────────────────────
        report.set_stage(
            ReportStage.EXTRACTING,
            30,
            "영상을 분석하고 있어요",
            _expected(ReportStage.EXTRACTING),
        )
        extraction = _extract(
            report, canon, sample, ledger, external_account_id, fake=fake, pacer=pacer
        )
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
            _expected(ReportStage.COMMENTS),
        )
        agg = aggregate.build_aggregates(canon, m, extraction, sample)
        cstats, cfilter = _comments(
            canon, extraction, ledger, username, external_account_id, fake=fake
        )
        agg.update(aggregate.build_v3_extras(m, extraction, cstats, cfilter))
        aggregate.save_aggregates(username, agg)
        if pacer:
            pacer.wait(ReportStage.COMMENTS)

        # ── S6 합성 + S7 검증 ─────────────────────────────────────
        report.set_stage(
            ReportStage.SYNTHESIZING,
            72,
            "인사이트를 쓰고 있어요",
            _expected(ReportStage.SYNTHESIZING),
        )
        slots, gate_meta = _synthesize_and_verify(
            report, canon, m, agg, cstats, cfilter, ledger, username, fake=fake, pacer=pacer
        )

        # ── S8 렌더 + PDF ─────────────────────────────────────────
        report.set_stage(
            ReportStage.RENDERING,
            93,
            "리포트를 만들고 있어요",
            _expected(ReportStage.RENDERING),
        )
        try:
            html_path = Path(render.render_report_v3(canon, m, agg, slots))
            html = html_path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            raise ReportFailure(ReportErrorCode.RENDER_FAILED, f"{type(e).__name__}: {e}") from e
        if pacer:
            pacer.wait(ReportStage.RENDERING)

        report.set_stage(
            ReportStage.EXPORTING,
            97,
            "파일로 저장하고 있어요",
            _expected(ReportStage.EXPORTING),
        )
        html_bytes = html.encode("utf-8")
        if pacer:
            pacer.wait(ReportStage.EXPORTING)

        # ── 영속화 ────────────────────────────────────────────────
        cov = m.get("coverage") or {}
        cost = ledger.summary()
        report.html_file.save(f"{report.id}.html", ContentFile(html_bytes), save=False)
        report.html_bytes = len(html_bytes)
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
        summary = collect_official.collect(
            username,
            connection.external_account_id,
            token,
            fallback_profile_url=connection.profile_picture_url or "",
        )
    except collect_official.CollectError as e:
        # 세 갈래를 구별한다 — 안내 문구가 갈리기 때문.
        #   token     → "연결이 만료됐어요, 다시 연결해 주세요"  (재연동해야 풀린다)
        #   transient → "일시적인 오류예요, 잠시 후 다시 시도"    (그냥 다시 누르면 된다)
        #   그 외      → "게시물을 찾지 못했어요"                  (계정에 게시물이 없다)
        # 예전에는 token 이 아니면 전부 NO_POSTS 라, Graph 일시 오류가 "게시물 없음"으로
        # 나가거나 400 이 통째로 TOKEN_INVALID 로 나갔다(collect_official 상단 주의사항).
        if e.token_invalid:
            code = ReportErrorCode.TOKEN_INVALID
        elif e.transient:
            code = ReportErrorCode.INTERNAL
        else:
            code = ReportErrorCode.NO_POSTS
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

    # ⚠️ Apify 에 **Graph 에서 받은 릴스 permalink 를 직접** 넘긴다. 프로필 URL 만 주면
    #    액터가 공개 프로필 그리드만 훑어 그리드에 없는 릴스가 빠지고, 조회수 있는 릴스가
    #    5개 미만이면 리포트가 통째로 실패한다(@yeonhada__ 실측: Graph 39개 vs Apify 3개).
    #    조회수는 Graph 인사이트로 대체 불가(지표 29종 전부 403 — 앱 권한 미승인, 재확인 완료).
    reel_urls = _reel_permalinks(_read_official_doc(username))

    # ── 릴스 부족은 **조회수 수집 전에** 판정한다 ────────────────────────
    # 조회수를 받을 수 있는 최대치가 곧 이 개수다. 5개 미만이면 뒤 게이트
    # (`metrics.insufficient`)에서 탈락이 확정이므로 Apify 를 부를 이유가 없다.
    # 이 판정이 없던 동안 릴스 0개 계정이 프로필 URL 폴백으로 넘어가 그리드를 긁고
    # `VIEWS_UNAVAILABLE`("조회수 정보를 가져오지 못했어요 — 잠시 후 다시 시도")로 끝났다.
    # 사용자에겐 우리 쪽 일시 장애로 읽혀 재시도 루프가 됐다(@searchforwork__: 게시물
    # 1개·릴스 0개인데 1시간 반 동안 4회). 사유를 맞추면 안내도 맞는다.
    if len(reel_urls) < config.MIN_REELS_FOR_REPORT:
        raise ReportFailure(
            ReportErrorCode.NOT_ENOUGH_REELS,
            f"릴스 {len(reel_urls)}개 < {config.MIN_REELS_FOR_REPORT} "
            f"(조회수 수집 전 판정 · 수집 게시물 {summary.get('post_count')}개)",
        )

    try:
        apify_summary = collect_apify.collect(
            username,
            permalinks=reel_urls,
            on_tick=lambda st, secs: _tick_message(
                report, ReportStage.COLLECTING, f"조회수를 모으고 있어요 ({secs}초)"
            ),
        )
    except collect_apify.ApifyError as e:
        raise ReportFailure(ReportErrorCode.VIEWS_UNAVAILABLE, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise ReportFailure(ReportErrorCode.VIEWS_UNAVAILABLE, f"{type(e).__name__}: {e}") from e

    # 송수신 개수를 남긴다 — 이게 없으면 "조회수 있는 릴스 4개 < 5" 가 계정 사정인지
    # 수집 누락인지 사후에 가릴 수 없다(2026-09-01: Apify API 를 직접 뒤져서야 확인했다).
    logger.info(
        "insta_report: apify 송신 %s개 → 수신 %s개 (영상 %s · 조회수 있음 %s) report=%s",
        len(reel_urls),
        apify_summary.get("count"),
        apify_summary.get("videos"),
        apify_summary.get("videos_with_views"),
        report.id,
    )
    return {**summary, "apify": apify_summary}


def _read_apify_doc(username: str) -> dict:
    """조회수 수집 결과를 다시 읽는다(영상·썸네일 URL 폴백용). 없으면 빈 dict."""
    try:
        return json.loads((config.APIFY_DIR / f"{username}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _read_official_doc(username: str) -> dict:
    """공식 수집 산출물(방금 collect_official 이 쓴 파일)."""
    try:
        return json.loads((config.RAW_DIR / f"{username}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ReportFailure(
            ReportErrorCode.INTERNAL, f"수집 결과 읽기 실패: {type(e).__name__}"
        ) from e


def _reel_permalinks(doc: dict) -> list[str]:
    """공식 수집 결과에서 조회수를 받아야 할 게시물 permalink (Apify 에 직접 넘길 대상).

    영상만 넘기는 이유: 조회수가 필요한 건 영상뿐이고(지표·분포·벤치마크 전부 릴스 기준),
    좋아요·댓글 수는 Graph 가 이미 정확히 준다. 항목 수가 줄어 Apify 비용도 낮아진다.

    ⚠️ 판정을 `media_product_type == "REELS"` **단독으로 하지 말 것.** 진입 게이트가 세는
    것은 `media_type == "VIDEO"`(normalize 가 "reel" 로 변환)이므로, REELS 로만 좁히면
    피드 동영상·IGTV 가 조회수 수집에서 빠져 **게이트가 세는 분자만 줄어든다**. 두 조건을
    OR 로 묶어 세는 쪽과 받는 쪽을 일치시킨다.
    """
    return [
        p["permalink"]
        for p in (doc.get("posts") or [])
        if p.get("permalink")
        and (p.get("media_product_type") == "REELS" or p.get("media_type") == "VIDEO")
    ]


def _extract(
    report, canon: dict, sample: dict, ledger, external_account_id: str, *, fake: bool, pacer=None
) -> dict:
    if fake:
        extraction = fake_mode.fake_extraction(canon, sample)
        # 프론트가 "영상 분석 n/N" 하위 진행률을 확인할 수 있게 실제와 같은 리듬으로 흘린다.
        done_list = list(extraction["features"])
        total = len(done_list) or 1
        for idx in range(total):
            if pacer:
                pacer.wait(ReportStage.EXTRACTING, (idx + 1) / total)
            _tick(report, ReportStage.EXTRACTING, idx + 1, total, f"영상 분석 {idx + 1}/{total}")
        return extraction

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
    pacer=None,
) -> tuple[dict, dict]:
    if fake:
        # 가짜 모드는 AI 없이 폴백 슬롯(실코드)으로 렌더 계약을 검증한다.
        slots = verify_v3.fallback_slots_v3(m, agg)
        if pacer:
            pacer.wait(ReportStage.SYNTHESIZING)
        # 검수 단계도 프론트에 보여야 하므로(10단계 전부 확인) 실제 순서대로 밟는다.
        report.set_stage(
            ReportStage.VERIFYING,
            88,
            "숫자와 표현을 검수하고 있어요",
            _expected(ReportStage.VERIFYING),
        )
        if pacer:
            pacer.wait(ReportStage.VERIFYING)
        return slots, {"fake": True, "fallback_slots": ["ALL"]}

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
        _expected(ReportStage.VERIFYING),
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

    # 분류 키(영어) → 사람말 라벨. AI 입력에는 라벨만 넣는다(위 comment_stats 주의사항 참고).
    cat_ko = cstats.get("category_ko") or {}
    quote_pool_ko = {
        cat_ko.get(cat, cat): [
            {"quote_id": q["quote_id"], "text": q["text"][:100], "likes": q["likes"]} for q in qs
        ]
        for cat, qs in cstats["quote_pool"].items()
    }
    counts_ko = {cat_ko.get(k, k): v for k, v in (cstats.get("counts") or {}).items() if v}
    pcts_ko = {cat_ko.get(k, k): v for k, v in (cstats.get("pcts") or {}).items() if v}
    tones_ko = [
        {"name": t["label"], "pct": t["pct"], "count": t["count"]}
        for t in (cstats.get("tones") or [])
    ]
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
        # 계정 규모·도달 방식 — 합성 프롬프트가 이걸 보고 조언 방향을 바꾼다
        # (`synthesize._audience_guidance`). 빼면 다시 "중간 규모용 조언" 하나만 나온다.
        "audience": m.get("audience") or {},
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
            # ⚠️ **분류 키(영어)를 AI 에 넣지 않는다** — 2026-08-04 실측: `counts`/`pcts`/
            # `quote_pool` 의 키를 그대로 넣었더니 모델이 `hostile`·`debate` 를 리포트 문장에
            # 그대로 써서 게이트가 "영어 표현 금지" 로 반려했다(모듈 상단 주의사항 위반).
            # 사람말 라벨만 넘긴다.
            "counts": counts_ko,
            "pcts": pcts_ko,
            "tones": tones_ko,
            "n_analyzed": cstats["n_analyzed"],
            "save_mentions": cstats["save_mentions"],
            "motivations": cstats["motivations"],
            "quote_pool": quote_pool_ko,
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
