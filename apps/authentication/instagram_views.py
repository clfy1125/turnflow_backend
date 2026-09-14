"""인스타그램 로그인/가입 — ``GET /auth/instagram/start/`` + ``POST /auth/instagram/``.

⭐ **이 기능의 요점은 "두 번 인증"을 없애는 것이다** (2026-09-10 병목 진단 1순위).
   지금은 ①구글/이메일로 가입 → ②설정에서 인스타 연동, 두 번 인증해야 자동 DM 을 쓸 수
   있다. 여기서는 **인스타 인증 한 번으로 가입 + 워크스페이스 생성 + IG 연동까지** 끝난다.

응답 모양(user / is_new_user / tokens)·탈퇴 유예 409·attribution·CAPI 발사 조건은
``GoogleLoginView`` / ``KakaoLoginView`` 와 **의도적으로 동일**하다 — 다르면 프론트가
가입 수단마다 분기를 세 벌 든다. 추가된 것은 ``ig_connection`` 하나뿐이다.

⚠️ 기본 **비활성**(``INSTAGRAM_LOGIN_ENABLED=False``). 2026-09-10 내부 논의:
   "한 번 넣으면 그걸로 로그인한 사람이 한 명이라도 생기는 순간 다시는 못 뺀다."
   테스트 URL 에서 먼저 써 보고 켜기로 했다.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import instagram
from .serializers import InstagramLoginSerializer, UserSerializer
from .tokens import AppRefreshToken

logger = logging.getLogger(__name__)


def _error(message: str, code: str, http_status: int) -> Response:
    """``detail``/``code`` + §6 통일 envelope 를 **동시** 송출.

    이 뷰는 DRF 예외 핸들러를 우회해 Response 를 직접 만든다. detail 만 내면 같은 URL 이
    시리얼라이저 400(envelope)과 우리 400(detail)을 번갈아 내게 된다 — 카카오 로그인에서
    같은 함정을 이미 밟았다(KAKAO_LOGIN_FRONTEND.md).
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


def _error_extra(message: str, code: str, http_status: int, extra: dict) -> Response:
    """``_error`` + 프론트가 문구를 만들 때 쓰는 추가 필드(마스킹된 값만)."""
    payload = {
        "detail": message,
        "code": code,
        "success": False,
        "error": {
            "code": http_status,
            "message": message,
            "details": {"code": code, **extra},
        },
    }
    payload.update(extra)
    return Response(payload, status=http_status)


class InstagramLoginStartView(APIView):
    """authorize URL 발급 — 프론트는 이 URL 로 이동만 하면 된다."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_instagram"

    @extend_schema(
        tags=["authentication"],
        summary="인스타그램 로그인 시작",
        description="""
## 개요
Instagram Business Login 의 **authorize URL 과 state** 를 발급합니다. 프론트는 받은
`authorize_url` 로 이동(또는 팝업)시키기만 하면 됩니다.

## 사용 시나리오
1. 로그인/가입 화면에서 "인스타그램으로 시작하기" 클릭 → 이 API 호출
2. 응답의 `authorize_url` 로 이동 → 사용자가 인스타에서 동의
3. 인스타가 `redirect_uri` 로 `?code=...&state=...` 를 붙여 되돌려 보냄
4. 프론트 콜백 라우트가 `POST /api/v1/auth/instagram/` 에 `code` + `state` 전달

## 인증
**불필요** — 가입 전 사용자가 호출합니다.

## 비즈니스 로직
- `state` 는 서버가 만들어 **1회용**으로 저장합니다(TTL 10분). 교환 때 검증·소비합니다.
- `redirect_uri` 는 **허용된 프론트 origin 과 정확히 일치**해야 합니다(오픈 리다이렉트
  방어 — IG 연동의 `return_to` 와 같은 허용목록을 씁니다). 생략하면 서버 기본값
  (`INSTAGRAM_LOGIN_REDIRECT_URI`)을 씁니다.
- ⚠️ 여기서 준 `redirect_uri` 와 **글자 그대로 같은 값**이 Meta 앱 대시보드에 등록돼
  있어야 합니다. 다르면 인스타가 authorize 단계에서 거부합니다.

## 쿼리 파라미터
| 이름 | 필수 | 설명 |
|------|:----:|------|
| `redirect_uri` | 선택 | 인스타가 되돌려 보낼 프론트 주소. 허용 origin 과 완전일치 |

