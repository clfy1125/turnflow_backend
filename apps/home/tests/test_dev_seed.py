"""홈 테스트 계정 시드 회귀 테스트.

시드는 **프론트가 화면을 확인하는 유일한 수단**이라 조용히 깨지면 아무도 모른다.
알림 판정(`alerts.py`)이 바뀌면 시드가 만든 상태도 같이 바뀌어야 하는데, 그 어긋남을
여기서 잡는다.

⚠️ 전부(43개)를 돌리면 수십 초가 걸린다 — 판정 분기가 서로 다른 대표 코드만 고른다.
⚠️ 이 테스트는 dev DB 를 쓴다(`test-db-not-clean`). 시드는 멱등이라 같은 상태로 덮어쓸 뿐이다.
"""

import pytest
from django.contrib.auth import get_user_model

from apps.home import dev_seed
from apps.home.alerts import build_home_alerts
from apps.home.dev_seed_specs import ALL_CODES, SPEC_BY_CODE

# 코드 → 그 계정에서 **반드시** 나와야 하는 알림 코드
MUST_HAVE = {
    "H01": {"recent_post_no_campaign", "report_ready", "migration_review_pending"},
    "H02": {"payment_failed", "ig_disconnected", "campaign_post_restricted", "dm_send_blocked"},
    "H05": {"dm_quota_exhausted"},
    "H09": {"dm_quota_warning"},
    "H11": {"create_campaign", "link_page_empty"},
    "H12": {"create_campaign"},
    "I04": set(),  # 강제 팝업만 — 아래에서 따로 본다
}


@pytest.fixture(autouse=True)
def _debug_on(settings):
    # guard() 는 DEBUG=True 를 요구한다. pytest 는 기본 DEBUG=False.
    settings.DEBUG = True


@pytest.mark.django_db
@pytest.mark.parametrize("code", sorted(MUST_HAVE))
def test_seed_produces_expected_alerts(code):
    [result] = dev_seed.run([code])
    user = get_user_model().objects.get(email=result.email)
    ws = user.owned_workspaces.get()

    payload = build_home_alerts(ws, user)
    codes = {a["code"] for a in payload["alerts"]}
    assert MUST_HAVE[code] <= codes, f"{code}: {MUST_HAVE[code] - codes} 가 빠졌다"

    if code == "I04":
        assert payload["blocking"]["code"] == "ig_account_selection_required"
    else:
        assert payload["blocking"] is None, f"{code}: 예상 못 한 강제 팝업"


@pytest.mark.django_db
def test_h12_is_quiet():
    """「정상 작동 중」 은 기본 상태 하나만 떠야 한다 — 다른 알림이 끼면 큰 카드가 바뀐다."""
    [result] = dev_seed.run(["H12"])
    user = get_user_model().objects.get(email=result.email)
    payload = build_home_alerts(user.owned_workspaces.get(), user)
    assert [a["code"] for a in payload["alerts"]] == ["create_campaign"]
    assert payload["alerts"][0]["data"]["is_default"] is True


@pytest.mark.django_db
def test_seeded_media_ids_exist_in_mock_pool():
    """캠페인·최신 게시물의 media_id 는 **목록 API 가 돌려주는 id** 여야 한다.

    여기서 어긋나면 화면은 "사진이 안 나온다"로만 보인다 — 종전 시더가 밟은 함정이다.
    """
    from apps.integrations.models import AutoDMCampaign, IGAccountConnection
    from apps.integrations.services import MockInstagramProvider

    [result] = dev_seed.run(["H01"])
    user = get_user_model().objects.get(email=result.email)
    conn = IGAccountConnection.objects.get(workspace__owner=user)
    pool = {
        m["id"]
        for m in MockInstagramProvider.mock_list_media_page(conn.external_account_id, limit=20)[
            "data"
        ]
    }
    assert conn.latest_media_id in pool
    for camp in AutoDMCampaign.objects.filter(ig_connection=conn):
        assert camp.media_id in pool, f"{camp.name}: {camp.media_id} 가 목 풀에 없다"


@pytest.mark.django_db
@pytest.mark.parametrize("code", ["H16", "H17"])
def test_seeded_report_values_are_real_choices(code):
    """리포트 시드의 ``stage``·``error_code`` 는 **TextChoices 안의 값**이어야 한다.

    CharField 는 ``save()`` 때 choices 를 검증하지 않는다 — 없는 값을 넣어도 조용히
    저장되고 화면만 잘못 그려진다. 실제로 ``stage="analyzing"``(없는 값) 때문에 45%
    진행 화면이 「대기 중」으로, ``error_code="extract_failed"``(소문자) 때문에 실패
    화면이 「원인을 알 수 없는 문제」로 나왔다(2026-10-09 프론트 제보).
    """
    from apps.insta_reports.models import InstagramReport, ReportErrorCode, ReportStage

    [result] = dev_seed.run([code])
    user = get_user_model().objects.get(email=result.email)
    report = InstagramReport.objects.filter(workspace=user.owned_workspaces.get()).get()

    assert report.stage in ReportStage.values, f"{code}: stage={report.stage!r}"
    if report.error_code:
        assert report.error_code in ReportErrorCode.values, f"{code}: {report.error_code!r}"


@pytest.mark.django_db
def test_unknown_code_is_rejected():
    with pytest.raises(ValueError):
        dev_seed.run(["NOPE"])


def test_registry_has_no_duplicates():
    assert len(ALL_CODES) == len(set(ALL_CODES))
    assert len(SPEC_BY_CODE) == len(ALL_CODES)


@pytest.mark.django_db
def test_guard_refuses_prod_settings_even_with_debug_on(settings):
    """운영 보호는 **두 겹**이다 — DEBUG 가 켜져 있어도 prod 설정이면 거부한다.

    HTTP 진입점(dev_views)에 인증이 없어서, 게이트가 하나뿐이면 DEBUG 오설정 한 번에
    누구나 계정을 만들고 지울 수 있게 된다.
    """
    settings.DEBUG = True
    settings.SETTINGS_MODULE = "config.settings.prod"
    with pytest.raises(RuntimeError):
        dev_seed.run(["H12"])
