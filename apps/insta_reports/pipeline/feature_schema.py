"""S4 피처 추출 스키마 v2 — 관찰 가능한 것만, enum 고정, 주관 점수 금지, 서술은 한국어.

설계 근거: reference/design_extract.json + critic 수정(person_action 추가,
benefit_offer 정의 확장) + 기획서 표4-2 (transcript·상업 요소 그룹).
"""

import re

FEATURE_SCHEMA_VERSION = 2  # v2: 관찰 서술 한국어 강제 (v1 영어 출력 문제 수정)

HOOK_TYPES = [
    "question",
    "shock_warning",
    "loss_aversion",
    "numbered_list",
    "result_first",
    "benefit_offer",
    "relatable_skit",
    "story_experience",
    "plain_statement",
    "other",
]
# 표 라벨(짧게) — 아래 HOOK_PLAIN_KO(문장용 서술형)와 어휘를 반드시 일치시킬 것.
# 한 개념에 이름이 두 가지로 갈리면 리포트에서 "결과 먼저형/결과제시형"처럼 뒤섞인다.
HOOK_LABEL_KO = {
    "question": "질문으로 시작하기",
    "shock_warning": "놀랄 소식으로 시작하기",
    "loss_aversion": "“모르면 손해”라고 말하기",
    "numbered_list": "“몇 가지”라고 개수 예고하기",
    "result_first": "결과물을 먼저 보여주기",
    "benefit_offer": "자료·혜택을 준다고 말하기",
    "relatable_skit": "공감되는 상황으로 시작하기",
    "story_experience": "경험담으로 시작하기",
    "plain_statement": "바로 설명 시작하기",
    "other": "그 외 방식",
    "none": "첫 마디 없음",
}
# AI 합성 입력·출력에 쓰는 "사람말 이름" — 내부 enum/전문용어가 리포트에 새지 않게.
# 표 라벨(HOOK_LABEL_KO)보다 더 서술적이어서 문장 안에 그대로 넣어도 읽힘.
HOOK_PLAIN_KO = {
    "question": "시청자에게 질문하며 시작하는 영상",
    "shock_warning": "놀랄 만한 소식으로 시작하는 영상",
    "loss_aversion": "'모르면 손해'라고 말하며 시작하는 영상",
    "numbered_list": "'몇 가지'라고 개수를 예고하며 시작하는 영상",
    "result_first": "결과물을 먼저 보여주며 시작하는 영상",
    "benefit_offer": "자료·혜택을 준다고 먼저 말하는 영상",
    "relatable_skit": "공감되는 상황으로 시작하는 영상",
    "story_experience": "본인 경험담으로 시작하는 영상",
    "plain_statement": "특별한 장치 없이 바로 설명하는 영상",
    "other": "그 외 방식으로 시작하는 영상",
    "none": "첫 마디 없이 화면만 나오는 영상",
}
OPENING_PLAIN_KO = {
    "talking_face": "사람이 카메라를 보고 말하며 시작하는 영상",
    "screen_recording": "컴퓨터·앱 화면부터 보여주는 영상",
    "result_showcase": "완성된 결과물을 먼저 보여주는 영상",
    "text_card": "글자 화면으로 시작하는 영상",
    "product_closeup": "제품·사물을 가까이 보여주며 시작하는 영상",
    "person_action": "손동작이나 시연 장면으로 시작하는 영상",
    "broll": "풍경·분위기 장면으로 시작하는 영상",
    "other": "그 외 화면으로 시작하는 영상",
}

