"""스팸 필터 DM 분리 + LLM(gemma) 하이브리드 테스트.

- classify_comment: 규칙 즉시차단 / 짧은 텍스트 skip / LLM(mock) 스팸·정상·저신뢰·fail-open
- run_spam_filter_check: 캠페인 없어도 검사(결합 해제), self·플랜·비활성 skip, 외부 답글 검사,
  멱등(중복 웹훅), auto_hide on/off, mock 토큰 Meta 미호출
- 대시보드 집계 / 수동 hide·unhide 게이트
"""

import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from apps.billing.models import SubscriptionPlan, UserSubscription
from apps.integrations import spam_classifier
from apps.integrations.models import (
    AutoDMCampaign,
    IGAccountConnection,
    SpamCommentLog,
    SpamFilterConfig,
)
from apps.integrations.spam_classifier import classify_comment
from apps.integrations.tasks import run_spam_filter_check
from apps.workspace.models import Membership, Workspace

User = get_user_model()

Status = SpamCommentLog.Status
CfgStatus = SpamFilterConfig.Status


# ───────────────────────── helpers ─────────────────────────


def _user(prefix="spam"):
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4().hex[:10]}@example.com", password="Pass1234!"
    )


def _ws(user):
    ws = Workspace.objects.create(name="sp-ws", slug=f"sp-{uuid.uuid4().hex[:10]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws


def _conn(ws, token="mock_token_dev", status=IGAccountConnection.Status.ACTIVE, ext=None):
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=ext or f"ig_{uuid.uuid4().hex[:12]}",
        username=f"u{uuid.uuid4().hex[:6]}",
        account_type="BUSINESS",
        status=status,
        last_verified_at=timezone.now(),
    )
    conn.access_token = token
    conn.save()
    return conn


def _give_plan(user, plan_name):
    plan = SubscriptionPlan.objects.get(name=plan_name)
    sub, _ = UserSubscription.objects.get_or_create(user=user, defaults={"plan": plan})
    sub.plan = plan
    sub.status = "active"
    sub.current_period_end = timezone.now() + timedelta(days=20)
    sub.save()
    return sub


def _payload(
    conn, comment_id, text, *, from_id="commenter1", username="c", media_id="m1", parent=None
):
    value = {
        "id": comment_id,
        "text": text,
        "from": {"id": from_id, "username": username},
        "media": {"id": media_id},
    }
    if parent:
        value["parent_id"] = parent
    return {"field": "comments", "value": value, "entry_id": conn.external_account_id}


def _run(payload):
    """태스크 실행 후, 단일 연결 시나리오면 그 연결의 결과를 언랩해서 반환.

    태스크는 계정의 활성 필터 연결마다 팬아웃해 ``{"status":"processed","results":[...]}`` 를
    돌려주지만, 테스트는 연결 1개만 세팅하므로 결과 1건을 그대로 꺼내 단언을 단순화한다.
    스킵/에러(top-level)는 그대로 반환.
    """
    r = run_spam_filter_check.apply(args=[payload]).get()
    if r.get("status") == "processed" and len(r.get("results", [])) == 1:
        return r["results"][0]
    return r


def _fake_llm(content):
    return mock.Mock(content=content)


# ───────────────────────── classify_comment ─────────────────────────


