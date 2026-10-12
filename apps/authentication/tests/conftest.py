"""휴대폰 인증 테스트의 **공통 함정 제거** — 캐시에 사는 상태는 롤백되지 않는다.

pytest-django 는 DB 를 트랜잭션으로 되돌리지만 **캐시(Redis)는 되돌리지 않는다.**
휴대폰 발송 경로에는 캐시에 사는 카운터가 둘 있다:

1. **IP 일일 카운터** (``phone_guard.ip_counter_key``) — 테스트가 쌓아 올리면 몇 번
   돌리는 사이 상한(100)에 닿아 **전부 429** 가 된다.
2. **DRF 스로틀 이력** (``phone_send`` 5회/시간, 사용자 pk 로 키잉) — dev 시더처럼
   **같은 계정을 재사용**하는 테스트끼리 서로의 할당을 깎아먹는다.

둘 다 "따로 돌리면 통과, 같이 돌리면 실패" 라는 가장 찾기 나쁜 형태로 나타나서,
각 테스트 파일이 각자 기억하게 두지 않고 여기 한 곳에서 없앤다.

⚠️ 스로틀을 통째로 풀어도 되는 이유: 이 디렉터리에 **스로틀 발동을 단언하는 테스트가
   없다**. 생긴다면 그 테스트만 ``settings`` 로 요율을 되돌려 쓸 것.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_phone_rate_state(settings):
    """IP 카운터를 비우고 휴대폰 스로틀을 사실상 무제한으로 둔다."""
    from django.core.cache import cache

    from apps.authentication.phone_guard import ip_counter_key

    # ⚠️ 캐시 전체 flush 금지 — 이 저장소는 그것 때문에 DM 이 1시간 멈춘 적이 있다
    #    (memory: prod-deploy-f96e732). 우리가 만든 키만 지운다.
    cache.delete(ip_counter_key("127.0.0.1"))

    rates = dict(settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}))
    rates["phone_send"] = "100000/hour"
    rates["phone_verify"] = "100000/min"
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}
    yield
    cache.delete(ip_counter_key("127.0.0.1"))
