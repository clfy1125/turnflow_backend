"""apps/admin_api/dm_error_catalog.py — DM 오류 코드 → 원인·조치·분류 사전 (OPS-2-b).

운영 대시보드 `오류 상세` 팝업이 숫자(`-1`, `200`)만 보여주던 문제를 없애기 위해,
``(error_code, error_subcode, status)`` 조합에 대한 **한국어 원인·조치**를 서버가 준다.
프론트에 사전을 두면 새 코드가 나타날 때마다 프론트 배포가 필요하지만, 서버가 주면
백엔드 배포 한 번으로 끝난다.

판정 우선순위 (좁은 것 → 넓은 것):
  1. (code, subcode) 정확 일치
  2. (code, status)  같은 코드가 여러 의미를 갖는 경우의 갈래 (예: code 10 = 권한 or 윈도우)
  3. (code)          코드 단위
  4. status          상태 단위 기본값
  5. 없음 → 빈 문자열 (프론트 로컬 사전 폴백)

분류 근거의 원본은 :mod:`apps.integrations.dm_exceptions`(classify_api_error) 와
:mod:`apps.integrations.dm_frontend_actions`(사용자 안내 문구) 다 — 여기 문구를 고칠 때
그쪽 분류 로직과 어긋나지 않는지 확인할 것.

실측 목록은 ``python manage.py dump_dm_error_census`` 로 뽑는다(OPS-2-c).

──────────────────────────────────────────────────────────────────────────
분류 체계 (2026-07-31 개정 — 5분류 → 2분류)
──────────────────────────────────────────────────────────────────────────
운영 화면에서 사람이 답해야 할 질문은 하나뿐이다:
**"사람이 봐야 하는가, 아니면 정해진 대로 자동 처리되는가?"**

  policy = investigate  🔴 원인 미확정이거나 우리 판단·조치가 필요 → 사람이 열어봐야 함
  policy = normal       ⚪ 원인이 확정돼 있고 대응도 정해져 있음 → 자동 처리·안내

기존의 '사용자 조치'(재연동·결제)와 '수신자 사정'은 **대응이 정해져 있으므로 normal** 이다.
그 대신 "무엇이 자동으로 나가는가"를 ``auto_action`` 으로 따로 단다.

⚠️ ``auto_action`` 은 지금 **API 로 내보내지 않는다**(어드민 화면에 구현여부 배지를 만들지
   않기로 결정 — 안내를 구현한 뒤 UI 를 또 고쳐야 하므로). 유저 콘솔 안내를 구현할 때
   "어떤 안내를 띄울지"의 단일 소스로 쓰기 위해 사전에만 들고 있는다.
   구현 현황은 ``DM_ERROR_POLICY_PLAN.md`` §3 체크리스트에서 관리한다.

──────────────────────────────────────────────────────────────────────────
사유 머신 키 ``reason`` (2026-07-31 추가 — 프론트 DM-14)
──────────────────────────────────────────────────────────────────────────
화면이 "사유별 보러가기"로 드릴다운하려면 **문구가 아니라 키**로 필터해야 한다.
한국어 title 은 앞으로도 다듬을 예정이고, ``(code, subcode)`` 는 사유와 1:1 이 아니다
(같은 "인스타가 '대화창 밖'이라며 거부"가 4개 조합으로 오고, 반대로 code 10 하나가 두 사유로
갈린다). 그래서 사전 항목마다 고정 키를 달고 그 키로 필터한다.

불변식 (테스트가 강제):
  - ``reason`` ↔ ``title`` 은 **1:1** — 같은 reason 을 쓰는 항목은 title 도 같아야 한다
    (프론트가 reason 으로 묶어 title 을 보여주므로, 어긋나면 한 칩에 두 문구가 생긴다).
  - 오류 사전 reason 과 건너뜀 사유 키는 **서로 겹치지 않는다** (한 파라미터로 필터하므로).
  - SQL 필터(``dm_error_filters``)의 판정 결과 == 여기 파이썬 판정 결과.
"""

from __future__ import annotations

from apps.integrations.dm_status_groups import (
    HIDDEN_SPAM_SUBCODE,
    WINDOW_PEAK_SUBCODE,
    WINDOW_STALLED_SUBCODE,
)

# ── 사유 머신 키 (DM-14) ──────────────────────────────────────────────
# 값은 **영구 고정**이다. 문구를 바꿔도 여기는 그대로 둘 것 — 프론트 링크·저장된 필터가 깨진다.
R_WINDOW_AFTER_CLOSE = "window_after_close"
R_ALREADY_REPLIED = "already_replied"
R_HIDDEN_SPAM_INBOX = "hidden_spam_inbox"
R_RECIPIENT_NOT_FOUND = "recipient_not_found"
R_CONVERSATION_DELETED = "conversation_deleted"
R_POST_BLOCKED = "post_blocked"
R_STALLED_BY_US = "stalled_by_us"
R_WINDOW_PEAK_BACKLOG = "window_peak_backlog"
R_CONNECTION_LOST = "connection_lost"
R_SESSION_EXPIRED = "session_expired"
R_PERMISSION_OR_WINDOW_UNKNOWN = "permission_or_window_unknown"
R_NO_TARGET_OR_EXPIRED = "no_target_or_expired"
R_SCOPED_PERMISSION = "scoped_permission"
R_RECIPIENT_UNREACHABLE = "recipient_unreachable"
R_RATE_LIMITED = "rate_limited"
R_APP_RATE_LIMITED = "app_rate_limited"
R_NO_REASON_GIVEN = "no_reason_given"
R_TOKEN_INVALID = "token_invalid"
R_WINDOW_EXPIRED_LEGACY = "window_expired_legacy"
R_NO_TRACE = "no_trace"
R_NO_TRACE_UNUSED = "no_trace_unused"  # 아래 _BY_CODE["no_trace"] 전용 (죽은 항목)
R_LEGACY_FAILURE = "legacy_failure"
R_RECOVERY_PENDING = "recovery_pending"
R_RECOVERY_EXPIRED = "recovery_expired"

