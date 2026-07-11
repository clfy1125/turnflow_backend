"""가입 attribution 캡처 통합 테스트 — 이메일 register / Google 가입 경로.

핵심 계약:
  - **모든** 가입에 SignupAttribution 1행 (페이로드 없으면 channel="unknown")
  - 잘못된 attribution 이 가입을 절대 깨뜨리지 않는다 (silent capture)
  - 방문(LandingVisit)과 가입은 visitor_id 로 조인된다
"""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.analytics.attribution import capture_signup_attribution
from apps.analytics.channels import CH_DIRECT, CH_IG_ORGANIC, CH_META_ADS, CH_UNKNOWN
from apps.analytics.models import LandingVisit, SignupAttribution

User = get_user_model()

REGISTER_URL = "/api/v1/auth/register/"
GOOGLE_URL = "/api/v1/auth/google/"


@pytest.fixture
def client():
    return APIClient()


def _register_payload(**overrides):
    email = f"attr-{uuid.uuid4().hex[:12]}@test.com"
    payload = {
        "email": email,
        "password": "SecurePass123!",
        "password_confirm": "SecurePass123!",
        "full_name": "귀속 테스터",
    }
    payload.update(overrides)
    return payload


def _attr(**overrides):
    attr = {
        "visitor_id": str(uuid.uuid4()),
        "utm_source": "meta",
        "utm_medium": "cpc",
        "utm_campaign": "launch",
        "utm_content": "video_a",
        "referrer": "",
        "landing_path": "/",
    }
    attr.update(overrides)
    return attr


