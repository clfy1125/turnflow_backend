"""웹 단독 회원탈퇴 공개 엔드포인트 (`turnflow.link/delete-account` 백엔드).

Google Play 계정 삭제 정책 대응 — 로그인·앱 없이 이메일 소유 증명만으로 탈퇴가
가능해야 한다. 판정·상태전이는 전부 `account_deletion.py` 가 하고, 여기는 HTTP 껍데기다.

⚠️ 전부 `AllowAny` 다. 세 가지를 반드시 지킬 것:
   1. **열거 방지** — 가입 여부에 따라 응답이 달라지면 이메일 유효성 확인 도구가 된다.
      request 는 항상 같은 200 을 준다.
   2. **스로틀** — 메일 폭격 방어. scope 이름은 settings 의 DEFAULT_THROTTLE_RATES 와
      정확히 일치해야 한다 (fail-open 이라 오타면 조용히 꺼진다).
   3. **토큰 단일사용** — 파괴적 동작 직전에만 consume 한다.
"""

from __future__ import annotations

import logging

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .account_deletion import (
    DeletionError,
    confirm_deletion,
    describe_pending,
    describe_public_policy,
    request_deletion,
    restore_by_token,
)

logger = logging.getLogger(__name__)

_TAGS = ["auth"]


def _client_ip(request) -> str | None:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _error(exc: DeletionError, http_status: int = status.HTTP_400_BAD_REQUEST) -> Response:
    return Response(
        {
            "success": False,
            "error": {
                "code": http_status,
                "message": exc.message,
                "details": {"code": exc.code},
            },
        },
        status=http_status,
    )


# ─────────────────────────────────────────────────────────────────────────────
class DeletionPolicySerializer(serializers.Serializer):
    grace_days = serializers.IntegerField()
    deleted_items = serializers.ListField(child=serializers.CharField())
    legal_retention = serializers.ListField(child=serializers.DictField())


class AccountDeletionPolicyView(APIView):
    """삭제 범위·법정 보존 항목 고지문. 페이지 첫 화면이 이걸 그려준다."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        tags=_TAGS,
        summary="탈퇴 고지문 조회",
        description="""
## 개요
회원탈퇴 시 **삭제되는 항목**과 **법령에 따라 보존되는 항목**을 반환합니다.
`turnflow.link/delete-account` 첫 화면이 이 값을 그대로 렌더합니다.

## 인증
불필요 (공개).

## 왜 서버가 주는가
"7일 후 모두 삭제"는 사실이 아닙니다 — 전자상거래법·전자금융거래법상 결제·거래
기록은 최대 5년 보존 의무가 있습니다. 이 고지문을 프론트에 하드코딩하면 법령이
바뀔 때 화면과 실제 처리가 어긋나므로 **서버가 단일 소스**로 내려줍니다.

## 응답 필드
- `grace_days` (int): 탈퇴 확정 후 영구 삭제까지의 유예 일수
- `deleted_items` (string[]): 유예 만료 시 삭제되는 항목
- `legal_retention` (object[]): `{item, basis, period}` — 보존 항목·근거 법령·기간

