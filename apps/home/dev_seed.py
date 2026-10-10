"""홈 화면(V2) 프론트 확인용 **dev 테스트 계정** 시드 — 상태 정의 단일 소스.

프론트 요청서 ``backend-home-test-accounts.md``(2026-10-08)의 H/P/I/A 코드를 그대로
계정 1개씩으로 만든다. 목적은 하나다 — **로그인만 하면 그 상태의 화면이 바로 보인다.**

설계 원칙
---------
1. **실제 판정을 그대로 탄다.** 알림을 고정값으로 주입하지 않고 :mod:`apps.home.alerts` 가
   읽는 DB 상태를 만든다. 고정 주입은 "시드에선 보이는데 실계정에선 안 뜬다"를 만든다.
2. **Meta 를 부르지 않는다.** 토큰은 ``MockInstagramProvider.MOCK_TOKEN_PREFIX`` 접두어를
   쓴다 — ``should_use_mock()`` 이 이 접두어를 보고 게시물·댓글·헬스 조회를 목으로 돌린다.
   ⚠️ **네 번째 토큰 관례를 만들지 말 것**(services.py ``MOCK_TOKEN_PREFIXES`` 주석 참고).
3. **게시물 id 는 목 풀에서 가져온다.** 손으로 지어낸 ``179000…`` 를 쓰면 목록·배치조회에
   없는 id 가 되어 썸네일이 통째로 비고, 프론트는 "사진이 안 나온다"만 보게 된다
   (종전 ``seed_home_alerts_dev`` 가 실제로 그랬다).
4. **매번 같은 상태로 되돌린다.** 시드는 그 계정의 파생 데이터를 전부 지우고 다시 만든다 —
   알림을 닫거나 버튼을 눌러 상태가 바뀌어도 재시드 한 번이면 원점이다.
5. **시간 값은 재시드 시점 기준으로 다시 계산된다.** 남은 일수·결제 유예일·"2시간 전"은
   전부 ``now`` 상대값이라 며칠 뒤에 다시 돌려도 같은 화면이 나온다.

⚠️ ``DEBUG=True`` 에서만 동작한다(:func:`guard`). 운영 DB 에 테스트 계정을 만들지 않는다.
"""

from __future__ import annotations

import uuid
import zlib
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

PASSWORD = "Test1234!"
EMAIL_DOMAIN = "test.turnflow.link"

# dev 전용 제휴코드 (P11). 보너스 14일 — 자동 체험 30일 + 14일 = 44일.
REFERRAL_CODE = "DEVHOME14"
REFERRAL_BONUS_DAYS = 14

# 외부 호스트 의존 0 — 프로필 사진도 data URI 로 만든다.
# (IG CDN URL 은 서명 만료라 저장할 수 없고, 외부 더미 이미지 호스트는 죽는 순간
#  dev 화면이 통째로 깨진다 — 목 썸네일을 data URI 로 만든 것과 같은 이유.)
_AVATAR_BG = ("#6366f1", "#ec4899", "#14b8a6", "#f59e0b", "#8b5cf6")


def avatar_data_uri(seed: int, letter: str) -> str:
    """320x320 원형 아바타 (네트워크 호출 0, ``URLField(max_length=1024)`` 안에 들어간다)."""
    bg = _AVATAR_BG[seed % len(_AVATAR_BG)]
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='320'>"
        "<rect width='320' height='320' fill='" + bg + "'/>"
        "<text x='160' y='212' font-family='sans-serif' font-size='150' fill='#fff' "
        "text-anchor='middle'>" + letter + "</text></svg>"
    )
    return "data:image/svg+xml;utf8," + quote(svg)


# ── 공통 예시 데이터 (요청서 「공통 예시 데이터」) ────────────────────────────
# (이름, 종류) — 목록이 「최근 수정 순」으로 이 순서가 되도록 created_at 을 역순으로 박는다.
COMMON_CAMPAIGNS = [
    ("신제품 이벤트", "active"),
    ("팝업 스토어 예약", "paused"),
    ("말차 신메뉴 안내", "inactive"),
    ("가을 신상 미리 알림", "restricted"),
    ("여름 코디 할인", "completed"),
    ("10월 한정 신상품 출시 기념 댓글 이벤트 자동 안내 메시지", "active"),
    ("가을 피드 리뷰 이벤트", "active"),
]


