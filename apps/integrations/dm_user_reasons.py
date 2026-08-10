"""유저 콘솔용 사유(`user_reason`) — 운영 사유 24종을 사용자 관점 9종으로 접는 층.

설계 근거는 `../../docs/frontend/DM_USER_COPY_MAPPING.md` §4·§5. 요약하면:

- 운영자 사전(:mod:`apps.admin_api.dm_error_catalog`)은 **원인을 파고들기 위한** 축이라
  세분화가 미덕이다("우리가 제때 못 보냄" 같은 내부 사정도 적혀 있다).
- 유저 콘솔은 반대로 **"내 잘못인가 / 뭘 하면 되나 / 다시 갈 수 있나"** 세 가지만 답하면
  되므로, 24종을 9종으로 접고 내부 사정은 사용자에게 의미 있는 층위로 올려 말한다
  (예: `stalled_by_us` → "발송 가능 시간이 지났다").

──────────────────────────────────────────────────────────────────────────
왜 admin 사전을 import 하지 않는가
──────────────────────────────────────────────────────────────────────────
``apps.integrations`` → ``apps.admin_api`` 는 **역방향 의존**이라 금지다
(:mod:`apps.admin_api.dm_policy_rollup` 의 같은 판단과 동일한 이유 — 운영자 어휘가
유저 응답으로 새는 통로가 된다).

그래서 판정 테이블을 여기 따로 둔다. 두 벌이 되는 대신 **드리프트는 테스트가 막는다** —
``tests_dm_user_copy.py::TestAdminCatalogParity`` 가 admin 사전의 전 조합을 순회하며
`운영 reason → user_reason` 매핑이 빠짐없이 성립하는지 대조한다. 사전에 사유를 추가하고
여기 매핑을 빠뜨리면 그 테스트가 실패한다.

판정 우선순위 (4단 폴백):
  1. **subcode 단독**  2. (code, status)  3. (code)  4. status

⚠️ 1단계가 admin 사전과 다르다. admin 은 ``(code, subcode)`` 쌍으로 보는데, 여기서는
**subcode 만으로** 판정한다. 이유:

- 유저 콘솔의 탭·배지는 :mod:`apps.integrations.dm_status_groups` 가 정하는데, 그쪽은
  ``2534025`` 를 **code 와 무관하게** 숨김함으로 본다. 쌍으로 보면 code 가 비거나 다른
  값인 행에서 **탭은 '숨겨진 요청·스팸'인데 본문은 '댓글 7일 초과'** 가 되어 화면이
  자기모순에 빠진다(회귀 테스트로 고정).
- ``dm_exceptions.classify_api_error`` 도 ``2534023`` 을 code 조합보다 **먼저** 본다.
- Meta subcode 는 사실상 전역 고유라, 한 subcode 가 두 사유로 갈리는 경우가 없다
  (``2534022`` 는 code 10·100 양쪽에 있지만 둘 다 '시간 경과'로 같다).
"""

from __future__ import annotations

# ── user_reason 머신 키 ───────────────────────────────────────────────
# ⚠️ 값은 **영구 고정**이다. 프론트 i18n 키·저장된 링크가 이 문자열을 참조한다.
U_CONNECTION_LOST = "connection_lost"  # U1 인스타그램 연결 해제
U_RECIPIENT_UNAVAILABLE = "recipient_unavailable"  # U2 수신자 수신 불가
U_WINDOW_EXPIRED = "window_expired"  # U3 발송 가능 시간 경과
U_HIDDEN_REQUEST = "hidden_request"  # U4 숨겨진 요청·스팸함
U_POST_RESTRICTED = "post_restricted"  # U5 게시물 단위 제한
U_ALREADY_REPLIED = "already_replied"  # U6 이미 답장 있음
U_DELIVERY_UNCONFIRMED = "delivery_unconfirmed"  # U7 도착 미확인
U_SEND_DELAYED = "send_delayed"  # U8-지연 (아직 발송될 수 있음)
U_SEND_INCOMPLETE = "send_incomplete"  # U8-종결 (사유 미확인)

# 건너뜀(skipped) 8종 — 키는 admin 사전과 **같은 문자열**을 쓴다.
# 어드민 `?error_reason=` 드릴다운과 네임스페이스를 공유하기 위함이며, 이 문구들을
# 실제로 기록하는 곳이 integrations(`mark_skipped` 호출부)라 여기가 본가다.
S_MONTHLY_DM_LIMIT = "monthly_dm_limit"
S_CAMPAIGN_NOT_ACTIVE = "campaign_not_active"
S_OUTSIDE_SCHEDULE = "outside_schedule_window"
S_IG_ACCOUNT_INACTIVE = "ig_account_inactive"
S_SELF_RECIPIENT = "self_recipient"
S_CONNECTION_DISCONNECTED = "connection_disconnected"
S_DUPLICATE_CLEANUP = "duplicate_campaign_cleanup"
S_GHOST_CLEANUP = "ghost_opening_cleanup"
S_MESSAGING_WINDOW_SKIP = "messaging_window_skip"
S_OTHER = "other"

# 오류가 아닌 상태(성공·진행 중)는 사유가 없다 — 빈 문자열.
NO_REASON = ""