# 사전 어디에도 걸리지 않은 조합. **오류 8종 상태는 전부 _BY_STATUS 에 있으므로 실제로는
# 도달하지 않는다** — 새 실패 status 가 생겼는데 사전을 안 고친 경우의 안전망이다.
R_UNCLASSIFIED = "unclassified"
UNCLASSIFIED_TITLE = "분류되지 않은 실패"

# ── 분류(policy) ─────────────────────────────────────────────────────
INVESTIGATE = "investigate"  # 🔴 확인해야함
NORMAL = "normal"  # ⚪ 정상 (자동 처리)

# DM-12 — 상태 그룹의 "확인 필요"(status_group=attention)와 이름이 겹치지 않게 고른 표시명.
# 두 축은 독립이라 한 행에 나란히 뜬다: "확인 필요 · 자동 처리"(재연동 안내가 자동으로 나감) /
# "확인 필요 · 조사 필요"(사람이 봐야 함). policy 는 **조치 축**이므로 동사로 적는다.
# (그룹 쪽 이름을 바꾸는 안도 있었지만 그건 유저 콘솔 탭 이름이라 제품 결정이 필요하다.)
POLICY_DISPLAY = {
    INVESTIGATE: "조사 필요",
    NORMAL: "자동 처리",
}

# ── 자동 조치(auto_action) — policy=normal 일 때 무엇이 자동으로 나가는가 ──
# policy=investigate 면 항상 NO_ACTION 이다(자동 처리가 가능하면 조사 대상이 아니다).
NO_ACTION = "none"
RECONNECT_NOTICE = "reconnect_notice"  # 재연동 안내
UPGRADE_NOTICE = "upgrade_notice"  # 한도 소진 → 결제 유도
RECOVERY_FLOW = "recovery_flow"  # 실패 DM 복구 자동 진행
PEAK_NOTICE = "peak_notice"  # "요청이 몰려 제때 못 보냈다" 안내
EXPIRY_NOTICE = "expiry_notice"  # 메시징 창·댓글 만료 안내
RECIPIENT_NOTICE = "recipient_notice"  # 수신자 사정 안내 (조치 불가)


def _entry(
    reason: str,
    title: str,
    cause: str,
    action: str,
    policy: str,
    auto_action: str = NO_ACTION,
) -> dict:
    """사전 1행. policy=investigate 면 auto_action 은 강제로 none 이다."""
    return {
        "reason": reason,
        "title": title,
        "cause": cause,
        "action": action,
        "policy": policy,
        "auto_action": NO_ACTION if policy == INVESTIGATE else auto_action,
    }


