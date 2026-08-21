"""웹 단독 회원탈퇴 (`turnflow.link/delete-account`) — 판정·상태전이 단일 소스.

Google Play 계정 삭제 정책: 사용자가 **앱을 다시 설치하거나 앱으로 돌아가지 않고도**
계정 삭제 요청을 시작할 수 있어야 한다. 그래서 이 경로는 로그인 없이 **가입 이메일
소유 증명**만으로 진행된다.

흐름
    ① request  이메일 입력 → 메일로 인증 링크 발송   (열거 방지: 응답이 항상 같다)
    ② verify   링크 클릭 → 삭제 범위·영향 고지 화면   (토큰 소비하지 않음)
    ③ confirm  최종 동의 → 구독 즉시 해지 + 계정 비활성화 + 유예 예약
    ④ purge    유예 만료 후 주기잡이 하드 삭제
    ⑤ restore  유예 중 복구 (메일 링크 / 로그인 시도 안내)

⚠️ 법정 보존 의무가 삭제 요구를 이긴다 — 여기서 "모두 삭제"라고 고지하면 허위가 된다.
   보존 항목·근거·기간은 `LEGAL_RETENTION` 에 두고 화면·메일이 그것을 그대로 읽는다.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: 탈퇴 확정 → 영구 파기까지의 유예. 이 기간의 정당화는 User.deletion_scheduled_at 주석 참고.
GRACE_DAYS = getattr(settings, "ACCOUNT_DELETION_GRACE_DAYS", 7)

#: 삭제 요청 메일 링크의 유효시간.
REQUEST_TTL_MINUTES = getattr(settings, "ACCOUNT_DELETION_TTL_MINUTES", 30)

#: 복구 링크의 유효시간 — 유예 기간 전체를 덮어야 의미가 있다.
RESTORE_TTL_MINUTES = GRACE_DAYS * 24 * 60

#: 유예 기간 내 삭제되는 것 (화면·메일이 그대로 읽는다)
DELETED_ITEMS: list[str] = [
    "계정 정보 (이메일, 이름, 비밀번호)",
    "보유한 모든 페이지와 그 하위 데이터 (블록, 통계, 문의, 구독자, 업로드 이미지)",
    "인스타그램 연동 정보 및 액세스 토큰",
    "자동 DM 캠페인, 템플릿, 발송 로그",
    "워크스페이스 및 멤버십",
    "등록된 결제 수단 (카드 정보는 탈퇴 확정 즉시 삭제)",
]

#: 법정 의무로 **삭제할 수 없는** 것. (항목, 근거, 기간)
#: 이 목록을 줄이려면 근거 법령을 먼저 확인할 것 — 임의 축약은 허위 고지가 된다.
LEGAL_RETENTION: list[dict[str, str]] = [
    {
        "item": "계약 또는 청약철회 등에 관한 기록",
        "basis": "전자상거래 등에서의 소비자보호에 관한 법률 제6조",
        "period": "5년",
    },
    {
        "item": "대금결제 및 재화 등의 공급에 관한 기록",
        "basis": "전자상거래 등에서의 소비자보호에 관한 법률 제6조",
        "period": "5년",
    },
    {
        "item": "전자금융거래에 관한 기록",
        "basis": "전자금융거래법 제22조",
        "period": "5년",
    },
    {
        "item": "소비자의 불만 또는 분쟁처리에 관한 기록",
        "basis": "전자상거래 등에서의 소비자보호에 관한 법률 제6조",
        "period": "3년",
    },
    {
        "item": "접속에 관한 기록(로그)",
        "basis": "통신비밀보호법 제15조의2",
        "period": "3개월",
    },
]


class DeletionError(Exception):
    """탈퇴 처리 중 사용자에게 보여줄 수 있는 오류."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _grace_deadline(now=None):
    return (now or timezone.now()) + timezone.timedelta(days=GRACE_DAYS)


def legal_notice() -> dict:
    """화면·메일이 공유하는 고지 문구 묶음. 여기가 단일 소스다."""
    return {
        "grace_days": GRACE_DAYS,
        "deleted_items": DELETED_ITEMS,
        "legal_retention": LEGAL_RETENTION,
    }


def describe_public_policy() -> dict:
    """공개 고지문 (`GET /auth/deletion/policy/`).

    프론트에 하드코딩하지 않고 서버가 내려주는 이유: 법령이 바뀌거나 보존 항목이
    늘었을 때 화면과 실제 처리가 어긋나면 그 자체가 허위 고지가 된다.
    """
    return legal_notice()


