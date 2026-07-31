"""apps/admin_api/dm_error_filters.py — 사유·분류를 **SQL 조건으로 컴파일** (DM-14 / DM-15).

`dm_error_catalog.classify()` 는 파이썬 함수라 그대로는 `.filter()` 에 넣을 수 없다.
11차에서는 그래서 "대표 로그를 파이썬으로 분류 → (campaign, recipient) 쌍 OR 체인" 으로
필터했고, 쌍이 많아지는 전역 조회는 500쌍 상한에 걸려 400 이 났다(프론트 DM-15).

여기서는 **사전 자체를 Q 로 컴파일**한다. 판정이 (code, subcode, status) 4단 폴백이라는
사실만 SQL 로 옮기면 되고, 그러면 상한도 마이그레이션도 필요 없다.

    reason_q("window_after_close")
      → (code,subcode) 4조합 OR 매칭 + 상위 레벨에 먹히지 않았을 것 (NOT ...)

컴파일 규칙 (``catalog.describe()`` 의 우선순위를 그대로 뒤집은 것):

===== =========================== ==================================================
레벨   사전                        "이 레벨에 걸린다" 의 SQL
===== =========================== ==================================================
 1     ``_BY_CODE_SUBCODE``        code=c AND subcode=s
 2     ``_BY_CODE_STATUS``         code=c AND status=st AND subcode ∉ {c 의 1레벨 키}
 3     ``_BY_CODE``                code=c AND subcode ∉ {…} AND status ∉ {c 의 2레벨 키}
 4     ``_BY_STATUS``              status=st AND code ∉ {3레벨 키} AND (1·2레벨 잔여 제외)
===== =========================== ==================================================

4레벨의 "1·2레벨 잔여"는 **code 가 3레벨에 없는** 조합뿐이다 — 지금은 내부 표식
``("", window_stalled|window_peak)`` 둘. code 가 3레벨에 있으면 ``code ∉ {3레벨}`` 이
이미 걸러 준다.

⚠️ 이 모듈의 결과는 **파이썬 판정과 100% 같아야 한다**. 규칙을 손으로 두 벌 유지하는
   구조라, `tests_dm_error_filters.py` 가 사전 전 조합 + 미등록 조합을 DB 에 넣고
   ``qs.filter(policy_q(p))`` == ``{파이썬 policy_for == p}`` 를 직접 대조한다.
   사전에 항목을 추가하면 그 테스트가 자동으로 새 조합까지 검증한다.

`error_code` / `error_subcode` 는 ``CharField(blank=True)`` — NULL 이 없으므로 부정
조건(`~Q(...)`)의 3-value logic 함정이 없다.
"""

from __future__ import annotations

from django.db.models import Q

from apps.admin_api.dm_error_catalog import (
    _BY_CODE,
    _BY_CODE_STATUS,
    _BY_CODE_SUBCODE,
    _BY_STATUS,
    _ERROR_STATUSES,
    INVESTIGATE,
    R_UNCLASSIFIED,
    SKIPPED_OTHER,
    SKIPPED_REASONS,
    SKIPPED_STATUS,
    reason_policy_map,
)

# 필터 스코프 — `?error_scope=` 로 노출한다.
SCOPE_ALL = "all"
SCOPE_ERROR = "error"  # 오류 8종 (= dm_quality.failure_breakdown 의 모수)
SCOPE_SKIPPED = "skipped"  # 건너뜀 (= dm_quality.skipped_breakdown 의 모수)
SCOPES = (SCOPE_ALL, SCOPE_ERROR, SCOPE_SKIPPED)

ERROR_SCOPE_Q = Q(status__in=sorted(_ERROR_STATUSES))
SKIPPED_SCOPE_Q = Q(status=SKIPPED_STATUS)

# 레벨별 "상위에 먹혔는지" 판정을 위한 역인덱스 (모듈 로드 시 1회).
_CODES: frozenset[str] = frozenset(_BY_CODE)
_SUBCODES_BY_CODE: dict[str, list[str]] = {}
_STATUSES_BY_CODE: dict[str, list[str]] = {}
for _c, _s in _BY_CODE_SUBCODE:
    _SUBCODES_BY_CODE.setdefault(_c, []).append(_s)
for _c, _st in _BY_CODE_STATUS:
    _STATUSES_BY_CODE.setdefault(_c, []).append(_st)

# code 가 3레벨 사전에 없는 1·2레벨 키 — 4레벨/미분류 판정에서 따로 빼 줘야 하는 잔여.
_ORPHAN_SUBCODE_KEYS = [(c, s) for (c, s) in _BY_CODE_SUBCODE if c not in _CODES]
_ORPHAN_STATUS_KEYS = [(c, st) for (c, st) in _BY_CODE_STATUS if c not in _CODES]


def _never() -> Q:
    """아무것도 매칭하지 않는 Q (해당 사유의 항목이 사전에 없을 때)."""
    return Q(pk__in=[])


def _any(parts: list[Q]) -> Q:
    if not parts:
        return _never()
    out = parts[0]
    for part in parts[1:]:
        out |= part
    return out


def _level1_q(code: str, subcode: str) -> Q:
    return Q(error_code=code, error_subcode=subcode)


def _level2_q(code: str, status: str) -> Q:
    q = Q(error_code=code, status=status)
    subs = _SUBCODES_BY_CODE.get(code)
    if subs:
        q &= ~Q(error_subcode__in=sorted(subs))
    return q


