"""캠페인 순차 발송 큐 데모 시더 (개발 전용 — 실 DM 미발송).

특정 자동 DM 캠페인에 더미 ``SentDMLog(QUEUED)`` 를 채워 넣고, 지정한 총 시간
(``--duration``, 기본 2시간)에 걸쳐 QUEUED→ACCEPTED 로 하나씩 승격해 **"순차 발송 중"**
상황을 재현한다.

★ 핵심: **Meta API 를 절대 호출하지 않는다** (``send_dm_task`` 미경유). 순수 DB 상태
전이만으로 ``/dm-verification/queue-state/`` 게이지·ETA·people 블록을 살아있게 만든다.
따라서 ``INSTAGRAM_MOCK_MODE`` 여부와 무관하게 실제 발송이 일어나지 않는다.

★ beat 안전(beat-safe): ``requeue_deferred_dms`` beat(30초 주기)가 ``next_retry_at`` 이
now+35초 안에 든 QUEUED 를 실제 ``send_dm_task`` 로 재투입한다. 이 커맨드는 더미 슬롯을
항상 그 look-ahead 창(``BEAT_HORIZON``) 밖(now+``BEAT_SAFE_BUFFER``~)으로 배치하고, 드립이
앞단을 창보다 먼저 비워 beat 가 더미를 절대 못 집게 한다(느린 드립 대비 가드 재스탬프 포함).

⚠️ ``DEBUG=True`` 가 아니면 실행 거부(운영 데이터/카운터 보호).

사용 (dev 컨테이너 안에서):
    # 2시간에 걸쳐 60건을 순차 발송 (기본)
    python manage.py seed_campaign_queue --campaign <uuid>

    # 시딩만 (큐만 채우고 진행은 안 함)
    python manage.py seed_campaign_queue --campaign <uuid> --seed-only

    # 기존 더미 큐를 이어서 드립(예: 남은 것을 남은 시간에)
    python manage.py seed_campaign_queue --campaign <uuid> --drip-only --duration 3600

    # 더미 데이터 전부 제거 + 카운터 원복
    python manage.py seed_campaign_queue --campaign <uuid> --cleanup
"""

from __future__ import annotations

import time
import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, SentDMLog

# 이 명령이 만든 더미 로그 식별 접두사 (cleanup / 조회 필터).
COMMENT_PREFIX = "demoqueue-"
RECIPIENT_PREFIX = "demo_"

# beat(requeue_deferred_dms) look-ahead 창(now+35s).
BEAT_HORIZON_SECONDS = 35
# 큐 앞단(다음 발송분)을 항상 now 로부터 이만큼 미래로 유지 → beat 창(35s) 밖(마진 85s).
BEAT_SAFE_BUFFER_SECONDS = 120
# 남은 앞단 슬롯이 now+이 값 안으로 들어오면(beat 창 근접) 재스탬프해 밀어낸다(느린 드립 백스톱).
BEAT_GUARD_SECONDS = 60


def _stop_key(campaign_id) -> str:
    return f"demoqueue:stop:{campaign_id}"