def mask_email(email: str) -> str:
    """확인 화면에 어느 계정인지 보여주되 전체 주소는 노출하지 않는다.

    링크를 가로챈 제3자에게 가입 이메일 전체를 알려주지 않기 위한 것이다.
    """
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    shown = local[:2] if len(local) > 2 else local[:1]
    return f"{shown}{'*' * max(len(local) - len(shown), 1)}@{domain}"


# ─────────────────────────────────────────────────────────────────────────────
# ① 요청 — 인증 메일 발송
# ─────────────────────────────────────────────────────────────────────────────
def request_deletion(*, email: str, request_ip: str | None = None) -> bool:
    """가입 이메일로 탈퇴 인증 링크를 보낸다. 보냈으면 True.

    ⚠️ **호출자는 반환값으로 응답을 갈라선 안 된다.** 가입 여부에 따라 응답이 달라지면
    이 엔드포인트가 이메일 가입 여부 확인 도구(user enumeration)가 된다.
    반환값은 로깅·테스트 용도다.
    """
    from django.contrib.auth import get_user_model

    from apps.emails.tasks import send_account_deletion_email

    user = get_user_model().objects.filter(email__iexact=email.strip()).first()
    if user is None:
        logger.info("account_deletion: 미가입 이메일로 요청 (동일 응답 반환)")
        return False

    if user.is_pending_deletion:
        # 이미 유예 중 — 다시 삭제 링크를 보내면 혼란만 준다. 조용히 무시하되
        # 응답은 동일하게 유지한다.
        logger.info("account_deletion: 이미 유예 중 user_id=%s", user.pk)
        return False

    if user.is_superuser or user.is_staff:
        # 운영자 계정이 메일 한 통으로 사라지면 안 된다. 별도 절차로 처리한다.
        logger.warning("account_deletion: 운영자 계정 요청 차단 user_id=%s", user.pk)
        return False

    send_account_deletion_email.delay(user.pk, request_ip)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# ② 확인 화면용 조회 (토큰 소비하지 않음)
