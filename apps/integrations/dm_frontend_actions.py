"""DM 발송 결과별 프론트엔드 표시 가이드 (v4 — 유저 문구 재정의).

각 로그에 대해 프론트가 사용자에게 보여줄
- 사유 머신 키(`user_reason`) · 한국어 제목/이유/다음 행동
- 액션 타입 (reconnect / wait / info / checklist / success)
- 체크리스트 (도착 미확인의 자가 점검용)
- CTA 버튼 정의 · 심각도
를 단일 source of truth 로 제공한다.

──────────────────────────────────────────────────────────────────────────
v4 에서 바뀐 것 (2026-08-06, `../../docs/frontend/DM_USER_COPY_MAPPING.md`)
──────────────────────────────────────────────────────────────────────────
v3.2 문구는 어드민 오류 사전 정비 **이전**에 쓰여, 내부 용어("Private Reply", "파라미터
오류", "토큰")와 내부 처리 사정(검증 대기 35분)이 그대로 사용자에게 나가고 있었다.
프론트가 못 참고 런타임에 덮어쓰는 지경이었다(`DMResultModal.tsx:46`).

v4 는 문구 축을 **status → `user_reason`(사용자 관점 9종)** 으로 옮긴다:

- 같은 사유인데 status 가 갈려 서로 다른 문구가 나가던 문제가 사라진다.
  예) "수신자가 받을 수 없음"은 `failed_param`(100/2534014·2534001)과
      `failed_no_trace`(551)로 갈려 각각 빨간 오류/노란 체크리스트가 떴다.
- 문구는 §2 원칙을 따른다 — 상태 → 이유 → 다음 행동, 탓하지 않기, 단정하지 않기,
  **내부 처리 사정 비노출**.

**의도적으로 바꾸지 않은 것** (실서버 영향 최소화):
1. ``severity`` 는 예전처럼 **status 기준**으로 계산한다. 색을 바꾸면 목록 배지(status
   기준)와 모달 헤더(severity 기준)가 어긋난다 — 프론트가 배지까지 `user_reason` 으로
   그리게 된 뒤에 함께 옮긴다.
   └ 예외 1건: ``other``(미분류) 만 warning. `_SEVERITY_BY_USER_REASON` 참고.
2. **CTA 를 새로 추가하지 않는다.** 프론트 ``handleCtaClick`` 은
   ``reverify``/``retry``/``ig_reconnect`` 만 처리하므로, 모르는 action 을 내려보내면
   **눌러도 아무 일 없는 버튼**이 생긴다.
3. ``title``/``description`` 을 계속 채운다(하위호환). 프론트가 `user_reason` + 자체
   i18n 으로 이행한 뒤 제거한다.
"""

from __future__ import annotations

from .dm_user_reasons import (
    NO_REASON,
    S_CAMPAIGN_NOT_ACTIVE,
    S_CONNECTION_DISCONNECTED,
    S_DUPLICATE_CLEANUP,
    S_GHOST_CLEANUP,
    S_IG_ACCOUNT_INACTIVE,
    S_MESSAGING_WINDOW_SKIP,
    S_MONTHLY_DM_LIMIT,
    S_OTHER,
    S_OUTSIDE_SCHEDULE,
    S_SELF_RECIPIENT,
    U_ALREADY_REPLIED,
    U_CONNECTION_LOST,
    U_DELIVERY_UNCONFIRMED,
    U_HIDDEN_REQUEST,
    U_POST_RESTRICTED,
    U_RECIPIENT_UNAVAILABLE,
    U_SEND_DELAYED,
    U_SEND_INCOMPLETE,
    U_WINDOW_EXPIRED,
    user_reason_for,
)

HIDDEN_SPAM_SUBCODE = "2534025"

