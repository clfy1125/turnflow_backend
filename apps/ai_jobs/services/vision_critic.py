"""스크린샷 기반 디자인 비평 + 보정 루프 (VLM-as-judge).

연구 근거(요약):
  - LLM 의 **자기 비평(intrinsic self-correction)** 은 오히려 품질을 떨어뜨린다(DeepMind 2310.01798).
    렌더된 **스크린샷**은 모델 밖의 **외부 신호**라, 이걸 보고 고치는 건 실제로 효과가 있다.
  - 비평은 **독립된 모델**(생성기와 다른)로, **per-axis 루브릭** + **구체적 수정안**으로(점수만 X).
  - 반복은 1~2회로 제한(과편집/진동 방지), 효과는 1회차에 집중.

설계:
  - 생성기는 deepseek(텍스트), 비평기는 gemma-4(비전, base64) — 자연스럽게 독립.
  - 비평기가 보는 건 실제 렌더 픽셀. 출력은 **디자인 패치**(design_settings + page.custom_css)로
    한정 — 안전하고, 적용 후 design_guard(대비 가드)를 다시 통과시켜 회귀를 막는다.
  - 렌더는 호출자가 콜백(``render_png``)으로 주입한다(Celery=프리뷰페이지+capture_page_snapshot,
    실험 하네스=라이브 슬러그). 이 모듈은 렌더 방법에 의존하지 않는다.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field

from .design_guard import enforce_design_quality
from .llm_client import call_llm_messages_with_usage
from .parsers import extract_json

logger = logging.getLogger(__name__)

# 비전 가능한 자체호스팅 모델 — 무료. 필요시 openai 비전 모델로 교체 가능.
DEFAULT_CRITIC_MODEL = "gemma-4"

_AXES = ("content", "design", "image", "readability")

CRITIC_SYSTEM = """너는 링크인바이오 페이지를 평가하는 **시니어 디자인 리뷰어**다. 주어진 것은 모바일(375px)
페이지의 **실제 렌더 스크린샷**이다. 픽셀을 보고 아래 4개 축을 1~5점으로 평가하고, 문제는 **구체적
수정안**과 함께 적는다(막연한 점수 금지). 그리고 디자인 패치(색/CSS)를 제안한다.

# 평가 축 (각 1~5, 5가 최고)
1. content   — 컨셉에 맞는 적절한 내용/블록 구성인가.
2. design    — 색 조화·대비·여백·위계가 세련됐나. (무지개색/저대비/중간톤배경/AI슬롭 보라 = 감점)
3. image     — 이미지가 관련 있고 잘 배치됐나. (빈 방/무의미 스톡/중복/빈 썸네일 = 감점)
4. readability — 글이 과하지 않고 읽기 쉬운가. **단 청첩장·돌잔치·초대장·공지처럼 글이 본질인
                 페이지는 글이 많아도 감점하지 마라.**

# 색 규칙(중요)
- 본문 글자색은 backgroundColor 대비로 자동 결정된다 — textColor 는 의미 없음.
- 패치는 design_settings 의 backgroundColor/frameBackgroundColor/blockBgColor/buttonColor/
  buttonShape/fontFamily 와 page_custom_css 만. (개별 블록은 건드리지 않는다.)
- 바꿀 키만 넣어라. 멀쩡한 건 빼라(과편집 금지). buttonColor 는 단 하나의 강조색.

