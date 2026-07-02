"""
토스페이먼츠 빌링 API views.

1. TossPrepareView       — 카드 등록창 오픈에 필요한 키 발급
2. TossConfirmView       — authKey → 빌링키 발급 + 구독 시작/카드 변경
3. TossWebhookView       — 토스 웹훅 수신 (검증은 재조회 방식, 처리는 Celery)
4. TossDevIssueView      — dev 전용: 카드번호 직접 입력 (Swagger 단독 검증용)
5. ExtraAccountsView     — 프로 추가 IG 계정 구매/축소
"""

import hashlib
import json
import logging

from django.conf import settings
from django.http import Http404, HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TossWebhookLog
from .serializers import (
    ExtraAccountsRequestSerializer,
    PaymentHistorySerializer,
    TossConfirmRequestSerializer,
    TossDevIssueRequestSerializer,
    UserSubscriptionSerializer,
)
from .subscription_utils import ensure_subscription
from .toss_flows import (
    BillingFlowError,
    ChargeDeclinedError,
    ChargePendingError,
    change_extra_accounts,
    confirm_billing,
    ensure_customer_key,
)

logger = logging.getLogger(__name__)


def _flow_error_response(e: BillingFlowError) -> Response:
    body = {"detail": e.detail, **e.extra}
    if isinstance(e, ChargePendingError):
        body["payment"] = PaymentHistorySerializer(e.payment).data
    if isinstance(e, ChargeDeclinedError):
        body["payment"] = PaymentHistorySerializer(e.payment).data
    return Response(body, status=e.status_code)


def _confirm_result_response(result: dict) -> Response:
    return Response(
        {
            "detail": result["detail"],
            "scenario": result["scenario"],
            "subscription": UserSubscriptionSerializer(result["subscription"]).data,
            "payment": (
                PaymentHistorySerializer(result["payment"]).data if result["payment"] else None
            ),
            "first_charge_at": result["first_charge_at"],
        }
    )


class TossPrepareView(APIView):
    """빌링키 등록 준비 — SDK 카드 등록창에 필요한 키 반환"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["결제(토스)"],
        summary="카드 등록 준비 (클라이언트 키 발급)",
        description="""
## 목적
토스페이먼츠 **카드 등록창(requestBillingAuth)** 을 열기 위한 클라이언트 키와
사용자 고유 `customer_key`를 반환합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 사용 시나리오
- 요금제 결제 페이지 진입 시
- 결제 카드 변경 화면 진입 시

## 흐름 (프론트)
```
1. GET /billing/toss/prepare/ → { client_key, customer_key }
2. 토스 SDK v2 로드: <script src="https://js.tosspayments.com/v2/standard"></script>
3. const payment = TossPayments(client_key).payment({ customerKey: customer_key })
4. payment.requestBillingAuth({ method: 'CARD', successUrl, failUrl })
5. successUrl 쿼리로 authKey 수신 → POST /billing/toss/confirm/ 호출
```

## 응답 필드
| 필드 | 타입 | 설명 |
|------|------|------|
| `client_key` | string | 토스 클라이언트 키 (SDK 초기화용, 공개 가능) |
| `customer_key` | string | 사용자 고유 키. SDK의 customerKey로 그대로 사용 |
| `customer_email` | string | 사용자 이메일 (SDK customerEmail 파라미터용) |
| `has_billing_key` | bool | 이미 등록된 카드가 있는지 |
| `card_company` | string | 등록된 카드사 (없으면 빈 문자열) |
| `card_number_masked` | string | 등록된 카드 마스킹 번호 |