# ─────────────────────────────────────────────────────────────────────────────
def describe_pending(*, raw_token: str) -> dict:
    """토큰이 가리키는 계정의 삭제 영향 요약. 토큰은 **소비하지 않는다**."""
    from apps.emails.models import EmailToken, EmailTokenPurpose

    row = EmailToken.peek(raw_token=raw_token, purpose=EmailTokenPurpose.ACCOUNT_DELETE)
    if row is None:
        raise DeletionError(
            "invalid_token", "링크가 만료되었거나 이미 사용되었습니다. 처음부터 다시 요청해 주세요."
        )

    user = row.user
    if user.is_pending_deletion:
        raise DeletionError("already_pending", "이미 탈퇴가 접수된 계정입니다.")

    from apps.billing.models import SubscriptionStatus, UserSubscription

    sub = UserSubscription.objects.filter(user=user).select_related("plan").first()
    paid_active = bool(
        sub
        and sub.is_paid_plan
        and sub.status
        in (
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.TRIALING,
            SubscriptionStatus.PAST_DUE,
            SubscriptionStatus.PAUSED,
        )
    )

    # 소유 워크스페이스에 본인 외 멤버가 있으면 그 사람들의 데이터도 함께 사라진다.
    from apps.workspace.models import Membership, Workspace

    owned = Workspace.objects.filter(owner=user)
    other_members = (
        Membership.objects.filter(workspace__in=owned).exclude(user=user).count() if owned else 0
    )

    return {
        "email_masked": mask_email(user.email),
        "has_paid_subscription": paid_active,
        "plan": (sub.plan.display_name if paid_active and sub and sub.plan_id else None),
        # 잔여 유료기간은 환불 없이 소멸한다 — 확인 화면이 반드시 이걸 보여줘야 한다.
        "paid_until": (
            sub.current_period_end.isoformat() if paid_active and sub.current_period_end else None
        ),
        "other_workspace_members": other_members,
        **legal_notice(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ③ 확정 — 구독 해지 + 비활성화 + 유예 예약
# ─────────────────────────────────────────────────────────────────────────────
def confirm_deletion(*, raw_token: str) -> dict:
    """토큰을 소비하고 탈퇴를 확정한다.

    이 함수가 끝나면 계정은 로그인 불가(`is_active=False`)이고 과금도 멈춘다.
    실제 데이터 파기는 `purge_deleted_accounts` 가 유예 만료 후에 한다.
    """
    from apps.billing.deletion import cancel_subscription_for_deletion
    from apps.emails.models import EmailToken, EmailTokenPurpose

    # 파괴적 동작 직전에 consume — 단일사용을 여기서 보장한다.
    row = EmailToken.consume(raw_token=raw_token, purpose=EmailTokenPurpose.ACCOUNT_DELETE)
    if row is None:
        raise DeletionError(
            "invalid_token", "링크가 만료되었거나 이미 사용되었습니다. 처음부터 다시 요청해 주세요."
        )

    user = row.user
    if user.is_pending_deletion:
        raise DeletionError("already_pending", "이미 탈퇴가 접수된 계정입니다.")

    # 구독 해지는 트랜잭션 밖에서 먼저 한다 — 토스 호출(빌링키 삭제)이 섞이면
    # DB 트랜잭션이 외부 HTTP 를 붙들게 된다.
    billing = cancel_subscription_for_deletion(user)

    now = timezone.now()
    deadline = _grace_deadline(now)

    with transaction.atomic():
        locked = type(user).objects.select_for_update().get(pk=user.pk)
        if locked.is_pending_deletion:
            raise DeletionError("already_pending", "이미 탈퇴가 접수된 계정입니다.")
        locked.is_active = False
        locked.deletion_requested_at = now
        locked.deletion_scheduled_at = deadline
        locked.save(
            update_fields=[
                "is_active",
                "deletion_requested_at",
                "deletion_scheduled_at",
            ]
        )

    _blacklist_tokens(user)

    logger.info(
        "account_deletion: 확정 user_id=%s purge_at=%s paid=%s",
        user.pk,
        deadline.isoformat(),
        billing.get("was_paid"),
    )

    # 접수 확인 + 복구 링크 메일 (best-effort — 실패가 탈퇴를 되돌리면 안 된다)
    try:
        from apps.emails.tasks import send_account_deletion_confirmed_email

        send_account_deletion_confirmed_email.delay(user.pk)
    except Exception:  # noqa: BLE001
        logger.warning("account_deletion: 접수 메일 enqueue 실패 user_id=%s", user.pk, exc_info=True)

    return {
        "email_masked": mask_email(user.email),
        "purge_at": deadline.isoformat(),
        "cancelled_subscription": billing.get("was_paid", False),
        **legal_notice(),
    }


def _blacklist_tokens(user) -> None:
    """발급된 refresh 토큰을 즉시 무효화. 실패해도 탈퇴를 막지 않는다."""
    try:
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
        from rest_framework_simplejwt.tokens import RefreshToken

        for token in OutstandingToken.objects.filter(user=user):
            try:
                RefreshToken(token.token).blacklist()
            except Exception:  # noqa: BLE001 — 만료·중복 블랙리스트는 정상
                pass
    except ImportError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ 복구
# ─────────────────────────────────────────────────────────────────────────────
def restore_account(*, user) -> dict:
    """유예 중인 계정을 되살린다.

    ⚠️ **구독은 되살아나지 않는다.** 확정 시점에 이미 해지하고 빌링키를 지웠으므로
    복구된 계정은 무료 플랜 상태이고 카드를 다시 등록해야 한다. 이 사실을 복구
    화면·메일이 반드시 알려줘야 한다 — 안 알리면 "복구했는데 유료가 아니다"가 된다.
    """
    if not user.is_pending_deletion:
        raise DeletionError("not_pending", "탈퇴 접수 상태가 아닙니다.")

    with transaction.atomic():
        locked = type(user).objects.select_for_update().get(pk=user.pk)
        locked.is_active = True
        locked.deletion_requested_at = None
        locked.deletion_scheduled_at = None
        locked.save(
            update_fields=["is_active", "deletion_requested_at", "deletion_scheduled_at"]
        )

    logger.info("account_deletion: 복구 user_id=%s", user.pk)
    return {"email_masked": mask_email(user.email), "subscription_restored": False}


def restore_by_token(*, raw_token: str) -> dict:
    from apps.emails.models import EmailToken, EmailTokenPurpose

    row = EmailToken.consume(raw_token=raw_token, purpose=EmailTokenPurpose.ACCOUNT_RESTORE)
    if row is None:
        raise DeletionError(
            "invalid_token",
            "복구 링크가 만료되었거나 이미 사용되었습니다. 로그인 화면에서 복구를 시도해 주세요.",
        )
    return restore_account(user=row.user)


# ─────────────────────────────────────────────────────────────────────────────
# ④ 파기
# ─────────────────────────────────────────────────────────────────────────────
def purge_user(user) -> None:
    """하드 삭제. 유예가 만료된 계정에만 호출할 것.

    ⚠️ `Workspace.owner` 가 PROTECT 라 워크스페이스를 먼저 지워야 `user.delete()` 가
    ProtectedError 없이 통과한다. (기존 AccountDeleteView 와 같은 이유·같은 순서)
    """
    from apps.workspace.models import Workspace

    Workspace.objects.filter(owner=user).delete()
    user.delete()
