"""DM 자동화 프론트 검증용 종합 더미 시더 (개발 전용 · 실 DM 미발송).

프론트가 최근 반영한 DM 자동화 UI(스토리 만료 타일, 발송 실패 문구, 순차 발송 큐
게이지/ETA, 사람 단위 지표, 대댓글 상한, 다중 링크 버튼, 참여자 추이, 실패 DM 복구,
게이트 3분기, 숨겨진 요청·스팸 카드 등)를 dev 에서 **실제 데이터로** 눈으로 확인하기
위한 읽기용 시드다. 요청서: Downloads/Telegram Desktop/dm-dev-dummy-request.md.

★ 핵심 안전장치 (seed_campaign_queue 와 동일 철학):
  - **Meta API 를 절대 호출하지 않는다** — send_dm_task 미경유. 순수 DB 상태만 채운다.
    (INSTAGRAM_MOCK_MODE 여부와 무관하게 실 발송 0건.)
  - QUEUED(대기) 더미의 next_retry_at 은 항상 beat(requeue_deferred_dms) look-ahead 창
    (now+35s) 밖(now+BEAT_SAFE_BUFFER~)에 둔다 → beat 가 더미를 실제 발송으로 못 집는다.
  - ⚠️ DEBUG=True 가 아니면 실행 거부 (운영 데이터 보호).

생성물 (전부 접두사 `dmdummy` 로 태깅 → --cleanup 으로 완전 제거):
  - 테스트 계정 2개: free 1(dmdummy-free@turnflow.dev), pro 1(dmdummy-pro@turnflow.dev)
    비밀번호는 아래 SEED_PASSWORD. 워크스페이스/멤버십/구독(free·pro) 자동 구성.
  - IG 연동(mock 토큰) 여러 개: 정상(pro main)·쿨다운 계정·free 계정·토큰만료·소프트비활성.
  - 캠페인 세트: 트리거 4종 + story_reply 만료 + 게이트 3분기 + 회전발송 + 대댓글 상한
    (도달 포함) + 다중 링크버튼(1/2/3/legacy) + 복구(pro on / free no-op) + 상태 4종 +
    예약 상태 다양.
  - SentDMLog: 성공/도착/읽음 + failed_window(2534022) + hidden_spam(2534025) +
    failed_param + failed_token + rate_limited + failed_no_trace + recovery 3종 — 머신
    코드(status/error_code/error_subcode) 채움(프론트가 frontend_action 을 자동 생성).
  - stats/queue-state/timeseries/recipients/summary.usage/health 가 계산으로 살아나도록
    바닥 데이터를 채운다(대부분의 응답 필드는 서버 계산값이라 직접 세팅 불가).

사용 (dev 컨테이너 안):
    python manage.py seed_dm_dev_dummy            # 생성(재실행 안전 — 더미만 재구성)
    python manage.py seed_dm_dev_dummy --cleanup  # 더미 전부 제거
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.integrations.models import (
    AutoDMCampaign,
    DMAccountBlock,
    IGAccountConnection,
    SentDMLog,
)
from apps.workspace.models import Membership, Workspace

TAG = "dmdummy"
SEED_PASSWORD = "Test1234!"  # noqa: S105 — dev 전용 테스트 계정
FREE_EMAIL = "dmdummy-free@turnflow.dev"
PRO_EMAIL = "dmdummy-pro@turnflow.dev"

# beat(requeue_deferred_dms) look-ahead(now+35s) 밖으로 대기 슬롯을 밀어내는 버퍼.
BEAT_SAFE_BUFFER_SECONDS = 120

User = get_user_model()


class Command(BaseCommand):
    help = "DM 자동화 프론트 검증용 종합 더미 시더 (dev 전용 · 실 DM 미발송)"

    def add_arguments(self, parser):
        parser.add_argument("--cleanup", action="store_true", help="더미 데이터 전부 제거")

    # ------------------------------------------------------------------ #
    def handle(self, *args, **opts):
        if not settings.DEBUG:
            raise CommandError("거부: DEBUG=True 환경에서만 실행 가능 (운영 데이터 보호).")

        if opts["cleanup"]:
            return self._cleanup()

        self._seq = 0  # 전역 유일 idempotency_key / recipient_user_id 카운터
        self.now = timezone.now()
        with transaction.atomic():
            self._purge_dynamic()  # 재실행 안전: 기존 더미 캠페인/로그 제거 후 재구성
            self._seed_accounts()
            self._seed_pro_campaigns()
            self._seed_cooldown_account()
            self._seed_free_campaigns()
            self._seed_aux_connections()
        self._clear_runtime_caches()
        self._report()

    # ================================================================== #
    # 계정 / 워크스페이스 / 구독
    # ================================================================== #
    def _seed_accounts(self):
        from apps.billing.models import SubscriptionPlan, SubscriptionStatus, UserSubscription

        # --- 유저 ---
        self.free_user = self._ensure_user(FREE_EMAIL, "더미 무료계정")
        self.pro_user = self._ensure_user(PRO_EMAIL, "더미 프로계정")

        # --- 워크스페이스 + 멤버십 ---
        self.free_ws = self._ensure_ws("dmdummy-free", "[DUMMY] 무료 워크스페이스", self.free_user, "starter")
        self.pro_ws = self._ensure_ws("dmdummy-pro", "[DUMMY] 프로 워크스페이스", self.pro_user, "pro")

        # --- 구독 (free 는 기본 free, pro 는 pro 플랜 강제) ---
        free_plan = SubscriptionPlan.objects.get(name="free")
        pro_plan = SubscriptionPlan.objects.get(name="pro")
        UserSubscription.objects.update_or_create(
            user=self.free_user,
            defaults={
                "plan": free_plan,
                "status": SubscriptionStatus.ACTIVE,
                "current_period_start": self.now,
            },
        )
        UserSubscription.objects.update_or_create(
            user=self.pro_user,
            defaults={
                "plan": pro_plan,
                "status": SubscriptionStatus.ACTIVE,
                "current_period_start": self.now,
                # 여러 mock 연동을 붙이므로 허용량 넉넉히 (active 연동 게이트는 DB seed 엔 무영향이나 UI 정합)
                "extra_ig_accounts": 5,
                "monthly_amount_snapshot": pro_plan.monthly_price,
            },
        )

        # --- IG 연동 (mock) ---
        self.conn_pro = self._ensure_conn(
            self.pro_ws, "dmdummy_pro_main", "dmdummy_pro", name="더미 프로 계정"
        )
        self.conn_free = self._ensure_conn(
            self.free_ws, "dmdummy_free_main", "dmdummy_free", name="더미 무료 계정"
        )
        # next_media 폴링 헬스가 ok 로 보이도록 최근 폴링 흔적 남김
        self.conn_pro.last_polled_at = self.now - timedelta(minutes=3)
        self.conn_pro.last_seen_media_id = "dmdummy_media_lastseen"
        self.conn_pro.last_seen_media_at = self.now - timedelta(hours=6)
        self.conn_pro.save(
            update_fields=["last_polled_at", "last_seen_media_id", "last_seen_media_at"]
        )

    def _ensure_user(self, email, full_name):
        user = User.objects.filter(email=email).first()
        if user is None:
            user = User.objects.create_user(email=email, password=SEED_PASSWORD)
        user.full_name = full_name
        user.is_email_verified = True
        user.email_verified_at = self.now
        user.is_active = True
        user.set_password(SEED_PASSWORD)
        user.save()
        return user

    def _ensure_ws(self, slug, name, owner, plan):
        ws, _ = Workspace.objects.get_or_create(
            slug=slug, defaults={"name": name, "owner": owner, "plan": plan}
        )
        if ws.owner_id != owner.id or ws.plan != plan:
            ws.owner = owner
            ws.plan = plan
            ws.save(update_fields=["owner", "plan"])
        Membership.objects.get_or_create(
            user=owner, workspace=ws, defaults={"role": Membership.Role.OWNER}
        )
        return ws

    def _ensure_conn(self, ws, ext_id, username, *, name="", status=None, is_active=True,
                     token_days=60):
        status = status or IGAccountConnection.Status.ACTIVE
        conn, _ = IGAccountConnection.objects.get_or_create(
            workspace=ws,
            external_account_id=ext_id,
            defaults={"username": username},
        )
        conn.username = username
        conn.name = name or username
        conn.account_type = "BUSINESS"
        conn.status = status
        conn.is_active = is_active
        conn.scopes = ["instagram_business_basic", "instagram_business_manage_messages"]
        conn.access_token = f"mock-token-{ext_id}"  # EncryptedTextField descriptor 가 암호화
        if token_days is None:
            conn.token_expires_at = self.now - timedelta(days=1)  # 만료
        else:
            conn.token_expires_at = self.now + timedelta(days=token_days)
        conn.last_verified_at = self.now
        conn.save()
        return conn

    # ================================================================== #
    # 프로 계정 캠페인 세트
    # ================================================================== #
    def _seed_pro_campaigns(self):
        conn = self.conn_pro

        # ── C1 종합 (specific_media): §3 로그 전종류 · §7 5그룹 · §5 timeseries ·
        #    §2 하드실패+숨은스팸 · §4 발송중(+ETA/스피너) · §1-H overlap(specific active)
        c1 = self._campaign(
            conn, "[DUMMY] C1 종합(발송/실패/복구/추이)", trigger="specific_media",
            media_id="dmdummy_media_C1",
            opening_message_templates=[
                "안녕하세요! 신청 감사합니다 🙌 자료 보내드려요.",
                "댓글 감사해요 😊 아래에서 바로 확인하세요!",
            ],
        )
        self._fill_c1(c1)

        # ── C2 정상완료 (any_media): §2 healthy · §4 완료 · §1-A any_media · §1-H overlap
        c2 = self._campaign(
            conn, "[DUMMY] C2 정상·완료(any_media)", trigger="any_media", status="active",
        )
        # 전부 도착/읽음 → healthy, 대기 0 → 완료
        self._emit(c2, "read", 14, spread_days=6)
        self._emit(c2, "delivered", 10, spread_days=6)

        # ── C4 발송중 (specific): §4 발송중 + ETA + 스피너 + ahead>0(C1 보다 최신 대기)
        c4 = self._campaign(
            conn, "[DUMMY] C4 발송 중(큐 진행)", trigger="specific_media",
            media_id="dmdummy_media_C4",
        )
        self._emit(c4, "accepted", 18, spread_days=1)
        self._emit(c4, "delivered", 6, spread_days=1)
        self._emit(c4, "submitting", 2)  # in_flight → 헤더 스피너
        self._emit(c4, "queued", 26, waiting_slots=True)  # 미래 슬롯 → ETA "약 N분 후"

        # ── C5 대기/멈춤 (paused): gauge.waiting/in_flight=0 인데 people 남음(rate_limited)
        c5 = self._campaign(
            conn, "[DUMMY] C5 발송 대기(일시정지)", trigger="specific_media",
            media_id="dmdummy_media_C5", status="paused",
        )
        self._emit(c5, "accepted", 8, spread_days=2)
        self._emit(c5, "rate_limited", 12)  # people.waiting>0, gauge.waiting=0/in_flight=0

        # ── C3 next_media (폴링/헬스 §8, §1-A next_media) — 미부착(media_id="")
        self._campaign(
            conn, "[DUMMY] C3 다음 게시물(next_media)", trigger="next_media", status="active",
        )

        # ── 게이트 3분기 (§1-B) ──
        self._campaign(
            conn, "[DUMMY] G1 단순 DM(게이트 없음)", trigger="specific_media",
            media_id="dmdummy_media_G1", follow_gate_enabled=False,
        )  # 로그 0 → §2 case5(0건)도 겸함
        self._campaign(
            conn, "[DUMMY] G2 버튼 클릭 시 링크(button-only)", trigger="specific_media",
            media_id="dmdummy_media_G2", follow_gate_enabled=True, gate_verify_follow=False,
            follow_gate_prompt="자료가 필요하면 아래 버튼을 눌러주세요!",
            follow_gate_button_label="자료 받기",
            reward_message_template="여기 자료 링크예요 👉",
            link_buttons=[{"url": "https://turnflow.link/guide", "label": "가이드 보기"}],
        )
        g3 = self._campaign(
            conn, "[DUMMY] G3 팔로우 확인 후 보상(follow-gate)", trigger="specific_media",
            media_id="dmdummy_media_G3", follow_gate_enabled=True, gate_verify_follow=True,
            follow_gate_prompt="팔로우도 해주셨나요? 버튼을 눌러주세요!",
            follow_gate_prompt_templates=[  # §1-C 게이트 안내 회전
                "댓글 감사해요! 팔로우 확인 후 버튼을 눌러주세요 🙏",
                "선물 드릴게요 🎁 팔로우하시고 버튼 눌러주세요!",
            ],
            follow_gate_button_label="팔로우했어요",
            reward_message_template="팔로우 감사합니다! 약속한 자료 보내드려요 🎉",
            recovery_reply_enabled=True,  # 프로 → recovery_reply_available=true
            recovery_reply_templates=[
                "DM이 숨겨진 요청함으로 갔어요 🥲 수락 후 다시 댓글 남겨주시면 바로 보내드릴게요!",
            ],
            recovery_ttl_seconds=604800,
        )
        self._fill_gate(g3)  # opening + reward child (verified_follower, ctr_basis=click)

        # ── 대댓글(공개답글) 상한 (§1-D) ──
        self._campaign(
            conn, "[DUMMY] PR1 대댓글 상한 미도달", trigger="specific_media",
            media_id="dmdummy_media_PR1", public_reply_enabled=True, public_reply_limit=200,
            public_reply_posted_count=40,
            public_reply_templates=["DM 보내드렸어요! 확인해주세요 :)", "안내 드렸습니다 🎁"],
        )
        self._campaign(
            conn, "[DUMMY] PR2 대댓글 상한 도달", trigger="specific_media",
            media_id="dmdummy_media_PR2", public_reply_enabled=True, public_reply_limit=100,
            public_reply_posted_count=100,  # → public_reply_limit_reached=true
            public_reply_templates=["DM 보내드렸어요!", "확인 부탁드려요 :)"],
        )

        # ── 다중 링크 버튼 (§1-E) ──
        self._campaign(
            conn, "[DUMMY] LB1 링크버튼 1개", trigger="specific_media",
            media_id="dmdummy_media_LB1",
            link_buttons=[{"url": "https://turnflow.link/a", "label": "구매하기"}],
        )
        self._campaign(
            conn, "[DUMMY] LB2 링크버튼 2개", trigger="specific_media",
            media_id="dmdummy_media_LB2",
            link_buttons=[
                {"url": "https://turnflow.link/a", "label": "구매하기"},
                {"url": "https://turnflow.link/b", "label": "후기 보기"},
            ],
        )
        self._campaign(
            conn, "[DUMMY] LB3 링크버튼 3개(Meta 한도)", trigger="specific_media",
            media_id="dmdummy_media_LB3",
            link_buttons=[
                {"url": "https://turnflow.link/a", "label": "구매하기"},
                {"url": "https://turnflow.link/b", "label": "후기 보기"},
                {"url": "https://turnflow.link/c", "label": "이벤트 참여"},
            ],
        )
        self._campaign(
            conn, "[DUMMY] LB4 레거시 단일 버튼(link_buttons 빈)", trigger="specific_media",
            media_id="dmdummy_media_LB4",
            link_button_url="https://turnflow.link/legacy", link_button_label="자세히 보기",
        )

        # ── 스토리 답장 만료 (§1-A, 이번 신규 UI 핵심) ──
        # media_id 를 stories 엔드포인트가 반환하지 않는 값으로 + created_at 이틀 전 → 만료 추정
        s1 = self._campaign(
            conn, "[DUMMY] S1 스토리 답장(만료)", trigger="story_reply",
            media_id="dmdummy_story_expired_0001",
            opening_message="스토리 답장 감사해요! 자료 보내드릴게요 :)",
        )
        AutoDMCampaign.objects.filter(pk=s1.pk).update(
            created_at=self.now - timedelta(days=2)  # 목록 카드가 24h 이전이면 만료로 추정
        )

        # ── 상태 4종 (§1-G) : active(위 다수) / paused(C5) / completed / inactive ──
        self._campaign(
            conn, "[DUMMY] ST 완료 상태", trigger="specific_media",
            media_id="dmdummy_media_STc", status="completed",
        )
        self._campaign(
            conn, "[DUMMY] ST 비활성 상태", trigger="specific_media",
            media_id="dmdummy_media_STi", status="inactive",
        )

        # ── 예약 상태 다양 (§1-G) : scheduled / running / ended ──
        self._campaign(
            conn, "[DUMMY] SCH 예약 대기(scheduled)", trigger="specific_media",
            media_id="dmdummy_media_SCHs", status="active",
            scheduled_start_at=self.now + timedelta(days=2),  # 미래 시작 → scheduled
        )
        self._campaign(
            conn, "[DUMMY] SCH 진행 중(running)", trigger="specific_media",
            media_id="dmdummy_media_SCHr", status="active",
            scheduled_start_at=self.now - timedelta(days=1),
            scheduled_end_at=self.now + timedelta(days=3),  # 창 안 → running
        )
        self._campaign(
            conn, "[DUMMY] SCH 종료됨(ended)", trigger="specific_media",
            media_id="dmdummy_media_SCHe", status="completed",
            scheduled_start_at=self.now - timedelta(days=5),
            scheduled_end_at=self.now - timedelta(days=1),  # 과거 종료 → ended
        )

    def _fill_c1(self, c1):
        """C1 에 §3 전 상태 · §7 5그룹(페이지네이션) · §5 timeseries · 미확인 등 주입."""
        # 전송됨(sent 그룹) — 22명(페이지네이션 20+). accepted/delivered 혼합.
        self._emit(c1, "accepted", 8, spread_days=9)
        self._emit(c1, "delivered", 14, spread_days=9)
        # 읽음(read 그룹) — 9명
        self._emit(c1, "read", 9, spread_days=8)
        # 대기중(waiting 그룹) — 10명 (C1 자체 발송중; created_at 은 오래 전 → C4 ahead 데모)
        self._emit(c1, "queued", 10, waiting_slots=True, waiting_backdate_days=3)
        # 도착 미확인(unconfirmed) — no_trace 3명 (people 상 sent 버킷·stats unconfirmed)
        self._emit(c1, "failed_no_trace", 3, spread_days=5)
        # 숨겨진 요청·스팸(hidden_spam 그룹) — 복구 대기 3 + 만료 2 + 복구 OFF 1(2534025)
        self._emit(c1, "recovery_pending", 3, spread_days=2)
        self._emit(c1, "recovery_expired", 2, spread_days=6)
        self._emit(c1, "recovery_delivered", 2, spread_days=4)  # 복구 성공(sent 버킷)
        self._emit(c1, "failed_param", 1, error_code="100", error_subcode="2534025",
                   spread_days=3)  # 복구 OFF 숨김함
        # 확인 필요(attention 그룹) — 토큰/윈도우/파라미터/건너뜀
        self._emit(c1, "failed_token", 2, spread_days=4)
        self._emit(c1, "failed_window", 2, error_code="100", error_subcode="2534022",
                   spread_days=5)  # ★ 프론트 문구 덮어쓰기 케이스(7일 창/댓글 삭제)
        self._emit(c1, "failed_param", 2, error_code="100", error_subcode="2534014",
                   spread_days=4)  # 일반 파라미터 오류(비-2534025)
        self._emit(c1, "skipped", 1, spread_days=7)

    def _fill_gate(self, g3):
        """게이트 통과 흐름 — opening(PASSED) + reward child. verified_follower/ctr=click."""
        openings = self._emit(
            g3, "delivered", 6, dm_kind="opening", gate_status="passed", spread_days=3,
        )
        # reward child (parent_log=opening, Send API 버킷)
        for parent in openings:
            self._emit(
                g3, "delivered", 1, dm_kind="reward", parent=parent,
                comment=False, spread_days=3,
            )
        # 아직 미통과(pending) opening 2 — not_followed
        self._emit(g3, "accepted", 2, dm_kind="opening", gate_status="pending", spread_days=2)

    # ================================================================== #
    # 쿨다운 계정 (§4 action_block_cooldown)
    # ================================================================== #
    def _seed_cooldown_account(self):
        conn = self._ensure_conn(
            self.pro_ws, "dmdummy_pro_cool", "dmdummy_cooldown", name="더미 쿨다운 계정"
        )
        self.conn_cool = conn
        cd = self._campaign(
            conn, "[DUMMY] CD Action Block 쿨다운", trigger="specific_media",
            media_id="dmdummy_media_CD",
        )
        self._emit(cd, "accepted", 6, spread_days=1)
        self._emit(cd, "queued", 10, waiting_slots=True)  # 대기 있어야 배너 의미
        # DMAccountBlock 쿨다운 활성 → queue-state.blocking_reason=action_block_cooldown
        until = self.now + timedelta(hours=2)
        DMAccountBlock.objects.update_or_create(
            external_account_id="dmdummy_pro_cool",
            defaults={"cooldown_until": until, "level": 1, "last_tripped_at": self.now},
        )

    # ================================================================== #
    # 무료 계정 캠페인 (§6 사용량 · §4 월 한도 · §1-F recovery no-op)
    # ================================================================== #
    def _seed_free_campaigns(self):
        conn = self.conn_free
        # F1 — free 월 한도(200) 초과 + 대기 → §6 over-limit, §4 monthly_quota_reached
        f1 = self._campaign(
            conn, "[DUMMY] F1 무료 한도 초과", trigger="specific_media",
            media_id="dmdummy_media_F1",
        )
        self._emit(f1, "accepted", 210, spread_days=10)  # (캠페인×수신자) 고유 210 > 200
        self._emit(f1, "queued", 12, waiting_slots=True)  # 대기>0 → 월한도 배너/차단

        # F2 — recovery 켜도 free 라 recovery_reply_available=false (no-op 안내)
        self._campaign(
            conn, "[DUMMY] F2 복구 켰지만 무료(no-op)", trigger="specific_media",
            media_id="dmdummy_media_F2", recovery_reply_enabled=True,
            recovery_reply_templates=["요청함 수락 후 다시 댓글 남겨주세요!"],
        )

    # ================================================================== #
    # 부가 연동 (§0 토큰 만료 / 소프트 비활성)
    # ================================================================== #
    def _seed_aux_connections(self):
        # 토큰 만료 연동 (설정 헬스 배너 "재연결 필요" / health connection_status)
        self._ensure_conn(
            self.pro_ws, "dmdummy_pro_expired", "dmdummy_expired", name="더미 토큰만료 계정",
            status=IGAccountConnection.Status.EXPIRED, token_days=None,
        )
        # 소프트 비활성 연동 (설정 IG 카드 "사용 안 함" pill)
        self._ensure_conn(
            self.pro_ws, "dmdummy_pro_inactive", "dmdummy_inactive", name="더미 비활성 계정",
            is_active=False,
        )

    # ================================================================== #
    # 캠페인 / 로그 생성 헬퍼
    # ================================================================== #
    def _campaign(self, conn, name, *, trigger="specific_media", status="active", media_id="",
                  opening_message="", link_buttons=None, **fields) -> AutoDMCampaign:
        trig = getattr(AutoDMCampaign.TriggerType, trigger.upper())
        st = getattr(AutoDMCampaign.Status, status.upper())
        opening = opening_message or fields.pop("opening_message_template", "") or (
            "안녕하세요! 신청 감사합니다. 자료 보내드릴게요 :)"
        )
        camp = AutoDMCampaign.objects.create(
            ig_connection=conn,
            name=name,
            trigger_type=trig,
            media_id=media_id,
            status=st,
            opening_message_template=opening,
            message_template=opening,
            link_buttons=link_buttons or [],
            **fields,
        )
        return camp

    def _emit(self, campaign, status, count, *, dm_kind="opening", gate_status="none",
              error_code="", error_subcode="", parent=None, comment=True,
              waiting_slots=False, waiting_backdate_days=0, spread_days=0):
        """count 개의 SentDMLog 를 만든다 (distinct 수신자·idempotency). 실 발송 없음.

        bulk_create 는 auto_now_add 로 created_at 을 now 로 강제하므로, 원하는
        created_at/단계 타임스탬프는 생성 후 bulk_update 로 덮어쓴다.
        """
        st = getattr(SentDMLog.Status, status.upper())
        dk = getattr(SentDMLog.DMKind, dm_kind.upper())
        gs = getattr(SentDMLog.GateStatus, gate_status.upper())
        created_ats = self._spread_times(count, spread_days) if spread_days else None

        objs = []
        metas = []
        for i in range(count):
            self._seq += 1
            s = self._seq
            rid = f"{TAG}_{s}"
            cid = f"{TAG}-c-{s}" if comment else ""
            created_at = created_ats[i] if created_ats else (self.now - timedelta(minutes=5))
            objs.append(
                SentDMLog(
                    campaign=campaign,
                    comment_id=cid,
                    comment_text="[더미] 신청합니다" if comment else "",
                    recipient_user_id=rid,
                    recipient_username=f"dummy_user_{s:04d}",
                    message_sent=campaign.opening_message_template or "[더미] 안녕하세요!",
                    idempotency_key=f"{TAG}:{campaign.id}:{s}",
                    status=st,
                    dm_kind=dk,
                    gate_status=gs,
                    error_code=error_code,
                    error_subcode=error_subcode,
                    error_message=self._err_msg(status),
                    parent_log=parent,
                )
            )
            metas.append((created_at,))
        SentDMLog.objects.bulk_create(objs, batch_size=500)

        # 단계별 타임스탬프 채우기 (상태에 맞게) + created_at 덮어쓰기
        for idx, (obj, (created_at,)) in enumerate(zip(objs, metas)):
            obj.created_at = created_at
            self._stamp(obj, status, created_at, waiting_slots, waiting_backdate_days, idx)
        SentDMLog.objects.bulk_update(
            objs,
            [
                "created_at", "submitted_at", "accepted_at", "delivered_at", "read_at",
                "sent_at", "next_retry_at", "verified_via", "meta_message_id",
                "recovery_pending_at", "recovery_reply_id", "public_reply_posted_at",
                "public_reply_id",
            ],
            batch_size=500,
        )
        return objs

    def _stamp(self, obj, status, created_at, waiting_slots, waiting_backdate_days, idx=0):
        now = self.now
        if status in ("accepted", "delivered", "read", "failed_no_trace", "recovery_delivered"):
            obj.submitted_at = created_at
            obj.accepted_at = created_at + timedelta(seconds=3)
            obj.sent_at = obj.accepted_at
            obj.meta_message_id = f"mock-mid-{obj.recipient_user_id}"
        if status in ("delivered", "read", "recovery_delivered"):
            obj.delivered_at = created_at + timedelta(seconds=20)
            obj.verified_via = SentDMLog.VerifiedVia.ECHO
        if status == "read":
            obj.read_at = created_at + timedelta(minutes=4)
        if status == "submitting":
            obj.submitted_at = now - timedelta(seconds=8)
        if status == "queued" and waiting_slots:
            # beat look-ahead(now+35s) 밖으로 슬롯 배치 → 실제 발송으로 안 잡힘 + ETA 미래
            obj.next_retry_at = now + timedelta(seconds=BEAT_SAFE_BUFFER_SECONDS + idx * 5)
            if waiting_backdate_days:
                obj.created_at = now - timedelta(days=waiting_backdate_days)
        if status == "recovery_pending":
            obj.recovery_pending_at = created_at
            obj.recovery_reply_id = f"mock-recovery-cid-{obj.recipient_user_id}"
        if status == "recovery_expired":
            obj.recovery_pending_at = created_at

    @staticmethod
    def _err_msg(status):
        return {
            "failed_token": "Error validating access token (mock)",
            "failed_window": "(#10) This message is sent outside of allowed window (mock)",
            "failed_param": "(#100) Invalid parameter (mock)",
            "failed_no_trace": "accepted but no delivery trace within 35m (mock)",
            "rate_limited": "rate limited, will retry (mock)",
        }.get(status, "")

    def _spread_times(self, n, span_days):
        """최근 24h·7d·전 기간 버킷이 골고루 채워지도록 n개 시각을 흩뿌린다(차트 변동)."""
        now = self.now
        out = []
        for i in range(n):
            if i % 3 == 0:
                # 최근 24h 안 (여러 시간대)
                out.append(now - timedelta(hours=(i * 2) % 22 + 1))
            elif i % 3 == 1:
                # 최근 7d 안
                out.append(now - timedelta(days=(i % 6) + 1, hours=(i * 3) % 12))
            else:
                # 전 기간 (span_days 까지)
                d = min(span_days, 3 + (i % max(span_days, 1)))
                out.append(now - timedelta(days=d, hours=(i * 5) % 20))
        return out

    # ================================================================== #
    # 정리 / 재구성 / 캐시
    # ================================================================== #
    def _dummy_connections(self):
        return IGAccountConnection.objects.filter(external_account_id__startswith=TAG)

    def _purge_dynamic(self):
        """재실행 안전: 더미 연동의 캠페인(→로그 CASCADE) 제거 + 쿨다운/캐시 리셋."""
        conns = list(self._dummy_connections())
        if conns:
            AutoDMCampaign.objects.filter(ig_connection__in=conns).delete()
        DMAccountBlock.objects.filter(external_account_id__startswith=TAG).delete()

    def _clear_runtime_caches(self):
        # free owner 월 한도 hit 플래그 재계산 유도 + 페이서 포인터 잔재 제거
        try:
            cache.delete(f"dmquota:hit:{self.free_user.id}:{timezone.localtime(self.now):%Y%m}")
        except Exception:  # noqa: BLE001
            pass

    def _cleanup(self):
        conns = list(self._dummy_connections())
        n_camp = AutoDMCampaign.objects.filter(ig_connection__in=conns).count() if conns else 0
        AutoDMCampaign.objects.filter(ig_connection__in=conns).delete()
        DMAccountBlock.objects.filter(external_account_id__startswith=TAG).delete()
        IGAccountConnection.objects.filter(external_account_id__startswith=TAG).delete()

        from apps.billing.models import UserSubscription

        for email in (FREE_EMAIL, PRO_EMAIL):
            user = User.objects.filter(email=email).first()
            if not user:
                continue
            Workspace.objects.filter(owner=user).delete()  # CASCADE: membership 등
            UserSubscription.objects.filter(user=user).delete()
            user.delete()
        self.stdout.write(self.style.SUCCESS(
            f"cleanup OK: 더미 캠페인 {n_camp}개 + 연동/워크스페이스/계정 제거 완료"
        ))

    # ================================================================== #
    # 리포트
    # ================================================================== #
    def _report(self):
        base = "/api/v1/integrations"
        s = self.style
        w = self.stdout.write
        camps = AutoDMCampaign.objects.filter(
            ig_connection__external_account_id__startswith=TAG
        ).count()
        logs = SentDMLog.objects.filter(
            campaign__ig_connection__external_account_id__startswith=TAG
        ).count()

        w(s.SUCCESS("\n=== DM 더미 시드 완료 (실 DM 0건) ==="))
        w(f"캠페인 {camps}개 · SentDMLog {logs}건 생성\n")

        w(s.MIGRATE_HEADING("로그인 (dev):"))
        w(f"  POST {base.replace('/integrations','')}/auth/login/  (끝슬래시 필수)")
        w(f"  · 무료: {FREE_EMAIL} / {SEED_PASSWORD}")
        w(f"  · 프로: {PRO_EMAIL} / {SEED_PASSWORD}\n")

        w(s.MIGRATE_HEADING("주요 IG 연동(ig_connection_id):"))
        for label, conn in [
            ("프로 메인", self.conn_pro),
            ("쿨다운", self.conn_cool),
            ("무료", self.conn_free),
        ]:
            w(f"  · {label:6s} {conn.id}  (ws={conn.workspace_id})")
        w("")

        w(s.MIGRATE_HEADING("핵심 캠페인(campaign_id) → 검증 포인트:"))
        for camp in AutoDMCampaign.objects.filter(
            ig_connection__external_account_id__startswith=TAG
        ).order_by("ig_connection__external_account_id", "name"):
            w(f"  · {camp.id}  {camp.name}")
        w("")

        w(s.MIGRATE_HEADING("빠른 확인 예 (프로 로그인 토큰으로):"))
        w(f"  GET {base}/auto-dm-campaigns/                         (목록·임베드 통계)")
        w(f"  GET {base}/auto-dm-campaigns/summary/?ig_connection_id={self.conn_free.id}  (무료 한도 초과)")
        w(f"  GET {base}/dm-verification/queue-state/?campaign_id=<C4>   (발송중+ETA)")
        w(f"  GET {base}/dm-verification/queue-state/?campaign_id=<CD>   (action_block_cooldown)")
        w(f"  GET {base}/dm-verification/queue-state/?campaign_id=<F1>   (monthly_quota_reached)")
        w(f"  GET {base}/dm-verification/recipients/?campaign_id=<C1>&status_group=sent")
        w(f"  GET {base}/dm-verification/stats/?campaign_id=<C1>")
        w(f"  GET {base}/auto-dm-campaigns/<C1>/timeseries/?range=7d")
        w(s.WARNING("\n정리:  python manage.py seed_dm_dev_dummy --cleanup\n"))
