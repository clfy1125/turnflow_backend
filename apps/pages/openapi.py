"""
drf-spectacular postprocessing hook.
JSONField 서브클래스의 schema가 additionalProp1/2/3 으로 렌더링되는 버그를 수동으로 수정합니다.
"""

BLOCK_DATA_SCHEMA = {
    "type": "object",
    "properties": {
        # ── single_link ──────────────────────────────
        "url": {
            "type": "string",
            "format": "uri",
            "description": "[single_link 필수] 이동할 URL (https://...)",
            "example": "https://naver.me/abc",
        },
        "label": {
            "type": "string",
            "description": "[single_link 필수] 버튼 표시 텍스트",
            "example": "쿠팡 추천 링크",
        },
        "description": {
            "type": "string",
            "description": "[single_link 선택] 버튼 하단 짧은 설명",
            "example": "오늘만 할인",
        },
        "layout": {
            "type": "string",
            "enum": ["small", "large"],
            "description": "[single_link 선택] 버튼 크기 (기본: small)",
        },
        "thumbnail_url": {
            "type": "string",
            "format": "uri",
            "description": "[single_link 선택] 썸네일 이미지 URL",
        },
        # ── 상품 카드(single_link / group_link) ───────
        "price": {
            "type": "string",
            "description": "[상품 링크 선택] 판매가. 콤마 없는 숫자 문자열(소수점 보존)",
            "example": "25.99",
        },
        "original_price": {
            "type": "string",
            "description": "[상품 링크 선택] 할인 전 정가 (취소선 표시용)",
            "example": "35.99",
        },
        "currency": {
            "type": "string",
            "description": (
                '[상품 링크 선택] price 의 통화. ISO 4217 3글자("KRW"/"USD"). '
                "키가 없으면 프론트가 KRW 로 간주 — 기존 블록 마이그레이션 불필요. "
                "group_link 은 항목마다 links[].currency 로 따로 둔다."
            ),
            "example": "USD",
        },
        # ── profile ──────────────────────────────────
        "headline": {
            "type": "string",
            "description": "[profile 필수] 메인 한 줄 소개",
            "example": "독일 면도기 전문",
        },
        "subline": {
            "type": "string",
            "description": "[profile 선택] 부제목",
            "example": "방수 / 저소음",
        },
        "avatar_url": {
            "type": "string",
            "format": "uri",
            "description": "[profile 선택] 프로필 이미지 URL",
        },
        # ── contact ──────────────────────────────────
        "country_code": {
            "type": "string",
            "description": "[contact 필수] 국가 코드 (+82 형식)",
            "example": "+82",
        },
        "phone": {
            "type": "string",
            "description": "[contact 필수] 전화번호 (하이픈 없이)",
            "example": "01012345678",
        },
        "whatsapp": {
            "type": "boolean",
            "description": "[contact 선택] WhatsApp 링크 사용 여부",
        },
    },
    "example": {
        "url": "https://naver.me/abc",
        "label": "쿠팡 추천 링크",
        "description": "오늘만 할인",
        "layout": "large",
    },
}


def postprocess_block_data_schema(result, generator, request, public):
    """
    생성된 OpenAPI 스키마에서 Block* 컴포넌트의 `data` 필드를
    BLOCK_DATA_SCHEMA 로 교체합니다.
    """
    schemas = result.get("components", {}).get("schemas", {})
    for name, schema in schemas.items():
        if "Block" not in name:
            continue
        props = schema.get("properties", {})
        if "data" in props:
            props["data"] = BLOCK_DATA_SCHEMA
    return result