# ── (code, subcode) 정확 일치 ────────────────────────────────────────
_BY_CODE_SUBCODE: dict[tuple[str, str], dict] = {
    ("100", HIDDEN_SPAM_SUBCODE): _entry(
        R_HIDDEN_SPAM_INBOX,
        "숨겨진 요청 · 스팸함으로 들어감",
        "상대가 아직 팔로워가 아니라 대화방이 열려 있지 않아, 첫 DM 이 상대의 '메시지 요청' "
        "탭으로 들어갔습니다. 보내기는 했고 도착만 확인되지 않은 상태입니다.",
        "실패가 아닙니다. 고객이 '실패 DM 복구'(프로 플랜)를 켜면 댓글에 '요청함에서 "
        "수락하고 다시 댓글을 달아달라'는 안내를 남겨 자동으로 다시 보냅니다.",
        NORMAL,
        RECOVERY_FLOW,
    ),
    ("100", "2534014"): _entry(
        R_RECIPIENT_NOT_FOUND,
        "받는 사람을 찾을 수 없음",
        "Instagram 이 댓글을 쓴 사람을 찾지 못했습니다. 계정을 지웠거나, 비활성화했거나, "
        "우리를 차단한 경우입니다. 계정을 다시 연동하면 저장해 둔 받는 사람 정보가 "
        "무효가 되기도 합니다.",
        "한두 건이면 상대방 사정이라 조치할 것이 없습니다. 한 계정에서 계속 나오면 "
        "Instagram 재연동을 안내하세요.",
        NORMAL,
        RECIPIENT_NOTICE,
    ),
    # 실측 원문(2026-07-31 어드민팀 제보): "대화 소유자가 이 대화를 보관했거나 삭제했습니다.
    # 또는 대화가 존재하지 않습니다. | http=400 | code=100 | subcode=2534001"
    # 등록 전에는 code=100 일반 항목으로 떨어져 화면에 "댓글이 7일 넘음"으로 잘못 떴다.
    ("100", "2534001"): _entry(
        R_CONVERSATION_DELETED,
        "상대가 DM 대화방을 지움",
        "DM 을 보낼 대화방이 없어졌습니다. 상대가 대화방을 삭제했거나 보관함으로 옮긴 "
        "경우입니다. 첫 DM 은 정상 발송된 뒤 후속 DM 차례에 주로 발생합니다.",
        "상대방 사정이라 조치할 수 없습니다. 한 계정에서 반복되면 알려주세요.",
        NORMAL,
        RECIPIENT_NOTICE,
    ),
    # ★ "24시간이 지나서"라고 단정하면 안 된다 (2026-08-07 실측으로 반증됨).
    #   prod 의 이 계열 실패 6건은 **전부** 오프닝 도착 4초~6분 뒤에 발생했다. Meta 가 준
    #   subcode 의 문서상 의미(창 만료)를 그대로 문구로 옮겨 적은 탓에, 실제로는 3초 만에
    #   거부당한 건에도 "24시간이 지났다"고 표시돼 어드민이 장애로 오인했다.
    #   진짜 시간 경과는 우리 내부 age 가드가 error_code="" 로 따로 종결한다(아래 두 항목).
    ("100", "2534022"): _entry(
        R_WINDOW_AFTER_CLOSE,
        "인스타가 '대화창 밖'이라며 거부",
        "Instagram 은 상대가 **메시지를 보낸** 뒤 24시간 안에만 DM 을 보내게 합니다. "
        "댓글은 이 창을 열지 않습니다(댓글은 답장 1회 권한만 줍니다). 시간이 실제로 지나서일 "
        "수도 있고, 상대가 방금 반응했는데도 Instagram 이 그것을 창 열림으로 인식하지 못한 "
        "경우일 수도 있습니다 — Instagram 이 세부 사유를 주지 않아 구분되지 않습니다.",
        "발송 시각과 상대의 마지막 반응 시각을 비교해 보세요. 간격이 24시간보다 훨씬 짧으면 "
        "Instagram 쪽 인식 문제이며 우리 잘못이 아닙니다.",
        INVESTIGATE,
    ),
    ("100", "2534023"): _entry(
        R_ALREADY_REPLIED,
        "그 댓글에 이미 답장이 있음",
        "댓글 하나에는 답장 DM 을 한 번만 보낼 수 있습니다. 우리가 이미 보냈는데 "
        "Instagram 응답이 끊겨 다시 시도했거나, 다른 DM 자동화 툴이 먼저 답장한 경우입니다.",
        "다시 시도해도 성공하지 않아 바로 종료합니다. 같은 게시물에 켜져 있는 캠페인이 "
        "2개 이상인지, 다른 DM 툴이 함께 연결돼 있는지 확인하세요.",
        INVESTIGATE,
    ),
    # 실측 사례가 있는 유일한 조합 (prod 6건, 2026-08-07 원인 규명 완료).
    # 6건 모두 팔로우 게이트의 후속(reward) DM 이었고, 상대가 버튼을 누른 지 2~3초 만에
    # 거부됐다. Meta 대화 기록 대조 결과 그 버튼 탭이 '비즈니스 발신'으로 잘못 기록돼
    # (6건 중 4건) 24h 창이 열리지 않았다 — 우리 웹훅에는 상대 발신으로 오는데 Meta 의
    # Conversations API 는 다르게 보고하는 내부 불일치라 우리가 막을 수 없다.
    ("10", "2534022"): _entry(
        R_WINDOW_AFTER_CLOSE,
        "인스타가 '대화창 밖'이라며 거부",
        "24시간이 지나서가 아닙니다. 실제 발생 건은 모두 오프닝 DM 이 도착한 지 "
        "몇 초~몇 분 만에 나간 후속 DM 이었습니다. 대화창은 상대의 버튼 탭이 '상대가 보낸 "
        "메시지'로 기록될 때 열리는데, Instagram 이 그 탭을 간헐적으로 '우리가 보낸 것'으로 "
        "잘못 기록하거나 반영이 늦어 곧바로 나간 후속 DM 이 거부됩니다.",
        "상대가 버튼을 다시 누르면 자동으로 재발송됩니다(20초·1분·3분 자동 재시도 + 재탭 "
        "복구). 그래도 이 상태로 남아 있으면 두 복구 경로가 모두 실패한 것이니 알려주세요.",
        INVESTIGATE,
    ),
    ("10", "2018278"): _entry(
        R_WINDOW_AFTER_CLOSE,
        "인스타가 '대화창 밖'이라며 거부",
        "위와 같은 사유를 Instagram 이 다른 번호로 준 경우입니다. 대화창이 열려 있지 않다는 "
        "뜻일 뿐, 반드시 24시간이 지났다는 의미는 아닙니다.",
        "발송 시각과 상대의 마지막 반응 시각을 비교해 보세요. 간격이 짧으면 Instagram 쪽 "
        "인식 문제이며, 상대가 버튼을 다시 누르면 자동 재발송됩니다.",
        INVESTIGATE,
    ),
    ("200", "2534066"): _entry(
        R_POST_BLOCKED,
        "이 게시물만 자동 DM 이 막힘",
        "Instagram 이 이 게시물에 한해 자동 DM 을 막았습니다. 같은 계정이라도 다른 "
        "게시물은 정상 발송되며, 과거에 이 게시물로 대량 발송한 이력이 있으면 주로 "
        "나타납니다.",
        "이 게시물은 풀 수 없습니다. 새 게시물로 캠페인을 옮기고, 한 게시물에 짧은 시간 "
        "대량 발송을 피하도록 안내하세요. (대응 정책 사내 논의 중)",
        INVESTIGATE,
    ),
    # ── 내부 표식: 보낼 수 있는 기간 만료의 두 갈래 (code 는 항상 "" — 우리가 종결한 건) ──
    ("", WINDOW_STALLED_SUBCODE): _entry(
        R_STALLED_BY_US,
        "우리가 제때 못 보냄",
        "발송 순서를 기다리는 동안 보낼 수 있는 기간(댓글 답장 7일 / DM 24시간)이 "
        "지났습니다. 예약된 시각이 한참 지나도록 처리되지 않은 건이라 고객이 아니라 "
        "우리 시스템 문제입니다.",
        "개발팀에 알려 발송 처리기가 멈췄는지 확인하세요. 이 숫자가 0 이 아니면 다른 "
        "캠페인도 같이 밀리고 있을 수 있습니다.",
        INVESTIGATE,
    ),
    ("", WINDOW_PEAK_SUBCODE): _entry(
        R_WINDOW_PEAK_BACKLOG,
        "요청이 몰려 기간 안에 못 보냄",
        "짧은 시간에 요청이 몰려 발송 대기줄이 길어진 사이, 보낼 수 있는 기간(댓글 답장 "
        "7일 / DM 24시간)이 지났습니다. 그동안 발송 시도는 계속되고 있었습니다.",
        "조치할 것은 없습니다. 반복되면 발송 속도 조절과 계정 분산을 검토하고, 고객에게는 "
        "'요청이 몰려 제때 보내지 못했다'고 안내합니다.",
        NORMAL,
        PEAK_NOTICE,
    ),
}