# ── 4단 폴백 테이블 ──────────────────────────────────────────────────
# 1단계: subcode 단독 (code 무관 — 위 docstring 참고)
_BY_SUBCODE: dict[str, str] = {
    "2534025": U_HIDDEN_REQUEST,  # 비팔로워 채널 미개설 → 숨김함
    "2534014": U_RECIPIENT_UNAVAILABLE,  # 받는 사람 없음
    "2534001": U_RECIPIENT_UNAVAILABLE,  # 대화방 삭제/보관
    "2534022": U_WINDOW_EXPIRED,  # 창 만료 (code 10·100 공통)
    "2018278": U_WINDOW_EXPIRED,  # 창 만료 (같은 사유 다른 번호)
    "2534023": U_ALREADY_REPLIED,  # 이미 답장 있음
    "2534066": U_POST_RESTRICTED,  # 게시물 단위 자동 DM 차단
    # 내부 표식 subcode (Meta 값이 아니라 우리가 붙인 것). 둘 다 원인은 우리 쪽
    # 처리 사정이지만, 사용자에게 유효한 사실은 "시간이 지났다"이므로 U3 로 접는다.
    "window_stalled": U_WINDOW_EXPIRED,
    "window_peak": U_WINDOW_EXPIRED,
}

# 2단계: (code, status) — 같은 코드가 두 의미를 갖는 경우의 갈래
_BY_CODE_STATUS: dict[tuple[str, str], str] = {
    ("10", "failed_window"): U_WINDOW_EXPIRED,
}

# 3단계: code 단위
_BY_CODE: dict[str, str] = {
    "190": U_CONNECTION_LOST,
    "102": U_CONNECTION_LOST,
    "10": U_SEND_INCOMPLETE,  # 권한/기간 불명 — 우리도 못 가림
    "100": U_WINDOW_EXPIRED,  # 대상 없음/만료 (댓글 삭제·7일 초과 포함)
    "200": U_POST_RESTRICTED,
    "551": U_RECIPIENT_UNAVAILABLE,
    "613": U_SEND_DELAYED,
    "4": U_SEND_DELAYED,
    "-1": U_SEND_INCOMPLETE,
}

# 4단계: status 단위 기본값
_BY_STATUS: dict[str, str] = {
    "failed_token": U_CONNECTION_LOST,
    "failed_window": U_WINDOW_EXPIRED,
    "failed_param": U_WINDOW_EXPIRED,
    "failed_no_trace": U_DELIVERY_UNCONFIRMED,
    "failed": U_SEND_INCOMPLETE,  # legacy
    "failed_api": U_SEND_INCOMPLETE,  # legacy
    "recovery_pending": U_HIDDEN_REQUEST,
    "recovery_expired": U_HIDDEN_REQUEST,
    "rate_limited": U_SEND_DELAYED,
}

# ── 건너뜀 사유 판정 (error_message 부분일치) ─────────────────────────
# ⚠️ needle 은 **과거 로그 원문을 맞추는 값**이라 라벨과 함께 바꾸면 안 된다
#    (admin 사전 DM-18 과 같은 주의).
_SKIPPED_NEEDLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (S_MONTHLY_DM_LIMIT, ("monthly_dm_limit_reached",)),
    (S_CAMPAIGN_NOT_ACTIVE, ("campaign not active",)),
    (S_OUTSIDE_SCHEDULE, ("outside active schedule window",)),
    (S_IG_ACCOUNT_INACTIVE, ("ig account deactivated",)),
    (S_SELF_RECIPIENT, ("self recipient",)),
    (S_CONNECTION_DISCONNECTED, ("ig connection disconnected",)),
    (S_DUPLICATE_CLEANUP, ("duplicate campaign on same media",)),
    (S_GHOST_CLEANUP, ("유령 오프닝",)),
    (S_MESSAGING_WINDOW_SKIP, ("메시징 윈도우 밖",)),
)

SKIPPED_STATUS = "skipped"


def _norm(value) -> str:
    return str(value or "").strip()


def skipped_user_reason(error_message: str) -> str:
    """건너뜀 로그의 error_message → 사유 키. 미매칭은 ``other``."""
    text = _norm(error_message).lower()
    for reason, needles in _SKIPPED_NEEDLES:
        if any(n.lower() in text for n in needles):
            return reason
    return S_OTHER


def user_reason_for(
    status: str, error_code: str = "", error_subcode: str = "", error_message: str = ""
) -> str:
    """(status, code, subcode, message) → `user_reason`. 오류가 아니면 빈 문자열.

    우선순위를 바꿀 때는 admin 사전(`dm_error_catalog`)과 함께 봐야 한다 — 갈리면 같은
    로그에 대해 어드민과 유저 화면이 다른 사유를 말하게 된다
    (``tests_dm_user_copy.py::TestAdminCatalogParity`` 가 전 조합을 대조한다).
    """
    status = _norm(status)
    code = _norm(error_code)
    subcode = _norm(error_subcode)

    if status == SKIPPED_STATUS:
        return skipped_user_reason(error_message)

    if subcode in _BY_SUBCODE:
        return _BY_SUBCODE[subcode]
    if (code, status) in _BY_CODE_STATUS:
        return _BY_CODE_STATUS[(code, status)]
    if code in _BY_CODE:
        return _BY_CODE[code]
    return _BY_STATUS.get(status, NO_REASON)