# 사용자에게 보여주는 자가 점검 체크리스트 (도착 미확인 전용).
#
# ⚠️ v3.2 의 ``default_routing_app`` 항목을 **삭제**했다 — "Facebook 페이지 설정 > 고급
#    메시지 설정 > 기본 라우팅 앱"을 안내했는데, 우리는 **Instagram Login** 방식이라
#    그 설정 화면 자체가 존재하지 않는다. `../../docs/frontend/DISCONNECT_OTHER_DM_TOOLS_GUIDE.md` 는
#    정반대로 "그 절차는 TurnFlow 에 해당하지 않는다"고 안내 중이었다(두 문서가 상충).
# ⚠️ ``message_access_allowed`` 항목도 뺐다(제품 결정) — 확인 경로가 길고 실제 원인인
#    경우가 드물어 첫 항목으로 두기에 부적절했다.
# 순서 = 확인하기 쉬운 것부터. 프론트는 배열 순서대로 번호를 매긴다.
SELF_CHECK_CHECKLIST: list[dict] = [
    {
        "id": "recipient_account",
        "title": "수신자 계정 상태",
        "description": ("수신자가 비공개 계정이거나 메시지 수신을 제한한 경우일 수 있어요."),
    },
    {
        "id": "ads_restriction",
        "title": "광고 게시물 설정",
        "description": ("광고 게시물이라면 광고 설정에 제한이 적용되어 있지 않은지 확인해 주세요."),
    },
    {
        "id": "other_dm_tool",
        "title": "다른 DM 자동화 서비스 연결",
        "description": (
            "다른 DM 자동화 서비스가 함께 연결되어 있지 않은지 확인해 주세요. "
            "같은 댓글에 두 서비스가 응답하면 한쪽만 발송돼요."
        ),
    },
]


# ── 사유별 문구 (제목 · 이유 · 다음 행동) ─────────────────────────────
# 구조·표현 규칙은 `../../docs/frontend/DM_USER_COPY_MAPPING.md` §2. 이 파일을 고칠 때는 그 문서도 함께 고칠 것.
def _copy(title: str, cause: str, next_step: str) -> dict:
    return {"title": title, "cause": cause, "next_step": next_step}