class TestClassifier:
    def test_rule_url_shortcircuits_no_llm(self):
        with mock.patch.object(spam_classifier, "call_llm_messages_with_usage") as m:
            v = classify_comment(
                "좋은 정보 http://spam.example now", spam_keywords=[], block_urls=True
            )
        assert v.is_spam and v.engine == "rule"
        m.assert_not_called()

    def test_rule_keyword_shortcircuits_no_llm(self):
        with mock.patch.object(spam_classifier, "call_llm_messages_with_usage") as m:
            v = classify_comment("아이돌 영상 보세요", spam_keywords=["아이돌"], block_urls=False)
        assert v.is_spam and v.engine == "rule"
        m.assert_not_called()

    def test_trivial_short_text_no_llm(self):
        with mock.patch.object(spam_classifier, "call_llm_messages_with_usage") as m:
            v = classify_comment("👍", spam_keywords=[], block_urls=True)
        assert not v.is_spam and v.engine == "rule_trivial"
        m.assert_not_called()

    def test_use_llm_false_skips_llm(self):
        with mock.patch.object(spam_classifier, "call_llm_messages_with_usage") as m:
            v = classify_comment(
                "사진 잘 봤어요 감사합니다", spam_keywords=[], block_urls=True, use_llm=False
            )
        assert not v.is_spam and v.engine == "rule_only"
        m.assert_not_called()

    def test_llm_spam_high_conf(self):
        fake = _fake_llm(
            '{"is_spam": true, "category": "scam", "reason": "betting link", "confidence": 0.95}'
        )
        with mock.patch.object(spam_classifier, "call_llm_messages_with_usage", return_value=fake):
            v = classify_comment(
                "맞팔 원하시면 디엠 주세요 대박 이벤트", spam_keywords=[], block_urls=True
            )
        assert v.is_spam and v.engine == "llm" and v.category == "scam"

    def test_llm_clean(self):
        fake = _fake_llm('{"is_spam": false, "category": "clean", "confidence": 0.9}')
        with mock.patch.object(spam_classifier, "call_llm_messages_with_usage", return_value=fake):
            v = classify_comment(
                "사진 너무 예뻐요 잘 보고 갑니다", spam_keywords=[], block_urls=True
            )
        assert not v.is_spam and v.engine == "llm"

    def test_llm_low_confidence_not_hidden(self):
        fake = _fake_llm('{"is_spam": true, "category": "promo", "confidence": 0.4}')
        with mock.patch.object(spam_classifier, "call_llm_messages_with_usage", return_value=fake):
            v = classify_comment("한번 보고 가세요 좋은 상품", spam_keywords=[], block_urls=True)
        assert not v.is_spam and v.engine == "llm_lowconf"

    def test_llm_failopen_on_exception(self):
        with mock.patch.object(
            spam_classifier, "call_llm_messages_with_usage", side_effect=RuntimeError("timeout")
        ):
            v = classify_comment(
                "애매한 댓글 무엇일까요 판단 필요", spam_keywords=[], block_urls=True
            )
        assert not v.is_spam and v.engine == "llm_failopen"

    def test_llm_failopen_on_bad_json(self):
        fake = _fake_llm("도저히 JSON 이 아닌 응답")
        with mock.patch.object(spam_classifier, "call_llm_messages_with_usage", return_value=fake):
            v = classify_comment(
                "애매한 댓글 무엇일까요 판단 필요", spam_keywords=[], block_urls=True
            )
        assert not v.is_spam and v.engine == "llm_failopen"


# ───────────────────────── run_spam_filter_check ─────────────────────────