def guard():
    """운영 보호 — dev 에서만 돈다.

    ``DEBUG`` 와 **설정 모듈** 둘 다 본다. 이 시더는 계정 데이터를 지우고 다시 만들고,
    HTTP 진입점(:mod:`apps.home.dev_views`)에는 인증이 없다 — 한 겹으로 두지 않는다.
    """
    if not settings.DEBUG:
        raise RuntimeError("거부: DEBUG=True 환경에서만 실행할 수 있습니다 (운영 데이터 보호).")
    if "prod" in (getattr(settings, "SETTINGS_MODULE", "") or ""):
        raise RuntimeError("거부: prod 설정에서는 실행할 수 없습니다 (운영 데이터 보호).")


@dataclass
class SeedResult:
    code: str
    email: str
    password: str
    title: str
    summary: str
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "email": self.email,
            "password": self.password,
            "title": self.title,
            "summary": self.summary,
            "notes": self.notes,
        }


# ═══════════════════════════════════════════════════════════════════════════
# 시드 작업대
# ═══════════════════════════════════════════════════════════════════════════


class Account:
    """계정 1개를 **매번 같은 상태로** 다시 만드는 작업대.

    생성자가 기존 파생 데이터를 전부 지우므로(:meth:`_wipe`) 빌더는 "만들기"만 하면 된다.
    """

    def __init__(self, code: str, title: str, *, email: str = "", name: str | None = None):
        self.code = code
        self.title = title
        self.now = timezone.now()
        self.notes: list[str] = []
        self.email = email or f"home-{code.lower()}@{EMAIL_DOMAIN}"
        self.user = self._user(self.email, title if name is None else name)
        self.ws = self._workspace()
        self._conn_seq = 0
        self._wipe()

    # ── 사용자 / 워크스페이스 ──────────────────────────────────────────
    def _user(self, email, name):
        model = get_user_model()
        user = model.objects.filter(email=email).first()
        if user is None:
            user = model(email=email)
        user.full_name = name
        user.is_active = True
        user.is_email_verified = True
        user.email_verified_at = self.now
        user.kakao_id = None
        user.instagram_user_id = None
        user.pending_email = ""
        user.popup_state = {}
        user.deletion_requested_at = None
        user.deletion_scheduled_at = None
        user.set_password(PASSWORD)
        user.save()
        return user

    def _workspace(self):
        from apps.workspace.models import Membership, Workspace

        ws = Workspace.objects.filter(owner=self.user).order_by("created_at").first()
        if ws is None:
            ws = Workspace.objects.create(
                name=self.title,
                slug=f"home-{self.code.lower()}-{uuid.uuid4().hex[:6]}",
                owner=self.user,
            )
        elif ws.name != self.title:
            ws.name = self.title
            ws.save(update_fields=["name"])
        Membership.objects.get_or_create(
            workspace=ws, user=self.user, defaults={"role": Membership.Role.OWNER}
        )
        # 워크스페이스가 둘 이상이면 홈이 400 을 낸다(views._resolve_workspace) — 하나만 둔다.
        Workspace.objects.filter(owner=self.user).exclude(id=ws.id).delete()
        return ws

    def _wipe(self):
        """이 계정의 파생 데이터를 전부 지운다 — 재시드가 곧 원상복구가 되도록."""
        from apps.billing.models import PaymentConsent, PaymentHistory
        from apps.insta_reports.models import InstagramReport
        from apps.integrations.models import DMAccountBlock, DMMigrationJob, IGAccountConnection
        from apps.pages.models import Page

        from .models import HomeAlertDismissal

        ext_ids = list(
            IGAccountConnection.objects.filter(workspace=self.ws).values_list(
                "external_account_id", flat=True
            )
        )
        DMMigrationJob.objects.filter(ig_connection__workspace=self.ws).delete()
        # 캠페인·발송로그는 연결 CASCADE 로 함께 사라진다.
        IGAccountConnection.objects.filter(workspace=self.ws).delete()
        DMAccountBlock.objects.filter(external_account_id__in=ext_ids).delete()
        for ext in ext_ids:
            cache.delete(f"dm:ab:cooldown:{ext}")
            cache.delete(f"dm:ab:level:{ext}")
        Page.objects.filter(user=self.user).delete()
        InstagramReport.objects.filter(workspace=self.ws).delete()
        HomeAlertDismissal.objects.filter(user=self.user).delete()
        PaymentHistory.objects.filter(user=self.user).delete()
        PaymentConsent.objects.filter(user=self.user).delete()
        self.subscription("free")  # 구독도 초기 상태로
        self.flush_cache()

    def flush_cache(self):
        """홈 알림 30초 캐시 + 한도 도달 플래그 — 재시드 직후 바로 보이게."""
        from apps.billing.dm_limits import _quota_hit_cache_key

        cache.delete(f"home:alerts:{self.user.id}:{self.ws.id}")
        cache.delete(_quota_hit_cache_key(self.user.id))

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    # ── 구독 ──────────────────────────────────────────────────────────
    def subscription(self, plan_name="free", *, card=None, **fields):
        """구독을 **초기화한 뒤** 지정 값만 덮는다 — 이전 시드의 잔재가 남지 않게.

        ⚠️ ``current_period_end`` 는 되도록 미래로 둘 것. 과거 + 빌링키면
        ``billing.process_due_renewals``(10분 주기)가 **실제로 과금을 시도**한다.
        """
        from apps.billing.models import SubscriptionPlan, SubscriptionStatus, UserSubscription

        plan = SubscriptionPlan.objects.get(name=plan_name)
        sub, _ = UserSubscription.objects.get_or_create(user=self.user, defaults={"plan": plan})
        sub.plan = plan
        sub.status = SubscriptionStatus.ACTIVE
        sub.current_period_start = self.now
        sub.current_period_end = None
        sub.monthly_amount_snapshot = None
        sub.extra_ig_accounts = 0
        sub.pending_plan = None
        sub.pending_amount_snapshot = None
        sub.pending_extra_ig_accounts = None
        sub.trial_used_at = None
        sub.trial_plan = None
        sub.trial_kind = ""
        sub.cancelled_at = None
        sub.cancelled_during_trial_at = None
        sub.renewal_attempts = 0
        sub.next_billing_retry_at = None
        sub.last_billing_error = ""
        sub.pause_ends_at = None
        sub.paused_months = 0
        sub.last_pause_at = None
        sub.pause_resume_reminder_sent_at = None
        sub.retention_discount_pending = False
        sub.retention_discount_used_at = None
        sub.ig_account_activation_changed_at = None
        sub.ig_activation_review_needed = False
        sub.pro_activated_at = None
        sub.clear_billing_key()
        if not sub.toss_customer_key:
            sub.toss_customer_key = f"tf_dev_{uuid.uuid4().hex}"
        for key, value in fields.items():
            setattr(sub, key, value)
        if card:
            sub.set_billing_key(
                f"bk_dev_{self.code.lower()}", card_company=card[0], card_number=card[1]
            )
        sub.save()
        self.sub = sub
        return sub

    # ── IG 연결 ───────────────────────────────────────────────────────
    def connection(self, username, *, photo=True, display_name="", **fields):
        """가짜 IG 연결 1개.

        ``external_account_id`` 는 계정 코드에서 결정적으로 만든다 — **숫자만** 쓴다
        (목 미디어 id 규약 ``mm-{ig}-{i}`` 가 '-' 로 split 하므로 하이픈이 들어가면 안 된다).
        """
        from apps.integrations.models import IGAccountConnection
        from apps.integrations.services import MockInstagramProvider

        self._conn_seq += 1
        ext_id = f"17{zlib.crc32(self.code.encode()):010d}{self._conn_seq:02d}"
        conn = IGAccountConnection(
            workspace=self.ws,
            external_account_id=ext_id,
            username=username,
            name=display_name or username.replace("_", " ").replace(".", " ").title(),
            account_type="BUSINESS",
            token_expires_at=self.now + timedelta(days=60),
            status=IGAccountConnection.Status.ACTIVE,
            is_active=True,
            last_verified_at=self.now,
            followers_count=12840,
            follows_count=312,
            media_count=68,
            webhook_healthy=True,
            webhook_checked_at=self.now,
            profile_picture_url=(
                avatar_data_uri(self._conn_seq, username[0].upper()) if photo else ""
            ),
            profile_picture_synced_at=self.now if photo else None,
        )
        # ⚠️ ``access_token`` 은 모델 필드가 아니라 암호화 디스크립터라 create(...) 에 못 넘긴다.
        #    이 접두어라야 Meta 호출 없이 목으로 처리된다(services.should_use_mock).
        conn.access_token = f"{MockInstagramProvider.MOCK_TOKEN_PREFIX}{ext_id}"
        for key, value in fields.items():
            setattr(conn, key, value)
        conn.save()
        return conn

    def media_pool(self, conn, limit=12):
        """이 연결의 **목 게시물 목록** — 화면이 실제로 조회하는 것과 같은 소스.

        ``GET /integrations/instagram/connections/{id}/media/`` 가 돌려주는 바로 그
        항목들이라, 여기서 고른 ``media_id`` 는 썸네일·캡션이 반드시 따라온다.
        """
        from apps.integrations.services import MockInstagramProvider

        page = MockInstagramProvider.mock_list_media_page(conn.external_account_id, limit=limit)
        return page["data"]

    def latest_post(self, conn, *, hours_ago=2, media=None):
        """「새 게시물」 큰 카드가 읽는 값.

        Graph 호출 0 — 주기잡(``integrations.refresh_latest_media``)이 적어 두는 자리를
        직접 채운다. 홈 알림은 이 컬럼만 보고 판정한다.
        """
        item = media or self.media_pool(conn)[0]
        conn.latest_media_id = item["id"]
        conn.latest_media_at = self.now - timedelta(hours=hours_ago)
        conn.latest_media_permalink = item["permalink"]
        conn.latest_media_checked_at = self.now
        conn.save(
            update_fields=[
                "latest_media_id",
                "latest_media_at",
                "latest_media_permalink",
                "latest_media_checked_at",
            ]
        )
        return item

    # ── 캠페인 ────────────────────────────────────────────────────────
    def campaign(
        self, conn, name, media, *, kind="active", age_minutes=0, thumbnail=None, **fields
    ):
        """자동 DM 캠페인 1개. ``kind`` 는 프론트 카드 상태와 1:1.

        ⚠️ 서버에 ``draft``/``error`` 라는 status 값은 **없다**:
          · 작성 중(초안) = ``inactive``
          · 오류          = ``paused`` + ``auto_paused_reason="post_restricted"``
        """
        from apps.integrations.models import AutoDMCampaign

        status_map = {
            "active": AutoDMCampaign.Status.ACTIVE,
            "paused": AutoDMCampaign.Status.PAUSED,
            "inactive": AutoDMCampaign.Status.INACTIVE,
            "completed": AutoDMCampaign.Status.COMPLETED,
            "restricted": AutoDMCampaign.Status.PAUSED,
        }
        camp = AutoDMCampaign.objects.create(
            ig_connection=conn,
            name=name,
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id=media["id"],
            media_url=media["permalink"],
            # 「오류」 카드는 "게시물 사진 없음" 을 보여야 한다(thumbnail=False 로 따로도 지정 가능).
            thumbnail_url=(
                media["thumbnail_url"]
                if (kind != "restricted" if thumbnail is None else thumbnail)
                else ""
            ),
            message_template="안녕하세요! 신청 감사합니다 🙌 아래 링크에서 확인해 주세요.",
            opening_message_template="안녕하세요! 신청 감사합니다 🙌 아래 링크에서 확인해 주세요.",
            keyword_filter=["신청", "링크"],
            status=status_map[kind],
        )
        if kind == "restricted":
            camp.auto_paused_reason = "post_restricted"
            camp.auto_paused_at = self.now - timedelta(hours=5)
        for key, value in fields.items():
            setattr(camp, key, value)
        camp.save()
        if age_minutes:
            # created_at 은 auto_now_add 라 update() 로만 덮인다. 목록이 「최근 수정 순」으로
            # 요청서 순서와 같아지게 하려면 여기서 역순 시각을 박아야 한다.
            stamp = self.now - timedelta(minutes=age_minutes)
            AutoDMCampaign.objects.filter(pk=camp.pk).update(created_at=stamp, updated_at=stamp)
            camp.refresh_from_db()
        return camp

    def common_campaigns(self, conn, *, skip_latest=True, error_campaign=True):
        """요청서 「자동 DM 7개」 — 최근 수정 순으로 들어간다.

        ``skip_latest`` 면 목 풀의 0번(= 최신 게시물)은 쓰지 않는다. 그래야 그 게시물에
        캠페인이 없어 ``recent_post_no_campaign`` 이 뜬다.

        ⚠️ ``error_campaign=False`` 는 「오류」 캠페인을 평범한 '중지됨'(썸네일만 없음)으로
        바꾼다. **오류 캠페인은 반드시 ``campaign_post_restricted`` 알림을 동반하기 때문**이다
        (판정이 ``auto_paused_reason='post_restricted'`` 하나뿐이고, 그게 유일한 오류 사유다).
        그 알림은 rank 60 이라 「새 게시물」·「정상 작동 중」 큰 카드를 밀어낸다 — 큰 카드를
        확인해야 하는 계정에서는 꺼야 한다.
        """
        pool = self.media_pool(conn)
        offset = 1 if skip_latest else 0
        made = []
        for i, (name, kind) in enumerate(COMMON_CAMPAIGNS):
            thumbnail = None
            if kind == "restricted" and not error_campaign:
                kind, thumbnail = "paused", False
            made.append(
                self.campaign(
                    conn,
                    name,
                    pool[offset + i],
                    kind=kind,
                    age_minutes=i * 37,
                    thumbnail=thumbnail,
                )
            )
        return made

    # ── 링크 페이지 ───────────────────────────────────────────────────
    def page(self, slug, title, *, public=True, blocks=3):
        from apps.pages.models import Block, Page

        page = Page.objects.create(
            user=self.user,
            slug=f"{slug}-{self.code.lower()}",
            title=title,
            is_public=public,
            is_active=True,
        )
        if blocks:
            Block.objects.create(
                page=page,
                type=Block.BlockType.PROFILE,
                order=0,
                data={"name": title, "bio": "샘플 소개 문구입니다."},
            )
            for i in range(blocks):
                Block.objects.create(
                    page=page,
                    type=Block.BlockType.SINGLE_LINK,
                    order=i + 1,
                    data={
                        "_type": "single_link",
                        "title": f"샘플 링크 {i + 1}",
                        "url": "https://turnflow.link/",
                    },
                )
        return page

    def common_pages(self):
        """요청서 「링크 페이지 2개」 — 공개(블록 있음) + 비공개."""
        return [
            self.page("sample-creator", "샘플 크리에이터", public=True, blocks=3),
            self.page("sample-studio", "샘플 스튜디오", public=False, blocks=2),
        ]

    # ── 그 밖의 재료 ──────────────────────────────────────────────────
    def report(self, conn, status, **fields):
        from apps.insta_reports.models import InstagramReport, ReportStatus

        rep = InstagramReport.objects.create(
            workspace=self.ws,
            ig_connection=conn,
            requested_by=self.user,
            ig_username=conn.username,
            ig_name=conn.name,
            status=status,
            **fields,
        )
        if status in (ReportStatus.RUNNING, ReportStatus.QUEUED):
            self.note(
                "「리포트 만드는 중」 은 insta_reports.sweep_stale(30분 주기)이 생성 60분 뒤 "
                "실패로 확정한다 — 1시간 안에 확인하거나 재시드할 것."
            )
        return rep

    def migration_candidates(self, conn, count=3):
        from apps.integrations.models import DMCampaignCandidate, DMMigrationJob

        job = DMMigrationJob.objects.create(
            ig_connection=conn, requested_by=self.user, status="ready"
        )
        pool = self.media_pool(conn)
        DMCampaignCandidate.objects.bulk_create(
            [
                DMCampaignCandidate(
                    job=job,
                    ig_connection=conn,
                    status=DMCampaignCandidate.Status.DETECTED,
                    band=DMCampaignCandidate.Band.NEEDS_REVIEW,
                    media_id=pool[8 + i]["id"],
                    draft_name=f"불러온 캠페인 {i + 1}",
                    draft_opening_message="안녕하세요! 요청하신 자료 보내드려요 😊",
                )
                for i in range(count)
            ]
        )
        return job

    def action_block(self, conn, *, hours=3):
        """「인스타그램이 발송을 잠시 막음」 — 쿨다운의 내구 저장소에 적는다.

        캐시 키를 지워 두면 다음 조회가 DB 로 폴백하면서 캐시를 다시 프라임한다
        (rate_governor 의 정상 경로 — 캐시 값을 손으로 심지 않는다).
        """
        from apps.integrations.models import DMAccountBlock

        DMAccountBlock.objects.update_or_create(
            external_account_id=conn.external_account_id,
            defaults={"cooldown_until": self.now + timedelta(hours=hours), "level": 1},
        )
        cache.delete(f"dm:ab:cooldown:{conn.external_account_id}")
        cache.delete(f"dm:ab:level:{conn.external_account_id}")

    def queued_dms(self, campaign, count=12):
        """발송 대기 중인 건 — ``dm_send_blocked`` 의 ``waiting_count``."""
        from apps.integrations.models import SentDMLog

        SentDMLog.objects.bulk_create(
            [
                SentDMLog(
                    campaign=campaign,
                    comment_id=f"{self.code}-q-{i}",
                    recipient_user_id=f"{self.code}_wait_{i}",
                    recipient_username=f"waiting_{i}",
                    message_sent="대기 중",
                    status=SentDMLog.Status.QUEUED,
                    idempotency_key=f"seed-{self.code}-wait-{campaign.id}-{i}",
                )
                for i in range(count)
            ]
        )

    def fill_quota(self, campaign, *, ratio=None, count=None):
        """월 DM 한도를 채운다. 집계 단위가 (캠페인 × 수신자) 고유쌍이라 그만큼 만든다.

        ⚠️ **프로는 한도가 무제한**(``features.dm_monthly_limit = -1``)이라 한도 알림을
        재현할 수 없다 — 한도 계정(H05·H09)은 전부 free/basic 이다.
        """
        from apps.billing.dm_limits import get_dm_monthly_limit
        from apps.integrations.models import SentDMLog

        limit = get_dm_monthly_limit(self.user)
        if limit < 0:
            return 0, limit
        target = count if count is not None else int(limit * ratio)
        base = self.now - timedelta(days=3)
        SentDMLog.objects.bulk_create(
            [
                SentDMLog(
                    campaign=campaign,
                    comment_id=f"{self.code}-s-{i}",
                    recipient_user_id=f"{self.code}_sent_{i}",
                    recipient_username=f"sent_{i}",
                    message_sent="발송 완료",
                    status=SentDMLog.Status.DELIVERED,
                    idempotency_key=f"seed-{self.code}-sent-{campaign.id}-{i}",
                    delivered_at=base,
                )
                for i in range(target)
            ],
            batch_size=300,
        )
        # created_at 은 auto_now_add — 이번 달 집계 안에 들어가도록 update() 로 덮는다.
        SentDMLog.objects.filter(campaign=campaign, status=SentDMLog.Status.DELIVERED).update(
            created_at=base
        )
        self.flush_cache()
        return target, limit

    def quota_skips(self, campaign, *, blocked=37, resumable=31):
        """한도 때문에 못 나간 건. 되살림 창(댓글 7일) 밖으로 민 것이 ``blocked − resumable``.

        ⚠️ 두 숫자의 정의가 다르다 — ``blocked_count`` 는 **이번 달**, ``resumable_count`` 는
        **최근 7일**이다. 그래서 '막혔지만 못 살리는' 건은 *이번 달 안이면서 7일보다 오래된*
        구간에만 존재한다. 매월 1~7일에는 그 구간이 아예 없어 두 숫자가 같아진다(정상).
        """
        from apps.billing.dm_limits import QUOTA_SKIP_REASON
        from apps.integrations.campaign_stats import _month_bounds
        from apps.integrations.models import SentDMLog

        SentDMLog.objects.bulk_create(
            [
                SentDMLog(
                    campaign=campaign,
                    comment_id=f"{self.code}-k-{i}",
                    recipient_user_id=f"{self.code}_skip_{i}",
                    recipient_username=f"skipped_{i}",
                    message_sent="",
                    status=SentDMLog.Status.SKIPPED,
                    error_message=QUOTA_SKIP_REASON,
                    idempotency_key=f"seed-{self.code}-skip-{campaign.id}-{i}",
                )
                for i in range(blocked)
            ],
            batch_size=300,
        )
        stale = [f"seed-{self.code}-skip-{campaign.id}-{i}" for i in range(blocked - resumable)]
        period_start, _ = _month_bounds()
        stale_at = max(period_start + timedelta(hours=1), self.now - timedelta(days=8))
        SentDMLog.objects.filter(idempotency_key__in=stale).update(created_at=stale_at)
        if stale_at > self.now - timedelta(days=7):
            self.note(
                "이번 달 초라 '막혔지만 바로 못 살리는' 구간이 없다 — "
                f"blocked_count 와 resumable_count 가 똑같이 {blocked} 로 나온다(정상)."
            )

    def quiet_base(self, *, username="sample_creator", campaigns=1, pages=True):
        """ "알릴 게 없는" 바탕 — 확인하려는 상태 **하나만** 남기기 위한 공통 밑작업.

        연결 1개(최신 게시물에 캠페인이 붙어 있음) + 공개 링크 페이지(블록 있음).
        이렇게 해두면 ``recent_post_no_campaign`` 과 ``link_page_empty`` 가 뜨지 않고,
        캠페인이 1개 이상이라 ``create_campaign`` 은 맨 뒤(rank 999 기본 상태)로 간다.
        """
        conn = self.connection(username)
        pool = self.media_pool(conn)
        self.latest_post(conn, media=pool[0])
        made = [
            self.campaign(conn, f"샘플 캠페인 {i + 1}", pool[i], age_minutes=i * 41)
            for i in range(campaigns)
        ]
        # 뒤에서 "이 캠페인에 발송 로그를 붙여라" 로 쓴다 (related_name 은 dm_campaigns).
        self.base_campaign = made[0] if made else None
        if pages:
            self.page("sample-creator", "샘플 크리에이터", public=True, blocks=3)
        return conn


