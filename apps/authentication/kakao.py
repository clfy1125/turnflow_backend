"""
카카오 로그인 — Kakao REST API 클라이언트.

이 모듈은 **카카오와 말하는 부분만** 담당한다. 사용자 조회/생성·JWT 발급 같은 우리 쪽
판정은 ``views.KakaoLoginView`` 가 한다 (구글은 뷰가 google-auth 라이브러리를 직접 부르지만,
카카오는 검증용 공식 파이썬 SDK 가 없어 우리가 HTTP 를 직접 친다).

⭐ 흐름이 구글과 다른 점 — 왜 이렇게 생겼는지
--------------------------------------------------------------------------
구글은 프론트가 받은 **ID Token** 하나를 서버가 오프라인 검증(서명+aud)하면 끝이다.
카카오는 ID Token(OIDC)을 기본으로 주지 않으므로 **인가 코드(authorization code)** 를
서버가 카카오 토큰 엔드포인트에서 액세스 토큰으로 바꾼 뒤, 사용자 정보를 조회해야 한다.

그래서 인가 코드 교환이 **1순위 경로**다:
  · 클라이언트 시크릿이 서버에만 있다 (콘솔에서 '활성화 ON' 이라 교환 시 필수다).
  · 액세스 토큰이 브라우저에 노출되지 않는다.

액세스 토큰을 직접 받는 경로(2순위)도 열어 둔다 — 네이티브 앱(카카오 SDK)이 코드가 아니라
토큰을 손에 쥐기 때문이다. 다만 **남의 앱 토큰으로 우리 계정을 열 수 있으면 안 되므로**
(confused deputy) ``/v1/user/access_token_info`` 로 ``app_id`` 가 우리 앱인지 반드시 확인한다.
구글의 ``aud`` 검사와 정확히 같은 역할이며, 이 검사를 빼면 **아무 카카오 앱 개발자나
남의 계정으로 로그인할 수 있다**. 지우지 말 것.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

KAUTH_BASE = "https://kauth.kakao.com"
KAPI_BASE = "https://kapi.kakao.com"

# 카카오 응답이 느려도 로그인 요청이 워커를 오래 물면 안 된다. 외부 호출 2회(교환+조회)라
# 합쳐서 최대 ~20초. (프로젝트 지침 12 — 외부 API 는 타임아웃 필수)
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class KakaoError(Exception):
    """카카오 연동 실패. ``code`` 는 프론트가 분기할 수 있는 머신 키."""

    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class KakaoProfile:
    """``/v2/user/me`` 에서 우리가 실제로 쓰는 값만 추린 것.

    원본 응답을 그대로 들고 다니지 않는 이유: 카카오는 동의받지 않은 필드도 키 자체는
    내려보내므로(``email_needs_agreement`` 등) 호출부가 매번 같은 방어 코드를 쓰게 된다.
    여기서 한 번만 해석한다.
    """

    kakao_id: str
    email: str
    email_verified: bool
    nickname: str


def _post_form(url: str, data: dict) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            )
    except httpx.HTTPError as exc:
        logger.warning("kakao_login: token request transport error: %s", exc)
        raise KakaoError(
            "KAKAO_UNAVAILABLE",
            "카카오 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            status=502,
        ) from exc

    if resp.status_code != 200:
        # ⚠️ 카카오 오류 본문에는 우리가 보낸 파라미터가 되비쳐 나온다. code/secret 이
        #    섞여 나올 수 있으므로 **본문 전체를 로그에 찍지 않는다** (지침 14 — 키 평문 금지).
        body = _safe_error_body(resp)
        logger.warning(
            "kakao_login: token exchange failed status=%s error=%s desc=%s",
            resp.status_code,
            body.get("error"),
            body.get("error_description"),
        )
        raise KakaoError(
            "KAKAO_CODE_INVALID",
            "카카오 인가 코드가 유효하지 않습니다. 로그인을 다시 시도해 주세요.",
        )
    return resp.json()


def _safe_error_body(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    # error / error_description 만 통과시킨다 — 나머지는 우리가 보낸 값의 반향일 수 있다.
    return {k: data.get(k) for k in ("error", "error_description", "error_code")}


def exchange_code(code: str, redirect_uri: str) -> str:
    """인가 코드 → 액세스 토큰.

    ``redirect_uri`` 는 authorize 때 쓴 값과 **완전히 같아야** 카카오가 받아 준다
    (OAuth 규격). 값 자체는 카카오가 콘솔 등록 목록과 대조하므로 우리가 허용목록을
    따로 들 필요는 없다 — 미등록 URI 는 authorize 단계에서 이미 막힌다.
    """
    if not settings.KAKAO_REST_API_KEY:
        raise KakaoError(
            "KAKAO_NOT_CONFIGURED",
            "카카오 로그인이 서버에 설정되어 있지 않습니다.",
            status=503,
        )

    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.KAKAO_REST_API_KEY,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    # 콘솔에서 '클라이언트 시크릿 활성화 ON' 이면 **필수**다. 빠지면 카카오가 KOE010 으로
    # 거절한다. 반대로 OFF 인 앱에 실어 보내도 무해하므로 값이 있으면 항상 보낸다.
    if settings.KAKAO_CLIENT_SECRET:
        payload["client_secret"] = settings.KAKAO_CLIENT_SECRET

    data = _post_form(f"{KAUTH_BASE}/oauth/token", payload)
    token = data.get("access_token")
    if not token:
        raise KakaoError("KAKAO_CODE_INVALID", "카카오에서 액세스 토큰을 받지 못했습니다.")
    return token


def assert_token_belongs_to_us(access_token: str) -> None:
    """이 액세스 토큰이 **우리 앱** 것인지 확인한다 (구글 ``aud`` 검사와 같은 역할).

    프론트/네이티브가 토큰을 직접 보내는 경로에서만 필요하다. 인가 코드 교환 경로는
    우리가 우리 client_id 로 발급받은 토큰이라 이 검사가 의미 없다.
    """
    app_id = settings.KAKAO_APP_ID
    if not app_id:
        # 앱 ID 를 모르면 검증이 불가능하다 → **통과시키지 않는다**. 여기서 fail-open 하면
        # 설정 누락 하나로 남의 앱 토큰이 그대로 먹힌다.
        raise KakaoError(
            "KAKAO_NOT_CONFIGURED",
            "카카오 로그인이 서버에 설정되어 있지 않습니다.",
            status=503,
        )

    info = _get_json(f"{KAPI_BASE}/v1/user/access_token_info", access_token)
    if str(info.get("app_id")) != str(app_id):
        logger.warning(
            "kakao_login: token app_id mismatch (got=%s expected=%s)",
            info.get("app_id"),
            app_id,
        )
        raise KakaoError(
            "KAKAO_TOKEN_FOREIGN_APP",
            "다른 앱에서 발급된 카카오 토큰입니다.",
            status=403,
        )


def _get_json(url: str, access_token: str, params: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("kakao_login: kapi transport error url=%s err=%s", url, exc)
        raise KakaoError(
            "KAKAO_UNAVAILABLE",
            "카카오 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            status=502,
        ) from exc

    if resp.status_code == 401:
        raise KakaoError("KAKAO_TOKEN_INVALID", "카카오 액세스 토큰이 유효하지 않습니다.")
    if resp.status_code != 200:
        logger.warning(
            "kakao_login: kapi error url=%s status=%s body=%s",
            url,
            resp.status_code,
            _safe_error_body(resp),
        )
        raise KakaoError(
            "KAKAO_UNAVAILABLE", "카카오 사용자 정보를 가져오지 못했습니다.", status=502
        )
    return resp.json()


def fetch_profile(access_token: str) -> KakaoProfile:
    """``/v2/user/me`` 로 회원번호·이메일·닉네임을 가져온다.

    이메일은 콘솔에서 **필수 동의 + '값 없으면 카카오계정에서 수집'** 으로 설정해 두었으므로
    정상 경로에서는 항상 온다. 그래도 없을 때를 처리하는 이유:
      · 동의항목 설정을 나중에 누가 바꿀 수 있다 (설정은 코드 밖에 있다)
      · 이미 예전 설정으로 연결한 사용자가 재로그인하면 **기존 동의 범위**로 온다
        (필수로 바꿔도 기존 연결 사용자는 자동으로 다시 묻지 않는다)
    이 경우 우리 계정 키(email)가 없으니 로그인시킬 수 없다 → 명시적 오류로 되돌린다.
    """
    data = _get_json(
        f"{KAPI_BASE}/v2/user/me",
        access_token,
        # 필요한 필드만 요청한다 (개인정보 최소 수집 — 지침 5-6).
        params={"property_keys": '["kakao_account.email","kakao_account.profile"]'},
    )

    kakao_id = data.get("id")
    if kakao_id is None:
        raise KakaoError("KAKAO_TOKEN_INVALID", "카카오 사용자 정보를 가져오지 못했습니다.")

    account = data.get("kakao_account") or {}
    profile = account.get("profile") or {}

    email = (account.get("email") or "").strip().lower()
    if not email or account.get("email_needs_agreement"):
        raise KakaoError(
            "KAKAO_EMAIL_REQUIRED",
            "카카오 계정의 이메일 제공에 동의해야 로그인할 수 있습니다. "
            "카카오 동의 화면에서 이메일 제공에 동의해 주세요.",
        )

    return KakaoProfile(
        kakao_id=str(kakao_id),
        email=email,
        # 카카오가 "이 이메일은 본인 확인됨" 이라고 알려주는 값. 구글의 email_verified 와
        # 같은 의미이고, 같은 방식으로 **기존 계정 자동 연결의 게이트**로만 쓴다.
        email_verified=bool(account.get("is_email_verified")),
        nickname=(profile.get("nickname") or "").strip(),
    )


def resolve_profile(
    *, code: str = "", redirect_uri: str = "", access_token: str = ""
) -> KakaoProfile:
    """뷰가 부르는 단일 진입점 — 두 경로(코드/토큰)를 여기서 합류시킨다.

    합류점을 하나로 두는 이유: ``assert_token_belongs_to_us`` 를 **토큰 경로에서만**
    부르고 코드 경로에서는 건너뛴다는 규칙이 두 군데로 흩어지면, 한쪽이 검증 없이
    통과하는 구멍이 조용히 생긴다.
    """
    if code:
        access_token = exchange_code(code, redirect_uri)
    else:
        assert_token_belongs_to_us(access_token)
    return fetch_profile(access_token)
