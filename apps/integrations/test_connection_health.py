"""연결 헬스체크 + 웹훅 수동 재구독(Feature 2) 테스트.

라이브 경로 테스트는 INSTAGRAM_MOCK_MODE=False 로 강제(로컬 기본 True).
"""

import uuid
from datetime import timedelta

import pytest
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations.models import IGAccountConnection
from apps.integrations.services import InstagramOAuthService
from apps.integrations.views import IGHealthCheckThrottle
from apps.workspace.models import Membership, Workspace

User = get_user_model()

HEALTH_URL = "/api/v1/integrations/instagram/connections/{id}/health/"
RESUB_URL = "/api/v1/integrations/instagram/connections/{id}/resubscribe-webhooks/"


def _user(**kw):
    return User.objects.create_user(
        email=f"health-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!", **kw
    )


def _ws(user):
    ws = Workspace.objects.create(name="h-ws", slug=f"h-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws, status=IGAccountConnection.Status.ACTIVE, is_active=True, expires_in_days=47):
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=status,
        is_active=is_active,
        token_expires_at=timezone.now() + timedelta(days=expires_in_days),
        last_verified_at=timezone.now() - timedelta(days=1),
    )
    conn.access_token = "live_token_xyz"  # mock_token_ prefix 아님 → 라이브 취급
    conn.save()
    return conn


def _live(monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_MOCK_MODE", False)


def _patch_verify(monkeypatch, valid, error_code=None):
    monkeypatch.setattr(
        InstagramOAuthService,
        "verify_token",
        classmethod(lambda cls, tok: {"valid": valid, "error_code": error_code}),
    )


def _patch_subs(monkeypatch, fields):
    data = {"data": [{"subscribed_fields": list(fields)}]}
    monkeypatch.setattr(
        InstagramOAuthService,
        "get_webhook_subscriptions",
        classmethod(lambda cls, ig_id, tok: data),
    )


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


# ──────────────────────────────────────────────
# 헬스체크 GET
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestConnectionHealth:
    def test_healthy(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user))
        _patch_verify(monkeypatch, True)
        _patch_subs(monkeypatch, ["comments", "messages"])
        before = conn.last_verified_at

        resp = _client(user).get(HEALTH_URL.format(id=conn.id))
        assert resp.status_code == 200
        data = resp.data["data"]
        assert data["healthy"] is True
        assert data["issues"] == []
        assert data["token"]["valid"] is True
        assert data["webhook"]["subscribed"] is True
        # last_verified_at 갱신됨 (유일한 쓰기)
        conn.refresh_from_db()
        assert conn.last_verified_at > before

    def test_missing_field(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user))
        _patch_verify(monkeypatch, True)
        _patch_subs(monkeypatch, ["comments"])  # messages 누락

        resp = _client(user).get(HEALTH_URL.format(id=conn.id))
        data = resp.data["data"]
        assert data["healthy"] is False
        assert data["webhook"]["missing_fields"] == ["messages"]
        assert any(i["code"] == "WEBHOOK_FIELDS_MISSING" for i in data["issues"])

    def test_dead_token_reports_but_does_not_brick(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user))
        _patch_verify(monkeypatch, False, error_code=190)

        # 토큰 죽으면 웹훅 조회는 호출되면 안 됨
        def _boom(cls, ig_id, tok):
            raise AssertionError("죽은 토큰인데 웹훅 조회를 시도함")

        monkeypatch.setattr(InstagramOAuthService, "get_webhook_subscriptions", classmethod(_boom))

        resp = _client(user).get(HEALTH_URL.format(id=conn.id))
        data = resp.data["data"]
        assert data["token"]["valid"] is False
        assert any(i["code"] == "TOKEN_INVALID" for i in data["issues"])
        # status 는 바뀌지 않아야 함 (report-only)
        conn.refresh_from_db()
        assert conn.status == IGAccountConnection.Status.ACTIVE

    def test_meta_unreachable(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user))
        _patch_verify(monkeypatch, True)

        def _boom(cls, ig_id, tok):
            raise requests.ConnectionError("meta down")

        monkeypatch.setattr(InstagramOAuthService, "get_webhook_subscriptions", classmethod(_boom))

        resp = _client(user).get(HEALTH_URL.format(id=conn.id))
        assert resp.status_code == 200  # 진단은 200 유지
        data = resp.data["data"]
        assert data["webhook"]["subscribed"] is None
        assert any(i["code"] == "META_API_UNREACHABLE" for i in data["issues"])

    def test_revoked_connection(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user), status=IGAccountConnection.Status.REVOKED, is_active=False)
        conn.access_token = ""
        conn.save()

        def _boom(*a, **k):
            raise AssertionError("REVOKED 인데 Meta 호출함")

        monkeypatch.setattr(InstagramOAuthService, "verify_token", classmethod(_boom))
        monkeypatch.setattr(InstagramOAuthService, "get_webhook_subscriptions", classmethod(_boom))

        resp = _client(user).get(HEALTH_URL.format(id=conn.id))
        data = resp.data["data"]
        assert data["healthy"] is False
        assert any(i["code"] == "CONNECTION_REVOKED" for i in data["issues"])

    def test_inactive_connection(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user), is_active=False)
        _patch_verify(monkeypatch, True)
        _patch_subs(monkeypatch, ["comments", "messages"])

        resp = _client(user).get(HEALTH_URL.format(id=conn.id))
        data = resp.data["data"]
        assert data["healthy"] is False
        assert any(i["code"] == "CONNECTION_INACTIVE" for i in data["issues"])

    def test_403_outsider(self, monkeypatch):
        _live(monkeypatch)
        conn = _conn(_ws(_user()))
        resp = _client(_user()).get(HEALTH_URL.format(id=conn.id))
        assert resp.status_code == 403

    def test_404_unknown(self):
        resp = _client(_user()).get(HEALTH_URL.format(id=uuid.uuid4()))
        assert resp.status_code == 404

    def test_mock_mode_no_meta_calls(self, monkeypatch, settings):
        # 테스트 env 는 INSTAGRAM_MOCK_MODE=False 라 명시적으로 켠다 — Meta 호출 없이 시뮬레이션
        settings.INSTAGRAM_MOCK_MODE = True
        settings.DEBUG = True
        user = _user()
        conn = _conn(_ws(user))

        def _boom(*a, **k):
            raise AssertionError("mock 모드인데 Meta 호출함")

        monkeypatch.setattr(InstagramOAuthService, "verify_token", classmethod(_boom))
        monkeypatch.setattr(InstagramOAuthService, "get_webhook_subscriptions", classmethod(_boom))

        resp = _client(user).get(HEALTH_URL.format(id=conn.id))
        data = resp.data["data"]
        assert data["mode"] == "mock"
        assert data["healthy"] is True


