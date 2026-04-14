"""
프롬프트 조립.

룰셋(md) + few-shot 예시(json) + 사용자 입력을 합쳐
system / user 메시지를 생성한다.
"""

import json
import logging
import os
from pathlib import Path

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


# ─── job_type별 시스템 프롬프트 ───────────────────────────────

# job_type → examples 디렉토리 이름 매핑
_EXAMPLE_DIR_MAP: dict[str, str] = {
    "bio_remake": "bio",
    "theme_generation": "bio",
    "copy_generation": "bio",
}

_SYSTEM_PROMPTS: dict[str, str] = {
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
}


def build_prompts(
    job_type: str,
    user_input: dict,
) -> tuple[str, str]:
    """
    (system_prompt, user_prompt) 튜플을 반환한다.

    Args:
        job_type: 작업 유형 (bio_remake 등)
        user_input: 프론트에서 받은 사용자 입력
            - concept: 페이지 컨셉 설명
            - style: 원하는 스타일 (optional)
            - reference_text: 참고 텍스트 (optional)
    """
    # 1) System prompt
    system_file = _read_asset(f"prompts/{job_type}/system.md")
    system_prompt = system_file if system_file else _SYSTEM_PROMPTS.get(job_type, _SYSTEM_PROMPTS["bio_remake"])

    # 2) 블록 규칙 로드
    block_rules = _read_asset("rules/block_rules.md")

    # 3) few-shot 예시 로드
    example_dir = _EXAMPLE_DIR_MAP.get(job_type, "bio")
    examples = _load_examples(example_dir, max_count=4)

    # 4) 사용자 입력 조합
    concept = user_input.get("concept", "")
    style = user_input.get("style", "")
    reference = user_input.get("reference_text", "")

    user_parts = [
        "### [목표]",
        concept if concept else "링크인바이오 페이지를 만들어줘.",
        "",
    ]

    if style:
        user_parts += [f"### [스타일]\n{style}", ""]

    if reference:
        user_parts += [f"### [참고 자료]\n{reference}", ""]

    user_parts += [
        "### [이미지 URL 규칙 - 매우 중요!]",
        "- 실제 URL을 넣지 말 것!",
        "- 반드시 {{image:영문_검색어}} 형식으로 작성",
        "- 예시:",
        "  배너 이미지 → {{image:jpop band stage concert}}",
        "  앨범 커버 → {{image:music album cover aesthetic}}",
        "  프로필 → {{image:band member portrait}}",
        "",
    ]

    if block_rules:
        user_parts += [f"### [블록 규칙]\n{block_rules}", ""]

    if examples:
        user_parts += [f"### [예시 JSON]\n{examples}", ""]

    user_parts += ["### [출력]", "설명 없이 JSON만 출력"]

    user_prompt = "\n".join(user_parts)

    return system_prompt, user_prompt
