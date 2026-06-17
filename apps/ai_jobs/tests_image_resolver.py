"""image_resolver 의 {{user_image:N}} 치환 + 기존 {{image:}} 경로 보존 테스트."""

from __future__ import annotations

from .services.image_resolver import resolve_images


class TestUserImageResolve:
    def test_substitutes_user_image_placeholder(self):
        data = {
            "blocks": [
                {"data": {"image_url": "{{user_image:1}}"}},
                {"data": {"image_url": "{{user_image:2}}"}},
            ]
        }
        out = resolve_images(
            data,
            user_image_urls={"1": "https://cdn/a.jpg", "2": "https://cdn/b.jpg"},
        )
        assert out["blocks"][0]["data"]["image_url"] == "https://cdn/a.jpg"
        assert out["blocks"][1]["data"]["image_url"] == "https://cdn/b.jpg"

    def test_unknown_index_becomes_empty(self):
        data = {"blocks": [{"data": {"image_url": "{{user_image:9}}"}}]}
        out = resolve_images(data, user_image_urls={"1": "https://cdn/a.jpg"})
        assert out["blocks"][0]["data"]["image_url"] == ""

    def test_no_user_map_leaves_no_user_placeholder(self):
        # user_image_urls 미전달 → 매핑 없음 → 빈 문자열로 제거 (플레이스홀더 잔존 X)
        data = {"blocks": [{"data": {"image_url": "{{user_image:1}}"}}]}
        out = resolve_images(data)
        assert out["blocks"][0]["data"]["image_url"] == ""

    def test_no_placeholders_returns_data_untouched(self):
        data = {"blocks": [{"data": {"image_url": "https://already/real.jpg"}}]}
        out = resolve_images(data, user_image_urls={"1": "https://cdn/a.jpg"})
        assert out == data

    def test_pixabay_keyword_left_for_existing_path(self, monkeypatch):
        # {{image:키워드}} 는 병렬 경로(_resolve_one_detailed)로 위임 — 키워드 검출만 확인.
        import apps.ai_jobs.services.image_resolver as ir

        monkeypatch.setattr(
            ir, "_resolve_one_detailed", lambda kw, *a, **k: (f"RESOLVED::{kw}", None)
        )
        data = {
            "blocks": [{"data": {"image_url": "{{user_image:1}}", "bg": "{{image:cafe interior}}"}}]
        }
        out = resolve_images(data, user_image_urls={"1": "https://cdn/a.jpg"})
        assert out["blocks"][0]["data"]["image_url"] == "https://cdn/a.jpg"
        assert out["blocks"][0]["data"]["bg"] == "RESOLVED::cafe interior"


class TestPixabayDedup:
    def test_different_keywords_get_distinct_images(self, monkeypatch):
        import apps.ai_jobs.services.image_resolver as ir

        # 비전 게이트는 폴백(-1)으로 두어 기존 순서 로직(첫 미사용 후보)을 검증.
        monkeypatch.setattr(ir, "_vlm_pick_index", lambda kw, urls: -1)
        # 두 키워드가 같은 인기 후보(id=1)로 수렴하지만, 두 번째 키워드는 그걸 건너뛰고 id=2 채택.
        monkeypatch.setattr(
            ir,
            "_search_pixabay_candidates",
            lambda kw, n=20: [(1, "https://px/1.jpg"), (2, "https://px/2.jpg")],
        )
        monkeypatch.setattr(ir, "_download", lambda url: url.encode())  # raw = url bytes
        monkeypatch.setattr(
            ir,
            "_store_hosted",
            lambda raw, source_url: (f"https://r2/{source_url[-5:]}", source_url),  # digest=url
        )
        data = {
            "blocks": [
                {"data": {"a": "{{image:cute cat}}"}},
                {"data": {"b": "{{image:tiny kitten}}"}},
            ]
        }
        out = resolve_images(data)
        a = out["blocks"][0]["data"]["a"]
        b = out["blocks"][1]["data"]["b"]
        assert a != b  # 같은 이미지가 두 블록에 중복되지 않음
        assert a == "https://r2/1.jpg" and b == "https://r2/2.jpg"

    def test_no_api_key_uses_placeholder(self, monkeypatch):
        import apps.ai_jobs.services.image_resolver as ir

        monkeypatch.setattr(ir, "_search_pixabay_candidates", lambda kw, n=20: [])
        data = {"blocks": [{"data": {"a": "{{image:sunset beach}}"}}]}
        out = resolve_images(data)
        assert out["blocks"][0]["data"]["a"].startswith("https://placehold.co/")