@pytest.mark.django_db
class TestRunSpamFilterCheck:
    def _setup(
        self, *, plan="pro", active=True, auto_hide=False, use_llm=True, token="mock_token_dev"
    ):
        user = _user()
        if plan:
            _give_plan(user, plan)
        ws = _ws(user)
        conn = _conn(ws, token=token)
        sf = SpamFilterConfig.objects.create(
            ig_connection=conn,
            status=CfgStatus.ACTIVE if active else CfgStatus.INACTIVE,
            spam_keywords=["아이돌"],
            auto_hide_enabled=auto_hide,
            use_llm=use_llm,
        )
        return user, ws, conn, sf

    def test_checked_without_any_campaign(self):
        """결합 해제 회귀: AutoDMCampaign 이 전혀 없어도 검사된다."""
        _, _, conn, sf = self._setup()
        r = _run(_payload(conn, "c1", "좋은글 http://spam.example 링크"))
        assert r["status"] == "detected"  # 규칙 URL 스팸 + auto_hide off
        log = SpamCommentLog.objects.get(spam_filter=sf, comment_id="c1")
        assert log.status == Status.DETECTED

    def test_self_comment_skipped(self):
        _, _, conn, sf = self._setup()
        r = _run(_payload(conn, "c2", "http://x.example", from_id=conn.external_account_id))
        assert r["reason"] == "self_comment"
        assert not SpamCommentLog.objects.filter(comment_id="c2").exists()

    def test_external_reply_checked(self):
        """대댓글(외부 답글)도 검사한다(우리 답글은 self 로 걸러짐)."""
        _, _, conn, sf = self._setup()
        r = _run(_payload(conn, "c3", "아이돌 영상", parent="parent-1"))
        assert r["status"] == "detected"

    def test_idempotent_duplicate_webhook(self):
        _, _, conn, sf = self._setup()
        _run(_payload(conn, "c4", "http://x.example"))
        r2 = _run(_payload(conn, "c4", "http://x.example"))
        assert r2["reason"] == "already_processed"
        assert SpamCommentLog.objects.filter(spam_filter=sf, comment_id="c4").count() == 1

    def test_auto_hide_calls_meta(self):
        _, _, conn, sf = self._setup(auto_hide=True, token="live_token_not_mock")
        with mock.patch(
            "apps.integrations.tasks.InstagramCommentService.hide_comment",
            return_value={"success": True},
        ) as m:
            r = _run(_payload(conn, "c5", "아이돌"))
        m.assert_called_once()
        assert r["status"] == "hidden"
        log = SpamCommentLog.objects.get(comment_id="c5")
        assert log.status == Status.HIDDEN and log.hidden_at is not None

    def test_auto_hide_mock_token_no_meta_call(self):
        _, _, conn, sf = self._setup(auto_hide=True, token="mock_token_dev")
        with mock.patch("apps.integrations.tasks.InstagramCommentService.hide_comment") as m:
            r = _run(_payload(conn, "c6", "아이돌"))
        m.assert_not_called()
        assert r["status"] == "hidden_mock"
        assert SpamCommentLog.objects.get(comment_id="c6").status == Status.HIDDEN

    def test_detected_only_when_auto_hide_off(self):
        _, _, conn, sf = self._setup(auto_hide=False)
        with mock.patch("apps.integrations.tasks.InstagramCommentService.hide_comment") as m:
            r = _run(_payload(conn, "c7", "아이돌"))
        m.assert_not_called()
        assert r["status"] == "detected"
        assert SpamCommentLog.objects.get(comment_id="c7").status == Status.DETECTED

    def test_clean_comment_stays_clean(self):
        _, _, conn, sf = self._setup(use_llm=True)
        fake = _fake_llm('{"is_spam": false, "confidence": 0.9}')
        with mock.patch(
            "apps.integrations.spam_classifier.call_llm_messages_with_usage", return_value=fake
        ):
            r = _run(_payload(conn, "c8", "사진 잘 봤어요 감사합니다 좋은 하루"))
        assert r["status"] == "clean"
        assert SpamCommentLog.objects.get(comment_id="c8").status == Status.CLEAN

    def test_inactive_filter_skipped(self):
        _, _, conn, sf = self._setup(active=False)
        r = _run(_payload(conn, "c9", "http://x.example"))
        assert r["reason"] == "filter_inactive"
        assert not SpamCommentLog.objects.filter(comment_id="c9").exists()

    def test_free_plan_gated(self):
        _, _, conn, sf = self._setup(plan="free")
        r = _run(_payload(conn, "c10", "http://x.example"))
        assert r["reason"] == "plan_not_allowed"
        assert not SpamCommentLog.objects.filter(comment_id="c10").exists()

    def test_multi_connection_same_account_fans_out(self):
        """같은 IG 계정이 여러 워크스페이스에 연결돼도, 활성 필터 연결에서 검사된다(팬아웃 회귀).

        연결 A(필터 config 없음) + 연결 B(필터 active). .first() 가 A 를 집어도 스킵되면 안 됨.
        """
        ext = f"ig_shared_{uuid.uuid4().hex[:8]}"
        ua = _user()
        _give_plan(ua, "pro")
        _conn(_ws(ua), ext=ext)  # A: config 없음

        ub = _user()
        _give_plan(ub, "pro")
        conn_b = _conn(_ws(ub), ext=ext)  # B: config active
        sf_b = SpamFilterConfig.objects.create(
            ig_connection=conn_b,
            status=CfgStatus.ACTIVE,
            spam_keywords=["아이돌"],
            auto_hide_enabled=False,
        )

        payload = {
            "field": "comments",
            "value": {
                "id": "cshared",
                "text": "아이돌 영상 원본",
                "from": {"id": "z", "username": "z"},
                "media": {"id": "m"},
            },
            "entry_id": ext,
        }
        r = run_spam_filter_check.apply(args=[payload]).get()
        assert r["status"] == "processed"
        assert SpamCommentLog.objects.filter(spam_filter=sf_b, comment_id="cshared").exists()

    def test_unknown_connection_skipped(self):
        user = _user()
        _give_plan(user, "pro")
        payload = {
            "field": "comments",
            "value": {
                "id": "c11",
                "text": "http://x.example",
                "from": {"id": "z", "username": "z"},
                "media": {"id": "m"},
            },
            "entry_id": "ig_nonexistent_999",
        }
        assert _run(payload)["reason"] == "no_active_connection"


