"""게시물 '연령 제한' 판정·게이트·자동정지 테스트 (2026-09-07).

배경: 2026-09-04 부터 인스타가 게시물 단위로 '연령 제한 콘텐츠' 분류를 시작했고, 걸린
게시물은 댓글 웹훅이 끊기고 비공개 답장이 거부된다(``code 200 / subcode 2534066``).
캠페인을 켜둔 채 두면 실패만 무한히 쌓인다(실서버 257명·96명 사례).
조사: ``docs/system/DM_2534066_MEDIA_BLOCK_CENSUS_2026-09-07.md``

커버리지:
  - 판정 단일 소스 :mod:`apps.integrations.ig_content_restriction` 의 세 신호
  - 캠페인 **생성**/**복사** 게이트 → 409 ``media_content_restricted``
  - 점검 API 2종 (report-only, 항상 200)
  - 자동 정지 스위퍼 + 재개 시 표식 해제
  - live 검사가 설정 OFF 일 때 **외부 호출을 하지 않는다**

NOTE(test-db-not-clean): dev DB 를 그대로 쓰므로 **내가 만든 행 기준으로만** 단언한다.
NOTE(validate-detectors-against-broken-version): 마커 문자열은 실서버 응답에서 뜬 값이라
  리터럴을 바꾸면 판정이 조용히 0건이 된다 — 상수를 직접 참조해 고정한다.
"""

import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.integrations import ig_content_restriction as icr
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SeenComment, SentDMLog
from apps.integrations.tasks import sweep_restricted_campaigns
from apps.workspace.models import Membership, Workspace

SUB = "2534066"


