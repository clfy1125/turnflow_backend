"""홈 알림(GET /api/v1/home/alerts/) 프론트 확인용 dev 계정 시드.

계정 4개를 만들어 **알림 코드 전부를 한 번씩** 재현한다. 프론트가 상태별 화면을 실제 응답으로
확인할 수 있게 하는 것이 목적이다.

  home-clean@test.com     알림 0건 — 기본 상태(create_campaign)만 뜬다
  home-critical@test.com  멈춤 6종 + 강제팝업 없음
  home-todo@test.com      권유 4종 + 한도 경고
  home-blocking@test.com  강제 팝업(쓸 계정 선택) — 배너가 아니라 모달

  비밀번호: 전부 Test1234!

⚠️ DEBUG=True 에서만 실행된다(운영 데이터 보호).
⚠️ Meta 를 호출하지 않는다 — 토큰은 전부 가짜다. 홈 알림 판정 자체가 Graph 를 안 부르므로
   이 시드만으로 응답이 완전히 재현된다.

멱등: 다시 돌리면 같은 상태로 덮어쓴다.
"""

import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

PASSWORD = "Test1234!"

CLEAN = "home-clean@test.com"
CRITICAL = "home-critical@test.com"
TODO = "home-todo@test.com"
BLOCKING = "home-blocking@test.com"


