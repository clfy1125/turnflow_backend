"""
PayApp REST API 클라이언트.

PayApp의 REST API(https://api.payapp.kr/oapi/apiLoad.html)를 호출하여
정기결제 등록/해지/일시정지/재개, 단건 결제, 결제취소 등을 처리합니다.

모든 API 호출은 서버사이드(Server-to-Server)로 수행됩니다.
"""

import logging
from datetime import date
from typing import Optional
from urllib.parse import parse_qs

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# PayApp pay_type 코드 → 한글 표시명 매핑
PAY_TYPE_MAP = {
    "1": "신용카드",
    "2": "휴대전화",
    "4": "대면결제",
    "6": "계좌이체",
    "7": "가상계좌",
    "15": "카카오페이",
    "16": "네이버페이",
    "17": "등록결제",
    "21": "스마일페이",
    "22": "위챗페이",
    "23": "애플페이",
    "24": "내통장결제",
}

# PayApp pay_state 코드 의미
PAY_STATE = {
    "1": "요청",
    "4": "결제완료",
    "8": "요청취소",
    "9": "승인취소",
    "10": "결제대기",
    "16": "요청취소",
    "31": "요청취소",
    "32": "요청취소",
    "64": "승인취소",
    "70": "부분취소",
    "71": "부분취소",
    "99": "정기결제실패",
}


class PayAppError(Exception):
    """PayApp API 호출 실패"""

    def __init__(self, message: str, errno: str = "", raw: dict | None = None):
        self.errno = errno
        self.raw = raw or {}
        super().__init__(message)


