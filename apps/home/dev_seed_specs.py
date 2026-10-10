"""홈 테스트 계정 **상태별 빌더** — 요청서 H/P/I/A 코드 1:1.

:mod:`apps.home.dev_seed` 가 작업대(Account)를, 이 파일이 "그 계정은 어떤 상태인가"를 맡는다.
빌더는 ``Account`` 를 받아 상태를 만들고 한 줄 요약을 돌려준다.

용어
----
``quiet_base`` 를 먼저 깔고 확인하려는 상태 **하나만** 더 얹는 것이 기본이다.
그러지 않으면 "새 게시물에 캠페인이 없어요"·"링크 페이지를 만들어 보세요" 같은 권유가
항상 따라붙어, 확인하려는 카드가 큰 카드 자리를 뺏긴다.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .dev_seed import REFERRAL_BONUS_DAYS, REFERRAL_CODE, Account

# ═══════════════════════════════════════════════════════════════════════════
# 1. 홈 알림 (H)
# ═══════════════════════════════════════════════════════════════════════════


def h01(a: Account) -> str:
    """대표 화면 — 새 게시물 큰 카드 + 리포트 배너 + 알림 센터 + 자원 줄."""
    from apps.insta_reports.models import ReportStatus

    a.subscription(
        "pro",
        status="active",
        current_period_end=a.now + timedelta(days=21),
        monthly_amount_snapshot=14900,
        trial_used_at=a.now - timedelta(days=60),
        card=("신한", "45184445****364*"),
    )
    conn = a.connection("sample_creator")
    latest = a.latest_post(conn, hours_ago=2)  # 목 풀 0번 — 캠페인을 붙이지 않는다
    # 오류 캠페인은 반드시 campaign_post_restricted(60) 를 동반해 큰 카드를 뺏는다 → 끈다.
    a.common_campaigns(conn, error_campaign=False)  # 7개, 1~7번 게시물에 (0번은 비워 둔다)
    a.common_pages()
    a.report(conn, ReportStatus.SUCCEEDED, progress=100, stage="done")
    a.migration_candidates(conn, count=3)
    return (
        f"recent_post_no_campaign(200, 2시간 전 · {latest['id']}) + report_ready(230) + "
        "migration_review_pending(240, 3건) + create_campaign(999 기본) / 자동 DM 7 · 링크 2"
    )


def h02(a: Account) -> str:
    """멈춤 4종 — 빨간 칩 「멈춤 4」."""
    from apps.integrations.models import IGAccountConnection

    # basic + 추가 1 → 허용량 2. 연결 2개가 모두 켜져 있어도 강제 팝업이 뜨지 않는다.
    # 결제 유예는 current_period_end + GRACE_PERIOD_DAYS(7) 이므로 cpe=now → 유예 7일 뒤.
    # ⚠️ next_billing_retry_at 은 **미래**여야 한다. 과거면 process_due_renewals(10분)가
    #    실제로 과금을 재시도해 상태가 바뀐다.
    a.subscription(
        "basic",
        status="past_due",
        current_period_end=a.now,
        next_billing_retry_at=a.now + timedelta(days=2),
        monthly_amount_snapshot=4900,
        extra_ig_accounts=1,
        renewal_attempts=1,
        last_billing_error="카드 한도 초과",
        card=("신한", "45184445****364*"),
    )
    live = a.connection("sample_creator")
    pool = a.media_pool(live)
    a.latest_post(live, media=pool[0])
    a.campaign(live, "가을 댓글 이벤트", pool[0])  # 최신 게시물 권유를 끈다
    restricted = a.campaign(live, "연령제한 게시물 캠페인", pool[1], kind="restricted")
    a.action_block(live, hours=3)
    a.queued_dms(restricted, count=12)
    a.connection(
        "sample_studio",
        photo=False,
        status=IGAccountConnection.Status.EXPIRED,
        reconnect_reason="token_invalidated",
        token_dead_strikes=3,
        token_dead_first_seen_at=a.now - timedelta(days=1),
    )
    a.page("sample-creator", "샘플 크리에이터", public=True, blocks=3)
    return (
        "payment_failed(10, 유예 7일 뒤) + ig_disconnected(20, @sample_studio) + "
        "campaign_post_restricted(60, 연령제한 게시물 캠페인) + dm_send_blocked(70, 3시간 뒤 재개 · 대기 12)"
    )


def _disconnected(a: Account, reason: str, status: str) -> str:
    a.subscription("free")
    conn = a.connection(
        "sample_creator",
        status=status,
        reconnect_reason=reason,
        token_dead_strikes=3,
        token_dead_first_seen_at=a.now - timedelta(hours=20),
    )
    pool = a.media_pool(conn)
    a.campaign(conn, "가을 댓글 이벤트", pool[0])
    a.page("sample-creator", "샘플 크리에이터", public=True, blocks=3)
    return f"ig_disconnected(20) · reason={reason} · status={status}"


def h03a(a):
    from apps.integrations.models import IGAccountConnection

    return _disconnected(a, "token_invalidated", IGAccountConnection.Status.ERROR)


def h03b(a):
    from apps.integrations.models import IGAccountConnection

    return _disconnected(a, "account_checkpoint", IGAccountConnection.Status.ERROR)


def h03c(a):
    from apps.integrations.models import IGAccountConnection

    return _disconnected(a, "app_removed", IGAccountConnection.Status.ERROR)


def h03d(a):
    from apps.integrations.models import IGAccountConnection

    return _disconnected(a, "reconnect_required", IGAccountConnection.Status.EXPIRED)


def h04(a: Account) -> str:
    """댓글 받기가 끊김 — 복구 버튼이 `target_id`(연결 id)를 쓴다."""
    a.subscription("free")
    conn = a.quiet_base()
    conn.webhook_healthy = False
    conn.webhook_checked_at = a.now - timedelta(minutes=25)
    conn.save(update_fields=["webhook_healthy", "webhook_checked_at"])
    a.flush_cache()
    return f"comment_stream_down(40) · target_id={conn.id}"


def h05(a: Account) -> str:
    """월 한도 소진 — 프로는 무제한이라 재현 불가 → free(200건) 계정이다."""
    a.subscription("free")
    a.quiet_base()
    camp = a.base_campaign
    used, limit = a.fill_quota(camp, count=200)
    a.quota_skips(camp, blocked=37, resumable=31)
    a.flush_cache()
    return (
        f"dm_quota_exhausted(50) · {used}/{limit} · 대기 37 · 바로 재발송 31 · 다음 달 1일 초기화"
    )


def h06(a: Account) -> str:
    """게시물이 막혀 멈춘 캠페인 2개 — 캠페인마다 한 줄.

    「오류」 상태 캠페인 카드도 여기서 본다 — 오류 캠페인은 이 알림을 반드시 동반하므로
    큰 카드를 확인해야 하는 계정(H01·H18)에는 넣을 수 없다.
    """
    a.subscription("free")
    conn = a.connection("sample_creator")
    pool = a.media_pool(conn)
    a.latest_post(conn, media=pool[0])
    a.campaign(conn, "가을 댓글 이벤트", pool[0], age_minutes=5)  # 최신 게시물 권유를 끈다
    a.common_campaigns(conn)  # 7개 — 「가을 신상 미리 알림」이 오류(restricted)
    a.campaign(conn, "팝업 스토어 예약(게시물 차단)", pool[8], kind="restricted", age_minutes=300)
    a.page("sample-creator", "샘플 크리에이터", public=True, blocks=3)
    return (
        "campaign_post_restricted(60) × 2 (가을 신상 미리 알림 · 팝업 스토어 예약(게시물 차단)) "
        "· 자동 DM 9 (오류·중지됨·작성 중·종료됨 카드 전부 포함)"
    )


def h07(a: Account) -> str:
    """인스타그램이 발송을 잠시 막음 — 기다리는 것 말고 할 일이 없어 버튼이 없다."""
    a.subscription("free")
    conn = a.quiet_base()
    a.action_block(conn, hours=3)
    a.queued_dms(a.base_campaign, count=12)
    a.flush_cache()
    return "dm_send_blocked(70) · 3시간 뒤 자동 재개 · 대기 12건"


def h08(a: Account) -> str:
    """꺼져 있는 계정이 있는데 슬롯이 남음 → 「계정 켜기」."""
    a.subscription(
        "pro",
        status="active",
        current_period_end=a.now + timedelta(days=19),
        monthly_amount_snapshot=14900,
        extra_ig_accounts=1,
        card=("현대", "433012******123*"),
    )
    a.quiet_base()
    a.connection("sample_studio", photo=False, is_active=False)
    a.flush_cache()
    return "ig_account_disabled(80) · 꺼진 계정 1개(@sample_studio) · 허용량 2 / 켜짐 1"


def h09(a: Account) -> str:
    """월 한도 80% 경고 — free 200건의 85%."""
    a.subscription("free")
    a.quiet_base()
    used, limit = a.fill_quota(a.base_campaign, count=170)
    a.flush_cache()
    return f"dm_quota_warning(100) · {used}/{limit} (85%) · 남은 30건 · 다음 달 1일 초기화"


def h10(a: Account) -> str:
    """해지 예약 — 3일 뒤 종료 (P08 과 같은 상태)."""
    a.subscription(
        "pro",
        status="cancelled",
        current_period_end=a.now + timedelta(days=3, hours=6),  # days_left 가 2 로 깎이지 않게
        monthly_amount_snapshot=14900,
        cancelled_at=a.now - timedelta(days=1),
        trial_used_at=a.now - timedelta(days=90),
        # 해지 예약은 기간 끝까지 카드가 남아 있다. CANCELLED 는 process_due_renewals 가
        # 아예 제외하므로 카드가 있어도 과금되지 않는다.
        card=("현대", "433012******123*"),
    )
    a.quiet_base()
    return "subscription_ending(110) · 3일 뒤 종료 · plan=pro"


def h11(a: Account) -> str:
    """처음 가입한 사용자 홈 — 자동 DM 0, 링크 페이지 0.

    IG 는 연결돼 있지만 ``latest_media_*`` 는 비워 둔다(주기잡이 아직 안 돈 상태) —
    채우면 ``recent_post_no_campaign`` 이 먼저 떠서 "처음 가입" 화면이 안 보인다.
    """
    a.subscription("free")
    a.connection("sample_creator")
    return "create_campaign(210, is_default=false) + link_page_empty(220, no_page)"


def h12(a: Account) -> str:
    """정상 작동 중 — 「자동 DM이 정상적으로 작동하고 있어요」."""
    a.subscription("free")
    a.quiet_base(campaigns=1)
    return "create_campaign(999, is_default=true) 하나뿐 · 자동 DM 1(작동 중) · 공개 링크 페이지 1"


def h13(a: Account) -> str:
    """작성 중 자동 DM 1개 → 「이어서 설정하기」."""
    a.subscription("free")
    conn = a.quiet_base(campaigns=1)
    pool = a.media_pool(conn)
    a.campaign(conn, "말차 신메뉴 안내", pool[2], kind="inactive", age_minutes=15)
    return "H12 + 작성 중(status=inactive) 자동 DM 「말차 신메뉴 안내」 1개"


def h14(a: Account) -> str:
    """블록 없는 공개 페이지."""
    a.subscription("free")
    a.quiet_base(pages=False)
    a.page("sample-creator", "샘플 크리에이터", public=True, blocks=0)
    return "link_page_empty(220) · reason=no_blocks · 공개 페이지 1(블록 0)"


def h15(a: Account) -> str:
    """내용은 있는데 비공개."""
    a.subscription("free")
    a.quiet_base(pages=False)
    a.page("sample-studio", "샘플 스튜디오", public=False, blocks=3)
    return "link_page_empty(220) · reason=private · 비공개 페이지 1(블록 3)"


def h16(a: Account) -> str:
    """리포트 만드는 중 — 파란 배너.

    ⚠️ ``stage``·``error_code`` 는 **반드시 TextChoices 의 값**을 쓸 것. CharField 는
    ``save()`` 때 choices 를 검증하지 않아서, 없는 값을 넣어도 조용히 저장되고 화면만
    잘못 그려진다(실제로 ``stage="analyzing"`` 을 넣어 진행 화면이 「대기 중」으로 떨어졌다).
    """
    from apps.insta_reports.models import ReportStage, ReportStatus

    a.subscription(
        "pro",
        status="active",
        current_period_end=a.now + timedelta(days=17),
        monthly_amount_snapshot=14900,
        card=("현대", "433012******123*"),
    )
    conn = a.quiet_base()
    a.report(
        conn,
        ReportStatus.RUNNING,
        progress=45,
        stage=ReportStage.EXTRACTING,  # "영상 분석 중"
        message="영상 분석 중",
        stage_started_at=a.now - timedelta(minutes=4),
    )
    a.flush_cache()
    return "report_running(231) · progress=45 · stage=extracting(영상 분석 중)"


def h17(a: Account) -> str:
    """리포트 실패 — 빨간 배너.

    ``error_code`` 는 **대문자**(ReportErrorCode)다. 운영 경로도 전부 이 enum 을 쓰므로
    소문자로 내려가는 경로는 없다 — 소문자는 이 시드의 버그였다(2026-10-09 프론트 제보).
    """
    from apps.insta_reports.models import ReportErrorCode, ReportStage, ReportStatus

    a.subscription(
        "pro",
        status="active",
        current_period_end=a.now + timedelta(days=17),
        monthly_amount_snapshot=14900,
        card=("현대", "433012******123*"),
    )
    conn = a.quiet_base()
    a.report(
        conn,
        ReportStatus.FAILED,
        progress=62,
        # 실패 사유가 EXTRACT_FAILED 면 멈춘 단계도 그 단계여야 화면이 앞뒤가 맞는다.
        stage=ReportStage.EXTRACTING,
        error_code=ReportErrorCode.EXTRACT_FAILED,
        error_message="영상에서 특징을 뽑지 못했습니다.",
    )
    a.flush_cache()
    return "report_failed(232) · error_code=EXTRACT_FAILED · stage=extracting"


def h18(a: Account) -> str:
    """자원이 많음 — 자원 줄 5개 + 「전체 보기」 펼침."""
    a.subscription("free")
    conn = a.connection("sample_creator")
    pool = a.media_pool(conn)
    a.latest_post(conn, media=pool[0])
    a.campaign(conn, "가을 댓글 이벤트", pool[0], age_minutes=5)  # 최신 게시물 권유를 끈다
    # H01 과 같은 이유로 오류 캠페인은 끈다(큰 카드를 뺏긴다).
    a.common_campaigns(conn, error_campaign=False)  # 7개
    a.common_pages()  # 공개 + 비공개
    return "create_campaign(999, is_default=true) 하나뿐 · 자동 DM 8 · 링크 페이지 2"


# ═══════════════════════════════════════════════════════════════════════════
# 2. 요금제 · 결제 · 체험 (P)
# ═══════════════════════════════════════════════════════════════════════════


def p01(a: Account) -> str:
    a.subscription("free")
    a.quiet_base()
    return "free · trial_used_at=null (체험 미사용) · 카드 없음 → preview scenario=trial"


def _auto_trial(a: Account, days_left: int) -> str:
    """카드 없는 프로 자동 체험 (billing/auto_trial.py 가 지급하는 것과 같은 모양)."""
    from apps.billing.models import TrialKind

    sub = a.subscription(
        "pro",
        status="trialing",
        trial_kind=TrialKind.AUTO,
        current_period_start=a.now - timedelta(days=30 - days_left),
        current_period_end=a.now + timedelta(days=days_left),
        monthly_amount_snapshot=14900,
        trial_used_at=a.now - timedelta(days=30 - days_left),
        pro_activated_at=a.now - timedelta(days=30 - days_left),
    )
    sub.trial_plan = sub.plan
    sub.save(update_fields=["trial_plan"])
    return f"pro/trialing · 카드 없음(trial_kind=auto) · {days_left}일 남음 · 마지막 이용일 {sub.trial_last_day}"


def p02(a: Account) -> str:
    """카드 없는 프로 체험 25일 남음 + **인스타 미연결**."""
    summary = _auto_trial(a, 25)
    return summary + " · IG 연결 0 (가입 직후 계정 분석 팝업)"


def p03(a: Account) -> str:
    summary = _auto_trial(a, 3)
    a.quiet_base()
    return summary + " · 체험 종료 직전"


def p04(a: Account) -> str:
    """프로 체험 중 + 카드 등록함 → 첫 결제 예정일 안내."""
    from apps.billing.models import TrialKind

    sub = a.subscription(
        "pro",
        status="trialing",
        trial_kind=TrialKind.CARD,
        current_period_start=a.now - timedelta(days=8),
        current_period_end=a.now + timedelta(days=22),
        monthly_amount_snapshot=14900,
        trial_used_at=a.now - timedelta(days=8),
        pro_activated_at=a.now - timedelta(days=8),
        card=("현대", "433012******123*"),
    )
    sub.trial_plan = sub.plan
    sub.save(update_fields=["trial_plan"])
    a.quiet_base()
    return f"pro/trialing · 카드 있음(trial_kind=card) · 22일 남음 · 첫 결제 {sub.current_period_end:%Y-%m-%d}"


def p05(a: Account) -> str:
    """프로 유료(매월) + 추가 계정 2개 → 허용량 3, 연결 3."""
    a.subscription(
        "pro",
        status="active",
        current_period_end=a.now + timedelta(days=18),
        monthly_amount_snapshot=14900,
        extra_ig_accounts=2,
        trial_used_at=a.now - timedelta(days=120),
        card=("신한", "45184445****364*"),
    )
    a.quiet_base()
    a.connection("sample_studio", photo=False)
    a.connection("sample_archive")
    a.flush_cache()
    return "pro/active · 추가 계정 2 (허용량 3 / 연결 3, 전부 켜짐) · 다음 결제 18일 뒤"


def p06(a: Account) -> str:
    a.subscription(
        "basic",
        status="active",
        current_period_end=a.now + timedelta(days=20),
        monthly_amount_snapshot=4900,
        trial_used_at=a.now - timedelta(days=150),
        card=("국민", "53901234****567*"),
    )
    a.quiet_base()
    return "basic/active · 다음 결제 20일 뒤 → 요금제 창 업그레이드"


def p07(a: Account) -> str:
    a.subscription(
        "pro",
        status="past_due",
        current_period_end=a.now,
        next_billing_retry_at=a.now + timedelta(days=2),
        monthly_amount_snapshot=14900,
        renewal_attempts=1,
        last_billing_error="카드 한도 초과",
        trial_used_at=a.now - timedelta(days=90),
        card=("신한", "45184445****364*"),
    )
    a.quiet_base()
    return "pro/past_due · payment_failed(10) · 유예 7일 뒤 종료 · 재시도 2일 뒤"


def p08(a: Account) -> str:
    a.subscription(
        "pro",
        status="cancelled",
        current_period_end=a.now + timedelta(days=3, hours=6),  # days_left 가 2 로 깎이지 않게
        monthly_amount_snapshot=14900,
        cancelled_at=a.now - timedelta(days=1),
        trial_used_at=a.now - timedelta(days=90),
        # 해지 예약은 기간 끝까지 카드가 남아 있다. CANCELLED 는 process_due_renewals 가
        # 아예 제외하므로 카드가 있어도 과금되지 않는다.
        card=("현대", "433012******123*"),
    )
    a.quiet_base()
    return "pro/cancelled · subscription_ending(110) · 3일 뒤 종료 (H10 과 같은 상태)"


def p09(a: Account) -> str:
    """구독 일시정지 — 유료 기간이 끝나고 무과금 정지 중."""
    a.subscription(
        "pro",
        status="paused",
        current_period_end=a.now - timedelta(days=1),
        pause_ends_at=a.now + timedelta(days=59),
        paused_months=2,
        last_pause_at=a.now - timedelta(days=1),
        monthly_amount_snapshot=14900,
        trial_used_at=a.now - timedelta(days=120),
        card=("현대", "433012******123*"),
    )
    a.quiet_base()
    return "pro/paused · 2개월 정지 · 59일 뒤 자동 재개 (정지 중엔 무료 수준으로 게이팅)"


def p10(a: Account) -> str:
    from apps.billing.models import SubscriptionPlan

    sub = a.subscription("free", trial_used_at=a.now - timedelta(days=75))
    sub.trial_plan = SubscriptionPlan.objects.get(name="pro")
    sub.save(update_fields=["trial_plan"])
    a.quiet_base()
    return "free · trial_used_at 있음 → preview scenario=charge_now (체험 대신 바로 결제)"


def p11(a: Account) -> str:
    """프로 체험 중 + dev 제휴코드 — 적용 / 적용됨 / 쓸 수 없음 3상태."""
    summary = _auto_trial(a, 20)
    a.quiet_base()
    a.note(
        f"dev 제휴코드: {REFERRAL_CODE} (보너스 {REFERRAL_BONUS_DAYS}일). "
        "'쓸 수 없음' 은 아무 문자열이나 넣으면 된다(예: NOPE0000)."
    )
    return summary + f" · 제휴코드 {REFERRAL_CODE} 사용 가능"


# ═══════════════════════════════════════════════════════════════════════════
# 3. 인스타그램 연결 (I)
# ═══════════════════════════════════════════════════════════════════════════


def i01(a: Account) -> str:
    a.subscription("free")
    return "IG 연결 0 · 자동 DM 0 · 링크 페이지 0 (이메일 가입 직후)"


def i02(a: Account) -> str:
    """계정 전환 창의 세 상태 — 사용 중 / 연결 만료 / 사용 안 함."""
    from apps.integrations.models import IGAccountConnection

    # 허용량 2 (pro + 추가 1) · 켜진 계정 2 → 강제 팝업이 뜨지 않는다.
    a.subscription(
        "pro",
        status="active",
        current_period_end=a.now + timedelta(days=16),
        monthly_amount_snapshot=14900,
        extra_ig_accounts=1,
        card=("현대", "433012******123*"),
    )
    conn = a.connection("sample_creator")
    pool = a.media_pool(conn)
    a.latest_post(conn, media=pool[0])
    a.campaign(conn, "가을 댓글 이벤트", pool[0])
    a.connection(
        "sample_studio",
        photo=False,  # 사진 없는 계정 → 첫 글자 표시 확인용
        status=IGAccountConnection.Status.EXPIRED,
        reconnect_reason="reconnect_required",
        token_dead_first_seen_at=a.now - timedelta(days=2),
    )
    a.connection("sample_archive", is_active=False)
    a.page("sample-creator", "샘플 크리에이터", public=True, blocks=3)
    a.flush_cache()
    return (
        "연결 3 — 사용 중 @sample_creator(사진 O) · 연결 만료 @sample_studio(사진 X) · "
        "사용 안 함 @sample_archive / ig_disconnected(20) 1건"
    )


def i03(a: Account) -> str:
    """지금 쓰는 계정의 연결이 만료됨 — 계정 칩에 「다시 연결 필요」."""
    from apps.integrations.models import IGAccountConnection

    a.subscription("free")
    conn = a.connection(
        "sample_creator",
        status=IGAccountConnection.Status.EXPIRED,
        reconnect_reason="reconnect_required",
        token_dead_first_seen_at=a.now - timedelta(hours=9),
    )
    pool = a.media_pool(conn)
    a.campaign(conn, "가을 댓글 이벤트", pool[0])
    a.page("sample-creator", "샘플 크리에이터", public=True, blocks=3)
    return "연결 1 · status=expired · is_active=true → 사용 중인데 「다시 연결 필요」"


def i04(a: Account) -> str:
    """연결 3개인데 한도 1개 → 반드시 골라야 넘어가는 모달."""
    a.subscription("free")  # 허용량 1
    a.connection("sample_creator")
    a.connection("sample_studio", photo=False)
    a.connection("sample_archive")
    a.flush_cache()
    return "blocking = ig_account_selection_required · 허용량 1 / 켜진 계정 3"


def i05(a: Account) -> str:
    """사용 계정을 오늘 이미 바꿈 → 계정 고르기 잠김(하루 1회)."""
    a.subscription(
        "pro",
        status="active",
        current_period_end=a.now + timedelta(days=14),
        monthly_amount_snapshot=14900,
        extra_ig_accounts=1,
        ig_account_activation_changed_at=timezone.now(),
        card=("현대", "433012******123*"),
    )
    conn = a.connection("sample_creator")
    pool = a.media_pool(conn)
    a.latest_post(conn, media=pool[0])
    a.campaign(conn, "가을 댓글 이벤트", pool[0])
    a.connection("sample_studio", photo=False)
    a.connection("sample_archive", is_active=False)
    a.page("sample-creator", "샘플 크리에이터", public=True, blocks=3)
    a.flush_cache()
    return (
        "허용량 2 / 켜짐 2 · 꺼짐 1 · ig_account_activation_changed_at=지금 "
        "→ GET /billing/ig-account-activation/ 의 can_change=false"
    )


def i06(a: Account) -> str:
    """인스타그램으로 가입 — 자리표시 메일(`@ig.invalid`)."""
    a.user.instagram_user_id = f"devig{a.user.id}"
    a.user.save(update_fields=["instagram_user_id"])
    a.subscription("free")
    a.quiet_base()
    a.note(
        "로그인 이메일이 다른 계정과 다르다: "
        f"{a.email} (IG 로 가입하면 이메일이 없어 자리표시를 발급한다)"
    )
    return "email_is_placeholder=true · instagram_user_id 있음 → 사이드바 이메일 등록 안내"


def i07(a: Account) -> str:
    a.subscription("free")
    a.quiet_base()
    a.user.full_name = ""
    a.user.save(update_fields=["full_name"])
    return "full_name='' → 사이드바·더보기 프로필 줄의 빈 이름 처리"


# ═══════════════════════════════════════════════════════════════════════════
# 4. 가입 · 로그인 (A)
# ═══════════════════════════════════════════════════════════════════════════


def a01(a: Account) -> str:
    """이메일 인증 미완료 — 로그인은 되고, 6자리 인증 화면이 떠야 한다."""
    a.subscription("free")
    a.quiet_base()
    a.user.is_email_verified = False
    a.user.email_verified_at = None
    a.user.save(update_fields=["is_email_verified", "email_verified_at"])
    a.note("로그인 자체는 막히지 않는다 — `user.is_email_verified=false` 로 화면을 분기할 것.")
    return "is_email_verified=false · 그 밖은 깨끗함"


def a02(a: Account) -> str:
    a.subscription("free")
    a.quiet_base()
    a.user.kakao_id = f"devkakao{a.user.id}"
    a.user.save(update_fields=["kakao_id"])
    a.note(
        "카카오 **버튼** 흐름은 실제 카카오 계정이 있어야 돈다 — 이 계정은 "
        "`kakao_id` 가 박힌 '카카오로 가입한 상태'의 화면 확인용이다."
    )
    return "kakao_id 있음 (카카오로 가입한 계정의 서버 상태)"


def a03(a: Account) -> str:
    a.subscription("free")
    a.quiet_base()
    a.note(
        "⚠️ 서버에 '구글로 가입' 표식 필드가 **없다**(구글은 email 로만 매칭). "
        "구글 로그인 자체는 dev 에 GOOGLE_CLIENT_ID 가 있어 실제 구글 계정으로 바로 된다."
    )
    return "구글 가입 전용 상태값이 없어 일반 이메일 계정과 서버 상태가 같다"


def a04(a: Account) -> str:
    """이미 다른 Turnflow 계정에 연결된 인스타 계정."""
    a.subscription("free")
    a.quiet_base()
    a.note(
        "⚠️ 409 INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE 를 **실제로 띄우려면** 테스트에 쓸 "
        "진짜 IG 계정의 user_id 가 필요하다(목 로그인은 매번 랜덤 id 를 만들어 재현 불가). "
        "쓸 IG 계정을 알려주면 그 id 를 이 계정에 박아 둔다 — "
        "`--ig-user-id <id>` 로 재시드."
    )
    return "이 계정이 'IG 를 선점한 쪽' 역할 (상대편 id 는 별도 지정 필요)"


# ═══════════════════════════════════════════════════════════════════════════
# 레지스트리
# ═══════════════════════════════════════════════════════════════════════════

# (코드, 제목, 빌더)
SPECS: list[tuple[str, str, object]] = [
    ("H01", "대표 화면 — 새 게시물 + 리포트 + 알림 센터", h01),
    ("H02", "멈춤 4종", h02),
    ("H03a", "연결 끊김 · 비밀번호 변경", h03a),
    ("H03b", "연결 끊김 · 본인확인 필요", h03b),
    ("H03c", "연결 끊김 · 앱 권한 회수", h03c),
    ("H03d", "연결 끊김 · 기타", h03d),
    ("H04", "댓글 받기 끊김", h04),
    ("H05", "월 한도 소진", h05),
    ("H06", "게시물 제한으로 멈춘 캠페인 2개", h06),
    ("H07", "인스타가 발송을 막음", h07),
    ("H08", "꺼져 있는 계정", h08),
    ("H09", "월 한도 85% 경고", h09),
    ("H10", "해지 예약", h10),
    ("H11", "처음 가입한 사용자", h11),
    ("H12", "정상 작동 중", h12),
    ("H13", "작성 중 자동 DM", h13),
    ("H14", "블록 없는 공개 페이지", h14),
    ("H15", "내용 있는 비공개 페이지", h15),
    ("H16", "리포트 만드는 중", h16),
    ("H17", "리포트 실패", h17),
    ("H18", "자원이 많음", h18),
    ("P01", "무료 · 체험 미사용", p01),
    ("P02", "카드 없는 프로 체험 25일 · IG 미연결", p02),
    ("P03", "카드 없는 프로 체험 3일 남음", p03),
    ("P04", "프로 체험 + 카드 등록", p04),
    ("P05", "프로 유료 + 추가 계정 2", p05),
    ("P06", "베이직 유료", p06),
    ("P07", "결제 실패(유예 중)", p07),
    ("P08", "해지 예약", p08),
    ("P09", "구독 일시정지", p09),
    ("P10", "무료 · 체험 사용함", p10),
    ("P11", "프로 체험 + 제휴코드", p11),
    ("I01", "IG 연결 0", i01),
    ("I02", "IG 연결 3종 상태", i02),
    ("I03", "쓰는 계정의 연결 만료", i03),
    ("I04", "연결 3 · 한도 1 (강제 선택)", i04),
    ("I05", "오늘 이미 계정 변경", i05),
    ("I06", "인스타그램으로 가입", i06),
    ("I07", "이름이 빈 계정", i07),
    ("A01", "이메일 인증 미완료", a01),
    ("A02", "카카오로 가입", a02),
    ("A03", "Google 로 가입", a03),
    ("A04", "IG 가 다른 계정에 연결됨", a04),
]

SPEC_BY_CODE = {code: (title, fn) for code, title, fn in SPECS}
ALL_CODES = [code for code, _t, _f in SPECS]
