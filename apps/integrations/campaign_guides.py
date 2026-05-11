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
            (
                "동일 IG 계정에 next 캠페인이 여러 개 있으면 "
                "모두 같은 신규 게시물에 한 번에 적용됩니다."
            ),
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


# Follow-gate 안내 (deprecated — Meta 한계로 silent 검증 불가)
FOLLOW_GATE_GUIDE: Dict = {
    "deprecated": True,
    "headline": "[지원 중단] Follow-gate 기능",
    "items": [
        "Meta API 정책상 '실제 팔로우 여부'는 silent 검증이 불가능합니다.",
        "이 기능은 더 이상 동작하지 않습니다 (옵션을 켜셔도 무시됩니다).",
        "팔로우 요청을 원하시면 공개 답글/Opening DM 본문에 안내 문구만 포함해주세요.",
    ],
}


# 공개 답글 안내 (v3.5 — 봇 검사 회피 다중 템플릿 + 배치 쿨다운)
PUBLIC_REPLY_GUIDE: Dict = {
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


def build_campaign_guide() -> Dict:
    """프론트가 한 번에 받아 갈 수 있는 통합 가이드."""
    return {
        "version": "v3.4",
        "trigger_types": TRIGGER_TYPE_GUIDE,
        "keyword_modes": KEYWORD_MODE_GUIDE,
        "follow_gate": FOLLOW_GATE_GUIDE,
        "public_reply": PUBLIC_REPLY_GUIDE,
    }