class TestVlmGate:
    """게이트는 기본 OFF(AI_IMAGE_VLM_RERANK=False) — 테스트는 명시적으로 켜고 검증한다."""

    def _patch_common(self, ir, monkeypatch):
        # 게이트는 이제 기본 OFF — 게이트 로직 자체를 검증하는 테스트라 명시적으로 켠다.
        monkeypatch.setattr(ir.settings, "AI_IMAGE_VLM_RERANK", True, raising=False)
        monkeypatch.setattr(
            ir,
            "_search_pixabay_candidates",
            lambda kw, n=20: [
                (1, "https://px/1.jpg"),
                (2, "https://px/2.jpg"),
                (3, "https://px/3.jpg"),
            ],
        )
        monkeypatch.setattr(ir, "_download", lambda url: url.encode())
        monkeypatch.setattr(
            ir,
            "_store_hosted",
            lambda raw, source_url: (f"https://r2/{source_url[-5:]}", source_url),
        )

    def test_vlm_picks_chosen_candidate(self, monkeypatch):
        import apps.ai_jobs.services.image_resolver as ir

        self._patch_common(ir, monkeypatch)
        monkeypatch.setattr(ir, "_vlm_pick_index", lambda kw, urls: 2)  # 2번 후보 선택
        data = {"blocks": [{"data": {"a": "{{image:cute cat}}"}}]}
        out = resolve_images(data)
        assert out["blocks"][0]["data"]["a"] == "https://r2/2.jpg"

    def test_vlm_reject_all_returns_empty(self, monkeypatch):
        import apps.ai_jobs.services.image_resolver as ir

        self._patch_common(ir, monkeypatch)
        # 단일 호출 게이트가 "전부 무관(0)" — 최근접 선택까지 포함된 판단이므로 빈 슬롯.
        monkeypatch.setattr(ir, "_vlm_pick_index", lambda kw, urls: 0)
        data = {"blocks": [{"data": {"a": "{{image:obscure thing}}"}}]}
        out = resolve_images(data)
        assert out["blocks"][0]["data"]["a"] == ""

    def test_hosting_failure_falls_back_to_external_url(self, monkeypatch):
        import apps.ai_jobs.services.image_resolver as ir

        self._patch_common(ir, monkeypatch)
        monkeypatch.setattr(ir, "_vlm_pick_index", lambda kw, urls: 1)
        # 다운로드가 전부 실패해도 빈 슬롯 대신 선택 후보의 외부 URL 폴백.
        monkeypatch.setattr(ir, "_host_candidate", lambda pid, purl, kw, used: None)
        data = {"blocks": [{"data": {"a": "{{image:cafe}}"}}]}
        out = resolve_images(data)
        assert out["blocks"][0]["data"]["a"] == "https://px/1.jpg"

    def test_vlm_fallback_keeps_order(self, monkeypatch):
        import apps.ai_jobs.services.image_resolver as ir

        self._patch_common(ir, monkeypatch)
        monkeypatch.setattr(ir, "_vlm_pick_index", lambda kw, urls: -1)  # 폴백 → 첫 후보
        data = {"blocks": [{"data": {"a": "{{image:cafe}}"}}]}
        out = resolve_images(data)
        assert out["blocks"][0]["data"]["a"] == "https://r2/1.jpg"


class TestParallelResolve:
    def test_worker_exception_falls_back_to_placeholder(self, monkeypatch):
        # 한 키워드 resolve 가 터져도 그 슬롯만 placeholder 로, 나머지는 정상 해석(병렬·비치명).
        import apps.ai_jobs.services.image_resolver as ir

        def fake(kw, *a, **k):
            if "boom" in kw:
                raise RuntimeError("explode")
            return (f"https://r2/{kw}.jpg", kw)

        monkeypatch.setattr(ir, "_resolve_one_detailed", fake)
        data = {"blocks": [{"data": {"a": "{{image:boom kw}}", "b": "{{image:good kw}}"}}]}
        out = resolve_images(data)
        assert out["blocks"][0]["data"]["a"].startswith("https://placehold.co/")
        assert out["blocks"][0]["data"]["b"] == "https://r2/good kw.jpg"

    def test_duplicate_digest_reresolved(self, monkeypatch):
        # 두 키워드가 같은 digest 로 수렴하면 두 번째는 seen 을 피해 재선택(중복 이미지 방지).
        import apps.ai_jobs.services.image_resolver as ir

        calls = {"n": 0}

        def fake(kw, forbidden_digests=frozenset()):
            # 1차: 둘 다 dig1. 재선택(forbidden 에 dig1 있음): dig2.
            if "dig1" in forbidden_digests:
                return ("https://r2/2.jpg", "dig2")
            return ("https://r2/1.jpg", "dig1")

        monkeypatch.setattr(ir, "_resolve_one_detailed", fake)
        data = {"blocks": [{"data": {"a": "{{image:k1}}"}}, {"data": {"b": "{{image:k2}}"}}]}
        out = resolve_images(data)
        a = out["blocks"][0]["data"]["a"]
        b = out["blocks"][1]["data"]["b"]
        assert a != b
        assert {a, b} == {"https://r2/1.jpg", "https://r2/2.jpg"}