# ── (code, status) — 같은 코드가 두 의미를 갖는 경우의 갈래 ───────────
# Meta 는 code 10 으로 '권한 부족'과 '24h 윈도우 만료'를 모두 반환한다. subcode 가 없으면
# 코드만으로는 못 가르지만, 우리 분류기가 이미 status 로 갈라놨으므로 그걸 신뢰한다.
_BY_CODE_STATUS: dict[tuple[str, str], dict] = {
    ("10", "failed_window"): _entry(
        R_WINDOW_AFTER_CLOSE,
        "인스타가 '대화창 밖'이라며 거부",
        "대화창이 열려 있지 않다며 발송이 막혔습니다. Instagram 이 세부 번호를 주지 않아 "
        "권한 문제와 구분되지 않지만, 우리 분류는 창 쪽으로 봤습니다. 시간 경과가 원인이라고 "
        "단정할 수는 없습니다.",
        "Instagram 원문, 발송 시각과 상대의 마지막 반응 시각 간격을 함께 확인하세요.",
        INVESTIGATE,
    ),
}

# ── code 단위 (subcode 무관) ─────────────────────────────────────────
_BY_CODE: dict[str, dict] = {
    "190": _entry(
        R_CONNECTION_LOST,
        "Instagram 연결이 끊김",
        "Instagram 접속 권한이 만료됐거나 회수됐습니다. 고객이 비밀번호를 바꿨거나, "
        "Instagram 설정에서 우리 앱 연결을 해제했거나, 60일 유효기간이 끝난 경우입니다.",
        "고객에게 Instagram 재연동을 안내하세요. 연결 상태는 'IG 연결' 화면에서 볼 수 있습니다.",
        NORMAL,
        RECONNECT_NOTICE,
    ),
    "102": _entry(
        R_SESSION_EXPIRED,
        "Instagram 연결이 끊김 (세션 만료)",
        "Instagram 접속 세션이 더 이상 유효하지 않습니다. 위 '연결 끊김'과 같은 계열입니다.",
        "고객에게 Instagram 재연동을 안내하세요.",
        NORMAL,
        RECONNECT_NOTICE,
    ),
    "10": _entry(
        R_PERMISSION_OR_WINDOW_UNKNOWN,
        "권한 문제인지 기간 만료인지 불명",
        "Instagram 이 '권한 부족'과 '보낼 수 있는 기간 만료' 두 가지를 같은 번호로 줍니다. "
        "세부 번호가 없어 어느 쪽인지 가려지지 않습니다 — 조치가 정반대라 그냥 둘 수 없습니다.",
        "Instagram 원문을 열어 어느 쪽인지 확인하세요. 권한 쪽이면 재연동 안내, 기간 "
        "만료 쪽이면 조치할 것이 없습니다.",
        INVESTIGATE,
    ),
    # DM-9 — 원인을 하나로 단정하지 않는다. code 100 은 최소 5가지가 섞이는 통이라
    # "대부분 7일 초과"로 적으면 나머지에서 화면 설명이 Instagram 원문과 어긋난다
    # (실측: subcode 2534001 '대화방 삭제' 건에 "댓글이 7일 넘음"이 떴다).
    "100": _entry(
        R_NO_TARGET_OR_EXPIRED,
        "보낼 대상이 없거나 만료됨",
        "DM 을 보낼 댓글이나 대화방이 이미 없었습니다. 아래 중 하나입니다. "
        "· 댓글을 쓴 지 7일이 지남 — 댓글 답글로는 7일까지만 보낼 수 있습니다 "
        "· 댓글이나 게시물이 삭제됨 · 상대가 DM 대화방을 지움",
        "정확한 사유는 Instagram 원문에 있습니다. 대부분 상대방 사정이라 조치할 것이 "
        "없습니다. 한 캠페인에 몰리면 알려주세요.",
        INVESTIGATE,
    ),
    "200": _entry(
        R_SCOPED_PERMISSION,
        "이 사람·이 게시물에 대한 권한 문제",
        "계정 전체가 아니라 이 받는 사람 또는 이 게시물에만 걸린 제한입니다. 계정 전체를 "
        "멈추지 않도록 '도착 미확인'으로 처리합니다.",
        "한두 건이면 넘어가도 됩니다. 한 게시물에서 반복되면 그 게시물의 자동 DM 이 막힌 "
        "것이므로 새 게시물로 옮기도록 안내하세요.",
        INVESTIGATE,
    ),
    "551": _entry(
        R_RECIPIENT_UNREACHABLE,
        "상대가 메시지를 받을 수 없음",
        "상대가 우리를 차단했거나, 메시지 요청을 거부했거나, 비공개 계정이라 받을 통로가 "
        "없습니다.",
        "상대방 사정이라 조치할 수 없습니다. 한 캠페인에서 비율이 유난히 높으면 문구나 "
        "타깃을 점검해 보세요.",
        NORMAL,
        RECIPIENT_NOTICE,
    ),
    "613": _entry(
        R_RATE_LIMITED,
        "Instagram 이 잠시 속도를 늦춤",
        "짧은 시간에 너무 많이 호출해 Instagram 이 잠시 막았습니다. 실패가 아니라 지연이며 "
        "서버가 알아서 다시 시도합니다.",
        "조치할 것이 없습니다. 계속 나오면 발송 속도 조절을 검토하세요.",
        NORMAL,
    ),
    "4": _entry(
        R_APP_RATE_LIMITED,
        "앱 전체 호출 한도 초과",
        "특정 계정이 아니라 우리 앱 전체가 Instagram 호출 한도에 걸렸습니다. 역시 자동으로 "
        "다시 시도합니다.",
        "조치할 것이 없습니다. 반복되면 발송을 시간대별로 나누는 것을 검토하세요.",
        NORMAL,
    ),
    "-1": _entry(
        R_NO_REASON_GIVEN,
        "Instagram 이 이유를 알려주지 않음",
        "Instagram 이 사유 없이 실패를 반환했습니다. 대개 Instagram 쪽 일시 장애입니다. "
        "드문드문 섞이면 정상 범위이고, 특정 계정·게시물에만 몰리면 그쪽에 문제가 있습니다.",
        "Instagram 원문으로 실제 사유를 확인하세요. 한 계정에 몰려 있으면 연결 상태와 "
        "게시물 상태를 점검합니다.",
        INVESTIGATE,
    ),
    # ⚠️ 미사용(디버깅용) — 실제 로그의 error_code 는 빈 문자열이라 이 항목은 호출되지 않는다
    #    (tests_dashboard_ops.py 가 code=="" 를 단언). 실제로 뜨는 건 아래 _BY_STATUS 쪽이다.
    #    지우지 않고 남겨 두되 "안 쓰인다"를 여기 명시한다 (2026-07-31 결정).
    "no_trace": _entry(
        R_NO_TRACE_UNUSED,
        "도착 미확인 (미사용 항목)",
        "Instagram 이 접수는 했지만 35분 동안 도착 흔적을 찾지 못한 경우입니다. "
        "(이 사전 항목 자체는 현재 코드 경로에서 호출되지 않습니다 — 실제로 뜨는 것은 "
        "아래 상태 단위 '도착 미확인' 항목입니다.)",
        "화면 동작에는 영향이 없습니다. 코드 정리 대상인지만 확인하면 됩니다.",
        NORMAL,
    ),
}

