"""
DM 발송 결과별 프론트엔드 액션 가이드 (v3.2).

각 SentDMLog.status 에 대해 프론트가 사용자에게 보여줄
- 한국어 표시명
- 액션 타입 (reconnect / wait / info / checklist / success)
- 안내 문구
- 체크리스트 항목 (FAILED_NO_TRACE 의 자가 점검용)
- CTA 버튼 정의
를 단일 source of truth 로 제공한다.
"""

from __future__ import annotations

# 사용자에게 항상 보여주는 자가 점검 체크리스트
# (FAILED_NO_TRACE 또는 그에 준하는 상태에서 노출)
SELF_CHECK_CHECKLIST: list[dict] = [
    {
        "id": "message_access_allowed",
        "title": "메시지 액세스 허용 여부",
        "description": (
            "Instagram 앱 > 설정 > 메시지 및 스토리 답장 > 메시지 관리 에서 "
            "'메시지 액세스 허용'이 ON으로 되어 있는지 확인해주세요."
        ),
    },
    {
        "id": "default_routing_app",
        "title": "기본 대화 라우팅 앱 설정",
        "description": (
            "Facebook 페이지 설정 > 고급 메시지 설정 에서 "
            "'기본 라우팅 앱'이 본 서비스로 선택되어 있는지 확인해주세요."
        ),
    },
    {
        "id": "restricted_content",
        "title": "제한된 컨텐츠 여부",
        "description": (
            "광고 부스팅 게시물 키워드가 미설정이거나, 연령/민감 콘텐츠 제한이 "
            "걸려 있을 경우 메시지 발송이 제한될 수 있습니다."
        ),
    },
    {
        "id": "recipient_account",
        "title": "수신자 계정 문제",
        "description": (
            "수신자가 비공개 계정이거나 메시지 요청을 거부한 경우, "
            "또는 본 서비스의 메시지를 차단한 경우 발송이 제한될 수 있습니다."
        ),
    },
]


