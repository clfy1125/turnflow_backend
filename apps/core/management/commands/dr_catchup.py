"""DR: 복구 후 DB 기반 catch-up (Redis 큐 재구성).

Redis 는 휘발성으로 간주하고, 복구된 DB 를 진실로 하여 기존 **멱등** 태스크들을 순서대로
동기 실행한다(신규 dedupe 로직 없음). failover.sh 가 마이그레이션 직후, promote 전에 호출.

순서(설계: DR_IMPLEMENTATION_PLAN.md §7.5):
  STEP 0  거버너 재수화(rate_governor.rehydrate_from_db) — 카운터/Action Block 복원 + 동결 해제
  STEP 1  reconcile_stuck_submitting   (in-flight SUBMITTING 먼저)
  STEP 2  reconcile_accepted_dms
  STEP 3  ACTIVE 연동별 revive_failed_token_logs (제자리 되살림, idempotency_key 재사용)
  STEP 4  requeue_deferred_dms
  STEP 5  enforce_campaign_schedules
  STEP 6  poll_missed_comments          (마지막; 느린 Meta 호출 — --skip-poll 로 1차 패스 생략 가능)
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "DR 복구 후 catch-up — 기존 멱등 태스크를 DR 순서로 동기 실행."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="실행 없이 단계만 출력")
        parser.add_argument(
            "--skip-poll", action="store_true", help="poll_missed_comments(느린 Meta 호출) 생략"
        )

    def _run(self, label, fn, dry):
        if dry:
            self.stdout.write(f"[dry-run] would run: {label}")
            return
        t0 = time.time()
        try:
            result = fn()
            self.stdout.write(
                self.style.SUCCESS(f"✓ {label} ({time.time() - t0:.1f}s) -> {result}")
            )
        except Exception as e:  # noqa: BLE001 — 한 단계 실패가 전체를 막지 않게
            self.stdout.write(self.style.ERROR(f"✗ {label} failed: {type(e).__name__}: {e}"))

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        from apps.integrations import rate_governor, tasks
        from apps.integrations.models import IGAccountConnection

        self.stdout.write(self.style.WARNING("=== DR catch-up 시작 ==="))

        # STEP 0 — 거버너 재수화(동결 해제 + Action Block 복원)
        self._run("STEP0 rehydrate_governor", rate_governor.rehydrate_from_db, dry)

        # STEP 1~2 — in-flight 정리
        self._run("STEP1 reconcile_stuck_submitting", tasks.reconcile_stuck_submitting, dry)
        self._run("STEP2 reconcile_accepted_dms", tasks.reconcile_accepted_dms, dry)

        # STEP 3 — ACTIVE 연동별 토큰 되살림
        conn_ids = list(
            IGAccountConnection.objects.filter(
                status=IGAccountConnection.Status.ACTIVE
            ).values_list("id", flat=True)
        )
        if dry:
            self.stdout.write(f"[dry-run] would revive_failed_token_logs for {len(conn_ids)} conns")
        else:
            revived = 0
            for cid in conn_ids:
                try:
                    tasks.revive_failed_token_logs(str(cid))
                    revived += 1
                except Exception as e:  # noqa: BLE001
                    self.stdout.write(self.style.ERROR(f"  revive {cid} failed: {e}"))
            self.stdout.write(self.style.SUCCESS(f"✓ STEP3 revive_failed_token_logs x{revived}"))

        # STEP 4~5 — 재큐 + 스케줄 정리
        self._run("STEP4 requeue_deferred_dms", tasks.requeue_deferred_dms, dry)
        self._run("STEP5 enforce_campaign_schedules", tasks.enforce_campaign_schedules, dry)

        # STEP 6 — 누락 댓글 폴링(느림)
        if opts["skip_poll"]:
            self.stdout.write("STEP6 poll_missed_comments — skipped (--skip-poll)")
        else:
            self._run("STEP6 poll_missed_comments", tasks.poll_missed_comments, dry)

        self.stdout.write(self.style.WARNING("=== DR catch-up 완료 ==="))
        try:
            from apps.core.telegram import send_telegram_notification

            send_telegram_notification("✅ DR catch-up 완료" + (" (dry-run)" if dry else ""))
        except Exception:  # noqa: BLE001
            pass
