"""
Auto DM 캠페인 사용자 안내 문구 (단일 source of truth).

프론트엔드가 캠페인 생성/수정 폼의 라디오/토글 옆에 노출할 설명, 툴팁,
주의사항을 여기서 한 번에 정의한다. 백엔드 검증 로직과 사용자 노출 텍스트가
서로 어긋나지 않도록 분리.
"""

from __future__ import annotations

# 트리거 타입별 안내 (캠페인 생성 폼의 첫 라디오 그룹)
TRIGGER_TYPE_GUIDE: list[dict] = [
    {
        "value": "specific_media",
        "label": "특정 게시물에 댓글",
        "description": ("선택한 한 개의 게시물에 달린 댓글에 대해서만 자동 DM을 발송합니다."),
        "requires": ["media_id"],
        "tier": "free",
    },
    {
        "value": "any_media",
        "label": "모든 게시물에 댓글",
        "description": (
            "연결된 Instagram 계정의 모든 피드 게시물·릴스에 달린 댓글에 대해 "
            "자동 DM을 발송합니다."
        ),
        "requires": [],
        "tier": "pro",
    },
    {
        "value": "next_media",
        "label": "다음(새) 게시물에 댓글",
        "description": (
            "캠페인 활성화 후 새로 올리는 게시물 1개에 자동으로 적용됩니다. "
            "첫 댓글이 도착하는 순간 즉시 attach 되며, 이후엔 'specific_media' "
            "로 자동 전환됩니다. 다음 게시물에도 적용하려면 새 캠페인을 만들어주세요."
        ),
        "requires": [],
        "tier": "pro",
        "notes": [
            "v3.6 부터: 첫 댓글 도착 즉시 attach (이전 5분 지연 제거).",
            "캠페인 생성 이후 작성된 게시물만 적용 (과거 게시물의 첫 댓글은 무시).",
            (
                "동일 IG 계정에 next 캠페인이 여러 개 있으면 "
                "모두 같은 신규 게시물에 한 번에 적용됩니다."
            ),
        ],
    },
    {
        "value": "story_reply",
        "label": "특정 Story 답장",
        "description": (
            "선택한 Story 에 사용자가 메시지(답장)를 보내면 자동 DM을 발송합니다. "
            "Story 는 24시간만 활성 상태이므로 캠페인은 그 동안만 트리거됩니다."
        ),
        "requires": ["media_id"],
        "tier": "pro",
        "notes": [
            "GET /instagram/workspaces/{id}/stories/ 로 현재 활성 Story 목록 조회.",
            "Story 는 댓글이 아닌 'DM 답장' 으로 받습니다 (messages webhook).",
            "공개 답글(public_reply) 은 사용 불가합니다 (Story 에 댓글 기능 없음).",
            "24시간 후 Story 만료되면 더 이상 트리거되지 않습니다.",
            "발송은 24h 메시징 윈도우 안에서만 가능 (사용자가 답장한 직후).",
        ],
    },
]


KEYWORD_MODE_GUIDE: list[dict] = [
    {
        "value": "any",
        "label": "키워드 중 하나라도 포함",
        "description": "댓글에 등록된 키워드 중 하나라도 들어 있으면 매칭됩니다.",
    },
    {
        "value": "all",
        "label": "모든 키워드 포함",
        "description": "댓글에 등록된 모든 키워드가 들어 있어야 매칭됩니다.",
    },
    {
        "value": "exact",
        "label": "댓글 전체 일치",
        "description": "댓글이 등록된 키워드 중 하나와 완전히 일치해야 합니다.",
    },
]


