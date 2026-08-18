"""DM 이전 — 타사 래퍼 링크를 원본으로 되돌리기 (:mod:`apps.integrations.dm_migration.links`).

여기 있는 입력은 **prod 실데이터에서 뽑은 모양**이다(2026-08-18, 후보 1,597건 · 고유 URL
425개). 래퍼 포맷은 남의 서비스가 정하는 것이라 우리가 상상한 모양으로 테스트하면
의미가 없다.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import cache

from apps.integrations.dm_migration import links
from apps.integrations.dm_migration.analyze import find_urls
from apps.integrations.models import DMMigrationJob

# ── 실데이터 표본 ──
INPOCK = (
    "https://link.inpock.co.kr/r?url=https%3A//open.kakao.com/o/gi3MUPji"
    "&code=669fb8c8b77c4303985485542204a522"
)
L_INSTAGRAM = (
    "https://l.instagram.com/?u=https%3A%2F%2Fapps.apple.com%2Fkr%2Fapp%2Fhailuo%2Fid6741675037"
    "&e=AUDIF01V-fCm2ZYbX_xKBrJIoWufYqD6WduQ"
)
SOCIALBIZ = (
    "https://socialbiz-c.nhndata.com/webhook/messenger/callback/redirect-view"
    "?uuid=be7333b1-bcf1-4faa-8fad-b361c03543c1&type=TEXT_BUTTON_URL&recipientId=18009298202668044"
)
MANYCHAT = (
    "https://my.manychat.com/r?act=7db3f989e020797cae2c8812382b7472&u=475938975&p=864903&h=f7"
)
MANYCHAT_PAGE = """<html><head>
<link href="https://app.manychat.com/css/proxy/proxy.css?2519" rel="stylesheet">
<link href="https://mccdn.me/assets/img/favicons/favicon_logo.png" rel="icon">
</head><body>
<a href="https://app.wadiz.kr/links/2Z8vTlxOpS?mcp_token=eyJwaWQiOjg2NDkwM30.2XFz">계속</a>
</body></html>"""


def _littly(inner: str) -> str:
    """리틀리 링크 모양 — 경로의 JWT 페이로드에 목적지가 들어 있다."""
    payload = json.dumps({"instagramReplyInstanceId": 4305418, "url": inner, "iss": "litt.ly"})
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"https://litt.ly/t/eyJhbGciOiJFUzI1NiJ9.{body}.SIGNATURE"


class _FakeResp:
    def __init__(self, status=302, location="", text=""):
        self.status_code = status
        self.headers = {"location": location} if location else {}
        self.text = text


class _FakeClient:
    """조회를 대신한다. **어떤 URL 로 두드렸는지**까지 기록한다 — 소셜비즈는 우리가
    recipientId 를 0 으로 바꿔 두드려야 하고, 그게 이 기능의 예의다."""

    def __init__(self, routes: dict, fail: set | None = None):
        self.routes = routes
        self.fail = fail or set()
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        host = urlsplit(url).netloc
        if host in self.fail:
            raise OSError("연결 실패")
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return _FakeResp(status=404)

    def close(self):
        pass


# 실제 클래스를 임포트 시점에 붙잡아 둔다 — `pipeline.links` 는 이 모듈과 **같은 객체**라
# monkeypatch 로 Resolver 를 갈면 아래 팩토리가 자기 자신을 부른다(RecursionError).
_RealResolver = links.Resolver


def _resolver(client, **kw):
    r = _RealResolver(**kw)
    r._client = client
    return r


@pytest.fixture(autouse=True)
def _clear_link_cache():
    cache.clear()
    yield
    cache.clear()


# ── 1. 오프라인 — 호출 0으로 푸는 래퍼 ──
class TestOfflineUnwrap:
    def test_inpock_param(self):
        final, how = links.unwrap_url(INPOCK)
        assert final == "https://open.kakao.com/o/gi3MUPji"
        assert "param" in how

    def test_instagram_param(self):
        final, _ = links.unwrap_url(L_INSTAGRAM)
        assert final == "https://apps.apple.com/kr/app/hailuo/id6741675037"

    def test_littly_jwt_payload(self):
        final, how = links.unwrap_url(_littly("https://mega.apob.ai/reelsdrgn"))
        assert final == "https://mega.apob.ai/reelsdrgn"
        assert "jwt" in how

    def test_wrapper_inside_wrapper(self):
        """리틀리가 인스타 래퍼를 감싼 실제 모양 — 재귀로 끝까지 푼다."""
        final, how = links.unwrap_url(_littly(L_INSTAGRAM))
        assert final == "https://apps.apple.com/kr/app/hailuo/id6741675037"
        assert how.startswith("jwt+param")

    def test_plain_url_is_untouched(self):
        """사장님이 직접 쓴 링크는 손대지 않는다 — 우리가 링크를 바꿨다는 의심을 살 일이 없어야 한다."""
        for u in (
            "https://buly.kr/CskwX6s",
            "https://bit.ly/49sguej",
            "https://melted-sunday-048.notion.site/33a?source=copy_link",
            "https://www.instagram.com/reel/C3SqJuhxpah/",
        ):
            final, how = links.unwrap_url(u)
            assert (final, how) == (u, "")

    def test_non_url_input_survives(self):
        for u in ("", "   ", "사진 참고", "프로필 링크", None):
            final, how = links.unwrap_url(u)
            assert how == ""
            assert final == (u or "")

    def test_schemeless_link_gets_https(self):
        """캠페인 링크 버튼은 http(s) 만 받는다 — 스킴이 없으면 **자동채택인데 불러오기가
        400** 난다(실측: prod 후보 1,597건 중 1건이 이 상태로 auto_draft 였다)."""
        final, how = links.unwrap_url("www.minimax.io/audio")
        assert final == "https://www.minimax.io/audio"
        assert how == "scheme"

    def test_schemeless_wrapper_is_still_unwrapped(self):
        final, how = links.unwrap_url(INPOCK.replace("https://", ""))
        assert final == "https://open.kakao.com/o/gi3MUPji"
        assert how == "scheme+param"

    def test_broken_jwt_falls_back_to_input(self):
        """남의 토큰 포맷이 바뀌어도 **입력을 그대로** 돌려준다(링크가 사라지면 안 된다)."""
        for bad in ("https://litt.ly/t/not-a-jwt", "https://litt.ly/t/aaa.!!!not-base64!!!.bbb"):
            final, _ = links.unwrap_url(bad)
            assert final == bad

    def test_generic_redirect_param_needs_a_foreign_absolute_url(self):
        """일반 `?to=` 는 **다른 호스트의 절대 URL** 일 때만 리다이렉터로 본다."""
        assert links.unwrap_url("https://shop.com/p?to=cart")[0] == "https://shop.com/p?to=cart"
        assert links.unwrap_url("https://x.com/go?to=https%3A%2F%2Fshop.com%2Fp")[0] == (
            "https://shop.com/p"
        )


# ── 2. 추적 파라미터 — 남의 것만 뗀다 ──
class TestStripTrackers:
    def test_manychat_token_is_stripped(self):
        final, how = links.unwrap_url("https://app.wadiz.kr/links/2Z8?mcp_token=eyJwaWQ.abc")
        assert final == "https://app.wadiz.kr/links/2Z8"
        assert "strip" in how

    def test_owner_affiliate_params_survive(self):
        """``refCode``·``sourceId``·``utm_*`` 는 **사장님의 제휴·귀속 코드**다. 떼면 수익이 사라진다."""
        src = "https://shop.com/p?refCode=ABC&sourceId=9&utm_source=instagram&recipientId=180"
        final, _ = links.unwrap_url(src)
        q = parse_qs(urlsplit(final).query)
        assert q["refCode"] == ["ABC"]
        assert q["sourceId"] == ["9"]
        assert q["utm_source"] == ["instagram"]
        assert "recipientId" not in q

    def test_no_tracker_means_byte_identical(self):
        """뗄 게 없으면 **문자열을 그대로** 돌려준다(재인코딩으로 모양이 바뀌면 안 된다)."""
        src = "https://n.site/x?a=%ED%95%9C%EA%B8%80&b=1&b=2&empty="
        assert links.strip_trackers(src) == (src, False)


# ── 3. 조회가 필요한 래퍼 ──
class TestNetworkResolve:
    def test_socialbiz_302_location(self):
        dest = "https://scientific-buckaroo-6db.notion.site/2024-VS-2025-2459?source=copy_link"
        client = _FakeClient({"nhndata.com": _FakeResp(302, dest)})
        with _resolver(client) as r:
            final, how = r.resolve(SOCIALBIZ)
        assert final == dest
        assert "fetch" in how

    def test_socialbiz_is_probed_with_recipient_zero(self):
        """실제 수신자 id 로 두드리면 **사장님의 타사 통계에 우리 클릭이 섞인다.**"""
        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/x")})
        with _resolver(client) as r:
            r.resolve(SOCIALBIZ)
        (called,) = client.calls
        assert parse_qs(urlsplit(called).query)["recipientId"] == ["0"]
        assert "18009298202668044" not in called

    def test_socialbiz_keeps_recipient_param_present_while_probing(self):
        """⚠️ 회귀 방지: 떼야 할 추적 파라미터를 **조회 전에** 떼면 소셜비즈가 302 를 주지
        않는다. 실측에서 이 순서를 틀려 uuid 75개가 전부 실패했다."""
        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/x")})
        with _resolver(client) as r:
            r.resolve(SOCIALBIZ)
        assert "recipientId" in client.calls[0]

    def test_manychat_html_href_minus_token(self):
        client = _FakeClient({"manychat.com": _FakeResp(200, "", MANYCHAT_PAGE)})
        with _resolver(client) as r:
            final, how = r.resolve(MANYCHAT)
        assert final == "https://app.wadiz.kr/links/2Z8vTlxOpS"  # mcp_token 제거됨
        assert "fetch" in how and "strip" in how

    def test_manychat_own_assets_are_not_mistaken_for_the_target(self):
        """중간 페이지에는 매니챗 자기 CSS·파비콘 href 가 섞여 있다."""
        page = MANYCHAT_PAGE.replace("?mcp_token=eyJwaWQiOjg2NDkwM30.2XFz", "")
        client = _FakeClient({"manychat.com": _FakeResp(200, "", page)})
        with _resolver(client) as r:
            final, _ = r.resolve(MANYCHAT)
        assert final == "https://app.wadiz.kr/links/2Z8vTlxOpS"

    def test_one_fetch_per_wrapper_key_not_per_recipient(self):
        """소셜비즈는 수신자마다 URL 이 다르다 — uuid 로 묶어야 591건이 75회가 된다."""
        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/x")})
        others = [SOCIALBIZ.replace("18009298202668044", str(1800 + i)) for i in range(6)]
        with _resolver(client) as r:
            got = r.resolve_many([SOCIALBIZ, *others])
        assert len(client.calls) == 1
        assert set(got.values()) == {"https://n.site/x"}

    def test_offline_wrappers_never_touch_the_network(self):
        client = _FakeClient({})
        with _resolver(client) as r:
            got = r.resolve_many([INPOCK, L_INSTAGRAM, _littly("https://a.b/c")])
        assert client.calls == []
        assert r.fetched == 0
        assert "https://open.kakao.com/o/gi3MUPji" in got.values()


# ── 4. 안전장치 — 실패하면 원본을 그대로 쓴다 ──
class TestFallsBackToTheOriginal:
    def test_network_error_keeps_the_original_link(self):
        client = _FakeClient({}, fail={"socialbiz-c.nhndata.com"})
        with _resolver(client) as r:
            final, _ = r.resolve(SOCIALBIZ)
        assert final == SOCIALBIZ  # 링크가 바뀌는 것보다 없어지는 게 나쁘다
        assert r.failed == 1

    def test_unusable_response_keeps_the_original_link(self):
        for resp in (_FakeResp(404), _FakeResp(200, "", "<html>없음</html>"), _FakeResp(302, "/x")):
            client = _FakeClient({"nhndata.com": resp})
            with _resolver(client) as r:
                final, _ = r.resolve(SOCIALBIZ)
            assert final == SOCIALBIZ

    def test_self_redirect_is_not_accepted(self):
        """같은 호스트로 도는 302 는 목적지가 아니다(무한 래퍼)."""
        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://socialbiz-c.nhndata.com/y")})
        with _resolver(client) as r:
            final, _ = r.resolve(SOCIALBIZ)
        assert final == SOCIALBIZ

    def test_flag_off_stops_fetching_but_keeps_offline_unwrap(self):
        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/x")})
        with _resolver(client, enabled=False) as r:
            assert r.resolve(SOCIALBIZ)[0] == SOCIALBIZ
            assert r.resolve(INPOCK)[0] == "https://open.kakao.com/o/gi3MUPji"
        assert client.calls == []

    def test_fetch_cap_keeps_the_rest_original(self):
        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/x")})
        urls = [SOCIALBIZ.replace("be7333b1", f"be7333b{i}") for i in range(5)]
        with _resolver(client, fetch_max=2) as r:
            got = r.resolve_many(urls)
        assert len(client.calls) == 2
        assert sum(1 for u in urls if got[u] == u) == 3  # 상한 넘은 것은 원본 유지


# ── 5. 캐시 — 같은 래퍼 키는 다시 두드리지 않는다 ──
class TestCacheAcrossJobs:
    def test_second_resolver_reuses_the_cache(self):
        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/x")})
        with _resolver(client) as r:
            r.resolve(SOCIALBIZ)
        client2 = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/CHANGED")})
        with _resolver(client2) as r2:
            final, how = r2.resolve(SOCIALBIZ)
        assert client2.calls == []
        assert final == "https://n.site/x"
        assert "cache" in how

    def test_a_dead_wrapper_is_not_hammered_every_run(self):
        client = _FakeClient({"nhndata.com": _FakeResp(404)})
        with _resolver(client) as r:
            r.resolve(SOCIALBIZ)
        client2 = _FakeClient({"nhndata.com": _FakeResp(404)})
        with _resolver(client2) as r2:
            assert r2.resolve(SOCIALBIZ)[0] == SOCIALBIZ
        assert client2.calls == []


# ── 6. 본문 안의 링크 ──
class TestRewriteText:
    def test_body_link_is_rewritten_too(self):
        """버튼만 바꾸고 본문을 놔두면 한 DM 안에서 두 링크가 갈린다."""
        text = f"자료는 여기에서 받으세요 → {INPOCK} 감사합니다!"
        out = links.rewrite_text(text, {INPOCK: "https://open.kakao.com/o/gi3MUPji"})
        assert out == "자료는 여기에서 받으세요 → https://open.kakao.com/o/gi3MUPji 감사합니다!"

    def test_prefix_overlap_picks_the_longer_wrapper(self):
        short = "https://my.manychat.com/r?act=abc"
        long = short + "&u=1&p=2"
        out = links.rewrite_text(
            f"{long} 그리고 {short}", {short: "https://a.b/SHORT", long: "https://a.b/LONG"}
        )
        assert out == "https://a.b/LONG 그리고 https://a.b/SHORT"

    def test_unmapped_and_empty_text_are_untouched(self):
        assert links.rewrite_text("https://a.b/c 만 있음", {}) == "https://a.b/c 만 있음"
        assert links.rewrite_text("", {INPOCK: "https://x"}) == ""

    def test_find_urls_trims_sentence_punctuation(self):
        got = find_urls("보기(https://a.b/c), 그리고 https://a.b/d. 끝 https://a.b/c")
        assert got == ["https://a.b/c", "https://a.b/d"]

    def test_find_urls_ignores_schemeless_text(self):
        """스킴 없는 ``www.`` 는 되돌릴 대상이 아니고, 치환 대상이 되면 본문을 잘못 건드린다."""
        assert find_urls("www.naver.com 참고") == []


# ── 7. 슬라이스와의 관계 ──
class TestLinkStageDoesNotEatSlices:
    """⚠️ 회귀 방지 — 이 단계는 **슬라이스를 접지 않는다.**

    링크 되돌리기는 링크 1개마다 짧게 끊긴다. 여기서 ``_SliceExhausted`` 를 올리면
    슬라이스 1개당 링크 1개만 처리하고 상한(MAX_SLICES)을 태워, 링크 때문에 잡이 미완으로
    끝난다(실제로 슬라이스 테스트가 이 사고를 잡아냈다).
    """

    def _stub_runner(self):
        from types import SimpleNamespace

        class _Stub:
            sd: dict = {}
            job = SimpleNamespace(id="job-1")

            def _persist(self, **kw):
                pass

            def _time_up(self):
                return True  # 항상 슬라이스가 끝난 상태

        s = _Stub()
        s.sd = {}
        return s

    def test_never_raises_slice_exhausted(self, monkeypatch):
        from apps.integrations.dm_migration import pipeline

        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/x")})
        monkeypatch.setattr(pipeline.links, "Resolver", lambda **kw: _resolver(client, **kw))
        recs = [{"offer": {"url": SOCIALBIZ.replace("be7333b1", f"be7333b{i}")}} for i in range(4)]

        got = pipeline._Runner._resolve_links(self._stub_runner(), recs)

        assert len(got) == 4
        assert all(v == "https://n.site/x" for v in got.values())

    def test_budget_exhaustion_keeps_originals_instead_of_failing(self, monkeypatch):
        from apps.integrations.dm_migration import pipeline

        client = _FakeClient({"nhndata.com": _FakeResp(302, "https://n.site/x")})
        monkeypatch.setattr(pipeline.links, "Resolver", lambda **kw: _resolver(client, **kw))
        monkeypatch.setattr(pipeline, "LINK_RESOLVE_SECONDS", -1)  # 즉시 예산 소진
        recs = [{"offer": {"url": SOCIALBIZ}}]

        got = pipeline._Runner._resolve_links(self._stub_runner(), recs)

        assert got == {}  # 아무것도 못 바꿨지만 예외 없이 넘어간다
        assert client.calls == []

    def test_body_and_gate_links_are_collected(self):
        from apps.integrations.dm_migration import pipeline

        rec = {
            "offer": {"url": SOCIALBIZ, "text": f"자료는 {INPOCK} 에서"},
            "gate": {"text": f"팔로우 확인 {L_INSTAGRAM}"},
        }
        got = pipeline._urls_in_recovery(rec)
        assert got == [SOCIALBIZ, INPOCK, L_INSTAGRAM]


# ── 8. 끝에서 끝까지 — 후보에 실제로 원본 링크가 담기나 ──
@pytest.mark.django_db
class TestCandidateCarriesTheOriginalLink:
    """사용자에게 나가는 것은 **되돌린 링크**, 근거로 남는 것은 **관측한 래퍼**."""

    def _run(self, monkeypatch, *, offer, page_routes=None):
        from unittest.mock import MagicMock

        from apps.integrations.dm_migration import pipeline
        from apps.integrations.test_dm_migration import _conn, _job, _user, _ws
        from apps.integrations.test_dm_migration_slices import _drive

        client = _FakeClient(page_routes or {"nhndata.com": _FakeResp(302, "https://n.site/real")})
        monkeypatch.setattr(pipeline.links, "Resolver", lambda **kw: _resolver(client, **kw))
        monkeypatch.setattr(
            pipeline.llm,
            "generate_drafts",
            lambda batch, *, model_code="deepseek": (
                {
                    c["media_id"]: {
                        "media_id": c["media_id"],
                        "name": "캠페인",
                        "description": "설명",
                        # LLM 초안이 본문 링크를 그대로 옮겨 적은 경우 — 여기도 바뀌어야 한다.
                        "first_dm_draft": c["template_text"],
                        "public_reply_draft": "확인해주세요",
                        "keywords": ["자료"],
                        "keyword_mode": "any",
                    }
                    for c in batch
                },
                {"llm_calls": 1, "llm_tokens": 10},
            ),
        )
        rec = {
            "media_id": "m1",
            "permalink": "https://x/1",
            "caption": "댓글에 자료 남겨주세요",
            "timestamp": "2026-07-01T00:00:00+0000",
            "comments_count": 30,
            "probed": 10,
            "trigger": "자료",
            "signal": True,
            "content_score": 0.8,
            "offer": offer,
            "gate": None,
            "grade": "auto_draft",
            "score": 0.72,
            "confirm_required": False,
            "keyword_hits": {"자료": 9},
        }
        conn = _conn(_ws(_user()), mock_token=True)
        job = _job(conn, estimated_seconds=10, stage_data={"media": [], "recoveries": [rec]})
        status, _runs = _drive(job, MagicMock())
        assert status == DMMigrationJob.Status.READY, status
        return job.candidates.get()

    def test_offer_url_is_the_real_destination_and_evidence_keeps_the_wrapper(self, monkeypatch):
        cand = self._run(
            monkeypatch,
            offer={"text": "자료 보내드려요", "url": SOCIALBIZ, "label": "받기", "hits": 8},
        )
        assert cand.offer_url == "https://n.site/real"
        assert cand.matched_template["recovered_url"] == SOCIALBIZ  # 근거는 관측값 그대로
        assert cand.matched_template["resolved_url"] == "https://n.site/real"

    def test_body_link_is_rewritten_in_the_message_too(self, monkeypatch):
        cand = self._run(
            monkeypatch,
            offer={"text": f"자료는 {INPOCK} 에서", "url": "", "hits": 8},
        )
        assert "inpock" not in cand.draft_opening_message
        assert "https://open.kakao.com/o/gi3MUPji" in cand.draft_opening_message

    def test_failure_leaves_the_original_link_intact(self, monkeypatch):
        """되돌리기가 실패해도 **링크는 살아 있어야 한다** — 이게 안전장치다."""
        cand = self._run(
            monkeypatch,
            offer={"text": "자료 보내드려요", "url": SOCIALBIZ, "hits": 8},
            page_routes={"nhndata.com": _FakeResp(500)},
        )
        assert cand.offer_url == SOCIALBIZ
        assert cand.matched_template["resolved_url"] == ""


# ── 9. 소급 명령 — 이미 만들어진 후보를 재수집 없이 고친다 ──
@pytest.mark.django_db
class TestResolveLinksCommand:
    """이미 몇 시간 걸려 만든 후보의 링크만 바꾼다 — 재수집은 다시 사는 것이라 안 한다."""

    def _cand(self, **kw):
        from apps.integrations.models import DMCampaignCandidate
        from apps.integrations.test_dm_migration import _conn, _job, _user, _ws

        conn = _conn(_ws(_user()), mock_token=True)
        job = _job(conn)
        defaults = {
            "job": job,
            "ig_connection": conn,
            "status": DMCampaignCandidate.Status.DETECTED,
            "band": DMCampaignCandidate.Band.AUTO_DRAFT,
            "media_id": "m1",
            "media_permalink": "https://www.instagram.com/reel/ABC/",
            "offer_url": SOCIALBIZ,
            "draft_opening_message": f"자료는 {SOCIALBIZ} 에서 받으세요",
            "matched_template": {"recovered_url": SOCIALBIZ, "recovered_text": "자료"},
        }
        defaults.update(kw)
        return job, DMCampaignCandidate.objects.create(**defaults)

    def _call(self, monkeypatch, job, *args, routes=None):
        from django.core.management import call_command

        from apps.integrations.dm_migration import links as links_mod

        client = _FakeClient(routes or {"nhndata.com": _FakeResp(302, "https://n.site/real")})
        monkeypatch.setattr(links_mod, "Resolver", lambda **kw: _resolver(client, **kw))
        call_command("dm_migration_resolve_links", "--job", str(job.id), *args)
        return client

    def test_preview_changes_nothing(self, monkeypatch):
        job, cand = self._cand()
        self._call(monkeypatch, job)
        cand.refresh_from_db()
        assert cand.offer_url == SOCIALBIZ

    def test_apply_rewrites_url_and_body_but_keeps_evidence(self, monkeypatch):
        job, cand = self._cand()
        self._call(monkeypatch, job, "--apply")
        cand.refresh_from_db()
        assert cand.offer_url == "https://n.site/real"
        assert "nhndata" not in cand.draft_opening_message
        assert cand.matched_template["recovered_url"] == SOCIALBIZ
        assert cand.matched_template["resolved_url"] == "https://n.site/real"

    def test_running_twice_is_a_no_op(self, monkeypatch):
        job, cand = self._cand()
        self._call(monkeypatch, job, "--apply")
        cand.refresh_from_db()
        first = (cand.offer_url, cand.draft_opening_message)
        client = self._call(monkeypatch, job, "--apply")
        cand.refresh_from_db()
        assert (cand.offer_url, cand.draft_opening_message) == first
        assert client.calls == []  # 되돌릴 게 없으면 두드리지도 않는다

    def test_offline_only_never_touches_the_network(self, monkeypatch):
        job, cand = self._cand(
            offer_url=INPOCK,
            draft_opening_message=f"자료는 {INPOCK}",
            matched_template={"recovered_url": INPOCK},
        )
        client = self._call(monkeypatch, job, "--offline-only", "--apply")
        cand.refresh_from_db()
        assert client.calls == []
        assert cand.offer_url == "https://open.kakao.com/o/gi3MUPji"

    def test_offline_only_leaves_fetch_wrappers_alone(self, monkeypatch):
        job, cand = self._cand()
        self._call(monkeypatch, job, "--offline-only", "--apply")
        cand.refresh_from_db()
        assert cand.offer_url == SOCIALBIZ  # 조회가 필요한 것은 손대지 않는다

    def _campaign(self, conn, *, source):
        from apps.integrations.models import AutoDMCampaign

        return AutoDMCampaign.objects.create(
            ig_connection=conn,
            name="캠페인",
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="m9",
            status=AutoDMCampaign.Status.ACTIVE,
            source=source,
            opening_message_template=f"자료는 {INPOCK} 에서",
            link_buttons=[{"url": INPOCK, "label": "받기"}],
            link_button_url=INPOCK,
        )

    def test_campaigns_flag_fixes_migrated_live_campaigns(self, monkeypatch):
        job, _cand = self._cand()
        camp = self._campaign(job.ig_connection, source="dm_migration")
        self._call(monkeypatch, job, "--campaigns", "--apply")
        camp.refresh_from_db()
        assert camp.link_buttons[0]["url"] == "https://open.kakao.com/o/gi3MUPji"
        assert camp.link_button_url == "https://open.kakao.com/o/gi3MUPji"
        assert "inpock" not in camp.opening_message_template
        assert camp.link_buttons[0]["label"] == "받기"  # 라벨은 건드리지 않는다

    def test_user_made_campaigns_are_never_touched(self, monkeypatch):
        """사용자가 직접 인포크 링크를 넣어 만든 캠페인은 **본인 선택**이다."""
        job, _cand = self._cand()
        mine = self._campaign(job.ig_connection, source="")
        self._call(monkeypatch, job, "--campaigns", "--apply")
        mine.refresh_from_db()
        assert mine.link_buttons[0]["url"] == INPOCK
        assert mine.link_button_url == INPOCK
        assert INPOCK in mine.opening_message_template

    def test_campaigns_are_untouched_without_the_flag(self, monkeypatch):
        job, _cand = self._cand()
        camp = self._campaign(job.ig_connection, source="dm_migration")
        self._call(monkeypatch, job, "--apply")
        camp.refresh_from_db()
        assert camp.link_buttons[0]["url"] == INPOCK


# ── 10. `[링크]` 자리표시자 — 치환하는 곳이 없어서 글자가 그대로 발송됐다 ──
class TestLinkPlaceholder:
    """실측(2026-08-19 prod): 후보 1,829건 · active 캠페인 42개 · 실제 발송 2건(둘 다 읽음)."""

    def test_brackets_are_removed_keeping_korean_particles(self):
        """``버튼`` 으로 바꾸면 조사가 깨진다 — 링크(종성 없음) vs 버튼(종성 있음)."""
        from apps.integrations.dm_migration.analyze import unwrap_link_placeholder as f

        cases = [
            ("아래 [링크]를 눌러 받아가세요!", "아래 링크를 눌러 받아가세요!"),
            ("아래 [링크]로 참여하실 수 있고", "아래 링크로 참여하실 수 있고"),
            (
                "자세한 자료는 [링크]에서 받아보실 수 있습니다.",
                "자세한 자료는 링크에서 받아보실 수 있습니다.",
            ),
            ("확인해 보세요 👇 [링크]", "확인해 보세요 👇 링크"),
            ("아래 [ 링크 ] 를 확인해주세요", "아래 링크 를 확인해주세요"),
            ("아래 [link]를 확인", "아래 링크를 확인"),
            ("【링크】 확인", "링크 확인"),
        ]
        for src, want in cases:
            assert f(src) == want, src

    def test_untouched_when_absent(self):
        from apps.integrations.dm_migration.analyze import unwrap_link_placeholder as f

        for t in ("", None, "아래 링크를 눌러주세요", "[중요] 확인 부탁드려요", "[이벤트] 참여"):
            assert f(t) == t

    def test_idempotent(self):
        from apps.integrations.dm_migration.analyze import unwrap_link_placeholder as f

        once = f("아래 [링크]를 눌러")
        assert f(once) == once

    @pytest.mark.django_db
    def test_pipeline_never_writes_a_placeholder_into_a_candidate(self, monkeypatch):
        """LLM 이 자리표시자를 내놔도 **후보에는 남지 않는다** (유일한 쓰기 지점에서 막는다)."""
        from unittest.mock import MagicMock

        from apps.integrations.dm_migration import pipeline
        from apps.integrations.test_dm_migration import _conn, _job, _user, _ws
        from apps.integrations.test_dm_migration_slices import _drive

        monkeypatch.setattr(
            pipeline.links, "Resolver", lambda **kw: _resolver(_FakeClient({}), **kw)
        )
        monkeypatch.setattr(
            pipeline.llm,
            "generate_drafts",
            lambda batch, *, model_code="deepseek": (
                {
                    c["media_id"]: {
                        "media_id": c["media_id"],
                        "name": "캠페인",
                        "description": "설명",
                        "first_dm_draft": "자료는 아래 [링크]를 눌러 받아가세요",
                        "public_reply_draft": "DM 확인해주세요",
                        "keywords": ["자료"],
                        "keyword_mode": "any",
                    }
                    for c in batch
                },
                {"llm_calls": 1, "llm_tokens": 10},
            ),
        )
        rec = {
            "media_id": "m1",
            "permalink": "https://x/1",
            "caption": "댓글에 자료 남겨주세요",
            "timestamp": "2026-07-01T00:00:00+0000",
            "comments_count": 30,
            "probed": 10,
            "trigger": "자료",
            "signal": True,
            "content_score": 0.8,
            "offer": {"text": "자료 보내드려요", "url": "https://a.b/c", "hits": 8},
            "gate": None,
            "grade": "auto_draft",
            "score": 0.72,
            "confirm_required": False,
            "keyword_hits": {"자료": 9},
        }
        conn = _conn(_ws(_user()), mock_token=True)
        job = _job(conn, estimated_seconds=10, stage_data={"media": [], "recoveries": [rec]})
        status, _runs = _drive(job, MagicMock())
        assert status == DMMigrationJob.Status.READY, status
        cand = job.candidates.get()
        assert "[링크]" not in cand.draft_opening_message
        assert cand.draft_opening_message == "자료는 아래 링크를 눌러 받아가세요"

    @pytest.mark.django_db
    def test_repair_command_fixes_live_campaigns(self, monkeypatch):
        """살아있는 캠페인도 고친다 — 이미 나가고 있는 문구가 문제였다."""
        from django.core.management import call_command

        from apps.integrations.models import AutoDMCampaign
        from apps.integrations.test_dm_migration import _conn, _user, _ws

        conn = _conn(_ws(_user()), mock_token=True)
        camp = AutoDMCampaign.objects.create(
            ig_connection=conn,
            name="이전됨",
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="m1",
            status=AutoDMCampaign.Status.ACTIVE,
            opening_message_template="아래 [링크]를 눌러 받아가세요",
            reward_message_template="보상은 [링크]에서",
            public_reply_templates=["DM 확인! [링크]"],
            link_buttons=[{"url": "https://a.b/c", "label": "받기"}],
        )
        call_command("dm_migration_fix_link_placeholder")  # 미리보기 — 쓰지 않는다
        camp.refresh_from_db()
        assert "[링크]" in camp.opening_message_template

        call_command("dm_migration_fix_link_placeholder", "--apply")
        camp.refresh_from_db()
        assert camp.opening_message_template == "아래 링크를 눌러 받아가세요"
        assert camp.reward_message_template == "보상은 링크에서"
        assert camp.public_reply_templates == ["DM 확인! 링크"]

    @pytest.mark.django_db
    def test_candidates_only_leaves_live_campaigns_alone(self):
        from django.core.management import call_command

        from apps.integrations.models import AutoDMCampaign
        from apps.integrations.test_dm_migration import _conn, _user, _ws

        conn = _conn(_ws(_user()), mock_token=True)
        camp = AutoDMCampaign.objects.create(
            ig_connection=conn,
            name="이전됨",
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id="m1",
            status=AutoDMCampaign.Status.ACTIVE,
            opening_message_template="아래 [링크]를 눌러",
        )
        call_command("dm_migration_fix_link_placeholder", "--candidates-only", "--apply")
        camp.refresh_from_db()
        assert camp.opening_message_template == "아래 [링크]를 눌러"


# ── 11. 판정용 표시 ──
class TestIsWrapped:
    def test_detects_both_kinds(self):
        assert links.is_wrapped(INPOCK) is True
        assert links.is_wrapped(SOCIALBIZ) is True
        assert links.is_wrapped(_littly("https://a.b/c")) is True

    def test_plain_links_are_not_flagged(self):
        assert links.is_wrapped("https://open.kakao.com/o/gi3MUPji") is False
        assert links.is_wrapped("") is False
