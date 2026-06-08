"""DM 파이프라인 합성 부하 측정 (스테이징 전용).

하드닝(threads 풀 dm_send 워커 + PgBouncer + 비동기 webhook)이 "잘 동작하는지 + 얼마나 빠른지"를
숫자로 확인한다. INSTAGRAM_MOCK_MODE=True 에서 send_dm_task 가 실제 Meta 호출 없이 mock message_id 로
ACCEPTED 까지 진행하므로, 측정되는 건 **서버측 처리 능력**(Celery 동시성 + DB 쓰기 + 커넥션 풀)이다.

사용 (스테이징 컨테이너 안에서):
    python manage.py loadtest_dm --seed
    python manage.py loadtest_dm --count 5000 --campaigns 20
    python manage.py loadtest_dm --cleanup

⚠️ DEBUG=True + INSTAGRAM_MOCK_MODE=True 가 아니면 실행 거부(실 Meta 호출/실데이터 보호).
"""

from __future__ import annotations

import time
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SentDMLog
from apps.workspace.models import Workspace

EMAIL = "loadtest@turnflow.local"
EXT_ACCOUNT = "loadtest_ig"
CAMPAIGN_PREFIX = "loadtest-campaign"
COMMENT_PREFIX = "lt-"               # 모든 부하 SentDMLog.comment_id 접두사 (cleanup/측정 필터)
TERMINAL = (
    SentDMLog.Status.ACCEPTED, SentDMLog.Status.DELIVERED, SentDMLog.Status.READ,
)
FAILED = (
    SentDMLog.Status.FAILED_TOKEN, SentDMLog.Status.FAILED_WINDOW,
    SentDMLog.Status.FAILED_PARAM, SentDMLog.Status.FAILED_NO_TRACE,
    SentDMLog.Status.FAILED_API, SentDMLog.Status.FAILED, SentDMLog.Status.SKIPPED,
)