# ── status 단위 기본값 (코드가 비어 있는 실패) ────────────────────────
_BY_STATUS: dict[str, dict] = {
    "failed_token": _entry(
        R_TOKEN_INVALID,
        "Instagram 연결이 끊겨 보내지 못함",
        "연결이 끊긴 상태라 Instagram 에 요청조차 하지 않고 막았습니다.",
        "고객에게 Instagram 재연동을 안내하세요.",
        NORMAL,
        RECONNECT_NOTICE,
    ),
    # 신규 건은 내부 가드가 subcode(window_stalled/window_peak)를 남기므로 위 (code,subcode)
    # 항목에서 갈린다. 여기 오는 건 **subcode 가 없는 과거 데이터**뿐이다.
    "failed_window": _entry(
        R_WINDOW_EXPIRED_LEGACY,
        "보낼 수 있는 기간이 지남",
        "보낼 수 있는 기간(댓글 답장 7일 / DM 24시간)이 지난 뒤 발송 차례가 돌아왔습니다. "
        "예전 기록이라 '몰려서'인지 '우리가 늦어서'인지는 구분되지 않습니다.",
        "조치할 것은 없습니다. 발송 적체가 크면 발송 속도 조절을 점검하세요.",
        NORMAL,
        EXPIRY_NOTICE,
    ),
    "failed_param": _entry(
        R_NO_TARGET_OR_EXPIRED,
        "보낼 대상이 없거나 만료됨",
        "DM 을 보낼 댓글이나 대화방이 이미 없었습니다. 댓글을 쓴 지 7일이 지났거나, "
        "댓글·게시물이 삭제됐거나, 상대가 대화방을 지운 경우입니다.",
        "정확한 사유는 Instagram 원문에 있습니다. 대부분 상대방 사정이라 조치할 것이 " "없습니다.",
        INVESTIGATE,
    ),
    "failed_no_trace": _entry(
        R_NO_TRACE,
        "도착 미확인",
        "Instagram 이 접수는 했는데 35분을 지켜봐도 도착한 흔적이 없습니다. 상대의 메시지 "
        "수신 설정이 꺼져 있거나, 다른 DM 툴이 메시지 경로를 가로챘거나, 상대 계정에 "
        "제한이 걸린 경우입니다.",
        "로그 상세의 [다시 확인] 버튼을 눌러보세요. 도착이 확인되면 성공으로 바뀝니다. "
        "한 계정에서 비율이 높으면 고객에게 Instagram 메시지 설정과 다른 DM 툴 해제를 "
        "안내하세요.",
        INVESTIGATE,
    ),
    "failed": _entry(
        R_LEGACY_FAILURE,
        "실패 (예전 형식 기록)",
        "사유를 나눠 기록하기 전 버전의 실패라 원인이 남아 있지 않습니다.",
        "예전 기록이면 그대로 두면 됩니다. 최근 날짜로 새로 찍혔다면 개발팀에 알려주세요.",
        INVESTIGATE,
    ),
    "failed_api": _entry(
        R_LEGACY_FAILURE,
        "실패 (예전 형식 기록)",
        "사유를 나눠 기록하기 전 버전의 실패입니다. 지금 코드에서는 더 이상 생기지 않아야 "
        "합니다.",
        "예전 기록이면 그대로 두면 됩니다. 최근 날짜로 새로 찍혔다면 개발팀에 알려주세요.",
        INVESTIGATE,
    ),
    "recovery_pending": _entry(
        R_RECOVERY_PENDING,
        "복구 진행 중",
        "상대의 숨겨진 요청·스팸함으로 간 DM 에 대해 '요청함에서 수락하고 다시 댓글을 "
        "달아달라'는 답글을 남기고 기다리는 중입니다. 아직 실패가 아닙니다.",
        "조치할 것이 없습니다. 다시 댓글이 오면 자동으로 재발송되고, 기한이 지나면 " "만료됩니다.",
        NORMAL,
        RECOVERY_FLOW,
    ),
    "recovery_expired": _entry(
        R_RECOVERY_EXPIRED,
        "복구 기한 만료",
        "안내 답글을 남긴 뒤 기한 안에 다시 댓글이 오지 않아 끝났습니다.",
        "조치할 것이 없습니다. 비율이 높으면 안내 문구를 점검해 보세요.",
        NORMAL,
    ),
}