# Follow-gate / Button-gate 안내 (v4.0 — 버튼 클릭 → reward, 팔로우 검증은 선택)
FOLLOW_GATE_GUIDE: dict = {
    "headline": "버튼 게이트 (버튼 클릭 시 reward DM 발송)",
    "description": (
        "opening DM 에 버튼을 첨부하고, 사용자가 버튼을 클릭하면 reward_message_template "
        "(본 DM)을 발송합니다. 버튼 클릭 시 '팔로우 여부 검증'을 할지(follow 모드) "
        "검증 없이 바로 보낼지(button-only 모드)는 gate_verify_follow 로 선택합니다."
    ),
    "modes": {
        "follow": (
            "follow_gate_enabled=true + gate_verify_follow=true (기본): "
            "버튼 클릭 시 IG Profile API(is_user_follow_business)로 팔로우 여부를 검증한 뒤, "
            "팔로우한 경우에만 reward 를 발송합니다. 미팔로우면 follow_gate_retry_message "
            "(재안내)를 같은 버튼과 함께 다시 보냅니다."
        ),
        "button": (
            "follow_gate_enabled=true + gate_verify_follow=false: "
            "팔로우 확인 없이 버튼을 누르기만 하면 즉시 reward 를 발송합니다. "
            "follow_gate_retry_message 는 사용되지 않습니다."
        ),
        "off": "follow_gate_enabled=false: 게이트 미사용 (opening DM 만 발송).",
    },
    "items": [
        "두 모드 모두 reward_message_template (버튼 클릭 후 보낼 본 DM)이 필수입니다.",
        "button-only 모드에선 follow_gate_button_label / follow_gate_prompt 를 용도에 맞게 "
        "직접 지정하세요. 비우면 follow 용 기본 문구('팔로우했어요' 등)가 노출됩니다.",
        "gate_trigger_keywords 는 postback 미수신 구버전 클라이언트 fallback 입니다 "
        "(이 키워드로 답장해도 reward 발송).",
        "reward 발송은 사용자가 버튼/답장한 직후의 24시간 메시징 윈도우 안에서 이뤄집니다.",
    ],
    "fields": {
        "follow_gate_enabled": "게이트 사용 여부 (true 시 버튼 첨부)",
        "gate_verify_follow": "true=팔로우 검증 후 발송 / false=즉시 발송 (button-only)",
        "follow_gate_prompt": "opening DM 본문(버튼 안내 문구). 비우면 기본 문구.",
        "follow_gate_button_label": "버튼 라벨 (Meta 한도 20자)",
        "follow_gate_retry_message": "미팔로우 시 재안내 (follow 모드 전용)",
        "reward_message_template": "버튼 클릭 후 보낼 본 DM (필수)",
    },
}


# 공개 답글 안내 (v3.5 — 봇 검사 회피 다중 템플릿 + 배치 쿨다운)
PUBLIC_REPLY_GUIDE: dict = {
    "headline": "공개 답글 함께 게시",
    "description": (
        "DM 발송 직후 댓글에도 답글을 게시합니다. "
        "여러 템플릿을 등록해두면 매 댓글마다 무작위로 골라 답글이 달립니다 "
        "(Instagram 봇 검사 회피)."
    ),
    "items": [
        (
            "**다양한 문구 3개 이상**을 등록해주세요. "
            "같은 답글이 반복되면 Instagram이 봇으로 탐지할 수 있습니다."
        ),
        "예: ['DM 드렸어요!', '확인 부탁드려요 :)', '안내 보내드렸습니다 🎁']",
        (
            "같은 IG 계정에서 단시간에 많은 답글이 게시되지 않도록 "
            "자동으로 배치 쿨다운이 적용됩니다 (기본: 10건마다 5분 대기)."
        ),
        "각 답글 사이에 5~15초 무작위 지터가 적용되어 사람처럼 동작합니다.",
        "댓글 작성 후 7일 이내에만 게시 가능합니다 (Meta 정책).",
        "답글 게시 실패는 best-effort — 실패해도 DM 흐름엔 영향 없습니다.",
    ],
    "fields": {
        "public_reply_templates": "답글 문구 목록 (필수, 최소 1개, 권장 3개+)",
        "public_reply_batch_size": "이 개수만큼 게시 후 쿨다운 (기본 10)",
        "public_reply_batch_pause_seconds": "쿨다운 대기 시간 초 (기본 300)",
    },
}


