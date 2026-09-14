"""
카카오 로그인 view.

`views.py` 가 아니라 별도 모듈인 이유는 `deletion_views.py` 와 같다 — views.py 가 이미
1,100줄을 넘겨 소셜 로그인 하나를 더 얹으면 읽기 어려워진다.

⭐ 구조는 `views.GoogleLoginView` 와 **의도적으로 같게** 맞춰 두었다:
응답 형태(user/is_new_user/tokens), 탈퇴 유예 409, 미확인 이메일의 기존 계정 연결 거부,
attribution 저장과 Meta CAPI 발사 조건(`if created:`)까지 동일하다.
두 소셜 로그인이 다르게 동작하면 프론트가 분기를 두 벌 들게 되고, 한쪽에만 버그가 남는다.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import generics, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .kakao import KakaoError, resolve_profile
from .serializers import KakaoAuthResponseSerializer, KakaoLoginSerializer, UserSerializer
from .tokens import AppRefreshToken

User = get_user_model()
logger = logging.getLogger(__name__)


def _error(message: str, code: str, http_status: int) -> Response:
    """정책 거절 응답 — **두 포맷을 동시에** 낸다.

    ⚠️ 이 헬퍼는 DRF 예외 핸들러를 거치지 않으므로, 그냥 만들면 `{detail}` 단독이 되어
    §6 의 표준 envelope 를 위반한다. 같은 엔드포인트의 시리얼라이저 ValidationError 는
    핸들러를 타서 envelope 로 나가므로 **한 URL 이 두 포맷을 내게 된다** — billing
    `toss_views._flow_error_response` 가 실제로 밟았던 함정이다(CLAUDE.md 참고).

    그래서 envelope 를 싣되 `detail`/`code` 도 남긴다. `detail` 은 GoogleLoginView 와
    같은 자리라 프론트가 두 소셜 로그인을 한 코드로 처리할 수 있다 — 지우지 말 것.
    """
    return Response(
        {
            "detail": message,
            "code": code,
            "success": False,
            "error": {"code": http_status, "message": message, "details": {"code": code}},
        },
        status=http_status,
    )


class KakaoLoginView(generics.GenericAPIView):
    """카카오 로그인 endpoint — 인가 코드(권장) 또는 액세스 토큰(네이티브)을 받아 JWT 를 발급한다."""

    permission_classes = [AllowAny]
    serializer_class = KakaoLoginSerializer
    # H-1 과 동일 — 무인증 엔드포인트라 IP 기준으로 조인다.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_kakao"

    @extend_schema(
        tags=["Authentication"],
        summary="카카오 소셜 로그인",
        description="""
카카오 로그인으로 받은 **인가 코드(code)** 를 전송하면, 백엔드가 카카오에서 토큰을 교환하고
사용자 정보를 조회한 뒤 유저 조회/생성 후 우리 서비스의 JWT를 발급합니다.

### 사용 시나리오 / 타이밍
1. 프론트가 `https://kauth.kakao.com/oauth/authorize?client_id={REST_API_KEY}&redirect_uri={URI}&response_type=code` 로 이동
2. 사용자가 동의 → 콘솔에 등록된 `redirect_uri` 로 `?code=...` 를 달고 되돌아옴
3. 프론트가 `POST /api/v1/auth/kakao/` 에 `{ "code": "...", "redirect_uri": "..." }` 전송
4. 백엔드가 카카오 토큰 교환(client_secret 포함) → `/v2/user/me` 조회 → JWT 발급

`redirect_uri` 는 2단계에서 쓴 값과 **완전히 동일**해야 합니다(한 글자만 달라도 카카오가 거절).
`code` 는 **1회용**이라 같은 값으로 재요청하면 400 `KAKAO_CODE_INVALID` 입니다 — React
StrictMode 의 이중 실행에 주의하세요.

### 네이티브(카카오 SDK) 경로
`code` 대신 `{ "access_token": "..." }` 를 보낼 수 있습니다. 이 경우 백엔드가 그 토큰이
**우리 앱에서 발급된 것인지** 검증한 뒤 진행합니다. 둘을 동시에 보내면 400 입니다.

### 인증 요구사항
- 인증 불필요(AllowAny). IP 기준 스로틀 20/min.