class PayAppClient:
    """
    PayApp REST API 클라이언트.
    settings에서 PAYAPP_USERID, PAYAPP_LINKKEY, PAYAPP_LINKVAL을 참조합니다.
    """

    API_URL = "https://api.payapp.kr/oapi/apiLoad.html"
    TIMEOUT = 30

    # ───── 내부 헬퍼 ─────

    @staticmethod
    def _call_api(postdata: dict) -> dict:
        """
        PayApp REST API에 POST 호출.
        응답은 URL-encoded query string이므로 parse_qs로 파싱합니다.
        """
        try:
            resp = requests.post(
                PayAppClient.API_URL,
                data=postdata,
                timeout=PayAppClient.TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("PayApp API 네트워크 오류: %s", e)
            raise PayAppError(f"PayApp API 네트워크 오류: {e}")

        # 응답 파싱: "state=1&errorMessage=&rebill_no=123&payurl=..." 형태
        parsed = parse_qs(resp.text, keep_blank_values=True)
        result = {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

        if result.get("state") != "1":
            error_msg = result.get("errorMessage", "알 수 없는 오류")
            errno = result.get("errno", "")
            logger.warning(
                "PayApp API 실패: cmd=%s errno=%s msg=%s",
                postdata.get("cmd"),
                errno,
                error_msg,
            )
            raise PayAppError(error_msg, errno=errno, raw=result)

        return result

    # ───── 정기결제 (rebill) ─────

    @classmethod
    def create_rebill(
        cls,
        goodname: str,
        goodprice: int,
        recvphone: str,
        cycle_day: int = 1,
        rebill_expire: Optional[date | str] = None,
        var1: str = "",
        var2: str = "",
    ) -> dict:
        """
        정기결제 등록 (rebillRegist).
        월 정기결제를 등록하고, 구매자가 최초 1회 결제 승인하면
        다음 주기부터 자동 결제가 발생합니다.

        Note: rebillRegist에는 linkkey가 필요하지 않습니다.
              (해지/일시정지/재개에만 linkkey 사용)

        Args:
            goodname: 상품명 (예: "턴플로우 프로 월간 구독")
            goodprice: 월 결제 금액 (원)
            recvphone: 구매자 휴대전화번호
            cycle_day: 매월 결제일 (1~31, 90=말일)
            rebill_expire: 정기결제 만료일 (date, "yyyy-mm-dd" str, None→10년 뒤)
            var1: 임의 변수 1 (내부 식별용, 예: subscription_id)
            var2: 임의 변수 2 (내부 식별용, 예: plan_name)

        Returns:
            dict: {"rebill_no": "123", "payurl": "https://payapp.kr/..."}
        """
        if rebill_expire is None:
            expire_str = date(date.today().year + 10, 12, 31).strftime("%Y-%m-%d")
        elif isinstance(rebill_expire, str):
            expire_str = rebill_expire
        else:
            expire_str = rebill_expire.strftime("%Y-%m-%d")

        postdata = {
            "cmd": "rebillRegist",
            "userid": settings.PAYAPP_USERID,
            "goodname": goodname,
            "goodprice": str(goodprice),
            "recvphone": recvphone,
            "rebillCycleType": "Month",
            "rebillCycleMonth": str(cycle_day),
            "rebillExpire": expire_str,
            "feedbackurl": settings.PAYAPP_FEEDBACK_URL,
            "failurl": settings.PAYAPP_FAIL_URL,
            "var1": var1,
            "var2": var2,
            "smsuse": "n",
            "openpaytype": "card",
            "checkretry": "y",
            "returnurl": settings.PAYAPP_RETURN_URL,
        }
        result = cls._call_api(postdata)
        return {
            "rebill_no": result.get("rebill_no", ""),
            "payurl": result.get("payurl", ""),
        }

    @classmethod
    def cancel_rebill(cls, rebill_no: str) -> dict:
        """
        정기결제 해지 (rebillCancel).
        해지 후 다음 주기에 정기결제가 발생하지 않습니다.
        """
        postdata = {
            "cmd": "rebillCancel",
            "userid": settings.PAYAPP_USERID,
            "linkkey": settings.PAYAPP_LINKKEY,
            "rebill_no": rebill_no,
        }
        return cls._call_api(postdata)

    @classmethod
    def pause_rebill(cls, rebill_no: str) -> dict:
        """정기결제 일시정지 (rebillStop)."""
        postdata = {
            "cmd": "rebillStop",
            "userid": settings.PAYAPP_USERID,
            "linkkey": settings.PAYAPP_LINKKEY,
            "rebill_no": rebill_no,
        }
        return cls._call_api(postdata)

    @classmethod
    def resume_rebill(cls, rebill_no: str) -> dict:
        """정기결제 재개 (rebillStart). 일시정지 → 승인으로 변경."""
        postdata = {
            "cmd": "rebillStart",
            "userid": settings.PAYAPP_USERID,
            "linkkey": settings.PAYAPP_LINKKEY,
            "rebill_no": rebill_no,
        }
        return cls._call_api(postdata)

    # ───── 결제 취소 ─────

    @classmethod
    def cancel_payment(cls, mul_no: str, memo: str = "구독 해지 환불") -> dict:
        """
        결제(요청/승인) 전체 취소 (paycancel).
        취소 성공 시 feedbackurl로도 통보됩니다.
        """
        postdata = {
            "cmd": "paycancel",
            "userid": settings.PAYAPP_USERID,
            "linkkey": settings.PAYAPP_LINKKEY,
            "mul_no": str(mul_no),
            "cancelmemo": memo,
            "partcancel": "0",
        }
        return cls._call_api(postdata)

    @classmethod
    def cancel_payment_partial(
        cls, mul_no: str, cancel_price: int, memo: str = "부분 환불"
    ) -> dict:
        """
        결제 부분 취소 (paycancel, partcancel=1).
        """
        postdata = {
            "cmd": "paycancel",
            "userid": settings.PAYAPP_USERID,
            "linkkey": settings.PAYAPP_LINKKEY,
            "mul_no": str(mul_no),
            "cancelmemo": memo,
            "partcancel": "1",
            "cancelprice": str(cancel_price),
        }
        return cls._call_api(postdata)

    # ───── 유틸 ─────

    @staticmethod
    def get_pay_type_display(pay_type: str) -> str:
        """pay_type 코드를 한글 이름으로 변환."""
        return PAY_TYPE_MAP.get(str(pay_type), f"기타({pay_type})")
