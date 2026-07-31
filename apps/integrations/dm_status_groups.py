"""DM 로그 상태 그룹 — 유저 콘솔 표시/필터의 단일 소스 (v4.5).

세분화된 `SentDMLog.status` 를 유저가 실제로 보는 **코스 그룹 5종**으로 접는다.
목록 탭 필터·상태 배지·수신자 롤업·통계 카드가 모두 이 매핑을 공유해, 프론트가
클라이언트에서 상태를 다시 쪼갤 필요가 없게 한다(백엔드가 정렬/필터를 소유).

그룹 (한 로그는 정확히 1개 그룹에 속함):
  waiting      대기중            우리 큐에서 발송 차례 대기/발송 중/재시도 (아직 Meta 미접수).
                                (구 프론트 명칭 "순차발송" → "대기중" 으로 통일)
  sent         전송됨            Meta 접수·도착·복구 재전송 성공 (읽음은 아래 read 로 분리).
  read         읽음              수신자가 읽음.
  hidden_spam  숨겨진 요청 · 스팸  비팔로워라 채널이 안 열려(code=100/subcode=2534025) 첫 DM 이
                                상대의 '숨겨진 요청/스팸함'으로 간 경우. 실패 DM 복구의 대상 사유.
                                복구 ON → recovery_pending/recovery_expired,
                                복구 OFF/미보유 → failed_param@2534025 로 종결.
  attention    확인 필요          그 외 사용자 조치가 필요한 실패 (토큰 만료·24h 윈도우·기타
                                파라미터 오류·도착 미확인·건너뜀·legacy 실패).

주의: `failed_param` 은 subcode 로 갈린다 — 2534025(숨김함) 이면 hidden_spam, 그 외
(댓글 7일 초과·잘못된 ID 등) 는 attention. 그래서 매핑 함수는 status 뿐 아니라
error_subcode 도 받는다.
"""

from __future__ import annotations

from django.db.models import Q

# ── 그룹 머신값 ────────────────────────────────────────────────────────────
WAITING = "waiting"
SENT = "sent"
READ = "read"
HIDDEN_SPAM = "hidden_spam"
ATTENTION = "attention"

GROUP_ORDER = [WAITING, SENT, READ, HIDDEN_SPAM, ATTENTION]
VALID_GROUPS = set(GROUP_ORDER)

# ── 그룹 표시명 (한국어, 유저 콘솔 배지/탭 라벨) ─────────────────────────────
GROUP_DISPLAY = {
    WAITING: "대기중",
    SENT: "전송됨",
    READ: "읽음",
    HIDDEN_SPAM: "숨겨진 요청 · 스팸",
    ATTENTION: "확인 필요",
}

# 비팔로워 채널 미개설 → 첫 DM 이 상대 숨겨진 요청/스팸함으로 (복구 기능 대상 사유).
HIDDEN_SPAM_SUBCODE = "2534025"

# ── 내부 표식 subcode (Meta 가 준 값이 아니라 **우리가 붙이는 값**) ──────────
# 메시징 창 만료는 원인이 두 가지고 조치가 정반대인데, 둘 다 status=failed_window +
# error_code="" 로 같아서 화면에서 구분할 수 없었다. 종결 시점에만 알 수 있는 정보
# (예약 재시도 시각을 넘겼는가)를 subcode 자리에 남겨 사후 분류를 가능하게 한다.
#   - window_stalled: 예약된 재시도 시각을 한참 넘기도록 방치됨 → 발송 파이프라인 문제
#   - window_peak:    페이서 대기열이 길어진 사이 만료 → 정상(피크)
# Meta subcode 는 항상 숫자라 값 공간이 겹치지 않는다. 내부 가드가 종결하는 건은
# subcode 가 원래 비어 있어 덮어쓸 값도 없다.
WINDOW_STALLED_SUBCODE = "window_stalled"
WINDOW_PEAK_SUBCODE = "window_peak"

# hidden_spam 으로 접히는 상태들 (recovery 는 항상, failed_param 은 2534025 subcode 일 때만).
HIDDEN_SPAM_STATUSES = ["recovery_pending", "recovery_expired"]

# status → group (failed_param 의 2534025 예외는 status_group() 함수가 분기 처리).
_STATUS_TO_GROUP = {
    # 대기중 (아직 Meta 미접수)
    "queued": WAITING,
    "submitting": WAITING,
    "rate_limited": WAITING,
    "pending": WAITING,  # legacy
    # 전송됨 (Meta 접수 이상)
    "accepted": SENT,
    "delivered": SENT,
    "sent": SENT,  # legacy
    "recovery_delivered": SENT,  # 복구 재전송 성공
    # 읽음
    "read": READ,
    # 숨겨진 요청 · 스팸
    "recovery_pending": HIDDEN_SPAM,
    "recovery_expired": HIDDEN_SPAM,
    # 확인 필요 (failed_param 은 subcode=2534025 면 hidden_spam 으로 재분류됨)
    "failed_token": ATTENTION,
    "failed_window": ATTENTION,
    "failed_param": ATTENTION,
    "failed_no_trace": ATTENTION,
    "failed": ATTENTION,  # legacy
    "failed_api": ATTENTION,  # legacy
    "skipped": ATTENTION,
}


def status_group(status: str, error_subcode: str = "") -> str:
    """개별 status(+subcode) → 코스 그룹 머신값.

    failed_param + subcode=2534025 는 '숨김함'이므로 hidden_spam 으로 분류한다.
    미지의 status 는 보수적으로 attention.
    """
    if status == "failed_param" and str(error_subcode or "").strip() == HIDDEN_SPAM_SUBCODE:
        return HIDDEN_SPAM
    return _STATUS_TO_GROUP.get(status, ATTENTION)


def status_group_display(status: str, error_subcode: str = "") -> str:
    """개별 status(+subcode) → 그룹 표시명(한국어)."""
    return GROUP_DISPLAY[status_group(status, error_subcode)]


def _statuses_for(group: str) -> list[str]:
    return [s for s, g in _STATUS_TO_GROUP.items() if g == group]


def status_group_q(group: str, prefix: str = "") -> Q:
    """그룹 → SentDMLog 필터 Q (목록 status_group= 쿼리·통계 집계 공용).

    - hidden_spam: recovery_pending/expired + failed_param@2534025
    - attention:   나머지 실패군에서 2534025 failed_param 은 제외(hidden_spam 으로 빠짐)
    - 그 외:       단순 status__in

    ``prefix`` 를 주면 역참조 경로로 필드명을 감싼다 (예: ``prefix="dm_logs"`` →
    ``dm_logs__status``). 캠페인 목록의 사람 단위 annotate 처럼 SentDMLog 를 조인해서
    거를 때 쓴다 — 판정 규칙을 복제하지 않기 위한 것이므로 직접 하드코딩하지 말 것.
    """
    p = f"{prefix}__" if prefix else ""
    if group == HIDDEN_SPAM:
        return Q(**{f"{p}status__in": HIDDEN_SPAM_STATUSES}) | Q(
            **{f"{p}status": "failed_param", f"{p}error_subcode": HIDDEN_SPAM_SUBCODE}
        )
    if group == ATTENTION:
        base = [s for s in _statuses_for(ATTENTION) if s != "failed_param"]
        return Q(**{f"{p}status__in": base}) | (
            Q(**{f"{p}status": "failed_param"}) & ~Q(**{f"{p}error_subcode": HIDDEN_SPAM_SUBCODE})
        )
    return Q(**{f"{p}status__in": _statuses_for(group)})