_USER_COPY: dict[str, dict] = {
    U_CONNECTION_LOST: _copy(
        "인스타그램 계정 연결이 해제되어 발송되지 않았어요",
        # ⚠️ "비밀번호를 변경하셨거나"를 뺐다(2026-08-07) — Meta 의 Instagram Platform
        #    문서는 토큰 무효화 사유를 **아예 다루지 않는다**(만료 기간만 명시).
        #    널리 인용되는 "비밀번호 변경" 문구는 Facebook Login 의 subcode 460
        #    설명이고, 우리는 Instagram Login 이라 경로가 다르다. 실제 로그에도
        #    subcode 460 은 0건이고 subcode 없는 code 190 뿐이다.
        #    → 근거 없는 사유를 적으면 사용자가 없는 원인을 찾게 된다(§2-4).
        "인스타그램 설정에서 연결이 해제되었거나, 계정 보안 정책에 따라 연결이 만료된 경우에 발생할 수 있어요. 연결이 해제된 동안에는 이 계정의 자동 "
        "DM 발송이 모두 중단돼요.",
        "다시 연결하시면 이후 작성되는 댓글부터 발송이 재개돼요. 아직 발송 가능 기간인 댓글 작성 후 7일 이내의 건은 별도 설정 없이 자동으로 다시 "
        "발송됩니다.",
    ),
    U_RECIPIENT_UNAVAILABLE: _copy(
        "수신자가 메시지를 받을 수 없는 상태였어요",
        "수신자 계정이 삭제·비활성화되었거나, 비공개로 전환되었거나, 메시지 수신을 제한한 경우에 발생할 수 있어요. 대화방을 삭제한 경우에도 동일하게 "
        "처리돼요. 이 수신자 한 분에게만 해당되며, 다른 분들에게는 정상적으로 발송돼요.",
        "",
    ),
    U_WINDOW_EXPIRED: _copy(
        "댓글을 삭제했거나 작성된 지 7일이 초과되었어요",
        "댓글이 삭제된 경우 자동 DM을 발송할 수 없어요. 또한 인스타그램은 댓글 작성 후 7일까지만 자동 DM 발송을 허용해서 7일이 지난 경우에도 "
        "동일하게 처리돼요.",
        "",
    ),
    U_HIDDEN_REQUEST: _copy(
        "수신자의 '숨겨진 요청 · 스팸함'으로 이동했을 수 있어요",
        "아직 팔로우하지 않은 분에게 보내는 첫 DM은 받은편지함 대신 '숨겨진 요청'이나 스팸함으로 분류될 수 있어요. 발송은 정상적으로 처리되었고, "
        "수신자가 아직 확인하지 않은 상태예요. 수신자가 요청을 수락하면 이후 DM은 받은편지함으로 도착해요.",
        "실패 DM 복구를 켜두시면 게시물에 안내 댓글을 남기고, 수신자가 다시 댓글을 남기면 자동으로 재발송해요.",
    ),
    U_POST_RESTRICTED: _copy(
        "이 게시물에는 자동 DM 발송이 제한되어 있어요",
        "인스타그램의 자동화 정책 또는 시스템 판단에 따라 이 게시물의 자동 DM 발송이 제한되었어요. 게시물마다 제한 여부가 다를 수 있어, 다른 "
        "게시물에서는 정상적으로 발송될 수 있어요.",
        "다른 게시물로 캠페인을 만드시면 정상적으로 발송돼요.",
    ),
    U_ALREADY_REPLIED: _copy(
        "이 댓글에는 이미 답장이 발송되어 있어요",
        "인스타그램은 댓글 하나당 자동 DM 발송을 한 번만 허용해요. 해당 댓글 작성자에게 이미 직접 DM을 보내셨거나, 다른 DM 자동화 서비스가 "
        "함께 연결되어 있는 경우에 이렇게 처리될 수 있어요.",
        "다른 DM 자동화 서비스를 함께 사용 중이시라면, 사용하지 않는 서비스의 연결을 해제해 중복 발송을 줄일 수 있어요.",
    ),
    U_DELIVERY_UNCONFIRMED: _copy(
        "발송은 되었으나 도착이 확인되지 않았어요",
        "인스타그램에서 도착 확인 정보가 전달되지 않았어요. 계정의 설정 상태에 따라 발생할 수 있어요. 실제로는 전달되었을 수도 있으며, 도착 여부만 "
        "확인되지 않은 상태예요.",
        "아래 항목을 확인해 보시면 도움이 될 수 있어요.",
    ),
    U_SEND_DELAYED: _copy(
        "인스타그램 요청 제한으로 발송이 지연되고 있어요",
        "인스타그램은 짧은 시간에 요청이 많아지면 발송 속도를 일시적으로 조절해요. 발송 실패가 아니라 대기 상태이며, 제한이 해제되면 순서대로 "
        "발송돼요.",
        "",
    ),
    U_SEND_INCOMPLETE: _copy(
        "발송이 완료되지 않았어요",
        "인스타그램 서버 오류로 발송이 완료되지 않았어요.",
        "같은 캠페인에서 반복해서 발생한다면 문의해 주세요. 확인해 드릴게요.",
    ),
    # ── 건너뜀 10종 ──
    S_MONTHLY_DM_LIMIT: _copy(
        "이번 달 DM 발송 한도를 모두 사용했어요",
        "현재 플랜의 월 발송 한도에 도달해 이 건은 발송되지 않았어요. 한도는 매월 1일에 초기화돼요.",
        "플랜을 업그레이드하시면 중단된 발송을 바로 이어갈 수 있어요. 댓글 작성 후 7일 이내인 건은 업그레이드 즉시 자동으로 다시 발송됩니다.",
    ),
    S_CAMPAIGN_NOT_ACTIVE: _copy(
        "캠페인이 꺼져 있어 발송되지 않았어요",
        "댓글이 접수된 시점에 캠페인이 일시정지 상태였어요.",
        "캠페인을 켜시면 이후 작성되는 댓글부터 발송돼요.",
    ),
    S_OUTSIDE_SCHEDULE: _copy(
        "예약된 발송 시간대가 아니어서 발송되지 않았어요",
        "댓글이 접수된 시각이 캠페인에 설정하신 발송 시간대 밖이었어요.",
        "발송 시간대는 캠페인 설정에서 변경할 수 있어요.",
    ),
    S_IG_ACCOUNT_INACTIVE: _copy(
        "이 인스타그램 계정이 비활성 상태여서 발송되지 않았어요",
        "플랜에서 사용할 계정으로 선택되어 있지 않은 상태예요. 연결과 데이터는 그대로 보관돼요.",
        "사용할 계정으로 선택하시면 이후 발송이 재개돼요.",
    ),
    S_SELF_RECIPIENT: _copy(
        "계정 소유자 본인의 댓글이라 발송되지 않았어요",
        "자동 DM은 본인 계정에는 발송되지 않아요.",
        "정상 동작이라 조치하실 일은 없어요.",
    ),
    S_CONNECTION_DISCONNECTED: _copy(
        "인스타그램 연결이 해제되어 정리된 건이에요",
        "연결이 해제된 시점에 대기 중이던 발송 건이 함께 정리되었어요.",
        "다시 연결하시면 이후 작성되는 댓글부터 발송돼요.",
    ),
    S_DUPLICATE_CLEANUP: _copy(
        "중복 발송을 방지했어요",
        "같은 게시물에 캠페인이 중복되어 있어, 같은 분께 두 번 발송되지 않도록 처리했어요.",
        "",
    ),
    S_GHOST_CLEANUP: _copy(
        "중복 발송을 방지했어요",
        "이미 답장이 발송된 건이라, 같은 분께 두 번 발송되지 않도록 처리했어요.",
        "",
    ),
    S_MESSAGING_WINDOW_SKIP: _copy(
        "발송 가능 시간이 지나 발송되지 않았어요",
        "인스타그램이 허용하는 발송 가능 시간이 지난 뒤에 발송 순서가 되어, 발송을 시작하지 않았어요.",
        "",
    ),
    # ⚠️ `other` 는 **사유가 아니라 미분류**다 — 건너뜀 원문이 위 needle 9종 어디에도
    #    안 걸렸다는 뜻. 우리도 원인을 모르는 상태라 "설정/계정 상태 때문"처럼 원인을
    #    지목하면 안 된다(§2-4: 근거 없는 원인 금지). 문의로 유도하는 게 정직하다.
    #    status 는 skipped 지만 그룹은 attention("전송 실패") 이라 문구도 실패 화법으로 맞춘다.
    S_OTHER: _copy(
        "발송 중 문제가 발생했어요",
        "일시적인 오류로 메시지를 정상적으로 발송하지 못했어요.",
        "계속 같은 문제가 발생하면 문의해 주세요.",
    ),
}

