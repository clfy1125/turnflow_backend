"""계정 단위 DM 발송 정지(Action Block 쿨다운) 조회 · 해제 · dev 재현 커맨드.

두 가지 목적을 한 커맨드에 둔다 — 둘이 **같은 3곳**을 건드리기 때문이다:

1. **운영(prod 가능)**: 고객이 Meta 쪽 제한을 먼저 풀었을 때 우리 정지를 해제한다.
   2026-08-26 CS #66027015 에서 이 절차를 손으로 했다. 세 곳을 다 건드려야 하는데
   하나라도 빠지면 조용히 실패한다:
     (a) ``DMAccountBlock`` 행       — DR 듀얼라이트. 여기만 고치면 캐시가 이긴다.
     (b) ``dm:ab:cooldown|level`` 캐시 — ``action_block_cooldown_remaining`` 의 1순위 조회.
     (c) 적체 로그의 ``next_retry_at`` — 쿨다운 만료 시각으로 못박혀 있어서, 정지만 풀면
         **아무것도 나가지 않는다**(그 시각까지 requeue 워커가 안 집는다).
   ⚠️ ``level`` 을 0 으로 함께 내린다. 안 내리면 재발 시 쿨다운이 24h 가 아니라 48h 다
   (에스컬레이션 ``2**(level-1)``). 감사용 ``last_tripped_at`` 은 보존한다.

2. **개발(DEBUG 전용)**: 정지 화면을 실제로 띄워보기 위한 상태 주입. 프론트는 이 화면을
   확인하려면 Meta 가 실제로 계정을 제한해야 해서 재현이 불가능했다.

⚠️ 정지 **주입**(--pause/--queue)은 DEBUG=True 에서만 허용한다. 운영 계정에 정지를 걸면
   그 계정의 모든 DM 이 멈춘다. 조회(--list)와 해제(--release)는 어디서나 허용한다.

사용::

    python manage.py dm_account_pause --list
    python manage.py dm_account_pause --account @use.ai.likejimin --release --flush
    python manage.py dm_account_pause --account dmdummy_pro_cool --pause --hours 21 --queue 47
"""

from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, DMAccountBlock, IGAccountConnection, SentDMLog
from apps.integrations.rate_governor import _ab_keys, action_block_cooldown_remaining

# 정지 주입으로 만든 더미 로그 표식 — --queue 재실행 시 이전 것만 지우기 위해.
QUEUE_TAG = "dmpause"
# beat(requeue_deferred_dms) look-ahead(now+35s) 밖 — 더미가 실제 발송으로 안 잡히게.
BEAT_SAFE_BUFFER_SECONDS = 180


