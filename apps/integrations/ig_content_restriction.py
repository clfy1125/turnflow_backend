"""게시물 단위 '연령 제한(제한 콘텐츠)' 판정 — 캠페인 점검의 **단일 소스**.

배경 (2026-09-04~, 조사: ``docs/system/DM_2534066_MEDIA_BLOCK_CENSUS_2026-09-07.md``)
──────────────────────────────────────────────────────────────────────────
인스타그램이 게시물 하나를 '연령 제한 콘텐츠'로 분류하면 그 게시물에서만 **세 가지가
동시에** 일어난다.

1. 로그아웃·10대 사용자에게 게시물이 안 보인다
2. 그 게시물의 **댓글 웹훅이 우리 앱에 전달되지 않는다**
3. 그 게시물 댓글에 대한 **비공개 답장(private reply)이 거부**된다 → ``code 200 / subcode 2534066``

즉 우리 자동 DM 은 그 게시물에서 **한 건도** 나갈 수 없다. 재연결·권한 재승인으로 풀리지
않는다(토큰 문제가 아니다). 우리만의 문제도 아니다 — NHN 소셜비즈도 2026-09-05 에 같은
공지를 냈다.

판정 신호 3종 (비용·정확도·시점이 다르다)
──────────────────────────────────────────────────────────────────────────
====================  ========  ==========================  ==========================
신호                   비용       언제 알 수 있나              한계
====================  ========  ==========================  ==========================
``history``           0         캠페인 **생성 전**            그 media 에 과거 이력이
 (과거 2534066 이력)                                          있어야만 안다
``runtime``           0         첫 실패 몇 건 뒤 (~수분)      새 게시물엔 사전 경고 불가
 (웹훅 침묵+연속실패)
``live``              HTTP 1회  즉시, 새 게시물도             ⚠️ 크롤러 UA 필요(약관 회색)
 (공개페이지 조회)                                            기본 **비활성**
====================  ========  ==========================  ==========================

``history`` · ``runtime`` 은 우리 DB 만 읽으므로 Graph 쿼터도 외부 호출도 0 이다. 실서버
CS 사례 대부분이 이 둘로 잡힌다(죽은 게시물에 캠페인을 **복사**해서 42건을 더 태운 건이
``history`` 로 100% 막힌다).

⚠️ ``live`` 검사에 대하여
──────────────────────────────────────────────────────────────────────────
인스타는 **검색엔진 크롤러 UA 에게만** SSR 본문을 준다. 실측(2026-09-07):

- UA 없음 / ``curl/8.5`` / 자기표명 ``TurnFlowBot/1.0`` → 마커 없음, **판별 불가**
- ``bingbot`` / ``Googlebot`` → 판별 가능. 사망 13건·정상 12건 **25/25 정확**

즉 판별하려면 검색봇을 사칭해야 한다. 이는 인스타 약관의 자동 수집 금지에 저촉될 수 있고
prod IP 차단 위험이 있다. 그래서 **기본 비활성**(``IG_RESTRICTION_LIVE_CHECK_ENABLED=False``)
이며, 켜더라도 사용자가 명시적으로 요청한 1회 조회(캠페인 생성·점검 버튼)에만 쓴다.
대량 스위핑에는 쓰지 말 것.

관련: :mod:`apps.integrations.dm_user_reasons` 의 ``U_POST_RESTRICTED``(=``post_restricted``)
와 같은 사건을 가리킨다. 유저 문구는 그쪽 키를 재사용한다.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── 판정 결과 코드 (프론트 계약 — 값 영구 고정) ────────────────────────
STATE_OK = "ok"  # 이상 없음
STATE_RESTRICTED = "restricted"  # 제한 확정 (자동 DM 불가)
STATE_SUSPECTED = "suspected"  # 제한 의심 (웹훅 침묵 + 연속 실패)
STATE_UNKNOWN = "unknown"  # 판정 불가 (이력 없음 / 검사 실패)

# 어떤 신호로 판정했나
SOURCE_HISTORY = "history"
SOURCE_RUNTIME = "runtime"
SOURCE_LIVE = "live"
SOURCE_NONE = "none"

# ── live 검사 상수 ────────────────────────────────────────────────────
# ⚠️ 이 마커는 인스타 SSR 응답 JSON 안의 문자열이다. HTML 구조가 바뀌면 깨질 수 있어
#    바이트 임계(_LIVE_SIZE_*)와 **둘 다** 본다. 테스트가 이 상수를 고정한다.
_LIVE_MARKER = '"title":"Age-restricted content"'
_LIVE_MARKER_ALT = "This content is age-restricted based on your age or account settings."
# 실측: 제한 페이지 699~709KB / 정상 930~952KB. 중간값 820KB 를 임계로 둔다.
_LIVE_SIZE_THRESHOLD = 820_000
_LIVE_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
_LIVE_TIMEOUT = 12.0

# ── runtime 판정 임계 ────────────────────────────────────────────────
# 연속 2534066 이 이만큼 쌓이고 그 사이 성공이 0 이면 '의심'.
#
# ⚠️ history 임계(5)보다 **낮아야** 한다. 같으면 history 가 항상 먼저 '확정'을 내서
#    runtime 의 '의심'이 도달 불가능한 죽은 코드가 된다(설계 실수로 한 번 그렇게 됐다).
#    낮게 두면 확정 전에 조기경보가 뜬다 — prod 실측에서 @josu1011(실패 4건)이 여기 잡힌다.
RUNTIME_MIN_FAILURES = 3
# 판정에 쓰는 최근 시간창.
RUNTIME_WINDOW = timedelta(hours=24)
# history: 이 media 에서 2534066 이 이만큼 났고, **마지막 성공 이후로도** 이만큼 이어지면 '확정'.
#
# ⚠️ 왜 '첫 실패 이후 성공 수'가 아니라 '마지막 성공 이후 실패 수'인가 (2026-09-07 실측):
#   백필(소급발송)은 실패와 성공을 뒤섞어 남긴다. '첫 실패 이후 성공이 몇 건인가'로 보면
#   @ouir.min(실패 178·첫실패 이후 성공 26)·@idbadaa(실패 79·성공 5) 처럼 **확실히 죽은
#   게시물이 정상으로 빠져나갔다**. 기준을 '마지막 성공 이후'로 바꾸니 prod 실데이터에서
#   탐지 12/16 → 13/16 으로 오르고 오탐은 0/13 그대로였다.
HISTORY_MIN_FAILURES = 5
HISTORY_MIN_FAIL_AFTER_SUCCESS = 5

_CACHE_PREFIX = "ig_restrict:v1:"
_CACHE_TTL_RESTRICTED = 6 * 3600  # 제한은 잘 안 풀린다(실측 나흘째 회복 0) — 길게
_CACHE_TTL_OK = 1800  # ⚠️ '정상'을 길게 캐시하지 말 것: 게시 18분 뒤 제한된 사례 있음


@dataclass
class RestrictionVerdict:
    """게시물 1개에 대한 판정 결과. 그대로 API 응답 ``restriction`` 블록이 된다."""

    state: str = STATE_UNKNOWN
    source: str = SOURCE_NONE
    media_id: str = ""
    #: 사용자에게 보여줄 머신 키 (프론트 i18n 키). dm_user_reasons 와 네임스페이스 공유.
    user_reason: str = ""
    #: 판정 근거 수치 — 화면에 그대로 보여줘도 되는 값만 담는다.
    evidence: dict[str, Any] = field(default_factory=dict)
    #: 캠페인 생성을 막아야 하는가
    blocking: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _q():
    # 지연 import — 모델 import 순환 방지
    from apps.integrations.models import SeenComment, SentDMLog

    return SentDMLog, SeenComment


def check_history(media_id: str) -> RestrictionVerdict:
    """DB 이력만으로 판정 (외부 호출 0).

    같은 게시물에 **이미 죽은 캠페인이 있었는가**를 본다. 죽은 게시물에 캠페인을 새로
    만들거나 복사하는 것을 막는 게 목적이다(실서버 CS #6d5b14ce 가 정확히 이 케이스 —
    이미 막힌 릴스에 캠페인을 복사해 42건을 더 태웠다).
    """
    SentDMLog, _ = _q()
    v = RestrictionVerdict(media_id=media_id)
    if not media_id:
        return v

    logs = SentDMLog.objects.filter(media_id=media_id)
    fails = logs.filter(error_subcode="2534066")
    n_fail = fails.count()
    if n_fail < HISTORY_MIN_FAILURES:
        return v

    last_ok = (
        logs.filter(status__in=["sent", "accepted", "delivered", "read"])
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    # 성공이 아예 없으면 전부가 '마지막 성공 이후'다.
    fail_after = n_fail if last_ok is None else fails.filter(created_at__gt=last_ok).count()

    v.evidence = {
        "failures": n_fail,
        "failures_after_last_success": fail_after,
        "last_success_at": last_ok.isoformat() if last_ok else None,
    }
    if fail_after >= HISTORY_MIN_FAIL_AFTER_SUCCESS:
        v.state = STATE_RESTRICTED
        v.source = SOURCE_HISTORY
        v.user_reason = "post_restricted"
        v.blocking = True
    else:
        # 최근에 성공한 이력이 있다 → 제한이 아니라 산발적 실패.
        v.state = STATE_OK
        v.source = SOURCE_HISTORY
    return v


def check_runtime(media_id: str, *, window: timedelta = RUNTIME_WINDOW) -> RestrictionVerdict:
    """현재 진행 중인 캠페인의 로그로 이상징후를 판정 (외부 호출 0).

    제한의 지문은 **"댓글 웹훅이 끊기고, 그 뒤 폴러가 주운 건이 전부 2534066"** 이다.
    웹훅으로 들어온 댓글은 실측 2,123건 중 2534066 이 0건이었다.
    """
    SentDMLog, SeenComment = _q()
    v = RestrictionVerdict(media_id=media_id)
    if not media_id:
        return v

    since = timezone.now() - window
    logs = SentDMLog.objects.filter(media_id=media_id, created_at__gte=since)
    n_fail = logs.filter(error_subcode="2534066").count()
    n_ok = logs.filter(status__in=["sent", "accepted", "delivered", "read"]).count()

    seen = SeenComment.objects.filter(media_id=media_id)
    n_webhook = seen.filter(source=SeenComment.Source.WEBHOOK).count()
    last_webhook = (
        seen.filter(source=SeenComment.Source.WEBHOOK)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    # 마지막 웹훅 이후 폴러로만 들어온 건수 (웹훅 침묵의 크기)
    poll_after = seen.filter(source=SeenComment.Source.POLL)
    if last_webhook:
        poll_after = poll_after.filter(created_at__gt=last_webhook)
    n_poll_after = poll_after.count()

    v.evidence = {
        "failures_2534066": n_fail,
        "successes": n_ok,
        "webhook_comments_total": n_webhook,
        "last_webhook_at": last_webhook.isoformat() if last_webhook else None,
        "poll_only_comments_after_last_webhook": n_poll_after,
        "window_hours": int(window.total_seconds() // 3600),
    }
    if n_fail >= RUNTIME_MIN_FAILURES and n_ok == 0:
        v.state = STATE_SUSPECTED
        v.source = SOURCE_RUNTIME
        v.user_reason = "post_restricted"
        v.blocking = True
    elif n_fail or n_ok:
        v.state = STATE_OK
        v.source = SOURCE_RUNTIME
    return v


def _live_enabled() -> bool:
    return bool(getattr(settings, "IG_RESTRICTION_LIVE_CHECK_ENABLED", False))


def check_live(permalink: str, *, media_id: str = "", use_cache: bool = True) -> RestrictionVerdict:
    """공개 페이지를 1회 조회해 즉시 판정.

    ⚠️ **기본 비활성.** 모듈 docstring 의 경고를 먼저 읽을 것. 켜져 있어도 실패는 전부
    ``unknown`` 으로 접어 fail-open 한다 — 검사가 캠페인 생성을 막아선 안 된다.
    """
    v = RestrictionVerdict(media_id=media_id)
    if not permalink or not _live_enabled():
        return v

    ck = _CACHE_PREFIX + (media_id or permalink)
    if use_cache:
        hit = cache.get(ck)
        if hit:
            v.state, v.source = hit["state"], SOURCE_LIVE
            v.evidence = hit.get("evidence", {}) | {"cached": True}
            v.user_reason = hit.get("user_reason", "")
            v.blocking = hit.get("blocking", False)
            return v

    try:
        r = requests.get(
            permalink,
            headers={"User-Agent": _LIVE_UA, "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=_LIVE_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException:
        logger.info("ig_content_restriction: live check 실패 media=%s", media_id, exc_info=True)
        return v

    if r.status_code != 200:
        v.evidence = {"http_status": r.status_code}
        return v

    body = r.text
    size = len(body)
    marked = (_LIVE_MARKER in body) or (_LIVE_MARKER_ALT in body)
    small = size < _LIVE_SIZE_THRESHOLD
    v.evidence = {
        "http_status": 200,
        "bytes": size,
        "marker": marked,
        "below_size_threshold": small,
    }

    if marked:
        v.state, v.source, v.blocking = STATE_RESTRICTED, SOURCE_LIVE, True
        v.user_reason = "post_restricted"
    elif small:
        # 마커는 못 찾았는데 본문이 비정상적으로 작다 → 구조 변경 가능성. 단정하지 않는다.
        v.state, v.source = STATE_UNKNOWN, SOURCE_LIVE
        logger.warning(
            "ig_content_restriction: 마커 없음 + 응답 과소(%s bytes) — HTML 구조 변경 의심 media=%s",
            size,
            media_id,
        )
    else:
        v.state, v.source = STATE_OK, SOURCE_LIVE

    if use_cache and v.state in (STATE_OK, STATE_RESTRICTED):
        cache.set(
            ck,
            {
                "state": v.state,
                "evidence": v.evidence,
                "user_reason": v.user_reason,
                "blocking": v.blocking,
            },
            _CACHE_TTL_RESTRICTED if v.state == STATE_RESTRICTED else _CACHE_TTL_OK,
        )
    return v


#: 강한 판정이 약한 판정을 이긴다. 같은 등급이면 먼저 온 것을 유지.
_PRECEDENCE = {STATE_RESTRICTED: 3, STATE_SUSPECTED: 2, STATE_OK: 1, STATE_UNKNOWN: 0}

# ── 유저 문구 (프론트가 그대로 노출해도 되는 완성 문장) ────────────────
# ⚠️ 프론트가 자체 i18n 을 쓰면 `user_reason` 머신 키로 분기하고 이 문구는 폴백으로 둔다.
#    문구를 고칠 때 `docs/frontend/IG_AGE_RESTRICTION_CREATOR_GUIDE.md` 와 함께 맞출 것.
_HOW_TO_CHECK = (
    "시크릿 창(로그아웃 상태)에서 게시물 링크를 열어보세요. "
    "'연령 제한 콘텐츠'라고 뜨면 인스타그램이 제한한 것이 맞습니다. "
    "휴대폰 인스타 앱은 이미 로그인돼 있어 확인되지 않습니다."
)
_NEXT_STEPS_RESTRICTED = [
    "다른 게시물로 캠페인을 만들어 주세요. 가장 확실하고 빠른 방법입니다.",
    "인스타그램 앱 → 설정 → 계정 상태에서 이의(검토 요청)를 넣을 수 있습니다. 통과 보장은 없습니다.",
    "재연결·권한 재승인은 효과가 없습니다. 토큰 문제가 아닙니다.",
    "같은 게시물에 캠페인을 다시 만들거나 복사하면 실패만 쌓입니다.",
]
_NEXT_STEPS_SUSPECTED = [
    "잠시 후 다시 점검해 주세요. 일시적인 지연일 수 있습니다.",
    "시크릿 창으로 게시물이 '연령 제한 콘텐츠'인지 먼저 확인해 주세요.",
    "제한이 맞다면 다른 게시물로 캠페인을 만드는 것이 가장 빠릅니다.",
]
_MESSAGES = {
    STATE_RESTRICTED: (
        "이 게시물은 인스타그램이 '연령 제한 콘텐츠'로 분류해 댓글 자동 DM을 보낼 수 없습니다. "
        "계정 문제가 아니라 이 게시물 하나의 문제이며, 재연결로는 해결되지 않습니다."
    ),
    STATE_SUSPECTED: (
        "이 게시물에서 자동 DM이 연속으로 실패하고 있습니다. "
        "인스타그램이 게시물을 제한했을 가능성이 있습니다."
    ),
    STATE_OK: "",
    STATE_UNKNOWN: "",
}


def restriction_payload(verdict: RestrictionVerdict) -> dict[str, Any]:
    """API 응답용 dict — 판정값 + 유저에게 그대로 보여줄 문구를 함께 담는다."""
    d = verdict.as_dict()
    d["user_message"] = _MESSAGES.get(verdict.state, "")
    d["how_to_check"] = (
        _HOW_TO_CHECK if verdict.state in (STATE_RESTRICTED, STATE_SUSPECTED) else ""
    )
    d["next_steps"] = (
        _NEXT_STEPS_RESTRICTED
        if verdict.state == STATE_RESTRICTED
        else (_NEXT_STEPS_SUSPECTED if verdict.state == STATE_SUSPECTED else [])
    )
    return d


def inspect_media(
    media_id: str,
    *,
    permalink: str = "",
    allow_live: bool = False,
) -> RestrictionVerdict:
    """세 신호를 합쳐 게시물 1개를 판정하는 **단일 진입점**.

    ``allow_live`` 는 "사용자가 명시적으로 점검을 눌렀다"는 뜻일 때만 True 로 준다.
    설정(``IG_RESTRICTION_LIVE_CHECK_ENABLED``)이 꺼져 있으면 이 값과 무관하게 건너뛴다.
    """
    verdicts = [check_history(media_id), check_runtime(media_id)]
    if allow_live and permalink:
        verdicts.append(check_live(permalink, media_id=media_id))

    best = max(verdicts, key=lambda x: _PRECEDENCE.get(x.state, 0))
    # 근거는 신호별로 모아서 돌려준다 — 프론트가 "왜 그렇게 판정했나"를 보여줄 수 있게.
    best.evidence = {
        "history": verdicts[0].evidence,
        "runtime": verdicts[1].evidence,
        "live": verdicts[2].evidence if len(verdicts) > 2 else {},
        "live_check_enabled": _live_enabled(),
    }
    best.media_id = media_id
    return best