# 복구 진행 상태별 '다음 행동' — U4 는 복구 여부에 따라 안내가 갈린다.
_RECOVERY_NEXT = {
    "recovery_pending": (
        "안내 댓글을 남겨두었어요. 수신자가 다시 댓글을 남기면 자동으로 재발송돼요."
    ),
    "recovery_expired": ("복구 가능 기간 내에 수신자의 새 댓글이 없어 자동 재발송이 종료되었어요."),
}

# ── 성공/진행 중 상태 (사유 없음) ────────────────────────────────────
_NON_ERROR_COPY: dict[str, dict] = {
    "delivered": _copy("수신자에게 전달됨", "메시지가 수신자에게 전달되었어요.", ""),
    "read": _copy("수신자가 읽었어요", "수신자가 메시지를 확인했어요.", ""),
    "sent": _copy("발송 완료", "발송이 완료되었어요.", ""),  # legacy
    "recovery_delivered": _copy(
        "재발송이 완료되었어요",
        "첫 발송이 전달되지 않았지만, 수신자가 안내를 보고 다시 댓글을 남겨 재발송이 "
        "완료되었어요.",
        "",
    ),
    "accepted": _copy(
        "발송 요청이 접수되었어요",
        "인스타그램이 발송 요청을 접수했어요. 도착 여부는 잠시 후 자동으로 확인돼요.",
        "",
    ),
    "queued": _copy("발송을 준비하고 있어요", "발송 순서를 기다리고 있어요.", ""),
    "submitting": _copy("발송 중이에요", "인스타그램에 발송을 요청하고 있어요.", ""),
    "pending": _copy("발송을 준비하고 있어요", "발송 순서를 기다리고 있어요.", ""),
}

# ── 심각도 (⚠️ v3.2 그대로 — status 기준) ────────────────────────────
# 색을 사유 기준으로 옮기면 목록 배지(status 기준)와 모달 헤더가 어긋난다.
# 프론트가 배지까지 user_reason 으로 그리게 된 뒤 함께 옮길 것.
_SEVERITY_BY_STATUS: dict[str, str] = {
    "delivered": "success",
    "read": "success",
    "sent": "success",
    "recovery_delivered": "success",
    "accepted": "info",
    "queued": "info",
    "submitting": "info",
    "pending": "info",
    "rate_limited": "warning",
    "failed_token": "error",
    "failed_param": "error",
    "failed_window": "error",
    "failed_no_trace": "warning",
    "skipped": "info",
    "recovery_pending": "warning",
    "recovery_expired": "info",
    "failed": "warning",
    "failed_api": "warning",
}

_SUCCESS_STATUSES = ("delivered", "read", "sent", "recovery_delivered")
_WAIT_STATUSES = ("accepted", "queued", "submitting", "pending", "rate_limited")


# 사유 기준 예외 — status 기준값을 덮어쓴다.
# 원칙(위 주석)은 "status 기준 유지"지만, `other` 는 **우리가 원인을 모르는 건**이라
# 파란 info 로 두면 "정상 처리"처럼 읽힌다. 사용자가 문의해야 하는 유일한 건너뜀이라
# 예외로 warning(주황·경고 아이콘)을 준다.
# ※ 목록 배지는 status(skipped=회색) 기준이라 헤더만 주황이 된다 — 실데이터 0건이라
#   영향 범위가 사실상 없고, 프론트가 배지를 user_reason 으로 옮기면(UC-6) 해소된다.
_SEVERITY_BY_USER_REASON: dict[str, str] = {
    S_OTHER: "warning",
}


