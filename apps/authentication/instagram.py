"""인스타그램 로그인/가입 — OAuth 교환·프로필 조회·자리표시 이메일.

``apps/authentication/kakao.py`` 와 같은 자리의 모듈이다: HTTP 껍데기(뷰)와 분리해
"외부 사업자에게서 신원을 받아 오는 일"만 담당한다.

⭐ **연동(integrations)과 무엇이 다른가**
   토큰을 받아 오는 절차 자체는 ``integrations.InstagramOAuthService`` 와 **완전히 같고,
   그 클래스를 그대로 재사용한다**(엔드포인트·스코프·앱 키를 두 벌 들면 한쪽만 바뀌었을 때
   조용히 갈린다). 다른 것은 두 가지뿐이다:
     1. ``redirect_uri`` 가 백엔드 콜백이 아니라 **프론트 라우트**다
        (가입 화면에서 시작해 가입 화면으로 돌아와야 하므로).
     2. 받아 온 IG 계정으로 **사람을 찾거나 만든다** — 워크스페이스가 아직 없다.

⚠️ **Instagram Business Login 은 이메일을 주지 않는다.** 스코프에 이메일이 없고, 추가할
   방법도 없다(Facebook Login 이라면 가능하지만 그건 별도 심사 + 한 달 이상이고, 회의에서
   보류했다). 우리 로그인 키는 ``User.email`` 이라 비워 둘 수 없으므로 **자리표시 이메일**을
   만든다. 그 계정에는 메일을 보낼 수 없고(sender 게이트가 막는다), 프론트는
   ``user.email_is_placeholder`` 로 "알림 받을 이메일을 등록해 주세요"를 띄운다.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from apps.integrations.services import InstagramOAuthService, MockInstagramProvider

from .models import PLACEHOLDER_EMAIL_DOMAIN, InstagramLoginState

logger = logging.getLogger(__name__)

STATE_TTL_MINUTES = 10
# 만료된 state 를 얼마나 오래 남겨 둘지 — 디버깅용 짧은 꼬리. start 때 같이 청소한다
# (행이 작고 생성 빈도도 낮아 별도 beat 를 만들 이유가 없다).
STATE_SWEEP_AFTER_HOURS = 24


class InstagramAuthError(Exception):
    """뷰가 그대로 응답으로 바꾸는 인스타 로그인 오류."""

    def __init__(self, message: str, code: str, status: int = 400):
        self.message = message
        self.code = code
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class InstagramProfile:
    """인스타에서 받아 온 신원 + 토큰 (연동 행을 만들 때 그대로 쓴다)."""

    user_id: str
    username: str
    name: str
    account_type: str
    access_token: str
    token_expires_at: object
    profile_picture_url: str = ""


def is_configured() -> bool:
    """서버에 인스타 앱 자격증명이 있는가. 없으면 503 으로 빠르게 알린다."""
    if MockInstagramProvider.is_mock_mode():
        return True
    return bool(
        InstagramOAuthService.get_instagram_app_id()
        and InstagramOAuthService.get_instagram_app_secret()
    )


def login_enabled() -> bool:
    """인스타 로그인 킬스위치.

    ⚠️ 기본 False 인 이유가 정책적이다 — 2026-09-10 내부 논의에서 "한 번 넣으면 그걸로
    로그인한 사람이 한 명이라도 있는 순간 다시는 못 뺀다"는 지적이 나왔고, 테스트 URL 에서
    먼저 써 보고 결정하기로 했다. 그래서 **코드는 완성해 두고 스위치는 꺼 둔다**.
    """
    return bool(getattr(settings, "INSTAGRAM_LOGIN_ENABLED", False))


def default_redirect_uri() -> str:
    return str(getattr(settings, "INSTAGRAM_LOGIN_REDIRECT_URI", "") or "")


def resolve_redirect_uri(requested: str) -> str:
    """쓸 ``redirect_uri`` 를 정한다 — **허용목록 안의 origin 만**.

    클라이언트가 보낸 값을 그대로 쓰면 오픈 리다이렉트가 된다(공격자 페이지로 인가 코드가
    간다). ``integrations.oauth_return`` 의 origin 완전일치 검증을 그대로 재사용한다 —
    IG 연동의 ``return_to`` 가 이미 같은 문제를 푼 곳이고, 허용 origin 목록이 두 벌로
    갈리면 한쪽만 조여진다.
    """
    from apps.integrations import oauth_return

    requested = (requested or "").strip()
    if not requested:
        fallback = default_redirect_uri()
        if not fallback:
            raise InstagramAuthError(
                "인스타그램 로그인 복귀 주소가 서버에 설정되어 있지 않습니다.",
                "INSTAGRAM_REDIRECT_NOT_CONFIGURED",
                503,
            )
        return fallback

    validated, reason = oauth_return.validate_return_to(requested)
    if not validated:
        raise InstagramAuthError(
            "허용되지 않은 redirect_uri 입니다. 프론트 origin 과 정확히 일치해야 합니다.",
            "INSTAGRAM_INVALID_REDIRECT_URI",
            400,
        )
    # validate_return_to 는 쿼리스트링을 보존한다. OAuth redirect_uri 는 등록값과 **글자
    # 그대로** 같아야 하므로 그대로 돌려준다 — 여기서 손대면 Meta 가 거부한다.
    _ = reason
    return validated


def create_state(redirect_uri: str) -> InstagramLoginState:
    """1회용 state 발급 + 오래된 행 청소."""
    try:
        InstagramLoginState.objects.filter(
            expires_at__lt=timezone.now() - timedelta(hours=STATE_SWEEP_AFTER_HOURS)
        ).delete()
    except Exception:  # noqa: BLE001
        logger.warning("instagram login state sweep failed", exc_info=True)

    return InstagramLoginState.objects.create(
        state=secrets.token_urlsafe(32),
        redirect_uri=redirect_uri,
        expires_at=timezone.now() + timedelta(minutes=STATE_TTL_MINUTES),
    )


def consume_state(state: str) -> InstagramLoginState:
    """state 검증 + 소비. 잘못됐으면 ``InstagramAuthError``.

    ⚠️ 여기서 ``redirect_uri`` 를 **저장값으로** 돌려주는 게 핵심이다. 클라이언트가
       교환 요청에 다시 실어 보낸 값을 쓰면, authorize 때와 다른 값이 와서 Meta 가 거부하는
       사고(디버깅이 어려운 400)가 난다.
    """
    state = (state or "").strip()
    if not state:
        raise InstagramAuthError("state 가 없습니다.", "INSTAGRAM_STATE_MISSING", 400)

    row = InstagramLoginState.objects.filter(state=state).first()
    if row is None:
        raise InstagramAuthError(
            "인증 정보가 만료되었거나 올바르지 않습니다. 다시 시도해 주세요.",
            "INSTAGRAM_STATE_INVALID",
            400,
        )
    if row.consumed_at is not None:
        raise InstagramAuthError(
            "이미 처리된 로그인 요청입니다. 다시 시도해 주세요.",
            "INSTAGRAM_STATE_USED",
            400,
        )
    if row.is_expired:
        raise InstagramAuthError(
            "인증 정보가 만료되었습니다. 다시 시도해 주세요.",
            "INSTAGRAM_STATE_EXPIRED",
            400,
        )

    row.consumed_at = timezone.now()
    row.save(update_fields=["consumed_at"])
    return row


def authorization_url(redirect_uri: str, state: str) -> str:
    if MockInstagramProvider.is_mock_mode():
        return MockInstagramProvider.generate_mock_authorization_url(redirect_uri, state)
    return InstagramOAuthService.get_authorization_url(redirect_uri, state)


def exchange_code(code: str, redirect_uri: str) -> InstagramProfile:
    """인가 코드 → 장기 토큰 + 프로필. 실패는 전부 ``InstagramAuthError``.

    Mock 모드(``INSTAGRAM_MOCK_MODE``)에서도 **같은 반환 모양**을 준다 — 운영 승인 전에도
    로그인 플로우 전체를 검증할 수 있어야 한다(CLAUDE.md §5-7).
    """
    code = (code or "").strip()
    if not code:
        raise InstagramAuthError("인가 코드가 없습니다.", "INSTAGRAM_CODE_MISSING", 400)

    if code.startswith("mock_code_") or MockInstagramProvider.is_mock_mode():
        token = MockInstagramProvider.exchange_mock_code_for_token(code)
        long_lived = MockInstagramProvider.get_mock_long_lived_token(token["access_token"])
        info = MockInstagramProvider.get_mock_account_info(long_lived["access_token"])
        expires_in = long_lived.get("expires_in", 5184000)
        return InstagramProfile(
            user_id=str(info.get("user_id") or info.get("id") or token.get("user_id") or ""),
            username=info.get("username", ""),
            name=info.get("name", "") or info.get("username", ""),
            account_type=info.get("account_type", "BUSINESS") or "BUSINESS",
            access_token=long_lived["access_token"],
            token_expires_at=timezone.now() + timedelta(seconds=expires_in),
            profile_picture_url=info.get("profile_picture_url", "") or "",
        )

    try:
        token = InstagramOAuthService.exchange_code_for_token(code, redirect_uri)
    except requests.HTTPError as exc:
        # Meta 는 실패 사유를 **본문 JSON**으로 준다. raise_for_status 가 본문을 버리므로
        # 여기서 남겨야 "코드 만료"와 "redirect_uri 불일치"를 구분할 수 있다.
        body = ""
        try:
            body = (exc.response.text or "")[:300]
        except Exception:  # noqa: BLE001
            pass
        logger.warning("instagram login: code 교환 실패 body=%s", body)
        raise InstagramAuthError(
            "인스타그램 인증에 실패했습니다. 다시 시도해 주세요.",
            "INSTAGRAM_CODE_INVALID",
            400,
        ) from None
    except requests.RequestException:
        raise InstagramAuthError(
            "인스타그램 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            "INSTAGRAM_UNAVAILABLE",
            502,
        ) from None

    short_lived = token.get("access_token", "")
    if not short_lived:
        raise InstagramAuthError(
            "인스타그램 인증에 실패했습니다. 다시 시도해 주세요.",
            "INSTAGRAM_CODE_INVALID",
            400,
        )
    token_user_id = str(token.get("user_id", "") or "")
    # 토큰·시크릿은 절대 로깅하지 않는다. 권한 목록만 남겨 "권한 미부여 vs 계정타입" 구분.
    logger.info(
        "instagram login: short-lived OK user_id=%s permissions=%r",
        token_user_id,
        token.get("permissions"),
    )

    try:
        long_lived = InstagramOAuthService.get_long_lived_token(short_lived)
        access_token = long_lived["access_token"]
        info = InstagramOAuthService.get_account_info(access_token)
    except requests.RequestException:
        raise InstagramAuthError(
            "인스타그램 계정 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.",
            "INSTAGRAM_API_ERROR",
            502,
        ) from None
    except Exception:
        logger.exception("instagram login: 계정 정보 조회 실패")
        raise InstagramAuthError(
            "인스타그램 계정 정보를 가져오지 못했습니다.",
            "INSTAGRAM_API_ERROR",
            502,
        ) from None

    user_id = str(info.get("user_id") or token_user_id or info.get("id") or "")
    if not user_id:
        raise InstagramAuthError(
            "인스타그램 계정 식별자를 확인하지 못했습니다.",
            "INSTAGRAM_API_ERROR",
            502,
        )

    expires_in = long_lived.get("expires_in", 5184000)  # 기본 60일
    return InstagramProfile(
        user_id=user_id,
        username=info.get("username", "") or "",
        name=info.get("name", "") or info.get("username", "") or "",
        account_type=info.get("account_type", "BUSINESS") or "BUSINESS",
        access_token=access_token,
        token_expires_at=timezone.now() + timedelta(seconds=expires_in),
        profile_picture_url=info.get("profile_picture_url", "") or "",
    )


def placeholder_email(instagram_user_id: str) -> str:
    """IG 가입자의 자리표시 이메일.

    IG 사용자 ID 로 만든다 — ``username`` 으로 만들면 사용자가 핸들을 바꾸는 순간
    같은 사람의 이메일이 달라지고, 더 나쁘게는 **남이 그 핸들을 이어받아** 같은 주소를
    쓰게 된다(unique 충돌 또는 계정 혼선).
    """
    return f"ig_{instagram_user_id}@{PLACEHOLDER_EMAIL_DOMAIN}"