_EMPTY = {
    "reason": "",
    "title": "",
    "cause": "",
    "action": "",
    "policy": "",
    "auto_action": NO_ACTION,
}

# ── 건너뜀(skipped) 사유 ──────────────────────────────────────────────
# skipped = "Meta 에 요청을 보내지 않고 취소한 건". **실패가 아니라 발송을 시작하지 않은
# 상태**이며, 조치가 필요한 것은 월 DM 한도 하나뿐이다(업셀 신호).
# 사유 컬럼이 따로 없어 error_message 문자열로 판별한다.
#   (원문 출처: integrations/tasks.py 의 log.mark_skipped(...) 호출부 +
#    integrations/models.py 의 IGAccountConnection._halt_automation)
# ⚠️ 뒤 2개는 **운영 중 수동 정리**로 찍힌 문구다(2026-07 중복 캠페인/유령 오프닝 사고
#    대응). 코드 경로가 아니라 사고 대응 셸에서 나왔지만 prod 실측 66건 중 40건을 차지해,
#    없으면 패널의 다수가 '기타'로 보인다 — 지우지 말 것.
#
# 2026-07-31: `views/dashboard_ops.py` 에서 여기로 옮겼다 — 오류 사전과 **같은 reason
# 네임스페이스**를 쓰고(`?error_reason=` 한 파라미터로 둘 다 필터), `not_sent` 분해가
# 건너뜀 로그에도 사유 라벨을 붙일 수 있어야 하기 때문이다(전에는 '분류되지 않은 실패').
# 형식: (reason 키, 한국어 라벨, actionable, 매칭 부분문자열들)
SKIPPED_REASONS: tuple[tuple[str, str, bool, tuple[str, ...]], ...] = (
    ("monthly_dm_limit", "월 DM 한도 도달", True, ("monthly_dm_limit_reached",)),
    ("campaign_not_active", "캠페인 일시정지 중", False, ("campaign not active",)),
    ("outside_schedule_window", "예약 발송 창 밖", False, ("outside active schedule window",)),
    ("ig_account_inactive", "IG 계정 비활성(플랜 축소)", False, ("ig account deactivated",)),
    ("self_recipient", "계정 자신의 댓글", False, ("self recipient",)),
    ("connection_disconnected", "IG 연결 해제 정리", False, ("ig connection disconnected",)),
    # DM-18(2026-08-06): 라벨에 **`(관리자 수동 조치)`** 접미사를 통일했다.
    #   ① `유령 오프닝` 은 2026-07-21 사고를 겪은 사람만 아는 내부 은어였다 — 화면만 보고
    #      무슨 일이었는지 알 수 없었다(로그 상세의 Meta 원문을 열어야 했다).
    #   ② 자동 분류 쪽에 **같은 원인**의 `그 댓글에 이미 답장이 있음`(🔴 investigate,
    #      code 100 · subcode 2534023)이 있다. 접미사가 없으면 한 팝업에 거의 같은 문장이
    #      두 줄 뜨고 "왜 하나는 🔴 이고 하나는 ⚪ 인가"가 설명되지 않는다. 차이는
    #      **사람이 이미 보고 닫았다** 하나뿐이라 그걸 문구에 넣는다.
    #   ③ "이미 **나가**" 가 아니라 "이미 **있어**" 인 것은 주어 때문이다 — 우리가 보낸 게
    #      아니라 다른 DM 자동화 툴이 먼저 답장한 경우도 있어 발신자를 특정하지 않는다.
    # ⚠️ **네 번째 값(매칭 문자열)은 절대 같이 고치지 말 것.** 그건 과거 로그의
    #    error_message 원문을 맞추는 값이라, 라벨과 함께 바꾸면 기존 행이 매칭에서 빠져
    #    전부 `기타` 로 떨어진다 (prod 건너뜀의 다수가 이 두 사유다).
    (
        "duplicate_campaign_cleanup",
        "같은 게시물 중복 캠페인 정리 (관리자 수동 조치)",
        False,
        ("duplicate campaign on same media",),
    ),
    (
        "ghost_opening_cleanup",
        "답장이 이미 있어 발송 취소 (관리자 수동 조치)",
        False,
        ("유령 오프닝",),
    ),
    # 2026-08-07: `other`(미분류)로 떨어지던 실측 문구를 등록한다. 발송 직전 가드가
    # "보낼 수 있는 창이 이미 지났다"를 확인하고 Meta 를 부르기 전에 취소한 건이라
    # **정상 동작**이다(발송 실패가 아님). 위 2건과 마찬가지로 현재 코드 경로에서는
    # 더 이상 찍히지 않는 과거 기록이며(2026-07-07~09), 미분류로 두면 '조사 필요'로
    # 잡혀 운영자가 매번 열어보게 된다.
    (
        "messaging_window_skip",
        "발송 가능 시간이 지나 건너뜀",
        False,
        ("메시징 윈도우 밖",),
    ),
)
SKIPPED_OTHER = ("other", "기타", False)