class Command(BaseCommand):
    help = "DM 파이프라인 합성 부하 측정 (스테이징/mock 전용)"

    def add_arguments(self, parser):
        parser.add_argument("--seed", action="store_true", help="테스트 워크스페이스/연동/캠페인 생성")
        parser.add_argument("--cleanup", action="store_true", help="모든 부하 테스트 데이터 삭제")
        parser.add_argument("--count", type=int, default=0, help="발송할 DM 개수")
        parser.add_argument("--campaigns", type=int, default=20, help="부하를 분산할 캠페인 수(핫로우 완화)")
        parser.add_argument("--timeout", type=int, default=180, help="처리 완료 대기 한도(초)")

    def handle(self, *args, **o):
        if not (settings.DEBUG and getattr(settings, "INSTAGRAM_MOCK_MODE", False)):
            raise CommandError(
                "거부: DEBUG=True + INSTAGRAM_MOCK_MODE=True 환경에서만 실행 가능 "
                "(실 Meta 호출/실데이터 보호). 스테이징(.env.staging)에서 실행하세요."
            )

        if o["cleanup"]:
            return self._cleanup()
        if o["seed"]:
            self._seed(o["campaigns"])
            return
        if o["count"] > 0:
            return self._run(o["count"], o["campaigns"], o["timeout"])
        self.stdout.write("아무 동작 없음. --seed / --count N / --cleanup 중 하나를 지정하세요.")

    # ---------- seed ----------
    def _seed(self, k: int):
        User = get_user_model()
        user = User.objects.filter(email=EMAIL).first()
        if not user:
            try:
                user = User.objects.create_user(email=EMAIL, password="loadtest1234")
            except TypeError:
                user = User.objects.create(email=EMAIL)
        ws, _ = Workspace.objects.get_or_create(
            slug="loadtest", defaults={"name": "LoadTest", "owner": user, "plan": "enterprise"}
        )
        conn = IGAccountConnection.objects.filter(
            workspace=ws, external_account_id=EXT_ACCOUNT
        ).first()
        if not conn:
            conn = IGAccountConnection(
                workspace=ws,
                external_account_id=EXT_ACCOUNT,
                username="loadtest_ig",
                status=IGAccountConnection.Status.ACTIVE,
                scopes=["instagram_business_manage_messages"],
            )
            conn.access_token = "mock_token_loadtest"   # EncryptedTextField 디스크립터가 암호화 저장
            conn.save()
        have = AutoDMCampaign.objects.filter(
            ig_connection=conn, name__startswith=CAMPAIGN_PREFIX
        ).count()
        for i in range(have, k):
            AutoDMCampaign.objects.create(
                ig_connection=conn,
                name=f"{CAMPAIGN_PREFIX}-{i}",
                trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
                status=AutoDMCampaign.Status.ACTIVE,
                opening_message_template="load test message",
                max_sends_per_hour=10_000_000,   # 측정 중 시간당 제한에 안 걸리도록
            )
        self.stdout.write(self.style.SUCCESS(
            f"seed OK: workspace={ws.slug} conn={conn.external_account_id} campaigns={max(k, have)}"
        ))

    # ---------- run ----------
    def _run(self, count: int, k: int, timeout: int):
        from apps.integrations.tasks import send_dm_task

        conn = IGAccountConnection.objects.filter(external_account_id=EXT_ACCOUNT).first()
        if not conn:
            self.stdout.write("연동이 없어 자동 seed 합니다…")
            self._seed(k)
            conn = IGAccountConnection.objects.filter(external_account_id=EXT_ACCOUNT).first()
        campaigns = list(
            AutoDMCampaign.objects.filter(ig_connection=conn, name__startswith=CAMPAIGN_PREFIX)
        )
        if not campaigns:
            raise CommandError("캠페인이 없습니다. 먼저 --seed 하세요.")

        run_tag = f"{COMMENT_PREFIX}{int(time.time())}"
        self.stdout.write(f"run={run_tag} count={count} campaigns={len(campaigns)} dm_send 워커로 enqueue…")

        # 1) SentDMLog QUEUED 대량 생성 (캠페인 라운드로빈 → 단일 핫로우 완화)
        rows = []
        for i in range(count):
            camp = campaigns[i % len(campaigns)]
            key = f"{run_tag}-{i}"
            rows.append(SentDMLog(
                campaign=camp,
                comment_id=key,
                recipient_user_id=f"u{i}",
                recipient_username=f"user{i}",
                message_sent="load test",
                idempotency_key=key,
                status=SentDMLog.Status.QUEUED,
                dm_kind=SentDMLog.DMKind.STANDALONE,
                gate_status=SentDMLog.GateStatus.NONE,
            ))
        SentDMLog.objects.bulk_create(rows, batch_size=1000)

        # 2) enqueue + 타이머 시작
        t0 = time.monotonic()
        for r in rows:
            send_dm_task.delay(str(r.id))
        enqueue_done = time.monotonic()

        # 3) 폴링: 이 run 의 처리 완료 추이
        qkey = f"{run_tag}-"
        last = -1
        while True:
            qs = SentDMLog.objects.filter(comment_id__startswith=qkey)
            acc = qs.filter(status__in=TERMINAL).count()
            fail = qs.filter(status__in=FAILED).count()
            done = acc + fail
            elapsed = time.monotonic() - t0
            if done != last:
                rate = acc / elapsed if elapsed > 0 else 0
                self.stdout.write(
                    f"  t={elapsed:6.1f}s  accepted={acc:>6} failed={fail:>4} "
                    f"done={done}/{count}  rate={rate:7.1f} DM/s  dm_send_lag={self._qlen('dm_send')}"
                )
                last = done
            if done >= count or elapsed > timeout:
                break
            time.sleep(0.5)

        # 4) 결과 집계
        qs = SentDMLog.objects.filter(comment_id__startswith=qkey)
        acc = qs.filter(status__in=TERMINAL).count()
        fail = qs.filter(status__in=FAILED).count()
        total_elapsed = time.monotonic() - t0
        lat = self._latency_pcts(qs.filter(status__in=TERMINAL))

        self.stdout.write(self.style.SUCCESS("──────── 결과 ────────"))
        self.stdout.write(f"  대상            : {count}")
        self.stdout.write(f"  accepted        : {acc}")
        self.stdout.write(f"  failed          : {fail}")
        self.stdout.write(f"  enqueue 소요    : {enqueue_done - t0:.2f}s")
        self.stdout.write(f"  전체 wall       : {total_elapsed:.2f}s")
        if total_elapsed > 0:
            self.stdout.write(self.style.SUCCESS(
                f"  처리량          : {acc/total_elapsed:.1f} DM/s  (≈ {acc/total_elapsed*60:.0f} DM/분)"
            ))
        if lat:
            self.stdout.write(f"  지연 p50/p95/max: {lat['p50']:.0f} / {lat['p95']:.0f} / {lat['max']:.0f} ms (created→accepted)")
        if fail:
            self.stdout.write(self.style.WARNING(f"  ⚠ 실패 {fail}건 — status 분포 확인 필요 (mock 모드에선 0이어야 정상)"))

    def _latency_pcts(self, qs):
        vals = []
        for created, accepted in qs.values_list("created_at", "accepted_at")[:5000]:
            if created and accepted:
                vals.append((accepted - created).total_seconds() * 1000.0)
        if not vals:
            return None
        vals.sort()
        n = len(vals)
        return {
            "p50": vals[int(n * 0.50)],
            "p95": vals[min(int(n * 0.95), n - 1)],
            "max": vals[-1],
        }

    def _qlen(self, queue: str) -> int:
        try:
            from redis import Redis
            r = Redis(host=settings.REDIS_HOST, port=int(settings.REDIS_PORT), db=0)
            return r.llen(queue)
        except Exception:
            return -1

    # ---------- cleanup ----------
    def _cleanup(self):
        n_logs = SentDMLog.objects.filter(comment_id__startswith=COMMENT_PREFIX).delete()[0]
        ws = Workspace.objects.filter(slug="loadtest").first()
        if ws:
            ws.delete()   # CASCADE: ig_connection → campaigns → 남은 logs
        User = get_user_model()
        User.objects.filter(email=EMAIL).delete()
        self.stdout.write(self.style.SUCCESS(f"cleanup OK (logs deleted ~{n_logs}, workspace/user 제거)"))