## 사용 예시
```bash
curl https://api.turnflow.link/api/v1/auth/deletion/policy/
```
```json
{
  "grace_days": 7,
  "deleted_items": ["계정 정보 (이메일, 이름, 비밀번호)", "..."],
  "legal_retention": [
    {"item": "대금결제 및 재화 등의 공급에 관한 기록",
     "basis": "전자상거래 등에서의 소비자보호에 관한 법률 제6조",
     "period": "5년"}
  ]
}
```
        """,
        responses={
            200: OpenApiResponse(
                response=DeletionPolicySerializer, description="고지문 (항상 200)"
            ),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def get(self, request):
        return Response(describe_public_policy())


# ─────────────────────────────────────────────────────────────────────────────
class DeletionRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="가입 시 사용한 이메일 주소")


class AccountDeletionRequestView(APIView):
    """① 가입 이메일로 탈퇴 인증 링크를 보낸다."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "account_deletion_request"

    @extend_schema(
        tags=_TAGS,
        summary="탈퇴 인증 메일 발송",
        description="""
## 개요
입력한 이메일이 가입된 계정이면 **탈퇴 인증 링크**를 메일로 보냅니다.
이 요청만으로는 **아무것도 삭제되지 않습니다** — 메일의 링크를 눌러 최종 동의해야
탈퇴가 확정됩니다.

## 사용 시나리오
`turnflow.link/delete-account` 에서 사용자가 이메일을 입력하고 "인증 메일 보내기"를
누를 때. Google 로그인 사용자도 계정에 이메일이 있으므로 동일하게 동작합니다.

## 인증
불필요 (공개). 로그인 없이 탈퇴를 시작할 수 있어야 하는 Google Play 정책 대응입니다.

## 요청 필드
- `email` (필수, EmailField): 가입 시 사용한 이메일 주소

## ⚠️ 응답이 항상 같습니다 (의도된 동작)
가입 여부·이미 탈퇴 접수 여부와 **무관하게 항상 200 + 동일한 메시지**를 반환합니다.
응답이 갈리면 이 엔드포인트가 "이 이메일이 가입돼 있는지" 확인하는 도구
(user enumeration)가 되기 때문입니다. 따라서 프론트는 "메일을 확인해 주세요"만
안내하고 성공/실패를 구분해 표시하지 말아야 합니다.

운영자 계정(staff/superuser)은 메일 한 통으로 사라지면 안 되므로 이 경로에서
제외되며, 이 경우에도 응답은 동일합니다.

## 제한
`5회/시간` (IP 기준). 초과 시 429.

## 사용 예시
```bash
curl -X POST https://api.turnflow.link/api/v1/auth/deletion/request/ \\
  -H "Content-Type: application/json" \\
  -d '{"email":"user@example.com"}'
```
```javascript
await fetch('/api/v1/auth/deletion/request/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email }),
});
// 성공/실패를 구분해 보여주지 말 것 — 항상 같은 안내를 띄운다
```
```json
{ "detail": "입력하신 이메일로 탈퇴 인증 링크를 보냈습니다. 메일함을 확인해 주세요." }
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | `email` 누락 또는 이메일 형식 아님 |
| 429 | 시간당 요청 한도 초과 |
| 500 | 서버 오류 |
        """,
        request=DeletionRequestSerializer,
        responses={
            200: OpenApiResponse(description="메일 발송 시도 완료 (가입 여부와 무관하게 동일)"),
            400: OpenApiResponse(description="이메일 형식 오류 또는 누락"),
            429: OpenApiResponse(description="요청 한도 초과"),
            500: OpenApiResponse(description="서버 오류"),
        },
        examples=[
            OpenApiExample("요청", value={"email": "user@example.com"}, request_only=True),
            OpenApiExample(
                "응답",
                value={"detail": "입력하신 이메일로 탈퇴 인증 링크를 보냈습니다. 메일함을 확인해 주세요."},
                response_only=True,
            ),
        ],
    )
    def post(self, request):
        serializer = DeletionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # 반환값으로 응답을 갈라선 안 된다 (열거 방지). 로깅만 한다.
        request_deletion(
            email=serializer.validated_data["email"], request_ip=_client_ip(request)
        )

        return Response(
            {"detail": "입력하신 이메일로 탈퇴 인증 링크를 보냈습니다. 메일함을 확인해 주세요."},
            status=status.HTTP_200_OK,
        )


# ─────────────────────────────────────────────────────────────────────────────
class DeletionTokenSerializer(serializers.Serializer):
    token = serializers.CharField(help_text="메일 링크의 token 쿼리 값")


class AccountDeletionVerifyView(APIView):
    """② 링크의 토큰으로 삭제 영향을 조회한다 (토큰 소비 안 함)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "account_deletion_confirm"

    @extend_schema(
        tags=_TAGS,
        summary="탈퇴 확인 화면 데이터 조회",
        description="""
## 개요
메일 링크의 `token` 으로 **어떤 계정이 어떤 영향을 받는지** 조회합니다.
최종 확인 화면을 그리기 위한 조회이며 **토큰을 소비하지 않습니다** —
화면만 보고 그만둔 사용자가 메일을 다시 받아야 하는 일을 막기 위함입니다.
(메일 클라이언트·보안 스캐너가 링크를 미리 여는 경우도 같은 사고가 됩니다)

## 인증
불필요. 토큰 소유가 곧 인증입니다.

## 요청 필드
- `token` (필수): 메일 링크의 `?token=` 값

## 응답 필드
- `email_masked`: 마스킹된 가입 이메일 (`us**@example.com`) — 링크를 가로챈 제3자에게
  전체 주소를 알려주지 않기 위해 마스킹합니다