# 사람말 설명 + 범용 예시 2개 (실제 채록 훅이 있으면 그걸 우선 사용)
HOOK_DESC_KO = {
    "question": (
        "시청자에게 직접 물어보며 시작",
        ["여러분은 아침에 뭐 드세요?", "이 차이 눈치채셨나요?"],
    ),
    "shock_warning": ("놀람·경고로 시선을 잡는 시작", ["큰일났습니다", "이거 심각한데요"]),
    "loss_aversion": (
        "안 보면 뒤처진다는 느낌을 주는 시작",
        ["아직도 이렇게 하세요?", "이거 모르면 손해예요"],
    ),
    "numbered_list": (
        "몇 가지를 알려줄지 숫자부터 말하는 시작",
        ["이 3가지만 바꾸세요", "딱 4개만 기억하세요"],
    ),
    "result_first": (
        "완성된 결과부터 보여주고 시작",
        ["한 달 만에 이렇게 됐어요", "10분 만에 만들었어요"],
    ),
    "benefit_offer": (
        "자료·혜택을 준다고 먼저 말하는 시작",
        ["정리본 받아가세요", "팔로우하면 보내드려요"],
    ),
    "relatable_skit": (
        "일상 공감 상황으로 시작",
        ["월요일 아침에 이런 적 있죠", "다이어트 3일차 상황극"],
    ),
    "story_experience": ("자기 경험담으로 시작", ["제가 3년 해보니까요", "처음엔 저도 실패했어요"]),
    "plain_statement": (
        "특별한 장치 없이 바로 본론으로 들어가는 시작",
        ["오늘은 사용법을 알려드릴게요", "신기능 소개합니다"],
    ),
    "other": ("위 유형에 해당하지 않는 시작", []),
    "none": (
        "말도 자막도 없이 화면만 나오는 시작",
        ["BGM만 깔린 채 결과물 슬라이드", "풍경 화면으로 시작"],
    ),
}
OPENING_DESC_KO = {
    "talking_face": (
        "사람이 카메라를 보고 말하면서 시작",
        ["'여러분!' 하며 얼굴 등장", "놀란 표정 리액션"],
    ),
    "screen_recording": (
        "프로그램·앱 화면 녹화로 바로 시작",
        ["코드 편집기에 타이핑", "앱 설정 화면 누르기"],
    ),
    "result_showcase": (
        "완성작·변화 결과가 첫 화면",
        ["완성된 결과물이 딱 등장", "전→후 비교 화면"],
    ),
    "text_card": ("글자만 있는 표지 카드로 시작", ["풀스크린 제목 카드", "질문이 적힌 검은 화면"]),
    "product_closeup": (
        "제품·음식·사물 클로즈업으로 시작",
        ["제품을 손에 들고 클로즈업", "완성 요리 클로즈업"],
    ),
    "person_action": ("사람의 동작·시연이 중심인 시작", ["운동 동작 시연", "조리 과정 손동작"]),
    "broll": ("말·시연 없는 풍경·무드 컷으로 시작", ["카페 풍경", "걷는 뒷모습"]),
    "other": ("위 유형에 해당하지 않는 시작", []),
}

OPENING_TYPES = [
    "talking_face",
    "screen_recording",
    "result_showcase",
    "text_card",
    "product_closeup",
    "person_action",
    "broll",
    "other",
]
OPENING_LABEL_KO = {
    "talking_face": "사람이 말하는 장면",
    "screen_recording": "컴퓨터·앱 화면",
    "result_showcase": "완성된 결과물 화면",
    "text_card": "글자만 있는 화면",
    "product_closeup": "제품·사물을 가까이",
    "person_action": "손동작·시연 장면",
    "broll": "풍경·분위기 장면",
    "other": "그 외 화면",
}
CTA_TYPES = ["follow", "comment_keyword", "comment_open", "save", "share", "link_or_dm", "none"]
STRUCTURE_TYPES = [
    "hook_body_cta",
    "listicle",
    "tutorial_steps",
    "before_after",
    "storytelling",
    "single_scene",
    "other",
]
SEGMENT_ROLES = ["hook", "context", "main_point", "demo", "result", "cta", "other"]

# Gemini response_schema (OpenAPI 3.0 subset)
GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {
            "type": "object",
            "properties": {
                "text_verbatim": {"type": "string"},
                "source": {"type": "string", "enum": ["spoken", "on_screen_text", "both", "none"]},
                "type": {"type": "string", "enum": HOOK_TYPES},
            },
            "required": ["text_verbatim", "source", "type"],
        },
        "opening": {
            "type": "object",
            "properties": {
                "screen_type": {"type": "string", "enum": OPENING_TYPES},
                "visual_description": {"type": "string"},
                "shows_face": {"type": "boolean"},
                "on_screen_text_verbatim": {"type": "string"},
                "value_shown_in_2s": {"type": "boolean"},
            },
            "required": [
                "screen_type",
                "visual_description",
                "shows_face",
                "on_screen_text_verbatim",
                "value_shown_in_2s",
            ],
        },
        "cta": {
            "type": "object",
            "properties": {
                "types": {"type": "array", "items": {"type": "string", "enum": CTA_TYPES}},
                "comment_keyword_verbatim": {"type": "string"},
                "quotes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "timestamp_sec": {"type": "number"},
                            "quote_verbatim": {"type": "string"},
                        },
                        "required": ["timestamp_sec", "quote_verbatim"],
                    },
                },
            },
            "required": ["types", "comment_keyword_verbatim", "quotes"],
        },
        "structure": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": STRUCTURE_TYPES},
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "start_sec": {"type": "number"},
                            "end_sec": {"type": "number"},
                            "role": {"type": "string", "enum": SEGMENT_ROLES},
                            "label": {"type": "string"},
                        },
                        "required": ["start_sec", "end_sec", "role", "label"],
                    },
                },
            },
            "required": ["type", "segments"],
        },
        "pacing": {
            "type": "object",
            "properties": {
                "cut_count_first_10s": {"type": "integer"},
                "video_duration_sec": {"type": "number"},
            },
            "required": ["cut_count_first_10s", "video_duration_sec"],
        },
        "audio": {
            "type": "object",
            "properties": {
                "has_voiceover": {"type": "boolean"},
                "has_subtitles": {"type": "boolean"},
                "has_bgm": {"type": "boolean"},
                "transcript_short": {"type": "string"},
            },
            "required": ["has_voiceover", "has_subtitles", "has_bgm", "transcript_short"],
        },
        "commercial": {
            "type": "object",
            "properties": {
                "is_promotional": {"type": "boolean"},
                "brands_or_products": {"type": "array", "items": {"type": "string"}},
                "price_mentioned": {"type": "boolean"},
            },
            "required": ["is_promotional", "brands_or_products", "price_mentioned"],
        },
        "topic_keywords": {"type": "array", "items": {"type": "string"}},
        "uncertainty_notes": {"type": "string"},
    },
    "required": [
        "hook",
        "opening",
        "cta",
        "structure",
        "pacing",
        "audio",
        "commercial",
        "topic_keywords",
        "uncertainty_notes",
    ],
}

