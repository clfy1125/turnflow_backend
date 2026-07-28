"""apps/admin_api/dm_error_catalog.py — DM 오류 코드 → 원인·조치 사전 (OPS-2-b).

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
"""

from __future__ import annotations

from apps.integrations.dm_status_groups import HIDDEN_SPAM_SUBCODE

# ── (code, subcode) 정확 일치 ────────────────────────────────────────
_BY_CODE_SUBCODE: dict[tuple[str, str], dict] = {
    ("100", "2534025"): {
        "title": "숨겨진 요청 · 스팸함 유입",
        "cause": "수신자가 아직 팔로워가 아니라 DM 채널이 열려 있지 않아, 첫 DM 이 상대의 "
        "'숨겨진 요청/스팸함'으로 들어갔습니다. 발송 자체는 나갔고 도착만 미확인입니다.",
        "action": "실패가 아니라 복구 대상입니다. 해당 워크스페이스의 '실패 DM 복구'(프로 전용)를 "
        "켜면 댓글에 '요청함에서 수락 후 다시 댓글' 안내를 남겨 재발송합니다.",
    },
    ("100", "2534014"): {
        "title": "수신자를 찾을 수 없음",
        "cause": "Meta 가 recipient(댓글 작성자)를 조회하지 못했습니다. 계정 삭제·비활성화·"
        "차단이거나, 재연동으로 instagram-scoped id 스코프가 바뀐 경우입니다.",
        "action": "단발이면 무시해도 됩니다. 한 계정에서 반복되면 IG 연결을 재연동하고 캠페인 "
        "수신자를 다시 수집하세요.",
    },
    ("100", "2534022"): {
        "title": "메시징 윈도우 24시간 만료",
        "cause": "수신자와의 마지막 상호작용으로부터 24시간이 지나 표준 메시징 창이 닫혔습니다.",
        "action": "정책상 정상 실패입니다. 수신자가 다시 반응해야 발송 가능합니다.",
    },
    ("100", "2534023"): {
        "title": "댓글에 이미 답글 있음",
        "cause": "같은 댓글에 이미 비공개 답장이 달려 있습니다. 우리 1차 발송이 성공했는데 응답이 "
        "5xx/타임아웃으로 끊겨 재시도한 자기충돌이거나, 다른 DM 툴이 먼저 답글을 단 경우입니다.",
        "action": "재시도로는 절대 성공하지 않아 즉시 종결합니다. 같은 게시물에 활성 캠페인이 "
        "2개 이상이거나 타사 DM 툴이 함께 연결돼 있지 않은지 확인하세요.",
    },
    ("10", "2534022"): {
        "title": "메시징 윈도우 24시간 만료",
        "cause": "수신자와의 마지막 상호작용으로부터 24시간이 지나 표준 메시징 창이 닫혔습니다.",
        "action": "정책상 정상 실패입니다. 수신자가 다시 반응해야 발송 가능합니다.",
    },
    ("10", "2018278"): {
        "title": "메시징 윈도우 24시간 만료",
        "cause": "24시간 메시징 창 밖 발송입니다(윈도우 만료의 다른 subcode).",
        "action": "정책상 정상 실패입니다. 수신자가 다시 반응해야 발송 가능합니다.",
    },
    ("200", "2534066"): {
        "title": "게시물 단위 자동 메시징 차단",
        "cause": "특정 게시물에 대해 Instagram 이 자동 메시징(비공개 답장)을 차단한 상태입니다. "
        "같은 토큰으로 다른 게시물은 정상 발송되며, 과거 대량 발송 이력이 있는 게시물에서 "
        "주로 나타납니다.",
        "action": "해당 게시물은 풀 수 없습니다. 새 게시물로 캠페인을 옮기고, 한 게시물에 "
        "단시간 대량 발송을 피하세요.",
    },
}

# ── (code, status) — 같은 코드가 두 의미를 갖는 경우의 갈래 ───────────
# Meta 는 code 10 으로 '권한 부족'과 '24h 윈도우 만료'를 모두 반환한다. subcode 가 없으면
# 코드만으로는 못 가르지만, 우리 분류기가 이미 status 로 갈라놨으므로 그걸 신뢰한다.
_BY_CODE_STATUS: dict[tuple[str, str], dict] = {
    ("10", "failed_window"): {
        "title": "메시징 윈도우 24시간 만료",
        "cause": "수신자와의 마지막 상호작용으로부터 24시간이 지나 표준 메시징 창이 닫혔습니다 "
        "(code 10 은 권한 오류와 공유하지만 이 건은 윈도우로 분류됐습니다).",
        "action": "정책상 정상 실패입니다. 수신자가 다시 반응해야 발송 가능합니다.",
    },
}