# ───────────────────────── 캠페인 트리거 댓글 면제 ─────────────────────────
#
# 회귀(2026-07-21 3dragon_pd): 활성 캠페인 트리거 키워드("풀버전"·"가이드🔥"·"비밀코드"·
# "ㅋㄹㄷ" 등)로 댓글을 단 팬이 gemma 에 adult/scam/promo 로 오분류돼 스팸 감지됨(DM 은 정상
# 발송). 캠페인을 실제 발동시키는 댓글(media+keyword)은 스팸 분류에서 제외해야 한다.


@pytest.mark.django_db
class TestCampaignTriggerExempt:
    def _setup(self, *, kw="풀버전", media="m1"):
        user = _user()
        _give_plan(user, "pro")
        ws = _ws(user)
        conn = _conn(ws, token="live_token_not_mock")  # 실토큰 → 숨김 시 (mock 처리된) Meta 호출
        sf = SpamFilterConfig.objects.create(
            ig_connection=conn,
            status=CfgStatus.ACTIVE,
            spam_keywords=["아이돌"],
            auto_hide_enabled=True,  # 면제가 없으면 숨김까지 갔을 상황
            use_llm=True,
        )
        AutoDMCampaign.objects.create(
            ig_connection=conn,
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id=media,
            keyword_filter=[kw],
            name="trigger camp",
            message_template="hi",
            status=AutoDMCampaign.Status.ACTIVE,
        )
        return conn, sf

    def test_trigger_keyword_comment_is_exempt(self):
        """캠페인 트리거 댓글은 LLM 이 스팸이라 해도 분류 자체를 건너뛰고 CLEAN 유지."""
        conn, sf = self._setup(kw="풀버전", media="m1")
        spam = _fake_llm('{"is_spam": true, "confidence": 0.95, "category": "adult"}')
        with (
            mock.patch(
                "apps.integrations.spam_classifier.call_llm_messages_with_usage", return_value=spam
            ) as llm,
            mock.patch("apps.integrations.tasks.InstagramCommentService.hide_comment") as hide,
        ):
            r = _run(_payload(conn, "cx1", "풀버전", media_id="m1"))
        assert r["status"] == "clean"
        assert r["engine"] == "campaign_trigger_exempt"
        llm.assert_not_called()  # 분류 자체를 하지 않음
        hide.assert_not_called()  # 숨김도 없음
        log = SpamCommentLog.objects.get(spam_filter=sf, comment_id="cx1")
        assert log.status == Status.CLEAN

    def test_nonmatching_media_still_classified(self):
        """캠페인은 m1 인데 댓글이 다른 게시물(m2)이면 면제 안 됨 → 정상 분류."""
        conn, sf = self._setup(kw="풀버전", media="m1")
        spam = _fake_llm('{"is_spam": true, "confidence": 0.95, "category": "adult"}')
        with (
            mock.patch(
                "apps.integrations.spam_classifier.call_llm_messages_with_usage", return_value=spam
            ),
            mock.patch(
                "apps.integrations.tasks.InstagramCommentService.hide_comment",
                return_value={"success": True},
            ),
        ):
            r = _run(_payload(conn, "cx2", "풀버전", media_id="m2"))
        assert r["status"] == "hidden"  # 면제 없음 → LLM 스팸 → auto_hide

    def test_non_trigger_comment_still_classified(self):
        """트리거 키워드가 아닌 진짜 스팸 댓글은 그대로 분류/감지된다."""
        conn, sf = self._setup(kw="풀버전", media="m1")
        with mock.patch(
            "apps.integrations.tasks.InstagramCommentService.hide_comment",
            return_value={"success": True},
        ):
            r = _run(_payload(conn, "cx3", "아이돌 영상 원본", media_id="m1"))
        assert r["status"] == "hidden"  # 규칙 키워드 "아이돌" 스팸 (면제 대상 아님)


