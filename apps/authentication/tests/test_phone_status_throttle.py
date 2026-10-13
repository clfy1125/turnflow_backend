"""휴대폰 **상태 조회(GET)** 가 발송 스로틀에 걸리던 결함의 회귀 테스트.

사고 (2026-10-13 프론트 보고)
─────────────────────────────────────────────────────────────────────────
DRF 의 ``throttle_classes`` 는 **뷰 전체**에 걸린다. ``PhoneSendCodeView`` 하나에
get/post/delete 가 같이 있어서 **조회가 발송 한도(`phone_send` = 5/hour)를 깎았다.**

- 문자를 한 번도 보낸 적 없는 dev 더미 계정(`dmdummy-pro@turnflow.dev`)이
  ``GET /api/v1/auth/me/phone/`` **두 번째**에 ``429 RATE_LIMITED`` 를 받았다.
- 프론트는 이 조회를 **화면이 열릴 때 1회**씩만 부른다(설정 · 혜택 팝업 · 본인확인 창).
  폴링이 아니다. 즉 **설정 화면을 한 시간에 다섯 번 열면 그 뒤로 막힌다** — 운영도 같은 값.
- 바로 그 GET 의 API 문서가 *"부작용이 없고 문자를 보내지 않으므로 팝업/배너를 띄울지
  판단할 때 **자유롭게 호출하세요**"* 라고 약속하고 있었다. **문서와 구현이 정반대였다.**

막혀도 화면은 안 깨진다(프론트가 ``/auth/me`` 로 폴백) — 대신 ``deletable`` 안내 ·
혜택 문구 · 진행 중인 인증번호 되살리기가 **조용히 빠진다.** 그래서 늦게 발견됐다.

⚠️ 이 파일을 쓰면서 밟은 함정 두 가지 (같은 실수를 반복하지 말 것)
─────────────────────────────────────────────────────────────────────────
1. **POST 를 반복해 429 를 확인하는 테스트는 불안정하다.** 테스트 DB 가 dev DB 라
   (`[[test-db-not-clean]]`) 다른 실행이 남긴 **번호별 쿨다운**(`PHONE_RESEND_TOO_SOON`,
   phone_guard) 이 먼저 끼어든다. 같은 429 지만 **스로틀이 아니다.**
   → 발송 쪽은 **건드리지 않았으므로** 여기서 검증하지 않는다.
2. **`ScopedRateThrottle` 인스턴스에는 `scope` 속성이 없다.** `allow_request()` 안에서
   `view.throttle_scope` 를 읽어 그때 설정한다. 그래서 `getattr(t, "scope", None)` 은
   `None` 을 돌려주고, 그걸로 비교하면 **아무것도 검증하지 않는 테스트**가 된다.
   → 클래스와 `view.throttle_scope` 로 판정한다.
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

User = get_user_model()
URL = "/api/v1/auth/me/phone/"
SEND_RATE_LIMIT = 5  # settings 의 phone_send 기본값(5/hour)


@pytest.fixture
def user_client(db):
    """매번 새 사용자 — 스로틀 키가 사용자별이라 테스트끼리 간섭하지 않는다."""
    user = User.objects.create_user(
        email=f"phone-throttle-{uuid.uuid4().hex[:8]}@test.com", password="Pass1234!"
    )
    c = APIClient(SERVER_NAME="127.0.0.1")
    c.force_authenticate(user=user)
    return c, user


def _send_throttle_keys(user) -> list[str]:
    """이 사용자의 **발송(phone_send)** 스로틀 캐시 키들."""
    return [f"throttle_phone_send_{user.pk}", f"throttle_phone_send_{user.pk}_"]


# ─── 1. 조회가 발송 한도에 걸리지 않는가 (핵심) ───────────────────────


@pytest.mark.django_db
class TestStatusGetNotThrottledBySend:
    def test_get_beyond_send_limit(self, user_client):
        """발송 한도의 **두 배 이상** 불러도 전부 200 이어야 한다.

        옛 코드는 6번째(5/hour 초과)에서 429 였다.
        """
        c, _ = user_client
        n = SEND_RATE_LIMIT * 3
        codes = [c.get(URL).status_code for _ in range(n)]
        assert codes == [200] * n, (
            f"조회 {n}회 중 막힌 것이 있다: {codes}\n"
            "GET 은 문자를 보내지 않으므로 발송 한도(phone_send)를 쓰면 안 된다."
        )

    def test_get_does_not_touch_send_throttle_cache(self, user_client):
        """조회는 **발송 스로틀의 기록 자체를 남기면 안 된다.**

        429 가 나는지로 보지 않고 캐시 키를 직접 확인한다 — 번호별 쿨다운 같은
        다른 429 가 끼어들 여지를 없앤다.
        """
        c, user = user_client
        for key in _send_throttle_keys(user):
            cache.delete(key)

        for _ in range(SEND_RATE_LIMIT * 2):
            assert c.get(URL).status_code == 200

        leftovers = {k: cache.get(k) for k in _send_throttle_keys(user) if cache.get(k)}
        assert not leftovers, (
            f"조회가 발송 스로틀 기록을 남겼다: {leftovers}\n"
            "그만큼 실제 문자 발송 한도가 깎인다."
        )


# ─── 2. 설정이 실제로 분리됐는가 ──────────────────────────────────────


@pytest.mark.django_db
class TestThrottleScopesAreSeparate:
    def _throttles_for(self, method: str):
        from apps.authentication.phone_views import PhoneSendCodeView

        view = PhoneSendCodeView()
        view.request = type("_R", (), {"method": method})()
        return view.get_throttles()

    def test_get_does_not_use_send_scope(self):
        """GET 의 스로틀은 ``phone_send`` 를 읽지 않아야 한다.

        ⚠️ ``ScopedRateThrottle`` 은 ``view.throttle_scope``(= "phone_send") 를 읽는다.
           GET 이 그 클래스를 쓰면 **자동으로 발송 한도에 묶인다** — 그게 이 버그였다.
        """
        throttles = self._throttles_for("GET")
        assert throttles, "GET 에 스로틀이 아예 없다 — 무제한은 의도가 아니다"
        for t in throttles:
            assert not isinstance(t, ScopedRateThrottle), (
                f"GET 이 {type(t).__name__}(ScopedRateThrottle) 을 쓴다 — "
                "view.throttle_scope='phone_send' 를 그대로 읽어 발송 한도에 묶인다."
            )
        scopes = {getattr(type(t), "scope", None) for t in throttles}
        assert "phone_status" in scopes, f"GET 이 phone_status scope 를 안 쓴다: {scopes}"

    def test_post_keeps_send_scope(self):
        """POST(발송)는 **여전히** ``phone_send`` 스로틀이어야 한다. 돈이 나가는 경로다."""
        from apps.authentication.phone_views import PhoneSendCodeView

        throttles = self._throttles_for("POST")
        assert any(
            isinstance(t, ScopedRateThrottle) for t in throttles
        ), "POST 에서 ScopedRateThrottle 이 사라졌다 — phone_send 방어가 풀렸다(SMS 펌핑)."
        assert PhoneSendCodeView.throttle_scope == "phone_send"

    def test_delete_keeps_send_scope(self):
        """DELETE 도 종전 그대로 둔다 (이번 변경 범위 밖 — 바뀌면 알아채야 한다)."""
        throttles = self._throttles_for("DELETE")
        assert any(isinstance(t, ScopedRateThrottle) for t in throttles)


# ─── 3. 설정이 등록돼 있는가 ──────────────────────────────────────────


def test_phone_status_rate_is_configured():
    """``phone_status`` scope 가 settings 에 있어야 한다.

    ⚠️ ``DEFAULT_THROTTLE_RATES`` 에 없는 scope 를 쓰면 DRF 가 **예외를 던진다** —
       즉 조회가 500 이 된다. 이름을 바꿀 때 한쪽만 고치면 그 즉시 깨진다.
    """
    from django.conf import settings

    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    assert "phone_status" in rates, "phone_status rate 가 settings 에 없다"
    assert rates["phone_status"] != rates.get(
        "phone_send"
    ), "phone_status 가 phone_send 와 같은 값이다 — 분리한 의미가 없다"