# ── code 단위 (subcode 무관) ─────────────────────────────────────────
_BY_CODE: dict[str, dict] = {
    "190": {
        "title": "토큰 만료 · 무효",
        "cause": "액세스 토큰이 만료됐거나 권한이 회수됐습니다(비밀번호 변경·앱 권한 해제·"
        "60일 장기 토큰 만료).",
        "action": "고객에게 Instagram 재연동을 안내하세요. 연결 상태는 "
        "`/admin/ig-connections/` 에서 확인할 수 있습니다.",
    },
    "102": {
        "title": "세션 무효",
        "cause": "Meta 세션이 무효화됐습니다(토큰 계열 오류).",
        "action": "Instagram 재연동이 필요합니다.",
    },
    "10": {
        "title": "권한 부족 또는 윈도우 밖",
        "cause": "Meta 가 권한 부족과 24시간 창 만료를 같은 code 10 으로 반환합니다. "
        "subcode 2534022 / 2018278 이면 윈도우 만료, 그 외는 권한(스코프·앱 검수) 문제입니다.",
        "action": "subcode 가 비어 있으면 원문 메시지(sample_error_message)로 갈라내세요. "
        "권한 쪽이면 재연동, 윈도우 쪽이면 정상 실패입니다.",
    },
    "100": {
        "title": "파라미터 오류",
        "cause": "요청 파라미터가 유효하지 않습니다. 대부분 Private Reply 7일 제한 초과"
        "(댓글이 작성된 지 7일 넘음)이거나 recipient id 가 이 계정 스코프에서 무효인 경우입니다.",
        "action": "어떤 파라미터인지는 원문 메시지(sample_error_message)에 있습니다. "
        "7일 초과면 정상 실패, recipient 무효가 반복되면 재연동 후 수신자 재수집.",
    },
    "200": {
        "title": "권한 · 수신자 단위 오류",
        "cause": "토큰 자체가 아니라 이 수신자/게시물에 대한 권한 문제입니다(예: subcode 2534066 "
        "게시물 차단). 연결 전체를 브릭하지 않도록 '도착 미확인'으로 분류합니다.",
        "action": "단발이면 무시. 한 게시물에서 반복되면 그 게시물의 자동 메시징이 차단된 "
        "것이므로 새 게시물로 옮기세요.",
    },
    "551": {
        "title": "수신자 도달 불가",
        "cause": "수신자가 메시지를 받을 수 없는 상태입니다(차단·메시지 요청 거부·비공개 계정 등).",
        "action": "수신자 측 사유라 조치 불가. 반복 비율이 높으면 캠페인 문구/타깃을 점검하세요.",
    },
    "613": {
        "title": "레이트 리밋",
        "cause": "Meta 호출 한도에 걸렸습니다. 일시적 오류라 서버가 자동 재시도합니다.",
        "action": "조치 불필요. 지속되면 발송 페이서 속도를 낮추는 것을 검토하세요.",
    },
    "4": {
        "title": "앱 호출 한도 초과",
        "cause": "앱 단위 호출 한도(rate limit)입니다. 일시적이며 자동 재시도 대상입니다.",
        "action": "조치 불필요. 반복되면 발송량 분산을 검토하세요.",
    },
    "-1": {
        "title": "Meta 내부 오류 (분류 불가)",
        "cause": "Meta 가 코드 없이 실패를 반환했습니다(대개 http 5xx 또는 일시적 내부 오류). "
        "간헐적이면 정상 범위이고, 특정 계정·게시물에 몰리면 그쪽 문제입니다.",
        "action": "원문 메시지(sample_error_message)로 실제 사유를 확인하세요. "
        "특정 계정에 몰리면 토큰/게시물 상태를 점검합니다.",
    },
    # 코드 없이 우리 내부에서 종결한 케이스 (error_code 자리에 센티넬이 들어감)
    "no_trace": {
        "title": "도착 미확인 (내부 종결)",
        "cause": "Meta 가 발송을 접수했지만 35분 내 도착 흔적을 확인하지 못했습니다. "
        "수신자의 '메시지 액세스 허용' 꺼짐, 다른 DM 툴과의 라우팅 충돌, 수신자 계정 제한 등이 "
        "흔한 원인입니다.",
        "action": "`/admin/auto-dm/logs/{id}/reverify/` 로 재검증할 수 있습니다. 한 계정에서 "
        "비율이 높으면 고객에게 IG 메시지 액세스 설정과 타 DM 툴 연결 해제를 안내하세요.",
    },
}