# ``SentDMLog.Status.SKIPPED`` 의 값. 사전 모듈이 모델을 import 하지 않도록 리터럴로 두고
# (import 그래프를 가볍게 유지), 두 값이 갈라지면 테스트가 잡는다.
SKIPPED_STATUS = "skipped"


def classify_skipped(message: str) -> tuple[str, str, bool]:
    """건너뜀 로그의 error_message → (reason, label, actionable). 미매칭은 other."""
    text = (message or "").strip().lower()
    for reason, label, actionable, needles in SKIPPED_REASONS:
        if any(n in text for n in needles):
            return reason, label, actionable
    return SKIPPED_OTHER


def _skipped_entry(message: str) -> dict:
    """건너뜀 로그 1건 → 오류 사전과 **같은 모양**의 dict.

    건너뜀은 전부 '설정대로 동작'이라 정상이다 — 유일한 예외가 미분류(other)로,
    사전에 없는 문구가 찍혔다는 뜻이므로 사람이 봐야 한다(오류 사전과 같은 규칙).
    """
    reason, label, _actionable = classify_skipped(message)
    policy = INVESTIGATE if reason == SKIPPED_OTHER[0] else NORMAL
    return {
        "reason": reason,
        "title": label,
        "cause": "",
        "action": "",
        "policy": policy,
        "auto_action": NO_ACTION,
    }


# sample_error_message 상한 (원문이 길 수 있어 자름)
SAMPLE_ERROR_MESSAGE_MAX = 500

# 사전 미등록일 때 '확인해야함'으로 떨어뜨릴 상태들 (= 오류로 집계되는 상태).
# 성공/진행 중 상태(delivered·read·queued…)는 여기 없으므로 normal 로 남는다.
_ERROR_STATUSES = frozenset(
    (
        "failed_token",
        "failed_window",
        "failed_param",
        "failed_no_trace",
        "failed",
        "failed_api",
        "recovery_pending",
        "recovery_expired",
    )
)


def describe(code: str, subcode: str, status: str) -> dict:
    """(code, subcode, status) → ``{reason, title, cause, action, policy, auto_action}``.

    미등록이면 reason/title/cause/action/policy 가 모두 빈 문자열이다
    (프론트는 title 이 있으면 그것을, 없으면 기존 로컬 사전으로 폴백).
    **최종 분류가 필요하면 :func:`classify` 나 :func:`policy_for` 를 쓸 것** — 미등록
    오류를 investigate 로 떨어뜨리는 폴백과 건너뜀 처리가 거기 있다.
    """
    code = (code or "").strip()
    subcode = (subcode or "").strip()
    entry = (
        _BY_CODE_SUBCODE.get((code, subcode))
        or _BY_CODE_STATUS.get((code, status))
        or _BY_CODE.get(code)
        or _BY_STATUS.get(status)
        or _EMPTY
    )
    return dict(entry)


def classify(code: str, subcode: str, status: str, message: str = "") -> dict:
    """이 발송 1건의 **최종** 분류 — 폴백까지 적용한 ``describe()``.

    ``describe()`` 와 달리 빈 값을 남기지 않는다. 화면에 쓰는 값은 전부 여기서 나온다:

    - ``status == skipped`` → 건너뜀 사유표(:data:`SKIPPED_REASONS`)로 판정.
      건너뜀은 오류 사전에 없어서 예전에는 '분류되지 않은 실패'로 떨어졌고, 같은 로그가
      운영 대시보드에서는 사유 라벨을 갖는 **두 갈래 판정**이 있었다 — 그걸 없앤다.
    - 오류 사전에도 없으면 ``unclassified`` + 오류 상태면 investigate.

    ``message`` 는 건너뜀 판정에만 쓴다(사유 컬럼이 없어 원문 문자열로 가른다).
    """
    if status == SKIPPED_STATUS:
        return _skipped_entry(message)
    entry = describe(code, subcode, status)
    if entry["policy"]:
        return entry
    entry["reason"] = R_UNCLASSIFIED
    entry["title"] = entry["title"] or UNCLASSIFIED_TITLE
    entry["policy"] = INVESTIGATE if status in _ERROR_STATUSES else NORMAL
    return entry