class Command(BaseCommand):
    help = "계정 DM 발송 정지 조회/해제(운영) · 정지 상태 주입(dev 전용)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            help="IG 계정 식별자 — external_account_id / @username / 연동 UUID 중 아무거나",
        )
        parser.add_argument("--list", action="store_true", help="현재 정지 중인 계정 전부 출력")
        parser.add_argument("--release", action="store_true", help="정지 해제 (운영 가능)")
        parser.add_argument(
            "--flush",
            action="store_true",
            help="--release 와 함께: 적체된 대기 건의 next_retry_at 을 now 로 당겨 즉시 재개",
        )
        parser.add_argument(
            "--flush-limit",
            type=int,
            default=0,
            help="--flush 대상 건수 상한 (0=전부). 카나리아 확인용 — 오래된 순.",
        )
        parser.add_argument("--pause", action="store_true", help="[DEBUG 전용] 정지 주입")
        parser.add_argument("--hours", type=float, default=21.0, help="--pause 쿨다운 시간")
        parser.add_argument(
            "--queue",
            type=int,
            default=0,
            help="[DEBUG 전용] 대기(queued) 더미 로그 N건 생성 — 정지 배너/건수 확인용",
        )

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        self.now = timezone.now()

        if opts["list"] or not any(opts.get(k) for k in ("release", "pause", "queue", "account")):
            return self._list()

        conn = self._resolve(opts.get("account"))
        ext = str(conn.external_account_id)

        if opts["release"]:
            self._release(conn, ext, flush=opts["flush"], limit=opts["flush_limit"])
        if opts["pause"]:
            self._require_debug("--pause")
            self._pause(conn, ext, hours=opts["hours"])
        if opts["queue"]:
            self._require_debug("--queue")
            self._seed_queue(conn, opts["queue"])

        self._show(conn, ext)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_debug(flag: str):
        if not settings.DEBUG:
            raise CommandError(
                f"{flag} 는 DEBUG=True 에서만 허용됩니다 — 운영 계정에 정지를 걸면 "
                "그 계정의 모든 DM 발송이 멈춥니다. (조회/해제는 운영에서도 가능합니다.)"
            )

    def _resolve(self, account: str | None) -> IGAccountConnection:
        if not account:
            raise CommandError("--account 를 지정하세요 (external_account_id / @username / UUID)")
        needle = account.strip().lstrip("@")
        qs = IGAccountConnection.objects.filter(
            Q(external_account_id=needle) | Q(username__iexact=needle)
        )
        if not qs.exists():
            try:
                qs = IGAccountConnection.objects.filter(id=needle)
            except (ValueError, TypeError):
                qs = IGAccountConnection.objects.none()
        conns = list(qs[:5])
        if not conns:
            raise CommandError(f"IG 연동을 찾지 못했습니다: {account}")
        if len(conns) > 1:
            joined = ", ".join(f"{c.external_account_id}(@{c.username})" for c in conns)
            raise CommandError(
                f"식별자가 여러 연동에 걸립니다 — external_account_id 로 주세요: {joined}"
            )
        return conns[0]

    # ------------------------------------------------------------------ #
    def _list(self):
        rows = DMAccountBlock.objects.filter(cooldown_until__gt=self.now).order_by("cooldown_until")
        if not rows:
            self.stdout.write(self.style.SUCCESS("현재 발송 정지 중인 계정이 없습니다."))
            return
        self.stdout.write(self.style.WARNING(f"발송 정지 중인 계정 {rows.count()}개"))
        for r in rows:
            conn = IGAccountConnection.objects.filter(
                external_account_id=r.external_account_id
            ).first()
            remaining = action_block_cooldown_remaining(r.external_account_id)
            waiting = SentDMLog.objects.filter(
                campaign__ig_connection__external_account_id=r.external_account_id,
                status=SentDMLog.Status.QUEUED,
            ).count()
            self.stdout.write(
                f"  {r.external_account_id} @{getattr(conn, 'username', '?')} "
                f"level={r.level} 남은시간={remaining / 3600:.2f}h "
                f"해제예정={r.cooldown_until:%Y-%m-%d %H:%M %Z} 대기={waiting}건"
            )

    def _release(self, conn, ext: str, *, flush: bool, limit: int):
        before = action_block_cooldown_remaining(ext)
        if before <= 0:
            self.stdout.write(self.style.WARNING(f"@{conn.username} 는 이미 정지 상태가 아닙니다."))
        # (a) DB — 행은 남긴다(last_tripped_at 감사용). level=0 → 재발 시 기본 쿨다운.
        n = DMAccountBlock.objects.filter(external_account_id=ext).update(
            cooldown_until=self.now, level=0
        )
        # (b) 캐시 — 여기를 안 지우면 DB 를 고쳐도 캐시가 이긴다.
        cd_key, lvl_key = _ab_keys(ext)
        cache.delete(cd_key)
        cache.delete(lvl_key)
        self.stdout.write(
            self.style.SUCCESS(
                f"정지 해제: @{conn.username} (직전 잔여 {before / 3600:.2f}h, DB {n}행, 캐시 2키 삭제)"
            )
        )

        # (c) 적체 로그 — next_retry_at 이 쿨다운 만료 시각에 못박혀 있다.
        qs = SentDMLog.objects.filter(
            campaign__ig_connection=conn, status=SentDMLog.Status.QUEUED
        ).order_by("created_at")
        total = qs.count()
        if not flush:
            self.stdout.write(
                f"  대기 {total}건은 그대로 둡니다 — 즉시 재개하려면 --flush 를 함께 주세요. "
                "(정지 해제만으로는 예약 시각까지 아무것도 나가지 않습니다.)"
            )
            return
        ids = list(qs.values_list("id", flat=True)[: limit or total])
        moved = SentDMLog.objects.filter(id__in=ids, status=SentDMLog.Status.QUEUED).update(
            next_retry_at=self.now
        )
        self.stdout.write(
            self.style.SUCCESS(f"  재개 투입: {moved}/{total}건 (페이서가 간격을 직렬화합니다)")
        )

    def _pause(self, conn, ext: str, *, hours: float):
        until = self.now + timedelta(hours=hours)
        DMAccountBlock.objects.update_or_create(
            external_account_id=ext,
            defaults={"cooldown_until": until, "level": 1, "last_tripped_at": self.now},
        )
        cd_key, lvl_key = _ab_keys(ext)
        ttl = max(int(hours * 3600), 60)
        cache.set(cd_key, int(time.time()) + ttl, timeout=ttl)
        cache.set(lvl_key, 1, timeout=30 * 24 * 3600)
        self.stdout.write(
            self.style.SUCCESS(
                f"정지 주입: @{conn.username} {hours}h (해제 예정 {until:%m-%d %H:%M})"
            )
        )

    def _seed_queue(self, conn, count: int):
        campaign = AutoDMCampaign.objects.filter(ig_connection=conn).order_by("-created_at").first()
        if campaign is None:
            raise CommandError(
                f"@{conn.username} 에 캠페인이 없습니다 — 먼저 seed_dm_dev_dummy 를 돌리세요."
            )
        SentDMLog.objects.filter(
            campaign=campaign, idempotency_key__startswith=f"{QUEUE_TAG}:"
        ).delete()
        objs = [
            SentDMLog(
                campaign=campaign,
                comment_id=f"{QUEUE_TAG}-c-{i:04d}",
                comment_text="[더미] 신청합니다",
                recipient_user_id=f"{QUEUE_TAG}_{i:04d}",
                recipient_username=f"pause_user_{i:04d}",
                message_sent=campaign.opening_message_template or "[더미] 안녕하세요!",
                idempotency_key=f"{QUEUE_TAG}:{campaign.id}:{i}",
                status=SentDMLog.Status.QUEUED,
                dm_kind=SentDMLog.DMKind.OPENING,
            )
            for i in range(count)
        ]
        SentDMLog.objects.bulk_create(objs, batch_size=500)

        # ★ 사람 수 ≠ 이벤트 수 로 벌린다.
        # 둘이 같으면 프론트가 `gauge.waiting`(이벤트)을 "N명"으로 잘못 배선해도 화면이
        # 맞아 보여서 버그를 못 잡는다. 실제로도 한 사람에게 여러 건이 대기할 수 있다
        # (팔로우 게이트 리워드 = 2번째 DM · 재안내). 앞쪽 1/5 에 자식 1건씩 붙인다.
        multi = count // 5
        children = [
            SentDMLog(
                campaign=campaign,
                comment_id="",  # 리워드는 댓글이 아니라 user_id 경로
                recipient_user_id=objs[i].recipient_user_id,  # 부모와 **같은 사람**
                recipient_username=objs[i].recipient_username,
                message_sent="[더미] 자료 보내드려요!",
                idempotency_key=f"{QUEUE_TAG}:{campaign.id}:{i}:reward",
                status=SentDMLog.Status.QUEUED,
                dm_kind=SentDMLog.DMKind.REWARD,
                parent_log=objs[i],
            )
            for i in range(multi)
        ]
        if children:
            SentDMLog.objects.bulk_create(children, batch_size=500)

        # ★ 절반은 **창이 닫힐 예정**으로 만든다 (`waiting_window_risk` 렌더 확인용).
        # 2번째 DM 의 창은 24h 인데 기본 쿨다운도 24h 라, 정지 시작 시점에 이미 대기 중이던
        # 리워드는 재개 때 만료돼 있다(prod 실측 0.6초 차이). 이 화면 분기를 dev 에서 보려면
        # 더미도 그 상태여야 한다 — 전부 방금 만들면 위험 0 으로만 보인다.
        at_risk = children[: multi // 2]
        for o in at_risk:
            o.created_at = self.now - timedelta(hours=4)
        if at_risk:
            SentDMLog.objects.bulk_update(at_risk, ["created_at"], batch_size=500)

        # beat 가 더미를 실제 발송으로 집지 않도록 슬롯을 미래로.
        all_objs = objs + children
        for idx, o in enumerate(all_objs):
            o.next_retry_at = self.now + timedelta(seconds=BEAT_SAFE_BUFFER_SECONDS + idx * 5)
        SentDMLog.objects.bulk_update(all_objs, ["next_retry_at"], batch_size=500)
        self.stdout.write(
            self.style.SUCCESS(
                f"대기 더미 생성 → campaign_id={campaign.id}\n"
                f"    사람 {count}명 · 이벤트 {len(all_objs)}건 "
                f"(그중 {multi}명은 2번째 DM 도 함께 대기 — people ≠ gauge 확인용)\n"
                f"    2번째 DM {len(at_risk)}건은 재개 전 창 만료 예정 — waiting_window_risk 확인용"
            )
        )

    def _show(self, conn, ext: str):
        remaining = action_block_cooldown_remaining(ext)
        waiting = SentDMLog.objects.filter(
            campaign__ig_connection=conn, status=SentDMLog.Status.QUEUED
        ).count()
        state = f"정지중({remaining / 3600:.2f}h 남음)" if remaining > 0 else "정상"
        self.stdout.write(
            f"\n현재 상태 — @{conn.username} ({ext}): {state} · 대기 {waiting}건\n"
            f"  queue-state:  blocking_reason="
            f"{'action_block_cooldown' if remaining > 0 else 'null'} "
            f"action_block_cooldown_seconds={int(remaining)}\n"
            f"  로그 user_reason: {'account_send_paused' if remaining > 0 else '(없음)'}"
        )
