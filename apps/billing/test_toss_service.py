"""TossBillingClient 단위 테스트 — httpx.MockTransport로 실 네트워크 없이 검증."""

import base64
import json

import httpx
import pytest

from apps.billing import toss_service
from apps.billing.toss_service import TossBillingClient, TossError, TossNetworkError

TEST_SECRET = "test_sk_unittest_secret"


@pytest.fixture(autouse=True)
def toss_settings(settings):
    settings.TOSS_SECRET_KEY = TEST_SECRET
    settings.TOSS_API_BASE = "https://api.tosspayments.com"
    yield
    toss_service._transport = None


def _install(handler):
    toss_service._transport = httpx.MockTransport(handler)


def test_basic_auth_header_has_trailing_colon():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={})

    _install(handler)
    TossBillingClient.get_payment("pay_key_1")

    expected = base64.b64encode(f"{TEST_SECRET}:".encode()).decode()
    assert captured["auth"] == f"Basic {expected}"


def test_charge_sends_idempotency_key_and_body():
    captured = {}

    def handler(request):
        captured["idem"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"status": "DONE", "paymentKey": "pk_1"})

    _install(handler)
    result = TossBillingClient.charge(
        billing_key="bk_secret",
        customer_key="tf_abc",
        amount=9900,
        order_id="tfsub-abc-20260801-a0",
        order_name="턴플로우 프로 월간 구독",
        idempotency_key="idem-uuid-1",
        customer_email="user@example.com",
    )

    assert result["status"] == "DONE"
    assert captured["idem"] == "idem-uuid-1"
    assert captured["url"].endswith("/v1/billing/bk_secret")
    assert captured["body"]["amount"] == 9900
    assert captured["body"]["customerKey"] == "tf_abc"
    assert captured["body"]["orderId"] == "tfsub-abc-20260801-a0"


def test_api_error_raises_toss_error_with_code():
    def handler(request):
        return httpx.Response(400, json={"code": "REJECT_CARD_PAYMENT", "message": "한도 초과"})

    _install(handler)
    with pytest.raises(TossError) as exc_info:
        TossBillingClient.charge(
            billing_key="bk",
            customer_key="ck",
            amount=100,
            order_id="o1",
            order_name="n",
            idempotency_key="i1",
        )

    err = exc_info.value
    assert err.code == "REJECT_CARD_PAYMENT"
    assert "한도 초과" in err.message
    assert err.http_status == 400
    assert not isinstance(err, TossNetworkError)


def test_timeout_raises_network_error():
    def handler(request):
        raise httpx.ConnectTimeout("boom")

    _install(handler)
    with pytest.raises(TossNetworkError):
        TossBillingClient.charge(
            billing_key="bk",
            customer_key="ck",
            amount=100,
            order_id="o1",
            order_name="n",
            idempotency_key="i1",
        )


def test_get_retries_once_on_network_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("first fails")
        return httpx.Response(200, json={"status": "DONE"})

    _install(handler)
    result = TossBillingClient.get_payment("pk")
    assert result["status"] == "DONE"
    assert calls["n"] == 2


def test_charge_does_not_auto_retry_on_network_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("always fails")

    _install(handler)
    with pytest.raises(TossNetworkError):
        TossBillingClient.charge(
            billing_key="bk",
            customer_key="ck",
            amount=100,
            order_id="o1",
            order_name="n",
            idempotency_key="i1",
        )
    assert calls["n"] == 1


def test_issue_billing_key_posts_auth_key():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "billingKey": "bk_new",
                "customerKey": "tf_abc",
                "cardCompany": "현대",
                "cardNumber": "433012******123*",
            },
        )

    _install(handler)
    result = TossBillingClient.issue_billing_key("auth_key_x", "tf_abc")
    assert result["billingKey"] == "bk_new"
    assert captured["body"] == {"authKey": "auth_key_x", "customerKey": "tf_abc"}


def test_sensitive_values_not_logged(caplog):
    """오류 로그에 빌링키/시크릿/카드번호가 노출되지 않아야 한다."""

    def handler(request):
        return httpx.Response(400, json={"code": "INVALID_CARD", "message": "카드 오류"})

    _install(handler)
    with caplog.at_level("DEBUG"), pytest.raises(TossError):
        TossBillingClient.charge(
            billing_key="bk_super_secret_billing_key",
            customer_key="tf_customer_secret",
            amount=100,
            order_id="o1",
            order_name="n",
            idempotency_key="i1",
        )

    log_text = caplog.text
    assert "bk_super_secret_billing_key" not in log_text
    assert "tf_customer_secret" not in log_text
    assert TEST_SECRET not in log_text
