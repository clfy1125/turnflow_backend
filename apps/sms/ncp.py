"""네이버 클라우드 SENS(Simple & Easy Notification Service) 문자 발송 클라이언트.

2026-10-12 알리고에서 교체. 역할은 전과 같다 — **외부와 말하는 부분만** 담당하고,
"누구에게 보낼 자격이 있는가"는 ``apps.authentication.phone_guard``, "무엇을 보낼
것인가"(본문·로그)는 ``apps.sms.services`` 가 판단한다.

## 규격

``POST https://sens.apigw.ntruss.com/sms/v2/services/{serviceId}/messages``

| 헤더 | 값 |
|---|---|
| ``x-ncp-apigw-timestamp`` | epoch **밀리초** |
| ``x-ncp-iam-access-key`` | Access Key ID |
| ``x-ncp-apigw-signature-v2`` | base64(HMAC-SHA256(secretKey, StringToSign)) |

``StringToSign = "{method} {uri}\\n{timestamp}\\n{accessKey}"`` — ``uri`` 는 **쿼리까지 포함한
경로**이고, 서명에 쓴 문자열과 실제 요청 경로가 **바이트 단위로 같아야** 한다.
서비스 ID 에 ``:`` 가 들어가는데(``ncp:sms:kr:...``) RFC 3986 상 경로 세그먼트에서 합법이라
인코딩하지 않는다. httpx 도 건드리지 않는다 — 한쪽만 인코딩하면 401 이 나고, 그 401 은
"키가 틀렸다"처럼 보여서 원인을 찾기 매우 어렵다.

## 알리고와 달라진 것 — 코드에 영향 있는 차이

1. **선불 잔액이 없다.** 알리고는 포인트를 미리 사서 ``/remain/`` 으로 잔여 건수를 볼 수
   있었지만 SENS 는 후불이다. 대신 **일일·월간 이용 한도**가 있다(가입 시 국내 500건/일,
   10,000건/월). 그래서 "잔액 감시"는 의미가 없어졌고, ``sms.check_health`` 가
   **실패율**을 보는 쪽으로 바뀌었다 — 그쪽이 "가입이 막혔나"에 더 직접적인 답이다.
   ⚠️ ``PHONE_VERIFY_GLOBAL_DAILY_CAP`` 은 **NCP 일일 한도보다 낮게** 두어야 한다.
      높게 두면 NCP 가 먼저 막는데, 그 거절은 우리 쪽에서 평범한 발송 실패로 보인다.
2. **202 는 '접수'이지 '도착'이 아니다.** 실제 수신 결과는 ``requestId`` 로 따로 조회한다
   (``lookup_result``). OTP 화면에는 접수만으로 충분하지만, "안 왔다" CS 를 추적하려면
   ``SmsLog.provider_message_id`` 에 담아둔 requestId 가 유일한 단서다.
3. 오류가 **HTTP 상태로 온다** — 알리고처럼 200 안에 숨은 음수 코드를 파헤칠 필요가 없다.

⚠️ 키를 로그에 남기지 않는다. 서명 실패 디버깅이 필요해도 ``StringToSign`` 을 통째로
   찍지 말 것 — accessKey 가 그 안에 있다 (지침 14).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

API_BASE = "https://sens.apigw.ntruss.com"

# 사용자가 "인증번호 받기" 앞에서 기다리는 동기 경로다. 느리면 차라리 실패로 돌려
# 재시도를 유도한다(알리고 때와 같은 판단).
TIMEOUT = httpx.Timeout(8.0, connect=4.0)

#: SMS 한 건의 한도(byte, EUC-KR 기준 90). 넘으면 LMS 로 보내야 하고 단가가 올라간다.
SMS_BYTE_LIMIT = 90


class SmsProviderError(Exception):
    """문자 발송 실패. ``code`` 는 프론트가 분기할 머신 키."""

    def __init__(self, code: str, message: str, *, provider_code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider_code = provider_code
        self.status = status


@dataclass(frozen=True)
class SendResult:
    message_id: str  # SENS requestId
    result_code: int  # HTTP 상태 (202=접수). SmsLog.result_code 와 호환되게 int 로 둔다
    message: str
    mocked: bool = False


def is_mock_mode() -> bool:
    """실제 발송 대신 로그만 남기는 모드 (``INSTAGRAM_MOCK_MODE`` 와 같은 규약)."""
    return bool(getattr(settings, "SMS_MOCK_MODE", True))


def _credentials() -> tuple[str, str, str, str]:
    return (
        (getattr(settings, "NCP_ACCESS_KEY", "") or "").strip(),
        (getattr(settings, "NCP_SECRET_KEY", "") or "").strip(),
        (getattr(settings, "NCP_SMS_SERVICE_ID", "") or "").strip(),
        (getattr(settings, "NCP_SMS_SENDER", "") or "").strip(),
    )


def is_configured() -> bool:
    return all(_credentials())


def sms_byte_length(text: str) -> int:
    """EUC-KR 기준 byte 길이 — 한글 2 byte. SMS/LMS 판정과 같은 기준."""
    return len((text or "").encode("euc-kr", errors="replace"))


def _sign(method: str, uri: str, timestamp: str, access_key: str, secret_key: str) -> str:
    """SENS signature v2. ``uri`` 는 요청 경로와 **글자 그대로** 같아야 한다."""
    message = f"{method} {uri}\n{timestamp}\n{access_key}"
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _headers(method: str, uri: str) -> dict:
    access_key, secret_key, _, _ = _credentials()
    timestamp = str(int(time.time() * 1000))
    return {
        "Content-Type": "application/json; charset=utf-8",
        "x-ncp-apigw-timestamp": timestamp,
        "x-ncp-iam-access-key": access_key,
        "x-ncp-apigw-signature-v2": _sign(method, uri, timestamp, access_key, secret_key),
    }


def _error_body(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def send_sms(*, to: str, body: str, title: str = "") -> SendResult:
    """문자 1건 발송. 실패하면 ``SmsProviderError``.

    ``to`` 는 숫자만 남긴 국내 번호(``01012345678``). 정규화는 호출부
    (``apps.authentication.phone.normalize_phone``)의 책임이다.
    """
    access_key, _, service_id, sender = _credentials()

    if is_mock_mode() or not is_configured():
        if not is_mock_mode():
            # 설정 누락인데 Mock 이 아니다 → 조용히 넘어가면 "인증번호가 안 와요" CS 가
            # 원인 불명으로 쌓인다. 명시적으로 시끄럽게 남긴다.
            logger.error(
                "ncp_sms: 자격증명 미설정 (NCP_ACCESS_KEY/SECRET_KEY/SMS_SERVICE_ID/SENDER) "
                "— 발송을 건너뛴다"
            )
        logger.info("ncp_sms[mock]: to=%s***%s len=%s", to[:3], to[-4:], len(body))
        return SendResult(message_id="mock", result_code=202, message="mock", mocked=True)

    uri = f"/sms/v2/services/{service_id}/messages"
    is_long = sms_byte_length(body) > SMS_BYTE_LIMIT
    payload = {
        "type": "LMS" if is_long else "SMS",
        # COMM=일반(정보성) / AD=광고성. OTP 는 항상 COMM 이다.
        # ⚠️ 광고성을 COMM 으로 보내면 정보통신망법 위반이다 — 광고 발송 경로를 새로
        #    만들 때는 반드시 "AD" 로 두고 (광고)·무료수신거부 표기를 함께 넣을 것.
        "contentType": "COMM",
        "countryCode": "82",
        "from": sender,
        "content": body,
        "messages": [{"to": to}],
    }
    if is_long:
        # LMS 는 제목이 필수는 아니지만, 없으면 단말에 발신번호만 뜨는 기기가 있다.
        payload["subject"] = (title or "인증번호")[:40]

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(API_BASE + uri, headers=_headers("POST", uri), json=payload)
    except httpx.HTTPError as exc:
        logger.warning("ncp_sms: transport error: %s", exc)
        raise SmsProviderError(
            "SMS_UNAVAILABLE", "문자 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from exc

    if resp.status_code not in (200, 202):
        err = _error_body(resp)
        code = str(err.get("errorCode") or "")
        # ⚠️ 본문 전체를 찍지 않는다 — 우리가 보낸 수신번호가 되비쳐 나올 수 있다(개인정보).
        logger.error(
            "ncp_sms: send failed status=%s errorCode=%s message=%s",
            resp.status_code,
            code,
            str(err.get("message") or "")[:200],
        )
        if resp.status_code == 429:
            raise SmsProviderError(
                "SMS_CAPACITY_EXCEEDED",
                "일시적으로 인증번호를 보낼 수 없습니다. 잠시 후 다시 시도해 주세요.",
                provider_code=code,
                status=resp.status_code,
            )
        raise SmsProviderError(
            "SMS_SEND_FAILED",
            "문자 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.",
            provider_code=code,
            status=resp.status_code,
        )

    data = _error_body(resp)
    return SendResult(
        message_id=str(data.get("requestId") or ""),
        result_code=resp.status_code,
        message=str(data.get("statusName") or ""),
    )


def lookup_result(request_id: str) -> dict:
    """``requestId`` 로 실제 수신 결과를 조회한다 (CS 추적용).

    202 는 '접수'일 뿐이라 "보냈다고 하는데 안 왔다"를 가릴 유일한 수단이다.
    ⚠️ 실패해도 예외를 던지지 않는다 — 조회 실패가 호출부를 깨뜨릴 이유가 없다.
    """
    access_key, _, service_id, _ = _credentials()
    if not request_id or not is_configured() or is_mock_mode():
        return {}
    uri = f"/sms/v2/services/{service_id}/messages?requestId={request_id}"
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(API_BASE + uri, headers=_headers("GET", uri))
        if resp.status_code != 200:
            logger.warning("ncp_sms: lookup failed status=%s", resp.status_code)
            return {}
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("ncp_sms: lookup error: %s", exc)
        return {}