class Command(BaseCommand):
    help = "자동 DM 캠페인 순차 발송 큐 데모 시더 (dev 전용 · 실 DM 미발송)"

    def add_arguments(self, parser):
        parser.add_argument("--campaign", required=True, help="대상 캠페인 UUID")
        parser.add_argument("--count", type=int, default=60, help="채울 더미 대기 DM 수")
        parser.add_argument(
            "--duration", type=int, default=7200, help="큐 전체 진행 시간(초) — 기본 7200=2시간"
        )
        parser.add_argument("--seed-only", action="store_true", help="큐만 채우고 드립 안 함")
        parser.add_argument("--drip-only", action="store_true", help="기존 더미 큐만 드립")
        parser.add_argument("--cleanup", action="store_true", help="더미 데이터 제거 + 카운터 원복")
        parser.add_argument(
            "--max-wall", type=int, default=0, help="드립 최대 지속(초, 0=duration+600 자동)"
        )

    def handle(self, *args, **o):
        if not settings.DEBUG:
            raise CommandError("거부: DEBUG=True 환경에서만 실행 가능 (운영 데이터 보호).")

        campaign = self._get_campaign(o["campaign"])
        if o["cleanup"]:
            return self._cleanup(campaign)

        count = o["count"]
        duration = max(o["duration"], 1)

        if not o["drip_only"]:
            cache.delete(_stop_key(campaign.id))  # 이전 stop 신호 해제
            gap = duration / max(count, 1)
            self._seed(campaign, count, gap, duration)

        if o["seed_only"]:
            self._hint(campaign)
            return

        # 드립: 남은 건수 기준으로 gap 재계산(이어하기 대응).
        remaining = self._demo_qs(campaign).filter(status=SentDMLog.Status.QUEUED).count()
        if remaining == 0:
            self.stdout.write("드립할 QUEUED 더미가 없습니다.")
            return
        gap = duration / remaining
        max_wall = o["max_wall"] or (duration + 600)
        self._drip(campaign, gap, max_wall)
        self._hint(campaign)

    # ---------- helpers ----------
    def _get_campaign(self, campaign_id: str) -> AutoDMCampaign:
        try:
            return AutoDMCampaign.objects.select_related("ig_connection__workspace").get(
                id=campaign_id
            )
        except (AutoDMCampaign.DoesNotExist, ValueError, TypeError) as e:
            raise CommandError(f"캠페인을 찾을 수 없습니다: {campaign_id}") from e

    def _demo_qs(self, campaign):
        return SentDMLog.objects.filter(campaign=campaign, comment_id__startswith=COMMENT_PREFIX)

    def _slots(self, now, count: int, gap: float):
        """앞단이 항상 beat 창 밖(now+BUFFER~)에 있도록 gap 간격 슬롯 생성.

        슬롯 r = now + BUFFER + r*gap. 미승격 앞단의 (slot - now) 는 항상 >= BUFFER 라
        beat(now+35s) 가 절대 못 집는다(고정 간격 → 드리프트 0)."""
        base = float(BEAT_SAFE_BUFFER_SECONDS)
        return [now + timezone.timedelta(seconds=base + r * gap) for r in range(count)]

    # ---------- seed ----------
    def _seed(self, campaign, count: int, gap: float, duration: int):
        conn = campaign.ig_connection
        run = int(time.time())
        now = timezone.now()
        slots = self._slots(now, count, gap)

        rows = []
        for i in range(count):
            rows.append(
                SentDMLog(
                    campaign=campaign,
                    comment_id=f"{COMMENT_PREFIX}{run}-{i}",
                    comment_text="[데모] 순차 발송 큐 시딩 댓글",
                    recipient_user_id=f"{RECIPIENT_PREFIX}{run}_{i}",
                    recipient_username=f"데모유저{i + 1:03d}",
                    message_sent=(campaign.opening_message_template or "[데모] 안녕하세요!"),
                    idempotency_key=f"demoq:{run}:{i}",
                    status=SentDMLog.Status.QUEUED,
                    # 오프닝(루트) DM = comment_id 있음 + parent 없음 → private_reply 버킷.
                    # people_rollup 이 사람 단위로 세도록 dm_kind=opening, parent_log=None.
                    dm_kind=SentDMLog.DMKind.OPENING,
                    gate_status=SentDMLog.GateStatus.NONE,
                    # 슬롯은 항상 beat 창(now+35s) 밖 → beat 가 실제 발송으로 안 채감.
                    next_retry_at=slots[i],
                )
            )
        SentDMLog.objects.bulk_create(rows, batch_size=500)
        mins = duration // 60
        self.stdout.write(
            self.style.SUCCESS(
                f"seed OK: campaign={campaign.name!r} conn={conn.username} "
                f"더미 대기 DM {count}건 · 약 {mins}분에 걸쳐 진행(간격 ~{gap:.0f}초)"
            )
        )

    # ---------- drip ----------
    def _drip(self, campaign, gap: float, max_wall: int):
        stop_key = _stop_key(campaign.id)
        t0 = time.monotonic()
        promoted = 0
        self.stdout.write(f"drip 시작 — {gap:.0f}초마다 QUEUED→ACCEPTED 1건 (실 DM 없음)")

        while True:
            if cache.get(stop_key):
                self.stdout.write(self.style.WARNING("stop 신호 감지 — 드립 종료"))
                break
            if time.monotonic() - t0 > max_wall:
                self.stdout.write(self.style.WARNING(f"max-wall({max_wall}s) 초과 — 드립 종료"))
                break

            now = timezone.now()
            log = (
                self._demo_qs(campaign)
                .filter(status=SentDMLog.Status.QUEUED)
                .order_by("next_retry_at", "created_at")
                .first()
            )
            if log is None:
                self.stdout.write(self.style.SUCCESS(f"drip 완료 — 총 {promoted}건 전송 처리"))
                break
            self._accept(log)
            campaign.increment_sent()
            promoted += 1

            # ── beat 가드(백스톱): 남은 앞단 슬롯이 beat 창에 근접했으면 미래로 재스탬프.
            remaining = list(
                self._demo_qs(campaign)
                .filter(status=SentDMLog.Status.QUEUED)
                .order_by("next_retry_at", "created_at")
                .values_list("id", "next_retry_at")
            )
            guard_at = now + timezone.timedelta(seconds=BEAT_GUARD_SECONDS)
            if remaining and (remaining[0][1] is None or remaining[0][1] <= guard_at):
                slots = self._slots(now, len(remaining), gap)
                for (lid, _old), slot in zip(remaining, slots):
                    SentDMLog.objects.filter(id=lid).update(next_retry_at=slot)

            if promoted % 5 == 0 or not remaining:
                self.stdout.write(f"  전송됨 누적 {promoted} · 남은 대기 {len(remaining)}")
            time.sleep(gap)

    def _accept(self, log: SentDMLog):
        """QUEUED → ACCEPTED (mock). Meta 호출 없음 — DB 상태만 전이."""
        now = timezone.now()
        log.status = SentDMLog.Status.ACCEPTED
        log.submitted_at = now
        log.accepted_at = now
        log.next_retry_at = None
        log.meta_message_id = f"mock-demo-{uuid.uuid4().hex[:16]}"
        log.save(
            update_fields=[
                "status",
                "submitted_at",
                "accepted_at",
                "next_retry_at",
                "meta_message_id",
            ]
        )

    # ---------- cleanup ----------
    def _cleanup(self, campaign):
        cache.set(_stop_key(campaign.id), 1, timeout=3 * 3600)  # 진행 중 드립 중단 신호
        n = self._demo_qs(campaign).delete()[0]

        # 이 캠페인에 실제(비-데모) 로그가 없다면 카운터를 0 으로 원복.
        real_left = SentDMLog.objects.filter(campaign=campaign).count()
        if real_left == 0:
            AutoDMCampaign.objects.filter(pk=campaign.pk).update(
                total_sent=0, total_failed=0, total_unconfirmed=0
            )
            counter_note = "카운터(total_sent/failed/unconfirmed) 0 으로 원복"
        else:
            counter_note = f"실 로그 {real_left}건 남아 카운터 원복 생략(오염 방지)"
        self.stdout.write(self.style.SUCCESS(f"cleanup OK: 더미 로그 {n}건 삭제, {counter_note}"))

    def _hint(self, campaign):
        base = "/api/v1/integrations/dm-verification/queue-state/"
        self.stdout.write("")
        self.stdout.write(f"큐 현황 폴링: GET {base}?campaign_id={campaign.id}")
        self.stdout.write(
            f"정리:        python manage.py seed_campaign_queue --campaign {campaign.id} --cleanup"
        )
