"""퍼널 이벤트 비콘 — silent-204 계약 + 인증 선택 + 봇/크기 게이트.

이 엔드포인트의 계약은 "**절대 사용자 화면을 깨지 않는다**" 이다. 잘못된 페이로드에
400 을 내면 프론트가 `.catch()` 하지 않은 곳에서 에러가 터진다.

더러운 테스트 DB 대응(memory: test-db-not-clean): 전역 count 가 아니라
이 테스트가 만든 event 이름으로만 조회한다.
"""

import uuid

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.analytics.models import FunnelEvent

URL_NAME = "analytics:track-funnel-event"
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"
BOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


def _event_name():
    return f"tf_test_{uuid.uuid4().hex[:12]}"


@pytest.mark.django_db
class TestTrackFunnelEvent:
    @pytest.fixture
    def client(self):
        return APIClient()

    def test_anonymous_event_is_recorded(self, client):
        name = _event_name()
        res = client.post(
            reverse(URL_NAME),
            {"event": name, "payload": {"surface": "pc_modal", "attempt": 1}, "path": "/home"},
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        assert res.status_code == 204

        row = FunnelEvent.objects.get(event=name)
        assert row.user_id is None
        assert row.payload == {"surface": "pc_modal", "attempt": 1}
        assert row.path == "/home"
        # IP 는 해시만 저장한다 (원본 저장 금지)
        assert row.ip_hash and "." not in row.ip_hash

    def test_authenticated_event_binds_user(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            email=f"funnel-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
        )
        client.force_authenticate(user=user)
        name = _event_name()

        res = client.post(
            reverse(URL_NAME), {"event": name}, format="json", HTTP_USER_AGENT=DESKTOP_UA
        )
        assert res.status_code == 204
        assert FunnelEvent.objects.get(event=name).user_id == user.id

    def test_device_fields_are_stored(self, client):
        name = _event_name()
        client.post(
            reverse(URL_NAME),
            {"event": name, "device": "ios", "in_app": True, "in_app_kind": "instagram"},
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        row = FunnelEvent.objects.get(event=name)
        assert row.device == "ios"
        assert row.in_app is True
        assert row.in_app_kind == "instagram"
        # 서버 파생값도 함께 남는다 — 프론트 보고값과 어긋나는 경우를 나중에 볼 수 있게
        assert row.ua_class == "desktop"

    def test_bot_is_skipped_silently(self, client):
        name = _event_name()
        res = client.post(reverse(URL_NAME), {"event": name}, format="json", HTTP_USER_AGENT=BOT_UA)
        assert res.status_code == 204
        assert not FunnelEvent.objects.filter(event=name).exists()

    def test_missing_event_name_is_204_not_400(self, client):
        """silent-204 — 잘못된 페이로드로 프론트를 깨뜨리지 않는다."""
        res = client.post(
            reverse(URL_NAME), {"payload": {}}, format="json", HTTP_USER_AGENT=DESKTOP_UA
        )
        assert res.status_code == 204

    def test_oversized_payload_is_skipped(self, client):
        name = _event_name()
        res = client.post(
            reverse(URL_NAME),
            {"event": name, "payload": {"blob": "x" * 9000}},
            format="json",
            HTTP_USER_AGENT=DESKTOP_UA,
        )
        assert res.status_code == 204
        assert not FunnelEvent.objects.filter(event=name).exists()

    def test_retention_task_deletes_only_old_rows(self, client, settings):
        from datetime import timedelta

        from django.utils import timezone

        from apps.analytics.tasks import cleanup_funnel_events

        settings.FUNNEL_EVENT_RETENTION_DAYS = 30
        old_name, fresh_name = _event_name(), _event_name()
        old = FunnelEvent.objects.create(event=old_name)
        FunnelEvent.objects.create(event=fresh_name)
        # auto_now_add 라 생성 후에 밀어야 한다
        FunnelEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=31))

        cleanup_funnel_events()

        assert not FunnelEvent.objects.filter(event=old_name).exists()
        assert FunnelEvent.objects.filter(event=fresh_name).exists()
