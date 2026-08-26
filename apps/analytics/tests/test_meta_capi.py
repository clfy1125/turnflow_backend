"""Meta 전환 API(CAPI) 단위 테스트.

여기서 지키는 것은 두 가지다:
1. **중복 제거가 깨지지 않을 것** — event_id 규약이 프론트 픽셀과 같아야 한다.
   어긋나면 전환이 2배로 집계되고 Meta 알고리즘이 잘못 학습한다.
2. **계측 실패가 본 기능을 깨뜨리지 않을 것** — 가입·결제는 CAPI 가 죽어도 성공해야 한다.
"""

from __future__ import annotations

import hashlib

import pytest

from apps.analytics import meta_capi


class TestHashing:
    def test_email_normalized_before_hash(self):
        """Meta 규격 — 소문자 + 공백 제거 후 해시. 안 맞추면 매칭률이 떨어진다."""
        expected = hashlib.sha256(b"user@test.com").hexdigest()
        assert meta_capi.hash_email("  User@Test.COM  ") == expected

    def test_phone_keeps_digits_only(self):
        expected = hashlib.sha256(b"01012345678").hexdigest()
        assert meta_capi.hash_phone("010-1234-5678") == expected

    def test_external_id_hashed(self):
        assert meta_capi.hash_external_id(42) == hashlib.sha256(b"42").hexdigest()

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_blank_inputs_yield_empty(self, blank):
        assert meta_capi.hash_email(blank or "") == ""
        assert meta_capi.hash_phone(blank or "") == ""
        assert meta_capi.hash_external_id(blank) == ""


class TestUserData:
    def test_fbc_fbp_ip_ua_are_plaintext(self):
        """★ 이 넷을 해시하면 Meta 가 매칭에 못 쓴다 — 절대 해시하지 말 것."""
        data = meta_capi.build_user_data(
            email="a@b.com",
            fbc="fb.1.123.abc",
            fbp="fb.1.123.999",
            client_ip="1.2.3.4",
            client_user_agent="Mozilla/5.0",
        )
        assert data["fbc"] == "fb.1.123.abc"
        assert data["fbp"] == "fb.1.123.999"
        assert data["client_ip_address"] == "1.2.3.4"
        assert data["client_user_agent"] == "Mozilla/5.0"
        # 이메일은 반대로 반드시 해시
        assert data["em"] == [meta_capi.hash_email("a@b.com")]

    def test_blank_fields_are_omitted_not_empty(self):
        """빈 문자열을 보내면 매칭 품질 점수가 깎인다 — 키 자체를 빼야 한다."""
        data = meta_capi.build_user_data(email="a@b.com")
        assert "ph" not in data
        assert "fbc" not in data
        assert "client_ip_address" not in data


class TestBuildEvent:
    def test_rejects_unknown_event_name(self):
        with pytest.raises(ValueError):
            meta_capi.build_event(event_name="Lead", event_id="1", event_time=1, user_data={})

    @pytest.mark.parametrize(
        "name",
        ["CompleteRegistration", "StartTrial", "Purchase"],
    )
    def test_supported_events(self, name):
        event = meta_capi.build_event(
            event_name=name, event_id="abc", event_time=1787631094, user_data={"em": ["x"]}
        )
        assert event["event_name"] == name
        assert event["event_id"] == "abc"
        assert event["action_source"] == "website"
        assert event["event_time"] == 1787631094

    def test_custom_data_only_when_given(self):
        base = meta_capi.build_event(
            event_name="Purchase", event_id="p1", event_time=1, user_data={}
        )
        assert "custom_data" not in base
        priced = meta_capi.build_event(
            event_name="Purchase",
            event_id="p1",
            event_time=1,
            user_data={},
            custom_data={"currency": "KRW", "value": "9900"},
        )
        assert priced["custom_data"]["value"] == "9900"


