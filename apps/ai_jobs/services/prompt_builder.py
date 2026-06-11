"""프롬프트 조립.

룰셋(md) + few-shot 예시(json) + 사용자 입력을 합쳐 system / user 메시지를 생성한다.

bio_remake 작업은 두 모드(``full_restyle`` / ``style_only``) 로 분기.
구 방식(legacy) 은 slug 없이 새 페이지를 생성하는 경우에만 사용된다.
"""

import json
import logging
from pathlib import Path

from . import category_profiles as _cat

logger = logging.getLogger(__name__)

# ai_assets 디렉토리 경로
_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "ai_assets"


def _read_asset(relative_path: str) -> str:
    """ai_assets/ 하위 파일을 읽어 반환. 없으면 빈 문자열."""
    fp = _ASSETS_DIR / relative_path
    if not fp.is_file():
        logger.warning("Asset 파일 없음: %s", fp)
        return ""
    return fp.read_text(encoding="utf-8")


def _load_examples(category: str, max_count: int = 4) -> str:
    """ai_assets/examples/{category}/ 아래 JSON 파일을 최대 max_count개 로드."""
    examples_dir = _ASSETS_DIR / "examples" / category
    if not examples_dir.is_dir():
        logger.warning("예시 디렉토리 없음: %s", examples_dir)
        return ""

    parts: list[str] = []
    files = sorted(examples_dir.glob("*.json"))[:max_count]
    for f in files:
        content = f.read_text(encoding="utf-8")
        parts.append(f"\n--- {f.name} ---\n{content}")
    return "\n".join(parts)


def _load_example_from_db(reference_page_slug: str) -> str:
    """DB 의 레퍼런스 페이지 1건을 ai_assets/examples/bio/N.json 와 동일 포맷의
    JSON 문자열로 반환.

    반환 형식 — ``_load_examples()`` 와 동일한 ``\\n--- {label} ---\\n{json}`` prefix.
    페이지가 없거나 비공개/비활성/is_reference 가 아니면 빈 문자열 (폴백 트리거).
    """
    from apps.pages.models import Block, Page  # 순환 import 방지 — 함수 안에서 import

    page = Page.objects.filter(
        slug=reference_page_slug,
        is_reference=True,
        is_public=True,
        is_active=True,
    ).first()
    if not page:
        logger.warning(
            "레퍼런스 페이지 로드 실패 (없음/비공개/비활성): %s",
            reference_page_slug,
        )
        return ""

    blocks_qs = (
        Block.objects.filter(page=page)
        .order_by("order")
        .values("id", "type", "order", "is_enabled", "data", "custom_css")
    )
    payload = {
        "title": page.title,
        "is_public": page.is_public,
        "data": page.data,
        "custom_css": page.custom_css,
        "blocks": [
            {
                "id": b["id"],
                "type": b["type"],
                "order": b["order"],
                "data": b["data"],
                "custom_css": b["custom_css"],
            }
            for b in blocks_qs
        ],
    }
    label = f"reference-page-{reference_page_slug}.json"
    return f"\n--- {label} ---\n{json.dumps(payload, ensure_ascii=False, indent=2)}"


# ─── job_type별 시스템 프롬프트 ───────────────────────────────

# job_type → examples 디렉토리 이름 매핑
_EXAMPLE_DIR_MAP: dict[str, str] = {
    "bio_remake": "bio",
    "theme_generation": "bio",
    "copy_generation": "bio",
}