## 에러
| 코드 | 원인 |
|------|------|
| 401 | 토큰 없음/만료 |
| 500 | 서버 오류 |
        """,
        responses={
            200: OpenApiResponse(
                description="카드 등록창 오픈에 필요한 키",
                examples=[
                    OpenApiExample(
                        "카드 미등록 사용자",
                        value={
                            "client_key": "test_ck_4vZnjEJeQVxJzDoab4d8PmOoBN0k",
                            "customer_key": "tf_2f6f8a0f0f0a4b0f8f0a4b0f8f0a4b0f",
                            "customer_email": "user@example.com",
                            "has_billing_key": False,
                            "card_company": "",
                            "card_number_masked": "",
                        },
                    ),
                    OpenApiExample(
                        "카드 등록된 사용자 (카드 변경 시)",
                        value={
                            "client_key": "test_ck_4vZnjEJeQVxJzDoab4d8PmOoBN0k",
                            "customer_key": "tf_2f6f8a0f0f0a4b0f8f0a4b0f8f0a4b0f",
                            "customer_email": "user@example.com",
                            "has_billing_key": True,
                            "card_company": "현대",
                            "card_number_masked": "433012******123*",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
        },
    )
    def get(self, request):
        sub = ensure_subscription(request.user)
        customer_key = ensure_customer_key(sub)
        return Response(
            {
                "client_key": (settings.TOSS_CLIENT_KEY or "").strip(),
                "customer_key": customer_key,
                "customer_email": request.user.email,
                "has_billing_key": sub.has_billing_key,
                "card_company": sub.card_company,
                "card_number_masked": sub.card_number_masked,
            }
        )


class TossConfirmView(APIView):
    """빌링키 등록 확정 + 구독 시작/카드 변경"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["결제(토스)"],
        summary="카드 등록 확정 + 구독 시작",
        description="""
## 목적
카드 등록창(requestBillingAuth) 성공 후 받은 `authKey`로 **빌링키를 발급·저장**하고,
`plan_name`에 따라 구독을 시작하거나 결제 카드를 변경합니다.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `auth_key` | ✅ | string | successUrl 쿼리로 받은 authKey (일회성) |
| `plan_name` | 선택 | `basic`/`pro` | 구독 시작할 플랜. **생략 시 카드 변경**으로 동작 |
| `referral_code` | 선택 | string | 제휴 코드 — pro 최초 구독 시 무료 체험 +30일 |
| `extra_ig_accounts` | 선택 | int | pro 전용 추가 IG 계정 수 (계정당 +9,900원/월) |

## 시나리오별 동작
| 시나리오 | 조건 | 동작 |
|----------|------|------|
| **무료 체험 시작** | `plan_name=pro` + 체험 미사용 | 과금 없이 즉시 프로 활성화. **30일 후 첫 자동결제** (제휴코드 시 60일 후). 응답 `first_charge_at` 참고 |
| **즉시 결제** | `plan_name=basic`, 또는 체험 이미 사용한 pro 재구독 | 등록 즉시 첫 달 요금 결제 + 30일 구독 시작 |
| **카드 변경** | `plan_name` 생략 (유료 구독자) | 플랜/기간 변경 없이 카드만 교체. 미납(past_due)이면 자동 재시도 |
| **체험 중 카드 등록** | 무카드 제휴 체험 중 | 카드만 부착, **체험 기간 불변** (적층 없음) |

## 무료 체험 정책
- 프로 최초 구독 1인 1회 (재구독 시에는 즉시 결제)
- 체험 중 해지하면 과금 없이 체험 종료일까지 이용
- 제휴 코드는 1인 1회, pro 체험 시작 시에만 입력 가능

## 프론트엔드 통합
```typescript
// successUrl 페이지에서 (쿼리로 authKey, customerKey 수신)
const params = new URLSearchParams(location.search);
const res = await fetch('/api/v1/billing/toss/confirm/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${accessToken}`,
  },
  body: JSON.stringify({
    auth_key: params.get('authKey'),
    plan_name: 'pro',
    referral_code: inputCode || undefined,
  }),
});
const data = await res.json();
if (res.status === 200) {
  // data.scenario: trial | charge_now | card_change | attach_only
  // trial이면 data.first_charge_at 에 첫 결제 예정일
} else if (res.status === 402) {
  // 카드 거절 — data.detail 안내 후 다른 카드로 재시도
} else if (res.status === 202) {
  // 결제 결과 확인 중 — 잠시 후 결제 내역 조회
}
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | authKey 오류/만료, 카드 등록 실패, 제휴코드 무효, 이미 유료 구독 중, 잘못된 플랜 |
| 401 | 토큰 없음/만료 |
| 402 | 즉시 결제 시나리오에서 카드 승인 거절 (빌링키는 등록됨) |
| 404 | 존재하지 않는 플랜 |
| 202 | 결제 결과 확인 중 (네트워크 모호 — 잠시 후 결제 내역 확인) |
| 502 | 토스 API 통신 오류 |
        """,
        request=TossConfirmRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="구독 시작/카드 변경 완료",
                examples=[
                    OpenApiExample(
                        "무료 체험 시작 (pro)",
                        value={
                            "detail": "무료 체험이 시작되었습니다. 체험 종료 후 첫 결제가 진행됩니다.",
                            "scenario": "trial",
                            "subscription": {
                                "plan": {"name": "pro", "display_name": "프로"},
                                "status": "trialing",
                                "current_period_end": "2026-08-01T00:00:00Z",
                                "card_company": "현대",
                                "card_number_masked": "433012******123*",
                                "monthly_amount_snapshot": 9900,
                            },
                            "payment": None,
                            "first_charge_at": "2026-08-01T00:00:00Z",
                        },
                    ),
                    OpenApiExample(
                        "즉시 결제 (basic)",
                        value={
                            "detail": "베이직 플랜 구독이 시작되었습니다.",
                            "scenario": "charge_now",
                            "subscription": {
                                "plan": {"name": "basic"},
                                "status": "active",
                                "monthly_amount_snapshot": 3900,
                            },
                            "payment": {
                                "amount": 3900,
                                "status": "paid",
                                "receipt_url": "https://dashboard.tosspayments.com/...",
                            },
                            "first_charge_at": None,
                        },
                    ),
                ],
            ),
            202: OpenApiResponse(description="결제 결과 확인 중 (네트워크 모호)"),
            400: OpenApiResponse(
                description="검증 실패",
                examples=[
                    OpenApiExample(
                        "카드 등록 실패",
                        value={
                            "detail": "카드 등록에 실패했습니다: 유효하지 않은 카드입니다.",
                            "toss_code": "INVALID_CARD_NUMBER",
                        },
                    ),
                    OpenApiExample(
                        "이미 유료 구독",
                        value={
                            "detail": "이미 유료 구독 중입니다. 플랜 변경은 change-plan API, 카드 변경은 plan_name 없이 호출해주세요."
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(description="인증 실패"),
            402: OpenApiResponse(description="카드 승인 거절 (빌링키는 등록됨 — 재시도 가능)"),
            404: OpenApiResponse(description="플랜 없음"),
            502: OpenApiResponse(description="토스 API 통신 오류"),
        },
    )
    def post(self, request):
        serializer = TossConfirmRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = confirm_billing(
                request.user,
                auth_key=data["auth_key"],
                plan_name=data.get("plan_name"),
                referral_code=data.get("referral_code") or None,
                extra_ig_accounts=data.get("extra_ig_accounts") or 0,
            )
        except BillingFlowError as e:
            return _flow_error_response(e)

        return _confirm_result_response(result)


@method_decorator(csrf_exempt, name="dispatch")
class TossWebhookView(APIView):
    """토스 웹훅 수신 — 즉시 200 응답, 처리는 Celery로"""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        tags=["결제(토스)"],
        summary="토스페이먼츠 웹훅 (서버 전용)",
        description="""
## 목적
토스페이먼츠가 결제/빌링키 상태 변경 시 호출하는 웹훅 엔드포인트입니다.
개발자센터 → 웹훅 메뉴에 이 URL을 등록하세요.

## ⚠️ 프론트엔드에서 호출하지 마세요
**토스 서버 → 백엔드** 전용입니다.

## 처리 이벤트
| eventType | 처리 |
|-----------|------|
| `PAYMENT_STATUS_CHANGED` | 미확정(pending) 결제 확정 / 취소 반영 |
| `CANCEL_STATUS_CHANGED` | 환불 반영 (대시보드 수동 취소 포함) — 구독 다운그레이드 |
| `BILLING_DELETED` | 빌링키 무효화 — 구독 갱신 중단 (기간말까지 이용) |

## 보안 (서명 없음 → 재조회 검증)
토스 일반결제 웹훅에는 서명 헤더가 없습니다. 본문을 신뢰하지 않고
**paymentKey로 결제 조회 API를 재호출**해 실제 상태를 확인한 후 반영합니다.
위조 본문은 재조회 단계에서 무시됩니다.

## 멱등
본문 구조 기반 `dedup_key`(unique)로 중복 수신을 차단합니다.
토스는 10초 내 200 응답이 없으면 최대 7회 재전송합니다 —
이 뷰는 수신 즉시 200을 반환하고 실제 처리는 Celery 태스크로 넘깁니다.

## 응답
항상 HTTP 200 (본문 "OK"). 파싱 불가 본문도 200 (재전송 무한루프 방지).
        """,
        request=None,
        responses={200: OpenApiResponse(description="OK — 수신 확인 (처리는 비동기)")},
    )
    def post(self, request):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("토스 웹훅 파싱 불가 본문 수신 (무시)")
            return HttpResponse("OK", status=200)

        event_type = str(payload.get("eventType", ""))
        data = payload.get("data") or {}
        # 구버전 웹훅(PAYMENT_STATUS_CHANGED)은 data 없이 최상위에 필드가 올 수 있음
        if not data and payload.get("status"):
            data = payload

        payment_key = str(data.get("paymentKey", "") or "")
        order_id = str(data.get("orderId", "") or "")
        event_status = str(data.get("status", "") or "")

        # ── dedup_key 계산 + 빌링키 평문 치환 ──
        if event_type == "BILLING_DELETED":
            billing_key = str(data.get("billingKey", "") or "")
            key_hash = hashlib.sha256(billing_key.encode()).hexdigest()
            dedup_key = f"billdel:{key_hash[:32]}"
            data = dict(data)
            data["billingKey"] = f"sha256:{key_hash}"  # 평문 저장 금지
            payload = dict(payload)
            payload["data"] = data
        elif event_type == "CANCEL_STATUS_CHANGED":
            cancels = data.get("cancels") or []
            last_tx = data.get("lastTransactionKey") or (
                cancels[-1].get("transactionKey") if cancels else ""
            )
            dedup_key = (
                f"cancel:{payment_key}:{event_status}:{last_tx or payload.get('createdAt', '')}"
            )
        elif event_type:
            dedup_key = f"{event_type[:20].lower()}:{payment_key or order_id}:{event_status}"
        else:
            logger.warning("토스 웹훅 eventType 없음 (무시): keys=%s", list(payload.keys())[:10])
            return HttpResponse("OK", status=200)

        try:
            log, created = TossWebhookLog.objects.get_or_create(
                dedup_key=dedup_key[:255],
                defaults={
                    "event_type": event_type[:50] or "UNKNOWN",
                    "payment_key": payment_key[:200],
                    "order_id": order_id[:64],
                    "raw_data": payload,
                },
            )
        except Exception:
            logger.exception("토스 웹훅 로그 저장 실패 (200 반환)")
            return HttpResponse("OK", status=200)

        if created:
            from .tasks import process_toss_webhook

            process_toss_webhook.delay(str(log.id))
        else:
            logger.info("토스 웹훅 중복 수신 무시: %s", dedup_key[:100])

        return HttpResponse("OK", status=200)


class TossDevIssueView(APIView):
    """dev 전용 — 카드번호 직접 입력으로 빌링키 발급 (Swagger 단독 검증)"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["결제(토스)"],
        summary="[DEV] 카드번호로 빌링키 발급",
        description="""
## 목적
프론트 SDK 없이 **Swagger/curl만으로 결제 플로우를 검증**하기 위한 개발 전용 API.
카드 정보를 직접 받아 빌링키를 발급한 뒤 confirm과 동일한 구독 플로우를 실행합니다.

## ⚠️ 활성화 조건
`DEBUG=True` **그리고** `TOSS_DEV_CARD_AUTH_ENABLED=True`일 때만 존재합니다.
그 외 환경에서는 404를 반환합니다. **운영 배포 시 반드시 비활성.**

## 테스트 카드
토스 테스트 키에서는 카드번호 **앞 6자리(BIN)만 유효**하면 등록됩니다.
예: `4330121111111111` / 유효기간 미래 아무 값 / 생년월일 `900101`

## 요청/응답
`plan_name`, `referral_code`, `extra_ig_accounts` 의미는 confirm API와 동일합니다.

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 카드 정보 오류, 플로우 검증 실패 |
| 401 | 토큰 없음/만료 |
| 402 | 즉시 결제 거절 |
| 404 | dev 헬퍼 비활성 (DEBUG/플래그 꺼짐) |
| 502 | 토스 API 통신 오류 |
        """,
        request=TossDevIssueRequestSerializer,
        responses={
            200: OpenApiResponse(description="confirm과 동일한 응답"),
            202: OpenApiResponse(description="결제 결과 확인 중"),
            400: OpenApiResponse(description="검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            402: OpenApiResponse(description="카드 승인 거절"),
            404: OpenApiResponse(description="dev 헬퍼 비활성"),
            502: OpenApiResponse(description="토스 API 통신 오류"),
        },
    )
    def post(self, request):
        if not (settings.DEBUG and settings.TOSS_DEV_CARD_AUTH_ENABLED):
            raise Http404

        serializer = TossDevIssueRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        dev_card = {
            "card_number": data["card_number"],
            "card_expiration_year": data["card_expiration_year"],
            "card_expiration_month": data["card_expiration_month"],
            "customer_identity_number": data["customer_identity_number"],
        }
        if data.get("card_password"):
            dev_card["card_password"] = data["card_password"]

        try:
            result = confirm_billing(
                request.user,
                dev_card=dev_card,
                plan_name=data.get("plan_name"),
                referral_code=data.get("referral_code") or None,
                extra_ig_accounts=data.get("extra_ig_accounts") or 0,
            )
        except BillingFlowError as e:
            return _flow_error_response(e)

        return _confirm_result_response(result)


class ExtraAccountsView(APIView):
    """프로 추가 IG 계정 구매/축소"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["결제(토스)"],
        summary="추가 IG 계정 구매/축소 (pro)",
        description="""
## 목적
프로 플랜의 **추가 IG 계정 슬롯**을 변경합니다. 기본 1계정 + 추가 계정당 **9,900원/월**.

## 인증
`Authorization: Bearer <access_token>` 헤더 필수 — **프로 플랜 전용**

## 과금 규칙
- **증가**: 증가분 × 9,900원을 **즉시 결제** (비례배분 없음, 한 달치).
  결제 성공 시에만 슬롯이 늘어나며, 이후 매월 갱신 금액에 합산됩니다.
- **감소**: 무과금. 단, 현재 연동된 IG 계정 수가 새 허용량(기본 1 + count) 이하여야
  합니다. 초과 상태면 먼저 연동을 해제해야 합니다.

## 요청 필드
| 필드 | 필수 | 타입 | 설명 |
|------|------|------|------|
| `count` | ✅ | int (0~10) | 변경 후 추가 계정 **총** 수 (현재 값이 아닌 목표 값) |

## 프론트엔드 통합
```typescript
const res = await fetch('/api/v1/billing/extra-accounts/', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${accessToken}`,
  },
  body: JSON.stringify({ count: 2 }),  // 총 3계정 (기본 1 + 추가 2)
});
```

## 에러
| 코드 | 원인 |
|------|------|
| 400 | 프로 플랜 아님, 카드 미등록, 동일 값, 감소 시 연동 수 초과, 미납/해지예약 상태 |
| 401 | 토큰 없음/만료 |
| 402 | 증가분 결제 거절 |
| 202 | 결제 결과 확인 중 |
| 502 | 토스 API 통신 오류 |
        """,
        request=ExtraAccountsRequestSerializer,
        responses={
            200: OpenApiResponse(
                description="변경 완료",
                examples=[
                    OpenApiExample(
                        "추가 구매 성공",
                        value={
                            "detail": "추가 IG 계정이 2개로 변경되었습니다.",
                            "subscription": {
                                "extra_ig_accounts": 2,
                                "plan": {"name": "pro"},
                            },
                            "payment": {"amount": 9900, "status": "paid"},
                            "next_renewal_amount": 29700,
                        },
                    )
                ],
            ),
            202: OpenApiResponse(description="결제 결과 확인 중"),
            400: OpenApiResponse(description="검증 실패"),
            401: OpenApiResponse(description="인증 실패"),
            402: OpenApiResponse(description="결제 거절"),
            502: OpenApiResponse(description="토스 API 통신 오류"),
        },
    )
    def post(self, request):
        serializer = ExtraAccountsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = change_extra_accounts(request.user, serializer.validated_data["count"])
        except BillingFlowError as e:
            return _flow_error_response(e)

        sub = result["subscription"]
        return Response(
            {
                "detail": f"추가 IG 계정이 {sub.extra_ig_accounts}개로 변경되었습니다.",
                "subscription": UserSubscriptionSerializer(sub).data,
                "payment": (
                    PaymentHistorySerializer(result["payment"]).data if result["payment"] else None
                ),
                "next_renewal_amount": sub.renewal_amount,
            }
        )