# ───────────────────────── 대시보드 ─────────────────────────


@pytest.mark.django_db
class TestDashboard:
    def test_dashboard_counts_and_excludes_clean(self):
        user = _user()
        _give_plan(user, "pro")
        ws = _ws(user)
        conn = _conn(ws)
        sf = SpamFilterConfig.objects.create(ig_connection=conn, status=CfgStatus.ACTIVE)

        def _log(cid, status):
            return SpamCommentLog.objects.create(
                spam_filter=sf,
                comment_id=cid,
                comment_text="x",
                commenter_user_id="u",
                commenter_username="u",
                status=status,
            )

        _log("det1", Status.DETECTED)
        _log("det2", Status.DETECTED)
        h = _log("hid1", Status.HIDDEN)
        h.hidden_at = timezone.now()
        h.save(update_fields=["hidden_at"])
        _log("cln1", Status.CLEAN)  # 통계 제외돼야 함

        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(f"/api/v1/integrations/spam-filters/ig-connections/{conn.id}/dashboard/")
        assert resp.status_code == 200
        s = resp.data["summary"]
        # 감지 = DETECTED(2) + HIDDEN(1) = 3, CLEAN 제외
        assert s["total_detected"] == 3
        assert s["today_detected"] == 3
        assert s["total_hidden"] == 1
        assert s["today_hidden"] == 1
        assert len(resp.data["chart_14d"]) == 14
        assert set(resp.data["biweekly"].keys()) == {
            "avg_detected",
            "avg_hidden",
            "max_detected",
            "max_hidden",
        }


# ───────────────────────── 수동 모더레이션 ─────────────────────────


@pytest.mark.django_db
class TestModeration:
    def _log(self, user, *, plan, status=Status.DETECTED, token="mock_token_dev"):
        _give_plan(user, plan)
        ws = _ws(user)
        conn = _conn(ws, token=token)
        sf = SpamFilterConfig.objects.create(ig_connection=conn, status=CfgStatus.ACTIVE)
        log = SpamCommentLog.objects.create(
            spam_filter=sf,
            comment_id=f"m-{uuid.uuid4().hex[:6]}",
            comment_text="x",
            commenter_user_id="u",
            commenter_username="u",
            status=status,
        )
        if status == Status.HIDDEN:
            log.hidden_at = timezone.now()
            log.save(update_fields=["hidden_at"])
        return conn, sf, log

    def test_pro_manual_hide_then_unhide(self):
        user = _user()
        _, _, log = self._log(user, plan="pro")
        client = APIClient()
        client.force_authenticate(user=user)

        r = client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/hide/")
        assert r.status_code == 200
        log.refresh_from_db()
        assert log.status == Status.HIDDEN

        r2 = client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/unhide/")
        assert r2.status_code == 200
        log.refresh_from_db()
        assert log.status == Status.DETECTED and log.hidden_at is None

    def test_free_hide_gated(self):
        user = _user()
        _, _, log = self._log(user, plan="free")
        client = APIClient()
        client.force_authenticate(user=user)
        r = client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/hide/")
        assert r.status_code == 403

    def test_free_unhide_ungated(self):
        user = _user()
        _, _, log = self._log(user, plan="free", status=Status.HIDDEN)
        client = APIClient()
        client.force_authenticate(user=user)
        r = client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/unhide/")
        assert r.status_code == 200

    def test_cannot_moderate_other_workspace(self):
        owner = _user()
        _, _, log = self._log(owner, plan="pro")
        stranger = _user()
        _give_plan(stranger, "pro")
        client = APIClient()
        client.force_authenticate(user=stranger)
        r = client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/hide/")
        assert r.status_code == 403

    def test_config_patch_toggles(self):
        user = _user()
        _give_plan(user, "pro")
        ws = _ws(user)
        conn = _conn(ws)
        client = APIClient()
        client.force_authenticate(user=user)
        r = client.patch(
            f"/api/v1/integrations/spam-filters/ig-connections/{conn.id}/",
            {"auto_hide_enabled": True, "use_llm": False},
            format="json",
        )
        assert r.status_code == 200
        assert r.data["auto_hide_enabled"] is True
        assert r.data["use_llm"] is False