## 응답
```json
{ "authorize_url": "https://www.instagram.com/oauth/authorize?...", "state": "…", "mode": "production" }
```
`mode` 는 `production` 또는 `mock`(INSTAGRAM_MOCK_MODE) 입니다.

## 주의사항
- iOS 는 `www.instagram.com/oauth/authorize` 를 **유니버설 링크로 판정해 인스타 앱을
  띄웁니다.** 팝업 방식이면 부모 창이 결과를 못 받습니다 → **같은 탭 이동**을 쓰세요.
- 이 기능은 서버 킬스위치(`INSTAGRAM_LOGIN_ENABLED`) 뒤에 있습니다. 꺼져 있으면 404 입니다.

## 사용 예시
```bash
curl "https://api.turnflow.link/api/v1/auth/instagram/start/?redirect_uri=https%3A%2F%2Fapp.turnflow.link%2Fauth%2Finstagram%2Fcallback"
```
        """,
        parameters=[
            OpenApiParameter(
                name="redirect_uri",
                description="인스타가 되돌려 보낼 프론트 주소 (허용 origin 과 완전일치). 생략 시 서버 기본값",
                required=False,
                type=str,
            )
        ],
        responses={
            200: OpenApiResponse(
                description="authorize URL 발급",
                examples=[
                    OpenApiExample(
                        "발급됨",
                        value={
                            "authorize_url": "https://www.instagram.com/oauth/authorize?client_id=...&state=...",
                            "state": "0lVQ3v…",
                            "mode": "production",
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="허용되지 않은 redirect_uri",
                examples=[
                    OpenApiExample(
                        "허용 안 된 주소",
                        value={
                            "detail": "허용되지 않은 redirect_uri 입니다. 프론트 origin 과 정확히 일치해야 합니다.",
                            "code": "INSTAGRAM_INVALID_REDIRECT_URI",
                        },
                    )
                ],
            ),
            401: OpenApiResponse(description="해당 없음 — 인증이 필요 없습니다"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(
                description="인스타그램 로그인이 비활성(INSTAGRAM_LOGIN_ENABLED=False)",
                examples=[
                    OpenApiExample(
                        "비활성",
                        value={
                            "detail": "인스타그램 로그인이 아직 활성화되지 않았습니다.",
                            "code": "INSTAGRAM_LOGIN_DISABLED",
                        },
                    )
                ],
            ),
            429: OpenApiResponse(description="요청이 너무 잦습니다 (IP 기준 20/min)"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
            503: OpenApiResponse(
                description="서버에 인스타 자격증명/복귀 주소가 설정되지 않음 (운영 설정 누락)"
            ),
        },
    )
    def get(self, request):
        if not instagram.login_enabled():
            return _error(
                "인스타그램 로그인이 아직 활성화되지 않았습니다.",
                "INSTAGRAM_LOGIN_DISABLED",
                status.HTTP_404_NOT_FOUND,
            )
        if not instagram.is_configured():
            return _error(
                "인스타그램 로그인이 서버에 설정되어 있지 않습니다.",
                "INSTAGRAM_NOT_CONFIGURED",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            redirect_uri = instagram.resolve_redirect_uri(
                request.query_params.get("redirect_uri", "")
            )
        except instagram.InstagramAuthError as exc:
            return _error(exc.message, exc.code, exc.status)

        state_row = instagram.create_state(redirect_uri)
        from apps.integrations.services import MockInstagramProvider

        return Response(
            {
                "authorize_url": instagram.authorization_url(redirect_uri, state_row.state),
                "state": state_row.state,
                "mode": "mock" if MockInstagramProvider.is_mock_mode() else "production",
            }
        )


class InstagramLoginView(APIView):
    """인가 코드 → 우리 JWT (+ IG 연동까지 한 번에)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth_instagram"
    serializer_class = InstagramLoginSerializer

    def get_serializer(self, *args, **kwargs):
        return InstagramLoginSerializer(*args, **kwargs)

    @extend_schema(
        tags=["authentication"],
        summary="인스타그램 로그인/가입",
        description="""
## 개요
인스타그램 인가 코드를 우리 서비스의 JWT 로 바꿉니다. **가입과 IG 연동이 한 번에**
끝나는 것이 이 엔드포인트의 존재 이유입니다 — 신규 사용자는 이 호출 하나로
계정 + 워크스페이스 + IG 연동이 모두 만들어지고, **별도의 연동 단계가 없습니다.**

## 사용 시나리오
- `GET /auth/instagram/start/` 로 받은 URL 에서 돌아온 프론트 콜백 라우트가 호출
- 가입과 로그인이 **같은 엔드포인트**입니다. 구별은 응답의 `is_new_user` 로 하세요.

## 인증
**불필요** — 이 호출의 결과로 토큰을 받습니다.

## 비즈니스 로직
### 계정 매칭 (중요)
**인스타로 가입한 계정만 인스타로 로그인됩니다.**

1. `User.instagram_user_id` 가 이 IG 와 일치 → 그 사용자로 **로그인**
2. 그 IG 가 **다른 턴플로우 계정에 연동 중** → **409 로 거부** (아래 참고)
3. 그 외 → **신규 가입** (새 계정 + 새 워크스페이스)

⚠️ **그 IG 가 다른 계정(구글·이메일)에 연동돼 있으면 로그인시키지 않습니다.**
그 계정으로 보내지도, 새 계정을 만들지도 않습니다.

```json
409 {
  "detail": "이 인스타그램 계정은 이미 다른 턴플로우 계정에 연결되어 있습니다. 기존 계정에서 연동을 해제한 뒤 다시 시도해 주세요.",
  "code": "INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE",
  "masked_email": "ow***@example.com",
  "instagram_username": "myhandle",
  "success": false,
  "error": { "code": 409, "message": "...", "details": { "code": "INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE", "masked_email": "ow***@example.com", "instagram_username": "myhandle" } }
}
```

프론트는 `masked_email` 로 "`ow***@example.com` 계정에 연결되어 있어요.
그 계정에서 연동을 해제한 뒤 다시 시도해 주세요" 를 띄우면 됩니다.
원본 이메일은 내려주지 않습니다(연동 충돌 화면과 같은 마스킹 규칙).

**기존 계정에서 연동을 해제하면** 점유가 풀려 다음 시도부터 **새 계정으로 가입**됩니다.

`username` 으로는 절대 찾지 않습니다 — IG 핸들은 언제든 바뀌고 남이 이어받을 수 있습니다.

### 신규 가입 시 서버가 하는 일
1. 사용자 생성 (비밀번호 없음 — 인스타로만 로그인)
2. **자리표시 이메일 발급** (`ig_<id>@ig.invalid`) — 아래 '이메일' 참고
3. 워크스페이스 생성 (IG username 기반 이름)
4. IG 연동 생성 + 웹훅 구독 + 백그라운드 부트스트랩
5. 가입 귀속(attribution) 저장 + Meta CAPI `CompleteRegistration` 발사

### 이메일이 없다는 것
**Instagram Business Login 은 이메일을 주지 않습니다.** 우리 로그인 키가 이메일이라
비워 둘 수 없어 `ig_<instagram_user_id>@ig.invalid` 를 만들어 넣습니다.
- 이 주소로는 **메일이 나가지 않습니다** (발송 계층이 차단).
- 응답의 `user.email_is_placeholder` 가 `true` 입니다 → 프론트는 "알림 받을 이메일을
  등록해 주세요" 배너를 띄우세요.
- `.invalid` 는 RFC 2606 예약 TLD 라 실수로 발송돼도 바운스가 생기지 않습니다.

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `code` | ✅ | string | 인스타가 돌려준 인가 코드 (1회용) |
| `state` | ✅ | string | `start` 에서 받은 값. 1회용·10분 |
| `attribution` | 선택 | object | 유입 귀속 (`visitor_id`/utm/fbclid …). 구글·카카오와 동일 |
| `marketing_opt_in` | 선택 | bool | 마케팅 수신 동의. **신규 가입 시에만** 반영 |

`redirect_uri` 는 **받지 않습니다** — `state` 에 저장된 값을 씁니다. 클라이언트가 다시
보낸 값을 쓰면 authorize 때와 한 글자만 달라도 Meta 가 거부하는데, 원인을 찾기 어렵습니다.

## 응답 (200)
```json
{
  "user": { "id": 123, "email": "ig_178414…@ig.invalid", "email_is_placeholder": true, ... },
  "is_new_user": true,
  "tokens": { "access": "…", "refresh": "…" },
  "ig_connection": { "id": "…", "username": "turnflow", "status": "active", ... },
  "ig_connection_error": null
}
```
- `ig_connection` 이 `null` 이고 `ig_connection_error` 가 채워질 수 있습니다 —
  **로그인 자체는 성공**했지만 연동이 막힌 경우입니다(`PLAN_LIMIT_EXCEEDED`,
  `ALREADY_CONNECTED_ELSEWHERE`). 이때도 200 이며, 프론트는 설정 화면으로 안내하세요.

## 주의사항
- `state` 는 1회용입니다. 콜백 화면에서 새로고침하면 `INSTAGRAM_STATE_USED` 400 이 납니다
  → "다시 시도" 버튼은 `start` 부터 다시 밟게 하세요.
- 탈퇴 유예 중인 계정은 **409** 입니다(구글·카카오와 동일).
- 오류 응답은 `detail`/`code` 와 표준 envelope 를 **함께** 담습니다.

## 사용 예시
```javascript
const res = await fetch('/api/v1/auth/instagram/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ code, state, attribution: readAttribution() }),
});
const data = await res.json();
if (data.is_new_user) trackCompleteRegistration(String(data.user.id));
```
        """,
        request=InstagramLoginSerializer,
        responses={
            200: OpenApiResponse(
                description="로그인/가입 성공",
                examples=[
                    OpenApiExample(
                        "신규 가입 (연동까지 완료)",
                        value={
                            "user": {
                                "id": 1234,
                                "email": "ig_17841400000000000@ig.invalid",
                                "full_name": "턴플로우",
                                "email_is_placeholder": True,
                                "is_email_verified": False,
                            },
                            "is_new_user": True,
                            "tokens": {"refresh": "eyJ…", "access": "eyJ…"},
                            "ig_connection": {
                                "id": "3f1c2b74-9a1e-4f7b-8f52-1d2c3e4a5b6c",
                                "username": "turnflow",
                                "status": "active",
                            },
                            "ig_connection_error": None,
                        },
                    ),
                    OpenApiExample(
                        "기존 사용자 로그인",
                        value={
                            "user": {"id": 1234, "email": "me@example.com"},
                            "is_new_user": False,
                            "tokens": {"refresh": "eyJ…", "access": "eyJ…"},
                            "ig_connection": {"id": "…", "username": "turnflow"},
                            "ig_connection_error": None,
                        },
                    ),
                    OpenApiExample(
                        "로그인은 됐으나 연동은 한도 초과",
                        value={
                            "user": {"id": 1234},
                            "is_new_user": False,
                            "tokens": {"refresh": "eyJ…", "access": "eyJ…"},
                            "ig_connection": None,
                            "ig_connection_error": "PLAN_LIMIT_EXCEEDED",
                        },
                    ),
                ],
            ),
            400: OpenApiResponse(
                description="code/state 문제 (만료·재사용·불일치) 또는 바디 검증 실패",
                examples=[
                    OpenApiExample(
                        "state 재사용",
                        value={
                            "detail": "이미 처리된 로그인 요청입니다. 다시 시도해 주세요.",
                            "code": "INSTAGRAM_STATE_USED",
                        },
                    ),
                    OpenApiExample(
                        "코드 무효",
                        value={
                            "detail": "인스타그램 인증에 실패했습니다. 다시 시도해 주세요.",
                            "code": "INSTAGRAM_CODE_INVALID",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="해당 없음 — 인증이 필요 없습니다"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(
                description="인스타그램 로그인이 비활성(INSTAGRAM_LOGIN_ENABLED=False)"
            ),
            409: OpenApiResponse(
                description=(
                    "① 이 IG 가 이미 다른 계정에 연동 중(`INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE`) "
                    "또는 ② 탈퇴 유예 중인 계정(`account_deletion_pending`)"
                ),
                examples=[
                    OpenApiExample(
                        "이미 다른 계정에 연동됨",
                        value={
                            "detail": "이 인스타그램 계정은 이미 다른 턴플로우 계정에 "
                            "연결되어 있습니다. 기존 계정에서 연동을 해제한 뒤 다시 시도해 주세요.",
                            "code": "INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE",
                            "masked_email": "ow***@example.com",
                            "instagram_username": "myhandle",
                        },
                    ),
                    OpenApiExample(
                        "탈퇴 접수됨",
                        value={
                            "success": False,
                            "error": {
                                "code": 409,
                                "message": "이 계정은 회원탈퇴가 접수되어 이용이 중단되었습니다.",
                                "details": {"code": "account_deletion_pending"},
                            },
                        },
                    ),
                ],
            ),
            429: OpenApiResponse(description="요청이 너무 잦습니다 (IP 기준 20/min)"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
            502: OpenApiResponse(description="인스타그램 서버 연결/응답 실패 — 재시도 가능"),
            503: OpenApiResponse(description="서버에 인스타 자격증명이 설정되지 않음"),
        },
    )
    def post(self, request):
        if not instagram.login_enabled():
            return _error(
                "인스타그램 로그인이 아직 활성화되지 않았습니다.",
                "INSTAGRAM_LOGIN_DISABLED",
                status.HTTP_404_NOT_FOUND,
            )
        if not instagram.is_configured():
            return _error(
                "인스타그램 로그인이 서버에 설정되어 있지 않습니다.",
                "INSTAGRAM_NOT_CONFIGURED",
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            state_row = instagram.consume_state(data["state"])
            profile = instagram.exchange_code(data["code"], state_row.redirect_uri)
        except instagram.InstagramAuthError as exc:
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

        workspace = self._ensure_workspace(user, profile)
        connection, connect_error = self._attach_connection(workspace, profile)

        refresh = AppRefreshToken.for_user(user)
        payload = {
            "user": UserSerializer(user).data,
            # ⭐ 가입/로그인이 같은 엔드포인트라 프론트가 구별할 방법이 이 값뿐이다.
            "is_new_user": created,
            "tokens": {"refresh": str(refresh), "access": str(refresh.access_token)},
            "ig_connection": None,
            "ig_connection_error": connect_error or None,
        }
        if connection is not None:
            from apps.integrations.serializers import IGAccountConnectionSerializer

            payload["ig_connection"] = IGAccountConnectionSerializer(connection).data
        return Response(payload, status=status.HTTP_200_OK)

    # ── 내부 헬퍼 ────────────────────────────────────────────────────────
    def _match_or_create_user(self, profile, data):
        """``(user, created, error_response)`` — 셋 중 하나만 의미가 있다.

        ⭐ **매칭은 ``instagram_user_id`` 하나뿐이다** (2026-09-13 정책 확정).
           "인스타로 가입한 계정만 인스타로 로그인된다."

        판정 순서:
          ① ``instagram_user_id`` 일치 → 그 사용자로 로그인
          ② 그 IG 가 **다른 계정에 연동 중**(status != REVOKED) → **409 로 거부**
          ③ 그 외 → 신규 가입

        ⚠️ **②를 "그 워크스페이스 owner 로 로그인" 으로 되돌리지 말 것.** 그렇게 하면 그 IG 에
           접근할 수 있는 다른 사람(대행사·직원)이 버튼 한 번으로 주인의 계정(결제·구독 포함)에
           그대로 들어간다. 실제로 그렇게 동작했고 2026-09-13 에 제거했다.

        ⚠️ **②를 "새 계정 생성" 으로도 두지 말 것.** 한 번 그렇게 만들었다가 되돌렸다 —
           만들어 봤자 IG 가 옛 워크스페이스에 점유돼 있어 연동에 실패하므로, 사용자는
           **아무것도 못 하는 빈 계정**에 갇히고 그 계정이 DB 에 영구히 남는다.
           거부하고 이유를 알려주면 프론트가 "옛 계정에서 연동을 해제하세요" 로 안내할 수 있다.

        옛 계정에서 연동을 **해제**하면(=REVOKED) 점유가 풀려 ③ 으로 떨어진다 — 즉 그때는
        **새 계정으로 새로 가입**된다.

        ⚠️ email 매칭은 **없다** — 인스타는 이메일을 주지 않으므로 비교할 값 자체가 없다.
        """
        from django.contrib.auth import get_user_model

        from apps.integrations.models import IGAccountConnection
        from apps.integrations.oauth_callback_pages import mask_email

        User = get_user_model()

        user = User.objects.filter(instagram_user_id=profile.user_id).first()
        if user is not None:
            self._sync_existing_user(user, profile)
            return user, False, None

        conflict = (
            IGAccountConnection.objects.filter(external_account_id=profile.user_id)
            .exclude(status=IGAccountConnection.Status.REVOKED)
            .select_related("workspace__owner")
            .order_by("-created_at")
            .first()
        )
        if conflict is not None and conflict.workspace and conflict.workspace.owner:
            owner_email = conflict.workspace.owner.email or ""
            logger.info(
                "instagram login 거부(이미 다른 계정에 연동): ig=%s conflict_ws=%s",
                profile.user_id,
                conflict.workspace_id,
            )
            # 원본 이메일은 절대 내보내지 않는다 — 연동 충돌 화면과 같은 마스킹 규칙.
            return (
                None,
                False,
                _error_extra(
                    "이 인스타그램 계정은 이미 다른 턴플로우 계정에 연결되어 있습니다. "
                    "기존 계정에서 연동을 해제한 뒤 다시 시도해 주세요.",
                    "INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE",
                    status.HTTP_409_CONFLICT,
                    {
                        "masked_email": mask_email(owner_email),
                        "instagram_username": profile.username or "",
                    },
                ),
            )

        marketing_opt_in = bool(data.get("marketing_opt_in"))
        user = User.objects.create(
            email=instagram.placeholder_email(profile.user_id),
            full_name=profile.name or profile.username or "",
            instagram_user_id=profile.user_id,
            # 자리표시 주소이므로 **절대 '인증됨'으로 올리지 않는다** — 그 표시를 믿는
            # 다른 경로(비밀번호 재설정 등)가 오염된다.
            is_email_verified=False,
            marketing_opt_in=marketing_opt_in,
            marketing_opt_in_at=timezone.now() if marketing_opt_in else None,
        )
        return user, True, None

    def _sync_existing_user(self, user, profile):
        """재로그인 시 표시 정보만 보정한다.

        ⚠️ **``instagram_user_id`` 를 여기서 쓰지 않는다.** 이 값은 *IG 로 가입할 때* 한 번
           박히고 그 뒤로 바뀌지 않는 로그인 식별자다. 여기서 채우면 "인스타로 가입하지
           않은 계정"이 인스타 로그인 대상이 되어, 위 docstring 의 두 사고가 그대로 돌아온다.
           (이 함수에 오는 사용자는 이미 그 값으로 찾아온 사람이라 채울 것도 없다.)
        """
        if (profile.name or profile.username) and not user.full_name:
            user.full_name = profile.name or profile.username
            user.save(update_fields=["full_name"])

    def _finalize_signup(self, user, data, request):
        user.set_unusable_password()
        user.save(update_fields=["password"])

        # 가입 유입 attribution — 절대 예외를 던지지 않아 가입을 막지 않는다.
        from apps.analytics.attribution import capture_signup_attribution

        capture_signup_attribution(user, data.get("attribution"), signup_kind="instagram")

        # Meta 전환 API — attribution **저장 뒤에** 불러야 fbc/fbp 를 함께 실어 보낸다.
        # 신규 가입일 때만 — 재로그인마다 발사하면 전환이 부풀려진다.
        from apps.analytics.conversions import track_signup

        track_signup(user, request=request)

    def _ensure_workspace(self, user, profile):
        """이 사용자의 워크스페이스 (없으면 생성).

        ⚠️ 기존 사용자는 **이미 있는 것을 쓴다** — 새로 만들면 그 사람의 페이지·캠페인이
           안 보이는 빈 워크스페이스로 떨어진다.
        """
        from apps.workspace.models import Membership, Workspace

        ws = Workspace.objects.filter(owner=user).order_by("created_at").first()
        if ws is not None:
            return ws

        base = (profile.username or profile.name or "workspace").strip()[:40] or "workspace"
        with transaction.atomic():
            ws = Workspace.objects.create(owner=user, name=base)
            # ⚠️ 소유자 Membership 을 같이 만든다 — WorkspaceCreateSerializer 가 하는 일과
            #    같다. 빠뜨리면 소유자인데 멤버 목록에 없고, 멤버십으로 권한을 보는
            #    화면에서 자기 워크스페이스가 안 보인다.
            Membership.objects.create(user=user, workspace=ws, role=Membership.Role.OWNER)
        logger.info("instagram login: 워크스페이스 생성 user=%s ws=%s", user.id, ws.id)
        return ws

    def _attach_connection(self, workspace, profile):
        """IG 연동 생성/갱신. 실패해도 **로그인은 성공시킨다**.

        연동이 막히는 사유(플랜 한도·다른 워크스페이스 점유)는 로그인의 실패 사유가 아니다.
        여기서 401/400 을 내면 사용자는 로그인조차 못 하고, 정작 문제를 풀 화면(설정)에
        들어갈 수가 없다.
        """
        from apps.integrations import connect_service

        try:
            connection, error = connect_service.attach_connection(workspace, profile)
        except Exception:
            logger.exception("instagram login: 연동 저장 실패 ws=%s", workspace.id)
            return None, "CONNECT_FAILED"
        return connection, error