EXTRACT_PROMPT = """당신은 숏폼 영상 '관찰 기록원'입니다. 첨부된 인스타그램 릴스 영상 1개를 처음부터 끝까지 보고, 화면과 소리에서 실제로 관찰되는 사실만 JSON으로 기록하세요.

[절대 규칙]
0. **모든 서술은 반드시 한국어로 쓰세요.** visual_description, segments[].label,
   uncertainty_notes 를 영어로 쓰면 검증 실패로 반려됩니다. (원문 인용 필드는 예외 — 2번 참조)
   예) visual_description: "빨간 펜으로 종이에 동그라미를 치는 손" (O)
       visual_description: "A hand circles text with a red marker" (X — 영어 금지)
1. 관찰만 기록: 평가·해석·추측·점수화 금지. "첫 마디가 강하다" 같은 판단 금지.
2. 원문 보존: *_verbatim 필드는 영상 표기 그대로(이모지·오탈자 포함). 번역·교정 금지.
   영상 속 발화·자막이 영어면 그 필드만 영어여도 됩니다(원문이므로).
3. 첫 문장(hook): 0~3초에서 사람이 말한 첫 문장 우선. 발화 없으면 화면 텍스트(source="on_screen_text"). 둘 다 없으면 text_verbatim=""·source="none"·type="other".
4. 셀 수 있는 것은 직접 세기: cut_count_first_10s = 첫 10초(짧으면 전체) 컷 전환 수. 줌·팬·자막 교체는 컷 아님.
5. 등장한 것만: brands_or_products 는 화면·음성에 실제 등장한 이름만. 추측 금지.
6. 애매하면 other 선택 후 uncertainty_notes 에 이유 한 줄.

[hook.type 정의 — 겹치면 우선순위: numbered_list > loss_aversion > benefit_offer > result_first > shock_warning > question > relatable_skit > story_experience > plain_statement]
- question: 시청자에게 직접 묻는 의문문 ("여러분은 아침에 뭐 드세요?")
- shock_warning: 놀람·경고·긴급 ("큰일났습니다", 🚨‼️)
- loss_aversion: 모르면 손해/뒤처짐 프레임 ("이거 모르면 손해예요", "아직도 ~하세요?")
- numbered_list: 개수 명시 ("이 3가지만 바꾸세요")
- result_first: 결과·성과·변화 먼저 ("한 달 만에 -5kg", "클릭 한 번으로 이런 디자인이")
- benefit_offer: 자료·혜택·할인·이벤트 제공 선언 ("가이드 받아가세요", "50% 할인")
- relatable_skit: 일상 공감 상황·상황극 ("월요일 아침에 이런 적 있죠")
- story_experience: 개인 경험담 서사 ("제가 3년 해보니까요")
- plain_statement: 장치 없이 주제·방법 바로 설명 (명사구 타이틀 포함)

[opening.screen_type — 0~3초 화면의 지배적 요소 1개]
talking_face(말하는 얼굴 중심) / screen_recording(PC·앱 화면 중심) / result_showcase(완성물·비포애프터 결과 먼저) / text_card(풀스크린 텍스트) / product_closeup(제품·음식·사물 클로즈업) / person_action(운동·조리·댄스 등 사람의 동작·시연 중심) / broll(발화·시연 없는 풍경·무드 컷) / other
value_shown_in_2s: 시청자가 얻을 결과물·혜택이 처음 2초 내 화면 또는 발화에 등장하는가.

[cta.types — 영상 전체(발화+자막+엔딩카드)에서 관찰된 전부, 없으면 ["none"] 단독]
follow / comment_keyword(특정 단어 댓글 유도 — 단어를 comment_keyword_verbatim 에 원문 그대로) / comment_open / save / share / link_or_dm / none. 각 CTA 원문과 시점을 quotes 에 최대 5개.

[structure] hook_body_cta / listicle / tutorial_steps / before_after / storytelling / single_scene / other. segments 는 화면·내용 전환 기준 1~12개, 시간순, 영상 전체를 덮을 것(±1초). label 은 보이는/들리는 것 관찰 서술 30자 이내.

[audio] transcript_short: 발화 전체를 300자 이내로 축약 채록(발화 없으면 빈 문자열).

[topic_keywords] 주제 핵심 명사 1~5개. '꿀팁'·'영상' 같은 일반어 금지, '홈트'·'아이라이너'처럼 구체어.

출력은 지정된 JSON 스키마와 정확히 일치. JSON 외 텍스트 금지."""

