"""
룰 기반 인사이트 진단.

LLM 호출 없이 결정론적 룰만 사용 (즉시 응답 + 비용 0).
화면 2 의 "약점 진단" 과 화면 3 의 "인사이트 매트릭스" 두 곳에서 사용.

각 룰 출력 형식:
    {
        "id":       "rule_id",
        "icon":     "🔥",                  # 프론트가 그대로 출력
        "severity": "info|warning|critical",
        "title":    "짧은 한 줄",
        "message":  "구체 해석 + 행동 지침",
        "metric":   {...}                   # 룰을 트리거한 raw 수치
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import IGAccountInsight, IGMedia, IGMediaInsight, MediaProductType


@dataclass
class Insight:
    id: str
    icon: str
    severity: str  # info | warning | critical
    title: str
    message: str
    metric: dict


# ─────────────────────────────────────────────────────────────
# 룰 임계치 (한 곳에서 튜닝)
# ─────────────────────────────────────────────────────────────


class Thresholds:
    # 릴스 약점 진단
    REELS_SKIP_RATE_CRITICAL = 50.0  # %
    REELS_SKIP_RATE_WARNING = 35.0
    REELS_WATCH_COMPLETION_WARNING = 50.0  # %

    # 인사이트 매트릭스
    SAVED_VS_SHARES_RATIO = 5.0  # saved 가 shares 대비 N 배 이상이면 "무거운 콘텐츠"
    HIGH_REACH_LOW_FOLLOW_REACH_MIN = 10_000  # 도달 N 이상 + follow 전환 낮음
    FOLLOW_RATE_LOW = 0.05  # follows / reach * 100 < 0.05 % (= 1만 도달에 5명)
    ER_TOP_BAND = 5.0  # ER 5% 이상 = 우수

    # 계정 단위 — follow_type breakdown 기반
    ACCOUNT_FOLLOWER_SHARE_DOMINANT = 85.0  # 도달의 85% 이상이 follower → 고인물 패턴
    ACCOUNT_NON_FOLLOWER_SHARE_HEALTHY = 30.0  # 비팔로워 30% 이상 → 신규 유저 도달 활발


# ─────────────────────────────────────────────────────────────
# 릴스 약점 진단 (화면 2A — Reels Detail)
# ─────────────────────────────────────────────────────────────


def diagnose_reels_hook(media: IGMedia, insight: IGMediaInsight) -> list[Insight]:
    """3초 훅 진단 — Reels 전용."""
    out: list[Insight] = []
    if media.media_product_type != MediaProductType.REELS:
        return out

    # 1) Skip rate
    sr = insight.reels_skip_rate
    if sr is not None:
        if sr >= Thresholds.REELS_SKIP_RATE_CRITICAL:
            out.append(
                Insight(
                    id="reels_skip_critical",
                    icon="🛑",
                    severity="critical",
                    title="초반 3초에서 절반 이상 이탈",
                    message=(
                        f"초반 3초 이내에 시청자의 {sr:.0f}%가 이탈했습니다. "
                        "텍스트 트랜지션이 너무 늦게 나오거나 썸네일과 본문 내용이 다를 확률이 높습니다."
                    ),
                    metric={"reels_skip_rate": sr},
                )
            )
        elif sr >= Thresholds.REELS_SKIP_RATE_WARNING:
            out.append(
                Insight(
                    id="reels_skip_warning",
                    icon="⚠️",
                    severity="warning",
                    title="초반 이탈률이 평균보다 높음",
                    message=(
                        f"초반 3초 이탈률이 {sr:.0f}%입니다. 영상 진입 직후 1초 안에 "
                        "강한 시각/문구 훅을 배치해 보세요."
                    ),
                    metric={"reels_skip_rate": sr},
                )
            )

    # 2) Watch completion
    wcr = insight.watch_completion_rate  # property
    if wcr is not None:
        if wcr < Thresholds.REELS_WATCH_COMPLETION_WARNING:
            duration = media.duration_seconds or 0
            avg_sec = (insight.ig_reels_avg_watch_time_ms or 0) / 1000.0
            out.append(
                Insight(
                    id="reels_low_watch_completion",
                    icon="⏱️",
                    severity="warning",
                    title="중반 이탈 의심",
                    message=(
                        f"초반 생존 후에도 평균 시청 시간이 {avg_sec:.0f}초"
                        f"(총 {duration:.0f}초 영상)에 머물러 있습니다. "
                        f"영상 중반부({avg_sec:.0f}초 부근)에 루즈해지는 구간이 있는지 확인하세요."
                    ),
                    metric={
                        "avg_watch_seconds": round(avg_sec, 1),
                        "duration_seconds": duration,
                        "completion_rate_pct": wcr,
                    },
                )
            )
        elif wcr >= 80.0:
            out.append(
                Insight(
                    id="reels_high_watch_completion",
                    icon="🎯",
                    severity="info",
                    title="시청 완료율 우수",
                    message=(
                        f"평균 시청 완료율이 {wcr:.0f}%로 매우 높습니다. "
                        "동일 길이/구조 포맷을 시리즈로 확장하면 안정적인 결과가 기대됩니다."
                    ),
                    metric={"completion_rate_pct": wcr},
                )
            )
    return out


# ─────────────────────────────────────────────────────────────
# 인사이트 매트릭스 룰 (화면 3 — 우측 사이드바)
# ─────────────────────────────────────────────────────────────


def rule_useful_but_heavy(media: IGMedia, insight: IGMediaInsight) -> Insight | None:
    """높은 저장 + 낮은 공유 = 유용하지만 무거운 콘텐츠."""
    saved = insight.saved or 0
    shares = insight.shares or 0
    if saved < 10:
        return None
    if shares > 0 and saved / max(shares, 1) < Thresholds.SAVED_VS_SHARES_RATIO:
        return None
    if shares == 0 and saved < 30:
        # 데이터 부족
        return None
    return Insight(
        id="useful_but_heavy",
        icon="💡",
        severity="info",
        title="유용하지만 무거운 콘텐츠",
        message=(
            f"저장({saved}개) 대비 공유({shares}개)가 현저히 낮습니다. "
            "유저들이 혼자만 보려고 저장하는 정보성 콘텐츠입니다. "
            "다음에는 '친구를 태그하면 템플릿 제공' 같은 공유 유도 캡션을 추가해 보세요."
        ),
        metric={"saved": saved, "shares": shares, "ratio": round(saved / max(shares, 1), 1)},
    )


def rule_viral_meme(media: IGMedia, insight: IGMediaInsight) -> Insight | None:
    """높은 공유/도달 + 낮은 팔로우 전환 = 가벼운 밈."""
    reach = insight.reach or 0
    follows = insight.follows or 0
    if reach < Thresholds.HIGH_REACH_LOW_FOLLOW_REACH_MIN:
        return None
    follow_rate = (follows / reach * 100) if reach else 0.0
    if follow_rate >= Thresholds.FOLLOW_RATE_LOW:
        return None
    return Insight(
        id="viral_meme",
        icon="⚠️",
        severity="warning",
        title="바이럴은 성공, 브랜드 연결 약함",
        message=(
            f"공유로 인해 도달은 폭발({reach:,})했으나 프로필 유입({follows})이 거의 없습니다. "
            "바이럴은 성공했지만 우리 브랜드로 연결되지 않았습니다. "
            "릴스 마지막에 '프로필에서 더 보기' CTA나 화살표 스티커를 삽입하세요."
        ),
        metric={"reach": reach, "follows": follows, "follow_rate_pct": round(follow_rate, 3)},
    )


def rule_followers_only(media: IGMedia, insight: IGMediaInsight) -> Insight | None:
    """
    "고인물 콘텐츠" — 신규 유저 노출 부족 (게시물-수준 proxy).

    ⚠️ 정확도 한계: Meta v25 의 미디어 인사이트는 home/explore/profile 같은
    surface_type breakdown 을 제공하지 않는다 (계정-수준만 follow_type 지원).
    따라서 이 룰은 follow_rate + profile_visits 가 낮을 때를 "팔로워 위주"
    신호로 간주하는 proxy. 카드 텍스트에 "(계정-수준 추정)" 라벨을 명시하고,
    정확한 신호는 별도 엔드포인트 `/accounts/{id}/audience-insight/` 의
    `rule_account_followers_dominant` 카드에서 노출.
    """
    reach = insight.reach or 0
    if reach <= 0 or reach >= Thresholds.HIGH_REACH_LOW_FOLLOW_REACH_MIN:
        return None
    follows = insight.follows or 0
    profile_visits = insight.profile_visits or 0
    if follows >= 5 or profile_visits >= 20:
        return None
    return Insight(
        id="followers_only",
        icon="🔍",
        severity="info",
        title="기존 팔로워 위주 도달 (게시물-수준 추정)",
        message=(
            f"도달이 {reach:,}로 제한적이고 프로필 방문({profile_visits}) / 팔로우 전환({follows})이 "
            "낮습니다. 알고리즘이 새 유저에게 추천하지 않은 것으로 보입니다. "
            "이번에 사용한 해시태그·캡션 키워드가 트렌드와 맞지 않을 수 있으니 점검하세요. "
            "※ Meta API 가 게시물별 home/explore 도달 분리를 제공하지 않아 follow 전환·프로필 방문량 "
            "기반 추정값입니다. 계정 전체 추이는 우측 '계정 청중 인사이트' 카드를 참고하세요."
        ),
        metric={"reach": reach, "follows": follows, "profile_visits": profile_visits},
    )


def rule_top_performer(media: IGMedia, insight: IGMediaInsight) -> Insight | None:
    """ER 우수 → 광고로 부스팅 권장."""
    er = insight.engagement_rate
    if er is None or er < Thresholds.ER_TOP_BAND:
        return None
    return Insight(
        id="top_performer",
        icon="🚀",
        severity="info",
        title="상위 성과 — 광고 부스팅 후보",
        message=(
            f"인게이지먼트율이 {er:.1f}%로 평균을 크게 상회합니다. "
            "광고 예산을 투입해 도달을 확장하면 효율이 가장 높을 가능성이 큰 게시물입니다."
        ),
        metric={"engagement_rate": er},
    )


def rule_paid_vs_organic(media: IGMedia, insight: IGMediaInsight) -> Insight | None:
    """광고 연동 시 — paid 효율 코멘트."""
    if not insight.has_paid_data:
        return None
    paid_reach = insight.paid_reach or 0
    paid_clicks = insight.paid_link_clicks or 0
    if paid_reach < 100:
        return None
    paid_ctr = paid_clicks / paid_reach * 100 if paid_reach else 0
    # 오가닉 추정
    organic_reach = insight.organic_reach
    organic_eng = (insight.likes or 0) + (insight.comments or 0) + (insight.saved or 0)
    organic_rate = organic_eng / organic_reach * 100 if organic_reach else 0
    if paid_ctr < organic_rate:
        return None
    return Insight(
        id="paid_efficient",
        icon="💰",
        severity="info",
        title="광고 효율이 오가닉보다 높음",
        message=(
            f"오가닉 인게이지먼트율({organic_rate:.2f}%) 대비 광고 CTR({paid_ctr:.2f}%)이 "
            "더 높습니다. 크리에이티브가 훌륭하니 타겟을 넓혀 예산을 증액해도 좋습니다."
        ),
        metric={
            "organic_rate_pct": round(organic_rate, 2),
            "paid_ctr_pct": round(paid_ctr, 2),
        },
    )


# 화면 3 룰 카탈로그 — 매트릭스에서 평가할 룰 순서
MATRIX_RULES: list[Callable[[IGMedia, IGMediaInsight], Insight | None]] = [
    rule_useful_but_heavy,
    rule_viral_meme,
    rule_followers_only,
    rule_top_performer,
    rule_paid_vs_organic,
]


# ─────────────────────────────────────────────────────────────
# 계정 단위 룰 (follow_type breakdown 활용 — IGAccountInsight)
# ─────────────────────────────────────────────────────────────


def rule_account_followers_dominant(ai: IGAccountInsight) -> Insight | None:
    """
    계정 전체 도달이 follower 위주 = 신규 유저 노출 부족.

    Meta v25 의 계정 단위 `breakdown=follow_type` 데이터를 사용하므로
    proxy 가 아닌 직접 데이터. 게시물별로 쪼갤 수는 없지만 계정 추세는 정확.
    """
    share = ai.follower_share_pct
    if share is None or share < Thresholds.ACCOUNT_FOLLOWER_SHARE_DOMINANT:
        return None
    return Insight(
        id="account_followers_dominant",
        icon="🔍",
        severity="warning",
        title=f"최근 {ai.period_days}일 도달의 {share:.0f}%가 팔로워",
        message=(
            f"최근 {ai.period_days}일 동안 발생한 도달({ai.total_reach:,}) 중 "
            f"{share:.0f}%가 기존 팔로워에서 발생했습니다. 비팔로워 도달이 "
            f"{ai.non_follower_share_pct or 0:.0f}%로 낮아 신규 유저 노출이 부족합니다. "
            "최근 콘텐츠의 해시태그·오디오 트렌드 적합도, 릴스 비중, "
            "탐색 친화적인 캡션 키워드를 점검해 보세요."
        ),
        metric={
            "period_days": ai.period_days,
            "total_reach": ai.total_reach,
            "follower_share_pct": share,
            "non_follower_share_pct": ai.non_follower_share_pct,
        },
    )


def rule_account_healthy_acquisition(ai: IGAccountInsight) -> Insight | None:
    """비팔로워 도달 비중이 건강한 수준 — 우수 신호."""
    nf = ai.non_follower_share_pct
    if nf is None or nf < Thresholds.ACCOUNT_NON_FOLLOWER_SHARE_HEALTHY:
        return None
    return Insight(
        id="account_healthy_acquisition",
        icon="🌱",
        severity="info",
        title=f"신규 유저 도달 비중 {nf:.0f}% — 양호",
        message=(
            f"최근 {ai.period_days}일 동안 비팔로워 도달이 전체의 {nf:.0f}%를 차지합니다. "
            "알고리즘이 신규 유저에게 콘텐츠를 적극적으로 추천하고 있습니다. "
            "이 흐름을 유지하려면 최근 잘 된 포맷·해시태그를 시리즈로 확장해 보세요."
        ),
        metric={
            "period_days": ai.period_days,
            "non_follower_share_pct": nf,
            "follower_share_pct": ai.follower_share_pct,
        },
    )


ACCOUNT_RULES: list[Callable[[IGAccountInsight], Insight | None]] = [
    rule_account_followers_dominant,
    rule_account_healthy_acquisition,
]


def diagnose_account(ai: IGAccountInsight | None) -> list[Insight]:
    """계정 단위 룰 평가 — `/accounts/{id}/audience-insight/` 응답의 cards 필드."""
    if ai is None:
        return []
    out: list[Insight] = []
    for rule in ACCOUNT_RULES:
        try:
            res = rule(ai)
        except Exception:
            res = None
        if res is not None:
            out.append(res)
    return out


def diagnose_media(media: IGMedia, insight: IGMediaInsight | None) -> list[Insight]:
    """
    화면 2 + 화면 3 통합 진단 — 모든 룰 평가 후 매칭된 Insight 리스트 반환.

    Insight 가 0개일 수도 있다 (= 평범한 결과). 그 경우 프론트는 "특이사항 없음" 표시.
    """
    if insight is None:
        return []
    out: list[Insight] = []
    # 릴스 약점 진단 먼저
    out.extend(diagnose_reels_hook(media, insight))
    # 매트릭스 룰
    for rule in MATRIX_RULES:
        try:
            res = rule(media, insight)
        except Exception:
            res = None
        if res is not None:
            out.append(res)
    return out