@pytest.mark.django_db
class TestModerationThrottle:
    """숨김/복원 버튼 연타 제한 — Meta quota 소진·밴 방지."""

    def _log(self, user, plan="pro", status=Status.DETECTED, token="mock_token_dev"):
        _give_plan(user, plan)
        ws = _ws(user)
        conn = _conn(ws, token=token)
        sf = SpamFilterConfig.objects.create(ig_connection=conn, status=CfgStatus.ACTIVE)
        log = SpamCommentLog.objects.create(
            spam_filter=sf,
            comment_id=f"m-{uuid.uuid4().hex[:6]}",
            comment_text="x",
            commenter_user_id="u",
            commenter_username="u",
            status=status,
        )
        if status == Status.HIDDEN:
            log.hidden_at = timezone.now()
            log.save(update_fields=["hidden_at"])
        return conn, sf, log

    def test_double_hide_same_comment_returns_429(self):
        from django.core.cache import cache

        cache.clear()
        user = _user()
        _, _, log = self._log(user, plan="pro")
        client = APIClient()
        client.force_authenticate(user=user)

        r1 = client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/hide/")
        assert r1.status_code == 200

        # 같은 댓글 같은 액션(hide) 연타 → 쿨다운으로 즉시 429
        r2 = client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/hide/")
        assert r2.status_code == 429
        assert r2.data["error"]["details"]["code"] == "moderation_rate_limited"
        assert r2.data["error"]["details"]["reason"] == "per_comment_cooldown"
        assert int(r2["Retry-After"]) > 0

    def test_hide_then_unhide_same_comment_allowed(self):
        # hide 직후 unhide '정정'은 액션이 달라 쿨다운을 공유하지 않으므로 허용.
        from django.core.cache import cache

        cache.clear()
        user = _user()
        _, _, log = self._log(user, plan="pro")
        client = APIClient()
        client.force_authenticate(user=user)

        assert (
            client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/hide/").status_code == 200
        )
        assert (
            client.post(f"/api/v1/integrations/spam-filters/logs/{log.id}/unhide/").status_code
            == 200
        )

    def test_per_minute_cap_blocks_burst(self, settings):
        # 계정당 분당 상한 초과 시 차단(서로 다른 댓글이라 쿨다운은 미적용).
        from django.core.cache import cache

        from apps.integrations.rate_governor import moderation_action_check

        cache.clear()
        settings.MODERATION_RATE_LIMITS = {
            "per_minute": 2,
            "per_hour": 1000,
            "per_comment_cooldown": 0,
        }
        acct = f"ig_{uuid.uuid4().hex[:8]}"
        assert moderation_action_check(acct, "c1", "hide").allowed
        assert moderation_action_check(acct, "c2", "hide").allowed
        d3 = moderation_action_check(acct, "c3", "hide")
        assert not d3.allowed
        assert d3.reason == "per_minute" and d3.retry_after > 0

    def test_missing_account_bypasses(self):
        # 계정 식별 불가 시엔 사용자 액션을 막지 않는다(안전 측).
        from apps.integrations.rate_governor import moderation_action_check

        assert moderation_action_check("", "c1", "hide").allowed
