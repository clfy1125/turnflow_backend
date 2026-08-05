"""캠페인 게시물 썸네일(우리 스토리지 사본) 일괄 백필.

## 왜 필요한가
프론트 카드/상세의 게시물 썸네일은 ``AutoDMCampaign.thumbnail_url`` 하나만 본다. 이 컬럼은
2026-08-05 에 신설됐으므로, 그 전에 만들어진 캠페인은 전부 비어 있다 → 배포 직후 한 번 이
명령으로 채운다(이후로는 생성 훅 + 목록조회 기회발행 + 6h 스위퍼가 자동으로 유지).

IG CDN URL 을 저장하지 않고 이미지를 우리 스토리지(R2)에 복사하는 이유는
:mod:`apps.integrations.media_thumbnail` 모듈 docstring 참고 (서명 URL 만료).

## 자리표시자 정리 (--clean-placeholders)
프론트 에디터가 게시물 이미지를 못 찾을 때 하드코딩된 unsplash 사진을 ``media_url`` 로
저장한 시기가 있었다. ``media_url`` 은 지금 **게시물 permalink 저장소**라, 이 값이 박힌 행은
permalink 스위퍼가 "빈값만 채운다" 규칙 때문에 영구히 건너뛴다 → 비워서 정상화한다.
(썸네일 자체는 별 컬럼이므로 이 정리 없이도 정상 동작한다 — 게시물 링크만 못 뜬다.)

사용:
    python manage.py backfill_campaign_thumbnails --dry-run
    python manage.py backfill_campaign_thumbnails --clean-placeholders
    python manage.py backfill_campaign_thumbnails --campaign-id <uuid> --force
"""

import time

from django.core.management.base import BaseCommand

from apps.integrations.models import AutoDMCampaign
from apps.integrations.services import is_instagram_permalink
from apps.integrations.tasks import sync_campaign_thumbnail

# 프론트 에디터가 저장하던 하드코딩 자리표시자 (실제 게시물과 무관한 사진)
PLACEHOLDER_URL_MARKERS = ("images.unsplash.com/photo-1611162617474",)


class Command(BaseCommand):
    help = "캠페인 게시물 썸네일을 우리 스토리지에 재호스팅 (일괄 백필)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="대상만 출력하고 저장 안 함")
        parser.add_argument("--campaign-id", default=None, help="특정 캠페인만 처리")
        parser.add_argument("--limit", type=int, default=0, help="처리 상한 (0=무제한)")
        parser.add_argument(
            "--sleep", type=float, default=0.3, help="건당 대기 초 (IG 레이트리밋 여유)"
        )
        parser.add_argument("--force", action="store_true", help="이미 사본이 있어도 다시 받아온다")
        parser.add_argument(
            "--clean-placeholders",
            action="store_true",
            help="media_url 에 박힌 하드코딩 자리표시자 URL 을 비워 permalink 백필이 가능하게 한다",
        )

    def handle(self, *args, **opts):
        if opts["clean_placeholders"]:
            self._clean_placeholders(dry_run=opts["dry_run"])

        qs = AutoDMCampaign.objects.select_related("ig_connection").exclude(media_id="")
        if opts["campaign_id"]:
            qs = qs.filter(id=opts["campaign_id"])
        elif not opts["force"]:
            qs = qs.filter(thumbnail_url="")
        targets = list(qs.order_by("-created_at"))
        if opts["limit"]:
            targets = targets[: opts["limit"]]

        self.stdout.write(f"썸네일 대상 {len(targets)}건 (dry-run={opts['dry_run']})")
        counts: dict = {}

        for c in targets:
            label = f"{str(c.id)[:8]} {c.name[:24]}"
            if opts["dry_run"]:
                self.stdout.write(f"  대상  {label} media_id={c.media_id}")
                counts["dry_run"] = counts.get("dry_run", 0) + 1
                continue
            try:
                res = sync_campaign_thumbnail(str(c.id), force=opts["force"])
            except Exception as e:  # noqa: BLE001 - 건별 실패가 전체를 멈추지 않게
                self.stderr.write(f"  FAIL  {label} {type(e).__name__}: {e}")
                counts["error"] = counts.get("error", 0) + 1
                continue
            key = res.get("status", "unknown")
            counts[key] = counts.get(key, 0) + 1
            if key == "ok":
                self.stdout.write(f"  OK    {label} -> {res['thumbnail_url']}")
            else:
                self.stdout.write(f"  {key:<6}{label} {res.get('reason', '')}")
            if opts["sleep"]:
                time.sleep(opts["sleep"])

        self.stdout.write(self.style.SUCCESS(f"완료: {counts}"))
        if opts["dry_run"]:
            self.stdout.write("dry-run — DB 변경 없음")

    def _clean_placeholders(self, *, dry_run: bool) -> None:
        """자리표시자 media_url 을 비운다 (permalink 인 값은 절대 건드리지 않는다)."""
        rows = []
        for marker in PLACEHOLDER_URL_MARKERS:
            rows += list(AutoDMCampaign.objects.filter(media_url__contains=marker))
        self.stdout.write(f"자리표시자 media_url {len(rows)}건")
        for c in rows:
            if is_instagram_permalink(c.media_url):  # 방어 — 실수로 permalink 를 지우지 않게
                continue
            self.stdout.write(f"  비움  {str(c.id)[:8]} {c.name[:24]} <- {c.media_url[:60]}")
            if not dry_run:
                c.media_url = ""
                c.save(update_fields=["media_url", "updated_at"])
