"""AI 폼-작성 도움 API (게시물 → AutoDM 캠페인 초안) 테스트.

커버리지:
  - 서비스 정규화(suggest_campaign_fields): N개 고유 보장 / 버튼 20자 캡 / follow_gate None /
    {{link}} 치환·제거 / JSON 파싱 실패 시 전 필드 폴백(raise 안 함)
  - API(ai-suggest, 비동기): 202+job_id+작업생성·디스패치 / 400(컨텍스트 없음·mock) /
    401(미인증) / 403(비멤버) / 404(워크스페이스 없음)
  - Celery 태스크(run_dm_campaign_assist_job): result_json 채움 / follow_gate 제외 /
    이미지 다운로드 / 없는 job 안전

LLM(call_llm_messages_with_usage)·task.delay·이미지 다운로드는 모두 patch 한다 (실제 호출 없음).
"""

from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai_jobs.services.dm_campaign_assistant import (
    REPLY_POOL_SIZE,
    DmAssistResult,
    sample_replies,
    suggest_campaign_fields,
)
from apps.integrations.models import IGAccountConnection
from apps.workspace.models import Membership, Workspace

_SVC = "apps.ai_jobs.services.dm_campaign_assistant.call_llm_messages_with_usage"


class _FakeLlm:
    """call_llm_messages_with_usage 반환 객체 흉내 (LlmCallResult 호환 속성)."""

    def __init__(self, content):
        self.content = content
        self.model = "gemma-4"
        self.elapsed_seconds = 0.1
        self.prompt_tokens = 10
        self.completion_tokens = 20
        self.total_tokens = 30
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 10
        self.estimated_cost_usd = 0.0


# ── 픽스처 ─────────────────────────────────────────────────────


