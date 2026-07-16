"""같은 Instagram 계정(external_account_id)이 둘 이상의 워크스페이스에 연동된 현황을 리포트한다.

전서비스 유일성(하나의 IG 계정 = 하나의 워크스페이스) 도입 전 기존 prod 데이터 파악용.
**읽기 전용** — 아무것도 수정/삭제하지 않는다. 여기서 파악한 중복을 수동 정리한 뒤에야
조건부 UNIQUE 제약(external_account_id where status != REVOKED) 마이그레이션을 검토한다.

용도:
- prod 배포 후 `python manage.py audit_ig_duplicates` 로 현황 스냅샷.
- `--json` 으로 후속 정리 스크립트가 먹을 수 있는 형태 출력.

기본은 REVOKED(하드 해제됨) 연동을 제외한다 — 이미 점유를 놓은 행이라 충돌이 아니다.
`--include-revoked` 로 전체를 본다.
"""

import json

from django.core.management.base import BaseCommand
from django.db.models import Count

from apps.integrations.models import IGAccountConnection


class Command(BaseCommand):
    help = "같은 IG 계정이 여러 워크스페이스에 연동된 중복 현황을 리포트한다 (읽기 전용)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--include-revoked",
            action="store_true",
            help="REVOKED(하드 해제) 연동도 포함해서 집계 (기본: 제외).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="사람용 표 대신 JSON 으로 출력 (후속 정리 스크립트용).",
        )

    def handle(self, *args, **opts):
        qs = IGAccountConnection.objects.select_related("workspace", "workspace__owner")
        if not opts["include_revoked"]:
            qs = qs.exclude(status=IGAccountConnection.Status.REVOKED)

        # 둘 이상의 워크스페이스에 걸친 external_account_id 만 추린다.
        dup_account_ids = list(
            qs.values("external_account_id")
            .annotate(n_ws=Count("workspace", distinct=True))
            .filter(n_ws__gt=1)
            .values_list("external_account_id", flat=True)
        )

        report = []
        for account_id in dup_account_ids:
            conns = qs.filter(external_account_id=account_id).order_by("created_at")
            entry = {
                "external_account_id": account_id,
                "username": next((c.username for c in conns if c.username), ""),
                "connections": [
                    {
                        "id": str(c.id),
                        "workspace_id": str(c.workspace_id),
                        "workspace_name": c.workspace.name,
                        "owner_email": c.workspace.owner.email,  # 운영자 도구 — 전체 노출
                        "status": c.status,
                        "is_active": c.is_active,
                        "created_at": c.created_at.isoformat(),
                        "dm_campaigns": c.dm_campaigns.count(),
                    }
                    for c in conns
                ],
            }
            report.append(entry)

        total_conns = sum(len(e["connections"]) for e in report)

        if opts["json"]:
            self.stdout.write(
                json.dumps(
                    {
                        "duplicated_accounts": len(report),
                        "connections": total_conns,
                        "include_revoked": opts["include_revoked"],
                        "accounts": report,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        if not report:
            self.stdout.write(
                self.style.SUCCESS("중복 없음 — 모든 IG 계정이 단일 워크스페이스에 연동됨.")
            )
            return

        for e in report:
            self.stdout.write(
                self.style.WARNING(
                    f"\nIG 계정 {e['external_account_id']} (@{e['username'] or '—'}) "
                    f"— {len(e['connections'])}개 워크스페이스"
                )
            )
            for c in e["connections"]:
                self.stdout.write(
                    f"  · conn={c['id']} ws={c['workspace_name']}({c['workspace_id']}) "
                    f"owner={c['owner_email']} status={c['status']} "
                    f"is_active={c['is_active']} campaigns={c['dm_campaigns']} "
                    f"created={c['created_at']}"
                )

        self.stdout.write(
            self.style.NOTICE(
                f"\n중복 IG 계정 {len(report)}개 / 연동 {total_conns}개 "
                f"(include_revoked={opts['include_revoked']})"
            )
        )
