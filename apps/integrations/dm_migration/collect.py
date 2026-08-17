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
    "total_graph": 2000,
}
# 게시물 수와 무관한 항목 — 대화 목록은 계정당 1회 훑는 것이라 게시물이 늘어도 안 늘어난다.
CAPS_FIXED = {"conversations_pages": 30}
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

    단가는 실측(docs/system/DM_MIGRATION_FULL_ACCOUNT_RUN.html §6)에서 나왔다 —
    복원이 잘 되는 계정은 게시물당 4.0콜, 실패가 많은 계정은 7.5콜(댓글 1.3 + 사람 6.2).
    total_graph 15/게시물은 그 worst case(7.5)의 2배로, 재개·재시도까지 흡수하는 여유다.

    선형 확장이라 게시물 456개면 total_graph 6,840 — 실측 3,874 를 넉넉히 덮는다.
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
CONVERSATION_CAP = 600
DM_LOOKBACK_DAYS = 90

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


def fetch_conversations(ctx: CollectContext, lookback_days: int = DM_LOOKBACK_DAYS) -> dict:
    """발신 DM 메시지 수집(직렬 커서·네스티드 메시지). 스코프 없음/레이트리밋 처리.

    반환: {"outbound":[{conv_id,msg_id,text,created_time,recipient}],
           "scope_missing": bool, "conversations_scanned": int}
    """
    from django.utils import timezone as _tz

    outbound: list[dict] = []
    scope_missing = False
    convs_scanned = 0
    after = None
    pages = 0
    msg_limit = 20
    cutoff = _tz.now() - timedelta(days=lookback_days)
    seen_norms: set = set()
    no_new_streak = 0
    max_pages = ctx.budget.caps.get("conversations_pages", 30)

    while pages < max_pages and convs_scanned < CONVERSATION_CAP:
        if ctx.cancelled() or ctx.budget.total_hit():
            break
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
        if pages >= 4:  # 2연속 신규 클러스터 없음 → 조기 종료
            no_new_streak = no_new_streak + 1 if page_new == 0 else 0
            if no_new_streak >= 2:
                break
        if not after:
            break
        if all_old and data:  # 페이지 전체가 lookback 밖(대화는 updated_time desc)
            break

    return {
        "outbound": outbound,
        "scope_missing": scope_missing,
        "conversations_scanned": convs_scanned,
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
