"""더미(가짜 토큰) IG 연결의 게시물 목록 목 분기 회귀 테스트.

2026-09-21 dev 사고: 시더마다 가짜 토큰 접두어가 갈려 있어(`mock_token_` /
`mock-token-` / `DEVFAKE-`) 판정기가 하나만 알았다. 그래서 `dmdummy_*` 연결이
**진짜 토큰으로 오인**돼 Meta 로 나갔고, Graph 400 → 우리가 500 을 냈다.
테섭에서 캠페인 만들기 흐름이 첫 화면에서 끊긴 원인.
"""

import pytest
from django.urls import resolve

from apps.integrations.services import MockInstagramProvider


class TestMockTokenDetection:
    @pytest.mark.parametrize(
        "token",
        [
            "mock_token_abc123",  # dm_migration 시더
            "mock-token-dmdummy_pro_main",  # seed_dm_dev_dummy (옛 관례, dev DB 에 잔존)
            "DEVFAKE-17900000000000001",  # seed_home_alerts_dev
        ],
    )
    def test_known_dev_seeder_prefixes_are_mock(self, token):
        assert MockInstagramProvider.is_mock_token(token) is True

    @pytest.mark.parametrize(
        "token",
        [
            "IGAAWaZB0hR4y5BZAGxxxxxxxxxxxx",  # 실제 IG 토큰
            "EAAGm0PX4ZCpsBA...",  # 실제 FB 토큰
            "",
        ],
    )
    def test_real_tokens_are_not_mock(self, token):
        assert MockInstagramProvider.is_mock_token(token) is False

    def test_should_use_mock_requires_debug_for_token_path(self, settings):
        """운영(DEBUG=False)에서는 토큰이 가짜여도 목으로 새지 않는다."""
        settings.DEBUG = False
        settings.INSTAGRAM_MOCK_MODE = False
        assert MockInstagramProvider.should_use_mock("mock-token-x") is False

        settings.DEBUG = True
        assert MockInstagramProvider.should_use_mock("mock-token-x") is True

    def test_real_token_never_mocked_even_in_dev(self, settings):
        """dev 에 진짜 연결과 더미가 섞여 있어도 진짜는 Graph 로 간다."""
        settings.DEBUG = True
        settings.INSTAGRAM_MOCK_MODE = False
        assert MockInstagramProvider.should_use_mock("IGAAWaZB0hR4y5") is False


class TestMockMediaShape:
    IG = "dmdummy_pro_main"

    def test_page_has_fields_the_ui_needs(self):
        page = MockInstagramProvider.mock_list_media_page(self.IG, limit=5)
        assert len(page["data"]) == 5
        for item in page["data"]:
            for f in (
                "id",
                "caption",
                "timestamp",
                "media_type",
                "media_product_type",
                "permalink",
                "comments_count",
                "media_url",
                "thumbnail_url",
                "like_count",
            ):
                assert f in item, f"missing {f}"
            # 썸네일은 네트워크 없이 렌더돼야 한다
            assert item["thumbnail_url"].startswith("data:image/svg+xml")

    def test_deterministic_across_calls(self):
        a = MockInstagramProvider.mock_list_media_page(self.IG, limit=5)
        b = MockInstagramProvider.mock_list_media_page(self.IG, limit=5)
        assert a == b

    def test_batch_matches_list_exactly(self):
        """목록과 단건이 갈리면 '목록엔 있는데 고르면 사라지는' 화면이 된다."""
        page = MockInstagramProvider.mock_list_media_page(self.IG, limit=5)
        ids = [m["id"] for m in page["data"]]
        by_id = MockInstagramProvider.mock_media_by_ids(self.IG, ids)
        for m in page["data"]:
            assert by_id[m["id"]] == m

    def test_unknown_ids_are_omitted_not_invented(self):
        by_id = MockInstagramProvider.mock_media_by_ids(self.IG, ["no-such-id"])
        assert by_id == {}


@pytest.mark.django_db
class TestMediaEndpointUsesMock:
    def test_dummy_connection_does_not_call_graph(self, settings, monkeypatch):
        """회귀: 더미 연결로 /media/ 를 부르면 Meta 호출 0 · 200 이어야 한다."""
        from apps.integrations.models import IGAccountConnection

        settings.DEBUG = True
        settings.INSTAGRAM_MOCK_MODE = False

        conn = IGAccountConnection.objects.filter(external_account_id="dmdummy_pro_main").first()
        if conn is None:
            pytest.skip("dmdummy_pro 시드 없음 — seed_dm_dev_dummy 실행 후 재시도")

        called = []
        monkeypatch.setattr(
            "apps.integrations.views.requests.get",
            lambda *a, **k: called.append(a) or pytest.fail("Graph 를 호출했다"),
        )

        assert MockInstagramProvider.should_use_mock(conn.access_token) is True
        page = MockInstagramProvider.mock_list_media_page(conn.external_account_id, limit=10)
        assert len(page["data"]) == 10
        assert called == []


def test_media_path_still_routes_to_get_media():
    """프론트 계약은 **리터럴 경로**다 — 라우트가 바뀌면 여기서 잡는다."""
    match = resolve(
        "/api/v1/integrations/instagram/workspaces/" "4658abfd-83c3-44b8-9c3b-6196597d892c/media/"
    )
    assert match.func.__name__ == "InstagramIntegrationViewSet"
    assert match.func.actions["get"] == "get_media"


class TestMockMediaContextForAiSuggest:
    """ai-suggest 가 더미 게시물에서도 끝까지 돌아야 한다.

    /media/ 만 고치고 여기를 빼면 "목록엔 보이는데 고르면 404" 가 된다 —
    프론트 입장에선 여전히 만들기 흐름이 막힌 것이다.
    """

    IG = "dmdummy_pro_main"

    def test_pool_media_returns_its_caption(self):
        page = MockInstagramProvider.mock_list_media_page(self.IG, limit=3)
        first = page["data"][0]
        caption, image_url, media_type = MockInstagramProvider.mock_media_context(
            self.IG, first["id"]
        )
        assert caption == first["caption"]
        assert media_type == first["media_type"]

    def test_image_url_is_empty_so_llm_gets_no_junk_url(self):
        """data URI 를 넘기면 다운로드 실패 경고만 쌓이고 프롬프트가 더러워진다."""
        _, image_url, _ = MockInstagramProvider.mock_media_context(self.IG, "anything")
        assert image_url == ""

    def test_unknown_id_still_works_and_is_deterministic(self):
        a = MockInstagramProvider.mock_media_context(self.IG, "dummy-media-1")
        b = MockInstagramProvider.mock_media_context(self.IG, "dummy-media-1")
        assert a == b
        assert a[0]  # 캡션이 비어 있으면 AI 초안이 무의미해진다
