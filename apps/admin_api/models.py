"""apps/admin_api/models.py — 어드민 백오피스 전용 모델.

이 앱은 기존 도메인 모델(authentication / workspace / pages / integrations)을
**읽기/제어**하는 백오피스 API 를 제공한다. 도메인 데이터의 진실의 원천은 각 앱 모델이며,
여기서는 백오피스 자체 소유 데이터만 정의한다:
- :class:`AdminActionLog` — 관리자 액션 감사 로그
- :class:`MarketingChannelLink` — 마케팅 채널 링크 생성기의 저장 링크 (전 관리자 공용)
- :class:`AdminMFADevice` / :class:`AdminBackupCode` / :class:`AdminDevice`
  — 어드민 2단계 인증 (docs/ops/ADMIN_AUTH_HARDENING_PLAN.md)
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.integrations.encryption import EncryptedTextField


class AdminActionLog(models.Model):
    """관리자(스태프)가 수행한 모든 변경(mutation)에 대한 감사 로그.

    - 조회(GET)는 기록하지 않는다. 상태를 바꾸는 PATCH/POST/DELETE 만 적재.
    - 개별 도메인 모델과 느슨하게 연결(``target_type`` + ``target_id`` 문자열)하여
      User(int) / Workspace(uuid) / Page(slug) 등 이종 PK 를 모두 수용한다.
    - 적재 실패가 본 요청을 깨지 않도록, 호출은 항상 ``apps.admin_api.audit.log_admin_action``
      헬퍼(try/except 래핑)를 통해서만 한다.
    """

    class Action(models.TextChoices):
        USER_UPDATE = "user.update", "회원 정보 수정"
        USER_PASSWORD_RESET = "user.password_reset", "회원 비밀번호 재설정 발송"
        USER_SUBSCRIPTION_UPDATE = "user.subscription_update", "회원 구독(요금제) 변경"
        WORKSPACE_UPDATE = "workspace.update", "워크스페이스 수정"
        MEMBERSHIP_UPDATE = "membership.update", "멤버 역할 변경"
        MEMBERSHIP_DELETE = "membership.delete", "멤버 제거"
        PAGE_UPDATE = "page.update", "페이지 차단/공개 변경"
        CAMPAIGN_PAUSE = "campaign.pause", "캠페인 일시중지"
        CAMPAIGN_RESUME = "campaign.resume", "캠페인 재개"
        DMLOG_RETRY = "dmlog.retry", "DM 재시도"
        DMLOG_REVERIFY = "dmlog.reverify", "DM 재검증"
        REFERRAL_CREATE = "referral.create", "레퍼럴 코드 생성"
        REFERRAL_UPDATE = "referral.update", "레퍼럴 코드 수정"
        REFERRAL_DELETE = "referral.delete", "레퍼럴 코드 삭제"
        CHANNEL_LINK_CREATE = "channel_link.create", "채널 링크 생성"
        CHANNEL_LINK_UPDATE = "channel_link.update", "채널 링크 수정"
        CHANNEL_LINK_DELETE = "channel_link.delete", "채널 링크 삭제"
        # RBAC-2 — 제한 역할(marketing_viewer)이 허용되지 않은 어드민 경로를 시도해 403.
        # 조회는 기록하지 않는 원칙의 예외 — 외부(외주) 계정의 접근 시도 이력은 남긴다.
        ADMIN_ACCESS_DENIED = "admin.access_denied", "어드민 접근 차단(권한 없음)"
        # ── 어드민 2단계 인증 ──
        # 로그인은 '변경'이 아니지만 기록한다 — 계정을 3명이 공유하던 때에는 남겨도 의미가
        # 없었고, 개인 계정으로 분리한 지금에야 "누가·언제·어디서" 가 성립한다.
        ADMIN_LOGIN = "admin.login", "어드민 로그인"
        ADMIN_MFA_ENROLLED = "admin.mfa_enrolled", "2단계 인증 등록"
        ADMIN_MFA_RESET = "admin.mfa_reset", "2단계 인증 초기화"
        ADMIN_BACKUP_CODES_REGENERATED = "admin.backup_codes_regenerated", "백업코드 재발급"
        ADMIN_DEVICE_TRUSTED = "admin.device_trusted", "기기 신뢰 등록"
        ADMIN_DEVICE_REVOKED = "admin.device_revoked", "기기 신뢰 해제"

    class Meta:
        db_table = "admin_action_logs"
        verbose_name = "Admin Action Log"
        verbose_name_plural = "Admin Action Logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["actor"]),
            models.Index(fields=["action"]),
            models.Index(fields=["target_type", "target_id"]),
        ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_actions",
        verbose_name="수행 관리자",
    )
    action = models.CharField(max_length=64, choices=Action.choices, verbose_name="액션")
    target_type = models.CharField(max_length=32, blank=True, verbose_name="대상 종류")
    target_id = models.CharField(max_length=64, blank=True, verbose_name="대상 ID")
    target_repr = models.CharField(max_length=255, blank=True, verbose_name="대상 표시명")
    changes = models.JSONField(default=dict, blank=True, verbose_name="변경 내역(before/after)")
    request_id = models.CharField(max_length=64, blank=True, verbose_name="X-Request-ID")
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="요청 IP")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="생성 시각")

    def __str__(self) -> str:
        who = self.actor.email if self.actor_id else "(deleted)"
        return f"{who} · {self.action} · {self.target_type}:{self.target_id}"


class MarketingChannelLink(models.Model):
    """마케팅 채널 링크 생성기의 '저장한 링크' — 전 관리자 공용 (M-4).

    프론트 localStorage 전용이던 저장 링크를 기기·관리자 간 공유되도록 서버로 이관.
    ``url``(utm 조합 최종 URL)·``channel``(analytics.channels.derive_channel 파생 키)은
    입력값이 아니라 **저장 시 서버가 계산**한다 — 계산 로직은
    :mod:`apps.admin_api.serializers.marketing` (단일 소스).
    조회 스코프는 전 관리자 공용(생성자 무관 전체 노출), created_by 는 표기용.
    """

    # MKT-13: 프론트가 링크 이름 입력칸을 없애고 `utm_campaign · utm_content` 로 자동
    # 조합한다 → 200(캠페인) + 3(구분자) + 200(콘텐츠) = 최대 403자.
    name = models.CharField(max_length=512, verbose_name="링크 이름")
    base_url = models.URLField(max_length=500, verbose_name="기본 URL")
    # UTM 4필드 상한 200 — analytics.LandingVisit/SignupAttribution 과 **동일해야 한다**.
    # 방문 쪽이 더 길면 그 값으로 들어온 유입에 대응하는 링크를 어드민에서 저장할 수 없어
    # (400) 영구히 '저장 안 된 링크(UTM)' 행으로 샌다. 한글 캠페인명 실사용 대비 상향.
    utm_source = models.CharField(max_length=200, blank=True, default="")
    utm_medium = models.CharField(max_length=200, blank=True, default="")
    utm_campaign = models.CharField(max_length=200, blank=True, default="")
    utm_content = models.CharField(max_length=200, blank=True, default="")
    url = models.URLField(
        max_length=2000,
        verbose_name="완성 URL",
        help_text=(
            "base_url + utm 파라미터 조합 (서버 계산, 기존 동일 utm 키는 교체). "
            "한글 UTM 은 퍼센트 인코딩으로 글자당 9자로 부풀기 때문에(1글자=3바이트×'%XX') "
            "2000자 상한을 넘을 수 있다 → 시리얼라이저가 400 으로 먼저 막는다"
        ),
    )
    channel = models.CharField(
        max_length=32,
        verbose_name="파생 채널 키",
        help_text="derive_channel(utm_source, utm_medium) 결과 — 대시보드 채널 키와 동일 어휘",
    )
    excluded_from_stats = models.BooleanField(
        default=False,
        verbose_name="집계 제외",
        help_text=(
            "채널별 성과·추이·퍼널에서 이 링크의 행을 빼고, 이 링크로 들어온 인원은 "
            "'기타' 행으로 흡수한다(인원을 총합에서 없애지는 않는다 — MKT-12). "
            "테스트/오생성/종료된 캠페인 링크가 표에 쌓이는 것을 정리하는 용도."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="marketing_channel_links",
        verbose_name="생성 관리자",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성 시각")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정 시각")

    class Meta:
        db_table = "marketing_channel_links"
        verbose_name = "마케팅 채널 링크"
        verbose_name_plural = "마케팅 채널 링크 목록"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["channel"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.channel})"


class AdminPreference(models.Model):
    """관리자 1인의 백오피스 **화면 설정** 보관함 (UI-1).

    모바일 하단 탭 구성처럼 "계정을 따라다녀야 하는" 프론트 상태를 담는다. 지금까지는
    브라우저 localStorage 에 있어서 기기를 바꾸면 사라졌고, 특히 안드로이드 WebView 셸은
    저장 공간이 부족하면 OS 가 데이터를 정리해 **예고 없이** 기본값으로 돌아갔다.

    ``data`` 는 **스키마를 검증하지 않는 자유 JSON** 이다. 프론트 화면 설정이라 키가 자주
    바뀌는데, 서버가 스키마를 들고 있으면 프론트가 키 하나 추가할 때마다 백엔드 배포를
    기다려야 한다. 값이 이상하면 프론트가 기본값으로 떨어지면 되고, 서버는 그 판단에
    관여하지 않는다. 대신 **크기 상한**(4KB)만 지킨다 — 검증하지 않는 칸은 언젠가
    쓰레기통이 되므로 무한히 자라지 못하게 막는 선은 필요하다.

    ⚠️ 여기에 **권한·요금제 같은 판정 값을 넣지 말 것.** 사용자가 PATCH 로 자기 값을 바꿀 수
    있으므로 신뢰 경계 밖이다. 순수 표시 설정만 담는다.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_preference",
        verbose_name="관리자",
    )
    data = models.JSONField(default=dict, blank=True, verbose_name="화면 설정(자유 JSON)")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="수정 시각")

    class Meta:
        db_table = "admin_preferences"
        verbose_name = "어드민 화면 설정"
        verbose_name_plural = "어드민 화면 설정 목록"

    def __str__(self) -> str:
        return f"{self.user_id} / {len(self.data)} keys"