IMAGE_PROMPT_SUFFIX = """
[이미지 모드] 첨부된 것은 영상이 아니라 게시물 대표 이미지 1장과 캡션입니다.
- hook: 캡션 첫 문장을 text_verbatim 으로, source="on_screen_text" 대신 "none"이 아닌 경우만.
- opening.screen_type: 이미지의 지배적 요소로 판정. value_shown_in_2s: 이미지에 결과물·혜택이 보이는가.
- pacing: cut_count_first_10s=0, video_duration_sec=0. audio: 전부 false, transcript_short="".
- structure: type="single_scene", segments 는 [{start_sec:0,end_sec:0,role:"main_point",label:이미지 관찰 서술}] 1개.
캡션:
"""


_HANGUL = re.compile(r"[가-힣]")
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")

# 추출 결과(구간 라벨·화면 묘사)에 남을 수 있는 업계 용어 → 사람말 치환.
# 코드가 만드는 문장(폴백·표 예시)에 적용한다. AI 생성 문장은 게이트가 따로 막는다.
_PLAIN_SUBS = [
    (re.compile(r"\bCTA\b", re.I), "행동 요청"),
    (re.compile(r"\b(훅|후킹)\b"), "첫 마디"),
    (re.compile(r"\b오프닝\b"), "시작 부분"),
    (re.compile(r"\b인트로\b"), "도입부"),
    (re.compile(r"\b아웃트로\b"), "마무리"),
    (re.compile(r"\b썸네일\b"), "표지 화면"),
    (re.compile(r"캡션"), "게시물 글"),
    (re.compile(r"[一-鿿]+"), ""),  # 한자 제거
]


def plainify(text: str) -> str:
    """코드가 리포트에 넣는 문장에서 업계 용어·한자를 사람말로 바꾼다."""
    if not text:
        return text
    for rx, rep in _PLAIN_SUBS:
        text = rx.sub(rep, text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _is_english(text: str) -> bool:
    """한글이 없고 라틴 단어가 2개 이상이면 영어 서술로 판정."""
    if not text or len(text) < 8:
        return False
    return not _HANGUL.search(text) and len(_LATIN_WORD.findall(text)) >= 2


def validate_feature(f: dict, duration_hint: float | None = None) -> list[str]:
    """코드 검증 — 위반 목록 반환(비면 통과). cut 재계산은 별도."""
    errs = []
    # 한국어 강제 (관찰 서술 필드만 — verbatim 은 원문이라 제외)
    if _is_english(f["opening"].get("visual_description", "")):
        errs.append("opening.visual_description 을 한국어로 쓰세요 (현재 영어)")
    for i, s in enumerate(f["structure"].get("segments") or []):
        if _is_english(s.get("label", "")):
            errs.append(f"structure.segments[{i}].label 을 한국어로 쓰세요 (현재 영어)")
            break
    if f["hook"]["type"] not in HOOK_TYPES:
        errs.append("hook.type enum 위반")
    if f["opening"]["screen_type"] not in OPENING_TYPES:
        errs.append("opening.screen_type enum 위반")
    types = f["cta"]["types"]
    if not types:
        errs.append("cta.types 비어있음")
    if "none" in types and len(types) > 1:
        errs.append("cta.types 에 none 이 다른 값과 공존")
    if f["cta"]["comment_keyword_verbatim"] and "comment_keyword" not in types:
        types.append("comment_keyword")  # 자가 복구
    segs = f["structure"]["segments"]
    if not segs:
        errs.append("segments 비어있음")
    else:
        for i in range(1, len(segs)):
            if segs[i]["start_sec"] < segs[i - 1]["start_sec"] - 1:
                errs.append("segments 시간 역행")
                break
    if len(f.get("topic_keywords") or []) < 1:
        errs.append("topic_keywords 비어있음")
    return errs


def cut_pace_of(cut_count: int, duration: float) -> str:
    """계산=코드: 10초 환산 컷 수로 페이스 확정."""
    if duration and duration < 10:
        cut_count = cut_count * 10 / max(duration, 1)
    if cut_count <= 2:
        return "slow"
    if cut_count <= 5:
        return "medium"
    return "fast"
