"""알리고(Aligo) 문자 발송 — **외부와 말하는 부분만** 담당하는 클라이언트.

``apps/authentication/kakao.py`` 와 같은 역할 분담이다: 여기는 HTTP 만 치고, "누구에게
보낼 자격이 있는가"(쿨다운·일일 상한·본인확인) 는 ``apps.authentication.phone_guard`` 가,
"무엇을 보낼 것인가"(본문·로그) 는 ``apps.sms.services`` 가 판단한다.

## 알리고 규격 (https://smartsms.aligo.in/admin/api/spec.html)

``POST https://apis.aligo.in/send/`` · ``application/x-www-form-urlencoded``

| 파라미터 | 필수 | 설명 |
|---|:--:|---|
| ``key`` | ✅ | API 키 (콘솔에서 발급) |
| ``user_id`` | ✅ | 알리고 로그인 아이디 |
| ``sender`` | ✅ | **사전등록된** 발신번호. 미등록 번호는 거절된다(발신번호 사전등록제) |
| ``receiver`` | ✅ | 수신번호. 쉼표로 최대 1,000명 |
| ``msg`` | ✅ | 본문 1~2,000 byte (90byte 초과 시 LMS) |
| ``msg_type`` | | SMS / LMS / MMS. 생략하면 길이로 자동 판정 |
| ``title`` | | LMS/MMS 제목 (1~44 byte) |
| ``testmode_yn`` | | ``Y`` 면 **실제로 나가지 않고 과금도 없다** |

응답 JSON: ``result_code``(1=성공, 음수=실패) · ``message`` · ``msg_id`` · ``success_cnt``
· ``error_cnt`` · ``msg_type``.

⚠️ **HTTP 200 이어도 실패일 수 있다.** 알리고는 인증 실패·잔액 부족·미등록 발신번호를
   전부 200 + ``result_code<0`` 으로 돌려준다. ``resp.raise_for_status()`` 만 믿으면
   "보냈다고 기록됐는데 아무도 못 받는" 상태가 된다 — 반드시 ``result_code`` 를 본다.

⚠️ **키를 로그에 남기지 않는다.** 알리고 오류 응답은 우리가 보낸 값을 되비추지 않지만,
   요청 payload 를 통째로 찍는 디버그 코드를 넣지 말 것 (지침 14).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

SEND_URL = "https://apis.aligo.in/send/"
REMAIN_URL = "https://apis.aligo.in/remain/"

# 문자 게이트웨이는 보통 1초 안에 답한다. 사용자가 "인증번호 받기" 버튼 앞에서 기다리는
# 동기 경로라 넉넉히 잡지 않는다 — 느리면 차라리 실패로 돌려 재시도를 유도한다.
TIMEOUT = httpx.Timeout(8.0, connect=4.0)

#: SMS 한 건의 한도(byte, EUC-KR 기준 90). 넘으면 알리고가 LMS 로 처리해 과금이 3배가 된다.
SMS_BYTE_LIMIT = 90


class AligoError(Exception):
    """문자 발송 실패. ``code`` 는 우리 쪽 머신 키, ``result_code`` 는 알리고 원본."""

    def __init__(self, code: str, message: str, *, result_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.result_code = result_code


@dataclass(frozen=True)
class SendResult:
    message_id: str
    result_code: int
    message: str
    mocked: bool = False


def is_mock_mode() -> bool:
    """실제 발송 대신 로그만 남기는 모드.

    ``INSTAGRAM_MOCK_MODE`` 와 같은 규약(지침 5-7). **자격증명이 비어 있으면 설정값과
    무관하게 Mock 으로 떨어진다** — 운영에 키를 넣는 걸 잊었을 때 500 대신 조용한
    Mock 이 되는 건 위험하므로, 그 경우는 ``send_sms`` 가 별도로 경고를 남긴다.
    """
    return bool(getattr(settings, "SMS_MOCK_MODE", False))


def _credentials() -> tuple[str, str, str]:
    return (
        (getattr(settings, "ALIGO_API_KEY", "") or "").strip(),
        (getattr(settings, "ALIGO_USER_ID", "") or "").strip(),
        (getattr(settings, "ALIGO_SENDER", "") or "").strip(),
    )


def is_configured() -> bool:
    return all(_credentials())


def sms_byte_length(text: str) -> int:
    """EUC-KR 기준 byte 길이 — 한글 2 byte. 알리고의 SMS/LMS 판정과 같은 기준."""
    return len((text or "").encode("euc-kr", errors="replace"))


def send_sms(*, to: str, body: str, title: str = "") -> SendResult:
    """문자 1건 발송. 실패하면 ``AligoError``.

    ``to`` 는 숫자만 남긴 국내 번호(``01012345678``)여야 한다 — 정규화는 호출부
    (``apps.authentication.phone.normalize_phone``)의 책임이다.
    """
    key, user_id, sender = _credentials()

    if is_mock_mode() or not (key and user_id and sender):
        if not is_mock_mode():
            # 설정 누락인데 Mock 이 아니다 → 조용히 넘어가면 "인증번호가 안 와요" CS 가
            # 원인 불명으로 쌓인다. 명시적으로 시끄럽게 남긴다.
            logger.error("aligo: 자격증명 미설정 (ALIGO_API_KEY/USER_ID/SENDER) — 발송을 건너뛴다")
        logger.info("aligo[mock]: to=%s***%s len=%s", to[:3], to[-4:], len(body))
        return SendResult(message_id="mock", result_code=1, message="mock", mocked=True)

    msg_type = "SMS" if sms_byte_length(body) <= SMS_BYTE_LIMIT else "LMS"
    payload = {
        "key": key,
        "user_id": user_id,
        "sender": sender,
        "receiver": to,
        "msg": body,
        "msg_type": msg_type,
    }
    if msg_type == "LMS" and title:
        payload["title"] = title[:40]
    if getattr(settings, "ALIGO_TEST_MODE", False):
        # 알리고 테스트 모드 — 과금·발송 없이 응답만 정상으로 돌아온다.
        payload["testmode_yn"] = "Y"

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(
                SEND_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            )
    except httpx.HTTPError as exc:
        logger.warning("aligo: transport error: %s", exc)
        raise AligoError(
            "SMS_UNAVAILABLE", "문자 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from exc

    if resp.status_code != 200:
        logger.warning("aligo: http %s", resp.status_code)
        raise AligoError("SMS_UNAVAILABLE", "문자 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.")

    try:
        data = resp.json()
    except ValueError:
        logger.warning("aligo: non-json response")
        raise AligoError("SMS_UNAVAILABLE", "문자 발송에 실패했습니다.") from None

    try:
        result_code = int(data.get("result_code"))
    except (TypeError, ValueError):
        result_code = -999
    message = str(data.get("message") or "")

    if result_code != 1:
        # 잔액 부족(-101 등)은 운영 사고다 — 사용자 문구와 분리해 error 로 남긴다.
        logger.error("aligo: send failed result_code=%s message=%s", result_code, message)
        raise AligoError(
            "SMS_SEND_FAILED",
            "문자 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.",
            result_code=result_code,
        )

    return SendResult(
        message_id=str(data.get("msg_id") or ""), result_code=result_code, message=message
    )


def remaining_counts() -> dict:
    """잔여 발송 건수 조회 — 잔액 소진 경보용. 실패해도 예외를 던지지 않는다."""
    key, user_id, _ = _credentials()
    if not (key and user_id) or is_mock_mode():
        return {}
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.post(REMAIN_URL, data={"key": key, "user_id": user_id})
        data = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("aligo: remain lookup failed: %s", exc)
        return {}
    return {
        "sms": _as_int(data.get("SMS_CNT")),
        "lms": _as_int(data.get("LMS_CNT")),
        "mms": _as_int(data.get("MMS_CNT")),
    }


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