@pytest.mark.django_db
class TestEmailRegisterAttribution:
    def test_register_with_attribution(self, client):
        vid = str(uuid.uuid4())
        payload = _register_payload(attribution=_attr(visitor_id=vid))
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201

        row = SignupAttribution.objects.get(user__email=payload["email"])
        assert row.channel == CH_META_ADS
        assert str(row.visitor_id) == vid
        assert row.signup_kind == "email"
        assert row.utm_campaign == "launch"

    def test_register_without_attribution_gets_unknown(self, client):
        payload = _register_payload()
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201

        row = SignupAttribution.objects.get(user__email=payload["email"])
        assert row.channel == CH_UNKNOWN
        assert row.visitor_id is None

    def test_register_with_utm_only(self, client):
        payload = _register_payload(attribution={"utm_source": "meta"})
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201
        row = SignupAttribution.objects.get(user__email=payload["email"])
        assert row.channel == CH_META_ADS
        assert row.visitor_id is None

    def test_register_with_referrer_only(self, client):
        payload = _register_payload(attribution={"referrer": "https://l.instagram.com/"})
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201
        row = SignupAttribution.objects.get(user__email=payload["email"])
        assert row.channel == CH_IG_ORGANIC

    def test_register_with_empty_attribution_object_is_direct_or_unknown(self, client):
        # 페이로드가 아예 없는 것({}) 은 unknown — "프론트 미연동" 과 "직접 유입" 구분
        payload = _register_payload(attribution={})
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201
        row = SignupAttribution.objects.get(user__email=payload["email"])
        assert row.channel == CH_UNKNOWN

    def test_register_with_junk_string_attribution_still_201(self, client):
        # JSONField 는 문자열도 유효한 JSON 으로 통과 — capture 가 무시하고 unknown 저장
        payload = _register_payload(attribution="garbage-string")
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201
        row = SignupAttribution.objects.get(user__email=payload["email"])
        assert row.channel == CH_UNKNOWN

    def test_register_with_junk_typed_fields_still_201(self, client):
        payload = _register_payload(
            attribution={"visitor_id": 12345, "utm_source": ["list"], "referrer": None}
        )
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201
        row = SignupAttribution.objects.get(user__email=payload["email"])
        assert row.visitor_id is None
        assert row.utm_source == ""
        assert row.channel == CH_DIRECT  # dict 는 있었으나 유효 신호 없음 → direct

    def test_invalid_visitor_id_string_row_created_with_null(self, client):
        payload = _register_payload(attribution={"visitor_id": "not-a-uuid", "utm_source": "meta"})
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201
        row = SignupAttribution.objects.get(user__email=payload["email"])
        assert row.visitor_id is None
        assert row.channel == CH_META_ADS

    def test_capture_failure_never_breaks_register(self, client, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(SignupAttribution.objects, "get_or_create", _boom)
        payload = _register_payload(attribution=_attr())
        res = client.post(REGISTER_URL, payload, format="json")
        assert res.status_code == 201  # 가입은 성공
        assert not SignupAttribution.objects.filter(user__email=payload["email"]).exists()


@pytest.mark.django_db
class TestGoogleSignupAttribution:
    def _mock_google(self, monkeypatch, email):
        def _fake_verify(token, request, client_id):
            return {"iss": "accounts.google.com", "email": email, "name": "구글 가입자"}

        monkeypatch.setattr("google.oauth2.id_token.verify_oauth2_token", _fake_verify)

    def test_new_google_user_captures_attribution(self, client, monkeypatch):
        email = f"google-{uuid.uuid4().hex[:12]}@test.com"
        self._mock_google(monkeypatch, email)

        vid = str(uuid.uuid4())
        res = client.post(
            GOOGLE_URL,
            {"token": "fake-google-id-token", "attribution": _attr(visitor_id=vid)},
            format="json",
        )
        assert res.status_code == 200

        row = SignupAttribution.objects.get(user__email=email)
        assert row.signup_kind == "google"
        assert row.channel == CH_META_ADS
        assert str(row.visitor_id) == vid

    def test_existing_google_user_login_does_not_overwrite(self, client, monkeypatch):
        email = f"google-{uuid.uuid4().hex[:12]}@test.com"
        self._mock_google(monkeypatch, email)

        res1 = client.post(
            GOOGLE_URL,
            {"token": "fake", "attribution": {"utm_source": "meta"}},
            format="json",
        )
        assert res1.status_code == 200
        # 두 번째 로그인 — created=False 경로라 capture 호출 자체가 없다
        res2 = client.post(
            GOOGLE_URL,
            {"token": "fake", "attribution": {"utm_source": "naver"}},
            format="json",
        )
        assert res2.status_code == 200

        rows = SignupAttribution.objects.filter(user__email=email)
        assert rows.count() == 1
        assert rows.first().channel == CH_META_ADS  # 최초 가입 시점 값 유지

    def test_new_google_user_without_attribution_gets_unknown(self, client, monkeypatch):
        email = f"google-{uuid.uuid4().hex[:12]}@test.com"
        self._mock_google(monkeypatch, email)

        res = client.post(GOOGLE_URL, {"token": "fake"}, format="json")
        assert res.status_code == 200
        row = SignupAttribution.objects.get(user__email=email)
        assert row.channel == CH_UNKNOWN
        assert row.signup_kind == "google"


@pytest.mark.django_db
class TestAttributionAggregationContract:
    """마케팅 대시보드 서브시스템이 의존할 집계/조인 계약 스모크."""

    def _make_user(self):
        return User.objects.create_user(
            email=f"agg-{uuid.uuid4().hex[:12]}@test.com", password="SecurePass123!"
        )

    def test_channel_group_by_counts(self, client):
        users = [self._make_user() for _ in range(4)]
        capture_signup_attribution(users[0], {"utm_source": "meta"}, signup_kind="email")
        capture_signup_attribution(users[1], {"utm_source": "meta"}, signup_kind="email")
        capture_signup_attribution(users[2], {"referrer": ""}, signup_kind="email")  # direct
        capture_signup_attribution(users[3], None, signup_kind="email")  # unknown

        from django.db.models import Count

        counts = {
            r["channel"]: r["n"]
            for r in SignupAttribution.objects.filter(user__in=users)
            .values("channel")
            .annotate(n=Count("id"))
        }
        assert counts == {CH_META_ADS: 2, CH_DIRECT: 1, CH_UNKNOWN: 1}

    def test_visit_signup_join_by_visitor_id(self, client):
        vid = uuid.uuid4()
        LandingVisit.objects.create(visitor_id=vid, utm_source="meta", channel=CH_META_ADS)
        user = self._make_user()
        capture_signup_attribution(
            user, {"visitor_id": str(vid), "utm_source": "meta"}, signup_kind="email"
        )

        attribution = SignupAttribution.objects.get(user=user)
        joined_visits = LandingVisit.objects.filter(visitor_id=attribution.visitor_id)
        assert joined_visits.count() == 1
        assert joined_visits.first().channel == attribution.channel == CH_META_ADS

    def test_capture_is_idempotent(self, client):
        user = self._make_user()
        capture_signup_attribution(user, {"utm_source": "meta"}, signup_kind="email")
        capture_signup_attribution(user, {"utm_source": "naver"}, signup_kind="email")
        rows = SignupAttribution.objects.filter(user=user)
        assert rows.count() == 1
        assert rows.first().channel == CH_META_ADS  # 최초 값 유지 (get_or_create)
