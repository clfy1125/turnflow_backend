"""
Custom User model with email as username
"""

import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone

# 인스타그램 로그인 사용자의 **자리표시 이메일** 도메인.
# ⭐ RFC 2606 이 예약한 `.invalid` TLD 를 쓴다 — 절대 해석되지 않으므로 실수로 메일이
#    나가도 **바운스가 우리 발송 도메인 평판을 깎는 일이 구조적으로 불가능**하다.
#    (우리 소유 서브도메인을 쓰면 언젠가 MX 가 붙어 진짜 메일함이 될 수 있다.)
# 발송 차단은 여기 문자열이 아니라 apps/emails/services/sender.py 의 게이트가 담당한다.
PLACEHOLDER_EMAIL_DOMAIN = "ig.invalid"


class UserManager(BaseUserManager):
    """Custom user manager for email-based authentication"""

    def create_user(self, email, password=None, **extra_fields):
        """Create and return a regular user with email and password"""
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Create and return a superuser with email and password"""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    """
    Custom User model
    - Email is used as the username field
    - Additional fields can be added later for profile, workspace relations, etc.
    """

    email = models.EmailField(unique=True, verbose_name="Email Address")
    full_name = models.CharField(max_length=255, blank=True, verbose_name="Full Name")
    is_email_verified = models.BooleanField(default=False, verbose_name="Email Verified")
    email_verified_at = models.DateTimeField(
        null=True, blank=True, verbose_name="Email Verified At"
    )
    # 마케팅(광고성) 수신 동의 — 정보통신망법. 윈백 등 마케팅 메일 발송의 필수 게이트.
    # 현재 수집 경로(가입/설정)가 연결되기 전까지 기본 False → 마케팅 발송 dormant.
    marketing_opt_in = models.BooleanField(default=False, verbose_name="마케팅 수신 동의")
    marketing_opt_in_at = models.DateTimeField(
        null=True, blank=True, verbose_name="마케팅 수신 동의 시각"
    )

    # ── 카카오 로그인 회원번호 (apps/authentication/kakao.py) ─────────────────────
    # ⭐ 구글은 이런 필드를 두지 않고 email 로만 찾는데, 카카오만 따로 두는 이유:
    #    카카오계정은 **전화번호가 주 식별자**이고 이메일은 사용자가 자유롭게 바꾸거나
    #    떼어낼 수 있는 부가 정보다. email 로만 찾으면 사용자가 카카오 이메일을 바꾼 순간
    #    같은 사람에게 **새 계정이 생기고**(=워크스페이스·구독과 단절) CS 로 들어온다.
    #    회원번호는 앱 연결이 유지되는 한 불변이라 이 사고를 구조적으로 막는다.
    # 조회 순서: kakao_id → (없으면) email → 찾으면 kakao_id 를 그때 채운다(=자동 연결).
    # null 을 쓰는 이유: unique 인데 빈 문자열을 쓰면 카카오 미사용자끼리 충돌한다.
    kakao_id = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        unique=True,
        default=None,
        verbose_name="카카오 회원번호",
        help_text="카카오 로그인 사용자의 앱별 회원번호(id). 이메일이 바뀌어도 불변.",
    )

    # ── 인스타그램 로그인 계정 식별자 (apps/authentication/instagram.py) ──────────
    # 카카오의 ``kakao_id`` 와 같은 이유로 둔다 — 다만 인스타는 사정이 더 나쁘다:
    # **Instagram Business Login 은 이메일을 주지 않는다.** 그래서 email 로는 애초에
    # 같은 사람을 찾을 방법이 없고, 이 필드가 **유일한 식별자**다.
    # 조회 순서: instagram_user_id → (없으면) 이미 그 IG 계정을 연동해 둔 워크스페이스의
    #            owner → 찾으면 그때 채운다(= 기존 사용자 자동 연결).
    # ⚠️ username 으로 찾으면 안 된다 — IG username 은 사용자가 언제든 바꿀 수 있고,
    #    남이 그 username 을 이어받을 수 있다(계정 탈취 표면).
    instagram_user_id = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        unique=True,
        default=None,
        verbose_name="인스타그램 사용자 ID",
        help_text="Instagram Business Login 의 앱 스코프 사용자 ID. username 과 달리 불변.",
    )

    # ── 이메일 등록 대기 (인스타 로그인 사용자용, 2026-09-12) ────────────────────
    # IG 로 가입하면 ``email`` 이 자리표시(`@ig.invalid`)라 메일을 보낼 수 없다. 실제
    # 주소를 받되 **인증이 끝나기 전까지는 ``email`` 을 바꾸지 않는다** — 오타 한 번에
    # 로그인 키가 아무도 소유하지 않는 주소로 바뀌고, 그 순간 복구 경로가 사라진다.
    # 그래서 후보 주소를 여기 담아 두고, 그 주소로 보낸 6자리 코드가 맞을 때만 승격한다.
    # ⚠️ unique 가 아니다 — 두 사람이 같은 주소를 '신청'만 하는 것은 막지 않고,
    #    **확정 시점에** 중복을 검사한다(그 사이 남이 먼저 가져갔을 수 있다).
    pending_email = models.EmailField(
        blank=True,
        default="",
        verbose_name="인증 대기 중인 이메일",
        help_text="POST /api/v1/auth/me/email/ 로 신청한 주소. 인증되면 email 로 승격되고 비워진다.",
    )

    # ── 성장 팝업 노출 상태 (2026-09-12) ────────────────────────────────────────
    # 대행사 요청서 §06: "서버 사용자 속성 권장. 브라우저 저장소만 쓰면 기기를 바꿀 때
    # 초기화됩니다." — 실제로 모바일에서 보고 PC 에서 다시 보면 팝업이 처음부터 다시
    # 뜬다(노출 규칙이 '최대 3회'인데 기기 수만큼 곱해진다).
    #
    # ⭐ 테이블이 아니라 **User 의 JSON 한 칸**인 이유: 이 데이터는 사용자당 항상 1행이고
    #    (팝업 종류 4개 × 카운터 몇 개), 언제나 통째로 읽고 통째로 쓴다. 조인도 집계도
    #    하지 않는다. 별도 테이블은 마이그레이션·조인·정리 배치를 늘리기만 한다.
    #    ⚠️ 반대로, **분석용 이벤트는 여기 넣지 말 것** — 그건 append-only 여야 하고
    #    analytics.FunnelEvent 가 담당한다. 여기 있는 건 "다음에 띄울까 말까"의 현재 상태뿐이다.
    #
    # 모양(프론트 src/lib/popupState.ts 계약):
    #   {"signup": {"shows": 2, "dismisses": 1, "lastShownAt": "...", "converted": false}, ...}
    # 서버는 **키를 강제하지 않는다** — 팝업이 추가될 때마다 백엔드 배포를 기다리지
    # 않도록. 대신 크기(POPUP_STATE_MAX_BYTES)와 최상위 키 개수만 제한한다.
    popup_state = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="팝업 노출 상태",
        help_text="팝업 키 → 노출/닫기 카운터. GET·PATCH /api/v1/auth/me/popup-state/",
    )

    # ── 회원탈퇴 유예 (Google Play 계정 삭제 정책 / 개인정보보호법 §21) ──────────────
    # 웹 단독 탈퇴(`turnflow.link/delete-account`)는 즉시 하드 삭제가 아니라
    #   ① 즉시 비활성화(is_active=False) + 구독 해지  ② `deletion_scheduled_at` 후 영구 파기
    # 2단으로 처리한다. 유예를 두는 이유가 두 개다:
    #   - 개인정보보호법 §21① "지체 없이 파기" 를 지키면서 오탈퇴를 되돌릴 창구가 필요하다.
    #     (유예 목적·기간을 고지하고 취소 경로를 실제로 제공해야 '지연'이 아닌 '유예'로 방어된다)
    #   - 탈퇴 자체가 메일함 접근만으로 가능하므로, 메일함 탈취 시 복구 경로가 된다.
    # ⚠️ 이 필드가 채워진 계정은 `is_active=False` 라 `authenticate()` 가 None 을 준다.
    #    LoginView 가 그 상태를 구분해 복구 안내를 내려준다 — 안 하면 "비밀번호 틀림"으로 보인다.
    deletion_requested_at = models.DateTimeField(
        null=True, blank=True, verbose_name="탈퇴 확정 시각"
    )
    deletion_scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="영구 삭제 예정 시각",
        help_text="이 시각이 지나면 authentication.purge_deleted_accounts 가 하드 삭제한다",
    )

    username = None  # Remove username field

    # Override username to use email
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # No additional required fields for createsuperuser

    objects = UserManager()

    class Meta:
        db_table = "users"
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["-date_joined"]

    def __str__(self):
        return self.email

    @property
    def display_name(self):
        """Return full name if available, otherwise email"""
        return self.full_name if self.full_name else self.email

    @property
    def is_pending_deletion(self) -> bool:
        """탈퇴 확정됐으나 아직 영구 파기 전(유예 중)인가."""
        return self.deletion_scheduled_at is not None

    @property
    def email_is_placeholder(self) -> bool:
        """이메일이 **우리가 지어낸 자리표시**인가 (= 이 사람에게 메일을 보낼 수 없다).

        Instagram Business Login 은 이메일을 주지 않는데 ``email`` 은 우리 로그인 키라
        비워 둘 수 없다. 그래서 IG 로 가입한 사용자에게는 ``ig_<id>@ig.invalid`` 를 만들어
        준다. 프론트는 이 값이 true 면 "알림을 받을 이메일을 등록해 주세요" 를 띄운다.
        """
        return (self.email or "").endswith("@" + PLACEHOLDER_EMAIL_DOMAIN)


class InstagramLoginState(models.Model):
    """인스타그램 **로그인** OAuth 의 state 1회용 저장소.

    ``integrations.IGOAuthState``(연동용)와 따로 두는 이유: 저쪽은 ``workspace`` FK 가
    필수인데, 로그인은 **워크스페이스는커녕 계정도 아직 없는** 사람이 시작한다.
    저 모델의 FK 를 nullable 로 바꾸면 라이브 연동 경로의 불변식이 하나 약해진다
    (그 코드는 workspace 가 항상 있다고 가정하고 200줄을 쓴다).

    ⚠️ **1회용**이다 — ``consumed_at`` 이 찍힌 state 는 재사용을 거부한다. 인가 코드는
       어차피 1회용이지만, state 를 재사용 가능하게 두면 새로고침 한 번에 같은 코드가
       두 번 교환돼 사용자에게 실패 화면이 뜬다.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=255, unique=True, db_index=True)
    redirect_uri = models.CharField(
        max_length=500,
        verbose_name="검증된 redirect_uri",
        help_text=(
            "authorize 에 쓴 값. 코드 교환 때 **글자 그대로** 다시 보내야 Meta 가 받아 준다 "
            "— 그래서 클라이언트가 다시 보내온 값을 믿지 않고 여기 저장한 값을 쓴다."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True, verbose_name="사용 시각")

    class Meta:
        db_table = "ig_login_oauth_states"
        verbose_name = "Instagram 로그인 State"
        verbose_name_plural = "Instagram 로그인 State 목록"

    def __str__(self) -> str:
        return f"InstagramLoginState({self.state[:8]}… exp={self.expires_at:%m-%d %H:%M})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at
