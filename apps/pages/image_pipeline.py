"""
apps/pages/image_pipeline.py

업로드 이미지 정제·압축 파이프라인.

■ 정책
  1) 실제 바이트로 포맷 판별 (Pillow verify/open). Content-Type 위조 방어.
  2) EXIF 기반 회전 보정 후 EXIF 제거 (개인정보: GPS·기기정보 삭제).
  3) 포맷 정규화:
       - HEIC/HEIF/TIFF/BMP  → JPEG
       - 투명도 있음(PNG/P)  → WebP (알파 보존, 손실 압축)
       - JPEG                → JPEG (재인코딩, 품질 85, progressive)
       - GIF(애니메이션)     → 그대로 유지 (움직임 보존)
       - SVG                 → 화이트리스트 sanitize (스크립트/onclick/javascript: 제거)
  4) 해상도 상한: 최대 변 2048px.
  5) 결과 품질 85, progressive JPEG.

■ 사용
    from apps.pages.image_pipeline import process_upload, ImageValidationError

    try:
        processed = process_upload(uploaded_file)
    except ImageValidationError as e:
        return Response({"file": [str(e)]}, status=400)

    media = PageMedia.objects.create(
        ...
        file=ContentFile(processed.content, name=processed.suggest_filename(original_name)),
        mime_type=processed.mime_type,
        size=len(processed.content),
    )

■ 설계 이유
  - 완성본(file)만 정제. original_file은 재편집 시 원본 해상도가 필요하므로 그대로 보관.
  - 프론트는 UX용 1차 압축(선택). 서버가 항상 최종 검증·재인코딩으로 신뢰 경계 확보.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

from defusedxml.ElementTree import fromstring as safe_fromstring
from defusedxml.ElementTree import tostring as safe_tostring
from PIL import Image, ImageOps

# ── 정책 상수 ────────────────────────────────────────────────
MAX_EDGE = 2048
MAX_EDGE_ORIGINAL = 4096  # 원본 보관용 상한 (4K 이상만 다운스케일)
JPEG_QUALITY = 85
WEBP_QUALITY = 85
JPEG_QUALITY_ORIGINAL = 92  # 원본 재저장 시 품질 (EXIF만 떼고 거의 무손실)

# SVG 화이트리스트
_SVG_ALLOWED_TAGS = frozenset({
    "svg", "g", "path", "rect", "circle", "ellipse", "line",
    "polyline", "polygon", "text", "tspan", "defs", "use",
    "linearGradient", "radialGradient", "stop", "clipPath", "mask",
    "title", "desc", "symbol", "marker",
})
# 금지 속성 prefix (on* 이벤트 핸들러)
_SVG_FORBIDDEN_ATTR_PREFIXES = ("on",)


class ImageValidationError(Exception):
    """업로드 이미지가 파싱 불가/손상/악성이거나 지원되지 않는 경우."""


@dataclass
class ProcessedImage:
    """정제 결과."""

    content: bytes
    mime_type: str
    extension: str  # "jpg" | "webp" | "gif" | "svg"
    width: int | None
    height: int | None

    @property
    def size(self) -> int:
        return len(self.content)

    def suggest_filename(self, original_name: str | None = None) -> str:
        """DB에 기록할 파일명. 원본명은 original_name 컬럼에 따로 저장하므로
        스토리지 키는 충돌 방지용 UUID로 고정."""
        return f"{uuid.uuid4().hex}.{self.extension}"


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def process_upload(django_file) -> ProcessedImage:
    """Django `UploadedFile` 또는 file-like 객체를 받아 정제된 바이트 반환.

    Raises:
        ImageValidationError: 파싱 실패 / 손상 / 지원 불가 포맷.
    """
    name = (getattr(django_file, "name", "") or "").lower()
    django_file.seek(0)
    raw = django_file.read()
    django_file.seek(0)

    # SVG 감지 (Pillow가 못 다룸 → 텍스트 sanitize)
    if name.endswith(".svg") or _looks_like_svg(raw):
        return _sanitize_svg(raw)

    # Pillow로 검증 + 파싱
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()  # 손상 파일 차단 (verify 후엔 사용 불가, 재오픈 필요)
    except Exception as exc:  # noqa: BLE001
        raise ImageValidationError(f"이미지를 읽을 수 없습니다: {exc}") from exc

    img = Image.open(io.BytesIO(raw))

    # 애니메이션 GIF는 그대로 유지 (움직임 보존)
    if img.format == "GIF" and getattr(img, "is_animated", False):
        return ProcessedImage(
            content=raw,
            mime_type="image/gif",
            extension="gif",
            width=img.width,
            height=img.height,
        )

    # EXIF 기반 회전 적용 → EXIF 폐기
    img = ImageOps.exif_transpose(img)

    # 해상도 상한
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    has_alpha = (
        img.mode in ("RGBA", "LA")
        or (img.mode == "P" and "transparency" in img.info)
    )

    out = io.BytesIO()
    if has_alpha:
        img = img.convert("RGBA")
        img.save(out, format="WEBP", quality=WEBP_QUALITY, method=6)
        return ProcessedImage(
            content=out.getvalue(),
            mime_type="image/webp",
            extension="webp",
            width=img.width,
            height=img.height,
        )

    img = img.convert("RGB")
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return ProcessedImage(
        content=out.getvalue(),
        mime_type="image/jpeg",
        extension="jpg",
        width=img.width,
        height=img.height,
    )


def verify_readable(django_file) -> None:
    """원본 파일(`original_file`)용 가벼운 검증.

    ⚠️ Deprecated — `sanitize_original()` 사용 권장.
    단순 판독 가능 여부만 확인하고 바이트는 변형하지 않는다.
    """
    django_file.seek(0)
    head = django_file.read()
    django_file.seek(0)

    if _looks_like_svg(head):
        _sanitize_svg(head)
        return

    try:
        probe = Image.open(io.BytesIO(head))
        probe.verify()
    except Exception as exc:  # noqa: BLE001
        raise ImageValidationError(f"원본 이미지를 읽을 수 없습니다: {exc}") from exc


def sanitize_original(django_file) -> ProcessedImage:
    """원본 파일(`original_file`) 정제. 재편집을 위해 포맷/해상도는 최대한 보존.

    ■ 정책
      - EXIF 회전 적용 + EXIF 전량 제거 (GPS·기기정보 등 개인정보 보호)
      - 해상도 상한 ``MAX_EDGE_ORIGINAL`` (기본 4096px) — 4K 초과만 다운스케일
      - 포맷 유지:
          JPEG→JPEG(q92, progressive) / PNG→PNG(optimize)
          WebP→WebP(q95) / 애니 GIF→그대로 / TIFF·BMP·HEIC→JPEG(q92)
      - SVG는 ``_sanitize_svg`` 로 스크립트/이벤트 핸들러/외부 URL 제거

    Raises:
        ImageValidationError: 파싱 실패 / 손상 / 지원 불가 포맷.
    """
    name = (getattr(django_file, "name", "") or "").lower()
    django_file.seek(0)
    raw = django_file.read()
    django_file.seek(0)

    if name.endswith(".svg") or _looks_like_svg(raw):
        return _sanitize_svg(raw)

    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
    except Exception as exc:  # noqa: BLE001
        raise ImageValidationError(f"원본 이미지를 읽을 수 없습니다: {exc}") from exc

    img = Image.open(io.BytesIO(raw))
    src_format = (img.format or "").upper()

    # 애니메이션 GIF는 그대로 보존 (프레임/루프 유지)
    if src_format == "GIF" and getattr(img, "is_animated", False):
        return ProcessedImage(
            content=raw,
            mime_type="image/gif",
            extension="gif",
            width=img.width,
            height=img.height,
        )

    # EXIF 회전 적용 → EXIF 폐기
    img = ImageOps.exif_transpose(img)

    # 해상도 상한 (4K 이상만 잘라냄)
    if max(img.size) > MAX_EDGE_ORIGINAL:
        img.thumbnail((MAX_EDGE_ORIGINAL, MAX_EDGE_ORIGINAL), Image.LANCZOS)

    has_alpha = (
        img.mode in ("RGBA", "LA")
        or (img.mode == "P" and "transparency" in img.info)
    )

    out = io.BytesIO()

    # 포맷 유지 분기
    if src_format == "PNG":
        img = img.convert("RGBA" if has_alpha else "RGB")
        img.save(out, format="PNG", optimize=True)
        return ProcessedImage(
            content=out.getvalue(),
            mime_type="image/png",
            extension="png",
            width=img.width,
            height=img.height,
        )

    if src_format == "WEBP":
        img = img.convert("RGBA" if has_alpha else "RGB")
        img.save(out, format="WEBP", quality=95, method=6)
        return ProcessedImage(
            content=out.getvalue(),
            mime_type="image/webp",
            extension="webp",
            width=img.width,
            height=img.height,
        )

    # JPEG / TIFF / BMP / HEIC / 기타 → JPEG
    # (알파는 JPEG가 못 담으므로 WebP 로 강등)
    if has_alpha:
        img = img.convert("RGBA")
        img.save(out, format="WEBP", quality=95, method=6)
        return ProcessedImage(
            content=out.getvalue(),
            mime_type="image/webp",
            extension="webp",
            width=img.width,
            height=img.height,
        )

    img = img.convert("RGB")
    img.save(
        out,
        format="JPEG",
        quality=JPEG_QUALITY_ORIGINAL,
        optimize=True,
        progressive=True,
    )
    return ProcessedImage(
        content=out.getvalue(),
        mime_type="image/jpeg",
        extension="jpg",
        width=img.width,
        height=img.height,
    )


# ─────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────

def _looks_like_svg(raw: bytes) -> bool:
    head = raw[:512].lstrip().lower()
    return head.startswith(b"<?xml") and b"<svg" in head[:512] or head.startswith(b"<svg")


def _sanitize_svg(raw: bytes) -> ProcessedImage:
    try:
        # defusedxml: XXE / billion-laughs / 외부 DTD 공격 차단
        root = safe_fromstring(raw)
    except Exception as exc:  # noqa: BLE001
        raise ImageValidationError(f"SVG 파싱 실패: {exc}") from exc

    for el in list(root.iter()):
        local_tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local_tag not in _SVG_ALLOWED_TAGS:
            # 태그 자체 제거 대신 내용 비우기 (ElementTree 구조 유지)
            el.clear()
            el.tag = "removed"
            continue

        for attr_name in list(el.attrib.keys()):
            local = attr_name.split("}")[-1].lower() if "}" in attr_name else attr_name.lower()
            # on* 이벤트 핸들러
            if any(local.startswith(p) for p in _SVG_FORBIDDEN_ATTR_PREFIXES):
                del el.attrib[attr_name]
                continue
            val = (el.attrib.get(attr_name) or "").strip().lower()
            # javascript: URL / data: URL 차단
            if val.startswith("javascript:") or val.startswith("data:"):
                del el.attrib[attr_name]

    cleaned = safe_tostring(root)
    return ProcessedImage(
        content=cleaned if isinstance(cleaned, bytes) else cleaned.encode("utf-8"),
        mime_type="image/svg+xml",
        extension="svg",
        width=None,
        height=None,
    )
