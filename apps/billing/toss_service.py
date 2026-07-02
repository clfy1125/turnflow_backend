"""
TossPayments 빌링(정기결제) API 클라이언트.

- 인증: Basic base64("{TOSS_SECRET_KEY}:") — 시크릿 키 뒤 콜론 필수
- 멱등: 모든 POST에 Idempotency-Key 헤더 지원(15일 보관). 승인(charge)은
  호출측(갱신 태스크)이 키를 소유·재사용해 이중 과금을 방지한다.
- 재시도: charge는 자동 재시도 금지(결과 모호 시 TossNetworkError로 구분해
  호출측이 동일 멱등키로 재시도). GET 계열만 1회 재시도.
- 보안: billingKey / authKey / secretKey / 카드번호 / customerKey 는
  절대 로그에 남기지 않는다.

라이브 전환 = TOSS_SECRET_KEY 를 live_sk_* 로 교체하면 끝 (코드 변경 없음).
"""

import base64
import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# 테스트에서 httpx.MockTransport 주입용 (None이면 실제 네트워크)
_transport = None


class TossError(Exception):
    """토스 API가 명시적으로 거절한 오류 (승인 거절, 잘못된 요청 등)."""

    def __init__(self, code: str, message: str, http_status: int = 0, raw: dict | None = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.raw = raw or {}
        super().__init__(f"[{code}] {message}")


class TossNetworkError(TossError):
    """타임아웃/커넥션 오류 — 결제가 됐는지 '모호'한 상태.

    호출측은 반드시 TossError(확정 실패)와 구분 처리해야 한다:
    동일 Idempotency-Key로 재시도하거나 조회 API로 실상태를 확정할 것.
    """

    def __init__(self, message: str):
        super().__init__(code="NETWORK_ERROR", message=message)


class TossBillingClient:
    """토스 빌링키 발급/승인/취소/조회 REST 래퍼."""

    TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

    # ── 내부 공통 ──

    @classmethod
    def _base_url(cls) -> str:
        return getattr(settings, "TOSS_API_BASE", "https://api.tosspayments.com").rstrip("/")

    @classmethod
    def _auth_header(cls) -> dict:
        # .strip(): docker-compose environment 로 주입되면 앞뒤 공백이 남을 수 있어
        # base64 인코딩 전에 제거 (공백이 섞이면 UNAUTHORIZED_KEY).
        secret = (settings.TOSS_SECRET_KEY or "").strip()
        token = base64.b64encode(f"{secret}:".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    @staticmethod
    def _safe_path(path: str) -> str:
        """로그용 경로 — 빌링키가 path에 들어가는 승인 호출은 마스킹."""
        if path.startswith("/v1/billing/") and not path.startswith("/v1/billing/authorizations"):
            return "/v1/billing/****"
        return path

    @classmethod
    def _request(
        cls,
        method: str,
        path: str,
        json_body: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        headers = cls._auth_header()
        headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        safe_path = cls._safe_path(path)

        try:
            with httpx.Client(timeout=cls.TIMEOUT, transport=_transport) as client:
                resp = client.request(
                    method, f"{cls._base_url()}{path}", json=json_body, headers=headers
                )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning(
                "Toss API 네트워크 오류: %s %s (%s)", method, safe_path, type(e).__name__
            )
            raise TossNetworkError(f"{type(e).__name__} on {method} {safe_path}") from e

        try:
            data = resp.json()
        except ValueError:
            data = {}

        if resp.is_success:
            return data

        code = data.get("code", f"HTTP_{resp.status_code}")
        message = data.get("message", "토스 API 오류")
        logger.warning(
            "Toss API 오류: %s %s → %s [%s] %s",
            method,
            safe_path,
            resp.status_code,
            code,
            message,
        )
        raise TossError(code=code, message=message, http_status=resp.status_code, raw=data)

    # ── 빌링키 발급 ──

    @classmethod
    def issue_billing_key(cls, auth_key: str, customer_key: str) -> dict:
        """SDK requestBillingAuth 성공 후 받은 authKey로 빌링키 발급.

        응답: {billingKey, customerKey, cardCompany, cardNumber(마스킹), card{...}, ...}
        빌링키는 재조회 불가 — 반드시 즉시 암호화 저장할 것.
        """
        return cls._request(
            "POST",
            "/v1/billing/authorizations/issue",
            {"authKey": auth_key, "customerKey": customer_key},
        )

    @classmethod
    def issue_billing_key_by_card(
        cls,
        customer_key: str,
        card_number: str,
        card_expiration_year: str,
        card_expiration_month: str,
        customer_identity_number: str,
        card_password: str | None = None,
    ) -> dict:
        """카드 정보 직접 입력으로 빌링키 발급 — dev 전용 헬퍼.

        테스트 키에서는 카드번호 앞 6자리(BIN)만 유효하면 등록된다.
        라이브에서는 별도 계약(비인증 결제 + PCI-DSS)이 필요하므로
        TOSS_DEV_CARD_AUTH_ENABLED 게이트 밖에서 호출 금지.
        """
        body = {
            "customerKey": customer_key,
            "cardNumber": card_number,
            "cardExpirationYear": card_expiration_year,
            "cardExpirationMonth": card_expiration_month,
            "customerIdentityNumber": customer_identity_number,
        }
        if card_password:
            body["cardPassword"] = card_password
        return cls._request("POST", "/v1/billing/authorizations/card", body)

    # ── 승인 (과금) ──

    @classmethod
    def charge(
        cls,
        billing_key: str,
        customer_key: str,
        amount: int,
        order_id: str,
        order_name: str,
        idempotency_key: str,
        customer_email: str | None = None,
        customer_name: str | None = None,
    ) -> dict:
        """빌링키 자동결제 승인. 성공 시 Payment 객체(status=DONE) 반환.

        idempotency_key 필수 — 동일 키 재호출 시 토스가 첫 응답을 돌려줘
        이중 과금이 방지된다(15일). 자동 재시도는 하지 않는다.
        """
        body = {
            "customerKey": customer_key,
            "amount": amount,
            "orderId": order_id,
            "orderName": order_name,
        }
        if customer_email:
            body["customerEmail"] = customer_email
        if customer_name:
            body["customerName"] = customer_name
        return cls._request(
            "POST", f"/v1/billing/{billing_key}", body, idempotency_key=idempotency_key
        )

    # ── 취소 / 환불 ──

    @classmethod
    def cancel_payment(
        cls,
        payment_key: str,
        cancel_reason: str,
        cancel_amount: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """결제 취소(환불). cancel_amount 생략 시 전액 취소."""
        body: dict = {"cancelReason": cancel_reason[:200]}
        if cancel_amount is not None:
            body["cancelAmount"] = cancel_amount
        return cls._request(
            "POST",
            f"/v1/payments/{payment_key}/cancel",
            body,
            idempotency_key=idempotency_key,
        )

    @classmethod
    def delete_billing_key(cls, billing_key: str, customer_key: str) -> dict:
        """자동결제 해지(빌링키 삭제) — best-effort.

        실질 해지는 우리 스케줄러가 승인 호출을 멈추는 것이고, 삭제는 위생 조치.
        실패해도 과금 위험이 없으므로 호출측은 로그만 남기고 진행한다.
        (경로는 실 API 검증됨: DELETE /v1/billing/{billingKey} + body customerKey)
        """
        return cls._request("DELETE", f"/v1/billing/{billing_key}", {"customerKey": customer_key})

    # ── 조회 ──

    @classmethod
    def get_payment(cls, payment_key: str) -> dict:
        """paymentKey로 결제 조회 — 웹훅 검증(재조회)에 사용."""
        return cls._get_with_retry(f"/v1/payments/{payment_key}")

    @classmethod
    def get_payment_by_order_id(cls, order_id: str) -> dict:
        """orderId로 결제 조회 — 모호 실패(PENDING) reconcile에 사용."""
        return cls._get_with_retry(f"/v1/payments/orders/{order_id}")

    @classmethod
    def _get_with_retry(cls, path: str) -> dict:
        """GET은 부수효과가 없으므로 네트워크 오류 시 1회 재시도."""
        try:
            return cls._request("GET", path)
        except TossNetworkError:
            return cls._request("GET", path)
