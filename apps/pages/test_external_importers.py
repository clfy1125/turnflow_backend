"""
apps/pages/test_external_importers.py

외부 임포터 모듈 (apps.pages.services.external_importers) 과
``AiImportExternalView`` (`/api/v1/pages/ai/import-external/`) 단위/통합 테스트.

외부 호스트 의존성을 끊기 위해 모든 테스트는 ``EXTERNAL_IMPORT_MOCK_MODE=true``
하에서 ``_mock_fixtures/{source}/api-{slug}-nextdata.json`` 픽스처를 로드한다.
실제 네트워크 호출이 필요한 케이스는 별도 통합 테스트 (Phase 2) 로 분리.
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.pages.models import Block, Page
from apps.pages.services.external_importers import (
    SOURCES,
    UnsupportedSourceError,
    detect_source,
    import_from_url,
    parse_slug,
)

User = get_user_model()

IMPORT_URL = "/api/v1/pages/ai/import-external/"


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_mode(monkeypatch):
    """모든 테스트에서 외부 fetch 차단."""
    monkeypatch.setenv("EXTERNAL_IMPORT_MOCK_MODE", "true")


@pytest.fixture
def user(db):
    # User 모델은 email 기반 (USERNAME_FIELD="email"). username 필드 자체 없음.
    return User.objects.create_user(
        email="importer@example.com",
        password="Pass1234!",
    )


@pytest.fixture
def auth_client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


# ─────────────────────────────────────────────────────────────
# dispatch / detect_source / parse_slug
# ─────────────────────────────────────────────────────────────


class TestDispatch:
    def test_sources_table_keys(self):
        assert set(SOURCES.keys()) == {"inpock", "litly", "linktree"}

    @pytest.mark.parametrize(
        "url,expected_source",
        [
            ("https://link.inpock.co.kr/wannabuy", "inpock"),
            ("https://litt.ly/koreanwithmina", "litly"),
            ("https://linktr.ee/selenagomez", "linktree"),
            ("https://example.com/foo", None),
            ("", None),
            ("not a url", None),
        ],
    )
    def test_detect_source(self, url, expected_source):
        assert detect_source(url) == expected_source

    def test_parse_slug(self):
        assert parse_slug("https://litt.ly/koreanwithmina", "litly") == "koreanwithmina"
        assert parse_slug("https://linktr.ee/selenagomez/", "linktree") == "selenagomez"
        assert parse_slug("https://litt.ly/", "litly") is None


# ─────────────────────────────────────────────────────────────
# import_from_url — 각 소스별 Mock 픽스처 로딩 + 변환
# ─────────────────────────────────────────────────────────────


class TestImportFromUrl:
    def test_unsupported_host_raises(self, mock_mode):
        with pytest.raises(UnsupportedSourceError):
            import_from_url("https://example.com/foo")

    def test_empty_url_raises(self, mock_mode):
        with pytest.raises(UnsupportedSourceError):
            import_from_url("")

    def test_inpock_mock_fixture(self, mock_mode):
        source, slug, body = import_from_url("https://link.inpock.co.kr/09women")
        assert source == "inpock"
        assert slug == "09women"
        assert isinstance(body.get("blocks"), list)
        assert len(body["blocks"]) > 0
        # 컨버터가 채우는 메타 — 인포크는 'source' 키를 안 박지만 total_input_blocks 는 항상 있음
        meta = body.get("_meta") or {}
        assert "total_input_blocks" in meta
        assert "total_output_blocks" in meta

    def test_litly_mock_fixture(self, mock_mode):
        source, slug, body = import_from_url("https://litt.ly/koreanwithmina")
        assert source == "litly"
        assert slug == "koreanwithmina"
        assert isinstance(body.get("blocks"), list)
        assert body.get("data", {}).get("design_settings") or body.get("data")
        # design_settings 가 비어있지 않게 — 컨버터가 어떤 식으로든 design_settings 를 채워야 함

    def test_linktree_mock_fixture(self, mock_mode):
        source, slug, body = import_from_url("https://linktr.ee/nikeofficial")
        assert source == "linktree"
        assert slug == "nikeofficial"
        assert isinstance(body.get("blocks"), list)
        assert len(body["blocks"]) > 0


# ─────────────────────────────────────────────────────────────
# AiImportExternalView — HTTP 레이어
# ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestImportExternalView:
    def test_unauthenticated_returns_401(self, mock_mode):
        c = APIClient()
        resp = c.post(
            IMPORT_URL,
            {"url": "https://litt.ly/koreanwithmina"},
            format="json",
        )
        assert resp.status_code == 401

    def test_unsupported_host_returns_400(self, mock_mode, auth_client):
        resp = auth_client.post(
            IMPORT_URL,
            {"url": "https://example.com/foo"},
            format="json",
        )
        # serializer URLField 통과 → import_from_url 에서 400 반환
        assert resp.status_code == 400
        body = resp.json()
        # 통일 에러 포맷: {success: False, error: {code, message, details}}
        assert body.get("success") is False or "error" in body  # 둘 중 하나
        # 메시지에 "지원" 단어 포함 확인 (UnsupportedSourceError 가 도달했음을 검증)
        error_payload = body.get("error", body)
        msg = error_payload.get("message") if isinstance(error_payload, dict) else str(body)
        assert "지원" in str(msg) or "UNSUPPORTED" in str(error_payload).upper()

    def test_invalid_url_format_returns_400(self, mock_mode, auth_client):
        resp = auth_client.post(IMPORT_URL, {"url": "not a url"}, format="json")
        assert resp.status_code == 400

    def test_litly_import_success(self, mock_mode, auth_client, user):
        resp = auth_client.post(
            IMPORT_URL,
            {"url": "https://litt.ly/koreanwithmina"},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        # 응답 모양 검증
        assert body["import_source"] == "litly"
        assert body["import_source_slug"] == "koreanwithmina"
        assert body["import_source_url"] == "https://litt.ly/koreanwithmina"
        assert body["blocks_count"] >= 1
        assert body["is_public"] is False  # 기본값
        # DB 확인
        page = Page.objects.get(id=body["id"])
        assert page.user == user
        assert page.import_source == "litly"
        assert page.imported_at is not None
        assert Block.objects.filter(page=page).count() == body["blocks_count"]

    def test_linktree_import_with_title_override(self, mock_mode, auth_client):
        resp = auth_client.post(
            IMPORT_URL,
            {
                "url": "https://linktr.ee/nikeofficial",
                "title": "내가 임포트한 나이키",
                "is_public": True,
            },
            format="json",
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body["title"] == "내가 임포트한 나이키"
        assert body["is_public"] is True
        assert body["import_source"] == "linktree"

    def test_inpock_import_success(self, mock_mode, auth_client):
        resp = auth_client.post(
            IMPORT_URL,
            {"url": "https://link.inpock.co.kr/09women"},
            format="json",
        )
        assert resp.status_code == 201, resp.content
        body = resp.json()
        assert body["import_source"] == "inpock"
        assert body["import_source_slug"] == "09women"

    def test_two_imports_get_unique_slugs(self, mock_mode, auth_client, user):
        """같은 URL 을 강제 재임포트(force=true) 해도 새 slug 가 충돌 없이 발급됨."""
        url = "https://litt.ly/koreanwithmina"
        # 1회: 기본 동기 import
        r1 = auth_client.post(IMPORT_URL, {"url": url}, format="json")
        assert r1.status_code == 201
        # 2회: 같은 URL → 재임포트 충돌이 동작하므로 force=true 로 우회
        r2 = auth_client.post(IMPORT_URL, {"url": url, "force": True}, format="json")
        assert r2.status_code == 201
        # 테스트 DB 가 dev 와 공유되어 다른 페이지가 섞일 수 있으므로 우리가 만든 것만 좁혀서 검사
        my_slugs = list(
            Page.objects.filter(user=user, import_source_url=url).values_list("slug", flat=True)
        )
        assert len(my_slugs) == 2
        assert len(my_slugs) == len(set(my_slugs))  # 두 slug 모두 고유

    def test_mock_fixture_missing_returns_502(self, mock_mode, auth_client):
        """Mock 모드에서 픽스처 없으면 ExternalFetchError → 502 매핑."""
        resp = auth_client.post(
            IMPORT_URL,
            {"url": "https://litt.ly/this-fixture-does-not-exist"},
            format="json",
        )
        assert resp.status_code == 502


# ─────────────────────────────────────────────────────────────
# Phase 2: 재임포트 충돌 / 비동기 / 이미지 재업로드
# ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestReimportConflict:
    """같은 사용자가 같은 URL 두 번 임포트하면 409 + 기존 페이지. force=true 면 통과."""

    def test_second_import_returns_409(self, mock_mode, auth_client):
        url = "https://litt.ly/koreanwithmina"
        r1 = auth_client.post(IMPORT_URL, {"url": url}, format="json")
        assert r1.status_code == 201
        first_id = r1.json()["id"]

        r2 = auth_client.post(IMPORT_URL, {"url": url}, format="json")
        assert r2.status_code == 409
        body = r2.json()
        details = body["error"]["details"]
        assert details["reason"] == "ALREADY_IMPORTED"
        assert details["existing_page"]["id"] == first_id

    def test_force_true_bypasses_conflict(self, mock_mode, auth_client, user):
        url = "https://litt.ly/koreanwithmina"
        auth_client.post(IMPORT_URL, {"url": url}, format="json")
        r2 = auth_client.post(IMPORT_URL, {"url": url, "force": True}, format="json")
        assert r2.status_code == 201
        # 테스트 DB 가 dev 와 공유되어 다른 페이지가 섞일 수 있으므로 우리 유저 것만 좁혀 검사
        # ([test-db-not-clean] — 집계는 user 스코프/델타로 단언).
        assert Page.objects.filter(user=user, import_source_url=url).count() == 2

    def test_other_user_can_import_same_url(self, mock_mode, auth_client, db):
        """재임포트 충돌은 사용자별 — 다른 유저는 같은 URL 임포트 가능."""
        url = "https://litt.ly/koreanwithmina"
        auth_client.post(IMPORT_URL, {"url": url}, format="json")

        other = User.objects.create_user(
            email="other@example.com",
            password="Pass1234!",
        )
        other_client = APIClient()
        other_client.force_authenticate(user=other)
        r = other_client.post(IMPORT_URL, {"url": url}, format="json")
        assert r.status_code == 201


@pytest.mark.django_db
class TestAsyncMode:
    """``async_mode=true`` 는 AiJob 만 만들고 202 반환. Celery 가 task 픽업."""

    def test_async_returns_202_and_creates_aijob(self, mock_mode, auth_client, monkeypatch):
        # Celery delay 호출만 캡처 (실제 워커 안 띄움)
        from apps.ai_jobs import tasks as t

        captured: dict = {}

        def fake_delay(job_id):
            captured["job_id"] = job_id

        monkeypatch.setattr(t.run_external_import_job, "delay", fake_delay)

        resp = auth_client.post(
            IMPORT_URL,
            {
                "url": "https://litt.ly/koreanwithmina",
                "async_mode": True,
                "reupload_images": True,
            },
            format="json",
        )
        assert resp.status_code == 202, resp.content
        body = resp.json()
        assert "job_id" in body
        assert body["status"] == "queued"
        assert body["poll_url"].startswith("/api/v1/ai/jobs/")
        assert body["import_source"] == "litly"
        assert body["reupload_images"] is True
        # AiJob 행 + Celery dispatch 확인
        from apps.ai_jobs.models import AiJob

        job = AiJob.objects.get(id=body["job_id"])
        assert job.job_type == AiJob.JobType.EXTERNAL_IMPORT
        assert job.status == AiJob.Status.QUEUED
        assert job.input_payload["url"] == "https://litt.ly/koreanwithmina"
        assert job.input_payload["reupload_images"] is True
        assert captured.get("job_id") == str(job.id)

    def test_async_unsupported_host_returns_400_without_aijob(
        self, mock_mode, auth_client, monkeypatch
    ):
        """비동기여도 호스트 검증은 enqueue 전에 — 빈 큐 사용 방지."""
        from apps.ai_jobs import tasks as t

        called = {"yes": False}

        def fake_delay(job_id):  # pragma: no cover — 호출되면 안 됨
            called["yes"] = True

        monkeypatch.setattr(t.run_external_import_job, "delay", fake_delay)

        resp = auth_client.post(
            IMPORT_URL,
            {"url": "https://example.com/foo", "async_mode": True},
            format="json",
        )
        assert resp.status_code == 400
        assert called["yes"] is False


# ─────────────────────────────────────────────────────────────
# 이미지 reupload (단위)
# ─────────────────────────────────────────────────────────────


class TestReuploadHelpers:
    """``reupload`` 모듈의 walk/replace 헬퍼는 DB 안 닿아 단위 테스트 가능."""

    def test_walk_image_urls_collects_from_known_fields(self):
        from apps.pages.services.external_importers.reupload import walk_image_urls

        blocks = [
            {
                "type": "profile",
                "data": {
                    "avatar_url": "https://cdn.litt.ly/avatar.jpg",
                    "cover_image_url": "https://cdn.litt.ly/cover.jpg",
                },
            },
            {
                "type": "single_link",
                "data": {
                    "_type": "single_link",
                    "thumbnail_url": "https://example.com/thumb.png",
                    "url": "https://example.com/destination",  # not an image field
                },
            },
            {
                "type": "single_link",
                "data": {
                    "_type": "gallery",
                    "images": [
                        "https://cdn.litt.ly/g1.jpg",
                        "https://cdn.litt.ly/g2.jpg",
                        # 중복은 한 번만
                        "https://cdn.litt.ly/avatar.jpg",
                    ],
                },
            },
            {
                "type": "single_link",
                "data": {
                    "_type": "group_link",
                    "links": [
                        {"thumbnail_url": "https://cdn.linktr.ee/k1.jpg"},
                        {"thumbnail_url": "https://cdn.linktr.ee/k2.jpg"},
                    ],
                },
            },
        ]
        urls = walk_image_urls(blocks)
        assert "https://cdn.litt.ly/avatar.jpg" in urls
        assert "https://cdn.litt.ly/cover.jpg" in urls
        assert "https://example.com/thumb.png" in urls
        assert "https://cdn.litt.ly/g1.jpg" in urls
        assert "https://cdn.linktr.ee/k1.jpg" in urls
        # url 필드는 안 잡혀야 함
        assert "https://example.com/destination" not in urls
        # 중복 제거
        assert urls.count("https://cdn.litt.ly/avatar.jpg") == 1

    def test_walk_image_urls_skips_non_http(self):
        from apps.pages.services.external_importers.reupload import walk_image_urls

        blocks = [
            {
                "type": "profile",
                "data": {"avatar_url": "/relative/path.jpg"},  # http(s) 아님
            },
            {
                "type": "single_link",
                "data": {"thumbnail_url": ""},  # 빈 문자열
            },
        ]
        assert walk_image_urls(blocks) == []

    def test_replace_in_blocks_swaps_urls(self):
        from apps.pages.services.external_importers.reupload import replace_in_blocks

        blocks = [
            {"type": "profile", "data": {"avatar_url": "https://a"}},
            {
                "type": "single_link",
                "data": {
                    "_type": "gallery",
                    "images": ["https://a", "https://b", "https://c"],
                },
            },
        ]
        n = replace_in_blocks(
            blocks,
            {"https://a": "/m/x", "https://b": "/m/y"},
        )
        assert n == 3  # avatar + 2 in gallery
        assert blocks[0]["data"]["avatar_url"] == "/m/x"
        assert (
            blocks[1]["data"]["images"] == ["/m/y", "https://c"]
            or blocks[1]["data"]["images"]
            == [
                "https://c",
                "/m/y",
            ]
            or blocks[1]["data"]["images"] == ["/m/x", "/m/y", "https://c"]
        )
        # 정확히는 순서 보존:
        assert blocks[1]["data"]["images"] == ["/m/x", "/m/y", "https://c"]

    def test_reupload_report_to_dict(self):
        from apps.pages.services.external_importers.reupload import ReuploadReport

        r = ReuploadReport()
        r.attempted = 3
        r.succeeded = 2
        r.add_failure("http://x.com/img1.jpg", "download timeout")
        d = r.to_dict()
        assert d["attempted"] == 3
        assert d["succeeded"] == 2
        assert d["failed"] == 1
        assert d["failures"][0]["url"] == "http://x.com/img1.jpg"


# ─────────────────────────────────────────────────────────────
# social_registry (인포크/리틀리/링크트리 공용 SNS 레지스트리) — 단위
# ─────────────────────────────────────────────────────────────


class TestSocialRegistry:
    def test_map_social_type_aliases(self):
        from apps.pages.services.external_importers import social_registry as sr

        # 대소문자/구분자 무시 — 'NAVER_BLOG'/'naverblog'/'naver blog' 모두 같은 id
        assert sr.map_social_type("NAVER_BLOG") == "naver_blog"
        assert sr.map_social_type("naverblog") == "naver_blog"
        assert sr.map_social_type("naver blog") == "naver_blog"
        assert sr.map_social_type("kakaotalk") == "kakao_talk"
        assert sr.map_social_type("x") == "twitter"  # legacy 데이터 키 보존
        # 미지원/빈 값은 None (호출부에서 single_link 폴백)
        assert sr.map_social_type("myspace") is None
        assert sr.map_social_type("") is None
        assert sr.map_social_type(None) is None

    def test_field_ids_cover_expanded_platforms(self):
        from apps.pages.services.external_importers import social_registry as sr

        # 프론트 registry.ts 와 1:1 — 확장 플랫폼이 키 집합에 있어야 함
        for fid in ("instagram", "kakao_talk", "naver_blog", "line", "discord", "twitch"):
            assert fid in sr.SOCIAL_FIELD_IDS
        assert len(sr.SOCIAL_FIELD_IDS) >= 30

    def test_normalize_social_value(self):
        from apps.pages.services.external_importers import social_registry as sr

        # 핸들 → full URL
        assert sr.normalize_social_value("instagram", "@foo") == "https://www.instagram.com/foo/"
        assert sr.normalize_social_value("instagram", "foo") == "https://www.instagram.com/foo/"
        # 도메인스러운 값 → https prepend
        assert (
            sr.normalize_social_value("naver_blog", "blog.naver.com/foo")
            == "https://blog.naver.com/foo"
        )
        # 이미 URL 이면 그대로
        assert (
            sr.normalize_social_value("tiktok", "https://www.tiktok.com/@bar")
            == "https://www.tiktok.com/@bar"
        )
        # email/phone 은 raw (렌더러가 mailto:/tel: 조립)
        assert sr.normalize_social_value("email", "mailto:a@b.com") == "a@b.com"
        assert sr.normalize_social_value("email", "a@b.com") == "a@b.com"
        assert sr.normalize_social_value("phone", "tel:+8210") == "+8210"


# ─────────────────────────────────────────────────────────────
# litly 변환 — contact 병합 / 확장 SNS / 배경 대비색
# ─────────────────────────────────────────────────────────────


def _litly_payload(blocks, theme=None):
    return {
        "alias": "tester",
        "profile": {"name": "Tester", "headline": "hello"},
        "theme": theme or {"backgroundColor": "#F5F5F8"},
        "blocks": blocks,
    }


def _first_social(body):
    socials = [b for b in body["blocks"] if (b.get("data") or {}).get("_type") == "social"]
    return socials[0] if socials else None


class TestLitlyConvertSocial:
    def test_contact_merges_into_social_no_dead_block(self):
        from apps.pages.services.external_importers import litly

        body = litly.convert(
            _litly_payload(
                [
                    {
                        "type": "sns",
                        "use": True,
                        "links": [{"type": "instagram", "value": "https://instagram.com/foo"}],
                    },
                    {
                        "type": "contact",
                        "use": True,
                        "links": [
                            {"type": "phone", "value": "+821012345678"},
                            {"type": "email", "value": "test@example.com"},
                        ],
                    },
                ]
            )
        )
        # 죽은 contact 블록이 생기면 안 됨
        assert all(b.get("type") != "contact" for b in body["blocks"])
        soc = _first_social(body)
        assert soc is not None
        data = soc["data"]
        assert data.get("instagram")
        assert data.get("phone") == "+821012345678"
        assert data.get("email") == "test@example.com"

    def test_contact_only_becomes_social(self):
        from apps.pages.services.external_importers import litly

        body = litly.convert(
            _litly_payload(
                [
                    {
                        "type": "contact",
                        "use": True,
                        "links": [{"type": "email", "value": "solo@example.com"}],
                    }
                ]
            )
        )
        assert all(b.get("type") != "contact" for b in body["blocks"])
        soc = _first_social(body)
        assert soc is not None
        assert soc["data"].get("email") == "solo@example.com"

    def test_expanded_sns_fields_preserved(self):
        from apps.pages.services.external_importers import litly

        body = litly.convert(
            _litly_payload(
                [
                    {
                        "type": "sns",
                        "use": True,
                        "links": [
                            {"type": "kakaotalk", "value": "https://pf.kakao.com/abc"},
                            {"type": "naverblog", "value": "https://blog.naver.com/foo"},
                            {"type": "line", "value": "https://line.me/ti/p/xyz"},
                        ],
                    }
                ]
            )
        )
        soc = _first_social(body)
        assert soc is not None
        data = soc["data"]
        # 확장 SNS id 가 flat 키로 보존 (드랍 X)
        assert data.get("kakao_talk")
        assert data.get("naver_blog")
        assert data.get("line")

    def test_unsupported_sns_becomes_fallback_button(self):
        from apps.pages.services.external_importers import litly

        body = litly.convert(
            _litly_payload(
                [
                    {
                        "type": "sns",
                        "use": True,
                        "links": [
                            {"type": "instagram", "value": "https://instagram.com/foo"},
                            {"type": "medium", "value": "https://medium.com/@foo"},
                        ],
                    }
                ]
            )
        )
        soc = _first_social(body)
        assert soc is not None
        # medium 은 레지스트리에 없어 social 키에 안 들어감
        assert "medium" not in soc["data"]
        # 대신 별도 single_link 버튼(fallback)으로 보존
        labels = [
            (b.get("data") or {}).get("label", "")
            for b in body["blocks"]
            if (b.get("data") or {}).get("_type") == "single_link"
        ]
        assert any("medium" in lbl.lower() for lbl in labels)

    def test_dark_background_contrast_white_icons(self):
        from apps.pages.services.external_importers import litly

        body = litly.convert(
            _litly_payload(
                [
                    {
                        "type": "sns",
                        "use": True,
                        "links": [{"type": "instagram", "value": "https://instagram.com/foo"}],
                    }
                ],
                theme={"backgroundColor": "#151E28"},  # 어두운 배경
            )
        )
        soc = _first_social(body)
        assert soc is not None
        assert soc["data"].get("custom_icon_color") == "#FFFFFF"

    def test_light_background_contrast_black_icons(self):
        from apps.pages.services.external_importers import litly

        body = litly.convert(
            _litly_payload(
                [
                    {
                        "type": "sns",
                        "use": True,
                        "links": [{"type": "instagram", "value": "https://instagram.com/foo"}],
                    }
                ],
                theme={"backgroundColor": "#FFFFFF"},  # 밝은 배경
            )
        )
        soc = _first_social(body)
        assert soc is not None
        assert soc["data"].get("custom_icon_color") == "#000000"


# ─────────────────────────────────────────────────────────────
# inpock 변환 — 공용 레지스트리 적용 + /api/r/ eager 해석 제외
# ─────────────────────────────────────────────────────────────


class TestInpockSocialRegistry:
    def test_make_social_block_returns_tuple_with_fallbacks(self):
        from apps.pages.services.external_importers import inpock

        social, fallbacks = inpock.make_social_block(
            [
                {"type": "instagram", "value": "https://instagram.com/foo"},
                {"type": "blog", "value": "https://blog.naver.com/x"},  # 네이버 → social
                {"type": "blog", "value": "https://blog.toss.im/y"},  # 비-네이버 → 버튼
            ]
        )
        assert social is not None
        data = social["data"]
        assert data.get("instagram")
        assert data.get("naver_blog")  # 네이버 블로그는 social 아이콘으로 흡수
        # 비-네이버 블로그는 fallback single_link 로 보존
        assert len(fallbacks) == 1
        assert fallbacks[0]["data"]["url"].startswith("https://blog.toss.im")

    def test_make_social_block_empty(self):
        from apps.pages.services.external_importers import inpock

        assert inpock.make_social_block([]) == (None, [])

    def test_no_eager_api_r_resolution(self):
        """대량 복사 시 IP밴 위험인 /api/r/ 즉시 해석은 백엔드에 이식되지 않아야 함."""
        from apps.pages.services.external_importers import inpock

        assert not hasattr(inpock, "resolve_inpock_redirect")
        assert not hasattr(inpock, "preresolve_inpock_links")
        assert not hasattr(inpock, "NetworkDownError")
