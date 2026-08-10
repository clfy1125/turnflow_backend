"""진행 단계 계약 — 서버·프론트 공용 단일 소스.

프론트는 `GET /reports/{id}/` 의 `steps[]` 를 그대로 체크리스트로 그리면 된다.
`stage_started_at + stage_expected_seconds` 로 폴링(3초) 사이를 부드럽게 채울 수 있다.

⚠️ 단계 키를 바꾸면 `models.ReportStage` 와 프론트 문서(docs/frontend/INSTA_REPORT_FRONTEND.md)를
   함께 고쳐야 한다. 진행률 구간은 겹치지 않고 0→100 을 정확히 덮는다.
"""

from __future__ import annotations

from django.utils import timezone

from .models import ReportStage, ReportStatus

# key, 사람말 라벨, 진행률 시작, 진행률 끝, 예상 소요(초)
STAGES: list[dict] = [
    {"key": ReportStage.QUEUED, "label": "대기 중", "start": 0, "end": 3, "expected": 10},
    {
        "key": ReportStage.COLLECTING,
        "label": "게시물 모으는 중",
        "start": 3,
        "end": 15,
        "expected": 120,
    },
    {"key": ReportStage.METRICS, "label": "숫자 계산 중", "start": 15, "end": 20, "expected": 5},
    {
        "key": ReportStage.PREPARING,
        "label": "영상 내려받는 중",
        "start": 20,
        "end": 30,
        "expected": 150,
    },
    {
        "key": ReportStage.EXTRACTING,
        "label": "영상 분석 중",
        "start": 30,
        "end": 65,
        "expected": 360,
    },
    {"key": ReportStage.COMMENTS, "label": "댓글 분석 중", "start": 65, "end": 72, "expected": 50},
    {
        "key": ReportStage.SYNTHESIZING,
        "label": "인사이트 쓰는 중",
        "start": 72,
        "end": 88,
        "expected": 260,
    },
    {"key": ReportStage.VERIFYING, "label": "검수하는 중", "start": 88, "end": 93, "expected": 120},
    {
        "key": ReportStage.RENDERING,
        "label": "리포트 만드는 중",
        "start": 93,
        "end": 97,
        "expected": 5,
    },
    {
        "key": ReportStage.EXPORTING,
        "label": "파일로 저장하는 중",
        "start": 97,
        "end": 100,
        "expected": 8,
    },
]

STAGE_BY_KEY = {s["key"]: s for s in STAGES}
STAGE_ORDER = [s["key"] for s in STAGES]

# 평균 총 소요(초) — API 문서·프론트 안내 문구의 근거값. 랩 실측 13~15분과 일치.
AVERAGE_TOTAL_SECONDS = sum(s["expected"] for s in STAGES)


def stage_bounds(stage: str) -> tuple[int, int]:
    s = STAGE_BY_KEY.get(stage)
    return (s["start"], s["end"]) if s else (0, 100)


def stage_label(stage: str) -> str:
    s = STAGE_BY_KEY.get(stage)
    return s["label"] if s else ""


def stage_expected(stage: str) -> int:
    s = STAGE_BY_KEY.get(stage)
    return s["expected"] if s else 0


def interpolate(stage: str, done: int, total: int) -> int:
    """단계 안에서 n/N 만큼 진행된 진행률. (영상 분석처럼 하위 진행이 있는 단계용)"""
    start, end = stage_bounds(stage)
    if total <= 0:
        return start
    ratio = min(max(done / total, 0.0), 1.0)
    return int(round(start + (end - start) * ratio))


def steps_payload(report, scale: float = 1.0) -> list[dict]:
    """프론트 체크리스트용 단계 목록.

    status: done | active | pending | failed
    ``scale`` = 가짜 모드 축소 배율(운영은 1.0) — 클라이언트 보간이 실제 속도와 맞게.
    """
    if report.status == ReportStatus.SUCCEEDED:
        cur_idx = len(STAGE_ORDER)
    else:
        try:
            cur_idx = STAGE_ORDER.index(report.stage)
        except ValueError:
            cur_idx = 0

    out = []
    for idx, s in enumerate(STAGES):
        if idx < cur_idx:
            status = "done"
        elif idx == cur_idx:
            status = "failed" if report.status == ReportStatus.FAILED else "active"
        else:
            status = "pending"
        out.append(
            {
                "key": str(s["key"]),
                "label": s["label"],
                "status": status,
                "detail": report.message if status in ("active", "failed") else "",
                "progress_start": s["start"],
                "progress_end": s["end"],
                "expected_seconds": max(int(round(s["expected"] * scale)), 1),
            }
        )
    return out


def eta_seconds(report, scale: float = 1.0) -> int | None:
    """남은 예상 시간(초). 완료/실패면 None.

    현재 단계의 남은 시간 + 이후 단계 예상치의 합. 현재 단계가 예상을 넘겼으면 0으로 깎는다.
    ``scale`` 은 가짜 모드 축소 배율(service.fake_time_scale) — 서버가 10초에 끝나는데
    "18분 남음" 이라고 표시하지 않기 위한 보정이며, 운영에서는 항상 1.0 이다.
    """
    if report.is_terminal:
        return None
    try:
        idx = STAGE_ORDER.index(report.stage)
    except ValueError:
        idx = 0
    remaining = sum(s["expected"] for s in STAGES[idx + 1 :]) * scale
    cur = STAGES[idx]
    spent = 0
    if report.stage_started_at:
        spent = int((timezone.now() - report.stage_started_at).total_seconds())
    remaining += max(cur["expected"] * scale - spent, 0)
    return int(round(remaining))