class TestEnabledGate:
    def test_disabled_without_token(self, settings):
        settings.META_CAPI_ENABLED = True
        settings.META_CAPI_ACCESS_TOKEN = ""
        settings.META_CAPI_DATASET_ID = "123"
        assert meta_capi.is_enabled() is False

    def test_disabled_without_dataset(self, settings):
        settings.META_CAPI_ENABLED = True
        settings.META_CAPI_ACCESS_TOKEN = "tok"
        settings.META_CAPI_DATASET_ID = ""
        assert meta_capi.is_enabled() is False

    def test_disabled_by_flag_even_with_credentials(self, settings):
        """★ 토큰이 있어도 플래그가 꺼져 있으면 안 보낸다 —
        프론트 event_id 배포보다 먼저 켜지면 전환이 2배로 집계된다."""
        settings.META_CAPI_ENABLED = False
        settings.META_CAPI_ACCESS_TOKEN = "tok"
        settings.META_CAPI_DATASET_ID = "123"
        assert meta_capi.is_enabled() is False

    def test_send_is_noop_when_disabled(self, settings, monkeypatch):
        settings.META_CAPI_ENABLED = False

        def _boom(*a, **k):  # 호출되면 안 된다
            raise AssertionError("비활성인데 HTTP 호출이 발생했다")

        monkeypatch.setattr("httpx.post", _boom)
        result = meta_capi.send_events([{"event_name": "Purchase"}])
        assert result["ok"] is True
        assert result["body"] == "disabled"


class TestSendEvents:
    @pytest.fixture
    def enabled(self, settings):
        settings.META_CAPI_ENABLED = True
        settings.META_CAPI_ACCESS_TOKEN = "tok-secret"
        settings.META_CAPI_DATASET_ID = "1057766930068893"
        settings.META_CAPI_API_VERSION = "v23.0"
        settings.META_CAPI_TEST_EVENT_CODE = ""
        return settings

    def _capture(self, monkeypatch, status=200, body=None):
        captured = {}

        class _Resp:
            status_code = status

            def json(self):
                return body if body is not None else {"events_received": 1}

            @property
            def text(self):
                return "err"

        def _post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return _Resp()

        monkeypatch.setattr("httpx.post", _post)
        return captured

    def test_token_goes_in_body_not_url(self, enabled, monkeypatch):
        """★ 토큰을 URL 에 실으면 httpx 로거·프록시 액세스로그에 남는다
        (토스 빌링키와 같은 함정)."""
        cap = self._capture(monkeypatch)
        meta_capi.send_events([{"event_name": "Purchase"}])
        assert "tok-secret" not in cap["url"]
        assert cap["json"]["access_token"] == "tok-secret"

    def test_url_uses_dataset_and_version(self, enabled, monkeypatch):
        cap = self._capture(monkeypatch)
        meta_capi.send_events([{"event_name": "Purchase"}])
        assert cap["url"] == "https://graph.facebook.com/v23.0/1057766930068893/events"

    def test_test_event_code_included(self, enabled, monkeypatch):
        cap = self._capture(monkeypatch)
        meta_capi.send_events([{"event_name": "Purchase"}], test_event_code="TEST67446")
        assert cap["json"]["test_event_code"] == "TEST67446"

    def test_test_event_code_from_settings(self, enabled, monkeypatch):
        enabled.META_CAPI_TEST_EVENT_CODE = "TEST67446"
        cap = self._capture(monkeypatch)
        meta_capi.send_events([{"event_name": "Purchase"}])
        assert cap["json"]["test_event_code"] == "TEST67446"

    def test_no_test_code_key_when_unset(self, enabled, monkeypatch):
        cap = self._capture(monkeypatch)
        meta_capi.send_events([{"event_name": "Purchase"}])
        assert "test_event_code" not in cap["json"]

    def test_error_status_returns_not_ok(self, enabled, monkeypatch):
        self._capture(monkeypatch, status=400, body={"error": {"message": "bad"}})
        result = meta_capi.send_events([{"event_name": "Purchase"}])
        assert result["ok"] is False
        assert result["status"] == 400

    def test_network_error_never_raises(self, enabled, monkeypatch):
        import httpx

        def _post(*a, **k):
            raise httpx.ConnectTimeout("timeout")

        monkeypatch.setattr("httpx.post", _post)
        result = meta_capi.send_events([{"event_name": "Purchase"}])
        assert result["ok"] is False
        assert result["status"] is None

    def test_empty_batch_short_circuits(self, enabled, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("빈 배치인데 호출됐다")

        monkeypatch.setattr("httpx.post", _boom)
        assert meta_capi.send_events([])["ok"] is True
