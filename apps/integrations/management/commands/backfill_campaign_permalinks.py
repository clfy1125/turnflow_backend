"""specific_media 캠페인의 게시물 permalink(media_url) 일괄 백필.

어드민 캠페인 목록/상세의 '게시물 보기' 링크는 ``media_url`` 이 instagram.com permalink 일
때만 뜬다. 캠페인 생성 경로 중 직접 생성만 백필 훅이 걸려 있던 기간에 만들어진 캠페인들은
media_url 이 비어 있어 링크가 안 떴다 (2026-08-03 prod 실측: 63건 중 41건).

기본은 **빈값만** 채운다 — 사용자가 넣은 참고 URL(예: unsplash 이미지)을 덮어쓰지 않기 위함.
그 값까지 permalink 로 교체하려면 ``--overwrite-non-permalink`` 를 준다.

사용:
    python manage.py backfill_campaign_permalinks --dry-run
    python manage.py backfill_campaign_permalinks
    python manage.py backfill_campaign_permalinks --campaign-id <uuid> --overwrite-non-permalink
"""

import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.integrations.models import AutoDMCampaign, IGAccountConnection
from apps.integrations.services import InstagramMediaService, is_instagram_permalink


class Command(BaseCommand):
    help = "specific_media 캠페인의 media_url 을 인스타 게시물 permalink 로 백필"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="조회만 하고 저장하지 않음")
        parser.add_argument("--campaign-id", default=None, help="특정 캠페인만 처리")
        parser.add_argument("--limit", type=int, default=0, help="처리 상한 (0=무제한)")
        parser.add_argument(
            "--sleep", type=float, default=0.3, help="건당 대기 초 (IG 레이트리밋 여유)"
        )
        parser.add_argument(
            "--overwrite-non-permalink",
            action="store_true",
            help="permalink 가 아닌 기존 media_url(사용자 입력 URL)도 교체",
        )

    def handle(self, *args, **opts):
        qs = AutoDMCampaign.objects.select_related("ig_connection").filter(
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA
        )
        if opts["campaign_id"]:
            qs = qs.filter(id=opts["campaign_id"])
        else:
            qs = qs.exclude(media_id="")
            if not opts["overwrite_non_permalink"]:
                qs = qs.filter(Q(media_url="") | Q(media_url__isnull=True))
        targets = list(qs.order_by("created_at"))
        if opts["limit"]:
            targets = targets[: opts["limit"]]

        self.stdout.write(f"대상 {len(targets)}건 (dry-run={opts['dry_run']})")
        counts = {"ok": 0, "already": 0, "no_permalink": 0, "api_error": 0, "inactive_conn": 0}

        for c in targets:
            if is_instagram_permalink(c.media_url):
                counts["already"] += 1
                continue
            conn = c.ig_connection
            if conn is None or conn.status != IGAccountConnection.Status.ACTIVE:
                counts["inactive_conn"] += 1
                self.stdout.write(
                    f"  SKIP  {str(c.id)[:8]} {c.name[:26]} (conn={conn and conn.status})"
                )
                continue
            try:
                permalink = InstagramMediaService.get_media_permalink(
                    media_id=c.media_id, access_token=conn.access_token
                )
            except Exception as e:  # noqa: BLE001 - 건별 실패가 전체를 멈추지 않게
                counts["api_error"] += 1
                self.stderr.write(f"  FAIL  {str(c.id)[:8]} {c.name[:26]} {type(e).__name__}: {e}")
                continue
            if not permalink or not is_instagram_permalink(permalink):
                counts["no_permalink"] += 1
                self.stdout.write(f"  없음  {str(c.id)[:8]} {c.name[:26]} (게시물 삭제 추정)")
                continue

            counts["ok"] += 1
            self.stdout.write(f"  OK    {str(c.id)[:8]} {c.name[:26]} -> {permalink}")
            if not opts["dry_run"]:
                c.media_url = permalink
                c.save(update_fields=["media_url", "updated_at"])
            if opts["sleep"]:
                time.sleep(opts["sleep"])

        self.stdout.write(self.style.SUCCESS(f"완료: {counts}"))
        if opts["dry_run"]:
            self.stdout.write("dry-run — DB 변경 없음")
