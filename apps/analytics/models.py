"""
제품 획득(acquisition) 분석 모델 — 랜딩 방문 + 가입 귀속.

`apps.pages` 의 PageView/BlockClick 이 "바이오링크 방문자" 분석이라면, 이 앱은
"우리 서비스로 유입된 잠재고객" 분석이다 (랜딩 방문 → 가입 전환 퍼널).

마케팅 대시보드 집계 시 반드시 지킬 지표 정의 (LandingVisit — append-only raw rows):
  - 방문자 (visitors) = 기간 내 COUNT(DISTINCT visitor_id)
  - 방문 (visits/sessions) = 기간 내 row count (JS 가 브라우저 세션당 1회 전송)
  - 채널별 방문→가입 전환율 = SignupAttribution(channel=X, 기간)
      ÷ DISTINCT visitor_id of LandingVisit(channel=X, 기간)

의도적으로 DB unique 제약을 두지 않는다 — 같은 방문자가 다른 날/다른 채널로 재방문하는
것이 정상 데이터이며, 일 단위 unique 제약은 채널 re-touch 데이터를 파괴하고 insert
레이스를 만든다. 중복 방어는 쓰기 시점 캐시(30분 burst dedup + 시간당 방문자별 캡)로 한다.
"""

from django.conf import settings
from django.db import models


class UAClass(models.TextChoices):
    """User-Agent 대분류. bot 은 기록 자체를 스킵하므로 DB 에는 거의 남지 않는다."""

    DESKTOP = "desktop", "데스크톱"
    MOBILE = "mobile", "모바일"
    TABLET = "tablet", "태블릿"
    BOT = "bot", "봇"
    UNKNOWN = "unknown", "알 수 없음"


class SignupKind(models.TextChoices):
    """가입 경로 종류."""

    EMAIL = "email", "이메일 가입"
    GOOGLE = "google", "Google 가입"


class LandingVisit(models.Model):
    """랜딩 페이지 방문 이벤트 (append-only raw row).

    랜딩 사이트(turnflow.link)의 트래킹 스니펫이 브라우저 세션당 1회
    ``POST /api/v1/track/visit/`` 로 전송한다. 원본 IP 는 절대 저장하지 않는다(해시만).
    보존: LANDING_VISIT_RETENTION_DAYS (기본 180일) — analytics.cleanup_landing_visits.
    """

    visitor_id = models.UUIDField(
        db_index=True,
        verbose_name="방문자 ID",
        help_text="클라이언트가 생성해 localStorage(tf_vid)에 영구 보관하는 UUID.",
    )
    utm_source = models.CharField(max_length=100, blank=True, default="", verbose_name="utm_source")
    utm_medium = models.CharField(max_length=100, blank=True, default="", verbose_name="utm_medium")
    utm_campaign = models.CharField(
        max_length=150, blank=True, default="", verbose_name="utm_campaign"
    )
    utm_content = models.CharField(
        max_length=150, blank=True, default="", verbose_name="utm_content"
    )
    referrer = models.CharField(
        max_length=500,
        blank=True,
        default="",
        verbose_name="리퍼러 URL",
        help_text="document.referrer 원문 (500자 절단). 채널 파생은 저장 시점에 수행.",
    )
    landing_path = models.CharField(
        max_length=300, blank=True, default="/", verbose_name="랜딩 경로"
    )
    channel = models.CharField(
        max_length=32,
        db_index=True,
        default="direct",
        verbose_name="유입 채널",
        help_text="channels.derive_channel() 로 저장 시점에 파생된 채널 키 (예: meta_ads).",
    )
    country = models.CharField(
        max_length=2,
        blank=True,
        default="",
        verbose_name="유입 국가",
        help_text="ISO 3166-1 alpha-2 (CF-IPCountry 헤더 우선).",
    )
    ip_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        verbose_name="IP 해시",
        help_text="SHA-256(IP). 개인정보 보호를 위해 원본 IP는 저장하지 않습니다.",
    )
    ua_class = models.CharField(
        max_length=10,
        choices=UAClass.choices,
        default=UAClass.UNKNOWN,
        verbose_name="UA 분류",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="방문 일시")

    class Meta:
        db_table = "analytics_landing_visit"
        verbose_name = "랜딩 방문"
        verbose_name_plural = "랜딩 방문 목록"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["channel", "created_at"]),
            models.Index(fields=["visitor_id", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.visitor_id} [{self.channel}] @{self.created_at:%Y-%m-%d %H:%M}"


class SignupAttribution(models.Model):
    """가입 1건당 1행 — 어떤 채널에서 온 가입인지 귀속.

    **모든 가입에 대해 행이 생성된다** (attribution 페이로드가 없어도):
      - 페이로드 있음 + utm/referrer 없음 → channel="direct" (URL 직접 입력)
      - 페이로드 자체가 없음 (구버전 프론트/API 가입) → channel="unknown"
        → 채널별 가입 수 합계 = 전체 가입 수 (깨끗한 퍼널 분모).

    referral(제휴코드) 채널 주의: 코드 사용(redemption)은 가입 *이후* 체험 시작 시점에
    일어나므로 (apps/billing/toss_flows.py `_consume_referral`) 가입 시점엔 저장 불가.
    마케팅 대시보드는 조회 시점 오버레이로 처리할 것:
      channel = "referral" if ReferralRedemption(user=...) 존재 else 저장된 channel.

    보존: TTL 없음 (사용자당 1행 업무 기록, user 삭제 시 CASCADE).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="signup_attribution",
        verbose_name="가입 사용자",
    )
    visitor_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="방문자 ID",
        help_text="LandingVisit 과 조인하는 키. 랜딩을 거치지 않은 가입은 NULL.",
    )
    utm_source = models.CharField(max_length=100, blank=True, default="", verbose_name="utm_source")
    utm_medium = models.CharField(max_length=100, blank=True, default="", verbose_name="utm_medium")
    utm_campaign = models.CharField(
        max_length=150, blank=True, default="", verbose_name="utm_campaign"
    )
    utm_content = models.CharField(
        max_length=150, blank=True, default="", verbose_name="utm_content"
    )
    referrer = models.CharField(max_length=500, blank=True, default="", verbose_name="리퍼러 URL")
    landing_path = models.CharField(
        max_length=300, blank=True, default="", verbose_name="랜딩 경로"
    )
    channel = models.CharField(
        max_length=32,
        db_index=True,
        default="unknown",
        verbose_name="유입 채널",
        help_text="channels.derive_channel() 파생 키. referral 은 조회 시점 오버레이(모델 docstring).",
    )
    signup_kind = models.CharField(
        max_length=10, choices=SignupKind.choices, verbose_name="가입 경로"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="가입 일시")

    class Meta:
        db_table = "analytics_signup_attribution"
        verbose_name = "가입 귀속"
        verbose_name_plural = "가입 귀속 목록"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["channel", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"user={self.user_id} [{self.channel}] {self.signup_kind}"