_SYSTEM_PROMPTS: dict[str, str] = {
    # 새 페이지 생성 (slug 없음) — 구 방식 유지.
    "bio_remake": (
        "너는 단순한 JSON 생성기가 아니라, "
        "브랜드 컨셉을 해석하고 색감, 레이아웃, 이미지까지 설계하는 최고 수준의 프로덕트 디자이너이자 프론트엔드 아키텍트다.\n\n"
        "너의 역할은 다음 4가지를 반드시 수행하는 것이다:\n"
        "1. 주어진 컨셉을 분석해서 전체 디자인 컨셉을 정의한다.\n"
        "2. 디자인 컨셉에 맞는 색상 팔레트(배경, 카드, 텍스트, 버튼)를 일관되게 설계한다.\n"
        "3. 이미지가 필요한 블록에는 {{image:영문_키워드}} 형식으로 이미지 검색어를 넣는다.\n"
        "4. 블록 구조를 사용자 경험(UX)이 좋게 재배치한다.\n\n"
        "중요 규칙:\n"
        "- 반드시 우리 블록 규칙을 100% 준수해야 한다.\n"
        "- 모든 블록에는 _type을 정확히 넣어야 한다.\n"
        "- 디자인은 하나의 브랜드처럼 일관된 색감을 유지해야 한다.\n"
        "- 단순 나열이 아니라 '스토리 흐름이 있는 페이지 구조'로 만들어야 한다.\n"
        "- 이미지 URL은 {{image:검색키워드}} 형식으로만 작성한다 (예: {{image:jpop band concert}}).\n"
        "- 절대 설명하지 말고 JSON 코드만 출력한다."
    ),
    # 기존 페이지 리메이크 — 모드별 분기.
    "bio_remake_full_restyle": (
        "너는 기존 링크인바이오 페이지의 디자인을 리뉴얼하는 프로덕트 디자이너다.\n\n"
        "[엄격한 제약]\n"
        "- 기존 블록의 콘텐츠(URL, 이미지, 텍스트)는 절대 변경하지 마라.\n"
        "- URL/이미지는 [URL_n] / [IMG_n] / [VIDEO_n] / [CONTACT_n] 같은 placeholder 토큰으로\n"
        "  주어진다. 응답에 그대로 echo 하거나 생략하면 백엔드가 원본으로 복원한다.\n"
        "  새 URL/이미지 텍스트를 생성하지 마라 — 환각된 placeholder 는 빈 문자열로 떨어진다.\n"
        "- _type 은 절대 변경하지 마라. 기존 블록은 id 로 매칭된다.\n\n"
        "[허용된 변경]\n"
        "- 페이지 design_settings (배경/버튼/폰트/색상 등) 전체 교체.\n"
        "- 각 블록의 스타일/세팅 (custom_bg_color, layout, *_layout, text_align 등) 변경.\n"
        "- 블록 순서 재배치 (order 1 부터).\n"
        "- 디자인 흐름상 필요한 새 블록 추가 — ``_new: true`` 표시. 새 블록의 텍스트 콘텐츠\n"
        "  (label, content 등) 는 직접 작성 가능하지만 URL/이미지는 비워둔다.\n"
        "  새 이미지 블록은 {{image:검색키워드}} placeholder 사용 가능.\n"
        "- 불필요한 기존 블록은 응답에서 누락하면 삭제된다.\n\n"
        "[출력 형식]\n"
        "오직 JSON 만 출력. 설명 금지.\n"
        '{ "page": {"title": ..., "is_public": ..., "data": {"design_settings": {...}}, "custom_css": "..."},\n'
        '  "blocks": [{"id": 123, "_type": "single_link", "order": 1, "data": {...스타일...}}, ...] }'
    ),
    "bio_remake_style_only": (
        "너는 큰 링크인바이오 페이지의 디자인 톤만 새롭게 입히는 디자이너다.\n\n"
        "[엄격한 제약]\n"
        "- 페이지 콘텐츠는 절대 만지지 마라. 블록 추가/삭제/순서변경/타입변경 금지.\n"
        "- 너에게는 페이지의 일부 블록만 샘플로 보인다 (처음 몇 개 + 중간 + 끝 + 각 _type 1개).\n"
        "  그러나 응답은 ``block_styles`` 객체 하나로 ``*`` 글로벌 + ``<subtype>`` 별 +\n"
        "  ``_by_id`` 개별 override 만 출력한다.\n"
        "- 화이트리스트에 없는 키를 응답에 넣어도 백엔드가 무시한다.\n\n"
        "[허용된 변경 — 화이트리스트]\n"
        "공통: custom_bg_color, custom_border_color, custom_text_color, custom_button_color\n"
        "profile: profile_layout, font_size\n"
        "single_link: layout, text_align\n"
        "group_link: group_layout, display_mode, text_align\n"
        "social: custom_icon_color\n"
        "video: video_layout, autoplay\n"
        "text: text_layout, text_align, text_size, custom_sub_text_color\n"
        "gallery: gallery_layout, auto_slide, keep_ratio\n"
        "spacer: divider_style, divider_width, divider_color, spacing\n"
        "notice: notice_layout\n"
        "customer: custom_input_bg_color\n"
        "folder: folder_icon, folder_icon_color, is_collapsed_default, folder_display_mode,\n"
        "        text_align, folder_toggle_bg, folder_popup_bg, folder_popup_text, folder_popup_accent\n"
        "schedule: schedule_layout\n\n"
        "[페이지 레벨] page.data.design_settings 전체 교체, page.custom_css 교체 가능. title/is_public 은 무시됨.\n\n"
        "[출력 형식]\n"
        "오직 JSON 만 출력. 설명 금지.\n"
        '{ "page": {"data": {"design_settings": {...}}, "custom_css": "..."},\n'
        '  "block_styles": {\n'
        '    "*": {"custom_bg_color": "#...", "custom_text_color": "#..."},\n'
        '    "single_link": {"layout": "wide"},\n'
        '    "_by_id": {"217": {"custom_button_color": "#..."}}\n'
        "  } }"
    ),
}


def resolve_design_lead(user_input: dict) -> str:
    """새-페이지 생성에서 **디자인 주도권**을 누가 갖는지 결정한다 (프롬프트 전략 라우터).

    세 소스(카테고리 레시피 무드 / 컨셉 이미지 팔레트 / 레퍼런스 템플릿)를 동시에 주입하면
    서로 경쟁해 "뭘 줘도 비슷한 색감"이 된다(사용자 피드백). 우선순위로 하나만 주도하게 한다:

    - ``template``      — 사용자가 레퍼런스 페이지를 명시 → 그 페이지가 디자인의 단일 기준.
    - ``concept_image`` — 업로드 이미지에서 팔레트/무드가 추출됨 → 이미지가 색·톤의 단일 기준.
    - ``recipe``        — 그 외 기본 → 카테고리 레시피 무드 사용.

    (별도 LLM 라우터는 불필요 — 신호가 이미 구조화돼 있다: 명시 슬러그, 라벨링 비전이
    추출한 palette/mood_notes.)
    """
    if (user_input.get("reference_page_slug") or "").strip():
        return "template"
    catalog = user_input.get("image_catalog") or {}
    if (catalog.get("palette") or {}).get("background") or (
        catalog.get("mood_notes") or ""
    ).strip():
        return "concept_image"
    return "recipe"


