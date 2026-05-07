"""
Auto DM 캠페인 사용자 안내 문구 (단일 source of truth).

프론트엔드가 캠페인 생성/수정 폼의 라디오/토글 옆에 노출할 설명, 툴팁,
주의사항을 여기서 한 번에 정의한다. 백엔드 검증 로직과 사용자 노출 텍스트가
서로 어긋나지 않도록 분리.
"""

from __future__ import annotations

from typing import Dict, List

# 트리거 타입별 안내 (캠페인 생성 폼의 첫 라디오 그룹)
TRIGGER_TYPE_GUIDE: List[Dict] = [
    {
        "value": "specific_media",
        "label": "특정 게시물에 댓글",
        "description": (
            "선택한 한 개의 게시물에 달린 댓글에 대해서만 자동 DM을 발송합니다."
        ),
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
            "한 번 적용된 후에는 'specific_media' 로 자동 전환되며, 그 이후 "
            "올리는 게시물에는 적용되지 않습니다. 다음 게시물에도 적용하려면 "
            "새 캠페인을 만들어주세요."
        ),
        "requires": [],
        "tier": "pro",
        "notes": [
            "최대 5분 정도 지연 후 새 게시물이 인식됩니다.",
            "캠페인을 만든 시점 이후의 새 게시물부터 적용됩니다 (과거 게시물은 무시).",
            "동일 IG 계정에 next 캠페인이 여러 개 있으면 모두 같은 신규 게시물에 한 번에 적용됩니다.",
        ],
    },
]


KEYWORD_MODE_GUIDE: List[Dict] = [
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


# Follow-gate 안내 (Meta 한계 명시)
FOLLOW_GATE_GUIDE: Dict = {
    "headline": "Follow-gate 사용 시 알아두실 점",
    "items": [
        "Meta API 정책상 '실제 팔로우 여부'는 검증할 수 없습니다.",
        "사용자가 Opening DM에 답장하는 시점을 'Gate 통과'로 간주합니다.",
        "Gate 통과 키워드를 비워두시면 어떤 답장이든 통과로 인정됩니다.",
        "Opening DM 후 24시간 내 답장이 없으면 자동으로 만료(EXPIRED) 처리됩니다.",
        "팔로우 없이 답장만 보내도 Gate가 통과되므로, 약관/안내에 명시하시는 것을 권장합니다.",
    ],
}


# 공개 답글 안내
PUBLIC_REPLY_GUIDE: Dict = {
    "headline": "공개 답글 함께 게시",
    "description": (
        "DM 발송 직후 댓글에도 답글을 게시합니다. "
        "예: 'DM 보내드렸습니다! 확인 부탁드려요 :)'"
    ),
    "items": [
        "댓글 작성 후 7일 이내에만 가능합니다 (Meta 정책).",
        "답글 게시는 best-effort — 실패해도 DM 흐름에는 영향이 없습니다.",
    ],
}


def build_campaign_guide() -> Dict:
    """프론트가 한 번에 받아 갈 수 있는 통합 가이드."""
    return {
        "version": "v3.4",
        "trigger_types": TRIGGER_TYPE_GUIDE,
        "keyword_modes": KEYWORD_MODE_GUIDE,
        "follow_gate": FOLLOW_GATE_GUIDE,
        "public_reply": PUBLIC_REPLY_GUIDE,
    }
