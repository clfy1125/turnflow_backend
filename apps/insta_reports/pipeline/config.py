"""파이프라인 전역 설정 — 경로·모델·단가표 (백엔드판).

랩(`insta_report_lab/pipeline/config.py`)과 이름·의미를 1:1 로 맞춘 어댑터다.
차이는 두 가지뿐:

1) **경로가 잡별로 바뀐다.** 랩은 저장소 안 고정 디렉터리를 썼지만 서버는 리포트마다
   임시 디렉터리를 쓴다 → `bind_run(dir)` 로 바인딩하고, 모듈 속성(`config.RUNS_DIR` 등)은
   PEP 562 `__getattr__` 가 그때그때 해석한다. 파이프라인 모듈 코드는 손대지 않는다.
2) **키·모델명이 Django settings 에서 온다.** (.env → settings → 여기)

⚠️ Celery prefork 는 태스크당 프로세스가 갈리므로 전역 바인딩이 새지 않는다. 그래도
   스레드 실행(테스트 등)에서 안전하도록 ContextVar 로 들고 있는다.
"""

from __future__ import annotations

import contextvars
from pathlib import Path

from django.conf import settings

# ── 잡별 작업 디렉터리 바인딩 ─────────────────────────────────────────
_RUN_DIR: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "insta_report_run_dir", default=None
)

# 속성 이름 → 작업 디렉터리 하위 경로
_DIRS = {
    "RAW_DIR": "raw",  # 공식 Graph 수집 원본 ({username}.json)
    "APIFY_DIR": "apify",  # Apify 수집 원본 ({username}.json)
    "MEDIA_DIR": "media",  # 영상/썸네일 (런 종료 시 파기)
    "FEATURE_DIR": "features",  # 추출 캐시 (DB 캐시를 여기로 warm/flush)
    "SAMPLE_DIR": "samples",
    "RUNS_DIR": "runs",  # posts/metrics/aggregates/slots/costs
    "REPORTS_DIR": "out",  # 렌더된 HTML
}


def bind_run(run_dir: str | Path) -> Path:
    """이 리포트 런이 쓸 작업 디렉터리를 바인딩하고 하위 디렉터리를 만든다."""
    base = Path(run_dir)
    for sub in _DIRS.values():
        (base / sub).mkdir(parents=True, exist_ok=True)
    _RUN_DIR.set(base)
    return base


def current_run_dir() -> Path:
    base = _RUN_DIR.get()
    if base is None:
        raise RuntimeError(
            "insta_reports 파이프라인 경로가 바인딩되지 않았습니다 — config.bind_run(dir) 먼저 호출"
        )
    return base


def __getattr__(name: str):  # PEP 562
    if name in _DIRS:
        return current_run_dir() / _DIRS[name]
    raise AttributeError(name)


# 렌더러 템플릿 (고정 — 잡과 무관)
TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "templates" / "insta_reports" / "report_v3.html.j2"
)

# ── 모델 ─────────────────────────────────────────────────────────────
GEMINI_API_KEY = getattr(settings, "INSTA_REPORT_GEMINI_API_KEY", "")
GEMINI_BASE = getattr(
    settings, "INSTA_REPORT_GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta"
)
EXTRACT_MODEL = getattr(settings, "INSTA_REPORT_EXTRACT_MODEL", "gemini-3.5-flash")

DEEPSEEK_API_KEY = getattr(settings, "INSTA_REPORT_DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = getattr(settings, "INSTA_REPORT_DEEPSEEK_BASE", "https://api.deepseek.com")
# deepseek-v4-pro 는 추론 모델 — 콜당 3~4분, 리포트 1건 13~15분. 문장 품질(한자 혼입·용어
# 뒤섞임 없음)이 flash 보다 확연히 좋아 기본값으로 고정한다. 급하면 settings 로 내릴 수 있다.
SYNTH_MODEL = getattr(settings, "INSTA_REPORT_SYNTH_MODEL", "deepseek-v4-pro")

# ── 단가표 (USD per 1M tokens) ───────────────────────────────────────
# ⚠️ gemini-3.5-flash / deepseek-v4-pro 공식 단가 미확인 → 직전 세대 단가로 추정.
#    토큰 원본을 tokens_json 에 보존하므로 단가 확정 시 여기만 고쳐 전액 재정산 가능.
PRICES = {
    "gemini-3.5-flash": {"in": 0.30, "out": 2.50, "estimated": True},
    "deepseek-v4-pro": {"in": 0.28, "out": 0.42, "estimated": True},
    "deepseek-v4-flash": {"in": 0.14, "out": 0.28, "estimated": True},
}

# 샘플러
SAMPLE_TOP = 10
SAMPLE_BOTTOM = 10
SAMPLE_MID = 5
SAMPLE_RECENT = 5
MATURITY_WINDOW_DAYS = 28  # 경과일 보정 관측창
TOP_MIN_AGE_DAYS = 7  # 상위 후보 최소 경과일 (미성숙 제외)
BOTTOM_MIN_AGE_DAYS = 14  # 하위 후보 최소 경과일 (신생 박제 방지)
CAROUSEL_LIGHT_MAX = 6  # 캐러셀/이미지 경량 분석 상한 (좋아요 상위)

# 추출
EXTRACT_CONCURRENCY = int(getattr(settings, "INSTA_REPORT_EXTRACT_CONCURRENCY", 6))
INLINE_MAX_BYTES = 14 * 1024 * 1024  # 초과 시 Gemini Files API

# 합성/검증
SYNTH_MAX_RETRY = 3  # 재합성 최대 3회(총 4시도)
MIN_CELL_N = 5  # 표본<5 셀 low_sample 플래그
MIN_REELS_FOR_REPORT = 5  # 진입 게이트: 조회수 있는 릴스 <5 → 리포트 거부