# ═══════════════════════════════════════════════════════════════════════════
# 실행
# ═══════════════════════════════════════════════════════════════════════════

# I06 은 **인스타그램으로 가입한 계정**이라 이메일이 자리표시여야 한다
# (`email_is_placeholder` 가 그 값을 보고 "이메일 등록 안내" 를 띄운다).
SPECIAL_EMAILS = {"I06": "ig_devhome06@ig.invalid"}


def ensure_referral_code():
    """dev 전용 제휴코드 — P11 이 쓸 '유효한 코드' 가 dev 에 하나도 없었다."""
    from apps.billing.models import ReferralCode, SubscriptionPlan

    code, _ = ReferralCode.objects.update_or_create(
        code=REFERRAL_CODE,
        defaults={
            "description": "dev 홈 테스트 전용 (보너스 14일)",
            "target_plan": SubscriptionPlan.objects.get(name="pro"),
            "trial_days": REFERRAL_BONUS_DAYS,
            "is_active": True,
            "excluded_from_stats": True,
            "max_uses": None,
            "valid_from": None,
            "valid_until": None,
        },
    )
    return code


def run(codes=None, *, ig_user_id: str = "") -> list[SeedResult]:
    """지정 코드(미지정이면 전부)를 시드하고 결과를 돌려준다.

    계정 하나하나가 독립 트랜잭션이다 — 한 코드가 깨져도 나머지는 남는다.
    """
    from .dev_seed_specs import ALL_CODES, SPEC_BY_CODE

    guard()
    ensure_referral_code()

    wanted = list(codes or ALL_CODES)
    unknown = [c for c in wanted if c not in SPEC_BY_CODE]
    if unknown:
        raise ValueError(f"알 수 없는 코드: {', '.join(unknown)}")

    results = []
    for code in wanted:
        title, builder = SPEC_BY_CODE[code]
        with transaction.atomic():
            account = Account(code, title, email=SPECIAL_EMAILS.get(code, ""))
            if code == "A04" and ig_user_id:
                account.user.instagram_user_id = str(ig_user_id)
                account.user.save(update_fields=["instagram_user_id"])
            summary = builder(account)
            account.flush_cache()
        results.append(
            SeedResult(
                code=code,
                email=account.email,
                password=PASSWORD,
                title=title,
                summary=summary,
                notes=list(account.notes),
            )
        )
    return results
