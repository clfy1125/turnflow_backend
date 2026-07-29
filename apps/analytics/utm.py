"""UTM 값 표준화 — 방문/가입/어드민 저장 링크가 공유하는 **단일 소스**.

Django 모델·설정에 의존하지 않는 순수 함수만 둔다. 어드민 대시보드
(apps/admin_api/views/dashboard_marketing.py)가 analytics 앱을 guarded import 하는
구조라, 모델을 건드리는 :mod:`apps.analytics.channels` 에 두면 앱 미배치 상황에서
대시보드가 깨진다 — 그래서 별 모듈로 분리했다. ``channels`` 가 재수출한다.

## 왜 필요한가 (한국어 UTM)

UTM 4필드는 세 곳에 각각 저장된다: ``LandingVisit``(방문 비콘) · ``SignupAttribution``
(가입) · ``MarketingChannelLink``(어드민이 저장한 링크). 어드민 마케팅 대시보드는 이
**4-튜플 완전일치**로 "이 유입이 어느 저장 링크의 것인가"를 판정한다
(``dashboard_marketing._utm_key``). 따라서 세 경로가 같은 표준형을 쓰지 않으면 링크 행은
0 으로 남고 트래픽은 '저장 안 된 링크(UTM)'로 새는데, **두 값이 화면상 완전히 똑같이
보이기 때문에** 눈으로는 원인을 찾을 수 없다.

한글을 UTM 에 쓰면 그 위험이 실제로 발생한다:

1. **NFC vs NFD** — macOS/iOS 에서 복사한 한글은 NFD(자모 분해)로 오는 경우가 있다.
   "테스트"(NFC, 3자)와 "테스트"(NFD, 9자)는 같은 글자로 보이지만 다른 문자열이고,
   글자수도 3배라 ``max_length`` 를 넘겨 조용히 잘리거나 버려진다.
2. **공백류 변형** — 엑셀/슬랙/광고관리자 복붙 경로에서 NBSP(``\\xa0``)·전각공백이 섞인다.
3. **연속 공백** — "테스트  캠페인"(두 칸)과 "테스트 캠페인"은 다른 문자열이다.
"""

from __future__ import annotations

import re
import unicodedata

UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")

# UTM 필드 공통 상한 — LandingVisit/SignupAttribution/MarketingChannelLink 3곳 동일.
# 한글도 NFC 정규화 후 '글자수'로 세므로 200자면 실무 캠페인명에 충분하다.
UTM_MAX_LENGTH = 200

# \s 는 유니코드 모드에서 NBSP(\xa0)·전각공백(　)·탭·개행을 모두 잡는다.
_WS_RE = re.compile(r"\s+")


def normalize_utm(value) -> str:
    """UTM 값 → 저장·매칭 표준형: NFC 정규화 → 공백류 1칸 축약 → 앞뒤 제거.

    대소문자는 **건드리지 않는다** — 표시값은 입력한 그대로 보존하고, 매칭할 때만
    호출부에서 ``lower()`` 한다 (``dashboard_marketing._norm``).
    """
    if not value:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", str(value))).strip()


def normalize_utm_payload(data):
    """dict 페이로드의 UTM 4필드만 표준형으로 바꾼 **얕은 복사본** 반환.

    시리얼라이저의 ``to_internal_value`` 에서 쓴다 — 필드 ``max_length`` 검증보다
    **먼저** 정규화해야 NFD 로 부풀어 온 한글이 길이 초과로 버려지지 않는다
    (방문 비콘은 검증 실패 = silent-204 = 기록 없음이라 특히 위험하다).
    dict 가 아니면 원본을 그대로 돌려준다.
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for field in UTM_FIELDS:
        if isinstance(out.get(field), str):
            out[field] = normalize_utm(out[field])
    return out