@pytest.fixture
def workspace_and_user(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email="aisuggest@example.com", password="pw12345!", full_name="AI Suggest Tester"
    )
    ws = Workspace.objects.create(name="AI Suggest WS", slug="ai-suggest-ws", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    return ws, user


@pytest.fixture
def ig_connection(workspace_and_user):
    ws, _ = workspace_and_user
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id="ig_aisuggest_001",
        username="aisuggestuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    conn.access_token = "mock_token_aisuggest"
    conn.save()
    return conn


def _full_json(_n_replies=50):
    """gemma 가 돌려주는 맥락 항목 JSON. 공개 답글은 코드 풀, 링크는 본문에 안 넣고 link_label 만."""
    return (
        '{"name": "신상 원피스 자동응대", "kw": ["사이즈", "재입고", "문의"], "kw_mode": "any",'
        ' "opening_dm": "안녕하세요! 문의 감사해요 🥰 자료 보내드릴게요",'
        ' "gate_prompt": "댓글 감사합니다! 버튼을 눌러주세요",'
        ' "gate_button": "자료 받기", "gate_button_alt": "팔로우했어요",'
        ' "reward_dm": "감사합니다! 약속드린 자료 보내드려요",'
        ' "gate_retry": "팔로우 확인이 안 됐어요. 다시 눌러주세요!",'
        ' "link_label": "받으러 가기"}'
    )


# ── 서비스 정규화 단위 테스트 ──────────────────────────────────


class TestSuggestNormalization:
    def test_returns_exactly_n_unique_replies(self):
        with patch(_SVC, return_value=_FakeLlm(_full_json(50))):
            r = suggest_campaign_fields(caption="테스트 캡션", reply_variant_count=50)
        assert len(r.public_reply_templates) == 50
        assert len(set(r.public_reply_templates)) == 50
        assert all(t.strip() for t in r.public_reply_templates)

    def test_replies_come_from_pool_not_llm(self):
        """공개 답글은 LLM 응답과 무관 — gemma 가 답글을 안 줘도 코드 풀에서 정확히 N개 고유."""
        with patch(_SVC, return_value=_FakeLlm(_full_json())):
            r = suggest_campaign_fields(caption="c", reply_variant_count=30)
        assert len(r.public_reply_templates) == 30
        assert len(set(r.public_reply_templates)) == 30
        assert all(t.strip() for t in r.public_reply_templates)

    def test_button_label_capped_to_20(self):
        long_btn = "가" * 50
        js = (
            '{"name": "n", "kw": [], "kw_mode": "any", "opening_dm": "o",'
            f' "gate_prompt": "p", "gate_button": "{long_btn}", "gate_button_alt": "{long_btn}",'
            ' "reward_dm": "r", "gate_retry": "rt"}'
        )
        with patch(_SVC, return_value=_FakeLlm(js)):
            r = suggest_campaign_fields(caption="c", reply_variant_count=1)
        assert len(r.follow_gate["follow_gate_button_label"]) <= 20
        assert len(r.follow_gate["follow_gate_button_label_alt"]) <= 20

    def test_follow_gate_none_when_excluded(self):
        with patch(_SVC, return_value=_FakeLlm(_full_json(5))):
            r = suggest_campaign_fields(
                caption="c", reply_variant_count=5, include_follow_gate=False
            )
        assert r.follow_gate is None

    def test_link_becomes_button_not_text(self):
        """링크는 본문에 안 들어가고 link_button(라벨+URL)으로 제안된다."""
        with patch(_SVC, return_value=_FakeLlm(_full_json(3))):
            r = suggest_campaign_fields(
                caption="c", reply_variant_count=3, link_url="https://shop.test/x"
            )
        assert "https://shop.test/x" not in r.opening_message_template
        assert "https://shop.test/x" not in r.follow_gate["reward_message_template"]
        assert r.link_button == {
            "link_button_label": "받으러 가기",
            "link_button_url": "https://shop.test/x",
        }

    def test_link_button_uses_example_url_when_no_link(self):
        """link_url 미입력이어도 link_button 은 항상 채워진다(라벨 + 예시 URL)."""
        with patch(_SVC, return_value=_FakeLlm(_full_json(3))):
            r = suggest_campaign_fields(caption="c", reply_variant_count=3, link_url="")
        assert r.link_button == {
            "link_button_label": "받으러 가기",
            "link_button_url": "https://example.com",
        }
        assert "{{link}}" not in r.opening_message_template

    def test_link_button_label_default(self):
        """link_label 비었는데 link_url 있으면 라벨 기본값 '자세히 보기'."""
        js = (
            '{"name": "n", "kw": [], "kw_mode": "any", "opening_dm": "o",'
            ' "gate_prompt": "p", "gate_button": "b", "gate_button_alt": "a",'
            ' "reward_dm": "r", "gate_retry": "rt", "link_label": ""}'
        )
        with patch(_SVC, return_value=_FakeLlm(js)):
            r = suggest_campaign_fields(
                caption="c", reply_variant_count=1, link_url="https://x.io/a"
            )
        assert r.link_button == {
            "link_button_label": "자세히 보기",
            "link_button_url": "https://x.io/a",
        }

    def test_keyword_mode_defaults_on_invalid(self):
        js = '{"kw": ["a"], "kw_mode": "weird", "replies": ["x"]}'
        with patch(_SVC, return_value=_FakeLlm(js)):
            r = suggest_campaign_fields(
                caption="c", reply_variant_count=1, include_follow_gate=False
            )
        assert r.keyword_mode == "any"

    def test_parse_failure_falls_back_without_raising(self):
        with patch(_SVC, return_value=_FakeLlm("이건 JSON 이 아니에요 그냥 텍스트")):
            r = suggest_campaign_fields(caption="c", reply_variant_count=10)
        # 파싱 실패해도 raise 없이 폴백: 이름/본문 채워지고 답글은 풀에서 정확히 10개
        assert r.name
        assert r.opening_message_template
        assert len(r.public_reply_templates) == 10
        assert r.follow_gate is not None

    def test_reply_count_clamped_to_50(self):
        with patch(_SVC, return_value=_FakeLlm(_full_json(50))):
            r = suggest_campaign_fields(caption="c", reply_variant_count=999)
        assert len(r.public_reply_templates) == 50


# ── 공개 답글 풀 (sample_replies, LLM 미사용) 테스트 ───────────


class TestSampleReplies:
    def test_pool_size_over_1000(self):
        assert REPLY_POOL_SIZE >= 1000

    def test_returns_n_unique_nonempty(self):
        out = sample_replies(50)
        assert len(out) == 50
        assert len(set(out)) == 50
        assert all(t.strip() for t in out)

    def test_clamped_to_50(self):
        assert len(sample_replies(999)) == 50

    def test_deterministic_with_seed(self):
        assert sample_replies(20, seed=42) == sample_replies(20, seed=42)

    def test_varies_without_seed(self):
        # 시드 없으면 호출마다 다른 조합 (50개가 우연히 동일 순서일 확률 무시 가능)
        assert sample_replies(50) != sample_replies(50)

    def test_distinct_base_text_for_spam_avoidance(self):
        # 50개의 '기본 문구'가 모두 달라야 스팸 탐지에 안전 (끝맺음만 변주된 게 아님).
        from apps.ai_jobs.services.dm_campaign_assistant import _REPLY_BASES

        bases_by_len = sorted(_REPLY_BASES, key=len, reverse=True)
        matched = []
        for t in sample_replies(50):
            base = next((b for b in bases_by_len if t.startswith(b)), None)
            assert base is not None, t
            matched.append(base)
        assert len(set(matched)) == 50


# ── API 엔드포인트 테스트 ──────────────────────────────────────


def _url(workspace_id):
    return f"/api/v1/integrations/auto-dm-campaigns/ai-suggest/?workspace_id={workspace_id}"


_DELAY = "apps.ai_jobs.tasks.run_dm_campaign_assist_job.delay"


class TestAiSuggestEndpoint:
    """POST 는 작업을 큐에 넣고 202 만 반환한다 (LLM 호출은 Celery 태스크에서)."""

    def test_202_creates_job_and_dispatches(self, workspace_and_user):
        from apps.ai_jobs.models import AiJob

        ws, user = workspace_and_user
        client = APIClient()
        client.force_authenticate(user=user)
        with patch(_DELAY) as mock_delay:
            resp = client.post(
                _url(ws.id),
                {
                    "media_id": "media_123",
                    "caption": "신상 원피스 입고!",
                    "image_url": "https://example.com/p.jpg",
                    "media_type": "IMAGE",
                    "link_url": "https://shop.test/x",
                    "reply_variant_count": 20,
                },
                format="json",
            )
        assert resp.status_code == 202, resp.content
        body = resp.json()
        assert body["job_id"]
        assert body["status"] == "queued"
        assert body["poll_url"] == f"/api/v1/ai/jobs/{body['job_id']}/"

        job = AiJob.objects.get(id=body["job_id"])
        assert job.user_id == user.id
        assert job.job_type == AiJob.JobType.DM_CAMPAIGN_ASSIST
        assert job.input_payload["caption"] == "신상 원피스 입고!"
        assert job.input_payload["reply_variant_count"] == 20
        mock_delay.assert_called_once_with(str(job.id))

    def test_does_not_fetch_graph_when_caption_supplied(self, workspace_and_user):
        """caption 제공 시 Graph 조회를 건너뛴다 (mock dev 동작 보장)."""
        ws, user = workspace_and_user
        client = APIClient()
        client.force_authenticate(user=user)
        with patch(_DELAY), patch("apps.integrations.views.requests.get") as mock_get:
            resp = client.post(
                _url(ws.id), {"caption": "캡션만", "reply_variant_count": 5}, format="json"
            )
        assert resp.status_code == 202, resp.content
        mock_get.assert_not_called()

    def test_400_when_no_context(self, workspace_and_user):
        ws, user = workspace_and_user
        client = APIClient()
        client.force_authenticate(user=user)
        with patch(_DELAY):
            resp = client.post(_url(ws.id), {"business_type": "쇼핑몰"}, format="json")
        assert resp.status_code == 400

    def test_401_when_unauthenticated(self, workspace_and_user):
        ws, _ = workspace_and_user
        client = APIClient()
        resp = client.post(_url(ws.id), {"caption": "c"}, format="json")
        assert resp.status_code == 401

    def test_403_when_not_member(self, workspace_and_user, db):
        ws, _ = workspace_and_user
        from django.contrib.auth import get_user_model

        User = get_user_model()
        other = User.objects.create_user(
            email="outsider@example.com", password="pw12345!", full_name="Outsider"
        )
        client = APIClient()
        client.force_authenticate(user=other)
        with patch(_DELAY) as mock_delay:
            resp = client.post(_url(ws.id), {"caption": "c"}, format="json")
        assert resp.status_code == 403
        mock_delay.assert_not_called()

    def test_404_when_workspace_missing(self, workspace_and_user):
        import uuid as _uuid

        _, user = workspace_and_user
        client = APIClient()
        client.force_authenticate(user=user)
        with patch(_DELAY):
            resp = client.post(_url(_uuid.uuid4()), {"caption": "c"}, format="json")
        assert resp.status_code == 404

    def test_400_when_mock_mode_and_only_media_id(self, workspace_and_user, settings):
        """caption/image 없이 media_id 만 → mock 모드면 400 (Graph 조회 불가)."""
        settings.DEBUG = True
        settings.INSTAGRAM_MOCK_MODE = True
        ws, user = workspace_and_user
        client = APIClient()
        client.force_authenticate(user=user)
        with patch(_DELAY):
            resp = client.post(_url(ws.id), {"media_id": "media_xyz"}, format="json")
        assert resp.status_code == 400


# ── Celery 태스크 테스트 (실제 생성은 여기서, LLM 은 patch) ──────


class TestDmAssistTask:
    def _make_job(self, user, **payload):
        from apps.ai_jobs.models import AiJob

        return AiJob.objects.create(
            user=user,
            job_type=AiJob.JobType.DM_CAMPAIGN_ASSIST,
            input_payload=payload,
        )

    def test_task_populates_result_json(self, workspace_and_user):
        from apps.ai_jobs.models import AiJob
        from apps.ai_jobs.tasks import run_dm_campaign_assist_job

        _, user = workspace_and_user
        job = self._make_job(
            user,
            caption="신상 원피스",
            image_url="",
            reply_variant_count=20,
            include_follow_gate=True,
            link_url="https://shop.test/x",
        )
        with patch(_SVC, return_value=_FakeLlm(_full_json(50))):
            run_dm_campaign_assist_job.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == AiJob.Status.SUCCEEDED
        assert job.stage == AiJob.Stage.COMPLETED
        assert job.progress == 100
        sug = job.result_json["suggestion"]
        assert sug["name"]
        assert len(sug["public_reply_templates"]) == 20
        assert sug["follow_gate"]["follow_gate_button_label"]
        # 링크는 본문이 아니라 link_button 으로
        assert "https://shop.test/x" not in sug["simple"]["opening_message_template"]
        assert sug["link_button"]["link_button_url"] == "https://shop.test/x"

    def test_task_follow_gate_excluded(self, workspace_and_user):
        from apps.ai_jobs.models import AiJob
        from apps.ai_jobs.tasks import run_dm_campaign_assist_job

        _, user = workspace_and_user
        job = self._make_job(user, caption="c", include_follow_gate=False, reply_variant_count=3)
        with patch(_SVC, return_value=_FakeLlm(_full_json(3))):
            run_dm_campaign_assist_job.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == AiJob.Status.SUCCEEDED
        assert job.result_json["suggestion"]["follow_gate"] is None

    def test_task_downloads_image_when_url_present(self, workspace_and_user):
        from apps.ai_jobs.models import AiJob
        from apps.ai_jobs.tasks import run_dm_campaign_assist_job

        _, user = workspace_and_user
        job = self._make_job(
            user, caption="c", image_url="https://example.com/p.jpg", reply_variant_count=3
        )
        with (
            patch(_SVC, return_value=_FakeLlm(_full_json(3))),
            patch("apps.ai_jobs.services.image_resolver._download", return_value=b"img") as mock_dl,
        ):
            run_dm_campaign_assist_job.apply(args=[str(job.id)])
        job.refresh_from_db()
        assert job.status == AiJob.Status.SUCCEEDED
        mock_dl.assert_called_once()

    def test_task_missing_job_is_safe(self, db):
        import uuid as _uuid

        from apps.ai_jobs.tasks import run_dm_campaign_assist_job

        # 존재하지 않는 job_id → 예외 없이 조용히 종료
        run_dm_campaign_assist_job.apply(args=[str(_uuid.uuid4())])


def test_dataclass_defaults():
    """DmAssistResult 기본값 sanity."""
    r = DmAssistResult()
    assert r.keyword_mode == "any"
    assert r.public_reply_enabled is True
    assert r.follow_gate is None
    assert r.link_button is None