def _severity_for(status: str, error_subcode: str, user_reason: str = "") -> str:
    """심각도 — v3.2 규칙 유지. failed_param@2534025 와 `other` 만 warning 으로 분기."""
    if user_reason in _SEVERITY_BY_USER_REASON:
        return _SEVERITY_BY_USER_REASON[user_reason]
    if status == "failed_param" and str(error_subcode or "").strip() == HIDDEN_SPAM_SUBCODE:
        return "warning"
    return _SEVERITY_BY_STATUS.get(status, "info")


def _action_type(status: str, user_reason: str) -> str:
    if status in _SUCCESS_STATUSES:
        return "success"
    if status in _WAIT_STATUSES:
        return "wait"
    if user_reason == U_DELIVERY_UNCONFIRMED:
        return "checklist"
    if user_reason == U_CONNECTION_LOST:
        return "reconnect"
    return "info"


def _cta_for(status: str, user_reason: str) -> dict | None:
    """CTA — **기존 3종만** 사용한다.

    프론트 ``handleCtaClick`` 이 reverify/retry/ig_reconnect 만 처리하므로 새 action 을
    내려보내면 눌러도 아무 일 없는 버튼이 된다. U5(새 게시물)·U6(다른 툴 해제) 의 CTA 는
    프론트가 핸들러를 붙인 뒤에 추가할 것.
    """
    if user_reason == U_CONNECTION_LOST:
        return {"label": "인스타그램 다시 연결하기", "action": "ig_reconnect"}
    if user_reason == U_DELIVERY_UNCONFIRMED:
        return {"label": "도착 여부 다시 확인하기", "action": "reverify"}
    # 숨겨진 요청·스팸함 — 복구가 아직 안 걸린 건에 한해 복구 켜기 안내.
    # (기존 v3.2 와 동일한 action. 프론트에 핸들러가 없어 현재는 표시 전용이다.)
    if user_reason == U_HIDDEN_REQUEST and status == "failed_param":
        return {"label": "실패 DM 복구 켜기", "action": "enable_recovery"}
    return None


def build_frontend_action(
    status: str,
    error_subcode: str = "",
    error_code: str = "",
    error_message: str = "",
) -> dict:
    """로그 1건 → 프론트 표시 가이드.

    Args:
        status: ``SentDMLog.status``
        error_subcode: ``SentDMLog.error_subcode`` (사유 판정 1순위 키)
        error_code: ``SentDMLog.error_code`` — **v4 신규**. 없으면 status 만으로 폴백하므로
            같은 status 안의 갈래(예: 100/2534014 수신자 없음 vs 100 일반)를 구분하지 못한다.
        error_message: 건너뜀(skipped) 사유 판정용 원문.

    Returns:
        {
          "type":        "success" | "wait" | "reconnect" | "info" | "checklist",
          "user_reason": "connection_lost" | ... | "" (성공·진행 중),
          "title":       "...",
          "cause":       "...",          # v4 신규 — 발생 이유
          "next_step":   "...",          # v4 신규 — 다음 행동 ("" 면 안내 없음)
          "description": "...",          # 하위호환 — cause + next_step
          "checklist":   [...] | None,
          "cta":         {"label","action"} | None,
          "severity":    "info" | "warning" | "error" | "success",
        }
    """
    status = str(status or "").strip()
    user_reason = user_reason_for(status, error_code, error_subcode, error_message)

    if user_reason == NO_REASON:
        copy = _NON_ERROR_COPY.get(status)
        if copy is None:  # 미지의 status — 빈 껍데기로 안전하게 폴백
            copy = _copy(status, "", "")
    else:
        copy = _USER_COPY.get(user_reason) or _copy(status, "", "")

    next_step = copy["next_step"]
    # U4 는 복구 진행 상태에 따라 다음 행동이 갈린다.
    if user_reason == U_HIDDEN_REQUEST and status in _RECOVERY_NEXT:
        next_step = _RECOVERY_NEXT[status]

    description = " ".join(part for part in (copy["cause"], next_step) if part)

    return {
        "type": _action_type(status, user_reason),
        "user_reason": user_reason,
        "title": copy["title"],
        "cause": copy["cause"],
        "next_step": next_step,
        "description": description,
        "checklist": (SELF_CHECK_CHECKLIST if user_reason == U_DELIVERY_UNCONFIRMED else None),
        "cta": _cta_for(status, user_reason),
        "severity": _severity_for(status, error_subcode, user_reason),
    }
