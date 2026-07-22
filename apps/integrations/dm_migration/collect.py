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
from .analyze import normalize_comment, parse_graph_time, placeholder_normalize

logger = logging.getLogger(__name__)

# pause(재개) 대상 코드 — 레이트리밋/Action Block. code 1(데이터 과다)·2·5xx 는 별도/비치명.
_RATE_PAUSE_CODES = {4, 17, 32, 368, 613}

# 기본 예산 상한(worst case 100 media 기준, 계획서 §1.7 + 타겟 복원).
DEFAULT_CAPS = {
    "media": 4,
    "comments_first": 110,
    "comments_expand": 170,
    "comments_oldest": 400,  # 후보 게시물 댓글 끝까지 페이징(초기 댓글러 확보)
    "targeted_dms": 600,  # 후보 게시물 댓글러 user_id 조회
    "conversations_pages": 30,
    "total_graph": 1500,
}
COMMENTS_OLDEST_MAX_PAGES = 10  # 게시물당 최대 페이지(=최대 500댓글)까지 페이징해 tail 확보
COMMENT_WORKERS = 6
COMMENT_EXPAND_MAX_PAGES = 4
CONVERSATION_CAP = 600
DM_LOOKBACK_DAYS = 90
# 게시물당 조회할 댓글러 수. mini_ai_ 실측: 6→12 로 늘리니 복원 게시물 13→25(≈2배).
# 댓글러당 발신 DM 적중률이 ~6%(비팔로워 미전달이 다수)라, 표본을 늘려야 커버리지가 오른다.
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
                text = msg.get("message") or ""
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


def fetch_oldest_commenters(
    ctx: CollectContext,
    media_ids: list,
    per_media: int = TARGETED_PER_MEDIA,
    max_pages: int = COMMENTS_OLDEST_MAX_PAGES,
) -> dict:
    """각 게시물 댓글을 끝(가장 오래된)까지 페이징해 '초기 댓글러' IGSID 를 뽑는다.

    캠페인은 게시 직후 돌아 **원래 댓글러가 DM 을 받았는데**, IG comments 는 최신순 고정
    (order=chronological 무시됨, 실측)이라 첫 페이지엔 한참 뒤 온 사람만 있다. 초기 댓글러의
    DM 복원율이 최신 대비 압도적이다(실측: 38% vs 1.7%). order 를 못 쓰니 끝까지 페이징해
    tail(오래된) 을 취한다.

    media_ids: 후보 게시물 id 리스트. 반환: {media_id: [oldest igsid, ...]}
    """
    out: dict = {}
    for mid in media_ids:
        if ctx.cancelled() or ctx.budget.cap_hit("comments_oldest") or ctx.budget.total_hit():
            break
        collected: list = []
        after = None
        pages = 0
        while pages < max_pages:
            if ctx.budget.cap_hit("comments_oldest") or ctx.budget.total_hit():
                break
            ctx.pacer.acquire()
            try:
                page = _fetch_comments_page(ctx, mid, after)
            except (MigrationTokenError, MigrationRateLimitPause):
                raise
            except requests.RequestException:
                break
            ctx.budget.charge("comments_oldest")
            collected.extend(page.get("data") or [])
            after = page.get("paging_after")
            pages += 1
            if not after:
                break
        # collected 는 최신→오래된 순 → tail(오래된)에서 서로 다른 댓글러 per_media 명.
        ids, seen = [], set()
        for c in reversed(collected):
            f = str((c.get("from") or {}).get("id") or "")
            if f and f != str(ctx.ig) and f not in seen:
                seen.add(f)
                ids.append(f)
            if len(ids) >= per_media:
                break
        if ids:
            out[mid] = ids
    return out


def fetch_targeted_dms(
    ctx: CollectContext,
    media_commenters: dict,
    per_media: int = TARGETED_PER_MEDIA,
) -> dict:
    """후보 게시물의 댓글러 IGSID 로 user_id 대화를 직접 조회해 계정 발신 DM 을 복원한다.

    댓글 from.id == 메시징 user_id(같은 IGSID, 실측 확인)라, 3만 개 대화방 페이징 없이
    게시물↔DM 을 정확히 잇는다. 자기발송/노이즈 제외는 상위(pipeline)가 처리.

    media_commenters: {media_id: [igsid, ...]}
    반환: {media_id: [{"text","created_time","recipient","msg_id"}, ...]}  (mock 모드면 {})
    """
    if ctx.mock:
        return {}
    out: dict = {}
    for mid, igsids in media_commenters.items():
        if ctx.cancelled() or ctx.budget.cap_hit("targeted_dms") or ctx.budget.total_hit():
            break
        rec = []
        for ig in list(igsids)[:per_media]:
            if ctx.budget.cap_hit("targeted_dms") or ctx.budget.total_hit():
                break
            ctx.pacer.acquire()
            try:
                msgs = InstagramMessagingService.list_user_conversation(ctx.ig, ctx.token, ig)
            except requests.HTTPError as exc:
                _maybe_raise_fatal(exc)  # token/rate → 전파
                continue  # 비치명(대화 없음/권한 등) → 다음 댓글러
            except requests.RequestException:
                continue
            ctx.budget.charge("targeted_dms")
            for m in msgs:
                if str((m.get("from") or {}).get("id") or "") != str(ctx.ig):
                    continue  # 계정 발신만
                text = m.get("message") or ""
                if not text.strip():
                    continue
                to = (m.get("to") or {}).get("data") or []
                rec.append(
                    {
                        "text": text[:640],
                        "created_time": m.get("created_time"),
                        "recipient": str(to[0].get("id")) if to else str(ig),
                        "msg_id": m.get("id"),
                    }
                )
        if rec:
            out[mid] = rec
    return out
