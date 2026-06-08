"""image_labeler.label_images 정규화 + 이미지 블록(URL/ base64) 테스트."""

from __future__ import annotations

import base64
import io
import json

from .services import image_labeler
from .services.image_labeler import (
    SYSTEM_PROMPT,
    _build_messages,
    _image_url_block,
    label_images,
)


class _FakeResult:
    def __init__(self, content: str):
        self.content = content
        self.model = "deepseek"
        self.elapsed_seconds = 0.1
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.total_tokens = 15
        self.estimated_cost_usd = 0.0


def _patch_llm(monkeypatch, content: str):
    monkeypatch.setattr(
        image_labeler,
        "call_llm_messages_with_usage",
        lambda **kwargs: _FakeResult(content),
    )


class TestUserBriefInPrompt:
    def test_concept_included_in_messages(self):
        msgs, _ = _build_messages(
            [{"id": "1", "url": "https://cdn/1.jpg"}],
            concept="첫 번째 사진은 로고니까 프로필로 써줘",
        )
        header_text = msgs[1]["content"][0]["text"]
        assert "USER_BRIEF" in header_text
        assert "첫 번째 사진은 로고니까 프로필로 써줘" in header_text

    def test_empty_concept_has_placeholder(self):
        msgs, _ = _build_messages([{"id": "1", "url": "https://cdn/1.jpg"}], concept="")
        header_text = msgs[1]["content"][0]["text"]
        assert "설명 없음" in header_text

    def test_system_prompt_instructs_to_use_brief(self):
        assert "USER_BRIEF" in SYSTEM_PROMPT
        assert "최우선" in SYSTEM_PROMPT


class TestImageUrlBlock:
    def test_public_http_url_passthrough(self):
        block = _image_url_block(
            {"id": "a", "url": "https://cdn/x.jpg", "storage_name": "k", "mime": "image/jpeg"}
        )
        assert block == {"type": "image_url", "image_url": {"url": "https://cdn/x.jpg"}}

    def test_local_url_falls_back_to_base64(self, monkeypatch):
        raw = b"\xff\xd8\xff\xe0fakejpeg"

        class _FakeStorage:
            def open(self, name, mode="rb"):
                return io.BytesIO(raw)

        monkeypatch.setattr(image_labeler, "default_storage", _FakeStorage())
        block = _image_url_block(
            {
                "id": "a",
                "url": "/media/x.jpg",
                "storage_name": "ai_source_images/x.jpg",
                "mime": "image/jpeg",
            }
        )
        expected_b64 = base64.b64encode(raw).decode("ascii")
        assert block["type"] == "image_url"
        assert block["image_url"]["url"] == f"data:image/jpeg;base64,{expected_b64}"

    def test_local_url_no_storage_name_returns_none(self):
        assert (
            _image_url_block({"id": "a", "url": "/media/x.jpg", "storage_name": "", "mime": ""})
            is None
        )


class TestLabelImages:
    def test_normalizes_and_filters(self, monkeypatch):
        content = json.dumps(
            {
                "images": [
                    {
                        "id": "1",
                        "role": "content",
                        "usable": True,
                        "summary": "딸기 케이크",
                        "suggested_use": "hero",
                        "quality": {"blurry": False},
                    },
                    {
                        "id": "2",
                        "role": "concept",
                        "usable": True,
                        "summary": "무드보드",
                    },  # concept면 usable 무효
                    {"id": "3", "role": "content", "usable": False, "summary": "흐릿"},
                ],
                "mood_notes": "파스텔 톤",
            }
        )
        _patch_llm(monkeypatch, content)
        images = [
            {"id": str(i), "url": f"https://cdn/{i}.jpg", "storage_name": "", "mime": ""}
            for i in (1, 2, 3)
        ]

        result = label_images(images=images, concept="카페")

        assert result.mood_notes == "파스텔 톤"
        # 1: usable content
        assert result.labels["1"].usable is True
        assert result.labels["1"].role == "content"
        assert result.labels["1"].summary == "딸기 케이크"
        # 2: concept → usable 강제 False
        assert result.labels["2"].usable is False
        assert result.labels["2"].role == "concept"
        # 3: content but usable False
        assert result.labels["3"].usable is False

    def test_missing_image_defaults_to_concept(self, monkeypatch):
        # LLM 이 id "2" 를 응답에서 누락 → 기본 concept/usable=False
        _patch_llm(
            monkeypatch,
            json.dumps(
                {
                    "images": [
                        {
                            "id": "1",
                            "role": "content",
                            "usable": True,
                            "summary": "x",
                            "suggested_use": "hero",
                        }
                    ],
                    "mood_notes": "",
                }
            ),
        )
        images = [{"id": "1", "url": "https://cdn/1.jpg"}, {"id": "2", "url": "https://cdn/2.jpg"}]
        result = label_images(images=images, concept="")
        assert result.labels["2"].usable is False
        assert result.labels["2"].role == "concept"

    def test_malformed_json_degrades_to_all_concept(self, monkeypatch):
        _patch_llm(monkeypatch, "이건 JSON 이 아닙니다")
        images = [{"id": "1", "url": "https://cdn/1.jpg"}]
        result = label_images(images=images, concept="")
        assert result.labels["1"].usable is False
        assert result.labels["1"].role == "concept"

    def test_empty_images_returns_empty(self):
        result = label_images(images=[], concept="x")
        assert result.labels == {}

    def test_unknown_id_in_response_is_dropped(self, monkeypatch):
        _patch_llm(
            monkeypatch,
            json.dumps(
                {
                    "images": [{"id": "999", "role": "content", "usable": True, "summary": "x"}],
                    "mood_notes": "",
                }
            ),
        )
        images = [{"id": "1", "url": "https://cdn/1.jpg"}]
        result = label_images(images=images, concept="")
        assert "999" not in result.labels
        assert result.labels["1"].usable is False  # 입력 id 는 기본값으로 채워짐