# ──────────────────────────────────────────────
# 재구독 POST
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestResubscribe:
    def test_happy_path_returns_fresh_health(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user))
        called = {}

        def _sub(cls, ig_user_id, access_token, fields="comments,messages"):
            called["fields"] = fields
            return {"ok": True}

        monkeypatch.setattr(InstagramOAuthService, "subscribe_to_webhooks", classmethod(_sub))
        _patch_verify(monkeypatch, True)
        _patch_subs(monkeypatch, ["comments", "messages"])

        resp = _client(user).post(RESUB_URL.format(id=conn.id))
        assert resp.status_code == 200
        assert resp.data["resubscribed"] is True
        assert called["fields"] == "comments,messages"
        assert resp.data["data"]["healthy"] is True

    def test_revoked_409(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user), status=IGAccountConnection.Status.REVOKED, is_active=False)
        conn.access_token = ""
        conn.save()

        def _boom(*a, **k):
            raise AssertionError("REVOKED 인데 재구독 시도함")

        monkeypatch.setattr(InstagramOAuthService, "subscribe_to_webhooks", classmethod(_boom))
        resp = _client(user).post(RESUB_URL.format(id=conn.id))
        assert resp.status_code == 409
        assert resp.data["error"]["action"] == "reconnect"

    def test_meta_error_502(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user))

        def _boom(cls, ig_user_id, access_token, fields="comments,messages"):
            raise requests.HTTPError("Instagram Graph API error: 500")

        monkeypatch.setattr(InstagramOAuthService, "subscribe_to_webhooks", classmethod(_boom))
        resp = _client(user).post(RESUB_URL.format(id=conn.id))
        assert resp.status_code == 502

    def test_expired_token_409(self, monkeypatch):
        _live(monkeypatch)
        user = _user()
        conn = _conn(_ws(user), expires_in_days=-1)  # 이미 만료

        def _boom(*a, **k):
            raise AssertionError("만료 토큰인데 재구독 시도함")

        monkeypatch.setattr(InstagramOAuthService, "subscribe_to_webhooks", classmethod(_boom))
        resp = _client(user).post(RESUB_URL.format(id=conn.id))
        assert resp.status_code == 409


# ──────────────────────────────────────────────
# 스로틀
# ──────────────────────────────────────────────


@pytest.mark.django_db
class TestThrottle:
    def test_health_throttled(self, monkeypatch):
        _live(monkeypatch)
        cache.clear()
        monkeypatch.setitem(IGHealthCheckThrottle.THROTTLE_RATES, "ig_health", "2/min")
        user = _user()
        conn = _conn(_ws(user))
        _patch_verify(monkeypatch, True)
        _patch_subs(monkeypatch, ["comments", "messages"])
        client = _client(user)

        assert client.get(HEALTH_URL.format(id=conn.id)).status_code == 200
        assert client.get(HEALTH_URL.format(id=conn.id)).status_code == 200
        assert client.get(HEALTH_URL.format(id=conn.id)).status_code == 429
        cache.clear()
