"""우리가 보내는 DM 포맷별 Meta 글자수 한도 + 버튼 템플릿(640) + 오프닝 다양화 테스트.

- 버튼(postback/web_url) 붙는 DM → button template text 640자.
- 버튼 없는 일반 텍스트 DM → UTF-8 1000 바이트(한글 ≈ 333자).
- 발송 페이로드는 generic(title 80) 이 아니라 button template 로 나간다.
- diversify_opening: 오프닝 1개 → 톤/의미 유지 N개 변형(파싱/클립/중복제거/개수 캡).

pure 함수/시리얼라이저 validate/서비스 페이로드/AI 정규화만 다뤄 DB 불필요.
"""

from types import SimpleNamespace

from apps.ai_jobs.services import dm_campaign_assistant as dca
from apps.ai_jobs.services.dm_campaign_assistant import _normalize_meta_fields, diversify_opening
from apps.integrations.models import BUTTON_TEMPLATE_TEXT_MAX, AutoDMCampaign
from apps.integrations.serializers import AutoDMCampaignCreateSerializer, _dm_body_length_errors
from apps.integrations.services import InstagramMessagingService

BUTTON_OVER = "가" * (BUTTON_TEMPLATE_TEXT_MAX + 10)  # 650자 (>640, 버튼 한도 초과)
BUTTON_OK = "가" * 100  # 100자 (≤640)
PLAIN_OVER = "가" * 400  # 1200 bytes (>1000, 일반 텍스트 바이트 초과) — 단 640자 미만
PLAIN_OK = "가" * 200  # 600 bytes (≤1000)
SHORT = "짧은 문구"


# ── 순수 판정 함수 (640 버튼 / 1000바이트 일반) ────────────────


class TestDmBodyLengthErrors:
    def test_gate_prompt_over_640_errors(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=True,
            follow_gate_prompt=BUTTON_OVER,
            follow_gate_retry_message=SHORT,
            reward_message_template=SHORT,
            opening_message=SHORT,
            link_button_url="",
        )
        assert "follow_gate_prompt" in errs

    def test_gate_all_ok(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=True,
            follow_gate_prompt=BUTTON_OK,
            follow_gate_retry_message=BUTTON_OK,
            reward_message_template=PLAIN_OK,
            opening_message=BUTTON_OVER,  # 게이트면 opening 미사용 → 무관
            link_button_url="",
        )
        assert errs == {}

    def test_gate_retry_over_640_errors(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=True,
            follow_gate_prompt=SHORT,
            follow_gate_retry_message=BUTTON_OVER,
            reward_message_template=SHORT,
            opening_message=SHORT,
            link_button_url="",
        )
        assert "follow_gate_retry_message" in errs

    def test_gate_link_reward_over_640_errors(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=True,
            follow_gate_prompt=SHORT,
            follow_gate_retry_message=SHORT,
            reward_message_template=BUTTON_OVER,
            opening_message=SHORT,
            link_button_url="https://x.co",
        )
        assert "reward_message_template" in errs

    def test_gate_nolink_reward_uses_byte_limit(self):
        # 링크 없으면 reward 는 일반 텍스트 → 바이트 기준. 400자(1200B) 는 640자 미만이지만 초과.
        errs = _dm_body_length_errors(
            follow_gate_enabled=True,
            follow_gate_prompt=SHORT,
            follow_gate_retry_message=SHORT,
            reward_message_template=PLAIN_OVER,
            opening_message=SHORT,
            link_button_url="",
        )
        assert "reward_message_template" in errs

    def test_gate_nolink_reward_ok(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=True,
            follow_gate_prompt=SHORT,
            follow_gate_retry_message=SHORT,
            reward_message_template=PLAIN_OK,
            opening_message=SHORT,
            link_button_url="",
        )
        assert "reward_message_template" not in errs

    def test_nogate_link_opening_over_640_errors(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=False,
            follow_gate_prompt="",
            follow_gate_retry_message="",
            reward_message_template="",
            opening_message=BUTTON_OVER,
            link_button_url="https://x.co",
        )
        assert "opening_message_template" in errs

    def test_nogate_nolink_opening_uses_byte_limit(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=False,
            follow_gate_prompt="",
            follow_gate_retry_message="",
            reward_message_template="",
            opening_message=PLAIN_OVER,  # 400자/1200B → 초과
            link_button_url="",
        )
        assert "opening_message_template" in errs

    def test_nogate_nolink_opening_ok(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=False,
            follow_gate_prompt="",
            follow_gate_retry_message="",
            reward_message_template="",
            opening_message=PLAIN_OK,
            link_button_url="",
        )
        assert errs == {}

    def test_boundary_exactly_640_ok(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=True,
            follow_gate_prompt="가" * BUTTON_TEMPLATE_TEXT_MAX,  # 정확히 640 → OK
            follow_gate_retry_message=SHORT,
            reward_message_template=SHORT,
            opening_message=SHORT,
            link_button_url="",
        )
        assert errs == {}


