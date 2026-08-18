"""DM 캠페인 이전 — 게시물 1건 복원 (정밀도 우선).

연구(2026-08-13~14, 실계정 4곳·게시물 1,000+)에서 확정된 규칙만 담는다.

핵심 3가지
    1. **첨부를 읽는다** — 버튼 DM 은 ``message`` 가 비어 있고 본문이 ``attachments`` 안에 있다.
       실측 은닉율 67~100%. 이걸 안 읽으면 복원율이 0 이 된다.
    2. **지지비율로 판정한다** — 같은 게시물의 여러 댓글러에게 *공통으로* 간 문구만 그 게시물의
       캠페인이다. 1명에게만 간 DM 은 86%가 다른 게시물에서 흘러든 것.
       실측 정밀도: ≤20% → 11% · 40~60% → 77% · 60%+ → 100%.
    3. **게이트와 오퍼를 따로 뽑는다** — 게이트(팔로우 확인)는 댓글러 전원에게 가서 지지 100%,
       오퍼(자료 링크)는 게이트 통과자만 받아 지지가 낮다. 최고 지지 하나만 뽑으면
       **구조적으로 링크 없는 게이트만** 나온다. 분리하니 오퍼 URL 확보가 3건 → 42건(100%).

버린 신호 (측정으로 기각)
    · 시간 컷오프를 1분까지 좁혀도 정밀도 76% — 한 사람이 여러 게시물에 연달아 댓글을 달면
      각 캠페인 DM 이 모두 몇 초 안에 도착해 시간으로 갈리지 않는다.
    · 트리거 일치율 — 수신자는 정의상 이 게시물 댓글러라 항상 트리거를 포함한다(무의미).
    · 공개답글 수 — 일상 게시물에 답글이 많고 대형 캠페인엔 없어 신호가 역방향.
    · 배타 할당(한 문구를 한 게시물에만) — 같은 게이트 문구를 전 게시물에서 쓰는 계정에서
      42개 중 35개를 죽였다. 지지비율만으로 충분하다.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import collect as C
from .analyze import (
    EMOJI_TOKEN,
    caption_keywords,
    comment_shape,
    content_match,
    fingerprint,
    is_personal_dm,
    normalize_comment,
    placeholder_normalize,
    wilson_lower_bound,
)

logger = logging.getLogger(__name__)

# ── 등급 컷 ──
# 목표는 "사람이 검수할 게 적게, 정확하게, 전부 옮기기"(CLAUDE.md §1)다. 검수 목록이
# 길면 그 자체로 실패다.
#
# ⚠️ 자동채택을 **신뢰하한**으로 재던 것이 실패였다. 연구가 "지지 60%+ → 정밀도 100%"
#    라고 측정한 건 **받은 비율**인데 코드는 윌슨 신뢰하한을 썼다. 표본 10명이면
#    9/10(90%)의 신뢰하한이 0.596 이라 **0.004 차이로** 자동채택에서 떨어진다.
#    실측(@highestlevel33): 검수필요 108건 중 **61건이 비율 60%+ 인데 신뢰하한 미달**
#    이었고, 그중 10건은 9/10(90%)였다. 사람에게 "10명 중 9명이 받은 것"을 검수시키는
#    셈이라 목표와 정면으로 어긋난다.
# → 자동채택은 **비율**로 재고, 신뢰하한은 표본이 아주 작을 때만 거르는 보조로 쓴다.
GRADE_AUTO_RATIO = 0.60  # 받은 비율 — 연구가 정밀도 100% 를 측정한 그 기준
# 초소표본 방어는 신뢰하한이 아니라 **조회 인원**으로 한다. 신뢰하한을 바닥으로 쓰면
# 6/10(60%, 하한 0.313)·8/12(67%, 0.391) 처럼 연구가 검증한 구간이 막힌다.
# 반대로 3/3(100%)은 하한 0.438 로 통과해버려 방어가 거꾸로 걸린다.
MIN_PROBED_FOR_AUTO = 5  # 3/3 같은 "몇 명 안 봤는데 다 맞음" 을 걸러낸다
# ⚠️ **비율은 조회 인원을 늘리면 저절로 떨어진다.** 조회를 10명 → 50명으로 키운 뒤
#    실측(@highestlevel33, 251건 시점): 검수필요 77건 중 **65건이 "지지 3명+ 인데 비율<60%"**
#    였고, 그 안에는 `29/49(비율 0.59) · 댓글 후 34초` 처럼 자동 발송이 명백한 건이 줄줄이
#    있었다. 깊게 파면 캠페인 시작 전 댓글·키워드 불일치 댓글·DM 차단 계정이 분모에 섞인다.
#    비율 컷은 10명 조회 시절 기준이라, 조회를 늘린 개선이 스스로 판정을 망가뜨렸다.
# → 그래서 자동채택 두 번째 문을 **'몇 명이 몇 초 안에 받았나'**(auto_hits)로 만든다.
#    분모가 없으므로 조회를 더 깊게 파도 판정이 흔들리지 않는다.
AUTO_FAST_MIN_HITS = 5
# 자동채택 ③ — **페이서로 지연된 자동 발송**. 도구들도 우리처럼 발송을 흩뿌리므로
# 중앙값이 몇 분까지 밀린다. 같은 문구를 3명 이상이 1시간 안에 받았고 글·댓글도 캠페인
# 이라고 말하면 사람에게 안 묻는다. 실측(@highestlevel33): 이 문을 열면 검수 34 → 24 로
# 줄고, 남는 24건 중 22건은 "문구가 다른 게시물 것"(사장님 규칙상 확인 대상)이다.
SLOW_AUTO_MIN_HITS = 3
SLOW_AUTO_MAX_GAP = 3600
GRADE_REVIEW = 0.40  # 확인 권장 — 실측 정밀도 77%
MIN_SUPPORT_HITS = 3  # 표본이 작을 때 비율만 믿지 않기 위한 절대 하한
GRADE_AUTO = GRADE_AUTO_RATIO  # 하위 호환(외부 참조)
# 지지 1~2명짜리를 살릴 때 요구하는 콘텐츠 점수 하한. 사장님이 애매 59건을 전수 검수한
# 결과 **0.55 이하는 전부 캠페인이 아니었다**(2026-08-17). 자동 판정으로는 못 얻는 값이라
# 사람 라벨을 그대로 상수로 박는다 — 바꾸려면 같은 방식으로 다시 라벨링할 것.
WEAK_SUPPORT_CONTENT_MIN = 0.55

# 캡션 행동유도. 어미 변화를 놓치면 안 된다 — 실측에서 "아무 댓글 **남기면**" 캠페인이
# "남겨" 만 보던 패턴에 안 걸려 통째로 탈락했다(점수 0.34, 기준 0.35).
_CAP_CTA_RE = re.compile(
    r"댓글에|댓글로|댓글\s*남|댓글\s*달|댓글\s*주|댓글\s*만|아무\s*댓글|남겨|남기|"
    r"입력|디엠|\bDM\b|dm\s*드|메시지\s*드",
    re.I,
)
# 인스타 시스템 알림·상투 문구 — 캠페인 DM 이 아니다.
_SYSTEM_NOISE = re.compile(r"^(답글\s*\d+개|좋아요\s*\d+개|스토리|사진을 보냈습니다)", re.I)


@dataclass
class PostRecovery:
    """게시물 1건 복원 결과."""

    media_id: str
    probed: int = 0
    trigger: str | None = None
    repetition: float = 0.0
    is_campaign_signal: bool = False  # 글·댓글 종합 판정 (judge_content)
    content_score: float = 0.0  # 콘텐츠 판정 점수 0~1
    content_reasons: list = field(default_factory=list)  # 어떤 신호가 걸렸나
    offer: dict | None = None  # {text, url, label, hits, ratio, score}
    gate: dict | None = None
    drops: list = field(default_factory=list)
    samples: list = field(default_factory=list)  # 근거 원문(7일 후 파기)
    keyword_hits: dict = field(default_factory=dict)
    # 캡션 ↔ 복원된 DM 이 **같은 이야기인가** — 겹친 고유 낱말.
    # 이미 있던 analyze.content_match 를 AI 대조 전처리에만 쓰고 **등급에는 안 썼다.**
    # 그게 구멍이었다(2026-08-18 사장님 검수 13건 전수).
    content_match: list = field(default_factory=list)
    # ── 캐시용: 어디까지 팠나 + 다음 실행에 물려줄 조회 대상 ──
    # 두 축(댓글·대화)을 다 소진했는지 남긴다(CLAUDE.md §1 '소진의 기준'). 조회 풀은
    # 끝까지 파서 얻은 캠페인 시기 댓글러라 **가장 비싼 재작업(200페이지)을 없애준다.**
    dug_all_comments: bool = False
    dug_conversations: bool = False
    probe_pool: list = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.offer or self.gate)

    @property
    def score(self) -> float:
        """등급 판정 점수 — **오퍼 기준**(사용자에게 중요한 건 자료 링크).

        키 접근이 아니라 get 을 쓴다 — 재개·이전 버전 캐시에서 온 기록은 필드가 빠져 있을
        수 있고, 여기서 KeyError 가 나면 잡 전체가 죽는다.
        """
        if self.offer:
            return float(self.offer.get("score") or 0.0)
        return float(self.gate.get("score") or 0.0) if self.gate else 0.0

    @property
    def grade(self) -> str:
        """등급 — :meth:`verdict` 의 앞부분."""
        return self.verdict()[0]

    @property
    def reject_reason(self) -> str:
        """제외한 **이유**. 빈 문자열이면 제외가 아니다.

        왜 이유를 남기나 — AI 내용 대조(:func:`attribute.apply_verdicts`)가 제외된 건을
        검수로 되살리는데, **왜 제외됐는지 안 보고 되살렸다.** 실측(@highestlevel33,
        2026-08-18): 검수 17건 중 10건이 이렇게 올라온 것이었고 간격이 `516,379초`·
        `602,519초`(6~7일)였다. 이 댓글의 응답일 수 없는 DM 이고, AI 는 시간을 보지
        않으므로 되살릴 자격이 없다. 사장님 라벨(내용 0.55 이하)도 마찬가지다.
        """
        return self.verdict()[1]

    def verdict(self) -> tuple[str, str]:
        """``(등급, 제외이유)``. **DM 증거와 콘텐츠 판정을 함께 본다.**

        예전에는 DM 증거만 봤다. 그래서 두 방향으로 틀렸다(실측 @highestlevel33):
          · 글이 "캠페인 맞다" 는데 DM 을 못 찾아 **52개를 통째로 버렸다**
          · 글이 "캠페인 아니다" 는데 DM 한 통 나왔다고 **35개를 통과시켰다**
        이제 콘텐츠가 강하면 DM 이 없어도 후보로 내고(문구는 사용자가 작성), 콘텐츠가
        아니라는데 지지가 1~2명뿐이면 내린다(= 남의 게시물 DM 이 흘러든 것).

        제외이유는 AI 구제 가능 여부를 가른다 — ``thin_support`` 만 되살릴 수 있다.
        """
        s = self.score
        hits = (self.offer or self.gate or {}).get("hits", 0)
        if not self.found:
            # DM 원문을 못 건진 건. 밴드는 excluded 를 유지하고(프론트 계약), 후보로 낼지는
            # 파이프라인이 content_score 로 정한다 — 글·댓글이 캠페인이라고 말하면 낸다.
            return "excluded", "not_found"
        best = self.offer or self.gate or {}
        # ratio 가 없는 옛 기록(재개·이전 버전 캐시)은 hits/probed 로 되살린다.
        ratio = float(best.get("ratio") or 0.0) or (hits / max(self.probed, 1))
        offer = self.offer or {}
        has_text = bool((offer.get("text") or "").strip())
        # ── 자동채택의 최소 조건: **옮길 수 있는 것이 있어야 한다** ──
        # 2026-08-18 사장님 지시: "끝까지 다 팠는데도 링크가 없으면 자동채택에서 빼는 게 맞다."
        # 실측 사고(C3SqJuhxpah): 링크 없는 3건이 자동채택에 있었고 문구가
        # "자료는 [링크]에서 확인하실 수 있어요" — **자리표시자가 그대로** 였다.
        # 그대로 켜면 "[링크]" 라는 글자가 DM 으로 나간다.
        # 초안 문구도 안 된다 — LLM 이 쓴 글을 옮기는 건 '복원' 이 아니다.
        #
        # ⚠️ 이건 **자동채택 문에만** 건다. 등급을 여기서 결정하면 아래의 제외 이유
        #    (content_says_no·impossible_timing)가 덮여 "왜 제외됐나" 가 흐려진다.
        #    (게이트만 나온 건도 여기서 함께 막힌다 — 옮길 링크가 없다.)
        can_auto = bool((offer.get("url") or "").strip()) and not offer.get("text_is_draft")
        # 자동채택 — 받은 **비율**이 60%+ 이고, 사람 수와 조회 인원이 충분하면 사람 손을
        # 안 탄다. 검수 목록을 짧게 유지하는 것이 이 기능의 목표다.
        if (
            can_auto
            and ratio >= GRADE_AUTO_RATIO
            and hits >= MIN_SUPPORT_HITS
            and self.probed >= MIN_PROBED_FOR_AUTO
        ):
            return "auto_draft", ""
        # 자동채택 ② — **자동 발송 지문**. 사람이 흉내낼 수 없는 것은 비율이 아니라 속도다.
        # "5명 이상이 자기 댓글 60초 안에 **같은 문구**를 받았다" 면 비율이 얼마든 캠페인이다.
        # 게이트가 아니라 오퍼 슬롯만 본다 — 옮길 문구가 있어야 후보로서 의미가 있고,
        # 게이트 문구는 원래 전 게시물에 공유돼 이 잣대로 재면 안 된다.
        # (문구 경쟁에서 내려간 오퍼는 attribute._repack 이 auto_hits 를 0 으로 만든다.)
        if (
            can_auto
            and int(offer.get("auto_hits") or 0) >= AUTO_FAST_MIN_HITS
            and has_text
            and self.probed >= MIN_PROBED_FOR_AUTO
        ):
            return "auto_draft", ""
        # 자동채택 ③ — 페이서로 몇 분 밀린 자동 발송. 속도가 느린 대신 **글·댓글도 캠페인
        # 이라고 말할 때만** 통과시킨다(사장님이 라벨링한 0.55 컷을 그대로 쓴다).
        gap_med = offer.get("gap_median")
        if (
            can_auto
            and int(offer.get("hits") or 0) >= SLOW_AUTO_MIN_HITS
            and gap_med is not None
            and 0 <= gap_med <= SLOW_AUTO_MAX_GAP
            and has_text
            and self.content_score > WEAK_SUPPORT_CONTENT_MIN
            and self.probed >= MIN_PROBED_FOR_AUTO
        ):
            return "auto_draft", ""
        # 자동채택 ④ — **간격을 컷이 아니라 곡선으로.** 느릴수록 더 많은 사람이 받았어야
        # 인정한다(collect.required_hits). 사장님이 @reels_drgn 검수 31건을 눈으로 보고
        # **27건이 실제 캠페인**(내용 일치·복원 정확)이라고 확인한 데서 나왔다 —
        # 간격이 길어지는 실제 사유가 있다(도구 오류로 지연 발송·나중에 손으로 보냄).
        # 인스타 Private Reply 창이 7일이므로 1시간에서 0 으로 떨어뜨릴 근거가 없다.
        #
        # ⚠️ **글 점수 0.55 초과를 요구한다.** 사장님이 애매 59건을 전수 라벨링한 컷이고,
        #    이 문이 그것을 우회하면 @highestlevel33 에서 제대로 걸러낸 것들이 풀린다.
        #    (시뮬레이션 확인: 제외 127건 → 127건 그대로.)
        if (
            can_auto
            and has_text
            and self.probed >= MIN_PROBED_FOR_AUTO
            and self.content_score > WEAK_SUPPORT_CONTENT_MIN
            and int(offer.get("hits") or 0) >= C.required_hits(gap_med, MIN_SUPPORT_HITS)
        ):
            return "auto_draft", ""
        # 자동채택 ⑤ — **캡션이 주겠다고 한 것과 DM 이 준 것이 같다.**
        # 사장님이 검수 13건을 눈으로 보고 8건을 "실제로 DM 캠페인도 맞고 내용도 일치함"
        # 이라고 지적한 데서 나왔다(2026-08-18). 아쉬운 8건과 잘 걸른 3건을 가른 것이
        # 정확히 이 신호였다 —
        #   살릴 것: 캡션 "'파도' 검색하면"      ↔ DM "파도와 연인 프롬프트 전달드립니다"
        #   버릴 것: 캡션 "캡컷 편집 효과 4가지" ↔ DM "Higgsfield X Claude 링크"
        # 내용이 일치하면 **인원·간격 요구를 완화**한다(간격은 7일 창 안이기만 하면 된다).
        # 지지 3명 하한은 유지한다 — 1명짜리는 남의 게시물 DM 이 흘러든 것이 86% 였다.
        # ⚠️ 초안 텍스트로는 이 문을 열지 않는다. 캐시가 LLM 초안을 원문 자리에 넣어주면
        #    "LLM 이 캡션 보고 쓴 글" 을 캡션과 대조하는 **순환**이 된다.
        if (
            can_auto
            and has_text
            and int(offer.get("hits") or 0) >= MIN_SUPPORT_HITS
            and self.content_match
            and C.gap_confidence(gap_med) > 0
        ):
            return "auto_draft", ""
        if s >= GRADE_REVIEW:
            return "needs_review", ""
        if hits >= MIN_SUPPORT_HITS:
            return "needs_review", ""
        # ── 지지 1~2명 — 여기가 오귀속의 온상이다 ──
        slot = self.offer or self.gate or {}
        gap = slot.get("gap_median")

        # ① 댓글 → DM 간격이 우선한다. 자동화 도구는 몇 초 안에 쏘고, 사람은 그렇게 못 한다.
        #    실측: 이 구간(≤60초)의 99%가 지지 3명+ 였다. 낱말·말투와 달리 계정 성격을
        #    안 타므로 가장 신뢰할 수 있는 잣대다.
        # ⚠️ **사람 라벨이 간격 신호보다 앞에 온다.** 순서가 뒤집혀 있었다 — 간격 검사가
        #    먼저라서 "60초 안에 1통 왔으면 인정" 이 사장님 라벨을 덮었다. 실측
        #    (@highestlevel33, 2026-08-18): 검수 17건 중 11건이 글·댓글에 캠페인 기미가
        #    거의 없는데(내용 0.00~0.40, 8건은 0.10 이하) 이 경로로 살아남은 것이었다.
        #    간격은 **추론**이고 0.55 컷은 사장님이 59건을 눈으로 보고 매긴 **사실**이다.
        #    지지가 1~2명뿐일 때는 사실이 이긴다.
        # (지지가 3명+ 이거나 자동채택 조건을 맞춘 건은 여기까지 내려오지 않는다 —
        #  실측 `46/46 · 내용 0.245` 처럼 글이 조용해도 도달이 넓으면 위에서 확정된다.)
        if self.content_score <= WEAK_SUPPORT_CONTENT_MIN:
            return "excluded", "content_says_no"

        # 여기부터는 글·댓글이 캠페인이라고 말하는 건들이다. 이제 간격을 본다.
        if gap is not None:
            if 0 <= gap <= C.AUTO_DM_MAX_GAP:
                return "needs_review", ""  # 자동 발송 확실 — 1명이 받았어도 인정
            if gap > C.EVIDENCE_MAX_GAP or gap < -C.CLOCK_SKEW_TOLERANCE:
                return "excluded", "impossible_timing"  # 사람이 쓴 것 / 이 댓글의 응답이 아님

        # 간격이 애매한 구간(1분~1일)은 링크 유무로 가른다 — 링크 없는 것은 대부분 팬과의
        # 1:1 잡담이었다(위 59건 전수 검수).
        if (self.offer or {}).get("url"):
            return "needs_review", ""
        return "excluded", "no_link"

    @property
    def confirm_required(self) -> bool:
        """사용자에게 '이 링크가 맞나요?' 를 물어야 하는가.

        지지 표본이 부족하면 링크가 다른 캠페인 것일 수 있다. 자동채택 등급이 아니면 확인.
        DM 을 아예 못 찾아 콘텐츠만으로 낸 후보도 당연히 확인 대상이다(문구가 비어 있다).
        """
        return self.grade != "auto_draft" and self.grade != "excluded"


def _is_noise(text: str, has_url: bool) -> bool:
    if not text:
        return True
    if _SYSTEM_NOISE.match(text.strip()):
        return True
    if has_url:
        return False
    compact = placeholder_normalize(text).replace("{emoji}", "").replace(" ", "")
    return len(compact) < 6


# ── 콘텐츠만으로 보는 캠페인 판정 (가중치는 실측값) ────────────────────────
#
# DM 을 찾았는지와 **무관하게**, 게시물 글과 댓글 모양만 보고 "여기 캠페인이 돌았나" 를
# 판정한다. 이게 있어야 "글은 캠페인이라는데 DM 을 못 건진" 게시물을 버리지 않을 수 있다.
#
# 가중치는 감이 아니라 실측이다. @highestlevel33 459개를 "확실(받은 사람 3명+)" 147개와
# "오탐 의심(받은 사람 1~2명)" 59개로 갈라, 각 신호가 양쪽에서 몇 %나 나오는지 쟀다.
#
#   신호                  확실   의심   차이
#   댓글 복붙 20%+         99%   29%   +0.71
#   캡션 행동유도           96%   41%   +0.55
#   초단문(3자↓) 30%+      64%   17%   +0.47
#   캡션 제공약속           81%   51%   +0.30
#   계정 대댓글 '보냈다'      2%    7%   -0.05  ← 이 계정은 안 쓴다. 다른 계정 대비 낮은 가중치로만
#
# 대댓글 **수**는 쓰지 않는다 — 캠페인 게시물은 댓글이 수백 개라 첫 페이지에 답글이 안 잡히고,
# 규모를 맞춰 비교하면 차이가 82% vs 94% 로 사라진다(수집 창 편향).
CONTENT_WEIGHTS = {
    "repetition": 0.35,  # 같은 말 복붙
    "caption_cta": 0.30,  # "댓글에 ○○ 남겨주세요"
    "tiny_comments": 0.20,  # 이모지·초단문 위주
    "caption_offer": 0.10,  # "무료 자료 드려요"
    "owner_reply_sent": 0.15,  # 계정이 "DM 보내드렸어요" 라고 답글
    # ⚠️ 위 가중치는 @highestlevel33 에서 뽑았고, **그 계정은 캡션에 행동유도를 96% 썼다.**
    #    캡션에 "댓글 남겨주세요" 를 안 쓰고 낱말만 흘리는 계정은 caption_cta(0.30)가 영영
    #    안 붙어 **구조적으로 0.45 에 갇힌다.** 실측(@reels_drgn, 2026-08-18 사장님 검수):
    #    복붙 80.6% · 트리거 'deevid' · 11/31명이 "Deevid AI 링크 보냅니다" 를 받은 게시물이
    #    0.45 로 검수에 떨어졌다. 복붙 40% 와 80% 가 같은 점수를 받는 것이 원인이었다.
    # → 트리거 낱말이 댓글에 **쏟아지면** 별도 신호로 센다. 캠페인 방식(캡션형/낱말형)에
    #    따라 신호가 갈리므로, 댓글 쪽에도 독립된 문이 있어야 한다.
    #    0.25 인 이유: 복붙(0.35)과 합쳐 **0.60** 이 되어 사장님 라벨 컷(0.55)을 넘고
    #    CONTENT_STRONG_MIN 에 닿는다. 댓글의 60%+ 가 같은 낱말이면 그것만으로
    #    캠페인이 확실하다는 뜻이다 — 캡션을 안 보고도 판정이 서야 한다.
    "trigger_flood": 0.25,
}
TRIGGER_FLOOD_MIN = 0.60  # 댓글의 이만큼이 같은 낱말이면 '쏟아진다' 로 본다
CONTENT_CAMPAIGN_MIN = 0.35  # 이 이상이면 "캠페인으로 본다"
CONTENT_STRONG_MIN = 0.60  # 이 이상이면 DM 을 못 찾아도 후보로 낸다

_CAP_OFFER_RE = re.compile(
    r"무료|자료|전자책|가이드|템플릿|특강|드려요|드립니다|보내드|정리해|나눔|공유해", re.I
)
_REPLY_SENT_RE = re.compile(r"보내드|보냈|드렸|디엠|\bDM\b|확인해\s*주|메시지\s*확인", re.I)


@dataclass
class ContentVerdict:
    """게시물 글 + 댓글 모양만으로 본 캠페인 판정."""

    trigger: str | None = None
    repetition: float = 0.0
    score: float = 0.0
    reasons: list = field(default_factory=list)
    shape: dict = field(default_factory=dict)

    @property
    def is_campaign(self) -> bool:
        return self.score >= CONTENT_CAMPAIGN_MIN

    @property
    def is_strong(self) -> bool:
        return self.score >= CONTENT_STRONG_MIN


def judge_content(
    media: dict, commenters: list[dict], owner_replies: list[str] | None = None
) -> ContentVerdict:
    """게시물 글·댓글·계정 대댓글을 **종합**해 캠페인 여부를 판정한다.

    하나의 신호로 자르지 않는다 — 캠페인 방식이 계정마다 달라서(키워드 지정형 / 아무 댓글이나
    받는 형 / 팔로우 게이트형) 어느 하나만 보면 그 방식만 잡힌다.
    """
    caption = (media.get("caption") or "").strip()
    texts = [u.get("text") or "" for u in commenters]
    shape = comment_shape(texts)
    v = ContentVerdict(repetition=shape["repetition"], shape=shape)

    hits: dict = {}
    if shape["repetition"] >= 0.20:
        hits["repetition"] = 1.0 if shape["repetition"] >= 0.40 else 0.7
    if _CAP_CTA_RE.search(caption):
        hits["caption_cta"] = 1.0
    if shape["tiny_ratio"] >= 0.30:
        hits["tiny_comments"] = 1.0
    if _CAP_OFFER_RE.search(caption):
        hits["caption_offer"] = 1.0
    if owner_replies and any(_REPLY_SENT_RE.search(t or "") for t in owner_replies):
        hits["owner_reply_sent"] = 1.0

    # 트리거 낱말이 댓글에 쏟아지는가 — 캡션에 행동유도를 안 쓰는 계정의 유일한 단서다.
    # (트리거 자체는 아래에서 정하지만, 판정은 '복붙 비율' 로 충분하다 — 같은 말이 60%면
    #  그 말이 트리거다.)
    if shape["repetition"] >= TRIGGER_FLOOD_MIN:
        hits["trigger_flood"] = 1.0

    v.score = round(min(1.0, sum(CONTENT_WEIGHTS[k] * w for k, w in hits.items())), 3)
    v.reasons = sorted(hits)

    # 트리거 단어 — 캡션이 인용한 것 우선, 없으면 가장 많이 복붙된 댓글.
    norms = [normalize_comment(t) for t in texts]
    norms = [n for n in norms if n]
    _, quoted = caption_keywords(caption)
    for q in quoted:
        qn = q.replace(" ", "")
        if qn and sum(1 for n in norms if qn in n.replace(" ", "")) >= 2:
            v.trigger = qn
            break
    if not v.trigger and shape["top_count"] >= 3 and shape["top_key"] != EMOJI_TOKEN:
        v.trigger = shape["top_key"].replace(" ", "")
    return v


def detect_signal(media: dict, commenters: list[dict]) -> tuple[str | None, float, bool]:
    """(하위 호환) → (트리거, 반복률, 캠페인 여부). 내부는 judge_content 가 판단한다."""
    v = judge_content(media, commenters)
    return v.trigger, v.repetition, v.is_campaign


def recover_post(
    ctx: C.CollectContext,
    media: dict,
    *,
    is_own_dm,
    seed: int = C.SEED_PROBE,
    full: int = C.FULL_PROBE,
    big: int = C.BIG_PROBE,
    campaign: int = C.CAMPAIGN_PROBE,
    workers: int = C.PROBE_WORKERS,
    probe: bool = True,
    seed_pool: list | None = None,
) -> PostRecovery:
    """게시물 1건을 복원한다.

    Args:
        is_own_dm: ``(msg_id, text) -> bool`` — 우리(TurnFlow)가 보낸 DM 판정.
            ⚠️ 이 콜러블은 **DB 를 만지지 않아야** 한다(스레드에서 호출됨).
    """
    mid = media.get("id") or ""
    mts = C.parse_graph_time(media.get("timestamp"))
    ncmt = media.get("comments_count") or 0
    out = PostRecovery(media_id=mid)

    if ctx.outbox:
        # 발신함 색인이 있으면 **색인에 있는 사람은 공짜**다(Graph 호출 0). 표본을 크게
        # 잡아 지지비율을 실측치에 가깝게 만든다 — "덜 봐서 애매한" 건이 사람 검수의
        # 최대 원인이었다(실측: 검수필요 108건 중 61건).
        # 색인에 없는 사람은 개별 조회로 내려가 돈이 드므로 무한대로 열지는 않는다.
        seed = max(seed, C.OUTBOX_SEED_PROBE)
        full = big = campaign = max(big, C.OUTBOX_MAX_PROBE)

    commenters, more = C.collect_commenters(ctx, mid, media_ts=mts, pages=1)
    # 댓글을 못 가져와도 **캡션만으로 판정은 남긴다.** 예전에는 여기서 그냥 return 해서
    # "댓글에 ○○ 남겨주세요" 라고 대놓고 쓰인 게시물이 점수 0 으로 사라졌다.
    verdict = judge_content(media, commenters or [])
    out.trigger, out.repetition = verdict.trigger, verdict.repetition
    out.is_campaign_signal = verdict.is_campaign
    out.content_score, out.content_reasons = verdict.score, verdict.reasons
    trigger = verdict.trigger
    if not commenters:
        return out
    if not probe:
        # 가벼운 경로 — 댓글이 적어 지지비율을 낼 수 없는 게시물. 판정만 하고 DM 은 안 본다
        # (표본이 1~7명이면 '몇 명이 같은 문구를 받았나' 가 의미를 못 가진다).
        return out

    tmpl: dict = {}
    probed_ids: set = set()

    swept_ids: set = set()  # 색인만으로 훑은 사람(모름 포함) — 중복 작업 방지용
    deep_ids: set = set()  # 대화를 끝까지 넘겨본 사람 — 두 번 넘기지 않게

    def _probe(users: list[dict], *, index_only: bool = False) -> None:
        """댓글러들이 받은 발신 DM 을 모아 문구별로 묶는다.

        ``index_only`` 면 발신함 색인만 본다(Graph 호출 0). 이때 **색인에 없는 사람은
        ``probed_ids`` 에 넣지 않는다** — 그건 '안 받았음' 이 아니라 '모름' 이고, 분모에
        넣으면 지지비율만 깎여서 멀쩡한 캠페인이 탈락한다.
        """
        if index_only:
            todo = [u for u in users if u["id"] not in probed_ids and u["id"] not in swept_ids]
            if not todo:
                return
            results = []
            for u in todo:
                swept_ids.add(u["id"])
                hits, known = C.outbound_from_index(ctx, u)
                if known:
                    results.append((u, hits))
        else:
            todo = [u for u in users if u["id"] not in probed_ids]
            if not todo:
                return
            # 병렬 조회 — 스레드는 HTTP 만. 집계는 아래 루프(메인 스레드)에서.
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(lambda u: (u, C.fetch_outbound_for_commenter(ctx, u)), todo))
        _absorb(results)

    def _probe_deep(users: list[dict]) -> None:
        """마지막 관문 — 대화를 **처음까지** 넘겨본다.

        여기가 20통 창을 뚫는 유일한 경로다. 대화 목록에 딸려오는 메시지는 최근 20통이
        상한이라, 그 사람이 이후에 DM 을 더 받았으면 오래된 캠페인 DM 이 창 밖으로 밀린다.
        실측(@highestlevel33): 우리가 가진 발신 DM 이 2025년 45,263건인데 **2024년은
        1,474건**뿐이다 — 2024년에 안 보낸 게 아니라 창에 가려 안 보이는 것이다.

        ⚠️ 예전에는 **N명을 전부 조회한 뒤에야** 결과를 봤다. 5번째에서 답이 나도 40명을
        다 사는 구조라, 사람 수를 못 늘렸다(12명). 이제 한 명씩 넣고 등급이 확정되면
        멈춘다 → 상한을 올려도 보통은 몇 명에서 끝난다.
        """
        todo = [u for u in users if u["id"] not in deep_ids]
        if not todo:
            return
        used = 0
        for u in todo:
            if ctx.budget.total_hit():
                break
            deep_ids.add(u["id"])
            _absorb([(u, C.fetch_outbound_deep(ctx, u))])
            used += 1
            if not _needs_more():
                break  # 답이 났다 — 여기서 멈춘다(뒤 사람들은 안 산다)
        if used:
            logger.info(
                "DM이전 대화 끝까지 파기 (media=%s): %d명 조회 (상한 %d)",
                mid,
                used,
                len(todo),
            )

    def _absorb(results: list) -> None:
        """조회 결과를 문구별 슬롯으로 묶는다(경로가 달라도 집계는 한 곳)."""
        for user, dms in results:
            probed_ids.add(user["id"])
            cts = user.get("ts")
            for d in dms:
                content = d.get("content") or {}
                urls = content.get("urls") or []
                if _is_noise(d["text"], bool(urls)):
                    continue
                if is_own_dm(d.get("msg_id"), d["text"]):
                    continue
                # 팬과의 1:1 잡담("존맛탱이죠 ㅎㅎ", "행복한 명절되세용")은 캠페인 문구가
                # 아니다. 그 사람이 마침 이 게시물에도 댓글을 달았을 뿐이다.
                # 버튼이 붙어 있으면 자동 발송이므로 잡담일 수 없다(게이트 보호).
                if is_personal_dm(
                    content.get("text") or d["text"],
                    has_url=bool(urls),
                    has_button=bool(content.get("buttons") or content.get("has_gate_button")),
                ):
                    continue
                # 댓글↔DM 시간차 — 수집 후 '이 DM 이 어느 게시물 것인가' 를 가리는 근거.
                # 문턱값으로 쓰면 안 갈린다(연달아 댓글 달면 둘 다 몇 초 안에 온다).
                # 여러 게시물이 같은 DM 을 주장할 때 **더 가까운 쪽**을 고르는 데 쓴다.
                dts = C.parse_graph_time(d.get("created_time"))
                gap = int((dts - cts).total_seconds()) if (dts and cts) else None
                key = placeholder_normalize(d["text"])[:120]
                slot = tmpl.setdefault(
                    key,
                    {
                        "users": set(),
                        "evidence": {},
                        "text": content.get("text") or d["text"],
                        "urls": Counter(),
                        "labels": Counter(),
                        "drops": set(),
                        "gate": False,
                        "samples": [],
                    },
                )
                slot["users"].add(user["id"])
                # 사용자당 가장 가까운 DM 1건만 근거로 둔다(팔로우게이트의 2통은 문구가
                # 달라 서로 다른 slot 에 들어가므로 여기서 잘리지 않는다).
                prev = slot["evidence"].get(user["id"])
                if prev is None or (gap is not None and abs(gap) < abs(prev.get("g") or 10**9)):
                    slot["evidence"][user["id"]] = {
                        "u": user["id"],
                        "m": d.get("msg_id") or "",
                        "g": gap,
                    }
                for u_ in urls:
                    slot["urls"][u_] += 1
                for b in content.get("buttons") or []:
                    if b.get("label"):
                        slot["labels"][b["label"]] += 1
                for code in content.get("media_drops") or []:
                    slot["drops"].add(code)
                if content.get("carousel"):
                    slot["drops"].add("carousel")
                if content.get("has_gate_button"):
                    slot["gate"] = True
                if len(slot["samples"]) < 3:
                    slot["samples"].append(
                        {"text": d["text"][:400], "created_time": d.get("created_time", "")}
                    )

    def _pack(slot, probed: int) -> dict:
        """문구 슬롯 → 결과 dict. ``probed`` 를 인자로 받는 이유는 **조사 중에도** 이걸
        불러 "지금 상태면 자동채택인가" 를 물어야 하기 때문이다(_would_auto_adopt)."""
        hits = len(slot["users"])
        url = slot["urls"].most_common(1)[0][0] if slot["urls"] else ""
        label = slot["labels"].most_common(1)[0][0] if slot["labels"] else ""
        gaps = sorted(
            g for g in (e.get("g") for e in slot.get("evidence", {}).values()) if g is not None
        )
        probed = max(int(probed or 0), 1)
        return {
            "text": slot["text"],
            "url": url,
            "label": label,
            # 누가·어느 메시지로·댓글과 몇 초 차이로 뒷받침했나 — 수집이 다 끝난 뒤
            # attribute.resolve 가 이걸 보고 게시물 간 중복 주장을 정리한다.
            "users": list(slot.get("evidence", {}).values()),
            # 댓글→DM 간격 — **자동 발송의 지문**. 중앙값과 '몇 초 안에 온 건수'를 남긴다.
            "gap_median": gaps[len(gaps) // 2] if gaps else None,
            "auto_hits": C.fast_hits(gaps),
            "hits": hits,
            "ratio": round(hits / probed, 3),
            "score": round(wilson_lower_bound(hits, probed), 3),
            "drops": sorted(slot["drops"]),
            "samples": slot["samples"],
        }

    def _best_slots(probed: int):
        """지금까지 모은 것에서 최고 오퍼(URL 있음)·최고 게이트를 뽑는다."""
        bo = bg = None
        for slot in tmpl.values():
            packed = _pack(slot, probed)
            if packed["url"]:
                if bo is None or packed["hits"] > bo["hits"]:
                    bo = packed
            elif bg is None or packed["hits"] > bg["hits"]:
                bg = packed
                bg["is_gate"] = slot["gate"]
        return bo, bg

    order = C.order_probe_targets(commenters, trigger)
    # 지난 실행이 **끝까지 파서** 얻어둔 캠페인 시기 댓글러 — 있으면 맨 앞에 세운다.
    # 이게 없으면 같은 게시물의 200페이지 댓글 페이징을 매번 다시 한다(가장 비싼 재작업).
    if seed_pool:
        known = {u["id"] for u in commenters}
        revived = [
            {"id": p["u"], "ts": C.parse_graph_time(p.get("ts")), "text": "", "replied": False}
            for p in seed_pool
            if p.get("u") and p["u"] not in known
        ]
        if revived:
            order = revived + order
            out.dug_all_comments = True  # 그 페이징 결과를 물려받았다
            logger.info(
                "DM이전 조회풀 재사용 (media=%s): %d명 — 댓글 재페이징 생략", mid, len(revived)
            )
    _probe(order[:seed])

    def _best_ratio() -> float:
        """지금까지 모은 근거 중 최고 지지비율."""
        if not probed_ids or not tmpl:
            return 0.0
        top = max(len(s["users"]) for s in tmpl.values())
        return top / max(len(probed_ids), 1)

    def _needs_more() -> bool:
        """**더 파야 하나** — 지금 상태로 끝내면 사람에게 넘어가나.

        이 함수가 '소진의 기준'(CLAUDE.md §1)의 문지기다. **세 번 틀렸고, 세 번 다 원인이
        같았다 — "무엇을 찾았나" 로 판정한 것.** 실측은 모두 @highestlevel33.

        ① ``if tmpl:`` — 게이트("팔로우 확인") 하나 나오면 더 파는 경로를 통째로 건너뛰었다.
           게이트는 전 게시물에 같은 문구가 나가 이 게시물의 근거가 못 된다.
           (댓글 10,050개 게시물이 최신 50개만 보고 종료됐다.)
        ② ``_found_offer()`` = "URL 있거나 **게이트 버튼이 안 달린** 슬롯" — 버튼 없는 텍스트
           DM 이 후자에 걸려 또 '찾았다' 가 됐다. 그런데 :func:`_pack` 은 **URL 있는 슬롯만**
           오퍼로 본다. 두 정의가 어긋나 검수 7건 중 4건이 "오퍼 없음" 인데도 안 팠다.
        ③ "URL 있는 오퍼의 지지가 3명 미만" — `지지 4명·링크O` 가 통과해 멈췄는데 **등급은
           간격이 창 밖이라 검수로 갔다.** 실측(2026-08-18): 검수 23건 중 19건이 두 축을
           하나도 안 판 상태였고 그중 10건은 캡션이 대놓고 캠페인이었다.

        "그만 파도 될 만큼 찾았나" 와 "**사람에게 안 물어도 될 만큼** 찾았나" 는 다른 질문인데
        계속 앞의 것으로 판정한 것이 잘못이었다.

        → **등급으로 판정한다.** 지금 상태로 자동채택이 안 되면 아직 안 끝난 것이다.
          판정 규칙을 여기 복제하지 않는다 — :class:`PostRecovery` 가 단일 소스여야
          등급 규칙을 고칠 때 이 문지기가 저절로 따라온다.
        """
        probed_now = max(len(probed_ids), 1)
        bo, bg = _best_slots(probed_now)
        # 옮길 **링크**가 아직 없으면 등급과 무관하게 안 끝났다. 등급만 보면 안 되는 이유:
        # 자동채택 1번 규칙(비율)이 ``offer or gate`` 를 보기 때문에, 게이트가 전원에게 가서
        # 비율 1.0 이 되면 **오퍼를 못 찾았는데도 auto_draft** 가 나온다(= ① 실패의 재현).
        if not bo or not (bo.get("url") or "").strip():
            return True
        probe = PostRecovery(
            media_id=mid,
            probed=probed_now,
            content_score=out.content_score,
            is_campaign_signal=out.is_campaign_signal,
            offer=bo,
            gate=bg,
        )
        return probe.grade != "auto_draft"

    def _would_review() -> bool:
        """지금 상태로 끝내면 **사람 검수 목록에 올라가나.**

        :func:`_needs_more` 와 한 글자 차이지만 쓰임이 다르다.
          · ``_needs_more``  = 자동채택이 아니다 (검수 **또는** 제외)
          · ``_would_review`` = 검수로 **올라간다** (제외는 아니다)

        더 팔지 말지를 정할 때는 이쪽을 봐야 한다. 제외로 떨어질 건은 아무도 안 보므로
        더 파도 사람의 일이 줄지 않는다 — 비용만 쓴다. 반대로 검수로 갈 건은 **우리가 덜
        본 것을 사람에게 떠넘기는 것**이라 끝까지 파야 한다.
        """
        # ⚠️ 근거가 **하나도** 없으면 여기서 True 를 주면 안 된다. 등급은 excluded 이고
        #    후보로 낼지는 파이프라인이 콘텐츠 점수로 정한다(= `verdict.is_strong` 이
        #    맡는 몫). 여기서 True 를 주면 글이 조용한 게시물까지 비싼 경로가 열려
        #    "비싼 경로는 유력 후보에만"(2026-08-17 제품 결정)이 무너진다.
        if not tmpl:
            return False
        probed_now = max(len(probed_ids), 1)
        bo, bg = _best_slots(probed_now)
        # 게이트만 나왔다 = 옮길 링크가 없다. 이대로 내면 사용자가 문구를 직접 써야 하므로
        # 사람의 일이다. 등급만 보면 게이트가 전원에게 가서 비율 1.0 → auto_draft 로
        # 잘못 끝난다(그래서 등급을 묻기 전에 링크를 본다).
        if not bo or not (bo.get("url") or "").strip():
            return True
        return (
            PostRecovery(
                media_id=mid,
                probed=probed_now,
                content_score=out.content_score,
                is_campaign_signal=out.is_campaign_signal,
                offer=bo,
                gate=bg,
            ).grade
            == "needs_review"
        )

    def _resolve_ambiguity() -> None:
        """판정이 애매하면 **결론이 날 때까지** 더 조회한다.

        10명에서 무조건 멈추면 9/10 같은 애매한 상태로 끝나고, 그걸 사람에게 넘기게 된다.
        우리가 덜 본 것을 검수로 떠넘기지 않는다 — 애매한 것만 골라 더 본다.
        """
        limit = max(C.ADAPTIVE_MAX_PROBE, C.OUTBOX_MAX_PROBE if ctx.outbox else 0)
        while len(probed_ids) < limit:
            r = _best_ratio()
            if r >= C.AMBIGUOUS_HIGH or r < C.AMBIGUOUS_LOW:
                return  # 이미 결론이 났다
            nxt = order[: len(probed_ids) + C.ADAPTIVE_STEP]
            if len(nxt) <= len(probed_ids):
                return  # 더 볼 사람이 없다
            before = len(probed_ids)
            _probe(nxt)
            if len(probed_ids) == before:
                return  # 예산 소진 등으로 진전 없음

    if not _needs_more():
        cap = big if ncmt >= C.BIG_COMMENTS else full
        _probe(order[:cap])
        _resolve_ambiguity()
    elif verdict.is_campaign or _would_review():
        # ⚠️ 바깥 문도 **같은 질문**을 해야 한다. 예전엔 `verdict.is_campaign`(내용점수
        #    ≥0.35) 만 봐서, 글이 조용한 게시물은 씨앗 몇 명만 보고 통째로 끝났다. 그중
        #    근거가 조금 나온 건은 그대로 **검수필요**로 올라갔다 — 우리가 덜 본 것을
        #    사람에게 넘긴 것이다. 제외로 떨어질 건은 여기 안 걸리므로 비용도 안 늘어난다.
        # ── 글·댓글이 "여기 캠페인 돌았다" 고 말하는데 씨앗에서 0건 ──
        # 예전에는 여기서 사실상 포기했다(댓글을 더 파도 다시 3명만 봤다). 그래서
        # @highestlevel33 에서 콘텐츠상 캠페인 215개 중 52개를 통째로 버렸다.
        # 실측(그 52개를 15명까지 조회): 28개에서 DM 이 나왔고 **8번째까지 보면 71%,
        # 10번째까지 71→82%** 가 걸린다. 3명은 구조적으로 모자란다.
        _probe(order[:campaign])
        if _needs_more() and more:
            # 그래도 0건이면 댓글이 모자란 것 — 게시 직후 댓글러까지 파고 다시 본다.
            # 실측: 미복원 253개 중 34개가 댓글 수백~1만 개라 첫 페이지가 엉뚱한
            # (한참 뒤에 단) 사람만 보여주고 있었다.
            deep, _ = C.collect_commenters(
                ctx, mid, media_ts=mts, pages=C.COMMENTS_OLDEST_MAX_PAGES
            )
            if deep:
                verdict = judge_content(media, deep)
                out.trigger, out.repetition = verdict.trigger, verdict.repetition
                out.is_campaign_signal = verdict.is_campaign
                out.content_score, out.content_reasons = verdict.score, verdict.reasons
                commenters = deep
                order = C.order_probe_targets(deep, verdict.trigger)
                _probe(order[:campaign])
        if _needs_more() and (verdict.is_strong or _would_review()):
            # ── 끝까지 판다 ──
            # ⚠️ **네 번째 같은 실수를 여기서 막는다.** 이 문은 `verdict.is_strong`
            #    (내용점수 ≥0.60) 만 봤다. 그래서 내용점수 0.4 인 게시물이 12명 보고
            #    멈춘 뒤 **검수필요로 넘어갔다** — 실측(@highestlevel33, 2026-08-18,
            #    검수 10건 전수): `조회 23/댓글 47`·`조회 23/댓글 29` 두 건이 두 축을
            #    하나도 안 판 상태였다. 댓글이 29~47개뿐이라 전원을 봐도 호출 24번인데
            #    아끼려고 안 본 것이다.
            #    `_needs_more()` 는 지난 수정에서 등급으로 판정하게 고쳤는데, 그 **바깥
            #    문**이 아직 내용점수로 판정하고 있었다(안쪽만 고치고 바깥을 놔둔 것).
            # → 근거(tmpl)가 하나라도 나왔으면 내용점수와 무관하게 두 축을 다 판다.
            #   근거가 나왔는데 자동채택이 안 되는 상태 = 그대로 두면 검수로 넘어가는
            #   상태이고, 그건 "우리가 덜 본 것을 사람에게 떠넘기는" 것이다.
            # 글·댓글이 캠페인이 확실하다는데 아직 0건이다. 여기서 멈추면 그 게시물은
            # "캠페인은 있는데 문구를 못 살림" 으로 나간다. 실측 18건이 전부 댓글
            # 600~10,050개짜리였다 — 도달률이 아니라 **보는 사람이 틀린** 것이다.
            # 캠페인 기간 컷오프를 풀고(campaign_window_days=0) 게시 직후 댓글러까지
            # 전부 훑은 뒤 다시 조회한다.
            allc, _ = C.collect_commenters(
                ctx,
                mid,
                media_ts=mts,
                pages=C.EXHAUSTIVE_COMMENT_PAGES,
                campaign_window_days=0,
            )
            out.dug_all_comments = True
            if len(allc) > len(commenters):
                logger.info(
                    "DM이전 끝까지 파기 (media=%s): 댓글러 %d → %d",
                    mid,
                    len(commenters),
                    len(allc),
                )
                commenters = allc
                v2 = judge_content(media, allc)
                out.trigger, out.repetition = v2.trigger, v2.repetition
                out.content_score, out.content_reasons = v2.score, v2.reasons
                order = C.order_probe_targets(allc, v2.trigger)
                _probe(order[: C.EXHAUSTIVE_PROBE])

            # ── 축1 마무리: 남은 댓글러 **전원**을 색인으로 대조 (Graph 호출 0) ──
            # 개별 조회는 1명당 1콜이라 댓글 1만 개짜리를 다 볼 수 없지만, 발신함 색인
            # 대조는 메모리 조회라 공짜다. 상한을 둘 이유가 없으므로 전원을 본다.
            # (색인에 없는 사람은 '모름' 이라 분모에 넣지 않는다 — _probe 참조)
            if _needs_more() and ctx.outbox:
                before = len(probed_ids)
                _probe(order[: C.INDEX_SWEEP_MAX], index_only=True)
                if len(probed_ids) > before:
                    logger.info(
                        "DM이전 색인 전수대조 (media=%s): 댓글러 %d명 훑음 → 확인 %d명 (호출 0)",
                        mid,
                        len(swept_ids),
                        len(probed_ids) - before,
                    )

            # ── 축2: 그 사람 **대화의 처음까지** 넘긴다 ──
            # 기본 조회는 대화 최근 25통만 본다. 그 사람이 이후에 대화를 많이 했으면
            # 오래된 캠페인 DM 이 뒤로 밀려 안 보인다. 실측(2026-08-17): 중첩 13통(26분치)
            # → 엣지 페이징 43통(3년 6개월치). "오래된 DM 은 API 에 없다" 는 결론은 틀렸다.
            # 여기까지 하고도 없으면 **그건 인정하고** 사람에게 넘긴다(제품 결정).
            if _needs_more() and not ctx.budget.total_hit():
                # ⚠️ order(최신·최고령 교대)를 쓰면 안 된다. 최근 댓글러는 캠페인이 꺼진
                # 뒤라 받은 게 없고, 받았다면 기본 조회로 이미 나왔다 — 깊게 파는 의미가
                # 없다. **게시 시점에 가까운 댓글러**가 오래된 DM 을 가진 사람들이다.
                _probe_deep(
                    C.order_deep_targets(commenters, mts, out.trigger)[: C.CONVO_DEEP_MAX_USERS]
                )
                out.dug_conversations = True
        if tmpl:
            _probe(order[: (big if ncmt >= C.BIG_COMMENTS else full)])
            _resolve_ambiguity()

    out.probed = max(len(probed_ids), 1)
    # 다음 실행에 물려줄 조회 대상 — 게시 시점에 가까운 순으로 자른다. **id·시각만** 담는다
    # (댓글 원문은 7일 파기 대상이라 영구 캐시에 넣지 않는다).
    if out.dug_all_comments:
        out.probe_pool = [
            {"u": u["id"], "ts": u["ts"].isoformat() if u.get("ts") else None}
            for u in C.order_deep_targets(commenters, mts, out.trigger)[: C.PROBE_POOL_KEEP]
        ]
    if not tmpl:
        return out

    best_offer, best_gate = _best_slots(out.probed)
    out.offer, out.gate = best_offer, best_gate
    # 캡션 ↔ DM 일치 — 등급 판정(자동채택 ⑤)이 쓴다. 여기서만 캡션을 온전히 볼 수 있다.
    if best_offer and (best_offer.get("text") or "").strip():
        out.content_match = content_match(
            media.get("caption") or "", best_offer["text"], out.trigger
        )

    drops: Counter = Counter()
    for p in (best_offer, best_gate):
        for code in (p or {}).get("drops", []):
            drops[code] += 1
    # 게이트가 있는데 오퍼를 못 찾았다 = 2단 구조의 뒷부분이 빠졌다
    if best_gate and not best_offer:
        drops["message_sequence"] += 1
    out.drops = [{"code": k, "count": v} for k, v in drops.items()]
    out.samples = ((best_offer or {}).get("samples") or []) + (
        (best_gate or {}).get("samples") or []
    )[:2]

    if trigger:
        out.keyword_hits = {
            trigger: sum(1 for u in commenters if trigger in (u.get("text") or "").replace(" ", ""))
        }
    return out


def build_own_dm_matcher(sent_mids: set, sent_fps: set, tmpl_norms: list):
    """자기(TurnFlow) 발송 판정 콜러블. DB 를 미리 읽어 넘겨받는다(스레드 안전).

    첨부 DM 은 ``message`` 가 비어 지문이 안 잡히던 문제가 있었는데,
    추출기를 거치면 제목 지문이 살아나 2층 방어가 복구된다(실측 225/225).
    """
    import difflib

    def _is_own(msg_id, text) -> bool:
        if msg_id and msg_id in sent_mids:
            return True
        if not text:
            return False
        if fingerprint(text) in sent_fps:
            return True
        n = placeholder_normalize(text)
        for tn in tmpl_norms:
            if abs(len(n) - len(tn)) / max(len(n), len(tn), 1) > 0.30:
                continue
            if difflib.SequenceMatcher(None, n, tn).ratio() >= 0.92:
                return True
        return False

    return _is_own


# ── 예상 소요 시간 ─────────────────────────────────────────────────────────
# 실측 단가(prod, 순차): 대화 조회 1.05~1.29초 · 댓글 1페이지 0.84초.
# 병렬 4워커에서 게시물당 5.4~6.3초로 수렴했다(성공률 높은 계정 기준).
SECONDS_PER_POST = 6.0
SECONDS_PER_POST_SLOW = 10.0  # 실패가 많아 2·3단계까지 도는 계정


def estimate_seconds(media_with_comments: int, *, workers: int = C.PROBE_WORKERS) -> dict:
    """게시물 수만으로 예상 소요를 계산한다(1단계).

    게시물 수가 곧 비용이다 — 호출 1건이 1초를 넘어 총 시간은 호출 수에 비례한다.
    """
    n = max(int(media_with_comments), 0)
    low = int(n * SECONDS_PER_POST)
    high = int(n * SECONDS_PER_POST_SLOW)
    return {
        "seconds": low,
        "seconds_max": high,
        "media_with_comments": n,
        "per_post_seconds": SECONDS_PER_POST,
        "workers": workers,
    }