class AdminMFADevice(models.Model):
    """관리자 1인의 TOTP(인증앱) 등록 — 계정당 최대 1개.

    시드는 **암호화 저장**한다. IG 토큰·토스 빌링키와 같은 등급의 비밀이고, 평문으로 두면
    DB 덤프 하나로 2단계가 통째로 무력화된다(CLAUDE.md §14).

    ``last_step`` 은 재사용(replay) 방지용이다. TOTP 는 같은 30초 창 안에서 같은 코드가
    계속 유효하므로, 어깨너머로 본 코드나 프록시에 남은 코드를 그 창 안에 다시 쓸 수 있다.
    성공한 스텝을 기록하고 **그보다 크지 않은 스텝을 거부**해서 1회용으로 만든다.

    등록 중인 시드는 ``_encrypted_pending_secret`` 에 **따로** 둔다. 재등록할 때 기존 시드를
    바로 덮으면, QR 만 띄우고 이탈한 순간 확인된 2단계가 사라져 계정이 비밀번호 하나로
    떨어진다. 확인(confirm)에 성공해야 pending 이 정본 자리로 옮겨간다.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_mfa_device",
        verbose_name="관리자",
    )
    _encrypted_secret = models.TextField(blank=True, default="", verbose_name="암호화된 TOTP 시드")
    secret = EncryptedTextField("_encrypted_secret")
    _encrypted_pending_secret = models.TextField(
        blank=True, default="", verbose_name="등록 중인 시드(확인 전)"
    )
    pending_secret = EncryptedTextField("_encrypted_pending_secret")
    confirmed_at = models.DateTimeField(
        null=True, blank=True, verbose_name="등록 확인 시각(첫 코드 검증 성공)"
    )
    last_step = models.BigIntegerField(
        default=0, verbose_name="마지막 성공 스텝", help_text="재사용 방지 — 이 값 이하 스텝은 거부"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="생성 시각")

    class Meta:
        db_table = "admin_mfa_devices"
        verbose_name = "어드민 2단계 인증"
        verbose_name_plural = "어드민 2단계 인증 목록"

    def __str__(self) -> str:
        state = "confirmed" if self.confirmed_at else "pending"
        return f"{self.user_id} / TOTP / {state}"

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None


class AdminBackupCode(models.Model):
    """TOTP 기기를 잃었을 때 쓰는 1회용 복구 코드.

    코드는 sha256 **해시만** 저장한다(발급 응답에서 1회 노출 후 서버는 원문을 모른다).
    엔트로피는 60비트(base32 12자)라 오프라인 대입이 불가능하므로 느린 해시가 필요 없고,
    해시 컬럼에 인덱스를 걸어 조회 1회로 검증한다.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_backup_codes",
        verbose_name="관리자",
    )
    code_hash = models.CharField(max_length=64, db_index=True, verbose_name="sha256(코드)")
    used_at = models.DateTimeField(null=True, blank=True, verbose_name="사용 시각")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="발급 시각")

    class Meta:
        db_table = "admin_backup_codes"
        verbose_name = "어드민 백업코드"
        verbose_name_plural = "어드민 백업코드 목록"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["user", "used_at"])]

    def __str__(self) -> str:
        return f"{self.user_id} / {'used' if self.used_at else 'live'}"


