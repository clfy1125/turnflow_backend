"""휴대폰 번호 정규화·마스킹·인증번호 해시 — **판정 단일 소스**.

번호를 다루는 규칙이 뷰·카카오 파서·어드민·CAPI 로 흩어지면 같은 사람이 서로 다른
문자열로 두 번 저장된다(``010-1234-5678`` 과 ``+821012345678``). 그러면 ① 중복 집계
② 알림톡 발송 실패 ③ "이미 등록된 번호" 판정 실패가 한꺼번에 난다. 모든 경로가
``normalize_phone`` 하나만 통과하게 한다.

## 저장 형식: ``01012345678`` (숫자만, 국내 형식)

E.164(``+821012345678``)가 아니라 국내 형식으로 저장하는 이유:
  · 알리고가 국내 형식만 받는다 (국제발송 미지원)
  · 카카오 알림톡 수신번호도 국내 형식이 표준이다
  · Meta CAPI 는 ``hash_phone`` 이 숫자만 남겨 해시하므로 어느 쪽이든 동작한다
    (단 **일관성**이 매칭률을 좌우하므로 한 가지로 통일하는 것이 핵심이다)

## 인증번호는 평문으로 저장하지 않는다

6자리는 100만 가지라 단순 SHA-256 은 DB 가 털리면 1초 만에 역산된다. ``SECRET_KEY`` 를
키로 쓰는 **HMAC** 이라 키 없이는 역산이 불가능하다. 그리고 번호를 해시 입력에 섞어
(``phone:code``) 다른 번호의 행으로 코드를 재사용하는 것도 막는다.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from django.conf import settings

_NON_DIGIT = re.compile(r"\D")

#: 국내 **휴대폰** 식별번호. 알림톡·문자 모두 휴대폰만 대상이라 유선번호는 받지 않는다.
#: 010 은 11자리 고정, 구번호(011/016/017/018/019)는 10~11자리 둘 다 존재한다.
_MOBILE_PATTERN = re.compile(r"^(010\d{8}|01[16789]\d{7,8})$")


class InvalidPhoneNumber(ValueError):
    """정규화할 수 없는 번호."""


def normalize_phone(raw: str) -> str:
    """입력 문자열 → ``01012345678``. 실패하면 ``InvalidPhoneNumber``.

    받아들이는 표기: ``010-1234-5678`` · ``010 1234 5678`` · ``+82 10-1234-5678`` ·
    ``+821012345678`` · ``821012345678`` · ``01012345678``
    """
    digits = _NON_DIGIT.sub("", raw or "")
    if not digits:
        raise InvalidPhoneNumber("번호를 입력해 주세요.")

    # 국가번호 82 를 떼고 국내 0 을 복원한다.
    # ⚠️ 사람들은 ``+8210…``(0 생략)과 ``+82010…``(0 유지)을 **둘 다** 쓴다. 떼고 나서
    #    0 이 이미 있는지 보고 붙여야 ``001012345678`` 같은 쓰레기가 안 나온다.
    #    국내 번호는 전부 0 으로 시작하므로(01x/02/03x…) "82 로 시작하는 국내번호"는 없다.
    for prefix in ("0082", "082", "82"):
        if digits.startswith(prefix):
            digits = digits[len(prefix) :]
            break
    if not digits.startswith("0"):
        digits = "0" + digits

    if not _MOBILE_PATTERN.match(digits):
        raise InvalidPhoneNumber("올바른 휴대폰 번호가 아닙니다. (예: 010-1234-5678)")
    return digits


def mask_phone(phone: str) -> str:
    """``01012345678`` → ``010-****-5678``. 화면·로그 노출용."""
    digits = _NON_DIGIT.sub("", phone or "")
    if len(digits) < 7:
        return "***"
    return f"{digits[:3]}-****-{digits[-4:]}"


def format_phone(phone: str) -> str:
    """``01012345678`` → ``010-1234-5678``. 본인에게 되돌려 줄 때만 쓴다."""
    digits = _NON_DIGIT.sub("", phone or "")
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def generate_code() -> str:
    """6자리 인증번호. ``secrets`` 를 쓴다 — ``random`` 은 예측 가능하다."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(phone: str, code: str) -> str:
    """HMAC-SHA256(SECRET_KEY, ``<phone>:<code>``) hex.

    번호를 섞는 이유: 같은 코드가 떠 있는 다른 사람의 행에 내 코드를 들이밀어도
    해시가 달라 맞지 않는다.
    """
    msg = f"{phone}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def code_matches(phone: str, code: str, stored_hash: str) -> bool:
    """타이밍 공격에 안전한 비교."""
    return hmac.compare_digest(hash_code(phone, code), stored_hash or "")
