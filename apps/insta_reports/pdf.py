"""렌더된 HTML → PDF (Headless Chromium).

playwright/chromium 은 이미 이미지에 있다(`pages/services/snapshot.py` 와 공유, Dockerfile
`python -m playwright install chromium`). 한글/이모지 글리프는 컨테이너 폰트
(fonts-noto-cjk / fonts-noto-color-emoji)에 의존한다 — 빼면 두부(□)로 나온다.

리포트 HTML 은 자기완결(썸네일 data-URI, Chart.js 인라인)이라 네트워크 없이 렌더된다.
`page.pdf()` 는 print 미디어를 에뮬레이트하므로, 탭 펼치기·페이지 나눔은 템플릿의
`@media print` 규칙이 담당한다(JS 개입 없음).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 템플릿이 <script src="chart.umd.min.js"> 로 상대 참조하므로 HTML 옆에 함께 놓는다.
CHARTJS_PATH = Path(__file__).resolve().parent / "static" / "insta_reports" / "chart.umd.min.js"

PDF_FORMAT = "A4"
PDF_MARGIN = {"top": "10mm", "bottom": "12mm", "left": "8mm", "right": "8mm"}
CHARTS_READY_TIMEOUT_MS = 20_000
LOAD_TIMEOUT_MS = 30_000


class PdfError(RuntimeError):
    """PDF 변환 실패 — 태스크가 error_code=PDF_FAILED 로 기록."""


def html_to_pdf(html: str) -> bytes:
    """자기완결 HTML 문자열을 PDF 바이트로 변환."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - 이미지에는 항상 있음
        raise PdfError("playwright 미설치") from e

    with tempfile.TemporaryDirectory(prefix="instarpt_pdf_") as tmp:
        src = Path(tmp) / "report.html"
        src.write_text(html, encoding="utf-8")
        out = Path(tmp) / "report.pdf"
        if CHARTJS_PATH.exists():
            shutil.copyfile(CHARTJS_PATH, Path(tmp) / CHARTJS_PATH.name)
        else:  # pragma: no cover - 배포 누락 시 차트만 비고 나머지는 정상 출력
            logger.error("insta_report: chart.umd.min.js 누락 — 차트 없이 PDF 생성")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    page = browser.new_page(viewport={"width": 1000, "height": 1400})
                    page.goto(src.as_uri(), wait_until="load", timeout=LOAD_TIMEOUT_MS)
                    # 차트가 캔버스에 그려진 뒤에 인쇄해야 빈 사각형이 안 나온다.
                    try:
                        page.wait_for_function(
                            "window.__chartsReady === true", timeout=CHARTS_READY_TIMEOUT_MS
                        )
                    except PlaywrightTimeout:
                        logger.warning("insta_report: charts ready timeout — 그대로 인쇄")
                    page.emulate_media(media="print")
                    page.pdf(
                        path=str(out),
                        format=PDF_FORMAT,
                        print_background=True,
                        margin=PDF_MARGIN,
                        prefer_css_page_size=False,
                    )
                finally:
                    browser.close()
        except PlaywrightError as e:
            raise PdfError(f"chromium 실패: {type(e).__name__}: {e}") from e
        data = out.read_bytes()

    if len(data) < 1024:
        raise PdfError(f"PDF 가 비정상적으로 작습니다({len(data)}B)")
    return data