# 예약 발송 안내 (v3.9 — 활성 기간 한정 + 자동 종료)
SCHEDULING_GUIDE: dict = {
    "headline": "예약 발송 (활성 기간 설정 + 자동 종료)",
    "description": (
        "캠페인이 DM을 발송하는 활성 기간을 지정합니다. 시작일 전에는 발송하지 않고, "
        "종료일이 지나면 자동으로 종료(completed)됩니다. 두 값을 모두 비우면 "
        "기존처럼 수동 운영(상시 발송)입니다."
    ),
    "items": [
        "scheduled_start_at: 이 시각부터 발송 시작. 비우면 즉시 시작.",
        "scheduled_end_at: 이 시각 이후 자동 종료. 비우면 수동 종료 전까지 무기한.",
        "시각은 ISO8601 + 타임존 포함 권장 (예: 2026-07-01T09:00:00+09:00).",
        "scheduled_end_at 은 scheduled_start_at 보다, 그리고 현재 시각보다 미래여야 합니다.",
        "자동 종료는 약 1분 이내 반영됩니다 (Celery Beat 1분 주기).",
        "설정/변경: POST /auto-dm-campaigns/{id}/schedule/ (창 통째 교체) "
        "또는 생성/수정(PATCH) 시 함께 전달.",
        "종료된 캠페인을 다시 켜려면 재개(resume) 또는 schedule 로 기간 재지정. "
        "재개 시 과거가 된 종료 예약은 자동 해제됩니다.",
    ],
    "fields": {
        "scheduled_start_at": "발송 시작일시 (nullable, ISO8601)",
        "scheduled_end_at": "자동 종료일시 (nullable, ISO8601)",
    },
    # 응답의 schedule_state 값 의미 (프론트 배지/필터용)
    "schedule_state_values": {
        "always_on": "기간 미설정 (수동 운영)",
        "scheduled": "시작 대기 중 (시작일 이전)",
        "running": "활성 기간 진행 중",
        "ended": "종료일 경과 (자동 종료됨)",
    },
}


# 링크 버튼 안내 (v4.1 — DM 카드에 web_url 버튼 첨부)
LINK_BUTTON_GUIDE: dict = {
    "headline": "링크 버튼 (DM 에 라벨 달린 링크 버튼 첨부)",
    "description": (
        "URL 을 본문 텍스트에 박는 대신, 발송되는 DM 카드에 라벨 달린 링크 버튼(Meta web_url 버튼)으로 "
        "첨부합니다. 인스타 앱에서 버튼으로 보이고, 첫 DM 텍스트에 URL 을 직접 넣어 스팸 판정되는 "
        "문제를 피합니다."
    ),
    "items": [
        "단순 DM(게이트 미사용)은 그 DM 에 링크 버튼이 붙습니다.",
        "follow-gate(팔로우 검증 / 버튼클릭 즉시) 모드에선 reward DM 에 붙습니다 "
        "(opening/재안내 DM 에는 게이트 버튼이 붙으므로 링크 버튼은 reward 에만).",
        "link_button_url 이 비어 있으면 버튼은 첨부되지 않습니다.",
        "Meta 버튼 라벨 한도는 20자입니다.",
    ],
    "fields": {
        "link_button_url": "버튼이 여는 URL (http/https). 비우면 버튼 없음.",
        "link_button_label": "버튼 글자 (최대 20자). 비우면 '자세히 보기'.",
    },
}


def build_campaign_guide() -> dict:
    """프론트가 한 번에 받아 갈 수 있는 통합 가이드."""
    return {
        "version": "v4.1",
        "trigger_types": TRIGGER_TYPE_GUIDE,
        "keyword_modes": KEYWORD_MODE_GUIDE,
        "follow_gate": FOLLOW_GATE_GUIDE,
        "public_reply": PUBLIC_REPLY_GUIDE,
        "scheduling": SCHEDULING_GUIDE,
        "link_button": LINK_BUTTON_GUIDE,
    }