- `has_paid_subscription` (bool): 유료 구독 중인지
- `plan`: 구독 중인 플랜 표시명 (없으면 null)
- `paid_until`: 이미 결제된 이용기간의 종료 시각 (ISO8601, 없으면 null).
  **탈퇴 시 이 잔여 기간은 환불 없이 소멸합니다** — 화면에 반드시 표시할 것
- `other_workspace_members` (int): 내가 소유한 워크스페이스의 나 외 멤버 수.
  0이 아니면 그 멤버들의 데이터도 함께 삭제되므로 경고를 띄울 것
- `grace_days`, `deleted_items`, `legal_retention`: 고지문 (policy 와 동일)

## 사용 예시
```bash
curl -X POST https://api.turnflow.link/api/v1/auth/deletion/verify/ \\
  -H "Content-Type: application/json" -d '{"token":"AbC..."}'
```
```json
{
  "email_masked": "us***@example.com",
  "has_paid_subscription": true,
  "plan": "프로",
  "paid_until": "2026-09-10T14:00:00Z",
  "other_workspace_members": 0,
  "grace_days": 7,
  "deleted_items": ["..."],
  "legal_retention": [{"item": "...", "basis": "...", "period": "5년"}]
}
```

## 에러
| 코드 | `details.code` | 원인 |
|------|----------------|------|
| 400 | `invalid_token` | 링크 만료/이미 사용됨 → 처음부터 다시 요청 |
| 400 | `already_pending` | 이미 탈퇴가 접수된 계정 |
| 429 | — | 요청 한도 초과 |
        """,
        request=DeletionTokenSerializer,
        responses={
            200: OpenApiResponse(description="확인 화면 데이터"),
            400: OpenApiResponse(description="토큰 만료/사용됨 또는 이미 접수됨"),
            429: OpenApiResponse(description="요청 한도 초과"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def post(self, request):
        serializer = DeletionTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(describe_pending(raw_token=serializer.validated_data["token"]))
        except DeletionError as exc:
            return _error(exc)


# ─────────────────────────────────────────────────────────────────────────────
class DeletionConfirmSerializer(serializers.Serializer):
    token = serializers.CharField(help_text="메일 링크의 token 쿼리 값")
    agree_permanent = serializers.BooleanField(
        help_text="계정과 데이터가 영구 삭제되며 되돌릴 수 없음에 동의"
    )
    agree_no_refund = serializers.BooleanField(
        help_text="남은 유료 이용기간이 환불 없이 소멸함에 동의"
    )

    def validate(self, attrs):
        # 두 동의를 서버에서 강제한다. 프론트 체크박스만 믿으면 동의 없는 삭제가
        # 통과할 수 있고, 그러면 '잔여기간 무환불 소멸'을 고지했다는 증거가 없어진다.
        missing = [
            name
            for name in ("agree_permanent", "agree_no_refund")
            if not attrs.get(name)
        ]
        if missing:
            raise serializers.ValidationError(
                {name: "탈퇴를 진행하려면 이 항목에 동의해야 합니다." for name in missing}
            )
        return attrs


class AccountDeletionConfirmView(APIView):
    """③ 최종 확정 — 구독 즉시 해지 + 계정 비활성화 + 유예 예약."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "account_deletion_confirm"

    @extend_schema(
        tags=_TAGS,
        summary="회원탈퇴 최종 확정",
        description="""
## 개요
탈퇴를 **확정**합니다. 이 호출이 성공하면 즉시:
1. 유료 구독이 있으면 **즉시 해지** (잔여 기간은 환불 없이 소멸)
2. 등록된 결제 수단(토스 빌링키) **삭제**
3. 계정 **비활성화** (로그인 불가)
4. 발급된 refresh 토큰 **전부 무효화**
5. `grace_days` 일 후 영구 삭제 예약

실제 데이터 파기는 유예 만료 후 주기 배치가 수행합니다. 그때까지는 복구할 수 있습니다.

## 인증
불필요. 토큰 소유가 곧 인증이며, 이 호출에서 토큰이 **소비**되어 재사용할 수 없습니다.

## 요청 필드
- `token` (필수): 메일 링크의 `?token=` 값
- `agree_permanent` (필수, **true 여야 함**): 영구 삭제·비가역에 동의
- `agree_no_refund` (필수, **true 여야 함**): 잔여 유료기간 무환불 소멸에 동의

두 동의는 **서버에서 강제**합니다(false 면 400). 프론트 체크박스만으로는
고지했다는 증거가 남지 않기 때문입니다.

## 응답 필드
- `email_masked`: 마스킹된 이메일
- `purge_at`: 영구 삭제 예정 시각 (ISO8601)
- `cancelled_subscription` (bool): 이 호출로 유료 구독을 해지했는지
- `grace_days`, `deleted_items`, `legal_retention`: 고지문

## 사용 예시
```bash
curl -X POST https://api.turnflow.link/api/v1/auth/deletion/confirm/ \\
  -H "Content-Type: application/json" \\
  -d '{"token":"AbC...","agree_permanent":true,"agree_no_refund":true}'
```
```json
{
  "email_masked": "us***@example.com",
  "purge_at": "2026-08-28T05:12:00Z",
  "cancelled_subscription": true,
  "grace_days": 7,
  "deleted_items": ["..."],
  "legal_retention": [{"item": "...", "basis": "...", "period": "5년"}]
}
```

## 에러
| 코드 | `details.code` | 원인 |
|------|----------------|------|
| 400 | — | 동의 항목 누락/false |
| 400 | `invalid_token` | 링크 만료/이미 사용됨 |
| 400 | `already_pending` | 이미 탈퇴가 접수된 계정 |
| 429 | — | 요청 한도 초과 |
        """,
        request=DeletionConfirmSerializer,
        responses={
            200: OpenApiResponse(description="탈퇴 접수 완료"),
            400: OpenApiResponse(description="동의 누락 또는 토큰 무효"),
            429: OpenApiResponse(description="요청 한도 초과"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def post(self, request):
        serializer = DeletionConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(confirm_deletion(raw_token=serializer.validated_data["token"]))
        except DeletionError as exc:
            return _error(exc)


# ─────────────────────────────────────────────────────────────────────────────
class AccountDeletionRestoreView(APIView):
    """⑤ 유예 중 복구 (메일의 복구 링크)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "account_deletion_confirm"

    @extend_schema(
        tags=_TAGS,
        summary="탈퇴 취소 (계정 복구)",
        description="""
## 개요
유예 기간 중인 탈퇴를 **취소**하고 계정을 되살립니다. 탈퇴 접수 메일에 담긴
복구 링크가 이 엔드포인트를 호출합니다.

## 인증
불필요. 복구 토큰 소유가 곧 인증이며, 이 호출에서 토큰이 소비됩니다.

## 요청 필드
- `token` (필수): 접수 메일 복구 링크의 `?token=` 값

## ⚠️ 구독은 복구되지 않습니다
탈퇴 확정 시점에 구독을 해지하고 결제 수단을 이미 삭제했으므로, 복구된 계정은
**무료 플랜** 상태입니다. 계속 이용하려면 결제 수단을 다시 등록해야 합니다.
응답의 `subscription_restored` 는 항상 `false` 이며, 화면에서 이 사실을 반드시
안내해야 합니다.

## 응답 필드
- `email_masked`: 마스킹된 이메일
- `subscription_restored` (bool): 항상 `false`

## 다른 복구 경로
복구 메일을 잃어버린 경우, 탈퇴한 계정으로 **로그인을 시도**하면
`POST /api/v1/auth/login/` 이 409 + `details.code == "account_deletion_pending"` 과
함께 남은 유예 기간을 알려줍니다.

## 사용 예시
```bash
curl -X POST https://api.turnflow.link/api/v1/auth/deletion/restore/ \\
  -H "Content-Type: application/json" -d '{"token":"AbC..."}'
```
```json
{ "email_masked": "us***@example.com", "subscription_restored": false }
```

## 에러
| 코드 | `details.code` | 원인 |
|------|----------------|------|
| 400 | `invalid_token` | 복구 링크 만료/사용됨 → 로그인 시도로 복구 |
| 400 | `not_pending` | 탈퇴 접수 상태가 아님 (이미 복구됨) |
| 429 | — | 요청 한도 초과 |
        """,
        request=DeletionTokenSerializer,
        responses={
            200: OpenApiResponse(description="복구 완료"),
            400: OpenApiResponse(description="토큰 무효 또는 접수 상태 아님"),
            429: OpenApiResponse(description="요청 한도 초과"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def post(self, request):
        serializer = DeletionTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            return Response(restore_by_token(raw_token=serializer.validated_data["token"]))
        except DeletionError as exc:
            return _error(exc)
