"""휴대폰 본인확인 — ``POST /auth/me/phone/`` · ``.../verify/`` · ``DELETE /auth/me/phone/``.

⭐ **왜 필요한가** (2026-10-09): 체험 종료·결제 실패·DM 발송 중단처럼 "지금 알려야
손실을 막는" 사건의 도달 수단이 메일뿐이었는데, 인스타로 가입한 349명은 자리표시
이메일이라 메일조차 못 받는다. 카카오 알림톡으로 도달하려면 번호가 있어야 하고,
우리 DB 에는 **한 건도 없었다**.

⭐ **왜 가입 폼이 아니라 로그인 뒤인가**
   ① 익명 발송 경로를 만들지 않으면 SMS 펌핑의 절반이 구조적으로 사라진다 — 공격자가
      먼저 계정을 만들어야 하고, 가입은 이미 IP 당 10회/시간으로 묶여 있다.
   ② 신규 가입(필수)과 기존 회원 회수(선택+보상)가 **같은 두 엔드포인트**를 쓴다.
      경로를 나누면 쿨다운·상한·보상 판정이 두 벌이 되고 한쪽이 조용히 샌다.
   가입 플로우의 일부로 보이게 하는 것은 프론트의 라우팅 문제다(``/signup/phone``) —
   카카오 동의항목 심사가 요구하는 "회원가입 화면 캡처"도 그 화면으로 제출한다.

⚠️ **서버는 미인증 사용자의 API 를 막지 않는다.** ``phone_verification_required`` 로
   "지금 강제해야 하는가"만 알려주고 차단은 프론트가 한다. 서버가 하드 차단을 하면
   프론트 배포가 하루만 늦어도 **전 신규 가입자가 서비스에 못 들어온다**. 되살리고
   싶으면 미들웨어 한 곳에 붙일 것 — 뷰마다 달면 새 엔드포인트가 조용히 열린다.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.billing import phone_reward
from apps.sms.aligo import AligoError
from apps.sms.models import SmsPurpose
from apps.sms.services import send_and_log

from . import phone_guard
from .models import PhoneVerification
from .phone import (
    InvalidPhoneNumber,
    generate_code,
    hash_code,
    mask_phone,
    normalize_phone,
)
from .serializers import UserSerializer

logger = logging.getLogger(__name__)


def _error(message: str, code: str, http_status: int, **extra) -> Response:
    """``detail``/``code`` + §6 통일 envelope 동시 송출.

    ⚠️ DRF 예외 핸들러를 우회해 Response 를 직접 만들기 때문에 두 포맷을 **같이** 낸다
       (kakao/instagram/email 뷰와 같은 규칙). ``detail`` 만 내면 같은 URL 이 두 가지
       오류 포맷을 내게 되고, 프론트가 그 함정을 밟는다.
    """
    body = {
        "detail": message,
        "code": code,
        "success": False,
        "error": {"code": http_status, "message": message, "details": {"code": code, **extra}},
    }
    body.update(extra)
    return Response(body, status=http_status)


def _client_ip(request) -> str | None:
    """원본 IP. ``REMOTE_ADDR`` 만 믿는다 — ``X-Forwarded-For`` 직접 파싱은 위조 가능하고,
    프록시 신뢰 처리는 미들웨어/``NUM_PROXIES`` 계층의 몫이다(conversions.py 와 같은 원칙)."""
    return request.META.get("REMOTE_ADDR") or None


def build_otp_message(code: str) -> str:
    """인증 문자 본문.

    ⚠️ **90 byte(EUC-KR)를 넘기면 LMS 로 승급돼 건당 비용이 3배가 된다.**
       문구를 늘릴 때는 ``tests/test_phone_verification.py`` 의 길이 단언을 볼 것.
    ⚠️ KISA 권고대로 ①발신 주체 ②유효시간 ③타인 공유 금지를 담는다 — 보이스피싱
       2차 피해 시 사업자 과실 판단의 기준이 된다.
    """
    return f"[턴플로우] 인증번호 {code} (3분 유효, 타인에게 알려주지 마세요)"


# ──────────────────────────────────────────────
# 시리얼라이저
# ──────────────────────────────────────────────


class PhoneSendSerializer(serializers.Serializer):
    phone = serializers.CharField(
        max_length=20,
        help_text="휴대폰 번호. `010-1234-5678` · `01012345678` · `+821012345678` 모두 허용.",
    )


class PhoneVerifySerializer(serializers.Serializer):
    code = serializers.CharField(min_length=6, max_length=6, help_text="문자로 받은 6자리 숫자")
    sms_marketing_opt_in = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "선택 — 광고성 문자·알림톡 수신 동의(정보통신망법 §50). "
            "체험 종료·결제 실패 같은 **정보성** 알림톡은 이 동의와 무관하게 발송됩니다."
        ),
    )


# ──────────────────────────────────────────────
# 발송
# ──────────────────────────────────────────────


class PhoneSendCodeView(APIView):
    """인증번호 발송 + 등록된 번호 삭제."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "phone_send"

    @extend_schema(
        tags=["authentication"],
        summary="휴대폰 등록 상태 조회",
        description="""
## 개요
현재 휴대폰 등록 상태와 **지금 등록하면 받는 보상**을 돌려줍니다. 부작용이 없고
문자를 보내지 않으므로, 팝업/배너를 띄울지 판단할 때 자유롭게 호출하세요.

## 사용 시나리오
- 기존 회원용 "번호 등록하고 프로 체험 7일 더 받기" 팝업의 노출 판단
- 설정 > 내 정보의 휴대폰 섹션 렌더
- 재발송 버튼의 남은 쿨다운 복원 (새로고침해도 카운트다운이 이어지도록)

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
- `reward` 는 **예고**입니다(지급되지 않음). `verify` 성공 시 같은 모양의 값이
  실제 지급 결과로 돌아옵니다. 두 값은 같은 판정 함수를 쓰므로 어긋나지 않습니다.
- `reward.kind === "none"` 이면 보상 문구를 **숨기세요**. 있지도 않은 혜택을
  약속하면 그대로 허위 고지가 됩니다.
- `resend_after` 는 **지금 발송을 요청하면 몇 초 뒤에 가능한가**입니다. 0 이면 즉시 가능.

## 응답 (200)
```json
{
  "phone": "010-1234-5678",
  "phone_verified": true,
  "phone_source": "sms",
  "required": false,
  "sms_marketing_opt_in": false,
  "resend_after": 0,
  "daily_remaining": 5,
  "reward": { "kind": "none", "days": 0, "reason": "already_granted", "trial_ends_at": null }
}
```

## 주의사항
- 미등록이면 `phone` 은 빈 문자열이고 `daily_remaining` 은 항상 최대치입니다
  (번호를 모르면 번호별 카운트를 셀 수 없습니다).

## 사용 예시
```javascript
const s = await (await fetch('/api/v1/auth/me/phone/', {
  headers: { Authorization: `Bearer ${token}` },
})).json();
if (!s.phone_verified && s.reward.kind !== 'none') showPhonePopup(s.reward.days);
```
        """,
        responses={
            200: OpenApiResponse(
                description="현재 상태",
                examples=[
                    OpenApiExample(
                        "미등록 + 보상 있음",
                        value={
                            "phone": "",
                            "phone_verified": False,
                            "phone_source": "",
                            "required": False,
                            "sms_marketing_opt_in": False,
                            "resend_after": 0,
                            "daily_remaining": 5,
                            "reward": {
                                "kind": "trial_extended",
                                "days": 7,
                                "reason": "",
                                "trial_ends_at": "2026-11-16T00:00:00+09:00",
                            },
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="해당 없음"),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def get(self, request):
        user = request.user
        from .phone import format_phone

        resend_after = 0
        daily_remaining = phone_guard.max_per_phone_per_day()
        if user.phone:
            last = (
                PhoneVerification.objects.filter(phone=user.phone)
                .order_by("-created_at")
                .only("created_at")
                .first()
            )
            if last is not None:
                elapsed = (timezone.now() - last.created_at).total_seconds()
                resend_after = max(0, int(phone_guard.resend_cooldown_seconds() - elapsed + 0.999))
            sent_today = PhoneVerification.objects.filter(
                phone=user.phone, created_at__gte=phone_guard._today_start()
            ).count()
            daily_remaining = max(0, phone_guard.max_per_phone_per_day() - sent_today)

        return Response(
            {
                "phone": format_phone(user.phone) if user.phone else "",
                "phone_verified": user.phone_verified,
                "phone_source": user.phone_source,
                "required": phone_reward.phone_verification_required(user),
                "sms_marketing_opt_in": user.sms_marketing_opt_in,
                "resend_after": resend_after,
                "daily_remaining": daily_remaining,
                "reward": phone_reward.preview(user).as_dict(),
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        tags=["authentication"],
        summary="휴대폰 인증번호 발송",
        description="""
## 개요
입력한 휴대폰 번호로 **6자리 인증번호**를 문자로 보냅니다. 이어서
`POST /api/v1/auth/me/phone/verify/` 로 코드를 확인해야 번호가 실제로 등록됩니다.

## 사용 시나리오
- **신규 가입자**: 가입 직후 휴대폰 인증 화면(`/signup/phone`). `GET /auth/me/` 의
  `phone_verification_required === true` 인 동안 이 화면을 벗어나지 못하게 합니다.
- **기존 회원**: 설정 > 내 정보, 또는 "번호 등록하고 프로 체험 7일 더 받기" 팝업.

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
1. 번호를 `01012345678` 형태로 정규화합니다. **국내 휴대폰만** 허용(010/011/016~019).
   유선번호·해외번호는 400 `PHONE_INVALID`.
2. 이미 **같은 번호**를 인증해 둔 계정이면 400 `PHONE_ALREADY_VERIFIED`
   (문자를 낭비하지 않습니다). 다른 번호로 바꾸는 것은 언제든 가능합니다.
3. 발송 자격 검사 — 네 겹입니다. 하나라도 걸리면 **문자를 보내지 않습니다**.
   | 제한 | 기본값 | 코드 |
   |---|---|---|
   | 같은 번호 재발송 쿨다운 | 60초 | `PHONE_RESEND_TOO_SOON` (+`retry_after` 초) |
   | 같은 번호 하루 발송 | 5회 | `PHONE_DAILY_LIMIT` |
   | 같은 IP 하루 발송 | 30회 | `PHONE_IP_LIMIT` |
   | 서비스 전체 하루 발송 | 2,000건 | `SMS_CAPACITY_EXCEEDED` (503) |
4. 직전에 보낸 인증번호는 **즉시 무효**가 됩니다(마지막 코드만 유효).
5. 코드는 서버에 **평문으로 저장되지 않습니다**(HMAC). 분실 시 재발송만 가능합니다.

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `phone` | ✅ | string | 휴대폰 번호. 하이픈/공백/국가번호 모두 허용 |

## 응답 (202)
```json
{
  "detail": "인증번호를 보냈습니다.",
  "phone_masked": "010-****-5678",
  "expires_in": 180,
  "resend_after": 60,
  "daily_remaining": 4,
  "reward": { "kind": "trial_extended", "days": 7, "reason": "", "trial_ends_at": "2026-11-16T00:00:00+09:00" }
}
```
`reward` 는 **아직 지급되지 않은 예고**입니다(부작용 없음). 인증을 마치면 같은 모양의
값이 `verify` 응답에 담겨 돌아오며, 그때가 실제 지급 시점입니다.
`kind === "none"` 이면 이 사용자에게 줄 보상이 없다는 뜻이니 보상 문구를 숨기세요.

## 주의사항
- 스로틀 **5회/시간**(사용자 기준). 초과 시 429 — 위 표의 번호별 제한과는 별개입니다.
- 문자는 건당 비용이 나갑니다. 화면에서 "재발송" 버튼은 `resend_after` 초 동안
  비활성화해 주세요(서버도 막지만, 막힌 요청이 429 로 보이면 사용자가 불안해합니다).
- `SMS_MOCK_MODE=True`(개발 서버)에서는 **문자가 실제로 가지 않습니다**. dev 에서
  테스트할 때는 서버 로그 `aligo[mock]` 줄이나 `SmsLog` 를 보세요.

## 사용 예시
```javascript
const res = await fetch('/api/v1/auth/me/phone/', {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ phone: '010-1234-5678' }),
});
if (res.status === 429) {
  const { code, retry_after } = await res.json();
  // code === 'PHONE_RESEND_TOO_SOON' → retry_after 초 카운트다운
}
```
```bash
curl -X POST https://api.turnflow.link/api/v1/auth/me/phone/ \\
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\
  -d '{"phone":"010-1234-5678"}'
```
        """,
        request=PhoneSendSerializer,
        responses={
            202: OpenApiResponse(
                description="인증번호 발송됨",
                examples=[
                    OpenApiExample(
                        "발송됨",
                        value={
                            "detail": "인증번호를 보냈습니다.",
                            "phone_masked": "010-****-5678",
                            "expires_in": 180,
                            "resend_after": 60,
                            "daily_remaining": 4,
                            "reward": {
                                "kind": "trial_extended",
                                "days": 7,
                                "reason": "",
                                "trial_ends_at": "2026-11-16T00:00:00+09:00",
                            },
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="번호 형식 오류 또는 이미 등록된 번호",
                examples=[
                    OpenApiExample(
                        "형식 오류",
                        value={
                            "detail": "올바른 휴대폰 번호가 아닙니다. (예: 010-1234-5678)",
                            "code": "PHONE_INVALID",
                        },
                    ),
                    OpenApiExample(
                        "이미 등록됨",
                        value={
                            "detail": "이미 인증된 번호입니다.",
                            "code": "PHONE_ALREADY_VERIFIED",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            429: OpenApiResponse(
                description="쿨다운·일일 상한·스로틀",
                examples=[
                    OpenApiExample(
                        "쿨다운",
                        value={
                            "detail": "43초 후에 다시 요청할 수 있습니다.",
                            "code": "PHONE_RESEND_TOO_SOON",
                            "retry_after": 44,
                        },
                    )
                ],
            ),
            502: OpenApiResponse(
                description="문자 게이트웨이 오류 — 재시도 안내",
                examples=[
                    OpenApiExample(
                        "발송 실패",
                        value={
                            "detail": "문자 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.",
                            "code": "SMS_SEND_FAILED",
                        },
                    )
                ],
            ),
            503: OpenApiResponse(description="서비스 전체 일일 발송 상한 도달 (운영 확인 필요)"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def post(self, request):
        serializer = PhoneSendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            phone = normalize_phone(serializer.validated_data["phone"])
        except InvalidPhoneNumber as exc:
            return _error(str(exc), "PHONE_INVALID", status.HTTP_400_BAD_REQUEST)

        user = request.user
        if user.phone == phone and user.phone_verified:
            return _error(
                "이미 인증된 번호입니다.",
                "PHONE_ALREADY_VERIFIED",
                status.HTTP_400_BAD_REQUEST,
            )

        ip = _client_ip(request)
        decision = phone_guard.check_can_send(user=user, phone=phone, ip=ip)
        if not decision.allowed:
            http_status = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if decision.code == "SMS_CAPACITY_EXCEEDED"
                else status.HTTP_429_TOO_MANY_REQUESTS
            )
            extra = {"retry_after": decision.retry_after} if decision.retry_after else {}
            return _error(decision.message, decision.code, http_status, **extra)

        code = generate_code()
        ttl = phone_guard.code_ttl_seconds()
        now = timezone.now()

        with transaction.atomic():
            # 직전 코드를 죽인다 — 살려 두면 두 코드가 동시에 유효해져 시도 횟수 제한이
            # 사실상 2배가 되고, "방금 받은 코드가 안 먹네" 혼선도 생긴다.
            PhoneVerification.objects.filter(
                user=user, verified_at__isnull=True, invalidated_at__isnull=True
            ).update(invalidated_at=now)
            row = PhoneVerification.objects.create(
                user=user,
                phone=phone,
                code_hash=hash_code(phone, code),
                expires_at=now + timezone.timedelta(seconds=ttl),
                request_ip=ip,
            )

        try:
            send_and_log(
                to=phone,
                body=build_otp_message(code),
                purpose=SmsPurpose.PHONE_VERIFY,
                user=user,
                request_ip=ip,
            )
        except AligoError as exc:
            # 발송에 실패했으면 그 행은 쓸 수 없다 — 살려 두면 쿨다운만 잡아먹는다.
            PhoneVerification.objects.filter(pk=row.pk).update(invalidated_at=timezone.now())
            http_status = (
                status.HTTP_502_BAD_GATEWAY
                if exc.code in ("SMS_UNAVAILABLE", "SMS_SEND_FAILED")
                else status.HTTP_400_BAD_REQUEST
            )
            return _error(exc.message, exc.code, http_status)

        phone_guard.note_sent(ip=ip)

        sent_today = PhoneVerification.objects.filter(
            phone=phone, created_at__gte=phone_guard._today_start()
        ).count()
        logger.info("phone_otp sent: user=%s phone=%s", user.id, mask_phone(phone))

        return Response(
            {
                "detail": "인증번호를 보냈습니다.",
                "phone_masked": mask_phone(phone),
                "expires_in": ttl,
                "resend_after": phone_guard.resend_cooldown_seconds(),
                "daily_remaining": max(0, phone_guard.max_per_phone_per_day() - sent_today),
                "reward": phone_reward.preview(user).as_dict(),
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        tags=["authentication"],
        summary="등록된 휴대폰 번호 삭제",
        description="""
## 개요
등록된 휴대폰 번호와 문자 수신 동의를 지웁니다. 개인정보 자기결정권(개인정보보호법 §37)
— **회원탈퇴 없이도** 번호 제공을 철회할 수 있어야 합니다.

## 사용 시나리오
설정 > 내 정보 > "휴대폰 번호 삭제".

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
1. 휴대폰 인증이 **필수인 사용자**(`phone_verification_required === true` 대상, 즉
   정책 시행 이후 가입자)는 삭제할 수 없습니다 → 409 `PHONE_REQUIRED`.
   번호를 바꾸려면 삭제가 아니라 **새 번호로 다시 인증**하세요(덮어쓰기됩니다).
2. 번호·인증 시각·출처·문자 수신 동의를 함께 비웁니다.
3. **이미 지급된 보상은 회수하지 않습니다**(`phone_reward_granted_at` 유지). 그래서
   지웠다 다시 등록해도 보상은 두 번 나가지 않습니다.
4. 과거 `SmsLog` 는 남습니다(발송 기록·분쟁 대응). 보존기간 경과 시 배치가 지웁니다.

## 응답 (200)
```json
{ "detail": "휴대폰 번호를 삭제했습니다.", "user": { "phone": "", "phone_verified": false } }
```

## 주의사항
- 삭제하면 체험 종료·결제 실패 알림톡을 받지 못합니다. 확인 모달에 이 문구를 넣어 주세요.

## 사용 예시
```javascript
await fetch('/api/v1/auth/me/phone/', {
  method: 'DELETE',
  headers: { Authorization: `Bearer ${token}` },
});
```
        """,
        responses={
            200: OpenApiResponse(response=UserSerializer, description="삭제됨"),
            400: OpenApiResponse(description="해당 없음"),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            409: OpenApiResponse(
                description="휴대폰 인증이 필수인 계정 — 삭제 불가",
                examples=[
                    OpenApiExample(
                        "필수 계정",
                        value={
                            "detail": "휴대폰 인증이 필요한 계정은 번호를 삭제할 수 없습니다.",
                            "code": "PHONE_REQUIRED",
                        },
                    )
                ],
            ),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def delete(self, request):
        user = request.user
        # 번호가 비었을 때의 판정을 보려면 '지금 비어 있다' 고 가정해야 한다 —
        # phone_verification_required 는 phone_verified 를 먼저 보기 때문이다.
        if not phone_reward.is_legacy_user(user):
            return _error(
                "휴대폰 인증이 필요한 계정은 번호를 삭제할 수 없습니다.",
                "PHONE_REQUIRED",
                status.HTTP_409_CONFLICT,
            )

        user.phone = ""
        user.phone_verified_at = None
        user.phone_source = ""
        user.sms_marketing_opt_in = False
        user.sms_marketing_opt_in_at = None
        user.save(
            update_fields=[
                "phone",
                "phone_verified_at",
                "phone_source",
                "sms_marketing_opt_in",
                "sms_marketing_opt_in_at",
            ]
        )
        logger.info("phone deleted: user=%s", user.id)
        return Response(
            {"detail": "휴대폰 번호를 삭제했습니다.", "user": UserSerializer(user).data},
            status=status.HTTP_200_OK,
        )


# ──────────────────────────────────────────────
# 확인
# ──────────────────────────────────────────────


class PhoneVerifyView(APIView):
    """인증번호 확인 → 번호 등록 + 보상 지급."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "phone_verify"

    @extend_schema(
        tags=["authentication"],
        summary="휴대폰 인증번호 확인",
        description="""
## 개요
문자로 받은 **6자리 인증번호**를 확인해 휴대폰 번호를 등록합니다. 성공하면
`user.phone_verified` 가 `true` 가 되고, 대상자에게는 **보상이 즉시 지급**됩니다.

## 사용 시나리오
`POST /api/v1/auth/me/phone/` 직후 코드 입력 화면.

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
1. 진행 중인 인증 건이 없으면 400 `NO_PENDING_PHONE_VERIFICATION` → 발송부터 다시.
2. 만료(기본 3분)면 400 `PHONE_CODE_EXPIRED`.
3. 코드가 틀리면 400 `PHONE_CODE_INVALID` + `attempts_left`.
   **5회 틀리면 그 인증 건은 죽습니다**(`attempts_left: 0`) → 재발송해야 합니다.
   서버는 코드를 평문으로 들고 있지 않으므로(HMAC) 재발송 외의 복구는 없습니다.
4. 성공 → `user.phone` 등록, `phone_source = "sms"`,
   `sms_marketing_opt_in` 을 보냈다면 동의 시각과 함께 기록.
5. **보상 지급**(`reward`) — 지급 대상·금액 판정은 서버가 합니다.
   | `kind` | 뜻 |
   |---|---|
   | `trial_extended` | 진행 중인 프로 체험을 `days` 일 연장했습니다 |
   | `trial_granted` | 프로 체험(30일)을 새로 시작했습니다 |
   | `paid_extended` | 다음 결제일을 `days` 일 미뤘습니다 |
   | `none` | 지급 없음. `reason` 에 사유 (`new_signup` = 인증이 필수인 신규 가입자라 애초에 보상 대상이 아님) |

## 요청 바디
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `code` | ✅ | string(6) | 문자로 받은 6자리 숫자 |
| `sms_marketing_opt_in` | | boolean | 광고성 문자·알림톡 수신 동의. 기본 `false` |

## 응답 (200)
```json
{
  "detail": "휴대폰 인증이 완료되었습니다.",
  "user": { "phone": "010-1234-5678", "phone_verified": true, "phone_verification_required": false },
  "reward": { "kind": "trial_extended", "days": 7, "reason": "", "trial_ends_at": "2026-11-16T00:00:00+09:00" }
}
```

## 주의사항
- `sms_marketing_opt_in` 은 **광고성** 발송에만 쓰입니다. 체험 종료·결제 실패 같은
  정보성 알림톡은 동의 없이도 발송됩니다(정보통신망법상 광고가 아님). 동의 체크박스
  문구에 "(선택)" 을 반드시 표기하고, 미체크를 기본값으로 두세요.
- 스로틀 **10회/분**(사용자 기준) — 무차별 대입 방어. 시도 횟수 제한(5회)과 별개입니다.
- 보상은 **1인 1회**이며 같은 번호로 두 번 받을 수 없습니다. 번호를 삭제했다 다시
  등록해도 재지급되지 않습니다.

## 사용 예시
```javascript
const res = await fetch('/api/v1/auth/me/phone/verify/', {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ code: '123456', sms_marketing_opt_in: true }),
});
const data = await res.json();
if (!res.ok && data.code === 'PHONE_CODE_INVALID') {
  setHint(`${data.attempts_left}회 남았습니다`);
}
```
        """,
        request=PhoneVerifySerializer,
        responses={
            200: OpenApiResponse(
                response=UserSerializer,
                description="인증 완료",
                examples=[
                    OpenApiExample(
                        "완료 + 보상",
                        value={
                            "detail": "휴대폰 인증이 완료되었습니다.",
                            "user": {
                                "phone": "010-1234-5678",
                                "phone_verified": True,
                                "phone_verification_required": False,
                            },
                            "reward": {
                                "kind": "trial_extended",
                                "days": 7,
                                "reason": "",
                                "trial_ends_at": "2026-11-16T00:00:00+09:00",
                            },
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="진행 중인 인증 없음 / 만료 / 코드 불일치",
                examples=[
                    OpenApiExample(
                        "코드 불일치",
                        value={
                            "detail": "인증번호가 올바르지 않습니다.",
                            "code": "PHONE_CODE_INVALID",
                            "attempts_left": 3,
                        },
                    ),
                    OpenApiExample(
                        "만료",
                        value={
                            "detail": "인증번호가 만료되었습니다. 다시 받아 주세요.",
                            "code": "PHONE_CODE_EXPIRED",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            429: OpenApiResponse(description="요청이 너무 잦습니다 (사용자 기준 10/min)"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def post(self, request):
        serializer = PhoneVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        code = serializer.validated_data["code"].strip()
        opt_in = bool(serializer.validated_data.get("sms_marketing_opt_in"))

        user = request.user
        now = timezone.now()

        with transaction.atomic():
            row = (
                PhoneVerification.objects.select_for_update()
                .filter(user=user, verified_at__isnull=True, invalidated_at__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if row is None:
                return _error(
                    "진행 중인 휴대폰 인증이 없습니다. 인증번호를 다시 받아 주세요.",
                    "NO_PENDING_PHONE_VERIFICATION",
                    status.HTTP_400_BAD_REQUEST,
                )
            if row.expires_at <= now:
                row.invalidated_at = now
                row.save(update_fields=["invalidated_at"])
                return _error(
                    "인증번호가 만료되었습니다. 다시 받아 주세요.",
                    "PHONE_CODE_EXPIRED",
                    status.HTTP_400_BAD_REQUEST,
                )

            # ⚠️ 시도 횟수는 **비교 전에** 올린다. 뒤에 올리면 예외·타임아웃으로 빠져나간
            #    요청이 공짜 시도가 되어 제한이 무력화된다.
            row.attempts += 1
            limit = phone_guard.max_attempts()

            from .phone import code_matches

            if not code_matches(row.phone, code, row.code_hash):
                attempts_left = max(0, limit - row.attempts)
                if attempts_left == 0:
                    row.invalidated_at = now
                row.save(update_fields=["attempts", "invalidated_at"])
                logger.info(
                    "phone_otp mismatch: user=%s attempts=%s/%s", user.id, row.attempts, limit
                )
                return _error(
                    (
                        "인증번호가 올바르지 않습니다."
                        if attempts_left
                        else "인증번호를 5회 잘못 입력했습니다. 인증번호를 다시 받아 주세요."
                    ),
                    "PHONE_CODE_INVALID",
                    status.HTTP_400_BAD_REQUEST,
                    attempts_left=attempts_left,
                )

            row.verified_at = now
            row.save(update_fields=["attempts", "verified_at"])

            user.phone = row.phone
            user.phone_verified_at = now
            user.phone_source = "sms"
            fields = ["phone", "phone_verified_at", "phone_source"]
            if opt_in and not user.sms_marketing_opt_in:
                user.sms_marketing_opt_in = True
                user.sms_marketing_opt_in_at = now
                fields += ["sms_marketing_opt_in", "sms_marketing_opt_in_at"]
            user.save(update_fields=fields)

        # 보상은 번호가 확정된 **뒤에** — 같은 번호 중복 지급 검사가 user.phone 을 본다.
        reward = phone_reward.grant(user)
        logger.info(
            "phone verified: user=%s phone=%s reward=%s",
            user.id,
            mask_phone(user.phone),
            reward.kind,
        )

        user.refresh_from_db()
        return Response(
            {
                "detail": "휴대폰 인증이 완료되었습니다.",
                "user": UserSerializer(user).data,
                "reward": reward.as_dict(),
            },
            status=status.HTTP_200_OK,
        )
