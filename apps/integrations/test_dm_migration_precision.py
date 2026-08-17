"""DM 캠페인 이전 — 정밀도 우선판 계약 테스트.

연구에서 확정된 규칙이 코드에서 깨지지 않도록 고정한다. 특히 **첨부 추출**은
빠지는 순간 복원율이 0 이 되므로(실측 은닉율 67~100%) 회귀 방지의 1순위다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from apps.integrations.dm_migration import collect as C
from apps.integrations.dm_migration import pipeline
from apps.integrations.dm_migration.analyze import (
    dm_text_for_match,
    extract_dm_content,
    wilson_lower_bound,
)
from apps.integrations.models import (
    AutoDMCampaign,
    DMCampaignCandidate,
    DMMigrationJob,
    IGAccountConnection,
)
from apps.integrations.test_dm_migration import _client, _conn, _job, _user, _ws

CAND_URL = "/api/v1/integrations/dm-migration/candidates"
JOB_URL = "/api/v1/integrations/dm-migration/jobs"


# ══════════════ 1. 첨부 추출 — 이게 깨지면 복원율 0 ══════════════


def test_extract_reads_button_template_body_and_url():
    """버튼 DM 은 ``message`` 가 비어 있고 본문이 attachments 안에 있다."""
    msg = {
        "id": "m1",
        "message": "",  # ← 실제 버튼 DM 이 이렇게 온다
        "attachments": {
            "data": [
                {
                    "generic_template": {
                        "title": "요청하신 자료 보내드려요!",
                        "cta": [
                            {"title": "자료 받기", "type": "web_url", "url": "https://ex.co/a"},
                            {"title": "팔로우 확인", "type": "postback"},
                        ],
                    }
                }
            ]
        },
    }
    c = extract_dm_content(msg)
    assert c["text"] == "요청하신 자료 보내드려요!"
    assert "https://ex.co/a" in c["urls"]
    assert c["has_gate_button"] is True  # url 없는 postback = 팔로우 확인 게이트
    assert {b["label"] for b in c["buttons"]} == {"자료 받기", "팔로우 확인"}
    joined = dm_text_for_match(msg)
    assert "자료" in joined and "https://ex.co/a" in joined


def test_extract_handles_unknown_wrapper_and_media():
    """모르는 래퍼 키여도 텍스트/URL 을 건져야 한다 — 도구마다 형식이 다르다."""
    msg = {
        "message": "",
        "attachments": {
            "data": [
                {"weird_wrapper": {"subtitle": "새 도구 문구", "media_url": "https://ex.co/z"}},
                {"image_data": {"url": "https://cdn/x.jpg"}},
            ]
        },
    }
    c = extract_dm_content(msg)
    assert c["text"] == "새 도구 문구"
    assert "https://ex.co/z" in c["urls"]
    assert "attachment_image" in c["media_drops"]  # 프론트 transfer.drops 로 나간다
    assert c["carousel"] is True  # 첨부 2장 이상


def test_extract_plain_text_message_still_works():
    c = extract_dm_content({"message": "안녕하세요 https://ex.co/p 확인해주세요"})
    assert c["kind"] == "text"
    assert c["urls"] == ["https://ex.co/p"]


# ══════════════ 2. 지지비율 — 소표본을 자동 강등 ══════════════


def test_wilson_lower_bound_penalises_small_samples():
    assert wilson_lower_bound(1, 2) < 0.20  # 1/2 = 50% 지만 믿을 수 없다
    assert 0.40 < wilson_lower_bound(3, 3) < 0.55
    assert wilson_lower_bound(10, 10) > 0.70  # 10/10 이라야 자동채택 컷을 넘는다
    assert wilson_lower_bound(0, 10) == 0.0


# ══════════════ 3. 7일 귀속창 — 다른 게시물 DM 배제 ══════════════


def test_attribution_window_excludes_other_campaign_dm(monkeypatch):
    ig = "17841400000000000"
    comment_at = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    msgs = [
        {
            "id": "in",
            "created_time": "2026-07-01T00:10:00+0000",  # 댓글 10분 뒤 = 이 게시물 것
            "message": "이 게시물 자료 https://ex.co/in",
            "from": {"id": ig},
            "to": {"data": [{"id": "u1"}]},
        },
        {
            "id": "out",
            "created_time": "2026-07-31T00:00:00+0000",  # 30일 뒤 = 다른 캠페인
            "message": "다른 캠페인 자료 https://ex.co/out",
            "from": {"id": ig},
            "to": {"data": [{"id": "u1"}]},
        },
    ]
    monkeypatch.setattr(C.InstagramMessagingService, "list_user_conversation", lambda *a, **k: msgs)
    ctx = C.CollectContext(
        ig=ig, token="tok", mock=False, pacer=C.RateLimiter(enabled=False), budget=C.Budget()
    )
    got = C.fetch_outbound_for_commenter(ctx, {"id": "u1", "ts": comment_at, "text": "자료"})
    texts = " ".join(g["text"] for g in got)
    assert "ex.co/in" in texts
    assert "ex.co/out" not in texts


def test_order_probe_targets_mixes_both_ends():
    """캠페인이 게시 직후에 켜졌는지 나중에 켜졌는지 모르므로 양쪽을 섞어 본다."""
    commenters = [{"id": f"u{i}", "text": "", "replied": False} for i in range(6)]
    commenters[3]["text"] = "자료 주세요"
    commenters[5]["replied"] = True
    order = [u["id"] for u in C.order_probe_targets(commenters, trigger="자료")]
    assert order[0] == "u3"  # 트리거 단어를 실제로 단 사람이 최우선
    assert order[1] == "u5"  # 그다음 공개답글 받은 사람
    assert set(order) == {f"u{i}" for i in range(6)}
    assert order.index("u5") < order.index("u0")


# ══════════════ 4. 등급/후보 생성 ══════════════


def _recovery(media_id, hits, probed, score, *, url="https://ex.co/pack"):
    return {
        "media_id": media_id,
        "permalink": f"https://x/{media_id}",
        "caption": "댓글에 '자료' 남겨주세요",
        "timestamp": "2026-07-01T00:00:00+0000",
        "comments_count": 30,
        "probed": probed,
        "trigger": "자료",
        "repetition": 0.5,
        "signal": True,
        "offer": {
            "text": "요청하신 자료 보내드려요",
            "url": url,
            "label": "자료 받기",
            "hits": hits,
            "ratio": hits / probed,
            "score": score,
        },
        "gate": {
            "text": "팔로우 확인을 위해 아래 버튼을 눌러주세요.",
            "url": "",
            "label": "팔로우 확인",
            "hits": probed,
            "ratio": 1.0,
            "score": 0.8,
            "is_gate": True,
        },
        "grade": "auto_draft" if score >= 0.60 else "needs_review",
        "score": score,
        "confirm_required": score < 0.60,
        "drops": [{"code": "attachment_image", "count": 1}] if hits < 3 else [],
        "samples": [{"text": "요청하신 자료", "created_time": "2026-07-01T05:00:00+0000"}],
        "keyword_hits": {"자료": 9},
    }


@pytest.mark.django_db
def test_support_ratio_drives_band_offer_and_confirmation(monkeypatch):
    """지지가 강하면 자동채택, 약하면 링크 확인 대상."""
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
    conn = _conn(_ws(_user()), mock_token=True)
    job = _job(
        conn,
        estimated_seconds=10,
        stage_data={
            "media": [],
            "recoveries": [_recovery("m-strong", 8, 10, 0.72), _recovery("m-weak", 1, 10, 0.09)],
        },
    )
    assert pipeline.run_migration(str(job.id)) == DMMigrationJob.Status.READY

    strong = job.candidates.get(media_id="m-strong")
    assert strong.band == DMCampaignCandidate.Band.AUTO_DRAFT
    assert strong.offer_url == "https://ex.co/pack"
    assert strong.offer_button_label == "자료 받기"
    assert strong.gate_detected is True
    assert strong.gate_button_label == "팔로우 확인"
    assert strong.confirm_required is False
    assert (strong.support_hits, strong.support_probed) == (8, 10)

    # 공개 답글은 **변주 여러 개** — 같은 문장 반복은 인스타 스팸 탐지에 걸린다.
    replies = strong.draft_public_reply_templates
    assert len(replies) >= 10, f"공개 답글이 {len(replies)}개뿐 — 다양화가 빠졌다"
    assert len(set(replies)) == len(replies), "중복 문구가 있다"
    assert all(r.strip() for r in replies)
    # 같은 게시물이면 재분석해도 같은 제안(시드 고정) — 사용자가 문구 바뀜을 겪지 않게.
    assert pipeline._reply_variants("m-strong", None) == pipeline._reply_variants("m-strong", None)
    # 게시물이 다르면 조합도 달라야 캠페인 간에도 겹치지 않는다.
    assert pipeline._reply_variants("m-strong", None) != pipeline._reply_variants("m-weak", None)

    weak = job.candidates.get(media_id="m-weak")
    assert weak.band == DMCampaignCandidate.Band.NEEDS_REVIEW
    assert weak.confirm_required is True  # 표본이 적어 링크 확인을 받아야 한다
    assert weak.transfer_drops  # 못 옮기는 항목이 프론트로 나간다


@pytest.mark.django_db
def test_excluded_band_is_created_when_signal_but_no_dm_found(monkeypatch):
    """DM 은 못 찾았지만 캠페인 정황이 있는 게시물 = `excluded` 후보로 남는다.

    프론트에 "밴드는 2종" 이라고 잘못 답한 적이 있어(2026-08-14) 실제 생성을 고정한다.
    초안 생성 대상이 아니므로 **이름·문구가 비어 있다** — 프론트는 폴백이 필요하다.
    """
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
    conn = _conn(_ws(_user()), mock_token=True)
    rec = _recovery("m-nodm", 0, 6, 0.0)
    rec.update(grade="excluded", offer=None, gate=None, signal=True, confirm_required=False)
    job = _job(conn, estimated_seconds=10, stage_data={"media": [], "recoveries": [rec]})
    assert pipeline.run_migration(str(job.id)) == DMMigrationJob.Status.READY

    c = job.candidates.get(media_id="m-nodm")
    assert c.band == DMCampaignCandidate.Band.EXCLUDED
    assert c.draft_name == "" and c.draft_opening_message == ""  # 초안 없음
    assert c.offer_url == "" and c.gate_detected is False
    assert (c.support_hits, c.support_probed) == (0, 6)
    assert c.confirm_required is False  # 확인받을 링크 자체가 없다
    # 캡션 발췌·키워드는 남는다 — 프론트 폴백 제목의 재료.
    assert c.media_caption_excerpt and c.suggested_keywords

    # 정황이 없으면(signal=False) 후보 자체가 안 만들어진다.
    rec2 = _recovery("m-quiet", 0, 6, 0.0)
    rec2.update(grade="excluded", offer=None, gate=None, signal=False)
    job2 = _job(conn, estimated_seconds=10, stage_data={"media": [], "recoveries": [rec2]})
    pipeline.run_migration(str(job2.id))
    assert not job2.candidates.filter(media_id="m-quiet").exists()


@pytest.mark.django_db
def test_estimate_stage_runs_before_analysis(monkeypatch):
    """본 분석 전에 '약 N분' 을 먼저 산출한다 — 프론트 진행바의 근거."""
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
    monkeypatch.setattr(C, "is_mock", lambda token: True)  # mock 픽스처로 오프라인 실행
    conn = _conn(_ws(_user()), mock_token=True)
    job = _job(conn)
    pipeline.run_migration(str(job.id))
    job.refresh_from_db()
    assert job.estimated_seconds and job.estimated_seconds > 0
    assert job.estimated_at is not None
    d = job.estimate_detail
    assert d["media_with_comments"] > 0
    assert d["seconds_max"] >= d["seconds"]


@pytest.mark.django_db
def test_posts_with_our_campaign_are_excluded(monkeypatch):
    """우리 캠페인이 도는 게시물만 뺀다 — 자기 DM 오염 방지.

    댓글이 적은 게시물은 **빼지 않는다.** 댓글이 3개여도 캡션에 "댓글 남기면 자료 드려요"
    라고 쓰여 있으면 캠페인이 맞다. 다만 DM 조회는 안 한다(표본이 없어 지지비율이 무의미).
    댓글이 0개인 것만 볼 게 없어 제외한다.
    """
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
    conn = _conn(_ws(_user()), mock_token=True)
    job = _job(conn)
    runner = pipeline._Runner(job)
    media = [
        {"id": "mine", "comments_count": 50},
        {"id": "theirs", "comments_count": 50},
        {"id": "quiet", "comments_count": 1},
        {"id": "silent", "comments_count": 0},
    ]
    AutoDMCampaign.objects.create(
        ig_connection=conn,
        name="우리 캠페인",
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
        media_id="mine",
        message_template="자료 드려요",
    )
    targets = runner._targets(media)
    assert {m["id"]: deep for m, deep in targets} == {
        "theirs": True,  # 댓글 50개 → DM 까지 조회
        "quiet": False,  # 댓글 1개 → 판정만
    }


# ══════════════ 5. 적용 — 소급발송 강제 OFF + 링크 버튼 승격 ══════════════


@pytest.mark.django_db
def test_apply_forces_backfill_off_and_promotes_link_to_button():
    """이전으로 만든 캠페인에 소급 발송을 켜면 최대 500명에게 DM 이 두 번째로 간다."""
    from apps.integrations.migration_views import apply_candidate

    conn = _conn(_ws(_user()))
    job = _job(conn, status=DMMigrationJob.Status.READY)
    cand = DMCampaignCandidate.objects.create(
        job=job,
        ig_connection=conn,
        band=DMCampaignCandidate.Band.AUTO_DRAFT,
        media_id="mm-1",
        suggested_keywords=["자료"],
        draft_name="자료 캠페인",
        draft_opening_message="요청하신 자료 보내드려요!",
        offer_url="https://ex.co/pack",
        offer_button_label="자료 받기",
        gate_detected=True,
        gate_message="팔로우 확인을 위해 아래 버튼을 눌러주세요.",
        gate_button_label="팔로우 확인",
        confidence=0.72,
    )
    campaign = apply_candidate(cand, {})
    assert campaign.status == AutoDMCampaign.Status.INACTIVE
    assert campaign.backfill_existing_comments is False  # ← 서버 강제
    assert campaign.source == "dm_migration"  # 프론트가 '불러온 캠페인' 배지를 그린다
    assert campaign.link_buttons[0]["url"] == "https://ex.co/pack"
    assert campaign.follow_gate_enabled is True  # 원본 게이트 구조 복원


@pytest.mark.django_db
def test_confirm_link_edit_then_apply_uses_confirmed_url():
    from apps.integrations.migration_views import apply_candidate

    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    job = _job(conn, status=DMMigrationJob.Status.READY)
    cand = DMCampaignCandidate.objects.create(
        job=job,
        ig_connection=conn,
        band=DMCampaignCandidate.Band.NEEDS_REVIEW,
        media_id="mm-2",
        suggested_keywords=["자료"],
        draft_opening_message="자료 드려요",
        offer_url="https://wrong.example/other",
        confirm_required=True,
        support_hits=1,
        support_probed=10,
    )
    resp = client.post(
        f"{CAND_URL}/{cand.id}/confirm-link/?workspace_id={ws.id}",
        {"url": "https://right.example/pack"},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    assert resp.data["offer"]["url"] == "https://right.example/pack"
    assert resp.data["offer"]["edited"] is True
    assert resp.data["confirm_required"] is False

    cand.refresh_from_db()
    campaign = apply_candidate(cand, {})
    assert campaign.link_buttons[0]["url"] == "https://right.example/pack"


@pytest.mark.django_db
def test_confirm_link_reject_dismisses_candidate():
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    job = _job(conn, status=DMMigrationJob.Status.READY)
    cand = DMCampaignCandidate.objects.create(
        job=job, ig_connection=conn, media_id="mm-3", confirm_required=True
    )
    resp = client.post(
        f"{CAND_URL}/{cand.id}/confirm-link/?workspace_id={ws.id}",
        {"correct": False},
        format="json",
    )
    assert resp.status_code == 200
    cand.refresh_from_db()
    assert cand.status == DMCampaignCandidate.Status.DISMISSED


# ══════════════ 6. 목록/집계/일괄적용 ══════════════


@pytest.mark.django_db
def test_candidates_list_pagination_search_ordering_and_summary():
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    job = _job(conn, status=DMMigrationJob.Status.READY)
    for i in range(7):
        DMCampaignCandidate.objects.create(
            job=job,
            ig_connection=conn,
            media_id=f"mm-{i}",
            band=(
                DMCampaignCandidate.Band.AUTO_DRAFT
                if i < 4
                else DMCampaignCandidate.Band.NEEDS_REVIEW
            ),
            draft_name=("룩북 캠페인" if i == 0 else f"캠페인 {i}"),
            offer_url=("https://ex.co/a" if i < 5 else ""),
            confirm_required=(i >= 4),
            media_timestamp=timezone.now() - timedelta(days=i),
        )
    base = f"{JOB_URL}/{job.id}"

    r = client.get(f"{base}/candidates/?workspace_id={ws.id}&page_size=3")
    assert r.status_code == 200
    assert r.data["count"] == 7 and len(r.data["results"]) == 3
    assert r.data["next"] and r.data["previous"] is None

    assert client.get(f"{base}/candidates/?workspace_id={ws.id}&search=룩북").data["count"] == 1
    assert (
        client.get(f"{base}/candidates/?workspace_id={ws.id}&needs_confirm=true").data["count"] == 3
    )
    assert client.get(f"{base}/candidates/?workspace_id={ws.id}&ordering=bogus").status_code == 400

    light = client.get(f"{base}/candidates/?workspace_id={ws.id}&view=list").data["results"][0]
    assert "evidence_raw" not in light  # 목록에서는 큰 필드를 뺀다
    assert "offer" in light and "support" in light

    s = client.get(f"{base}/candidates/summary/?workspace_id={ws.id}").data
    assert s["total"] == 7
    assert s["by_band"]["auto_draft"] == 4
    assert s["needs_confirm"] == 3
    assert s["with_offer_url"] == 5
    assert s["media_date_range"]["first"] and s["media_date_range"]["last"]


@pytest.mark.django_db
def test_apply_all_reports_partial_and_skips_reapplied():
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    job = _job(conn, status=DMMigrationJob.Status.READY)
    for i in range(3):
        DMCampaignCandidate.objects.create(
            job=job,
            ig_connection=conn,
            band=DMCampaignCandidate.Band.AUTO_DRAFT,
            media_id=f"mm-a{i}",
            suggested_keywords=["자료"],
            draft_name=f"캠페인 {i}",
            draft_opening_message="자료 드려요",
            offer_url="https://ex.co/a",
        )
    # 게시물을 특정 못 하는 후보는 대상에서 빠진다(media_id 필요)
    DMCampaignCandidate.objects.create(
        job=job, ig_connection=conn, band=DMCampaignCandidate.Band.AUTO_DRAFT, media_id=""
    )
    url = f"{JOB_URL}/{job.id}/apply-all/?workspace_id={ws.id}"
    resp = client.post(url, {}, format="json")
    assert resp.status_code == 200, resp.data
    assert len(resp.data["applied"]) == 3
    assert AutoDMCampaign.objects.filter(ig_connection=conn, source="dm_migration").count() == 3
    again = client.post(url, {}, format="json")
    assert again.data["skipped"] == 3 and again.data["applied"] == []


# ══════════════ 7. 계정 단위 상태 + 선작업 ══════════════


@pytest.mark.django_db
def test_prompt_answer_roundtrip_and_prefetched_job():
    """설문 답은 서버에 남아야 기기가 바뀌어도 같은 질문이 다시 뜨지 않는다."""
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    ready = _job(conn, status=DMMigrationJob.Status.READY, finished_at=timezone.now())
    url = f"{JOB_URL}/prompt-answer/?workspace_id={ws.id}"

    r0 = client.get(url)
    assert r0.data["prompt_answer"] is None
    assert r0.data["prefetched_job"]["id"] == str(ready.id)  # 선분석 결과가 이미 있다

    assert (
        client.post(url, {"prompt_answer": "used"}, format="json").data["prompt_answer"] == "used"
    )
    assert client.post(url, {"conflict_ack": True}, format="json").data["conflict_ack_at"]
    conn.refresh_from_db()
    assert conn.dm_migration_prompt_answer == "used"
    assert conn.dm_migration_conflict_ack_at is not None
    assert client.post(url, {"prompt_answer": "nope"}, format="json").status_code == 400


@pytest.mark.django_db
def test_conflict_ack_accepts_both_field_names_and_clears():
    """프론트가 `conflict_ack_at`(ISO 시각)으로 보내도 저장돼야 한다.

    회신 문서에는 `conflict_ack` 로 적었는데 프론트는 `conflict_ack_at` 로 호출했고,
    서버가 200 을 주면서 값을 버려 "확인했는데 매번 다시 묻는" 증상이 났다. 둘 다 받는다.
    """
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    url = f"{JOB_URL}/prompt-answer/?workspace_id={ws.id}"

    r = client.post(url, {"conflict_ack_at": "2026-08-14T11:36:00.000Z"}, format="json")
    assert r.data["conflict_ack_at"], "conflict_ack_at 로 보낸 값이 저장되지 않았다"
    conn.refresh_from_db()
    saved = conn.dm_migration_conflict_ack_at
    assert saved is not None
    # 클라이언트가 보낸 시각이 아니라 서버 시각을 찍는다 (시계 조작 방지).
    assert saved.year == timezone.now().year and saved > timezone.now() - timedelta(minutes=5)

    # false 로 해제 — 재테스트용.
    assert (
        client.post(url, {"conflict_ack_at": False}, format="json").data["conflict_ack_at"] is None
    )
    conn.refresh_from_db()
    assert conn.dm_migration_conflict_ack_at is None

    # 아예 안 보내면 기존 값이 유지된다(부분 갱신).
    client.post(url, {"conflict_ack": True}, format="json")
    client.post(url, {"prompt_answer": "used"}, format="json")
    conn.refresh_from_db()
    assert conn.dm_migration_conflict_ack_at is not None


@pytest.mark.django_db
def test_candidate_becomes_reappliable_when_its_campaign_is_deleted():
    """적용한 캠페인을 지우면 후보도 다시 불러올 수 있어야 한다.

    dev 실계정(altbit99)에서 캠페인을 전부 삭제했더니 캠페인은 0개인데 후보는 applied 로
    남아 "이미 다 불러왔어요" 가 떴다 — 그 게시물을 영영 다시 못 불러오는 막다른 상태.
    (AutoDMCampaign 삭제 시 applied_campaign 이 SET_NULL 이라 흔적만 남는다.)
    """
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    job = _job(conn, status=DMMigrationJob.Status.READY, finished_at=timezone.now())
    cand = DMCampaignCandidate.objects.create(
        job=job,
        ig_connection=conn,
        status=DMCampaignCandidate.Status.DETECTED,
        band=DMCampaignCandidate.Band.AUTO_DRAFT,
        media_id="m-del",
        draft_name="지워질 캠페인",
        draft_opening_message="안녕하세요",
        suggested_keywords=["자료"],
    )
    base = f"{JOB_URL}/{job.id}"
    r = client.post(f"{base}/apply-all/?workspace_id={ws.id}", {}, format="json")
    assert len(r.data["applied"]) == 1
    campaign = AutoDMCampaign.objects.get(id=r.data["applied"][0]["campaign_id"])

    cand.refresh_from_db()
    assert cand.status == DMCampaignCandidate.Status.APPLIED

    campaign.delete()  # 사용자가 캠페인을 지웠다

    # 목록을 다시 열면 '적용 가능' 으로 돌아와 있어야 한다.
    rows = client.get(f"{base}/candidates/?workspace_id={ws.id}").data["results"]
    assert rows[0]["status"] == DMCampaignCandidate.Status.DETECTED
    # 단 **적용 이력은 남는다** — 없으면 "한 번도 안 불러온 것" 과 구분이 안 돼
    # 「N개 찾음 · 불러오기」 배너가 사용자의 삭제 결정을 잊고 다시 뜬다.
    assert rows[0]["applied_at"], "applied_at 이 지워졌다 — 삭제 이력을 구분할 수 없다"
    cand.refresh_from_db()
    assert cand.applied_at is not None
    assert client.get(f"{base}/candidates/summary/?workspace_id={ws.id}").data["by_status"] == {
        "detected": 1
    }

    # 그리고 실제로 다시 적용된다.
    r2 = client.post(f"{base}/apply-all/?workspace_id={ws.id}", {}, format="json")
    assert len(r2.data["applied"]) == 1
    assert r2.data["applied"][0]["campaign_id"] != str(campaign.id)


@pytest.mark.django_db
def test_campaign_list_exposes_source_and_filters_by_it():
    """캠페인 목록에 `source` 가 실려야 "불러온 캠페인" 배지를 그릴 수 있다.

    DB 에는 있었지만 시리얼라이저 fields 에 빠져 있어 응답에 키 자체가 없었다.
    프론트는 후보를 다시 조회해 applied_campaign_id 로 역참조하고 있었는데,
    잡 목록이 20건까지만 내려가 오래된 사용자는 배지가 조용히 사라졌다.
    """
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)

    def _mk(name, media_id, source):
        return AutoDMCampaign.objects.create(
            ig_connection=conn,
            name=name,
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id=media_id,
            status=AutoDMCampaign.Status.INACTIVE,
            message_template="테스트",
            source=source,
        )

    imported = _mk("불러온 캠페인", "m-imported", "dm_migration")
    own = _mk("직접 만든 캠페인", "m-own", "")

    url = f"/api/v1/integrations/auto-dm-campaigns/?workspace_id={ws.id}"

    def _rows(suffix=""):
        r = client.get(url + suffix)
        assert r.status_code == 200, r.data
        data = r.data
        return data["results"] if isinstance(data, dict) and "results" in data else data

    by_id = {row["id"]: row for row in _rows()}
    assert by_id[str(imported.id)]["source"] == "dm_migration"
    assert by_id[str(own.id)]["source"] == ""

    assert [r["id"] for r in _rows("&source=dm_migration")] == [str(imported.id)]
    assert [r["id"] for r in _rows("&source=direct")] == [str(own.id)]
    assert client.get(url + "&source=bogus").status_code == 400

    # source 는 서버 소유 — 사용자가 PATCH 로 위조할 수 없다.
    r = client.patch(
        f"/api/v1/integrations/auto-dm-campaigns/{own.id}/?workspace_id={ws.id}",
        {"source": "dm_migration"},
        format="json",
    )
    assert r.status_code in (200, 400)
    own.refresh_from_db()
    assert own.source == ""


@pytest.mark.django_db
def test_prewarm_creates_job_once_and_respects_cache(monkeypatch):
    """연동 직후 선작업 — 이미 돌고 있거나 캐시가 살아 있으면 다시 돌리지 않는다."""
    from apps.integrations import tasks as t

    conn = _conn(_ws(_user()))
    dispatched = []
    monkeypatch.setattr(t.run_dm_migration_job, "delay", lambda jid: dispatched.append(jid))

    jid = t.prewarm_dm_migration(str(conn.id))
    assert dispatched == [jid]
    job = DMMigrationJob.objects.get(id=jid)
    assert job.trigger_source == "auto_connect"
    assert job.requested_by is None  # 사용자가 요청한 게 아니다

    assert t.prewarm_dm_migration(str(conn.id)) == "skipped:running"

    job.status = DMMigrationJob.Status.READY
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "finished_at"])
    assert t.prewarm_dm_migration(str(conn.id)) == "skipped:cached"


@pytest.mark.django_db
def test_prewarm_skips_inactive_connection(monkeypatch):
    from apps.integrations import tasks as t

    conn = _conn(_ws(_user()))
    conn.is_active = False
    conn.save(update_fields=["is_active"])
    monkeypatch.setattr(t.run_dm_migration_job, "delay", lambda jid: None)
    assert t.prewarm_dm_migration(str(conn.id)) == "skipped:no_connection"
    assert not DMMigrationJob.objects.filter(ig_connection=conn).exists()


@pytest.mark.django_db
def test_reuse_window_is_seven_days():
    """연동 직후 선분석 결과를 사용자가 열었을 때 그대로 재사용돼야 즉시 결과가 보인다."""
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    cached = _job(
        conn,
        status=DMMigrationJob.Status.READY,
        finished_at=timezone.now() - timedelta(days=5),
        trigger_source="auto_connect",
    )
    resp = client.post(
        f"{JOB_URL}/?workspace_id={ws.id}", {"ig_connection_id": str(conn.id)}, format="json"
    )
    assert resp.status_code == 200
    assert resp.data["reused"] is True
    assert resp.data["job"]["id"] == str(cached.id)
    assert resp.data["job"]["trigger_source"] == "auto_connect"


@pytest.mark.django_db
def test_free_plan_can_start_migration():
    """이전은 신규 유입 경로라 요금제로 막지 않는다(무료 포함)."""
    user = _user()
    ws = _ws(user)
    conn = _conn(ws)
    client = _client(user)
    assert not hasattr(conn.workspace, "subscription")  # 구독 없음 = 무료
    resp = client.post(
        f"{JOB_URL}/?workspace_id={ws.id}", {"ig_connection_id": str(conn.id)}, format="json"
    )
    assert resp.status_code in (200, 201), resp.data
    assert DMMigrationJob.objects.filter(ig_connection=conn).exists()
    assert IGAccountConnection.objects.filter(id=conn.id).exists()


# ══════════════ 8. 긴 원문 — 우리 한도에 맞춰 담기 ══════════════


def test_fit_dm_text_respects_format_limits():
    """타사는 여러 통으로 쪼개 보내서 한 통이 우리 한도를 넘는 원문이 나온다.

    링크 버튼을 붙이면 한도가 **오히려 늘어난다**(한글 333자 → 640자).
    """
    from apps.integrations.dm_migration.analyze import fit_dm_text

    korean = "가" * 500  # 500자 = 1500바이트

    # 버튼 없음 → UTF-8 1000바이트(한글 ≈333자) 한도 → 잘린다
    fitted, over = fit_dm_text(korean, has_button=False)
    assert over is not None
    assert over["format"] == "plain_text" and over["unit"] == "bytes"
    assert len(fitted.encode("utf-8")) <= 1000
    assert over["original_chars"] == 500

    # 버튼 있음 → 640자 한도 → 500자는 그대로 통과
    fitted2, over2 = fit_dm_text(korean, has_button=True)
    assert over2 is None and fitted2 == korean

    # 버튼 있어도 640자를 넘으면 자른다
    fitted3, over3 = fit_dm_text("나" * 700, has_button=True)
    assert over3["format"] == "button_card" and len(fitted3) <= 640


def test_fit_dm_text_cuts_at_sentence_boundary():
    from apps.integrations.dm_migration.analyze import fit_dm_text

    body = ("첫 문장입니다. " * 120) + "잘릴 꼬리"
    fitted, over = fit_dm_text(body, has_button=True)
    assert over is not None
    assert fitted.endswith(".")  # 말이 중간에 끊기지 않는다


@pytest.mark.django_db
def test_long_original_always_fits(monkeypatch):
    """긴 원문이어도 **한도 안에서 말이 되는 초안**이 나와야 한다.

    잘린 문장을 사용자에게 보여주지 않는다 — 초안 생성 단계가 한도 안에서 다시 쓴다.
    """
    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", True)
    # LLM 초안이 없는 상황(호출 실패 등) → 복원 원문이 그대로 쓰인다
    monkeypatch.setattr(
        pipeline.llm, "generate_drafts", lambda *a, **k: ({}, {"llm_calls": 0, "llm_tokens": 0})
    )
    conn = _conn(_ws(_user()), mock_token=True)
    rec = _recovery("m-long", 8, 10, 0.72)
    rec["offer"]["text"] = "가" * 900  # 한 통에 안 들어가는 원문
    job = _job(conn, estimated_seconds=10, stage_data={"media": [], "recoveries": [rec]})
    assert pipeline.run_migration(str(job.id)) == DMMigrationJob.Status.READY

    cand = job.candidates.get(media_id="m-long")
    assert len(cand.draft_opening_message) <= 640  # 링크가 있어 버튼 카드 한도
    assert cand.draft_opening_message  # 비어 있지 않다
    codes = [d["code"] for d in cand.transfer_drops]
    assert "opening_too_long" not in codes  # 잘렸다고 알리지 않는다(한도 안에서 다시 씀)


@pytest.mark.django_db
def test_apply_never_400s_on_long_recovered_text():
    """apply-all 중 한 건이 길이 때문에 400 나면 일괄 적용이 깨진다 — 그럴 일이 없어야 한다."""
    from apps.integrations.migration_views import apply_candidate

    conn = _conn(_ws(_user()))
    job = _job(conn, status=DMMigrationJob.Status.READY)
    cand = DMCampaignCandidate.objects.create(
        job=job,
        ig_connection=conn,
        band=DMCampaignCandidate.Band.AUTO_DRAFT,
        media_id="mm-long",
        suggested_keywords=["자료"],
        draft_name="긴 캠페인",
        draft_opening_message="가" * 640,  # 버튼 카드 한도 정확히
        offer_url="https://ex.co/pack",
        offer_button_label="자료 받기",
    )
    campaign = apply_candidate(cand, {})
    assert campaign.link_buttons  # 버튼이 붙어 640자 한도가 적용된다
    assert len(campaign.opening_message_template) <= 640


def test_generate_drafts_never_exceeds_limit(monkeypatch):
    """LLM 이 한도를 넘는 초안을 뱉어도 규칙 기반 짧은 초안으로 대체된다."""
    from apps.integrations.dm_migration import llm

    monkeypatch.setattr(settings, "DM_MIGRATION_FAKE_LLM", False)
    long_draft = {
        "drafts": [
            {
                "idx": "d0",
                "name": "긴 캠페인",
                "description": "설명",
                "keywords": ["자료"],
                "keyword_mode": "any",
                "public_reply_draft": "DM 드렸어요",
                "first_dm_draft": "가" * 900,  # 한도 초과
                "followup_candidates": [],
                "confidence": 0.8,
            }
        ]
    }
    monkeypatch.setattr(llm, "_call_json", lambda *a, **k: (1, 100, long_draft))
    monkeypatch.setattr(llm, "resolve_model", lambda code: "deepseek")

    out, _usage = llm.generate_drafts(
        [{"media_id": "m1", "caption": "댓글에 자료", "keywords": ["자료"], "has_button": True}]
    )
    text = out["m1"]["first_dm_draft"]
    assert 0 < len(text) <= 640
    assert "가" * 900 not in text  # 원문을 잘라 붙인 게 아니라 다시 쓴 문구


# ─── 예산 확장 (전수 복원) ────────────────────────────────────────────
#
# 연구는 @highestlevel33 456개 전수에서 313/313~315(99%+)를 복원했는데, 배포본은
# total_graph 1,500 고정이라 실측 3,874콜에 닿을 수 없었다. 예산이 게시물 수를 따라가야
# 한다 — 이 테스트가 그 회귀를 막는다.


class TestBudgetScaling:
    def test_caps_scale_with_media_limit(self):
        small = C.caps_for(100)
        big = C.caps_for(1000)
        assert big["total_graph"] == small["total_graph"] * 10
        assert big["targeted_dms"] == small["targeted_dms"] * 10

    def test_full_account_budget_covers_measured_cost(self):
        """실측: 456개 전수 = 3,874콜. 예산이 그보다 작으면 전수 복원이 구조적으로 불가능."""
        caps = C.caps_for(456)
        assert caps["total_graph"] >= 3874, caps["total_graph"]

    def test_media_pages_cover_requested_count(self):
        """미디어 목록은 1페이지 50개 — 페이지 캡이 모자라면 뒤쪽 게시물을 아예 못 본다."""
        for n in (50, 100, 456, 1500):
            caps = C.caps_for(n)
            assert caps["media"] * C.MEDIA_PAGE_SIZE >= n, (n, caps["media"])

    def test_conversations_cap_does_not_scale(self):
        """대화 목록은 계정당 1회 훑기라 게시물이 늘어도 늘 이유가 없다."""
        assert C.caps_for(100)["conversations_pages"] == C.caps_for(2000)["conversations_pages"]

    def test_small_accounts_are_not_starved(self):
        """게시물이 적어도 최소 예산은 남는다(0 으로 수렴하면 아무것도 못 한다)."""
        caps = C.caps_for(10)
        assert caps["total_graph"] > 0 and caps["targeted_dms"] > 0
        assert caps["media"] >= C.MIN_MEDIA_PAGES

    def test_default_caps_matches_100_media(self):
        """하위 호환 — 기존 DEFAULT_CAPS 는 media_limit=100 과 같아야 한다."""
        c100 = C.caps_for(100)
        for k, v in C.DEFAULT_CAPS.items():
            assert c100[k] == v, k