def build_prompts(
    job_type: str,
    user_input: dict,
    mode: str | None = None,
) -> tuple[str, str]:
    """
    (system_prompt, user_prompt) 튜플을 반환한다.

    Args:
        job_type: 작업 유형 (bio_remake 등)
        user_input: 프론트에서 받은 사용자 입력
            - concept: 페이지 컨셉 설명
            - existing_blocks: 기존 페이지 블록 (optional, 리메이크 시). placeholder 가 freeze 된 상태.
            - existing_page_meta: 기존 페이지 메타 (optional, 리메이크 시).
            - sample_blocks: style_only 모드에서 샘플링된 블록 (optional).
            - all_block_ids: style_only 모드에서 전체 블록 id 목록 (optional).
        mode: ``""``(legacy) | ``"full_restyle"`` | ``"style_only"``.
              ``None`` 이면 ``user_input.get("mode", "")`` 로 보강.
    """
    if mode is None:
        mode = user_input.get("mode") or ""

    is_remake = bool(user_input.get("existing_blocks") or user_input.get("sample_blocks"))

    # 새-페이지 생성: 카테고리 결정(명시 category 우선, 없으면 concept 추론).
    new_category = None if is_remake else _cat.resolve_category(user_input)

    # 1) System prompt
    if is_remake and mode == "full_restyle":
        system_file = _read_asset(f"prompts/{job_type}/system_restyle_full.md")
        fallback = _SYSTEM_PROMPTS.get("bio_remake_full_restyle", _SYSTEM_PROMPTS["bio_remake"])
    elif is_remake and mode == "style_only":
        system_file = _read_asset(f"prompts/{job_type}/system_restyle_style_only.md")
        fallback = _SYSTEM_PROMPTS.get("bio_remake_style_only", _SYSTEM_PROMPTS["bio_remake"])
    elif is_remake:
        # mode 미지정 + remake → legacy 호환 (구 system_remake.md)
        system_file = _read_asset(f"prompts/{job_type}/system_remake.md")
        fallback = _SYSTEM_PROMPTS.get("bio_remake_with_existing") or _SYSTEM_PROMPTS["bio_remake"]
    else:
        system_file = _read_asset(f"prompts/{job_type}/system.md")
        fallback = _SYSTEM_PROMPTS.get(job_type, _SYSTEM_PROMPTS["bio_remake"])

    system_prompt = system_file if system_file else fallback

    # 2) 블록 규칙 로드
    block_rules = _read_asset("rules/block_rules.md")

    # 3) few-shot 예시
    # - 새 페이지 생성: 출력 형식 학습용 4개 (예시 그대로 출력해도 OK).
    # - full_restyle: 디자인 톤(색 팔레트·레이아웃·블록 조합) 참고용 2개. 출력 형식은 위 스키마.
    # - style_only: 대형 페이지 — 토큰 절감 우선이라 예시 미포함.
    # - reference_page_slug 가 있으면 DB 의 어드민 큐레이션 페이지 1건을 우선 사용.
    example_dir = _EXAMPLE_DIR_MAP.get(job_type, "bio")
    reference_page_slug = (user_input.get("reference_page_slug") or "").strip()

    # 새-페이지: 디자인 주도권 라우팅 (template > concept_image > recipe).
    design_lead = "" if is_remake else resolve_design_lead(user_input)

    # 명시 레퍼런스가 없으면 카테고리 기본 레퍼런스(예: invitation → @wedding)로 폴백.
    # 단 **recipe 모드일 때만** — 컨셉 이미지가 주도할 때 레퍼런스까지 끼어들면 색감이
    # 레퍼런스 쪽으로 끌려가 "이미지를 줘도 비슷하게 나오는" 문제가 된다.
    if not reference_page_slug and new_category and design_lead != "concept_image":
        reference_page_slug = (_cat.get_profile(new_category).get("reference_slug") or "").strip()
        if reference_page_slug:
            design_lead = "template"

    using_reference = False
    if mode == "style_only":
        examples = ""
    elif reference_page_slug:
        examples = _load_example_from_db(reference_page_slug)
        if not examples:
            # DB 로드 실패 (페이지가 사라졌거나 비활성/비공개 전환됨) → 파일 폴백
            logger.info(
                "reference_page_slug=%s 로드 실패 — 파일 예시로 폴백",
                reference_page_slug,
            )
            examples = _load_examples(example_dir, max_count=2 if is_remake else 4)
        else:
            using_reference = True
    elif is_remake:
        examples = _load_examples(example_dir, max_count=2)
    else:
        # 새-페이지: 형식·블록 다양성 학습용 2개만(다크 음악 예시 4개 → 테마 모방 유발했음).
        examples = _load_examples(example_dir, max_count=2)

    # 레퍼런스 로드 실패 시 template 주도권 해제 → 남은 신호로 재라우팅.
    if design_lead == "template" and not using_reference:
        catalog_sig = user_input.get("image_catalog") or {}
        design_lead = (
            "concept_image"
            if (
                (catalog_sig.get("palette") or {}).get("background")
                or catalog_sig.get("mood_notes")
            )
            else "recipe"
        )
    if not is_remake:
        logger.info("prompt design_lead=%s (category=%s)", design_lead, new_category)

    # 4) 사용자 입력 조합
    concept = user_input.get("concept", "")

    # ────────────────────────────────────────────────────────
    # DeepSeek prompt cache 최적화 — 고정 prefix 먼저, 가변부 나중.
    # ────────────────────────────────────────────────────────

    # ── 고정 (캐시 prefix) ─────────────────────────────────
    fixed_parts: list[str] = []
    if not is_remake:
        # 새 페이지 생성에만 이미지 키워드 규칙 필요.
        fixed_parts += [
            "### [이미지 URL 규칙 - 매우 중요!]",
            "- 실제 URL을 넣지 말 것!",
            "- 반드시 {{image:영문_검색어}} 형식으로 작성",
            "- 예시:",
            "  배너 이미지 → {{image:jpop band stage concert}}",
            "  앨범 커버 → {{image:music album cover aesthetic}}",
            "  프로필 → {{image:band member portrait}}",
            "- 키워드는 **구체적이고 컨셉과 관련**되게(주제·인물·제품이 분명히 보이게). "
            "빈 방·바닥·무의미한 풍경 금지. 블록마다 키워드를 다르게 해 같은 사진이 반복되지 않게 하라.",
            "",
            "### [사용자 업로드 이미지 규칙]",
            "- 아래 [목표] 위에 [사용 가능한 사용자 이미지] 목록이 주어지면, 그 이미지를 "
            "{{user_image:N}} 형식으로 해당 블록의 image_url 에 배치한다 (N = 목록의 번호).",
            "- 목록에 있는 사용자 이미지는 가능한 한 **모두 활용**하고, 각 이미지의 추천 위치를 우선 따른다.",
            "- 사용자 이미지로 채우지 못하는 자리에만 {{image:키워드}}(Pixabay) 를 쓴다.",
            "- **[사용 가능한 사용자 이미지] 목록이 없으면 {{user_image:N}} 을 절대 쓰지 말고 "
            "{{image:키워드}} 만 사용한다.**",
            "",
            "### [디자인 체크리스트 — 새 페이지 (시스템 규칙 요약)]",
            "- 색은 4개 토큰만: backgroundColor(분명히 밝거나 어둡게, 중간톤 금지) = frameBackgroundColor, "
            "blockBgColor(카드, 배경과 살짝 다른 명도), buttonColor(단 하나의 강조색). "
            "**textColor 는 넣지 마라 — 본문 글자색은 배경 대비로 자동 결정된다.**",
            "- 아래 추출 팔레트 #hex 가 주어지면 그대로 사용(비슷한 색으로 바꾸지 마라). 전체 3~4색.",
            "- 대표 비주얼은 profile cover_bg 히어로로. 메뉴/상품은 group_link + 썸네일. "
            '**주요 전환 CTA(카톡 문의/무료체험/예약/주문) 딱 1개는 layout:"medium"(스탠다드)+badge**, '
            "쇼케이스(large)는 이미지가 핵심인 대표 1개만, 나머지 링크는 small. 같은 _type 무리는 같은 톤으로 통일.",
            "- page.custom_css 를 비우지 말 것(은은한 배경 그래디언트 한 줄). 깨끗함 > 화려함(네온/무지개 금지).",
            "- single_link 의 url 은 비우지 말고 그럴듯한 https URL 을 넣어라(빈 url 은 렌더되지 않음).",
            "- 이미지 슬롯(profile cover_image_url/avatar_url, gallery images, **group_link links 의 "
            "모든 항목 thumbnail_url — list 레이아웃 포함**, 쇼케이스 single_link thumbnail)은 "
            "**절대 비우지 말고** {{image:키워드}} 로 채워라. 빈 이미지=깨진 화면. "
            "(예외: 후기 리스트('이름 ★★★★★')와 텍스트 가격표만 썸네일 생략 가능.)",
            "",
        ]
        # 카테고리 레시피(섹션 구성·한국 실서비스·카피 톤·레이아웃 전략) 주입.
        # 무드/색 지시는 recipe 주도일 때만 — template/concept_image 주도면 색은 그쪽이 결정.
        if new_category:
            fixed_parts += [
                _cat.build_recipe_prompt(new_category, include_mood=(design_lead == "recipe"))
            ]
    if block_rules:
        fixed_parts += [f"### [블록 규칙]\n{block_rules}", ""]
    if examples:
        if is_remake:
            # 리메이크 모드는 출력 스키마가 예시와 다르다 — 디자인 톤만 참고하라고 명시.
            fixed_parts += [
                "### [디자인 톤 참고용 예시 페이지 — 출력 형식 모방 금지]",
                "(아래는 잘 디자인된 페이지의 전체 JSON 예시다. "
                "색 팔레트·design_settings·블록별 layout 조합·문구 톤·_type 선택을 참고하라. "
                "단 출력은 시스템 프롬프트의 스키마(스타일 패치)를 따라야 하며, "
                "이 예시 JSON 의 풀 구조를 그대로 출력하면 안 된다.)",
                examples,
                "",
            ]
        else:
            fixed_parts += [
                "### [예시 JSON — 출력 형식·블록 다양성 참고용 (테마 모방 금지)]",
                "(아래는 JSON 출력 형식과 블록 구성의 다양성을 보여주는 예시다. "
                "**색/업종/다크테마/문구를 그대로 따라하지 마라** — 색·구성·카피·레이아웃은 위 "
                "[카테고리 레시피] 를 따르고, 여기서는 _type 종류·필드 채우는 법·블록 풍부함만 참고하라.)",
                examples,
                "",
            ]
    fixed_parts += [
        "### [출력 형식]",
        "설명 없이 JSON만 출력",
        "",
    ]

    # ── 가변 (캐시 경계 아래) ──────────────────────────────
    variable_parts: list[str] = []
    if is_remake and mode == "full_restyle":
        existing_page_meta = user_input.get("existing_page_meta", {}) or {}
        existing_blocks = user_input.get("existing_blocks") or []
        # chunked 호출 컨텍스트 — None 이면 단일 호출.
        chunk_idx = user_input.get("_chunk_idx")
        total_chunks = user_input.get("_total_chunks")
        fixed_ds = user_input.get("_fixed_design_settings")
        is_chunked = (
            isinstance(chunk_idx, int) and isinstance(total_chunks, int) and total_chunks > 1
        )

        if is_chunked:
            if chunk_idx == 0:
                # 첫 chunk — design_settings + page.custom_css 를 결정한다.
                variable_parts += [
                    f"### [페이지 분할 처리 — chunk {chunk_idx + 1}/{total_chunks}]",
                    "이 페이지는 너무 커서 LLM 호출을 여러 번에 나눠 처리합니다.",
                    "당신은 **첫 chunk** 입니다. 책임:",
                    "  1) **응답 JSON 최상위에 `page` 키 반드시 포함**. `page.data.design_settings` 전부 채우고 `page.custom_css` 도 비워두지 마라 (body 배경 그래디언트 한 줄 이상). 후속 chunk 들이 이 톤을 따라야 한다.",
                    "  2) 이 chunk 의 블록 스타일/텍스트 패치.",
                    '응답 형식은 단일 호출과 동일 — `{"page": {...}, "blocks": [...]}`. **`page` 키 누락은 금지**.',
                    "",
                ]
            else:
                variable_parts += [
                    f"### [페이지 분할 처리 — chunk {chunk_idx + 1}/{total_chunks}]",
                    "이 페이지는 너무 커서 LLM 호출을 여러 번에 나눠 처리합니다.",
                    f"당신은 chunk {chunk_idx + 1} 입니다 — design_settings 는 첫 chunk 가 이미 확정했습니다.",
                    "**그 톤을 그대로 유지**하며 이 chunk 의 블록 스타일/텍스트 패치만 출력하세요.",
                    "응답의 ``page`` 키는 생략하거나 빈 객체로 두세요 — 백엔드가 첫 chunk 의 페이지 메타를 사용합니다.",
                    "",
                ]
                if isinstance(fixed_ds, dict) and fixed_ds:
                    variable_parts += [
                        "### [확정된 design_settings — 동일 톤 유지]",
                        f"```json\n{json.dumps(fixed_ds, ensure_ascii=False, indent=2)}\n```",
                        "",
                    ]

        chunk_label = f" (chunk {chunk_idx + 1}/{total_chunks})" if is_chunked else ""
        page_json = {
            "title": existing_page_meta.get("title", ""),
            "is_public": existing_page_meta.get("is_public", True),
            "data": existing_page_meta.get("data", {}),
            "blocks": existing_blocks,
        }

        # ───── 텍스트 정책 + 디자인 정책을 명확히 분리해서 명시 ─────
        # 이렇게 분리하지 않으면 AI 가 "보존 모드" 신호를 디자인까지 보수적으로 해석한다.
        preserve_content = user_input.get("preserve_content", False)
        if preserve_content:
            text_policy = [
                "### [텍스트 콘텐츠 — 보존]",
                "- 기존 텍스트(content, headline, label, subline, description, title 등) 의 **모든 의미·정보·뉘앙스를 유지**하라.",
                "- 표현은 컨셉에 맞게 다듬을 수 있지만 **정보가 빠져선 안 된다**. 압축은 가독성 향상 목적으로만.",
                "- **줄바꿈(\\n)·공백·들여쓰기·문단 구조를 그대로 유지**하라. 한 줄로 합치거나 공백을 지우지 마라.",
                "",
            ]
        else:
            text_policy = [
                "### [텍스트 콘텐츠 — 자유 작성]",
                "- 텍스트(label, headline, content 등) 를 컨셉에 맞게 자유롭게 다시 써도 된다.",
                "- 단 핵심 실데이터(URL/이미지/연락처) 는 placeholder 로 보호되므로 그 부분은 유지.",
                "",
            ]

        design_policy = [
            "### [디자인 — 극적 변화 권장 / CSS 필수 작성]",
            "텍스트와는 별개로 **시각적 정체성은 컨셉에 맞게 완전히 새로 설계**하라. 보수적으로 가지 마라.",
            "",
            "**디자인은 다음 3 가지 레이어가 모두 채워질 때 완성된다 — 셋 다 반드시 작성**:",
            "1. ``page.data.design_settings`` (전체 교체) — 배경·버튼색·폰트·강조색.",
            "2. ``page.custom_css`` (**비워두지 마라**) — 페이지 전역 톤의 마지막 한 끗. body 배경 그래디언트/패턴, font-feature-settings, 전역 typography 같은 한두 줄로도 충분. 빈 문자열로 응답하면 백엔드가 무시한다.",
            "   예: ``body{background:radial-gradient(circle at 50% 0%,#1a0033,#0a0a14);font-feature-settings:'ss01';}``",
            "3. 각 블록의 ``custom_css`` — box-shadow, border-radius, backdrop-filter, gradient text, hover 효과 등. 한 블록이 그냥 색만 바뀌는 게 아니라 카드 자체가 변해야 한다.",
            "",
            "추가로:",
            "- 블록 색(``custom_bg_color``, ``custom_text_color``, ``custom_button_color``) 컨셉에 맞게 과감히.",
            "- ``*_layout`` 적극 다양화 (``layout: large``, ``gallery_layout: carousel`` 등).",
            "",
            '**유일한 시각 자제 항목**: 기존 블록에 ``custom_border_color`` 가 없었으면 새로 넣지 마라. ``text_layout: "plain"`` 인 블록은 ``default`` 로 바꾸지 마라 (백엔드가 막는다). 그 외 모든 시각 변화는 자유.',
            "",
            "### [블록 무리 — 같은 디자인 통일]",
            "**연속된 같은 _type 블록 무리(2개 이상)는 같은 시각 디자인을 가져야 한다.** 같은 기능을 가진 카드가 각기 다른 톤이면 페이지가 어수선해진다.",
            "- 예: ``single_link/single_link`` 4개가 연속이면 4개 모두 동일한 ``custom_bg_color``·``custom_text_color``·``custom_button_color``·``custom_css`` 사용.",
            "- 무리 간 구분은 spacer / divider 블록으로 — 무리 안의 개별 차이로 X.",
            "- 백엔드가 후처리로 연속 무리의 시각 스타일을 첫 블록 기준으로 강제 통일한다. AI 가 다양화해도 무시되니 처음부터 통일해서 응답하라.",
            "",
            '**쇼케이스(``layout: "large"``) 남발 금지**: large 카드는 강조 전용이다. 같은 _type 그룹 안에서 large 는 **첫 블록 한 개만** 허용 — 나머지는 ``small``. 백엔드가 그룹 안 2번째 이후 large 를 small 로 강제 강등한다.',
            "",
        ]
        variable_parts += text_policy + design_policy

        # ── 전체 다시 작성(rewrite) = 새 페이지를 설계하듯 — 풍성함 목표 + 카테고리 레시피 ──
        # (이게 없으면 모델이 기존 블록 재스타일에 그쳐 결과가 빈약하다. preserve 모드는
        # 내용 보존이 우선이라 제외 — 디자인 정책만으로 충분.)
        if not preserve_content:
            _infer_bits = [concept, str(existing_page_meta.get("title") or "")]
            for _b in existing_blocks[:10]:
                _d = _b.get("data") or {}
                for _k in ("headline", "label", "content", "subline"):
                    _v = _d.get(_k)
                    if isinstance(_v, str) and _v:
                        _infer_bits.append(_v[:120])
            remake_category = _cat.resolve_category({"concept": " ".join(_infer_bits)[:800]})
            target_blocks = max(len(existing_blocks) + 6, 14)
            variable_parts += [
                "### [전체 다시 작성 — 새 페이지 설계 수준으로 보강 (핵심)]",
                f"기존 블록 재스타일에 **그치지 마라**. 새 페이지를 만들 듯 페이지 전체를 다시 설계하고, "
                f"부족한 섹션을 ``_new: true`` 블록으로 적극 보강해 **최종 {target_blocks}개 이상**이 되게 하라. "
                "기존 블록 몇 개에 색만 입힌 결과는 실패다.",
                "**응답 JSON 에는 `page` 와 `blocks` 배열을 반드시 모두 포함하라 — `blocks` 가 없는 "
                "응답은 실패로 간주된다.**",
                '- **섹션 리듬**: 이모지 섹션 헤더(text, text_layout:"default", headline 한 줄) → 내용 블록들 → spacer 구분선.',
                "- **비주얼 보강**: 새 gallery 블록(images 에 {{image:영문 구체 키워드}} 4장+) — 업종 분위기를 보여줄 것.",
                '- **후기 보강**: text toggle 1개(headline "💬 실제 후기", content 에 `아이디 ★★★★★\\n한줄평` 5~6개, 실명 금지).',
                "- **누락 섹션 보강**: SNS(social), 지도(map — 오프라인 업장이면), 이용안내/FAQ(text toggle), "
                "가격 안내(text plain) 등 아래 카테고리 레시피의 섹션을 참고해 채워라.",
                "- 새 group_link 도 가능: links 항목에 title/url(그럴듯한 실제형)/price/badge + thumbnail_url 은 {{image:키워드}}.",
                "",
                _cat.build_recipe_prompt(remake_category),
            ]

        # 사용자가 리뉴얼에 첨부한 이미지 — 새 블록으로 배치하게 안내.
        # (기존 블록 이미지는 placeholder freeze 로 보호되므로 건드리지 않는다.)
        remake_catalog = user_input.get("image_catalog") or {}
        remake_usable = remake_catalog.get("usable") or []
        if remake_usable:
            rlines = [
                "### [사용자가 첨부한 이미지 — 새 블록으로 반드시 배치]",
                "(리뉴얼하면서 사용자가 올린 이미지다. **가능한 한 모두** 페이지에 배치하라.)",
            ]
            for item in remake_usable:
                n = item.get("n")
                summary = (item.get("summary") or "").strip() or "(요약 없음)"
                use = (item.get("suggested_use") or "general").strip()
                rlines.append(f"{n}. {{{{user_image:{n}}}}} — 요약: {summary} · 추천 위치: {use}")
            rlines += [
                "배치 방법: ``_new: true`` 새 블록의 이미지 슬롯에 {{user_image:N}} 토큰으로.",
                "- **목록의 모든 {{user_image:N}} 을 빠짐없이 배치하라 — 1장도 누락 금지.** "
                '기본은 새 gallery 블록(images 배열에 전부 나열, gallery_layout:"thumbnail") + '
                "필요시 1장을 profile cover_image_url 로도 활용.",
                "- '시술 사진'·'매장 사진' 같은 섹션 헤더를 만들었으면 **바로 아래에 그 gallery** 가 와야 한다(라벨-블록 일치).",
                "- 기존 블록의 [IMG_n] placeholder 는 그대로 두고, 새 블록에만 배치한다.",
                "",
            ]
            remake_mood = (remake_catalog.get("mood_notes") or "").strip()
            if remake_mood:
                rlines += [
                    "### [첨부 이미지에서 읽은 디자인 방향]",
                    remake_mood,
                    "",
                ]
            variable_parts += rlines

        variable_parts += [
            f"### [현재 페이지 — full_restyle 대상{chunk_label}]",
            "URL/이미지/연락처는 placeholder([URL_n]/[IMG_n]/[VIDEO_n]/[CONTACT_n]) 로 주어집니다 — 그대로 echo 또는 생략.",
            "필요하면 ``_new: true`` 로 새 블록 추가, 응답에서 누락하면 그 블록은 삭제됩니다.",
            "새 블록은 text/gallery/single_link/spacer 위주로 — **customer/inquiry 폼은 새로 만들지 마라**"
            "(입력칸 라벨이 영어로 떠 어색하다. 문의 유도가 필요하면 카카오톡 채널 등 외부 링크 small 로). "
            "profile 을 cover/cover_bg 로 바꿀 거면 cover_image_url 을 채울 수 있을 때만(빈 커버=회색 띠).",
            f"```json\n{json.dumps(page_json, ensure_ascii=False, indent=2)}\n```",
            "",
        ]
    elif is_remake and mode == "style_only":
        existing_page_meta = user_input.get("existing_page_meta", {}) or {}
        sample = user_input.get("sample_blocks") or []
        all_ids = user_input.get("all_block_ids") or []
        snapshot = {
            "page": {
                "title": existing_page_meta.get("title", ""),
                "data": existing_page_meta.get("data", {}),
            },
            "sample_blocks": sample,
            "all_block_ids": all_ids,
        }
        variable_parts += [
            "### [현재 페이지 — style_only 대상 (대형 페이지)]",
            "전체 블록은 너무 길어 보내지 않습니다. 처음/중간/끝 일부 블록과 _type별 대표 1개를 샘플로 제공합니다.",
            "응답은 ``block_styles`` 만 — 글로벌 / subtype 별 / _by_id 개별 override.",
            "구조(순서/추가/삭제/타입) 는 절대 바꿀 수 없습니다.",
            f"```json\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}\n```",
            "",
        ]
    elif is_remake:
        # legacy 호환 — 구 bio_remake_with_existing.
        existing_page_meta = user_input.get("existing_page_meta", {}) or {}
        page_json = {
            "title": existing_page_meta.get("title", ""),
            "is_public": existing_page_meta.get("is_public", True),
            "data": existing_page_meta.get("data", {}),
            "blocks": user_input.get("existing_blocks") or [],
        }
        variable_parts += [
            "### [현재 페이지 구조 - 리메이크 대상]",
            "아래는 현재 페이지의 블록 구조입니다. 이 구조를 기반으로 사용자 요청에 맞게 리메이크해주세요.",
            "기존 링크 URL, 연락처 등 핵심 데이터는 유지하되 디자인·색상·레이아웃·문구를 개선하세요.",
            f"```json\n{json.dumps(page_json, ensure_ascii=False, indent=2)}\n```",
            "",
        ]

    # 사용자 업로드 이미지 카탈로그 (라벨링 단계가 채운 image_catalog). 새-생성 한정.
    # 캐시 프리픽스 무결성을 위해 요청별로 달라지는 이 내용은 반드시 가변부에 둔다.
    if not is_remake:
        image_catalog = user_input.get("image_catalog") or {}
        usable = image_catalog.get("usable") or []
        mood_notes = (image_catalog.get("mood_notes") or "").strip()
        if usable:
            lines = ["### [사용 가능한 사용자 이미지 — 반드시 활용]"]
            for item in usable:
                n = item.get("n")
                summary = (item.get("summary") or "").strip() or "(요약 없음)"
                use = (item.get("suggested_use") or "general").strip()
                lines.append(f"{n}. {{{{user_image:{n}}}}} — 요약: {summary} · 추천 위치: {use}")
            lines.append(
                "(위 번호를 {{user_image:N}} 형식으로 적절한 블록의 image_url 에 배치하라.)"
            )
            lines.append("")
            variable_parts += lines
        if mood_notes:
            variable_parts += [
                "### [컨셉 이미지에서 읽은 디자인 방향 — 사이트 성격까지 반영]",
                "(사용자가 올린 컨셉/참고 이미지를 분석한 디자인 방향이다. 이 이미지들은 페이지에 "
                "배치하지 말되, **색감뿐 아니라 사이트 성격·업종 느낌·레이아웃/구성 스타일·브랜드 톤을 "
                "이 방향에 맞춰** 전체 디자인과 블록 구성을 설계하라.)",
                mood_notes,
                "",
            ]
        # 비전 라벨러가 이미지에서 직접 읽은 색(#hex). prose(mood_notes)보다 구체적이라
        # design_settings 색을 이 값에 맞추라고 명시 — 색감 재현 정확도를 높이는 핵심.
        palette = image_catalog.get("palette") or {}
        if palette:
            plines = [
                "### [이미지에서 추출한 색 팔레트 — 이 #hex 를 그대로 design_settings 에 써라]",
                "(이미지 픽셀에서 결정적으로 추출한 실제 색이다. 무드 설명보다 **이 구체 #hex 를 최우선**하고, "
                "비슷한 색으로 바꾸지 마라. 본문 글자색은 배경 대비로 자동 결정되니 textColor 는 넣지 않는다.)",
            ]
            if design_lead == "concept_image":
                plines.append(
                    "**⚠️ 이 팔레트가 이 페이지 디자인의 유일한 색 기준이다** — 카테고리의 통상적 "
                    "색 취향(크림/베이지/파스텔 등)이나 예시 페이지의 색은 전부 무시하고, 배경/카드/"
                    "버튼/custom_css 그래디언트까지 모두 이 #hex 계열로만 설계하라. 사용자가 이미지를 "
                    "올린 이유는 **이 색감을 원해서**다."
                )
            label_map = [
                ("background", "backgroundColor (= frameBackgroundColor 동일값)"),
                ("surface", "blockBgColor (카드 배경)"),
                ("accent", "buttonColor (단 하나의 강조색 — 버튼/소셜/뱃지)"),
            ]
            for key, label in label_map:
                if palette.get(key):
                    plines.append(f"- {label}: {palette[key]}")
            if palette.get("brightness"):
                plines.append(f"- 전체 밝기: {palette['brightness']} (배경을 이 방향으로 분명하게)")
            if palette.get("dominant_colors"):
                plines.append(f"- 이미지 주요 색(참고): {', '.join(palette['dominant_colors'])}")
            plines.append("")
            variable_parts += plines

        # 레퍼런스 스크린샷에서 읽은 구조 — 히어로/블록순서/카드스타일을 원본에 맞춘다.
        structure = image_catalog.get("structure") or {}
        if structure and design_lead == "concept_image":
            # 구조까지 읽혔다 = 사용자가 올린 건 **완성 페이지 시안/스크린샷**일 가능성이
            # 높다. 색만 따라하는 게 아니라 디자인 시스템 전체를 재현하라고 못 박는다.
            variable_parts += [
                "### [⚠️ 참고 이미지는 사용자가 원하는 '완성 페이지 시안'이다 — 재현이 목표]",
                "사용자가 이 이미지를 올린 이유는 **이렇게 생긴 페이지를 원해서**다. 영감 수준이 "
                "아니라 **재현**하라: 배경/카드/버튼 색(위 팔레트 #hex), 밝기(다크/라이트), "
                "포인트 색 사용 방식(뱃지/CTA 강조), 히어로 구성, 블록 흐름까지 시안과 같은 "
                "인상이 나와야 한다. 카테고리의 통상적 디자인으로 회귀하지 마라.",
                "",
            ]
        if structure:
            slines = ["### [참고 이미지에서 읽은 구조 — 블록 구성/순서를 이에 맞춰라]"]
            hero = structure.get("hero")
            if hero:
                hero_map = {
                    "cover": '풀블리드 커버 히어로 (profile_layout: "cover_bg" + cover_image_url)',
                    "avatar": "원형 프로필 중심 (profile_layout: center/left)",
                    "none": "별도 히어로 없음",
                }
                slines.append(f"- 히어로: {hero_map.get(hero, hero)}")
            if structure.get("card_style"):
                slines.append(f"- 카드 스타일: {structure['card_style']}")
            if structure.get("block_order"):
                slines.append(
                    "- 관찰된 섹션 순서(위→아래): "
                    + " → ".join(structure["block_order"])
                    + " (이 흐름을 최대한 따르되 사용자 콘텐츠에 맞게 보정)"
                )
            slines.append("")
            variable_parts += slines

        # 참고 이미지에서 읽은 실제 글자(OCR) — 페이지 콘텐츠로 활용.
        text_content = image_catalog.get("text_content") or {}
        if text_content:
            tlines = [
                "### [참고 이미지에서 읽은 실제 텍스트 — 페이지 콘텐츠로 활용]",
                "(아래는 참고 이미지에 실제로 적혀 있던 문구다. 이 내용을 페이지의 실제 콘텐츠로 써라: "
                "title→프로필 headline, tagline→subline, buttons→CTA 버튼 라벨, items→카드/링크 제목. "
                "필요한 짧은 보조 설명은 새로 작성 가능하되 위 문구는 최대한 살려라.)",
            ]
            if text_content.get("title"):
                tlines.append(f"- 제목: {text_content['title']}")
            if text_content.get("tagline"):
                tlines.append(f"- 슬로건: {text_content['tagline']}")
            if text_content.get("buttons"):
                tlines.append("- 버튼: " + " / ".join(text_content["buttons"]))
            if text_content.get("items"):
                tlines.append("- 항목: " + " / ".join(text_content["items"]))
            tlines.append("")
            variable_parts += tlines

    goal_text = concept if concept else "링크인바이오 페이지를 만들어줘."
    if using_reference:
        goal_text = (
            "위 레퍼런스 페이지가 **디자인의 유일한 기준**이다 — design_settings 색, 카드 스타일, "
            "폰트, 블록 구성 흐름을 레퍼런스 그대로 따라 하되, 콘텐츠(문구/링크/이미지 키워드)만 "
            f"아래 목표에 맞게 바꿔라.\n{goal_text}"
        )
    variable_parts += [
        "### [목표]",
        goal_text,
    ]

    user_prompt = "\n".join(fixed_parts + variable_parts)

    return system_prompt, user_prompt