class AdminDevice(models.Model):
    """관리자가 로그인한 기기 1대 — 신뢰 여부와 원격 회수의 단위.

    **신뢰 기기 여부와 무관하게 모든 기기에 행을 만든다.** 신뢰한 것만 기록하면 임시
    기기(공용 PC 등)는 회수할 대상 자체가 없어져서, 세션을 끊을 방법이 비밀번호 변경밖에
    남지 않는다. ``revoked_at`` 이 찍히면 그 기기의 refresh 갱신이 즉시 막힌다.

    ``trusted_at`` = 이메일 코드로 승인해 등록한 시각. 신뢰 기기는 다음 로그인부터 이메일
    코드를 건너뛰고, refresh 수명도 길다(settings.ADMIN_REFRESH_TRUSTED_LIFETIME).
    **신뢰에는 만료를 두지 않는다** — 실질 상한은 refresh 수명이고, 그마저 지나면 비밀번호와
    TOTP 를 다시 통과해야 한다. 만료를 따로 두면 "왜 또 메일 코드가 오지"만 늘어난다.

    ``device_id`` 는 클라이언트가 만든 UUID 이며 비밀이 아니다(웹 localStorage / 앱
    SecureStore). 위조하면 남의 기기인 척할 수 있지만, 그것만으로는 아무것도 못 한다 —
    비밀번호와 TOTP 를 통과해야 토큰이 나오고, 위조가 우회하는 것은 이메일 승인 1단계뿐이다.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_devices",
        verbose_name="관리자",
    )
    device_id = models.CharField(max_length=64, verbose_name="클라이언트 기기 ID")
    label = models.CharField(max_length=100, blank=True, default="", verbose_name="기기 표시명")
    trusted_at = models.DateTimeField(
        null=True, blank=True, verbose_name="신뢰 등록 시각(null=비신뢰)"
    )
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name="마지막 접속 시각")
    last_seen_ip = models.GenericIPAddressField(
        null=True, blank=True, verbose_name="마지막 접속 IP"
    )
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="해제 시각")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="최초 로그인 시각")

    class Meta:
        db_table = "admin_devices"
        verbose_name = "어드민 기기"
        verbose_name_plural = "어드민 기기 목록"
        ordering = ["-last_seen_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "device_id"], name="uniq_admin_device_per_user")
        ]

    def __str__(self) -> str:
        return f"{self.user_id} / {self.device_id[:8]} / {'trusted' if self.is_trusted else 'new'}"

    @property
    def is_trusted(self) -> bool:
        return self.trusted_at is not None and self.revoked_at is None

    def touch(self, ip: str | None = None) -> None:
        """접속 흔적 갱신 — 보안 화면의 '마지막 접속' 열."""
        self.last_seen_at = timezone.now()
        if ip:
            self.last_seen_ip = ip
        self.save(update_fields=["last_seen_at", "last_seen_ip"])