class Command(BaseCommand):
    help = "홈 알림 확인용 dev 계정 4종 시드 (DEBUG=True 전용)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            choices=["clean", "critical", "todo", "blocking"],
            help="하나만 다시 시드",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("거부: DEBUG=True 환경에서만 실행 가능 (운영 데이터 보호).")

        only = options.get("only")
        made = []
        if only in (None, "clean"):
            made.append(self.seed_clean())
        if only in (None, "critical"):
            made.append(self.seed_critical())
        if only in (None, "todo"):
            made.append(self.seed_todo())
        if only in (None, "blocking"):
            made.append(self.seed_blocking())

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== 시드 완료 ==="))
        for email in made:
            self.stdout.write(f"  {email} / {PASSWORD}")
        self.stdout.write("")
        self.stdout.write("확인: POST /api/v1/auth/login/ → GET /api/v1/home/alerts/")

    # ── 공통 ────────────────────────────────────────────────────────────
    def _user(self, email, name):
        U = get_user_model()
        user, _ = U.objects.get_or_create(email=email, defaults={"full_name": name})
        user.full_name = name
        user.is_active = True
        if hasattr(user, "is_email_verified"):
            user.is_email_verified = True
        user.set_password(PASSWORD)
        user.save()
        return user

    def _workspace(self, user, name):
        from apps.workspace.models import Membership, Workspace

        ws = Workspace.objects.filter(owner=user).first()
        if ws is None:
            ws = Workspace.objects.create(
                name=name, slug=f"home-alert-{uuid.uuid4().hex[:8]}", owner=user
            )
        Membership.objects.get_or_create(
            workspace=ws, user=user, defaults={"role": Membership.Role.OWNER}
        )
        return ws

    def _subscription(self, user, plan_name="free", **fields):
        from apps.billing.models import SubscriptionPlan, UserSubscription

        plan = SubscriptionPlan.objects.get(name=plan_name)
        sub, _ = UserSubscription.objects.get_or_create(user=user, defaults={"plan": plan})
        sub.plan = plan
        for k, v in fields.items():
            setattr(sub, k, v)
        if not sub.toss_customer_key:
            sub.toss_customer_key = f"tf_{uuid.uuid4().hex}"
        sub.save()
        return sub

    def _connection(self, ws, username, ext_suffix, **fields):
        from apps.integrations.models import IGAccountConnection

        ext_id = f"1790000000000{ext_suffix:04d}"
        conn, _ = IGAccountConnection.objects.get_or_create(
            workspace=ws, external_account_id=ext_id, defaults={"username": username}
        )
        conn.username = username
        conn.account_type = "BUSINESS"
        conn.access_token = f"DEVFAKE-{ext_id}"  # 가짜 — Graph 호출은 어차피 안 한다
        conn.token_expires_at = timezone.now() + timedelta(days=60)
        conn.status = IGAccountConnection.Status.ACTIVE
        conn.is_active = True
        conn.last_verified_at = timezone.now()
        conn.followers_count = 3400
        conn.media_count = 52
        for k, v in fields.items():
            setattr(conn, k, v)
        conn.save()
        return conn

    def _campaign(self, conn, name, media_id, **fields):
        from apps.integrations.models import AutoDMCampaign

        camp, _ = AutoDMCampaign.objects.get_or_create(
            ig_connection=conn,
            name=name,
            defaults={
                "trigger_type": AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
                "media_id": media_id,
            },
        )
        camp.media_id = media_id
        camp.message_template = "안녕하세요! 신청 감사합니다 🙌"
        camp.keyword_filter = ["신청"]
        camp.status = AutoDMCampaign.Status.ACTIVE
        for k, v in fields.items():
            setattr(camp, k, v)
        camp.save()
        return camp

    # ── 1. 알림 0건 ─────────────────────────────────────────────────────
    @transaction.atomic
    def seed_clean(self):
        from apps.pages.models import Block, Page

        user = self._user(CLEAN, "홈알림 정상")
        ws = self._workspace(user, "홈알림 정상")
        self._subscription(user, "free")
        conn = self._connection(ws, "home.clean", 1)
        # 최근 게시물이 있어도 캠페인이 붙어 있으면 라1 이 안 뜬다
        conn.latest_media_id = "17900000000001001"
        conn.latest_media_at = timezone.now() - timedelta(days=2)
        conn.latest_media_permalink = "https://www.instagram.com/p/CLEAN01/"
        conn.latest_media_checked_at = timezone.now()
        conn.webhook_healthy = True
        conn.webhook_checked_at = timezone.now()
        conn.save()
        self._campaign(conn, "[정상] 최신 게시물 캠페인", "17900000000001001")

        page, _ = Page.objects.get_or_create(
            user=user, slug=f"home-clean-{user.id}", defaults={"title": "정상 페이지"}
        )
        page.is_public = True
        page.is_active = True
        page.save()
        if not page.blocks.exists():
            Block.objects.create(
                page=page, type=Block.BlockType.SINGLE_LINK, order=1, data={"title": "링크"}
            )
        self.stdout.write(self.style.SUCCESS(f"[clean] {CLEAN} — 알림 0건(기본 상태만)"))
        return CLEAN

    # ── 2. 멈춤 6종 ─────────────────────────────────────────────────────
    @transaction.atomic
    def seed_critical(self):
        from apps.billing.models import SubscriptionStatus
        from apps.integrations.models import (
            AutoDMCampaign,
            DMAccountBlock,
            IGAccountConnection,
            SentDMLog,
        )

        now = timezone.now()
        user = self._user(CRITICAL, "홈알림 멈춤")
        ws = self._workspace(user, "홈알림 멈춤")

        # 다1 결제 실패 (past_due)
        # basic — pro 는 DM 무제한이라 한도 소진을 재현할 수 없다(플랜 features 확인).
        self._subscription(
            user,
            "basic",
            status=SubscriptionStatus.PAST_DUE,
            current_period_end=now - timedelta(days=2),
            next_billing_retry_at=now + timedelta(days=1),
            card_company="신한",
            card_number_masked="45184445****364*",
            monthly_amount_snapshot=14900,
            extra_ig_accounts=2,  # 허용량 3 — 계정 3개를 켜도 강제팝업이 안 뜨게
        )

        # 가1 연결 끊김
        dead = self._connection(
            ws,
            "home.dead",
            11,
            status=IGAccountConnection.Status.ERROR,
            reconnect_reason="token_invalidated",
            token_dead_strikes=3,
            token_dead_first_seen_at=now - timedelta(days=1),
        )

        # 가4 실시간 댓글 수신 끊김 (재구독까지 실패한 상태로 기록)
        live = self._connection(
            ws, "home.blocked", 12, webhook_healthy=False, webhook_checked_at=now
        )

        # 나1 인스타 발송 제한 — cache miss 여도 DB 폴백이 살린다
        DMAccountBlock.objects.update_or_create(
            external_account_id=live.external_account_id,
            defaults={"cooldown_until": now + timedelta(hours=6), "level": 1},
        )

        # 나3 게시물 제한으로 자동 정지된 캠페인
        self._campaign(
            live,
            "[멈춤] 연령제한 게시물 캠페인",
            "17900000000002001",
            status=AutoDMCampaign.Status.PAUSED,
            auto_paused_at=now - timedelta(hours=5),
            auto_paused_reason="post_restricted",
            media_url="https://www.instagram.com/reel/BLOCKED1/",
        )

        # 나2 월 한도 소진 — pro 한도만큼 채우고, 막힌 건도 함께 만든다
        camp = self._campaign(live, "[멈춤] 한도 소진 캠페인", "17900000000002002")
        limit = self._fill_quota(user, camp)
        blocked = self._make_quota_skips(camp, count=37)

        # 대기 중인 발송(발송 제한 배너의 waiting_count)
        SentDMLog.objects.filter(campaign=camp, status=SentDMLog.Status.QUEUED).delete()
        SentDMLog.objects.bulk_create(
            [
                SentDMLog(
                    campaign=camp,
                    comment_id=f"seedq{i}",
                    recipient_user_id=f"seed_wait_{i}",
                    recipient_username=f"waiting_{i}",
                    message_sent="대기 중",
                    status=SentDMLog.Status.QUEUED,
                    idempotency_key=f"home-seed-wait-{camp.id}-{i}",
                )
                for i in range(12)
            ]
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"[critical] {CRITICAL} — 결제실패·연결끊김({dead.username})·수신끊김·"
                f"발송제한·게시물제한·한도소진(limit={limit}, 막힌 건={blocked})"
            )
        )
        return CRITICAL

    def _fill_quota(self, user, campaign):
        """한도를 채운다 — 집계 단위가 (캠페인 × 수신자) 고유쌍이라 그만큼 만든다."""
        from apps.billing.dm_limits import get_dm_monthly_limit
        from apps.integrations.campaign_stats import _month_bounds
        from apps.integrations.models import SentDMLog

        limit = get_dm_monthly_limit(user)
        if limit < 0:
            return limit
        period_start, _ = _month_bounds()
        base = max(period_start, timezone.now() - timedelta(days=5))
        existing = set(
            SentDMLog.objects.filter(
                campaign=campaign, status=SentDMLog.Status.DELIVERED
            ).values_list("recipient_user_id", flat=True)
        )
        rows = [
            SentDMLog(
                campaign=campaign,
                comment_id=f"seedc{i}",
                recipient_user_id=f"seed_sent_{i}",
                recipient_username=f"sent_{i}",
                message_sent="발송 완료",
                status=SentDMLog.Status.DELIVERED,
                idempotency_key=f"home-seed-sent-{campaign.id}-{i}",
                created_at=base,
                delivered_at=base,
            )
            for i in range(limit)
            if f"seed_sent_{i}" not in existing
        ]
        if rows:
            SentDMLog.objects.bulk_create(rows, batch_size=200)
        # 한도 도달 캐시 플래그를 지워 다음 조회가 실제 집계를 다시 하도록 둔다
        from django.core.cache import cache

        from apps.billing.dm_limits import _quota_hit_cache_key

        cache.delete(_quota_hit_cache_key(user.id))
        return limit

    def _make_quota_skips(self, campaign, count):
        """한도 때문에 나가지 못한 건 (blocked_count / resumable_count 재현)."""
        from apps.billing.dm_limits import QUOTA_SKIP_REASON
        from apps.integrations.models import SentDMLog

        SentDMLog.objects.filter(
            campaign=campaign, status=SentDMLog.Status.SKIPPED, error_message=QUOTA_SKIP_REASON
        ).delete()
        now = timezone.now()
        rows = [
            SentDMLog(
                campaign=campaign,
                comment_id=f"seedskip{i}",
                recipient_user_id=f"seed_skip_{i}",
                recipient_username=f"skipped_{i}",
                message_sent="",
                status=SentDMLog.Status.SKIPPED,
                error_message=QUOTA_SKIP_REASON,
                idempotency_key=f"home-seed-skip-{campaign.id}-{i}",
            )
            for i in range(count)
        ]
        SentDMLog.objects.bulk_create(rows, batch_size=200)
        # ⚠️ created_at 은 auto_now_add 라 생성 시 지정이 **무시된다** — update() 로만 덮인다.
        # 일부를 되살림 창(7일) 밖으로 밀어 blocked_count > resumable_count 를 재현한다.
        old_ids = [f"home-seed-skip-{campaign.id}-{i}" for i in range(min(6, count))]
        SentDMLog.objects.filter(idempotency_key__in=old_ids).update(
            created_at=now - timedelta(days=9)
        )
        return count

    # ── 3. 권유 4종 + 한도 경고 ──────────────────────────────────────────
    @transaction.atomic
    def seed_todo(self):
        from apps.billing.models import SubscriptionStatus
        from apps.insta_reports.models import InstagramReport, ReportStatus
        from apps.integrations.models import DMCampaignCandidate, DMMigrationJob
        from apps.pages.models import Page

        now = timezone.now()
        user = self._user(TODO, "홈알림 권유")
        ws = self._workspace(user, "홈알림 권유")
        # basic — 한도 경고(80%)를 재현하려면 유한 한도 플랜이어야 한다.
        # (리포트는 원래 프로 전용이지만 여기서는 행을 직접 심어 배너만 확인한다.)
        self._subscription(
            user,
            "basic",
            status=SubscriptionStatus.ACTIVE,
            current_period_end=now + timedelta(days=12),
            extra_ig_accounts=1,
        )
        conn = self._connection(ws, "home.todo", 21, webhook_healthy=True, webhook_checked_at=now)

        # 라1 최근 7일 이내 게시물인데 캠페인이 없다
        conn.latest_media_id = "17900000000003001"
        conn.latest_media_at = now - timedelta(days=1, hours=3)
        conn.latest_media_permalink = "https://www.instagram.com/p/TODO001/"
        conn.latest_media_checked_at = now
        conn.save()

        # 라2 는 캠페인이 1건이라도 있으면 「기본 상태」로 맨 뒤에 간다 → 하나 만들어 둔다
        self._campaign(conn, "[권유] 기존 캠페인", "17900000000003999")

        # 나4 한도 80% — pro 한도의 85% 를 채운다
        self._fill_quota_ratio(user, conn, 0.85)

        # 라3 링크 페이지가 비공개 + 블록 0
        page, _ = Page.objects.get_or_create(
            user=user, slug=f"home-todo-{user.id}", defaults={"title": "빈 페이지"}
        )
        page.is_public = False
        page.is_active = True
        page.save()
        page.blocks.all().delete()

        # 라4 리포트 완료
        InstagramReport.objects.filter(workspace=ws).delete()
        InstagramReport.objects.create(
            workspace=ws,
            ig_connection=conn,
            requested_by=user,
            ig_username=conn.username,
            status=ReportStatus.SUCCEEDED,
        )

        # 라5 불러온 캠페인 검수 대기
        job, _ = DMMigrationJob.objects.get_or_create(
            ig_connection=conn,
            requested_by=user,
            defaults={"status": "ready"},
        )
        DMCampaignCandidate.objects.filter(job=job).delete()
        DMCampaignCandidate.objects.bulk_create(
            [
                DMCampaignCandidate(
                    job=job,
                    ig_connection=conn,
                    status=DMCampaignCandidate.Status.DETECTED,
                    band=DMCampaignCandidate.Band.NEEDS_REVIEW,
                    media_id=f"1790000000000400{i}",
                )
                for i in range(3)
            ]
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"[todo] {TODO} — 새 게시물·빈 링크페이지·리포트 완료·검수 대기 3건·한도 85%"
            )
        )
        return TODO

    def _fill_quota_ratio(self, user, conn, ratio):
        from apps.billing.dm_limits import get_dm_monthly_limit
        from apps.integrations.models import SentDMLog

        limit = get_dm_monthly_limit(user)
        if limit < 0:
            return
        camp = self._campaign(conn, "[권유] 한도 경고용", "17900000000003888")
        target = int(limit * ratio)
        SentDMLog.objects.filter(campaign=camp, status=SentDMLog.Status.DELIVERED).delete()
        now = timezone.now()
        SentDMLog.objects.bulk_create(
            [
                SentDMLog(
                    campaign=camp,
                    comment_id=f"seedw{i}",
                    recipient_user_id=f"seed_warn_{i}",
                    recipient_username=f"warn_{i}",
                    message_sent="발송 완료",
                    status=SentDMLog.Status.DELIVERED,
                    idempotency_key=f"home-seed-warn-{camp.id}-{i}",
                    created_at=now - timedelta(days=2),
                    delivered_at=now - timedelta(days=2),
                )
                for i in range(target)
            ],
            batch_size=200,
        )
        from django.core.cache import cache

        from apps.billing.dm_limits import _quota_hit_cache_key

        cache.delete(_quota_hit_cache_key(user.id))

    # ── 4. 강제 팝업 ─────────────────────────────────────────────────────
    @transaction.atomic
    def seed_blocking(self):
        from apps.billing.models import SubscriptionStatus

        now = timezone.now()
        user = self._user(BLOCKING, "홈알림 강제선택")
        ws = self._workspace(user, "홈알림 강제선택")
        # 무료 = 허용량 1개인데 3개가 켜져 있다 → 반드시 골라야 넘어간다
        self._subscription(
            user,
            "free",
            status=SubscriptionStatus.ACTIVE,
            extra_ig_accounts=0,
            pending_extra_ig_accounts=None,
            current_period_end=now + timedelta(days=20),
        )
        for i in range(3):
            self._connection(
                ws, f"home.pick{i}", 31 + i, webhook_healthy=True, webhook_checked_at=now
            )
        self.stdout.write(
            self.style.SUCCESS(f"[blocking] {BLOCKING} — 허용량 1 / 켜진 계정 3 → 강제 팝업")
        )
        return BLOCKING