# ── Create 시리얼라이저 wiring ─────────────────────────────────


def _base_data(**kw):
    d = {
        "trigger_type": "any_media",
        "name": "글자수 한도 테스트",
        "opening_message_template": SHORT,
    }
    d.update(kw)
    return d


class TestCreateSerializerLimit:
    def test_base_valid(self):
        ser = AutoDMCampaignCreateSerializer(data=_base_data())
        assert ser.is_valid(), ser.errors

    def test_gate_prompt_over_640_rejected(self):
        ser = AutoDMCampaignCreateSerializer(
            data=_base_data(
                follow_gate_enabled=True,
                follow_gate_prompt=BUTTON_OVER,
                reward_message_template=SHORT,
            )
        )
        assert not ser.is_valid()
        assert "follow_gate_prompt" in ser.errors

    def test_nogate_link_opening_over_640_rejected(self):
        ser = AutoDMCampaignCreateSerializer(
            data=_base_data(
                opening_message_template=BUTTON_OVER, link_button_url="https://example.com"
            )
        )
        assert not ser.is_valid()
        assert "opening_message_template" in ser.errors

    def test_nogate_nolink_opening_byte_limit_rejected(self):
        # 버튼 없어도 일반 텍스트 1000바이트 초과면 거부(400자=1200B)
        ser = AutoDMCampaignCreateSerializer(data=_base_data(opening_message_template=PLAIN_OVER))
        assert not ser.is_valid()
        assert "opening_message_template" in ser.errors

    def test_nogate_nolink_opening_ok(self):
        ser = AutoDMCampaignCreateSerializer(data=_base_data(opening_message_template=PLAIN_OK))
        assert ser.is_valid(), ser.errors


# ── 발송 페이로드: button template (generic 아님) ──────────────


class TestBuildMessagePayload:
    _POSTBACK = [{"type": "postback", "title": "받기", "payload": "fg:1"}]

    def test_buttons_use_button_template(self):
        payload = InstagramMessagingService._build_message_payload(
            text="안녕하세요 혜택 안내드려요", buttons=self._POSTBACK
        )
        p = payload["attachment"]["payload"]
        assert p["template_type"] == "button"
        assert "text" in p
        assert "elements" not in p  # generic 아님

    def test_button_text_clipped_to_640(self):
        payload = InstagramMessagingService._build_message_payload(
            text=BUTTON_OVER, buttons=self._POSTBACK
        )
        assert len(payload["attachment"]["payload"]["text"]) == BUTTON_TEMPLATE_TEXT_MAX

    def test_no_buttons_plain_text(self):
        payload = InstagramMessagingService._build_message_payload(text="그냥 텍스트", buttons=None)
        assert payload == {"text": "그냥 텍스트"}

    def test_plain_text_clipped_to_1000_bytes(self):
        # 일반 텍스트 DM 은 발송 직전 UTF-8 1000바이트로 방어 클립(멀티바이트 미분할).
        payload = InstagramMessagingService._build_message_payload(
            text="가" * 400, buttons=None  # 1200 bytes
        )
        assert len(payload["text"].encode("utf-8")) <= 1000
        assert "�" not in payload["text"]  # 잘린 멀티바이트 깨짐 없음


# ── AI 초안 클립(640) ──────────────────────────────────────────


class TestAssistantClips640:
    def test_normalize_clips_to_640(self):
        raw = {
            "name": "n",
            "opening_dm": BUTTON_OVER,
            "gate_prompt": BUTTON_OVER,
            "reward_dm": BUTTON_OVER,
            "gate_button": "받기",
            "link_label": "자세히",
        }
        out = _normalize_meta_fields(raw, link_url="https://x.co", include_follow_gate=True)
        assert len(out["opening_message_template"]) <= BUTTON_TEMPLATE_TEXT_MAX
        fg = out["follow_gate"]
        assert len(fg["follow_gate_prompt"]) <= BUTTON_TEMPLATE_TEXT_MAX
        assert len(fg["reward_message_template"]) <= BUTTON_TEMPLATE_TEXT_MAX


# ── 오프닝 다양화 서비스 (LLM 모킹) ────────────────────────────


