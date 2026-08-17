"""DM 캠페인 이전 — 이어달리기(슬라이스) 계약 테스트.

게시물 1건 복원이 6~10초라 456개짜리 계정은 74분+ 이 필요하다. Celery 태스크 한도는
25분이므로 **한 번에 끝낼 수 없다**. 예전 코드는 루프를 끝까지 돌아야만 결과를 저장해서,
타임리밋에 잘리면 수집분이 통째로 사라지고 재개가 1번 게시물부터 다시 시작했다
→ 전수 복원이 구조적으로 불가능했다(실측 @highestlevel33 456개: 25분에 잘려 0건 저장).

여기서 지키는 계약:
    1. 시간이 다 되면 스스로 접고 **저장**한다 (실패가 아니다)
    2. 재개는 **남은 게시물부터** 한다 (같은 게시물을 다시 조회하지 않는다)
    3. 여러 슬라이스에 걸친 결과가 **합쳐진다**
    4. 결국 **완주**한다 — 못 끝내면 모은 것까지는 후보로 내보낸다
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.conf import settings
from django.utils import timezone

from apps.integrations.dm_migration import collect as C
from apps.integrations.dm_migration import pipeline
from apps.integrations.models import DMMigrationJob
from apps.integrations.test_dm_migration import _conn, _job, _user, _ws


class _FakeClock:
    """호출할 때마다 step 초씩 흐르는 단조 시계 — 실제로 기다리지 않고 슬라이스를 만든다."""

    def __init__(self, step: float):
        self.t = 0.0
        self.step = step

    def monotonic(self) -> float:
        self.t += self.step
        return self.t


def _slice_media(n: int) -> list[dict]:
    # 파이프라인은 **최신 게시물부터** 훑는다 → sm-0 이 가장 최신이 되도록 날짜를 내림차순으로.
    return [
        {
            "id": f"sm-{i}",
            "caption": "댓글에 자료 남겨주세요",
            "timestamp": f"2026-07-{20 - i:02d}T00:00:00+0000",
            "permalink": f"https://x/{i}",
            "comments_count": 20,  # MIN_COMMENTS 이상이어야 분석 대상이 된다
        }
        for i in range(n)
    ]


def _fake_recover_post(calls: list):
    """게시물 1건 복원을 대체 — 어떤 게시물을 몇 번 조회했는지 기록한다."""
    from apps.integrations.dm_migration import recover as R

    def _fn(ctx, media, **kw):
        calls.append(media["id"])
        r = R.PostRecovery(
            media_id=media["id"],
            probed=10,
            trigger="자료",
            repetition=0.5,
            is_campaign_signal=True,
        )
        r.offer = {
            "text": "요청하신 자료 보내드려요",
            "url": f"https://ex.co/{media['id']}",
            "label": "자료 받기",
            "hits": 8,
            "ratio": 0.8,
            "score": 0.72,
        }
        return r

    return _fn


def _drive(job, redispatch, *, max_runs: int = 20) -> tuple[str, int]:
    """이어달리기를 동기적으로 끝까지 돌린다 → (최종 status, 실행 횟수).

    슬라이스 사이에 ``updated_at`` 을 강제로 늙힌다 — run_migration 의 중복 실행 가드가
    "60초 내 갱신이면 다른 워커가 잡고 있다" 로 보고 양보하기 때문. 운영에서는
    RESUME_COUNTDOWN(>60초) 이 이 역할을 한다.
    """
    for run in range(1, max_runs + 1):
        status = pipeline.run_migration(str(job.id), redispatch=redispatch)
        if status != "continued":
            return status, run
        DMMigrationJob.objects.filter(id=job.id).update(
            updated_at=timezone.now() - timedelta(minutes=5)
        )
    raise AssertionError(f"이어달리기가 {max_runs}회 안에 끝나지 않았다")


def _no_network(monkeypatch):
    for name in ("fetch_media", "collect_commenters", "fetch_outbound_for_commenter"):

        def _boom(*a, __n=name, **k):
            raise AssertionError(f"{__n} 이 호출됐다 — 오프라인 테스트가 깨졌다")

        monkeypatch.setattr(C, name, _boom)
    # 발신함 전수 조사는 '가속기' 라 없어도 파이프라인이 돌아야 한다. 빈 결과로 두고
    # 기존(게시물별 조회) 경로가 그대로 동작하는지를 본다.
    monkeypatch.setattr(
        C,
        "fetch_conversations",
        lambda *a, **k: {
            "outbound": [],
            "scope_missing": False,
            "conversations_scanned": 0,
            "paging_after": None,
            "exhausted": True,  # 한 번에 다 훑었다 — 이어달리기 없이 다음 단계로
        },
    )


class TestSliceResume:
    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch):
        monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
        _no_network(monkeypatch)

    def _job_with_media(self, conn, n):
        # estimated_seconds 를 채워 _stage_estimate 를, media 를 채워 _stage_media 를 스킵.
        return _job(conn, estimated_seconds=10, stage_data={"media": _slice_media(n)})

    @pytest.mark.django_db
    def test_completes_across_slices_without_redoing_work(self, monkeypatch):
        """게시물 4개를 슬라이스로 쪼개 돌려도 **각 1회씩만** 조회하고 결국 완주한다."""
        conn = _conn(_ws(_user()), mock_token=True)
        job = self._job_with_media(conn, 4)
        calls: list = []
        monkeypatch.setattr(pipeline.recover, "recover_post", _fake_recover_post(calls))
        # 슬라이스당 게시물 1개만 처리되도록 시계를 빠르게 흘린다.
        monkeypatch.setattr(pipeline, "time", _FakeClock(step=700))
        redispatch = MagicMock()

        status, runs = _drive(job, redispatch)

        assert status == DMMigrationJob.Status.READY, status
        assert runs > 1, "슬라이스로 쪼개지지 않았다 — 이 테스트가 무의미해진다"
        # 핵심: 재개가 처음부터 다시 하지 않는다.
        assert sorted(calls) == [f"sm-{i}" for i in range(4)], calls
        assert len(calls) == len(set(calls)), f"같은 게시물을 다시 조회했다: {calls}"
        # 슬라이스에 걸쳐 모은 결과가 전부 후보가 됐다.
        job.refresh_from_db()
        assert job.candidates.count() == 4
        assert job.candidates.filter(offer_url__startswith="https://ex.co/").count() == 4
        assert redispatch.call_count == runs - 1

    @pytest.mark.django_db
    def test_partial_progress_is_persisted_between_slices(self, monkeypatch):
        """슬라이스가 끝나면 진행분이 DB 에 남는다 — 프로세스가 죽어도 여기서 이어간다."""
        conn = _conn(_ws(_user()), mock_token=True)
        job = self._job_with_media(conn, 4)
        monkeypatch.setattr(pipeline.recover, "recover_post", _fake_recover_post([]))
        monkeypatch.setattr(pipeline, "time", _FakeClock(step=700))

        assert pipeline.run_migration(str(job.id), redispatch=MagicMock()) == "continued"

        job.refresh_from_db()
        assert job.status == DMMigrationJob.Status.RUNNING  # 실패가 아니다
        assert job.stage_data["recover_done"] == ["sm-0"]
        assert len(job.stage_data["recover_partial"]) == 1
        assert "recoveries" not in job.stage_data  # 아직 확정 아님
        assert job.dm_messages_collected == 8  # 지지 인원이 카운터에 즉시 반영된다
        assert job.resume_at is not None

    @pytest.mark.django_db
    def test_slice_cap_still_produces_candidates(self, monkeypatch):
        """슬라이스 상한에 닿아도 **모은 것까지는 후보로 만들어** 준다(부분 완료).

        여기서 그냥 종결하면 몇 시간 수집한 결과가 후보 0건으로 나간다.
        """
        conn = _conn(_ws(_user()), mock_token=True)
        job = self._job_with_media(conn, 6)
        calls: list = []
        monkeypatch.setattr(pipeline.recover, "recover_post", _fake_recover_post(calls))
        monkeypatch.setattr(pipeline, "time", _FakeClock(step=700))
        monkeypatch.setattr(pipeline, "MAX_SLICES", 2)
        monkeypatch.setattr(pipeline, "ABSOLUTE_MAX_SLICES", 3)

        status, _runs = _drive(job, MagicMock())

        assert status == DMMigrationJob.Status.PARTIAL, status
        assert len(calls) == 2  # 상한에서 수집을 끊었다
        job.refresh_from_db()
        assert job.candidates.count() == 2  # 끊긴 시점까지는 초안이 나왔다
        assert job.stage_data.get("truncated") is True

    @pytest.mark.django_db
    def test_no_redispatch_finalizes_partial_instead_of_hanging(self, monkeypatch):
        """재개 경로가 없으면(동기 호출) running 으로 방치하지 않고 부분 완료로 종결한다.

        running 으로 두면 비종결 UNIQUE 제약이 연결을 스위퍼(2h)까지 잠근다.
        """
        conn = _conn(_ws(_user()), mock_token=True)
        job = self._job_with_media(conn, 4)
        monkeypatch.setattr(pipeline.recover, "recover_post", _fake_recover_post([]))
        monkeypatch.setattr(pipeline, "time", _FakeClock(step=700))

        assert pipeline.run_migration(str(job.id)) == DMMigrationJob.Status.PARTIAL

        job.refresh_from_db()
        assert job.status not in DMMigrationJob.NON_TERMINAL_STATUSES
        assert job.candidates.count() == 1  # 1건이라도 건져서 내보낸다

    @pytest.mark.django_db
    def test_progress_survives_token_failure(self, monkeypatch):
        """도중에 토큰이 죽어도 그때까지 조회한 게시물은 stage_data 에 남는다."""
        conn = _conn(_ws(_user()), mock_token=True)
        job = self._job_with_media(conn, 6)
        calls: list = []
        base = _fake_recover_post(calls)

        def _die_on_third(ctx, media, **kw):
            if len(calls) >= 2:
                raise C.MigrationTokenError(code=190)
            return base(ctx, media, **kw)

        monkeypatch.setattr(pipeline.recover, "recover_post", _die_on_third)

        assert pipeline.run_migration(str(job.id)) == DMMigrationJob.Status.FAILED

        job.refresh_from_db()
        assert job.error_code == "token_expired"
        assert len(job.stage_data.get("recover_done") or []) == 2
        assert len(job.stage_data.get("recover_partial") or []) == 2

    @pytest.mark.django_db
    def test_failed_post_is_not_retried_forever(self, monkeypatch):
        """게시물 1건이 계속 실패해도 진행은 넘어간다 — 재개 때 같은 곳에서 또 넘어지면 안 된다."""
        conn = _conn(_ws(_user()), mock_token=True)
        job = self._job_with_media(conn, 3)
        calls: list = []
        base = _fake_recover_post(calls)

        def _one_bad(ctx, media, **kw):
            if media["id"] == "sm-1":
                calls.append(media["id"])
                raise ValueError("이 게시물만 깨진다")
            return base(ctx, media, **kw)

        monkeypatch.setattr(pipeline.recover, "recover_post", _one_bad)
        monkeypatch.setattr(pipeline, "time", _FakeClock(step=700))

        status, _runs = _drive(job, MagicMock())

        assert status == DMMigrationJob.Status.READY, status
        assert calls.count("sm-1") == 1, "실패한 게시물을 재개할 때마다 다시 시도했다"
        job.refresh_from_db()
        assert job.candidates.count() == 2  # 깨진 1건만 빠졌다


@pytest.mark.django_db
def test_sliced_run_matches_single_run_end_to_end(monkeypatch):
    """**등가성** — 쪼개서 돌린 결과가 한 번에 돌린 결과와 같아야 한다.

    앞의 테스트들은 recover_post 를 가짜로 바꾼다. 여기서는 진짜 복원기 + mock 픽스처로
    전체 파이프라인을 두 번(한 번에 / 쪼개서) 돌려 후보가 동일한지 본다 — 이어달리기가
    결과를 바꾸지 않는다는 것이 이 기능의 전제다.
    """
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
    # 테스트 러너는 DEBUG=False 라 is_mock_mode()가 False → mock 픽스처로 강제.
    monkeypatch.setattr(C, "is_mock", lambda token: True)

    def _run(sliced: bool) -> list[tuple]:
        conn = _conn(_ws(_user()), mock_token=True)
        # mock 픽스처는 ig 계정 id 를 시드로 쓴다 → 두 번의 실행을 같은 데이터로 맞춘다.
        conn.external_account_id = "17841400000000001"
        conn.save(update_fields=["external_account_id"])
        job = _job(conn, media_limit=50)
        if sliced:
            monkeypatch.setattr(pipeline, "time", _FakeClock(step=700))
            # 게시물 1개마다 자르므로 슬라이스 상한을 넉넉히 — 여기선 '중간에 끊기'가 아니라
            # '끝까지 이어 달렸을 때 같은 결과인가'를 본다.
            monkeypatch.setattr(pipeline, "MAX_SLICES", 60)
            monkeypatch.setattr(pipeline, "ABSOLUTE_MAX_SLICES", 62)
            status, runs = _drive(job, MagicMock(), max_runs=70)
            assert runs > 2, f"쪼개지지 않았다 (runs={runs}) — 등가성 검증이 무의미해진다"
        else:
            monkeypatch.setattr(pipeline, "time", _FakeClock(step=1))  # 절대 안 잘림
            status = pipeline.run_migration(str(job.id))
        assert status in (DMMigrationJob.Status.READY, DMMigrationJob.Status.PARTIAL), status
        job.refresh_from_db()
        return sorted(
            (c.media_id, c.band, c.offer_url, c.support_hits, c.support_probed, c.gate_detected)
            for c in job.candidates.all()
        )

    whole = _run(sliced=False)
    assert whole, "mock e2e 가 후보를 하나도 못 만들었다 — 이 테스트의 전제가 깨졌다"
    assert [c for c in whole if c[2]], "오퍼 링크 복원 0건 (첨부 추출 회귀?)"
    assert _run(sliced=True) == whole


class TestDraftSlicing:
    """초안(LLM) 단계도 한 태스크에 안 들어간다 — 후보 200건이면 LLM 호출 30여 회.

    실측(2026-08-17 prod): 복원은 456/456 끝냈는데 초안 단계에서 하드 킬(1740s). 원인은
    ``max_tokens=4000`` 이 추론 모델의 reasoning 예산에 통째로 먹혀 빈 응답 → 재시도 →
    이어받기 6회로 번져 **배치 1개에 195초**가 걸린 것. 예산은 llm.DRAFTS_MAX_TOKENS 로
    올렸고, 그래도 오래 걸리는 계정을 위해 여기서 이어달리기를 보장한다.
    """

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch):
        _no_network(monkeypatch)

    def _job_with_recoveries(self, conn, n):
        recs = [
            {
                "media_id": f"dm-{i}",
                "permalink": f"https://x/{i}",
                "caption": "댓글에 자료 남겨주세요",
                "timestamp": "2026-07-01T00:00:00+0000",
                "comments_count": 30,
                "probed": 10,
                "trigger": "자료",
                "repetition": 0.5,
                "signal": True,
                "offer": {
                    "text": "요청하신 자료 보내드려요",
                    "url": f"https://ex.co/{i}",
                    "label": "자료 받기",
                    "hits": 8,
                    "ratio": 0.8,
                    "score": 0.72,
                },
                "gate": None,
                "grade": "auto_draft",
                "score": 0.72,
                "confirm_required": False,
                "drops": [],
                "samples": [],
                "keyword_hits": {"자료": 9},
            }
            for i in range(n)
        ]
        return _job(conn, estimated_seconds=10, stage_data={"media": [], "recoveries": recs})

    @staticmethod
    def _counting_llm(calls: list):
        def _fn(batch, *, model_code="deepseek"):
            calls.append([c["media_id"] for c in batch])
            out = {
                c["media_id"]: {
                    "media_id": c["media_id"],
                    "name": f"캠페인 {c['media_id']}",
                    "description": "설명",
                    "keywords": ["자료"],
                    "keyword_mode": "any",
                    "public_reply_draft": "DM 드렸어요",
                    "first_dm_draft": "요청하신 자료 보내드려요",
                    "followup_candidates": [],
                    "confidence": 0.8,
                }
                for c in batch
            }
            return out, {"llm_calls": 1, "llm_tokens": 100}

        return _fn

    @pytest.mark.django_db
    def test_drafts_resume_without_recalling_llm(self, monkeypatch):
        """초안도 슬라이스로 쪼개지고, 재개해도 **같은 게시물을 다시 LLM 에 안 보낸다**."""
        conn = _conn(_ws(_user()), mock_token=True)
        job = self._job_with_recoveries(conn, 18)
        calls: list = []
        monkeypatch.setattr(pipeline.llm, "generate_drafts", self._counting_llm(calls))
        monkeypatch.setattr(pipeline, "time", _FakeClock(step=700))  # 배치 1개마다 자름

        status, runs = _drive(job, MagicMock())

        assert status == DMMigrationJob.Status.READY, status
        assert runs > 1, "초안 단계가 쪼개지지 않았다"
        sent = [mid for batch in calls for mid in batch]
        assert len(sent) == len(set(sent)) == 18, f"LLM 재호출 발생: {sent}"
        job.refresh_from_db()
        assert job.candidates.count() == 18
        assert job.candidates.filter(draft_name__startswith="캠페인").count() == 18
        assert job.llm_calls == len(calls)

    @pytest.mark.django_db
    def test_terminal_path_creates_candidates_without_more_llm(self, monkeypatch):
        """상한에 닿으면 남은 초안은 **규칙 기반**으로 채우고 후보를 반드시 만든다.

        여기서 포기하면 몇 시간 복원한 결과가 후보 0건으로 나간다(실측 265건 위험).
        """
        conn = _conn(_ws(_user()), mock_token=True)
        job = self._job_with_recoveries(conn, 18)
        calls: list = []
        monkeypatch.setattr(pipeline.llm, "generate_drafts", self._counting_llm(calls))
        monkeypatch.setattr(pipeline, "time", _FakeClock(step=700))
        monkeypatch.setattr(pipeline, "MAX_SLICES", 1)
        monkeypatch.setattr(pipeline, "ABSOLUTE_MAX_SLICES", 1)

        status, _runs = _drive(job, MagicMock())

        assert status == DMMigrationJob.Status.PARTIAL, status
        drafted = len([m for b in calls for m in b])
        assert drafted < 18, "상한이 걸리지 않았다 — 테스트 전제가 깨졌다"
        job.refresh_from_db()
        assert job.candidates.count() == 18, "복원분이 후보로 안 나왔다"
        assert job.stage_data.get("drafts_truncated") == 18 - drafted
        # LLM 초안이 없는 후보도 **복원된 원문**은 들고 있어야 한다.
        assert job.candidates.exclude(offer_url="").count() == 18

    @pytest.mark.django_db
    def test_drafts_budget_is_large_enough_for_reasoning_tail(self):
        """추론 모델은 reasoning 이 completion 예산 안에 든다 — 4000 은 빈 응답을 만든다."""
        from apps.integrations.dm_migration import llm

        assert llm.DRAFTS_MAX_TOKENS >= 16000, llm.DRAFTS_MAX_TOKENS


class TestSliceTimingInvariants:
    """시간 상수들 사이의 관계 — 하나만 바꾸면 조용히 고장 나는 것들."""

    def test_resume_countdown_clears_duplicate_run_guard(self):
        """재개 지연이 중복 실행 가드(60초)보다 짧으면 재개 태스크가 조용히 스킵된다."""
        assert pipeline.RESUME_COUNTDOWN > 60

    def test_slice_ends_before_soft_time_limit(self):
        """슬라이스가 소프트 한도보다 늦게 끝나면 자발적 저장 경로가 죽은 코드가 된다."""
        from apps.integrations.tasks import run_dm_migration_job as task

        assert pipeline.SLICE_SECONDS < task.soft_time_limit
        assert task.soft_time_limit - pipeline.SLICE_SECONDS >= 120  # 진행 중 1건 마칠 여유

    def test_soft_to_hard_gap_allows_checkpoint_save(self):
        """soft→hard 간격이 짧으면 부분 저장이 하드 킬에 같이 잘린다(예전 60초 버그)."""
        from apps.integrations.tasks import run_dm_migration_job as task

        assert task.time_limit - task.soft_time_limit >= 240
        assert task.time_limit <= settings.CELERY_TASK_TIME_LIMIT


# ══════════════ 발신함 전수 훑기도 이어달리기가 된다 ══════════════
#
# 전수 훑기는 수백~천 페이지가 될 수 있어 한 태스크(25분)에 안 들어간다. 커서를 저장하지
# 않으면 슬라이스마다 1페이지부터 다시 시작해 **영원히 못 끝낸다** — 복원 단계에서 이미
# 겪은 실패라 같은 함정을 두 번 밟지 않도록 고정한다.


class TestOutboxSlicing:
    @pytest.mark.django_db
    def test_outbox_resumes_from_cursor(self, monkeypatch):
        monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
        for name in ("fetch_media", "collect_commenters", "fetch_outbound_for_commenter"):
            monkeypatch.setattr(C, name, lambda *a, **k: [])

        seen_cursors = []
        pages = {"n": 0}

        def _fake_conv(ctx, lookback_days=400, *, after=None, should_stop=None):
            seen_cursors.append(after)
            pages["n"] += 1
            last = pages["n"] >= 3
            return {
                "outbound": [
                    {
                        "recipient": f"u{pages['n']}",
                        "msg_id": f"m{pages['n']}",
                        "created_time": "2026-07-01T00:00:00+0000",
                        "text": "자료 링크",
                        "content": {},
                    }
                ],
                "scope_missing": False,
                "conversations_scanned": 25,
                "paging_after": None if last else f"cur{pages['n']}",
                "exhausted": last,
            }

        monkeypatch.setattr(C, "fetch_conversations", _fake_conv)
        conn = _conn(_ws(_user()), mock_token=True)
        job = _job(conn, estimated_seconds=10, stage_data={"media": []})

        status, _runs = _drive(job, MagicMock())

        assert seen_cursors == [None, "cur1", "cur2"], seen_cursors  # 커서를 이어받았다
        job.refresh_from_db()
        assert len(job.stage_data["outbox"]) == 3  # 슬라이스별 수집분이 **누적**됐다
        assert job.stage_data["outbox_done"] is True
        assert "outbox_cursor" not in job.stage_data
        assert job.conversations_scanned == 75  # 누적 집계
        assert status in (DMMigrationJob.Status.READY, DMMigrationJob.Status.PARTIAL)

    @pytest.mark.django_db
    def test_outbox_failure_falls_back_to_per_post(self, monkeypatch):
        """발신함은 가속기다 — 실패해도 기존 경로로 파이프라인이 끝나야 한다."""
        monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
        monkeypatch.setattr(C, "fetch_media", lambda *a, **k: [])
        monkeypatch.setattr(C, "fetch_conversations", lambda *a, **k: 1 / 0)  # noqa: ARG005
        conn = _conn(_ws(_user()), mock_token=True)
        job = _job(conn, estimated_seconds=10, stage_data={"media": []})
        status = pipeline.run_migration(str(job.id))
        assert status in (DMMigrationJob.Status.READY, DMMigrationJob.Status.PARTIAL)
        job.refresh_from_db()
        assert job.stage_data["outbox_done"] is False
