"""DM 오류 (code, subcode, status) 전수 목록 덤프 (OPS-2-c).

운영 DB 에 **실제로 존재하는** 오류 조합을 뽑아 원인·조치 사전
(:mod:`apps.admin_api.dm_error_catalog`)의 빈칸을 찾는다. 사전에 없는 조합은
``catalog=MISSING`` 으로 표시되므로 그 줄만 채우면 된다.

사용:
    docker compose exec web python manage.py dump_dm_error_census
    docker compose exec web python manage.py dump_dm_error_census --days 90 --format csv
"""

from __future__ import annotations

import csv
import sys
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Max
from django.utils import timezone

from apps.admin_api.dm_error_catalog import describe
from apps.admin_api.views.dashboard_ops import DM_ERROR_STATUSES
from apps.integrations.dm_status_groups import HIDDEN_SPAM, status_group

COLUMNS = ("code", "subcode", "status", "group", "count", "catalog", "title", "sample")


class Command(BaseCommand):
    help = "DM 오류 (error_code, error_subcode, status) 조합 전수 + 대표 원문 메시지 덤프"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=0, help="최근 N일로 제한 (0=전체 기간, 기본값)"
        )
        parser.add_argument("--format", choices=("table", "csv"), default="table", help="출력 형식")
        parser.add_argument("--sample-chars", type=int, default=140, help="원문 미리보기 길이")

    def handle(self, *args, **options):
        from apps.integrations.models import SentDMLog

        qs = SentDMLog.objects.filter(status__in=DM_ERROR_STATUSES)
        if options["days"]:
            qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=options["days"]))

        rows = (
            qs.values("error_code", "error_subcode", "status")
            .annotate(count=Count("id"), sample=Max("error_message"))
            .order_by("-count")
        )

        cut = options["sample_chars"]
        out = []
        for r in rows:
            code = r["error_code"] or ""
            subcode = r["error_subcode"] or ""
            desc = describe(code, subcode, r["status"])
            out.append(
                {
                    "code": code,
                    "subcode": subcode,
                    "status": r["status"],
                    "group": (
                        "hidden_spam"
                        if status_group(r["status"], subcode) == HIDDEN_SPAM
                        else "failed"
                    ),
                    "count": r["count"],
                    "catalog": "OK" if desc["title"] else "MISSING",
                    "title": desc["title"],
                    "sample": (r["sample"] or "").replace("\n", " ")[:cut],
                }
            )

        if options["format"] == "csv":
            writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(out)
            return

        self.stdout.write(
            f"{'code':<10}{'subcode':<10}{'status':<18}{'group':<12}"
            f"{'count':>7}  {'catalog':<8}{'title':<24}sample"
        )
        for r in out:
            self.stdout.write(
                f"{r['code']:<10}{r['subcode']:<10}{r['status']:<18}{r['group']:<12}"
                f"{r['count']:>7}  {r['catalog']:<8}{r['title']:<24}{r['sample']}"
            )
        missing = sum(1 for r in out if r["catalog"] == "MISSING")
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(f"사전 미등록 조합 {missing}건 / 전체 {len(out)}건")
            if missing
            else self.style.SUCCESS(f"전 조합({len(out)}건) 사전 등록 완료")
        )