def _fake_llm(content: str):
    return SimpleNamespace(
        content=content,
        model="gemma-4",
        elapsed_seconds=1.0,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        estimated_cost_usd=0.0,
    )


class TestDiversifyOpening:
    def test_parses_dedups_and_caps(self, monkeypatch):
        monkeypatch.setattr(
            dca,
            "call_llm_messages_with_usage",
            lambda **kw: _fake_llm('{"variants": ["가", "나", "다", "가"]}'),
        )
        res = diversify_opening(opening_message="원문", count=10)
        assert res.variants == ["가", "나", "다"]  # 중복 "가" 제거

    def test_count_cap(self, monkeypatch):
        monkeypatch.setattr(
            dca,
            "call_llm_messages_with_usage",
            lambda **kw: _fake_llm('{"variants": ["a","b","c","d","e"]}'),
        )
        res = diversify_opening(opening_message="원문", count=2)
        assert len(res.variants) == 2

    def test_variant_clipped_to_640(self, monkeypatch):
        import json as _json

        monkeypatch.setattr(
            dca,
            "call_llm_messages_with_usage",
            lambda **kw: _fake_llm(_json.dumps({"variants": [BUTTON_OVER]})),
        )
        res = diversify_opening(opening_message="원문", count=5)
        assert all(len(v) <= BUTTON_TEMPLATE_TEXT_MAX for v in res.variants)

    def test_empty_on_parse_fail(self, monkeypatch):
        monkeypatch.setattr(
            dca, "call_llm_messages_with_usage", lambda **kw: _fake_llm("총 쓰레기 응답 no json")
        )
        res = diversify_opening(opening_message="원문", count=5)
        assert res.variants == []


# ── 오프닝 회전(여러 개 중 1개 무작위 발송) ────────────────────


class TestOpeningRotation:
    def test_nogate_rotates_from_list(self):
        c = AutoDMCampaign(
            follow_gate_enabled=False,
            opening_message_templates=["A", "B", "C"],
            opening_message_template="single",
        )
        picks = {c.get_opening_message() for _ in range(40)}
        assert picks <= {"A", "B", "C"}  # 목록에서만
        assert "single" not in picks  # 목록이 단일값보다 우선

    def test_nogate_empty_list_falls_back_to_single(self):
        c = AutoDMCampaign(
            follow_gate_enabled=False,
            opening_message_templates=[],
            opening_message_template="single",
        )
        assert c.get_opening_message() == "single"

    def test_gate_rotates_from_gate_list(self):
        c = AutoDMCampaign(
            follow_gate_enabled=True,
            follow_gate_prompt_templates=["G1", "G2"],
            follow_gate_prompt="gsingle",
        )
        picks = {c.get_opening_message() for _ in range(40)}
        assert picks <= {"G1", "G2"}
        assert "gsingle" not in picks

    def test_gate_empty_list_falls_back_to_prompt(self):
        c = AutoDMCampaign(
            follow_gate_enabled=True,
            follow_gate_prompt_templates=[],
            follow_gate_prompt="gsingle",
        )
        assert c.get_opening_message() == "gsingle"


# ── 회전 목록 항목 길이 검증 ───────────────────────────────────


class TestTemplateListLimits:
    def test_gate_prompt_templates_item_over_640(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=True,
            follow_gate_prompt=SHORT,
            follow_gate_retry_message=SHORT,
            reward_message_template=SHORT,
            opening_message=SHORT,
            link_button_url="",
            follow_gate_prompt_templates=[SHORT, BUTTON_OVER],
        )
        assert "follow_gate_prompt_templates" in errs

    def test_opening_templates_item_over_640_with_link(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=False,
            follow_gate_prompt="",
            follow_gate_retry_message="",
            reward_message_template="",
            opening_message=SHORT,
            link_button_url="https://x.co",
            opening_message_templates=[SHORT, BUTTON_OVER],
        )
        assert "opening_message_templates" in errs

    def test_opening_templates_item_byte_limit_without_link(self):
        errs = _dm_body_length_errors(
            follow_gate_enabled=False,
            follow_gate_prompt="",
            follow_gate_retry_message="",
            reward_message_template="",
            opening_message=SHORT,
            link_button_url="",
            opening_message_templates=[PLAIN_OVER],  # 1200B
        )
        assert "opening_message_templates" in errs

    def test_create_serializer_rejects_bad_opening_template_item(self):
        ser = AutoDMCampaignCreateSerializer(
            data=_base_data(
                opening_message_templates=[BUTTON_OVER], link_button_url="https://example.com"
            )
        )
        assert not ser.is_valid()
        assert "opening_message_templates" in ser.errors