# ── 픽스처 ────────────────────────────────────────────────────────────
@pytest.fixture
def owner(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_user(
        email=f"cr_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="CR"
    )


@pytest.fixture
def workspace(owner):
    ws = Workspace.objects.create(name="CR WS", slug=f"cr-{uuid.uuid4().hex[:8]}", owner=owner)
    Membership.objects.create(workspace=ws, user=owner, role=Membership.Role.OWNER)
    return ws


@pytest.fixture
def conn(workspace):
    c = IGAccountConnection.objects.create(
        workspace=workspace,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="cruser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    c.access_token = "mock_token_cr"
    c.save()
    return c


@pytest.fixture
def client(owner):
    c = APIClient()
    c.force_authenticate(user=owner)
    return c


def _media() -> str:
    return f"m_{uuid.uuid4().hex[:16]}"


def _campaign(conn, media_id, **kw):
    defaults = {
        "ig_connection": conn,
        "name": f"CR {uuid.uuid4().hex[:6]}",
        "media_id": media_id,
        "message_template": "안녕하세요",
        "status": AutoDMCampaign.Status.ACTIVE,
        "started_at": timezone.now(),
    }
    defaults.update(kw)
    return AutoDMCampaign.objects.create(**defaults)


def _log(campaign, media_id, *, status, subcode="", when=None, kind=None):
    log = SentDMLog.objects.create(
        campaign=campaign,
        media_id=media_id,
        comment_id=f"c_{uuid.uuid4().hex[:12]}",
        recipient_user_id=f"u_{uuid.uuid4().hex[:12]}",
        idempotency_key=uuid.uuid4().hex,
        status=status,
        error_subcode=subcode,
        dm_kind=kind or SentDMLog.DMKind.OPENING,
    )
    if when:
        SentDMLog.objects.filter(pk=log.pk).update(created_at=when)
        log.refresh_from_db()
    return log


def _seen(conn, media_id, source, when=None):
    s = SeenComment.objects.create(
        ig_connection=conn,
        comment_id=f"sc_{uuid.uuid4().hex[:12]}",
        media_id=media_id,
        source=source,
        expires_at=timezone.now() + timedelta(days=10),
    )
    if when:
        SeenComment.objects.filter(pk=s.pk).update(created_at=when)
    return s


# ── 1. history 신호 ───────────────────────────────────────────────────
class TestHistorySignal:
    def test_실패만_쌓인_게시물은_제한_확정(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        v = icr.check_history(mid)
        assert v.state == icr.STATE_RESTRICTED
        assert v.source == icr.SOURCE_HISTORY
        assert v.blocking is True
        assert v.user_reason == "post_restricted"
        assert v.evidence["failures"] == icr.HISTORY_MIN_FAILURES

    def test_마지막_성공이_실패보다_뒤면_제한_아님(self, conn):
        """백필은 실패·성공을 뒤섞는다 — '최근에 성공했는가'가 판정 기준이다."""
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)
        _log(c, mid, status=SentDMLog.Status.DELIVERED)  # 마지막이 성공

        v = icr.check_history(mid)
        assert v.state == icr.STATE_OK
        assert v.blocking is False

    def test_실패가_임계_미만이면_판정하지_않음(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES - 1):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        assert icr.check_history(mid).state == icr.STATE_UNKNOWN

    def test_이력_없으면_unknown(self, db):
        assert icr.check_history(_media()).state == icr.STATE_UNKNOWN


# ── 2. runtime 신호 ───────────────────────────────────────────────────
class TestRuntimeSignal:
    def test_웹훅_침묵에_연속실패면_의심(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        old = timezone.now() - timedelta(hours=3)
        _seen(conn, mid, SeenComment.Source.WEBHOOK, when=old)
        for _ in range(6):
            _seen(conn, mid, SeenComment.Source.POLL)
        for _ in range(icr.RUNTIME_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        v = icr.check_runtime(mid)
        assert v.state == icr.STATE_SUSPECTED
        assert v.source == icr.SOURCE_RUNTIME
        # B2(2026-09-08) — 의심은 생성·활성화를 막지 않는다. blocking 은 게이트와 동치다.
        assert v.blocking is False
        assert v.evidence["poll_only_comments_after_last_webhook"] == 6

    def test_성공이_섞여_있으면_의심하지_않음(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.RUNTIME_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)
        _log(c, mid, status=SentDMLog.Status.DELIVERED)

        v = icr.check_runtime(mid)
        assert v.state == icr.STATE_OK
        assert v.blocking is False

    def test_시간창_밖_실패는_세지_않는다(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        long_ago = timezone.now() - timedelta(days=5)
        for _ in range(icr.RUNTIME_MIN_FAILURES + 3):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB, when=long_ago)

        assert icr.check_runtime(mid).state == icr.STATE_UNKNOWN


# ── 3. live 신호 ──────────────────────────────────────────────────────
class TestLiveSignal:
    def test_설정_꺼져있으면_외부호출을_하지_않는다(self, settings):
        settings.IG_RESTRICTION_LIVE_CHECK_ENABLED = False
        with patch("apps.integrations.ig_content_restriction.requests.get") as g:
            v = icr.check_live("https://www.instagram.com/reel/X/", media_id=_media())
        g.assert_not_called()
        assert v.state == icr.STATE_UNKNOWN

    def test_마커가_있으면_제한_확정(self, settings):
        settings.IG_RESTRICTION_LIVE_CHECK_ENABLED = True
        body = "x" * 700_000 + icr._LIVE_MARKER
        resp = MagicMock(status_code=200, text=body)
        with patch("apps.integrations.ig_content_restriction.requests.get", return_value=resp):
            v = icr.check_live(
                "https://www.instagram.com/reel/A/", media_id=_media(), use_cache=False
            )
        assert v.state == icr.STATE_RESTRICTED
        assert v.source == icr.SOURCE_LIVE
        assert v.blocking is True

    def test_마커_없고_본문이_크면_정상(self, settings):
        settings.IG_RESTRICTION_LIVE_CHECK_ENABLED = True
        resp = MagicMock(status_code=200, text="y" * 950_000)
        with patch("apps.integrations.ig_content_restriction.requests.get", return_value=resp):
            v = icr.check_live(
                "https://www.instagram.com/reel/B/", media_id=_media(), use_cache=False
            )
        assert v.state == icr.STATE_OK
        assert v.blocking is False

    def test_네트워크_실패는_unknown_이지_차단이_아니다(self, settings):
        settings.IG_RESTRICTION_LIVE_CHECK_ENABLED = True
        import requests as _rq

        with patch(
            "apps.integrations.ig_content_restriction.requests.get",
            side_effect=_rq.RequestException("boom"),
        ):
            v = icr.check_live(
                "https://www.instagram.com/reel/C/", media_id=_media(), use_cache=False
            )
        assert v.state == icr.STATE_UNKNOWN
        assert v.blocking is False


# ── 4. 합성 판정 + 유저 페이로드 ──────────────────────────────────────
class TestInspectAndPayload:
    def test_강한_판정이_약한_판정을_이긴다(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        v = icr.inspect_media(mid)
        assert v.state == icr.STATE_RESTRICTED
        # 근거는 신호별로 나눠 담긴다
        assert set(v.evidence) >= {"history", "runtime", "live", "live_check_enabled"}

    def test_페이로드에_유저_문구가_담긴다(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        p = icr.restriction_payload(icr.inspect_media(mid))
        assert p["state"] == icr.STATE_RESTRICTED
        assert p["user_message"]
        assert "시크릿" in p["how_to_check"]
        assert len(p["next_steps"]) >= 3

    def test_정상이면_문구가_비어_있다(self):
        p = icr.restriction_payload(icr.RestrictionVerdict(state=icr.STATE_OK))
        assert p["user_message"] == ""
        assert p["next_steps"] == []


# ── 5. 생성·복사 게이트 ───────────────────────────────────────────────
@pytest.mark.django_db
class TestCreateAndCopyGate:
    def test_제한된_게시물엔_캠페인을_만들_수_없다(self, client, conn, workspace):
        mid = _media()
        dead = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(dead, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        r = client.post(
            f"/api/v1/integrations/auto-dm-campaigns/?workspace_id={workspace.id}",
            {
                "ig_connection_id": str(conn.id),
                "name": "새 캠페인",
                "media_id": mid,
                "message_template": "안녕하세요",
            },
            format="json",
        )
        assert r.status_code == 409
        assert r.json()["error"]["details"]["code"] == "media_content_restricted"

    def test_정상_게시물은_그대로_생성된다(self, client, conn, workspace):
        r = client.post(
            f"/api/v1/integrations/auto-dm-campaigns/?workspace_id={workspace.id}",
            {
                "ig_connection_id": str(conn.id),
                "name": "정상 캠페인",
                "media_id": _media(),
                "message_template": "안녕하세요",
            },
            format="json",
        )
        assert r.status_code == 201

    def test_죽은_게시물이어도_복사는_허용된다(self, client, conn):
        """복사본은 INACTIVE 라 한 건도 안 나간다 — 게시물을 바꿔 쓰는 정상 흐름을 막지 않는다.

        2026-09-08 제품 결정(프론트 B5). 실제 피해는 '활성화'에서 나므로 게이트는 거기 둔다.
        """
        mid = _media()
        src = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(src, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        r = client.post(f"/api/v1/integrations/auto-dm-campaigns/{src.id}/copy/", {}, format="json")
        assert r.status_code == 201
        assert r.json()["status"] == AutoDMCampaign.Status.INACTIVE

    def test_제한된_게시물은_재개가_막힌다(self, client, conn):
        """실서버 CS #6d5b14ce — 막힌 릴스를 다시 켜면 실패만 쌓인다."""
        mid = _media()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        r = client.post(f"/api/v1/integrations/auto-dm-campaigns/{c.id}/resume/")
        assert r.status_code == 409
        assert r.json()["error"]["details"]["code"] == "media_content_restricted"
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.PAUSED

    def test_제한된_게시물은_PATCH_활성화도_막힌다(self, client, conn):
        mid = _media()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        r = client.patch(
            f"/api/v1/integrations/auto-dm-campaigns/{c.id}/",
            {"status": AutoDMCampaign.Status.ACTIVE},
            format="json",
        )
        assert r.status_code == 409
        assert r.json()["error"]["details"]["code"] == "media_content_restricted"

    def test_의심_단계는_생성을_막지_않는다(self, client, conn, workspace):
        """오탐으로 정상 캠페인을 막는 쪽이 손해가 크다 — suspected 는 통과."""
        mid = _media()
        now = timezone.now()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        # history 를 '정상'으로 만들려면 **첫 실패 이후에** 성공이 충분히 있어야 한다.
        # 그 성공들이 runtime 24h 창 밖에 있어야 runtime 은 의심으로 남는다.
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(
                c,
                mid,
                status=SentDMLog.Status.FAILED_NO_TRACE,
                subcode=SUB,
                when=now - timedelta(days=5),
            )
        for _ in range(icr.HISTORY_MIN_FAIL_AFTER_SUCCESS + 3):
            _log(c, mid, status=SentDMLog.Status.DELIVERED, when=now - timedelta(days=4))
        # 최근 24h: 웹훅 끊긴 뒤 실패만
        _seen(conn, mid, SeenComment.Source.WEBHOOK, when=now - timedelta(hours=3))
        for _ in range(icr.RUNTIME_MIN_FAILURES):
            _seen(conn, mid, SeenComment.Source.POLL)
            _log(
                c,
                mid,
                status=SentDMLog.Status.FAILED_NO_TRACE,
                subcode=SUB,
                when=now - timedelta(hours=2),
            )
        assert icr.check_history(mid).state == icr.STATE_OK
        v = icr.inspect_media(mid)
        assert v.state == icr.STATE_SUSPECTED
        # B2 — blocking 은 게이트와 동치여야 한다. 의심은 막지 않으므로 False.
        assert v.blocking is False

        r = client.post(
            f"/api/v1/integrations/auto-dm-campaigns/?workspace_id={workspace.id}",
            {
                "ig_connection_id": str(conn.id),
                "name": "의심이지만 생성",
                "media_id": mid,
                "message_template": "안녕하세요",
            },
            format="json",
        )
        assert r.status_code == 201


# ── 6. 점검 API ───────────────────────────────────────────────────────
@pytest.mark.django_db
class TestInspectEndpoints:
    def test_생성전_점검은_제한이어도_200(self, client, conn, workspace):
        mid = _media()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        r = client.get(
            "/api/v1/integrations/auto-dm-campaigns/inspect-media/"
            f"?workspace_id={workspace.id}&media_id={mid}"
        )
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["can_create_campaign"] is False
        assert d["restriction"]["state"] == icr.STATE_RESTRICTED
        assert d["restriction"]["user_message"]

    def test_생성전_점검_필수파라미터(self, client, workspace):
        r = client.get(
            f"/api/v1/integrations/auto-dm-campaigns/inspect-media/?workspace_id={workspace.id}"
        )
        assert r.status_code == 400

    def test_캠페인_점검은_상태를_바꾸지_않는다(self, client, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        r = client.get(f"/api/v1/integrations/auto-dm-campaigns/{c.id}/inspect/")
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["restriction"]["state"] == icr.STATE_RESTRICTED
        assert d["stats"]["opening_failed_2534066"] == icr.HISTORY_MIN_FAILURES
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.ACTIVE  # report-only


# ── 7. 자동 정지 스위퍼 ───────────────────────────────────────────────
@pytest.mark.django_db
class TestSweeper:
    def test_제한된_활성캠페인을_정지시킨다(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        out = sweep_restricted_campaigns()
        assert out["paused"] >= 1
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.PAUSED
        assert c.auto_paused_at is not None
        assert c.auto_paused_reason == "post_restricted"

    def test_dry_run_은_바꾸지_않는다(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        out = sweep_restricted_campaigns(dry_run=True)
        assert out["paused"] == 0
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.ACTIVE

    def test_이미_자동정지된_건_다시_손대지_않는다(self, conn):
        mid = _media()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.ACTIVE)
        c.auto_paused_at = timezone.now() - timedelta(days=1)
        c.auto_paused_reason = "post_restricted"
        c.save()
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        sweep_restricted_campaigns()
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.ACTIVE  # 사용자가 재개한 것을 존중

    def test_정상_캠페인은_건드리지_않는다(self, conn):
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(3):
            _log(c, mid, status=SentDMLog.Status.DELIVERED)

        sweep_restricted_campaigns()
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.ACTIVE

    def test_스위퍼는_외부호출을_하지_않는다(self, conn, settings):
        settings.IG_RESTRICTION_LIVE_CHECK_ENABLED = True  # 켜져 있어도
        mid = _media()
        c = _campaign(conn, mid)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        with patch("apps.integrations.ig_content_restriction.requests.get") as g:
            sweep_restricted_campaigns()
        g.assert_not_called()


# ── 8. 재개 시 표식 해제 ──────────────────────────────────────────────
@pytest.mark.django_db
class TestResumeClearsMark:
    def test_사용자가_재개하면_자동정지_표식이_지워진다(self, client, conn):
        mid = _media()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        c.auto_paused_at = timezone.now()
        c.auto_paused_reason = "post_restricted"
        c.save()

        r = client.post(f"/api/v1/integrations/auto-dm-campaigns/{c.id}/resume/")
        assert r.status_code == 200
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.ACTIVE
        assert c.auto_paused_at is None
        assert c.auto_paused_reason == ""


# ── 9. 프론트 계약 (2026-09-08 B1·B2·B3·B6) ───────────────────────────
@pytest.mark.django_db
class TestFrontendContract:
    def test_409_payload_는_원본_타입을_지킨다(self, client, conn, workspace):
        """B1 — DRF 기본 핸들러는 detail 말단값을 전부 문자열로 바꾼다. 우회했는지 확인."""
        mid = _media()
        dead = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(dead, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)

        r = client.post(
            f"/api/v1/integrations/auto-dm-campaigns/?workspace_id={workspace.id}",
            {
                "ig_connection_id": str(conn.id),
                "name": "타입 확인",
                "media_id": mid,
                "message_template": "안녕하세요",
            },
            format="json",
        )
        assert r.status_code == 409
        rest = r.json()["error"]["details"]["restriction"]
        assert rest["blocking"] is True, "bool 이어야 한다 (문자열 'True' 아님)"
        assert isinstance(rest["evidence"]["history"]["failures"], int)
        assert rest["evidence"]["history"]["last_success_at"] is None
        assert rest["evidence"]["live_check_enabled"] is False
        # 200 응답과 같은 블록이어야 한다 — 안내 문구도 함께 온다
        assert rest["user_message"]
        assert rest["how_to_check"]
        assert isinstance(rest["next_steps"], list) and rest["next_steps"]

    def test_can_create_campaign_이_실제_게이트와_일치한다(self, client, conn, workspace):
        """B2 — suspected 는 실제로 201 로 생성되므로 can_create_campaign 도 true 여야 한다."""
        mid = _media()
        holder = _campaign(conn, _media(), status=AutoDMCampaign.Status.PAUSED)
        n = icr.HISTORY_MIN_FAILURES - 1
        for _ in range(n):
            log = _log(holder, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)
            SentDMLog.objects.filter(pk=log.pk).update(media_id=mid)
        assert icr.inspect_media(mid).state == icr.STATE_SUSPECTED

        r = client.get(
            "/api/v1/integrations/auto-dm-campaigns/inspect-media/"
            f"?workspace_id={workspace.id}&media_id={mid}"
        )
        d = r.json()["data"]
        assert d["restriction"]["state"] == icr.STATE_SUSPECTED
        assert d["can_create_campaign"] is True
        assert d["restriction"]["blocking"] is False

        # 실제 생성도 통과해야 한다 (계약 일치 확인)
        r2 = client.post(
            f"/api/v1/integrations/auto-dm-campaigns/?workspace_id={workspace.id}",
            {
                "ig_connection_id": str(conn.id),
                "name": "의심 통과",
                "media_id": mid,
                "message_template": "안녕하세요",
            },
            format="json",
        )
        assert r2.status_code == 201

    def test_inspect_stats_에_고유_사용자_수가_있다(self, client, conn):
        """B3 — 한 사람이 두 번 실패해도 '명' 은 1 이어야 한다."""
        mid = _media()
        c = _campaign(conn, mid)
        dup_user = f"u_{uuid.uuid4().hex[:12]}"
        for _ in range(4):
            SentDMLog.objects.create(
                campaign=c,
                media_id=mid,
                comment_id=f"c_{uuid.uuid4().hex[:12]}",
                recipient_user_id=dup_user,  # 같은 사람이 여러 번
                idempotency_key=uuid.uuid4().hex,
                status=SentDMLog.Status.FAILED_NO_TRACE,
                error_subcode=SUB,
                dm_kind=SentDMLog.DMKind.OPENING,
            )
        _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB)  # 다른 사람 1명

        r = client.get(f"/api/v1/integrations/auto-dm-campaigns/{c.id}/inspect/")
        st = r.json()["data"]["stats"]
        assert st["opening_failed_2534066"] == 5, "시도 횟수"
        assert st["opening_failed_unique_users"] == 2, "사람 수"

    def test_auto_paused_필터와_counts(self, client, conn, workspace):
        """B6 — ?auto_paused=true 필터 + summary counts.auto_paused."""
        normal = _campaign(conn, _media(), status=AutoDMCampaign.Status.PAUSED)
        auto = _campaign(conn, _media(), status=AutoDMCampaign.Status.PAUSED)
        auto.auto_paused_at = timezone.now()
        auto.auto_paused_reason = "post_restricted"
        auto.save()

        r = client.get(
            f"/api/v1/integrations/auto-dm-campaigns/?ig_connection_id={conn.id}&auto_paused=true"
        )
        assert r.status_code == 200
        j = r.json()
        rows = j if isinstance(j, list) else j.get("results", [])
        ids = {row["id"] for row in rows}
        assert str(auto.id) in ids
        assert str(normal.id) not in ids

        r2 = client.get(
            f"/api/v1/integrations/auto-dm-campaigns/?ig_connection_id={conn.id}&auto_paused=false"
        )
        j2 = r2.json()
        rows2 = j2 if isinstance(j2, list) else j2.get("results", [])
        ids2 = {row["id"] for row in rows2}
        assert str(normal.id) in ids2
        assert str(auto.id) not in ids2

        r3 = client.get(
            f"/api/v1/integrations/auto-dm-campaigns/summary/"
            f"?workspace_id={workspace.id}&ig_connection_id={conn.id}"
        )
        assert r3.status_code == 200
        counts = r3.json()["counts"]
        assert counts["auto_paused"] == 1
        assert counts["paused"] == 2, "auto_paused 는 paused 의 부분집합"


# ── 10. 증거 유효기간 (영구 잠금 방지) ─────────────────────────────────
@pytest.mark.django_db
class TestStaleEvidence:
    def test_오래된_실패_이력은_확정하지_않는다(self, conn):
        """활성화 게이트가 restricted 를 막으므로, 낡은 증거로 영구 잠기면 안 된다."""
        mid = _media()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        old = timezone.now() - icr.HISTORY_STALE_AFTER - timedelta(days=1)
        for _ in range(icr.HISTORY_MIN_FAILURES + 5):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB, when=old)

        v = icr.check_history(mid)
        assert v.state == icr.STATE_UNKNOWN
        assert v.evidence["evidence_stale"] is True
        assert icr.inspect_media(mid).blocking is False

    def test_최근_실패는_그대로_확정한다(self, conn):
        mid = _media()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        recent = timezone.now() - timedelta(days=1)
        for _ in range(icr.HISTORY_MIN_FAILURES):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB, when=recent)

        v = icr.check_history(mid)
        assert v.state == icr.STATE_RESTRICTED
        assert v.evidence["evidence_stale"] is False

    def test_낡은_증거면_재개가_다시_열린다(self, client, conn):
        mid = _media()
        c = _campaign(conn, mid, status=AutoDMCampaign.Status.PAUSED)
        old = timezone.now() - icr.HISTORY_STALE_AFTER - timedelta(days=1)
        for _ in range(icr.HISTORY_MIN_FAILURES + 5):
            _log(c, mid, status=SentDMLog.Status.FAILED_NO_TRACE, subcode=SUB, when=old)
        c.auto_paused_at = old
        c.auto_paused_reason = "post_restricted"
        c.save()

        r = client.post(f"/api/v1/integrations/auto-dm-campaigns/{c.id}/resume/")
        assert r.status_code == 200
        c.refresh_from_db()
        assert c.status == AutoDMCampaign.Status.ACTIVE
        assert c.auto_paused_at is None
