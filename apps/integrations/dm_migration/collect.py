"""DM 캠페인 이전 — Graph API 수집기 (mock 분기·토큰버킷 페이서·레이트리밋 분류).

수집기는 서비스 계층(InstagramMediaService/InstagramMessagingService)을 호출하되,
mock 모드에선 MockInstagramProvider 픽스처로 분기한다(실 Graph 호출 0). 모든 실 호출은
전역 토큰버킷(3req/s)+지터로 페이싱하고, 레이트리밋/토큰 오류를 분류해 상위(pipeline)가
paused_rate_limited/failed 로 전이하게 예외를 올린다.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import timedelta

import requests

from ..dm_exceptions import TOKEN_CODES
from ..services import (
    InstagramMediaService,
    InstagramMessagingService,
    MockInstagramProvider,
)
from .analyze import (
    dm_text_for_match,
    extract_dm_content,
    normalize_comment,
    parse_graph_time,
    placeholder_normalize,
)

logger = logging.getLogger(__name__)

# pause(재개) 대상 코드 — 레이트리밋/Action Block. code 1(데이터 과다)·2·5xx 는 별도/비치명.
_RATE_PAUSE_CODES = {4, 17, 32, 368, 613}

# 기본 예산 상한 — **media 100개 기준의 단가표**다. 실제 상한은 :func:`caps_for` 가
# 잡의 ``media_limit`` 에 비례해 늘려서 만든다.
#
# ⚠️ 이 dict 를 그대로 쓰면 안 된다(과거 그렇게 쓰여서 대형 계정이 잘렸다).
#   @highestlevel33(게시물 493개) 전수 조사는 실측 3,874 콜이 필요했는데 total_graph 가
#   1,500 에 고정돼 있어, 게시물 상한을 풀어도 예산에서 먼저 잘렸다. 연구 결론은
#   "전수 복원"(docs/system/DM_MIGRATION_FULL_ACCOUNT_RUN.html — 313/313~315, 99%+)인데
#   구현이 100개 기준 고정 예산이라 그 결론에 닿을 수 없었다.
CAPS_PER_100_MEDIA = {
    "comments_first": 110,
    "comments_expand": 170,
    # 끝까지 파기(EXHAUSTIVE_COMMENT_PAGES)를 쓰는 게시물이 몇 개만 있어도 여기서 먼저
    # 막힌다 — 댓글 1만 개짜리 하나가 200페이지를 쓴다. 실측 5,003콜 실행에서 이 항목은
    # 637 만 썼으니 올려도 총량에 여유가 있다.
    "comments_oldest": 800,  # 후보 게시물 댓글 끝까지 페이징(초기 댓글러 확보)
    "targeted_dms": 600,  # 후보 게시물 댓글러 user_id 조회
    # ⚠️ **total_graph 는 전 항목 합계**다(total_hit = sum(made) >= 이 값). 항목별 상한을
    #    올려도 여기서 먼저 잘린다. 2000/100 = 게시물당 20콜은 실측과 딱 붙어 있었다 —
    #    @highestlevel33 실행에서 418/493 지점에 9,546/9,900 을 써서 남은 57개가 조회도
    #    못 받고 끝날 상황이었다. 실측 소모는 복원 19.6콜/게시물 + 발신함 훑기 1,587.
    #    조회 표본을 10명 → 60명으로 키운 것이 반영 안 된 값이었다.
    "total_graph": 3400,
}
# 게시물 수와 무관한 항목 — 대화 목록은 계정당 1회 훑는 것이라 게시물이 늘어도 안 늘어난다.
# ⚠️ 30 페이지(=750 대화)로 묶여 있었다. 발신함을 "표본" 으로 쓰던 시절의 값인데, 이제는
# **전수 색인**이 목적이라 여기서 막히면 뒤쪽 수신자를 통째로 놓친다(실측: 상한에 걸려
# 수신자 737명에서 끊겼다 — 게시물 하나 댓글이 1만 개인 계정에서).
# 25 대화/페이지 · 이 비용은 계정당 1회이고, 대신 게시물별 개별 조회 수천 건을 없앤다.
CAPS_FIXED = {"conversations_pages": 1200}
# 미디어 목록은 1페이지 50개 → 필요한 페이지 수 + 여유 1.
MEDIA_PAGE_SIZE = 50
MIN_MEDIA_PAGES = 4

# 하위 호환 — 기존 호출부/테스트가 참조한다. media_limit=100 일 때의 caps 와 같다.
DEFAULT_CAPS = {
    "media": MIN_MEDIA_PAGES,
    **CAPS_PER_100_MEDIA,
    **CAPS_FIXED,
}


def caps_for(media_limit: int) -> dict:
    """``media_limit`` 에 비례한 예산 상한.

    단가는 **실행 실측**에서 나왔다. 초기 연구값(게시물당 4.0~7.5콜)은 댓글러를 10명만
    보던 시절 것이라 이제 맞지 않는다 — 표본을 60명까지 늘렸고 발신함 전수 훑기가 붙었다.

    @highestlevel33(게시물 493개) 실측: 복원 19.6콜/게시물 + 발신함 훑기 1,587
    = 약 11,250. total_graph 34/게시물 → 16,762 로 재개·재시도·끝까지파기까지 흡수한다.
    (20/게시물이던 값으로는 418/493 지점에 9,546/9,900 을 써서 남은 57개를 못 봤다.)
    """
    n = max(int(media_limit or 0), 10)
    scale = n / 100.0
    caps = {k: max(int(v * scale), v // 4) for k, v in CAPS_PER_100_MEDIA.items()}
    caps.update(CAPS_FIXED)
    caps["media"] = max(MIN_MEDIA_PAGES, -(-n // MEDIA_PAGE_SIZE) + 1)  # ceil + 여유 1
    return caps


COMMENTS_OLDEST_MAX_PAGES = 12  # 대형 게시물에서 캠페인 기간까지 닿으려면 최대 12페이지
COMMENT_WORKERS = 6
COMMENT_EXPAND_MAX_PAGES = 4
# ── DM 함 전수 조사 ──
# 게시물마다 댓글러를 몇 명씩 찍어보는 방식은 **표본 조사**라, 덜 본 만큼이 그대로
# "애매함" 이 되어 사람 검수로 넘어간다. 대신 **발신함을 한 번 통째로** 훑어두면
#   · 게시물당 추가 호출 0 (메모리에서 조회)
#   · 댓글러를 몇 명이든 전부 대조 → 지지비율이 표본이 아니라 **실측치**가 된다
#   · 표본에서 빠져 못 찾던 문구도 나온다
# 한 번의 비용으로 전 게시물이 덕을 보므로, 여기 상한은 넉넉히 잡는 게 이득이다.
CONVERSATION_CAP = 20000
DM_LOOKBACK_DAYS = 400  # 캠페인은 켜두면 계속 돈다 — 90일은 오래된 게시물을 통째로 놓친다

# ── 표본 설계 (실측 근거) ──
# 조회 인원을 3명에서 멈추면 '지지비율'(= 같은 문구를 받은 사람 / 조회 인원)의 분모가 없어
# 정밀도를 판정할 수 없다. 실측 정밀도: 지지비율 ≤20% → 11% · 40~60% → 77% · 60%+ → 100%.
SEED_PROBE = 3  # 우선 3명 — **콘텐츠가 캠페인이라고 말하지 않는 게시물만** 여기서 끝낸다
FULL_PROBE = 10  # 1건이라도 나오면 여기까지 채워 지지비율을 계산한다
BIG_PROBE = 30  # 댓글 1,000개 이상 대형 게시물(전달률이 낮아 표본이 더 필요)
# 글·댓글이 "여기 캠페인 돌았다" 고 말하는 게시물은 0건이어도 여기까지 판다.
# 실측(@highestlevel33 미복원 52개를 15명까지 조회): 8번째까지 71% · 10번째 82% ·
# 14번째 100% 가 걸렸다. 3명에서 끊으면 이 28건을 전부 놓친다. 12 는 82~93% 구간에서
# 비용(게시물당 +9콜)과 회수의 균형점.
CAMPAIGN_PROBE = 12
# ── 끝까지 파기 ──
# 글·댓글이 "여기 캠페인 확실하다" 고 말하는데 위 단계에서 0건이면 **포기하지 않는다.**
# 실측(@highestlevel33): 그런 게시물이 18개 있었고 전부 댓글이 600~10,050개였다.
#   예) 댓글 10,050개 · "댓글로 'ai' 달면 1인 기업 필수 ai 사이트 보내드려요" · 30명 조회 → 0건
# 원인은 도달률이 아니라 **보는 사람이 틀렸다**는 것이다. 인스타 /comments 는 최신순이라
# 첫 페이지는 캠페인이 끝난 뒤에 댓글 단 사람들이다. 그 사람들은 받은 적이 없다.
# → 댓글을 **끝까지** 넘겨 캠페인이 돌던 시기의 댓글러를 찾아내고 거기서 다시 조회한다.
# 비용은 이 조건(콘텐츠 강함 & 0건)에 걸린 소수 게시물에만 든다.
EXHAUSTIVE_COMMENT_PAGES = 250  # 50개/페이지 → 최대 12,500개 댓글까지
EXHAUSTIVE_PROBE = 40

# ── 소진의 기준 (2026-08-17 제품 결정) ────────────────────────────────
# "조금 파보고 안 나오면 사람이 검수하세요" 는 우리가 덜 한 일을 사람에게 떠넘기는 것이다.
# **두 축을 다 소진**한 뒤에야 검수로 넘긴다.
#   축1. 댓글 — 제일 첫 댓글까지 넘긴다(EXHAUSTIVE_COMMENT_PAGES).
#   축2. 대화 — 그 사람 대화의 처음까지 넘긴다(CONVO_DEEP_MAX_PAGES).
# 그래도 DM 흔적이 없으면 **그건 인정하고** 사람에게 넘긴다.
#
# 축2 는 완전히 새로 생긴 경로다. 예전에는 대화당 최근 25통만 보고 "오래된 캠페인 DM 은
# API 에 없다" 고 결론지었는데 **틀렸다** — 중첩 필드의 paging.next 를 우리가 버리고 있었다.
# 실측(2026-08-17): 중첩 13통(26분치) → 엣지 페이징 43통(3년 6개월치, 2022-10 까지).
CONVO_DEEP_MAX_PAGES = 12  # 100통/페이지 → 대화 1,200통까지 거슬러 간다
# 이 경로를 쓸 **사람 수 상한**(게시물당). 12 였던 이유는 `_probe_deep` 이 N명을 전부
# 조회한 뒤에야 결과를 봤기 때문이다 — 답이 나도 끝까지 사서 상한을 못 올렸다.
# 이제 한 명씩 넣고 등급이 확정되면 멈추므로(early exit) 상한을 올려도 보통은 몇 명에서
# 끝난다. 실측 근거: 댓글러 2,151명 게시물을 12명만 깊게 파고 검수로 넘겼다.
# 조회 순서는 **게시 시점에 가까운 댓글러**부터라(order_deep_targets) 앞쪽 몇 명에서
# 나올 가능성이 높다. 한 명당 최대 1 + CONVO_DEEP_MAX_PAGES 콜.
CONVO_DEEP_MAX_USERS = 40
# 색인 전수 대조는 Graph 호출 0이라 상한이 필요 없다. 이 값은 로그·안전장치용 상한이다.
INDEX_SWEEP_MAX = 20000

# ── 답이 확실해질 때까지 더 본다 (사람 검수를 줄이는 핵심) ──────────────
# 10명에서 무조건 멈추면 9/10 같은 **애매한 채로** 끝나고, 그걸 사람에게 "검수하세요" 로
# 넘기게 된다. 실측(@highestlevel33): 검수필요 108건 중 61건이 이 경우였다.
# 우리가 덜 본 것을 사람에게 떠넘기는 셈이다.
# → 판정이 애매한 구간에 걸린 게시물만 **결론이 날 때까지** 더 조회한다.
#   이미 확실한 것(비율이 확 높거나 확 낮은 것)은 10명에서 그대로 멈춘다 — 비용은
#   애매한 것에만 쓴다.
# 발신함 색인이 있을 때의 표본 — 색인 적중분은 공짜라 크게 잡는다.
OUTBOX_SEED_PROBE = 10
OUTBOX_MAX_PROBE = 60

# ── 발신함 훑기 상한 (슬라이스 예산 보호) ──
# ⚠️ conversations_pages 상한은 **호출 1회당**이라 슬라이스마다 초기화된다. 그래서 대화가
# 아주 많은 계정에서는 발신함 훑기가 슬라이스를 통째로 먹어치우고, MAX_SLICES 에 닿는
# 순간 **복원 결과 0건으로 종결**된다(실측: 130분/6슬라이스를 쓰고도 게시물 조회 0).
# 대화는 updated_time 내림차순이라 앞쪽이 최신·활성 캠페인이다 → 충분히 모았으면 멈춘다.
OUTBOX_MESSAGE_TARGET = 50000  # 이만큼 모았으면 그만 (직전 실측 3,171건의 15배)
OUTBOX_MAX_SLICES = 4  # 발신함에 쓸 슬라이스 상한 — 나머지는 복원·초안에 남긴다

# ── 발신함은 **비싸다 → 저장하고 다시 쓴다** ──
# 실측: @highestlevel33 발신함 훑기 = 122분 · 1,577 페이지 · 88,899건. 그런데 이 결과가
# 잡의 stage_data 에만 있어서 **다음 잡이 0 에서 다시 훑었다.** 같은 계정 같은 데이터를
# 두 번 사는 셈이고, 그만큼 Meta 쿼터를 써서 다른 워크스페이스를 굶긴다(CLAUDE.md §1).
# → 같은 IG 연결의 최근 잡이 모아둔 것을 **그대로 물려받고**, 최신 몇 페이지만 덧칠한다.
#
# ⚠️ 영구 테이블에 담지 않는 이유: 발신함은 **타인의 DM 원문**이다. 7일 파기 정책
#    (tasks.purge_dm_migration_raw)이 붙은 stage_data 안에 두면 그 시계를 그대로 따른다.
#    재사용 창을 파기 기한보다 짧게 잡는 것이 정책과 어긋나지 않는 유일한 방법이다.
OUTBOX_REUSE_HOURS = 72  # 이 안에 모은 것이면 물려받는다 (7일 파기 기한보다 짧게)
# 대화 목록은 updated_time 내림차순이라 앞쪽 몇 페이지가 '그 사이에 새로 온 것' 이다.
OUTBOX_TOPUP_PAGES = 40  # 물려받은 뒤 최신 1,000 대화만 덧칠 (≈40콜)
# 중간 저장 주기 — 슬라이스 끝(20분)에만 저장하면 워커가 죽을 때 20분치가 날아간다.
OUTBOX_CHECKPOINT_PAGES = 60  # 1,500 대화마다 저장
# 게시물별로 다음 실행에 물려줄 조회 대상 수. 캠페인 시기 댓글러만 남기면 되므로 500 이면
# 충분하다(지지 판정에 필요한 표본은 60명 수준).
PROBE_POOL_KEEP = 500

ADAPTIVE_STEP = 10  # 한 번에 더 볼 인원
ADAPTIVE_MAX_PROBE = 40  # 여기까지 봐도 애매하면 사람에게 넘긴다
AMBIGUOUS_LOW = 0.35  # 이 아래면 '아님' 으로 확실
AMBIGUOUS_HIGH = 0.75  # 이 위면 '맞음' 으로 확실

# ── 댓글 → DM 간격 (자동 발송의 지문) ──────────────────────────────
# 자동화 도구는 댓글이 달리면 **몇 초 안에** DM 을 쏜다. 사람이 손으로 쓴 개인 DM 은
# 몇 시간~며칠 뒤다. 실측(@highestlevel33, 근거 2,916건):
#     확실한 캠페인(지지 3명+)  중앙값   7초 · 1분 내 79%
#     애매(지지 1~2명)         중앙값 190초 · 1분 내  9%
#     캠페인 아님              중앙값 3.7일 · 1분 내  0%
#   간격 구간별 '지지 3명+' 비율: 0~10초 99% · 10~60초 99% · 1~10분 85% · 1일+ 52%
# 낱말·말투 패턴과 달리 **계정 성격을 안 타서** 재튜닝이 필요 없다. 지금까지 찾은 신호 중
# 가장 강하다.
AUTO_DM_MAX_GAP = 60  # 이 안에 왔으면 자동 발송으로 본다
MANUAL_DM_MIN_GAP = 86400  # 하루가 지났으면 사람이 쓴 것 — 근거로 안 쓴다
# 댓글보다 DM 이 먼저인 건 이 댓글의 응답일 수 없다(실측 195건 — 그 사람이 예전에 다른
# 게시물에 단 댓글로 받은 DM 이 여기 근거로 잘못 붙은 것). 시계 오차만 허용한다.
CLOCK_SKEW_TOLERANCE = 120
BIG_COMMENTS = 1000


def fast_hits(gaps) -> int:
    """댓글과 DM 이 **거의 동시**인 근거 수 — 자동 발송의 지문.

    부호를 가리지 않는다(시계 오차 범위 안에서는 앞뒤가 의미 없다). 실측
    (@highestlevel33, 2026-08-17): 중앙값이 -60~-239초인 게시물 11건이 있었고 그중
    ``17/45``·``16/49`` 처럼 **같은 문구를 십수 명이 받은** 건이 섞여 있었다. 한 사람이
    몇 초 사이에 댓글을 두 번 달면(신청 → 완료) 캠페인은 첫 댓글에 반응하는데 우리가 쥔
    건 두 번째 댓글이라 음수가 된다. 부호로 자르면 이런 게시물을 통째로 검수로 내린다.
    (남의 게시물에서 흘러든 DM 은 :func:`attribute.by_time` 이 **더 가까운 게시물**로
    옮기므로, 여기까지 살아남은 음수는 '이 게시물이 가장 가깝다' 는 뜻이다.)

    ⚠️ recover._pack 과 attribute._repack 이 **같은 정의**를 써야 한다. 갈라지면 귀속
    정리 뒤 등급이 옛 숫자로 매겨진다 → 이 함수가 단일 소스다.
    """
    return sum(1 for g in gaps if g is not None and -CLOCK_SKEW_TOLERANCE <= g <= AUTO_DM_MAX_GAP)


PROBE_WORKERS = 4  # 병렬 조회. ⚠️ 스레드 안에서 DB 접근 금지(커넥션 폭주)
# Meta 규칙상 댓글에 대한 Private Reply 는 7일 내만 가능 → 귀속 판정의 상한
ATTRIBUTION_WINDOW_DAYS = 7
CAMPAIGN_WINDOW_DAYS = 3  # 댓글 페이징 중단 기준(게시 후 N일 댓글에 닿으면 충분)

# (구) 고정 표본 상한 — 하위 호환용. 신규 코드는 SEED/FULL/BIG_PROBE 를 쓸 것.
TARGETED_PER_MEDIA = 12


class MigrationRateLimitPause(Exception):
    """레이트리밋/Action Block — 잡을 paused_rate_limited 로 두고 countdown 후 재개."""

    def __init__(self, code=None):
        super().__init__(f"rate limited (code={code})")
        self.code = code


class MigrationTokenError(Exception):
    """토큰/세션 사망(102/190) — 잡 즉시 failed(token_expired)."""

    def __init__(self, code=None):
        super().__init__(f"token error (code={code})")
        self.code = code


@dataclass
class Budget:
    """스테이지별 Graph 콜 예산 (api_budget_state.made/caps 미러)."""

    caps: dict = field(default_factory=lambda: dict(DEFAULT_CAPS))
    made: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def charge(self, stage: str, n: int = 1) -> None:
        with self._lock:
            self.made[stage] = self.made.get(stage, 0) + n

    def cap_hit(self, stage: str) -> bool:
        return self.made.get(stage, 0) >= self.caps.get(stage, 10**9)

    def total(self) -> int:
        return sum(self.made.values())

    def total_hit(self) -> bool:
        return self.total() >= self.caps.get("total_graph", 10**9)


class RateLimiter:
    """전역 토큰버킷(rate/s)+지터. mock 모드에선 no-op(테스트 빠르게)."""

    def __init__(self, rate_per_sec: float = 3.0, enabled: bool = True):
        self._min_interval = 1.0 / max(rate_per_sec, 0.1)
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.enabled = enabled

    def acquire(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval
        time.sleep(random.uniform(0, 0.15))  # 지터(락 밖)


def _never_cancel() -> bool:
    return False


@dataclass
class CollectContext:
    ig: str
    token: str
    mock: bool
    pacer: RateLimiter
    budget: Budget
    cancelled: Callable[[], bool] = _never_cancel
    # 발신함 전수 색인 {수신자id: [메시지]}. 있으면 댓글러 조회가 **메모리 조회**가 되어
    # Graph 호출 없이 전원 대조할 수 있다(build_outbound_index).
    outbox: dict | None = None


def is_mock(token: str) -> bool:
    """마이그레이션 수집기의 mock 판정 — dev mock 모드 + mock 토큰일 때만."""
    return MockInstagramProvider.is_mock_mode() and MockInstagramProvider.is_mock_token(token or "")


def _err_fields(exc) -> tuple:
    resp = getattr(exc, "response", None)
    http_status = getattr(resp, "status_code", None)
    code = subcode = None
    try:
        err = (resp.json() or {}).get("error", {}) if resp is not None else {}
        code = err.get("code")
        subcode = err.get("error_subcode")
    except Exception:
        pass
    return http_status, code, subcode


def _maybe_raise_fatal(exc) -> tuple:
    """치명 오류면 MigrationTokenError/MigrationRateLimitPause 를 올리고, 아니면 필드 반환."""
    http_status, code, subcode = _err_fields(exc)
    if code in TOKEN_CODES:
        raise MigrationTokenError(code=code)
    if code in _RATE_PAUSE_CODES:
        raise MigrationRateLimitPause(code=code)
    return http_status, code, subcode


# ══════════════ 미디어 ══════════════


def fetch_media(ctx: CollectContext, limit: int) -> list[dict]:
    """최근 미디어 limit 개 (커서 페이지네이션). 실패는 best-effort(수집분 반환)."""
    items: list[dict] = []
    after = None
    while len(items) < limit and not ctx.budget.cap_hit("media") and not ctx.budget.total_hit():
        ctx.pacer.acquire()
        try:
            if ctx.mock:
                page = MockInstagramProvider.mock_list_media_page(ctx.ig, limit=limit, after=after)
            else:
                page = InstagramMediaService.list_media_page(
                    ctx.ig, ctx.token, limit=50, after=after
                )
        except requests.HTTPError as exc:
            _maybe_raise_fatal(exc)  # token/rate → 전파
            break  # 그 외 4xx/5xx → best-effort 종료
        except requests.RequestException:
            break
        ctx.budget.charge("media")
        items.extend(page.get("data") or [])
        after = page.get("paging_after")
        if not after:
            break
    return items[:limit]


# ══════════════ 댓글 ══════════════


def _fetch_comments_page(ctx: CollectContext, media_id: str, after) -> dict:
    if ctx.mock:
        return MockInstagramProvider.mock_list_media_comments(media_id, after=after)
    try:
        return InstagramMediaService.list_media_comments(
            media_id, ctx.token, limit=50, after=after, raise_on_error=True
        )
    except requests.HTTPError as exc:
        _maybe_raise_fatal(exc)  # token/rate → 전파
        raise  # 비치명 HTTPError → 호출부가 실패 미디어로 기록


def fetch_comments_first_pass(ctx: CollectContext, media_items: list[dict]) -> tuple[dict, list]:
    """댓글 있는 media 각각의 첫 페이지 (ThreadPool). comments_count=0 은 스킵.

    반환: ({media_id: {"comments":[...], "paging_after": cursor}}, failed_media_ids)
    """
    targets = [m for m in media_items if (m.get("comments_count") or 0) > 0]
    results: dict = {}
    failed: list = []
    fatal: dict = {"exc": None}

    def work(m):
        mid = m.get("id")
        if (
            ctx.cancelled()
            or fatal["exc"]
            or ctx.budget.cap_hit("comments_first")
            or ctx.budget.total_hit()
        ):
            return None
        ctx.pacer.acquire()
        try:
            page = _fetch_comments_page(ctx, mid, after=None)
        except (MigrationTokenError, MigrationRateLimitPause) as fe:
            fatal["exc"] = fe
            return None
        except requests.RequestException:
            failed.append(mid)
            return None
        ctx.budget.charge("comments_first")
        return mid, {"comments": page.get("data") or [], "paging_after": page.get("paging_after")}

    with ThreadPoolExecutor(max_workers=COMMENT_WORKERS) as ex:
        for fut in as_completed([ex.submit(work, m) for m in targets]):
            r = fut.result()
            if r:
                results[r[0]] = r[1]
    if fatal["exc"]:
        raise fatal["exc"]
    return results, failed


def fetch_comments_expand(ctx: CollectContext, candidates: list[dict]) -> None:
    """후보 media 만 추가 페이지 수집(포화 기반 조기 종료). candidates 를 in-place 갱신.

    candidates 항목: {"media_id","after","comments"(list, in-place),"known_norms"(set),"keywords"(list)}
    """
    for c in candidates:
        after = c.get("after")
        pages = 0
        while after and pages < COMMENT_EXPAND_MAX_PAGES:
            if ctx.cancelled() or ctx.budget.cap_hit("comments_expand") or ctx.budget.total_hit():
                break
            ctx.pacer.acquire()
            try:
                page = _fetch_comments_page(ctx, c["media_id"], after=after)
            except (MigrationTokenError, MigrationRateLimitPause):
                raise
            except requests.RequestException:
                break
            ctx.budget.charge("comments_expand")
            new = page.get("data") or []
            c["comments"].extend(new)
            norms = {normalize_comment(x.get("text", "")) for x in new}
            norms.discard("")
            new_unique = len(norms - c["known_norms"])
            kw = [normalize_comment(k) for k in (c.get("keywords") or []) if k]
            kw_hits = sum(
                1 for x in new if any(k and k in normalize_comment(x.get("text", "")) for k in kw)
            )
            c["known_norms"] |= norms
            after = page.get("paging_after")
            pages += 1
            # 포화(3페이지째부터): 신규 유니크 <20% & 신규 키워드 히트 <3 → 중단.
            if pages >= 2 and norms and (new_unique / len(norms)) < 0.20 and kw_hits < 3:
                break


# ══════════════ DM 대화 ══════════════


def fetch_conversations(
    ctx: CollectContext,
    lookback_days: int = DM_LOOKBACK_DAYS,
    *,
    after: str | None = None,
    should_stop=None,
    max_pages: int | None = None,
    on_progress=None,
) -> dict:
    """발신 DM 메시지 수집(직렬 커서·네스티드 메시지). 스코프 없음/레이트리밋 처리.

    ``after`` / ``should_stop`` 은 **이어달리기**용이다. 전수 훑기는 수백~천 페이지가 될 수
    있어 한 태스크(25분) 안에 안 끝난다. 시간이 다 되면 ``should_stop()`` 이 True 를 주고,
    그때까지 모은 것과 ``paging_after`` 를 반환한다. 다음 슬라이스가 그 커서부터 잇는다.
    (이 장치가 없으면 슬라이스마다 1페이지부터 다시 시작해 **영원히 못 끝낸다** —
     복원 단계에서 이미 겪은 실패다.)

    반환: {"outbound":[...], "scope_missing": bool, "conversations_scanned": int,
           "paging_after": 커서|None, "exhausted": 끝까지 갔나}
    """
    from django.utils import timezone as _tz

    outbound: list[dict] = []
    scope_missing = False
    convs_scanned = 0
    pages = 0
    msg_limit = 20
    cutoff = _tz.now() - timedelta(days=lookback_days)
    seen_norms: set = set()
    no_new_streak = 0
    if max_pages is None:
        max_pages = ctx.budget.caps.get("conversations_pages", 30)
    exhausted = False

    while pages < max_pages and convs_scanned < CONVERSATION_CAP:
        if ctx.cancelled() or ctx.budget.total_hit():
            break
        if should_stop and should_stop():
            break  # 시간 소진 — 여기까지 모은 것 + 커서를 돌려주고 다음 슬라이스가 잇는다
        ctx.pacer.acquire()
        try:
            if ctx.mock:
                page = MockInstagramProvider.mock_list_conversations(ctx.ig, after=after)
            else:
                page = InstagramMessagingService.list_conversations(
                    ctx.ig, ctx.token, limit=25, after=after, message_limit=msg_limit
                )
        except requests.HTTPError as exc:
            http_status, code, subcode = _err_fields(exc)
            if code in TOKEN_CODES:
                raise MigrationTokenError(code=code) from exc
            if code == 1 and msg_limit > 10:
                msg_limit = 10  # "데이터 과다" → 메시지 한도 축소 후 같은 페이지 재시도
                continue
            if code in (10, 200) or http_status == 403:
                scope_missing = True  # 메시징 스코프/권한 없음 → DM 분석 스킵(partial)
                break
            if code in _RATE_PAUSE_CODES:
                raise MigrationRateLimitPause(code=code) from exc
            break  # 그 외 → best-effort 종료
        except requests.RequestException:
            break

        ctx.budget.charge("conversations_pages")
        pages += 1
        data = page.get("data") or []
        page_new = 0
        all_old = bool(data)
        for conv in data:
            convs_scanned += 1
            upd = parse_graph_time(conv.get("updated_time", ""))
            if upd and upd >= cutoff:
                all_old = False
            for msg in (conv.get("messages") or {}).get("data") or []:
                frm = str((msg.get("from") or {}).get("id") or "")
                if frm != str(ctx.ig):
                    continue  # 발신(계정 본인)만
                # ⚠️ msg["message"] 직접 읽지 말 것 — 버튼 DM 은 비어 있다(analyze 참조)
                text = dm_text_for_match(msg)
                if not text.strip():
                    continue
                to = (msg.get("to") or {}).get("data") or []
                outbound.append(
                    {
                        "conv_id": conv.get("id"),
                        "msg_id": msg.get("id"),
                        "text": text,
                        "created_time": msg.get("created_time"),
                        "recipient": str(to[0].get("id")) if to else "",
                        "content": extract_dm_content(msg),
                    }
                )
                norm = placeholder_normalize(text)
                if norm and norm not in seen_norms:
                    seen_norms.add(norm)
                    page_new += 1

        after = page.get("paging_after")
        # ⚠️ "새 문구가 안 나오면 그만" 으로 끊으면 안 된다. 우리가 찾는 건 **새 문구**가
        # 아니라 **누가 그 문구를 받았는가**다. 같은 문구가 계속 나오는 페이지야말로 지지
        # 근거가 쌓이는 곳이다. 여기서 끊으면 뒤쪽 게시물의 수신자를 통째로 놓친다.
        # (원래 이 조기 종료 때문에 발신함 조사가 표본 수준에 머물렀다.)
        no_new_streak = no_new_streak + 1 if page_new == 0 else 0
        # 중간 저장 — 슬라이스가 끝날 때(20분)만 저장하면 **워커가 죽으면 20분치가 날아간다.**
        # 실측: 발신함 훑기가 122분/6슬라이스였다 → 최악의 경우 한 번에 22,000건이 사라진다.
        # stage_data 쓰기가 수 MB 라 매 페이지는 못 하고, 호출부가 정한 주기로만 부른다.
        if on_progress and pages % OUTBOX_CHECKPOINT_PAGES == 0:
            on_progress(outbound, after)
        if not after:
            exhausted = True  # 커서 끝 = 발신함을 다 봤다
            break
        if all_old and data:  # 페이지 전체가 lookback 밖(대화는 updated_time desc)
            break

    return {
        "outbound": outbound,
        "scope_missing": scope_missing,
        "conversations_scanned": convs_scanned,
        "paging_after": after,
        "exhausted": exhausted or not after,
    }


# ══════════════ 타겟 DM 복원 (게시물 댓글러 → 그가 받은 발신 DM) ══════════════


def collect_commenters(
    ctx: CollectContext,
    media_id: str,
    *,
    media_ts=None,
    pages: int = 1,
    campaign_window_days: int = 3,
) -> tuple[list[dict], bool]:
    """게시물 댓글을 수집해 댓글러 목록을 만든다.

    IG ``/comments`` 는 **최신순 고정**(order=chronological 무시, 실측)이라
    1페이지만 받으면 대형 게시물에서는 **게시 후 3~4주 뒤 댓글러**만 잡힌다
    (실측: 댓글 1,366개 게시물의 1페이지 최고령 댓글이 게시 26일 뒤).
    캠페인은 보통 게시 직후 돌므로 그 사람들은 DM 을 못 받았다.
    → ``pages`` 를 늘려 **캠페인 기간(게시 후 N일)에 닿을 때까지** 판다.

    Returns: ([{"id","ts","text","replied"}...], 더_팔_수_있는가)
        · replied=True 는 계정이 그 댓글에 공개답글을 단 사람(= 자동화 발동 확정)
    """
    collected: list = []
    after = None
    got = 0
    while got < pages:
        if ctx.cancelled() or ctx.budget.cap_hit("comments_oldest") or ctx.budget.total_hit():
            break
        ctx.pacer.acquire()
        try:
            page = _fetch_comments_page(ctx, media_id, after)
        except (MigrationTokenError, MigrationRateLimitPause):
            raise
        except requests.RequestException:
            break
        ctx.budget.charge("comments_oldest")
        collected.extend(page.get("data") or [])
        after = page.get("paging_after")
        got += 1
        if not after:
            break
        # 캠페인 기간에 닿았으면 그만 판다 (작은 게시물은 1페이지로 끝)
        if media_ts:
            oldest = min(
                (t for t in (parse_graph_time(c.get("timestamp")) for c in collected) if t),
                default=None,
            )
            if oldest and (oldest - media_ts).total_seconds() / 86400 <= campaign_window_days:
                break

    own = str(ctx.ig)
    by_id: dict = {}
    replied_to: set = set()
    for c in collected:
        author = str((c.get("from") or {}).get("id") or "")
        if author == own:
            # 계정 본인 답글 → parent 댓글 작성자를 '발동 확정' 으로 표시
            parent = str(c.get("parent_id") or "")
            if parent:
                replied_to.add(parent)
            continue
        if c.get("parent_id"):
            continue
        cid = str(c.get("id") or "")
        if author and author not in by_id:
            by_id[author] = {
                "id": author,
                "media_id": media_id,
                "comment_id": cid,
                "ts": parse_graph_time(c.get("timestamp")),
                "text": (c.get("text") or "")[:200],
                "replied": False,
            }
    for u in by_id.values():
        if u["comment_id"] in replied_to:
            u["replied"] = True
    return list(by_id.values()), bool(after)


def order_probe_targets(commenters: list[dict], trigger: str | None = None) -> list[dict]:
    """조회 우선순위.

    ①캡션의 트리거 단어를 **실제로 댓글에 단 사람** ②계정이 공개답글을 단 사람
    ③오래된/최근을 **교대로** — 캠페인이 게시 직후 켜졌는지 나중에 켜졌는지 알 수 없으므로
    한쪽만 보면 안 된다(실측: '나중에 켠 캠페인' 이 18%).
    """
    out: list[dict] = []
    seen: set = set()

    def push(u):
        if u["id"] not in seen:
            seen.add(u["id"])
            out.append(u)

    if trigger:
        t = trigger.replace(" ", "")
        for u in commenters:
            if t and t in (u.get("text") or "").replace(" ", ""):
                push(u)
    for u in commenters:
        if u.get("replied"):
            push(u)
    recent = list(commenters)  # 수집 순서 = 최신순
    oldest = list(reversed(commenters))
    for a, b in zip(oldest, recent, strict=False):
        push(a)
        push(b)
    for u in commenters:
        push(u)
    return out


def order_deep_targets(
    commenters: list[dict], media_ts=None, trigger: str | None = None
) -> list[dict]:
    """**대화 끝까지 파기** 전용 우선순위 — 게시 시점에 가까운 댓글러부터.

    :func:`order_probe_targets` 와 목적이 다르다. 그쪽은 "이 게시물 캠페인의 지지비율" 을
    재려고 최신·최고령을 교대로 본다. 이쪽은 **"오래된 DM 이 이후 대화에 밀려 안 보이는
    사람"** 을 찾는 일이다. 최근에 댓글 단 사람은 캠페인이 이미 꺼진 뒤라 받은 게 없고,
    받았다면 기본 조회(최근 25통)로 이미 나왔다 — 깊게 파는 의미가 없다.

    실측(@highestlevel33, 2024-02 게시물): 댓글 1페이지가 2024-12~2026-02 였다.
    게시 시점 댓글러는 200페이지 뒤에 있고, 그 사람들이 캠페인 DM 을 받은 사람들이다.
    """
    t = (trigger or "").replace(" ", "")

    def key(u):
        # ①트리거를 실제로 단 사람 ②게시 시점에 가까운 순
        hit = 0 if (t and t in (u.get("text") or "").replace(" ", "")) else 1
        ts = u.get("ts")
        if media_ts and ts:
            return (hit, abs((ts - media_ts).total_seconds()))
        return (hit, float("inf") if ts is None else -ts.timestamp())

    return sorted(commenters, key=key)


def build_outbound_index(outbound: list[dict]) -> dict:
    """발신 DM 목록 → ``{수신자id: [메시지, ...]}``.

    이 색인이 있으면 게시물마다 댓글러를 Graph 로 다시 찾아볼 필요가 없다. 표본이 아니라
    **댓글러 전원**을 대조할 수 있어 지지비율이 추정치가 아닌 실측치가 된다.
    """
    idx: dict[str, list] = {}
    for m in outbound:
        rid = str(m.get("recipient") or "")
        if rid:
            idx.setdefault(rid, []).append(m)
    return idx


def outbound_from_index(
    ctx: CollectContext,
    commenter: dict,
    *,
    window_days: int = ATTRIBUTION_WINDOW_DAYS,
) -> tuple[list[dict], bool]:
    """발신함 색인만 보고 조회한다 — **Graph 호출 0, 상한을 둘 이유가 없다.**

    이것이 "끝까지 파기" 를 가능하게 하는 열쇠다. 댓글러를 개별 조회하면 1명당 1콜이라
    댓글 1만 개짜리 게시물은 예산으로 감당이 안 되는데, 색인 대조는 메모리 조회라 전원을
    훑어도 공짜다. (실측 @highestlevel33: 색인 88,899건)

    Returns: ``(hits, known)``
        · ``known=True``  — 이 사람의 대화가 색인에 있다. ``hits`` 가 비었으면 **진짜로
          창 안에 받은 게 없다**(= 음성 근거로 세도 된다).
        · ``known=False`` — 색인에 아예 없다 = **모름**. 이걸 '안 받았음' 으로 세면
          지지비율의 분모만 부풀어 멀쩡한 캠페인이 탈락한다. 개별 조회로만 알 수 있다.
    """
    if not ctx.outbox:
        return [], False
    cached = ctx.outbox.get(str(commenter["id"]))
    if not cached:
        return [], False
    cts0 = commenter.get("ts")
    hits = []
    for m in cached:
        dt = parse_graph_time(m.get("created_time"))
        if cts0 and dt:
            gap = (dt - cts0).total_seconds()
            if gap < -3600 or gap > window_days * 86400:
                continue
        hits.append(m)
    return hits, True


def fetch_outbound_for_commenter(
    ctx: CollectContext,
    commenter: dict,
    *,
    window_days: int = ATTRIBUTION_WINDOW_DAYS,
) -> list[dict]:
    """댓글러 1명이 **그 댓글 때문에** 받은 발신 DM 을 복원한다.

    ⚠️ 시간창이 핵심이다. 같은 사람이 여러 게시물에 댓글을 달면 각 캠페인 DM 이 모두
    그 대화방에 쌓인다. Meta 규칙상 댓글에 대한 Private Reply 는 **7일 내**만 가능하므로
    ``댓글시각 ≤ DM시각 ≤ 댓글시각+7일`` 로 잘라야 다른 게시물 DM 이 딸려오지 않는다.
    (창이 없던 시절 실측: 2026-05 게시물에 2025-09 DM 이 섞여 들어왔다.)

    반환: [{"text","created_time","recipient","msg_id","content"}]
        · content = analyze.extract_dm_content() 결과(버튼·URL·미디어 종류 포함)
    """
    uid = commenter["id"]
    # 발신함 색인에 이 사람이 있으면 **공짜로** 꺼낸다(Graph 호출 0).
    # ⚠️ 색인은 완전하지 않다 — Meta 는 대화당 최근 ~20개 메시지만 준다. 그래서 "색인에
    # 없음"은 "DM 을 안 받았음"이 아니라 "모름"이다. 그 경우엔 아래 개별 조회로 내려간다.
    if ctx.outbox:
        hits, _known = outbound_from_index(ctx, commenter, window_days=window_days)
        if hits:
            return hits

    if ctx.mock:
        msgs = MockInstagramProvider.mock_user_conversation(
            ctx.ig, uid, commenter.get("media_id", "")
        )
    else:
        ctx.pacer.acquire()
        try:
            msgs = InstagramMessagingService.list_user_conversation(ctx.ig, ctx.token, uid)
        except requests.HTTPError as exc:
            _maybe_raise_fatal(exc)  # token/rate → 전파
            return []
        except requests.RequestException:
            return []
        ctx.budget.charge("targeted_dms")

    cts = commenter.get("ts")
    out: list[dict] = []
    for m in msgs:
        if str((m.get("from") or {}).get("id") or "") != str(ctx.ig):
            continue  # 계정 발신만
        dt = parse_graph_time(m.get("created_time"))
        if cts and dt:
            gap = (dt - cts).total_seconds()
            if gap < -3600 or gap > window_days * 86400:
                continue  # 이 댓글 때문에 간 DM 이 아니다
        content = extract_dm_content(m)
        text = dm_text_for_match(m)
        if not text:
            continue
        to = (m.get("to") or {}).get("data") or []
        out.append(
            {
                "text": text[:640],
                "created_time": m.get("created_time"),
                "recipient": str(to[0].get("id")) if to else str(uid),
                "msg_id": m.get("id"),
                "content": content,
            }
        )
    return out


def fetch_outbound_deep(
    ctx: CollectContext,
    commenter: dict,
    *,
    window_days: int = ATTRIBUTION_WINDOW_DAYS,
    max_pages: int = CONVO_DEEP_MAX_PAGES,
) -> list[dict]:
    """댓글러 1명의 대화를 **처음까지 넘겨** 받은 발신 DM 을 복원한다.

    마지막 수단이다. 기본 조회(``fetch_outbound_for_commenter``)는 대화의 최근 25통만
    보므로, 그 사람이 이후에 대화를 많이 했으면 오래된 캠페인 DM 이 뒤로 밀려 안 보인다.
    실측(2026-08-17): 중첩 조회 13통(26분치) vs 엣지 페이징 43통(3년 6개월치).

    비용은 대화당 페이지 수만큼이라(1페이지 100통) 아무 때나 쓰면 안 된다 — 글·댓글이
    "캠페인 확실" 이라고 말하는데 다른 모든 경로가 실패했을 때만 호출한다.
    """
    uid = commenter["id"]
    if ctx.mock:
        return fetch_outbound_for_commenter(ctx, commenter, window_days=window_days)
    cts = commenter.get("ts")
    ctx.pacer.acquire()
    try:
        conv = InstagramMessagingService.list_conversation_id(ctx.ig, ctx.token, uid)
    except requests.HTTPError as exc:
        _maybe_raise_fatal(exc)
        return []
    ctx.budget.charge("targeted_dms")
    if not conv:
        return []

    out: list[dict] = []
    after, pages = None, 0
    while pages < max_pages:
        if ctx.budget.cap_hit("targeted_dms") or ctx.budget.total_hit():
            break
        ctx.pacer.acquire()
        try:
            msgs, after = InstagramMessagingService.page_conversation_messages(
                ctx.token, conv, after=after
            )
        except requests.HTTPError as exc:
            _maybe_raise_fatal(exc)
            break
        ctx.budget.charge("targeted_dms")
        pages += 1
        stop = False
        for m in msgs:
            dt = parse_graph_time(m.get("created_time"))
            # 메시지는 최신순이다 → 댓글보다 확실히 오래된 구간에 닿으면 더 볼 필요가 없다.
            if cts and dt and (dt - cts).total_seconds() < -3600:
                stop = True
                continue
            if str((m.get("from") or {}).get("id") or "") != str(ctx.ig):
                continue
            if cts and dt and (dt - cts).total_seconds() > window_days * 86400:
                continue
            text = dm_text_for_match(m)
            if not text:
                continue
            to = (m.get("to") or {}).get("data") or []
            out.append(
                {
                    "text": text[:640],
                    "created_time": m.get("created_time"),
                    "recipient": str(to[0].get("id")) if to else str(uid),
                    "msg_id": m.get("id"),
                    "content": extract_dm_content(m),
                }
            )
        if stop or not after or not msgs:
            break
    return out