# ── status 단위 기본값 (코드가 비어 있는 실패) ────────────────────────
_BY_STATUS: dict[str, dict] = {
    "failed_token": {
        "title": "토큰 문제로 발송 차단",
        "cause": "연결의 토큰이 만료/무효 상태라 발송 전에 차단됐습니다(Meta 호출 자체를 안 함).",
        "action": "고객에게 Instagram 재연동을 안내하세요.",
    },
    "failed_window": {
        "title": "메시징 윈도우 만료",
        "cause": "24시간 메시징 창이 닫힌 뒤 발송 차례가 돌아왔습니다(큐 대기 중 만료 포함).",
        "action": "정책상 정상 실패입니다. 발송 적체가 크면 페이서/한도를 점검하세요.",
    },
    "failed_param": {
        "title": "파라미터 오류",
        "cause": "요청 파라미터가 유효하지 않습니다(Private Reply 7일 초과가 대부분).",
        "action": "원문 메시지(sample_error_message)에서 어떤 파라미터인지 확인하세요.",
    },
    "failed_no_trace": {
        "title": "도착 미확인",
        "cause": "접수는 됐으나 도착 흔적을 확인하지 못했습니다(수신자 설정·라우팅 충돌 등).",
        "action": "로그 상세에서 재검증(reverify)을 시도하고, 반복되면 고객 설정을 안내하세요.",
    },
    "failed": {
        "title": "실패 (legacy)",
        "cause": "세분화 이전 버전에서 기록된 실패입니다. 원인 코드가 남아 있지 않습니다.",
        "action": "원문 메시지로만 추적 가능합니다. 최근 발생분이면 코드 경로를 점검하세요.",
    },
    "failed_api": {
        "title": "API 실패 (legacy)",
        "cause": "세분화 이전 버전의 Meta API 실패입니다. 현재 코드 경로에서는 더 이상 생성되지 "
        "않습니다.",
        "action": "과거 데이터입니다. 최근 발생분이 있으면 백엔드에 알려주세요.",
    },
    "recovery_pending": {
        "title": "복구 진행 중",
        "cause": "숨겨진 요청/스팸함으로 간 DM 에 대해 '요청함 수락 후 재댓글' 안내 답글을 남기고 "
        "재댓글을 기다리는 상태입니다.",
        "action": "조치 불필요. 재댓글이 오면 자동 재발송되고, 기한이 지나면 만료됩니다.",
    },
    "recovery_expired": {
        "title": "복구 만료",
        "cause": "복구 안내 후 기한 내 재댓글이 오지 않아 종결됐습니다.",
        "action": "조치 불필요. 비율이 높으면 안내 문구를 점검하세요.",
    },
}

_EMPTY = {"title": "", "cause": "", "action": ""}

# sample_error_message 상한 (원문이 길 수 있어 자름)
SAMPLE_ERROR_MESSAGE_MAX = 500


def describe(code: str, subcode: str, status: str) -> dict:
    """(code, subcode, status) → ``{title, cause, action}``. 미등록이면 빈 문자열 3개.

    프론트는 title 이 있으면 그것을, 없으면 기존 로컬 사전으로 폴백한다.
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


def describe_for_log(code: str, subcode: str, status: str) -> dict:
    """로그 **상세/목록 행**용 평면 dict — ``describe()`` 결과에 ``error_`` 접두를 붙이고
    ``recoverable`` 을 함께 낸다 (DM-2).

    로그 상세는 error_code/error_message 등 다른 필드와 한 객체에 섞이므로 대시보드
    (`failure_breakdown`)의 title/cause/action 과 달리 접두를 둔다.
    """
    described = describe(code, subcode, status)
    return {
        "error_title": described["title"],
        "error_cause": described["cause"],
        "error_action": described["action"],
        "recoverable": is_recoverable(status, subcode),
    }


def truncate_message(message: str) -> str:
    """원문 오류 메시지를 상한까지 자른다 (초과 시 말줄임)."""
    text = (message or "").strip()
    if len(text) <= SAMPLE_ERROR_MESSAGE_MAX:
        return text
    return text[: SAMPLE_ERROR_MESSAGE_MAX - 1] + "…"