# 출력 — JSON 만 (코드펜스/설명 금지)
{
  "reasoning": "무엇이 좋고 무엇이 문제인지 2~4문장(스크린샷 근거).",
  "scores": {"content": n, "design": n, "image": n, "readability": n},
  "findings": [{"axis": "design", "severity": "high|med|low", "problem": "...", "fix": "..."}],
  "design_patch": {"backgroundColor": "#...", "buttonColor": "#...", "page_custom_css": "..."},
  "stop": true|false
}
- high severity 문제가 없으면 stop=true, design_patch 는 {} 로.
- fontFamily 는 Pretendard / Noto Sans KR / IBM Plex Sans KR / Nanum Gothic / Nanum Myeongjo 중에서만."""

_PATCH_DS_KEYS = (
    "backgroundColor",
    "frameBackgroundColor",
    "blockBgColor",
    "buttonColor",
    "buttonShape",
    "fontFamily",
)
_ALLOWED_FONTS = {
    "Pretendard",
    "Noto Sans KR",
    "IBM Plex Sans KR",
    "Nanum Gothic",
    "Nanum Myeongjo",
}
_ALLOWED_SHAPES = {"rounded", "pill", "square"}


@dataclass
class Critique:
    scores: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    design_patch: dict = field(default_factory=dict)
    reasoning: str = ""
    stop: bool = True
    raw: str = ""
    model: str = ""

    @property
    def total(self) -> int:
        return sum(int(self.scores.get(a, 0) or 0) for a in _AXES)

    @property
    def high_severity_count(self) -> int:
        return sum(1 for f in self.findings if (f or {}).get("severity") == "high")


def _png_to_data_uri(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def critique_screenshot(
    png_bytes: bytes,
    *,
    concept: str = "",
    has_user_images: bool = False,
    model: str = DEFAULT_CRITIC_MODEL,
    max_tokens: int = 1200,
) -> Critique:
    """렌더 스크린샷을 비전 모델로 비평. 실패는 비치명적(빈 Critique, stop=True)."""
    brief = (concept or "").strip() or "(컨셉 설명 없음)"
    user_text = (
        f"[페이지 컨셉]\n{brief}\n\n"
        f"[사용자 업로드 이미지 사용됨] {'예' if has_user_images else '아니오'}\n\n"
        "아래 스크린샷을 보고 위 스키마(JSON)로만 평가하라."
    )
    messages = [
        {"role": "system", "content": CRITIC_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": _png_to_data_uri(png_bytes)}},
            ],
        },
    ]
    try:
        res = call_llm_messages_with_usage(
            model=model, messages=messages, max_tokens=max_tokens, temperature=0.1
        )
    except Exception:  # noqa: BLE001
        logger.warning("vision_critic: 비평 호출 실패 — 보정 건너뜀", exc_info=True)
        return Critique(stop=True)

    try:
        parsed = extract_json(res.content)
        if not isinstance(parsed, dict):
            raise ValueError("dict 아님")
    except Exception:  # noqa: BLE001
        logger.warning("vision_critic: 비평 JSON 파싱 실패 — 보정 건너뜀")
        return Critique(stop=True, raw=res.content, model=res.model)

    scores = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
    findings = parsed.get("findings") if isinstance(parsed.get("findings"), list) else []
    patch = _sanitize_patch(parsed.get("design_patch"))
    return Critique(
        scores={a: scores.get(a) for a in _AXES},
        findings=[f for f in findings if isinstance(f, dict)][:8],
        design_patch=patch,
        reasoning=str(parsed.get("reasoning") or "")[:600],
        stop=bool(parsed.get("stop", not patch)),
        raw=res.content,
        model=res.model,
    )


def _sanitize_patch(patch) -> dict:
    """비평기가 낸 패치에서 허용 키/값만 남긴다 (환각/위험값 차단)."""
    from . import color_utils as C

    if not isinstance(patch, dict):
        return {}
    out: dict = {}
    for k in _PATCH_DS_KEYS:
        v = patch.get(k)
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        if k == "fontFamily":
            if v in _ALLOWED_FONTS:
                out[k] = v
        elif k == "buttonShape":
            if v in _ALLOWED_SHAPES:
                out[k] = v
        elif C.is_hex(v):  # 색 키
            out[k] = v
    css = patch.get("page_custom_css")
    if isinstance(css, str) and css.strip() and len(css) <= 2000:
        out["page_custom_css"] = css.strip()
    return out


def apply_design_patch(result: dict, patch: dict, *, palette: dict | None = None) -> dict:
    """디자인 패치를 result_json 에 머지하고 design_guard(대비 가드)를 재적용한다."""
    if not isinstance(result, dict) or not patch:
        return result
    data = result.setdefault("data", {})
    if not isinstance(data, dict):
        result["data"] = data = {}
    ds = data.setdefault("design_settings", {})
    if not isinstance(ds, dict):
        data["design_settings"] = ds = {}

    for k in _PATCH_DS_KEYS:
        if k in patch:
            ds[k] = patch[k]
    if "page_custom_css" in patch:
        result["custom_css"] = patch["page_custom_css"]

    # 패치 후 대비/슬롭 가드 재적용 — 비평기가 저대비 색을 제안해도 안전하게.
    return enforce_design_quality(result, palette=palette or {})


def refine_result_json(
    result: dict,
    *,
    render_png,
    apply_fn,
    concept: str = "",
    has_user_images: bool = False,
    palette: dict | None = None,
    max_cycles: int = 1,
    model: str = DEFAULT_CRITIC_MODEL,
) -> tuple[dict, list]:
    """스크린샷 비평 루프. keep-best(개선될 때만 패치 채택).

    Args:
        result: 현재 result_json (이미 한 번 적용/렌더 가능한 상태로 들어온다고 가정).
        render_png: ``() -> bytes`` — 현재 적용된 페이지의 스크린샷 PNG 바이트.
        apply_fn: ``(result_json) -> None`` — result_json 을 렌더 대상(프리뷰 페이지)에 적용.
        max_cycles: 최대 비평·보정 반복 (권장 1~2).

    Returns:
        (result_json, log) — log 는 각 사이클의 점수/채택 여부.
    """
    log: list = []
    current = result
    apply_fn(current)  # 초기 상태 보장

    for cycle in range(max_cycles):
        try:
            png = render_png()
        except Exception:  # noqa: BLE001
            logger.warning("vision_critic: 렌더 실패 — 루프 종료", exc_info=True)
            break
        if not png:
            break

        crit = critique_screenshot(
            png, concept=concept, has_user_images=has_user_images, model=model
        )
        entry = {
            "cycle": cycle,
            "scores": crit.scores,
            "total": crit.total,
            "high": crit.high_severity_count,
            "reasoning": crit.reasoning,
            "patch_keys": sorted(crit.design_patch.keys()),
            "applied": False,
        }

        # high severity 문제가 없으면 보정 생략 — 이미 충분히 좋다. 비싼 재렌더(+비평)를
        # 아끼고(루프 지연의 주범), cosmetic 패치로 멀쩡한 페이지를 건드릴 위험도 피한다.
        # 진짜 문제(high)가 있을 때만 패치 사이클을 돈다.
        if crit.stop or not crit.design_patch or crit.high_severity_count == 0:
            log.append(entry)
            break

        # 후보 패치 적용 → 재렌더 → 재비평. design 점수가 떨어지지 않으면 채택.
        before_total = crit.total
        candidate = apply_design_patch(
            json.loads(json.dumps(current)), crit.design_patch, palette=palette
        )
        apply_fn(candidate)
        try:
            png2 = render_png()
        except Exception:  # noqa: BLE001
            png2 = None
        if png2:
            crit2 = critique_screenshot(
                png2, concept=concept, has_user_images=has_user_images, model=model
            )
            if crit2.total >= before_total:
                current = candidate  # 개선 → 채택
                entry["applied"] = True
                entry["after_total"] = crit2.total
                log.append(entry)
                if crit2.stop:
                    break
                continue
        # 개선 아님 → 롤백(이전 current 유지)
        apply_fn(current)
        entry["after_total"] = "reverted"
        log.append(entry)
        break

    return current, log