### 비즈니스 로직
- **신규 유저**: 카카오 이메일로 계정 자동 생성(비밀번호 없음 → 이메일+비밀번호 로그인 불가)
- **기존 유저**: 카카오 회원번호(우선) → 이메일 순으로 매칭. 이메일로 매칭되면 그 시점에
  회원번호를 저장해, 이후 사용자가 카카오 이메일을 바꿔도 같은 계정으로 이어집니다.
- 이메일은 카카오 콘솔에서 **필수 동의**입니다. 그럼에도 이메일이 오지 않으면 400
  `KAKAO_EMAIL_REQUIRED` 로 거절합니다(이메일이 우리 계정 키입니다).
- 카카오가 이메일 본인확인을 못 해준 상태(`is_email_verified=false`)에서 **그 이메일의
  기존 계정이 있으면** 자동 연결하지 않고 403 으로 막습니다(계정 탈취 방지).

### 주의사항
- 가입/로그인이 같은 엔드포인트이므로 전환 이벤트는 응답의 `is_new_user` 로 분기하세요.
- 탈퇴 유예 중인 계정은 409 로 막히며 JWT 가 발급되지 않습니다.
""",
        request=KakaoLoginSerializer,
        examples=[
            OpenApiExample(
                "웹 · 인가 코드 (권장)",
                value={
                    "code": "AbCdEf_카카오_인가_코드",
                    "redirect_uri": "https://app.turnflow.link/auth/kakao/callback",
                    "marketing_opt_in": False,
                    "attribution": {"utm_source": "meta", "utm_medium": "cpc"},
                },
                request_only=True,
            ),
            OpenApiExample(
                "네이티브 · 액세스 토큰",
                value={"access_token": "카카오_액세스_토큰"},
                request_only=True,
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=KakaoAuthResponseSerializer,
                description="로그인 성공 — JWT 발급",
                examples=[
                    OpenApiExample(
                        "성공",
                        value={
                            "user": {
                                "id": 42,
                                "email": "user@kakao.com",
                                "full_name": "홍길동",
                                "is_email_verified": True,
                                "marketing_opt_in": False,
                            },
                            "is_new_user": False,
                            "tokens": {"refresh": "eyJhbGciOi...", "access": "eyJhbGciOi..."},
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description=(
                    "요청 형식 오류 또는 카카오 검증 실패. "
                    "두 종류가 있습니다 — 시리얼라이저 검증 오류는 표준 envelope 만, "
                    "카카오 거절은 envelope + `detail`/`code` 를 함께 담습니다. "
                    "프론트는 `error.details.code`(또는 최상위 `code`)로 분기하세요."
                ),
                examples=[
                    OpenApiExample(
                        "요청 형식 오류 (시리얼라이저 검증 — 표준 envelope 만)",
                        value={
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "code 또는 access_token 중 하나는 반드시 필요합니다.",
                                "details": {
                                    "code": ["code 또는 access_token 중 하나는 반드시 필요합니다."]
                                },
                            },
                        },
                    ),
                    OpenApiExample(
                        "인가 코드 무효/만료/재사용 (카카오 거절 — detail/code 동봉)",
                        value={
                            "detail": "카카오 인가 코드가 유효하지 않습니다. 로그인을 다시 시도해 주세요.",
                            "code": "KAKAO_CODE_INVALID",
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "카카오 인가 코드가 유효하지 않습니다. 로그인을 다시 시도해 주세요.",
                                "details": {"code": "KAKAO_CODE_INVALID"},
                            },
                        },
                    ),
                    OpenApiExample(
                        "이메일 미동의",
                        value={
                            "detail": "카카오 계정의 이메일 제공에 동의해야 로그인할 수 있습니다. "
                            "카카오 동의 화면에서 이메일 제공에 동의해 주세요.",
                            "code": "KAKAO_EMAIL_REQUIRED",
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "카카오 계정의 이메일 제공에 동의해야 로그인할 수 있습니다.",
                                "details": {"code": "KAKAO_EMAIL_REQUIRED"},
                            },
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="해당 없음 — 이 엔드포인트는 인증이 필요 없습니다."),
            403: OpenApiResponse(
                description="다른 앱의 토큰이거나, 미확인 이메일로 기존 계정 연결을 시도한 경우",
                examples=[
                    OpenApiExample(
                        "기존 계정 자동 연결 거부",
                        value={
                            "detail": "이 이메일로 가입된 계정이 이미 있습니다. 카카오에서 이메일 "
                            "소유 확인이 완료되지 않아 자동 연결할 수 없습니다. "
                            "비밀번호로 로그인하거나 이메일 인증을 완료해 주세요.",
                            "code": "KAKAO_EMAIL_UNVERIFIED",
                        },
                    ),
                    OpenApiExample(
                        "다른 앱에서 발급된 토큰",
                        value={
                            "detail": "다른 앱에서 발급된 카카오 토큰입니다.",
                            "code": "KAKAO_TOKEN_FOREIGN_APP",
                            "success": False,
                            "error": {
                                "code": 403,
                                "message": "다른 앱에서 발급된 카카오 토큰입니다.",
                                "details": {"code": "KAKAO_TOKEN_FOREIGN_APP"},
                            },
                        },
                    ),
                ],
            ),
            404: OpenApiResponse(
                description="해당 없음 — 이 엔드포인트는 리소스를 조회하지 않습니다."
            ),
            409: OpenApiResponse(
                description="탈퇴 유예 중인 계정 — 복구 후 이용 가능",
                examples=[
                    OpenApiExample(
                        "탈퇴 접수됨",
                        value={
                            "success": False,
                            "error": {
                                "code": 409,
                                "message": "이 계정은 회원탈퇴가 접수되어 이용이 중단되었습니다. "
                                "탈퇴 접수 메일의 '탈퇴 취소' 링크로 복구할 수 있습니다.",
                                "details": {
                                    "code": "account_deletion_pending",
                                    "purge_at": "2026-09-17T00:00:00+00:00",
                                    "support_email": "contact@turnflow.link",
                                },
                            },
                        },
                    )
                ],
            ),
            429: OpenApiResponse(description="요청이 너무 잦습니다 (IP 기준 20/min)."),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요."),
            502: OpenApiResponse(
                description="카카오 서버 연결/응답 실패 — 재시도 가능",
                examples=[
                    OpenApiExample(
                        "카카오 장애",
                        value={
                            "detail": "카카오 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                            "code": "KAKAO_UNAVAILABLE",
                        },
                    )
                ],
            ),
            503: OpenApiResponse(
                description="서버에 카카오 자격증명이 설정되지 않음(운영 설정 누락).",
                examples=[
                    OpenApiExample(
                        "미설정",
                        value={
                            "detail": "카카오 로그인이 서버에 설정되어 있지 않습니다.",
                            "code": "KAKAO_NOT_CONFIGURED",
                        },
                    )
                ],
            ),
        },
    )
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            profile = resolve_profile(
                code=data.get("code", ""),
                redirect_uri=(data.get("redirect_uri") or "").strip(),
                access_token=data.get("access_token", ""),
            )
        except KakaoError as exc:
            return _error(exc.message, exc.code, exc.status)

        user, created, conflict = self._match_or_create_user(profile, data)
        if conflict is not None:
            return conflict

        if not created and user.is_pending_deletion:
            # 여기서 막지 않으면 is_active=False 계정에 JWT 가 나간다(로그인 우회).
            # 자동 복구도 하지 않는다 — 탈퇴 취소는 사용자가 의식적으로 선택해야 한다.
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": status.HTTP_409_CONFLICT,
                        "message": (
                            "이 계정은 회원탈퇴가 접수되어 이용이 중단되었습니다. "
                            "탈퇴 접수 메일의 '탈퇴 취소' 링크로 복구할 수 있습니다."
                        ),
                        "details": {
                            "code": "account_deletion_pending",
                            "purge_at": (
                                user.deletion_scheduled_at.isoformat()
                                if user.deletion_scheduled_at
                                else None
                            ),
                            "support_email": settings.SUPPORT_EMAIL,
                        },
                    },
                },
                status=status.HTTP_409_CONFLICT,
            )

        if created:
            self._finalize_signup(user, data, request)
        else:
            self._sync_existing_user(user, profile)

        refresh = AppRefreshToken.for_user(user)
        return Response(
            {
                "user": UserSerializer(user).data,
                # ⭐ 가입/로그인이 같은 엔드포인트라 프론트가 구별할 방법이 이 값뿐이다.
                #    date_joined 로 추정하면 CompleteRegistration 이 누락/중복 발사된다.
                "is_new_user": created,
                "tokens": {"refresh": str(refresh), "access": str(refresh.access_token)},
            },
            status=status.HTTP_200_OK,
        )

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────
    def _match_or_create_user(self, profile, data):
        """(user, created, error_response) 를 돌려준다.

        매칭 순서가 중요하다:
          ① 카카오 회원번호 — 이메일이 바뀌어도 같은 사람으로 이어진다(모델 주석 참고).
          ② 이메일 — 기존(이메일/구글) 계정과의 **자동 연결**. 여기가 계정 탈취 표면이라
             구글과 같은 게이트를 건다.
        """
        user = User.objects.filter(kakao_id=profile.kakao_id).first()
        if user is not None:
            return user, False, None

        existing = User.objects.filter(email=profile.email).first()
        if existing is not None:
            if not profile.email_verified:
                # 카카오가 "이 이메일은 본인 확인 안 됨"이라고 알려준 경우.
                # 로그인 자체를 막는 게 아니라 **기존 계정과의 연결만** 막는다.
                logger.warning(
                    "kakao_login: is_email_verified=false, refusing link. domain=%s",
                    profile.email.rsplit("@", 1)[-1] if "@" in profile.email else "?",
                )
                return (
                    None,
                    False,
                    _error(
                        "이 이메일로 가입된 계정이 이미 있습니다. "
                        "카카오에서 이메일 소유 확인이 완료되지 않아 자동 연결할 수 없습니다. "
                        "비밀번호로 로그인하거나 이메일 인증을 완료해 주세요.",
                        "KAKAO_EMAIL_UNVERIFIED",
                        status.HTTP_403_FORBIDDEN,
                    ),
                )
            return existing, False, None

        marketing_opt_in = bool(data.get("marketing_opt_in"))
        user = User.objects.create(
            email=profile.email,
            full_name=profile.nickname,
            kakao_id=profile.kakao_id,
            # 카카오가 확인해 준 경우에만 '인증됨'으로 승격한다.
            is_email_verified=profile.email_verified,
            email_verified_at=timezone.now() if profile.email_verified else None,
            # 신규 가입 시에만 마케팅 동의 반영 (동의 시 시각도 기록).
            marketing_opt_in=marketing_opt_in,
            marketing_opt_in_at=timezone.now() if marketing_opt_in else None,
        )
        return user, True, None

    def _finalize_signup(self, user, data, request):
        user.set_unusable_password()
        user.save(update_fields=["password"])

        # 가입 유입 attribution — 절대 예외를 던지지 않아 가입을 막지 않는다.
        from apps.analytics.attribution import capture_signup_attribution

        capture_signup_attribution(user, data.get("attribution"), signup_kind="kakao")

        # Meta 전환 API — attribution **저장 뒤에** 불러야 fbc/fbp 를 함께 실어 보낸다.
        # ⚠️ 신규 가입일 때만 부른다 — 재로그인마다 발사하면 전환이 부풀려진다.
        from apps.analytics.conversions import track_signup

        track_signup(user, request=request)

    def _sync_existing_user(self, user, profile):
        updates = []
        # 기존 계정을 카카오에 처음 연결하는 순간 회원번호를 박아 둔다(다음부터는 ①로 매칭).
        if user.kakao_id != profile.kakao_id:
            user.kakao_id = profile.kakao_id
            updates.append("kakao_id")
        if profile.nickname and not user.full_name:
            user.full_name = profile.nickname
            updates.append("full_name")
        # 카카오가 확인해 준 경우에만 우리 쪽 '인증됨' 표시를 켠다 — 미확인 값으로 켜 주면
        # 그 표시를 신뢰하는 다른 경로(비밀번호 재설정 등)까지 오염된다.
        if profile.email_verified and not user.is_email_verified:
            user.is_email_verified = True
            user.email_verified_at = timezone.now()
            updates += ["is_email_verified", "email_verified_at"]
        if updates:
            user.save(update_fields=updates)
