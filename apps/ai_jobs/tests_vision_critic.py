"""vision_critic 순수 로직 테스트 (실제 VLM 호출은 monkeypatch)."""

from __future__ import annotations

from . import services as _  # noqa: F401
from .services import vision_critic as V
from .services.llm_client import LlmCallResult


class TestSanitizePatch:
    def test_keeps_valid_keys(self):
        p = V._sanitize_patch(
            {
                "backgroundColor": "#FDFBF7",
                "buttonColor": "#A67C52",
                "buttonShape": "pill",
                "fontFamily": "Noto Sans KR",
                "page_custom_css": "body{background:#fff;}",
            }
        )
        assert p["backgroundColor"] == "#FDFBF7"
        assert p["buttonShape"] == "pill"
        assert p["fontFamily"] == "Noto Sans KR"
        assert "page_custom_css" in p

    def test_drops_invalid(self):
        p = V._sanitize_patch(
            {
                "backgroundColor": "not-a-color",
                "buttonShape": "circle",
                "fontFamily": "Comic Sans",
                "bogus": "x",
                "buttonColor": "#123",
            }
        )
        assert "backgroundColor" not in p
        assert "buttonShape" not in p  # circle 비허용
        assert "fontFamily" not in p  # Comic Sans 비허용
        assert "bogus" not in p
        assert p["buttonColor"] == "#123"  # 짧은 hex 도 유효

    def test_long_css_dropped(self):
        p = V._sanitize_patch({"page_custom_css": "a" * 3000})
        assert "page_custom_css" not in p

    def test_non_dict(self):
        assert V._sanitize_patch(None) == {}
        assert V._sanitize_patch("x") == {}


class TestApplyDesignPatch:
    def test_merges_into_design_settings_and_css(self):
        r = {
            "data": {"design_settings": {"backgroundColor": "#fff", "buttonColor": "#2563eb"}},
            "blocks": [],
        }
        out = V.apply_design_patch(r, {"buttonColor": "#A67C52", "page_custom_css": "body{}"})
        assert out["data"]["design_settings"]["buttonColor"] == "#A67C52"
        assert out["custom_css"] == "body{}"

    def test_reapplies_guard_replaces_slop(self):
        # 비평기가 슬롭 보라를 제안해도 design_guard 가 다시 교체
        r = {"data": {"design_settings": {"backgroundColor": "#ffffff"}}, "blocks": []}
        out = V.apply_design_patch(r, {"buttonColor": "#8c25f4"}, palette={"accent": "#A67C52"})
        assert out["data"]["design_settings"]["buttonColor"] == "#A67C52"

    def test_empty_patch_noop(self):
        r = {"data": {"design_settings": {"backgroundColor": "#fff"}}}
        assert V.apply_design_patch(r, {}) is r


class TestCritiqueParsing:
    def _fake(self, content):
        def _call(*args, **kwargs):
            return LlmCallResult(content=content, model="gemma-4", elapsed_seconds=0.1)

        return _call

    def test_parses_structured_critique(self, monkeypatch):
        content = (
            '{"reasoning":"배경이 중간톤이라 대비 약함","scores":{"content":4,"design":2,'
            '"image":3,"readability":4},"findings":[{"axis":"design","severity":"high",'
            '"problem":"저대비","fix":"배경 밝게"}],'
            '"design_patch":{"backgroundColor":"#FFFFFF","buttonColor":"#A67C52"},"stop":false}'
        )
        monkeypatch.setattr(V, "call_llm_messages_with_usage", self._fake(content))
        c = V.critique_screenshot(b"\x89PNG", concept="cafe")
        assert c.scores["design"] == 2
        assert c.total == 13
        assert c.high_severity_count == 1
        assert c.design_patch["backgroundColor"] == "#FFFFFF"
        assert c.stop is False

    def test_bad_json_returns_safe_stop(self, monkeypatch):
        monkeypatch.setattr(V, "call_llm_messages_with_usage", self._fake("not json at all"))
        c = V.critique_screenshot(b"x")
        assert c.stop is True
        assert c.design_patch == {}

    def test_call_failure_is_nonfatal(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("llm down")

        monkeypatch.setattr(V, "call_llm_messages_with_usage", _boom)
        c = V.critique_screenshot(b"x")
        assert c.stop is True


class TestRefineLoop:
    def test_stops_when_critic_says_stop(self, monkeypatch):
        calls = {"render": 0}

        def render():
            calls["render"] += 1
            return b"PNGDATA"

        applied = []

        def apply_fn(rj):
            applied.append(rj)

        def _stop_critique(*a, **k):
            return V.Critique(scores={x: 5 for x in V._AXES}, stop=True)

        monkeypatch.setattr(V, "critique_screenshot", _stop_critique)
        r = {"data": {"design_settings": {"backgroundColor": "#fff"}}}
        out, log = V.refine_result_json(r, render_png=render, apply_fn=apply_fn, max_cycles=2)
        assert out is r
        assert len(log) == 1 and log[0]["applied"] is False
        assert calls["render"] == 1  # stop 즉시 종료

    def test_skips_patch_when_no_high_severity(self, monkeypatch):
        # 패치 제안이 있어도 high severity 가 없으면 비싼 재렌더 없이 종료(원본 유지).
        calls = {"render": 0}

        def render():
            calls["render"] += 1
            return b"PNG"

        def _cosmetic(*a, **k):
            return V.Critique(
                scores={x: 4 for x in V._AXES},
                findings=[{"axis": "design", "severity": "med", "problem": "p", "fix": "f"}],
                design_patch={"buttonColor": "#A67C52"},
                stop=False,
            )

        monkeypatch.setattr(V, "critique_screenshot", _cosmetic)
        r = {"data": {"design_settings": {"backgroundColor": "#fff"}}}
        out, log = V.refine_result_json(
            r, render_png=render, apply_fn=lambda rj: None, max_cycles=2
        )
        assert out is r
        assert log[0]["applied"] is False
        assert calls["render"] == 1  # 패치 사이클(재렌더) 안 돎