def build_frontend_action(status: str, error_subcode: str = "") -> dict:
    """
    SentDMLog.status(+error_subcode) 에 대응하는 프론트엔드 표시 액션을 반환.

    error_subcode 는 failed_param 을 세분화하기 위해 받는다:
    2534025(비팔로워 채널 미개설 → 숨겨진 요청/스팸함) 는 일반 파라미터 오류와 달리
    '숨겨진 요청 · 스팸' 안내로 분기한다(복구 미사용/미보유로 종결된 케이스).

    Returns:
        {
            "type":       "success" | "wait" | "reconnect" | "info" | "checklist",
            "title":      "...",
            "description":"...",
            "checklist":  [...] | None,
            "cta":        {"label": "...", "action": "..."} | None,
            "severity":   "info" | "warning" | "error" | "success"
        }
    """
    if status in ("delivered", "read", "sent"):  # sent: legacy
        return {
            "type": "success",
            "title": "수신자에게 전달됨",
            "description": (
                "Meta 메시징 파이프라인에 메시지가 전달되었습니다."
                if status == "delivered"
                else "수신자가 읽었습니다." if status == "read" else "발송 완료."
            ),
            "checklist": None,
            "cta": None,
            "severity": "success",
        }

    if status == "accepted":
        return {
            "type": "wait",
            "title": "Meta 접수됨 (도착 확인 중)",
            "description": (
                "Meta가 발송 요청을 수락했습니다. " "최대 35분 내 자동으로 도착 여부를 검증합니다."
            ),
            "checklist": None,
            "cta": None,
            "severity": "info",
        }

    if status in ("queued", "submitting", "pending"):
        return {
            "type": "wait",
            "title": "발송 처리 중",
            "description": "Meta 응답을 기다리는 중입니다.",
            "checklist": None,
            "cta": None,
            "severity": "info",
        }

    if status == "rate_limited":
        return {
            "type": "wait",
            "title": "Meta 응답 대기 중 (지연)",
            "description": (
                "Meta 측 일시적 레이트 리밋으로 발송이 지연되고 있습니다. "
                "서버가 자동으로 재시도합니다."
            ),
            "checklist": None,
            "cta": None,
            "severity": "warning",
        }

    if status == "failed_token":
        return {
            "type": "reconnect",
            "title": "Instagram 재연동 필요 (토큰 만료)",
            "description": (
                "액세스 토큰이 만료되었거나 권한이 회수되었습니다. "
                "Instagram 계정을 재연동해주세요."
            ),
            "checklist": None,
            "cta": {
                "label": "재연동하기",
                "action": "ig_reconnect",
            },
            "severity": "error",
        }

    if status == "failed_param":
        # 2534025 = 비팔로워 채널 미개설 → 첫 DM 이 상대의 숨겨진 요청/스팸함으로.
        # 복구가 켜져 있었으면 recovery_pending 로 갔을 것 — 여기 오는 건 복구 OFF/미보유.
        if str(error_subcode or "").strip() == "2534025":
            return {
                "type": "info",
                "title": "숨겨진 요청 · 스팸함으로 전달됨",
                "description": (
                    "수신자가 아직 팔로워가 아니라 DM 채널이 열려 있지 않아, 첫 DM이 상대의 "
                    "'숨겨진 요청/스팸함'으로 들어갔습니다(정상 도착 미확인). 실패 DM 복구를 켜면 "
                    "댓글에 '요청함에서 수락 후 다시 댓글 달아주세요' 안내를 남겨 재발송을 "
                    "시도합니다(프로 전용)."
                ),
                "checklist": None,
                "cta": {"label": "실패 DM 복구 켜기", "action": "enable_recovery"},
                "severity": "warning",
            }
        return {
            "type": "info",
            "title": "메시지 발송 불가 (파라미터 오류)",
            "description": (
                "댓글이 작성된 지 7일이 초과되었거나 ID가 유효하지 않습니다. "
                "Private Reply는 댓글 작성 후 7일 이내에만 가능합니다."
            ),
            "checklist": None,
            "cta": None,
            "severity": "error",
        }

    if status == "failed_window":
        return {
            "type": "info",
            "title": "메시징 윈도우 24시간 만료",
            "description": (
                "수신자와의 마지막 상호작용으로부터 24시간이 경과했습니다. "
                "수신자가 다시 메시지를 보내야 발송 가능합니다."
            ),
            "checklist": None,
            "cta": None,
            "severity": "error",
        }

    if status == "failed_no_trace":
        return {
            "type": "checklist",
            "title": "도착 미확인 — 다음 설정을 확인해주세요",
            "description": (
                "Meta가 발송 요청은 수락했으나 35분 내 도착을 확인할 수 없었습니다. "
                "보통 다음 중 하나가 원인입니다."
            ),
            "checklist": SELF_CHECK_CHECKLIST,
            "cta": {
                "label": "재검증 시도",
                "action": "reverify",
            },
            "severity": "warning",
        }

    if status == "skipped":
        return {
            "type": "info",
            "title": "건너뜀",
            "description": "발송 제한(시간당 한도) 또는 정책에 의해 건너뛰어진 건입니다.",
            "checklist": None,
            "cta": None,
            "severity": "info",
        }

    if status == "recovery_pending":
        return {
            "type": "wait",
            "title": "DM 복구 대기 중 (요청함 수락·재댓글 대기)",
            "description": (
                "첫 DM(비공개 답글)이 전달되지 못했습니다(비팔로워 채널 미개설 — DM 은 대부분 "
                "상대의 숨겨진 요청/스팸함에 있음). 댓글에 '요청함에서 수락 후 다시 댓글 "
                "달아주세요' 안내를 게시했습니다. 사용자가 다시 댓글을 달면 자동 재발송되고 "
                "성공 시 이 건은 복구 성공으로 바뀝니다. 아직 완전한 실패가 아닙니다."
            ),
            "checklist": None,
            "cta": None,
            "severity": "warning",
        }

    if status == "recovery_delivered":
        return {
            "type": "success",
            "title": "복구 성공 (재댓글 발송 도착)",
            "description": (
                "첫 발송은 실패했지만, 사용자가 안내를 보고 다시 댓글을 달아 재발송이 도착했습니다."
            ),
            "checklist": None,
            "cta": None,
            "severity": "success",
        }

    if status == "recovery_expired":
        return {
            "type": "info",
            "title": "복구 대기 만료 (수락·재댓글 없음)",
            "description": (
                "안내 답글 게시 후 유효기간 내에 사용자의 재댓글 발송 성공이 없어 만료되었습니다. "
                "계정주가 조치할 항목은 없습니다."
            ),
            "checklist": None,
            "cta": None,
            "severity": "info",
        }

    # legacy / unknown
    if status in ("failed", "failed_api"):
        return {
            "type": "checklist",
            "title": "발송 실패 — 다음 설정을 확인해주세요",
            "description": "원인을 확정하기 어려운 실패입니다. 자가 점검 항목을 확인해주세요.",
            "checklist": SELF_CHECK_CHECKLIST,
            "cta": {"label": "재검증 시도", "action": "reverify"},
            "severity": "warning",
        }

    return {
        "type": "info",
        "title": status,
        "description": "",
        "checklist": None,
        "cta": None,
        "severity": "info",
    }