def _level3_q(code: str) -> Q:
    q = Q(error_code=code)
    subs = _SUBCODES_BY_CODE.get(code)
    if subs:
        q &= ~Q(error_subcode__in=sorted(subs))
    statuses = _STATUSES_BY_CODE.get(code)
    if statuses:
        q &= ~Q(status__in=sorted(statuses))
    return q


def _not_higher_level_q() -> Q:
    """상위 레벨(1~3) 어디에도 걸리지 않았음 — 4레벨·미분류 공통 전제."""
    q = ~Q(error_code__in=sorted(_CODES))
    for code, subcode in _ORPHAN_SUBCODE_KEYS:
        q &= ~Q(error_code=code, error_subcode=subcode)
    for code, status in _ORPHAN_STATUS_KEYS:
        q &= ~Q(error_code=code, status=status)
    return q


def _level4_q(status: str) -> Q:
    return Q(status=status) & _not_higher_level_q()


def _catalog_q(match) -> Q:
    """``match(entry)`` 를 만족하는 **오류 사전** 항목들의 SQL 조건 (폴백 우선순위 반영)."""
    parts: list[Q] = []
    parts += [_level1_q(c, s) for (c, s), e in _BY_CODE_SUBCODE.items() if match(e)]
    parts += [_level2_q(c, st) for (c, st), e in _BY_CODE_STATUS.items() if match(e)]
    parts += [_level3_q(c) for c, e in _BY_CODE.items() if match(e)]
    parts += [_level4_q(st) for st, e in _BY_STATUS.items() if match(e)]
    if not parts:
        return _never()
    return ERROR_SCOPE_Q & _any(parts)


def _unclassified_q() -> Q:
    """오류인데 사전 어디에도 안 걸린 조합.

    지금은 ``_ERROR_STATUSES == set(_BY_STATUS)`` 라 **항상 공집합**이다(4레벨이 전부
    받아 준다). 새 실패 status 를 추가하면서 사전 항목을 빼먹으면 여기가 살아나
    화면에 '분류되지 않은 실패'로 뜬다 — 그게 의도된 안전망이라 하드코딩하지 않는다.
    """
    return ERROR_SCOPE_Q & ~Q(status__in=sorted(_BY_STATUS)) & _not_higher_level_q()


# ── 건너뜀 사유 ───────────────────────────────────────────────────────
def _skipped_needles_q(needles) -> Q:
    """error_message 부분일치 OR — `catalog.classify_skipped` 의 SQL 판.

    파이썬 쪽은 `.lower()` 후 `in` 이고 여기는 `icontains` 다(둘 다 대소문자 무시).
    """
    return _any([Q(error_message__icontains=n) for n in needles])


_ALL_SKIPPED_NEEDLES = [n for _r, _l, _a, needles in SKIPPED_REASONS for n in needles]


def _skipped_reason_q(reason: str) -> Q:
    if reason == SKIPPED_OTHER[0]:
        # '기타' = 사전 문구 어디에도 안 걸린 건너뜀.
        return SKIPPED_SCOPE_Q & ~_skipped_needles_q(_ALL_SKIPPED_NEEDLES)
    for key, _label, _actionable, needles in SKIPPED_REASONS:
        if key == reason:
            return SKIPPED_SCOPE_Q & _skipped_needles_q(needles)
    return _never()


_SKIPPED_REASON_KEYS = frozenset([r for r, _l, _a, _n in SKIPPED_REASONS] + [SKIPPED_OTHER[0]])


# ── 공개 API ──────────────────────────────────────────────────────────
def is_known_reason(reason: str) -> bool:
    return reason in reason_policy_map() or reason in _SKIPPED_REASON_KEYS


def reason_q(reason: str) -> Q:
    """사유 머신 키 → SQL 조건 (DM-14).

    오류 사유는 오류 8종 안에서만, 건너뜀 사유는 status=skipped 안에서만 매칭한다 —
    사유 키가 곧 스코프이므로 호출부가 따로 좁힐 필요가 없다.
    """
    if reason in _SKIPPED_REASON_KEYS:
        return _skipped_reason_q(reason)
    if reason == R_UNCLASSIFIED:
        return _unclassified_q()
    return _catalog_q(lambda e: e["reason"] == reason)


def policy_q(policy: str, scope: str = SCOPE_ALL) -> Q:
    """분류(investigate/normal) → SQL 조건 (DM-15 — 상한 없음).

    모수는 **오류 8종 + 건너뜀**이다. 성공·진행 중 로그는 어느 쪽에도 들어가지 않는다
    (`?error_policy=normal` 이 도착한 DM 전부를 끌어오면 화면이 무의미해진다).
    ``scope`` 로 한쪽만 볼 수 있다 — 팝업이 failure_breakdown 만 그리면 ``error``.
    """
    parts: list[Q] = []
    if scope in (SCOPE_ALL, SCOPE_ERROR):
        parts.append(_catalog_q(lambda e: e["policy"] == policy))
        if policy == INVESTIGATE:
            parts.append(_unclassified_q())
    if scope in (SCOPE_ALL, SCOPE_SKIPPED):
        other = SKIPPED_OTHER[0]
        keys = [other] if policy == INVESTIGATE else sorted(_SKIPPED_REASON_KEYS - {other})
        parts += [_skipped_reason_q(k) for k in keys]
    return _any(parts)


def scope_q(scope: str) -> Q:
    """``?error_scope=`` 단독 사용 — 분류 없이 모수만 좁힐 때."""
    if scope == SCOPE_ERROR:
        return ERROR_SCOPE_Q
    if scope == SCOPE_SKIPPED:
        return SKIPPED_SCOPE_Q
    return ERROR_SCOPE_Q | SKIPPED_SCOPE_Q