def policy_for(code: str, subcode: str, status: str, message: str = "") -> str:
    """이 건의 최종 분류 — ``investigate`` | ``normal``.

    사전에 없는 **오류** 조합은 investigate 로 떨어진다: 설명조차 못 다는 건 곧
    "우리가 모르는 실패"이므로 사람이 봐야 한다. 성공·진행 중 상태는 normal.
    """
    return classify(code, subcode, status, message)["policy"]


def reason_for(code: str, subcode: str, status: str, message: str = "") -> str:
    """이 건의 사유 머신 키 (DM-14) — 프론트가 ``?error_reason=`` 에 그대로 싣는 값."""
    return classify(code, subcode, status, message)["reason"]


def _all_entries():
    yield from _BY_CODE_SUBCODE.values()
    yield from _BY_CODE_STATUS.values()
    yield from _BY_CODE.values()
    yield from _BY_STATUS.values()


def reason_policy_map() -> dict[str, str]:
    """오류 사전의 ``reason`` → ``policy``.

    같은 reason 은 policy 도 같아야 한다(테스트가 강제) — 아니면 "사유 하나가 🔴 이면서
    ⚪" 인 행이 생겨 팝업의 분류 묶음이 성립하지 않는다.
    """
    out = {e["reason"]: e["policy"] for e in _all_entries()}
    out[R_UNCLASSIFIED] = INVESTIGATE
    return out


def reason_title_map() -> dict[str, str]:
    """오류 사전의 ``reason`` → ``title`` (reason ↔ title 은 1:1)."""
    out = {e["reason"]: e["title"] for e in _all_entries()}
    out[R_UNCLASSIFIED] = UNCLASSIFIED_TITLE
    return out


# 분류가 의미 있는 상태 = 오류 8종 + 건너뜀. `?error_policy=` / `?error_reason=` 필터의
# 암묵 모수이기도 하다 — 성공·진행 중 로그까지 normal 로 잡으면 "⚪ 자동 처리"를 눌렀을 때
# 도착한 DM 전부가 딸려 나온다.
CLASSIFIABLE_STATUSES = frozenset(_ERROR_STATUSES | {SKIPPED_STATUS})


def policy_display(policy: str) -> str:
    """분류 머신값 → 한국어 표시명 (프론트가 라벨을 하드코딩하지 않도록)."""
    return POLICY_DISPLAY.get(policy, "")


# 복구/재검증 경로가 있는 상태 — recoverable=true 판정 대상.
#   - failed_no_trace: 능동 재검증(GET /{message_id})으로 delivered 승격 가능
#   - recovery_pending/expired: 숨김채널 재댓글 복구 플로우 대상
#   - failed_param@2534025: 숨김채널 복구 대상 (아래 함수가 subcode 로 분기)
_RECOVERABLE_STATUSES = frozenset(("failed_no_trace", "recovery_pending", "recovery_expired"))


def is_recoverable(status: str, error_subcode: str = "") -> bool:
    """이 실패가 복구/재검증 경로를 갖는가 (운영 대시보드 failure_breakdown · 로그 상세 공용).

    recovery 퍼널(recovery_*)보다 넓은 개념 — failed_no_trace(재검증)와
    failed_param@2534025(숨김채널 복구)도 true. 프론트의 '재발송/재검증' 버튼 노출 판단용.
    """
    if status in _RECOVERABLE_STATUSES:
        return True
    return status == "failed_param" and str(error_subcode or "").strip() == HIDDEN_SPAM_SUBCODE


def describe_for_log(code: str, subcode: str, status: str, message: str = "") -> dict:
    """로그 **상세/목록 행**용 평면 dict — ``describe()`` 결과에 ``error_`` 접두를 붙이고
    ``recoverable`` 을 함께 낸다 (DM-2).

    로그 상세는 error_code/error_message 등 다른 필드와 한 객체에 섞이므로 대시보드
    (`failure_breakdown`)의 title/cause/action 과 달리 접두를 둔다.

    성공·진행 중 로그는 title/reason 이 **빈 문자열**이다(기존 계약 유지) — 분류가 붙는 것은
    오류 8종과 건너뜀뿐. 건너뜀은 오류 사전에 없으므로 :func:`classify` 가 채운다.
    """
    described = describe(code, subcode, status)
    final = classify(code, subcode, status, message)
    classifiable = status in CLASSIFIABLE_STATUSES
    return {
        "error_title": described["title"] or (final["title"] if classifiable else ""),
        "error_cause": described["cause"],
        "error_action": described["action"],
        "error_reason": described["reason"] or (final["reason"] if classifiable else ""),
        "error_policy": final["policy"],
        "error_policy_display": policy_display(final["policy"]),
        "recoverable": is_recoverable(status, subcode),
    }


def truncate_message(message: str) -> str:
    """원문 오류 메시지를 상한까지 자른다 (초과 시 말줄임)."""
    text = (message or "").strip()
    if len(text) <= SAMPLE_ERROR_MESSAGE_MAX:
        return text
    return text[: SAMPLE_ERROR_MESSAGE_MAX - 1] + "…"
