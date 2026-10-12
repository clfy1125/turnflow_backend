"""`code=200` 을 '게시물 제한'으로 읽어 **정반대 안내**를 하던 오분류의 회귀 테스트.

사고 이력
─────────────────────────────────────────────────────────────────────────
계정 소유자가 인스타 설정에서 '연결된 도구 → 메시지 액세스 허용'을 끄면 그 계정의
**모든** 자동 DM 이 ``http=403 / code=200 / subcode 없음`` 으로 거절된다. 그런데
``_BY_CODE["200"]`` 이 이 행을 ``U_POST_RESTRICTED`` 로 접어, 화면이 이렇게 안내했다.

    "이 게시물에는 자동 DM 발송이 제한되어 있어요 …
     **다른 게시물에서는 정상적으로 발송될 수 있어요** …
     다른 게시물로 캠페인을 만드시면 정상적으로 발송돼요"

계정 단위 차단이라 **다른 게시물도 똑같이 실패한다.** 실제 피해:

- @highestlevel33 — 2026-09-28 "연령제한 콘텐츠로 분류되어…" 문의 → 10-07 에야
  '메시지 액세스 허용 OFF' 로 판명. **9일**, 그 사이 2,671건 실패·256명 소급 발송.
- @zoommaracha — 2026-10-12 같은 문구로 문의. 안내대로 다른 게시물을 시도했다가
  "정상적으로 되던 게시물도 안 된다" 고 2차 문의.

prod 전수(code=200 3,531건)는 정확히 둘로 갈렸다 — **미분류 0건, 메시지 겹침 0건**.

| subcode | 본문 | 건수 | 뜻 |
|---|---|---|---|
| (없음) | ``The account owner has disabled access to Instagram Direct Messaging.`` | 2,693 | 계정 단위 차단 |
| 2534066 | ``액세스 토큰에 Instagram 비공개 답장에…`` | 838 | 게시물 단위 제한 |

이 파일이 지키는 것
─────────────────────────────────────────────────────────────────────────
1. 두 케이스가 **서로 다른 사유**로 갈린다.
2. 계정 차단 문구가 "다른 게시물" 을 권하지 **않는다** (정반대 안내 재발 방지).
3. 운영자 사전도 같은 축으로 읽는다 (어드민·유저 화면이 갈리지 않게).
"""

from __future__ import annotations

import pytest

from apps.admin_api.dm_error_catalog import describe
from apps.integrations.dm_frontend_actions import build_frontend_action
from apps.integrations.dm_user_reasons import (
    U_DM_ACCESS_DISABLED,
    U_POST_RESTRICTED,
    user_reason_for,
)

# 실제 prod 로그에서 그대로 가져온 값
DISABLED_MSG = "The account owner has disabled access to Instagram Direct Messaging."
POST_BLOCKED_MSG = (
    "액세스 토큰에 Instagram 비공개 답장에 대한 세분화된 Instagram 권한이 충분한지 확인하세요."
)

STATUS = "failed_no_trace"  # 두 케이스 모두 이 상태로 기록된다


# ── 1. 사유가 갈리는가 ────────────────────────────────────────────────


class TestUserReasonSplit:
    def test_account_disabled_is_not_post_restricted(self):
        """subcode 없는 code=200 = **계정** 차단. 게시물 제한으로 읽으면 안 된다."""
        reason = user_reason_for(STATUS, error_code="200", error_subcode="")
        assert reason == U_DM_ACCESS_DISABLED, (
            f"code=200(subcode 없음)이 {reason!r} 로 분류됐다. "
            "이건 계정 소유자가 DM 접근을 끈 것이라 게시물과 무관하다."
        )

    def test_post_block_still_post_restricted(self):
        """subcode 2534066 은 **게시물** 제한 그대로 — 1단계(subcode)에서 잡힌다."""
        assert (
            user_reason_for(STATUS, error_code="200", error_subcode="2534066") == U_POST_RESTRICTED
        )

    def test_two_cases_do_not_collapse(self):
        """두 케이스가 같은 사유로 접히면 CS 가 구분할 수 없다."""
        a = user_reason_for(STATUS, error_code="200", error_subcode="")
        b = user_reason_for(STATUS, error_code="200", error_subcode="2534066")
        assert a != b


# ── 2. 문구가 정반대 안내를 하지 않는가 ───────────────────────────────


class TestCopyDoesNotMisdirect:
    def test_account_disabled_copy_never_suggests_another_post(self):
        """ "다른 게시물로 만드세요" 는 **계정 차단에서 치명적인 오안내**다.

        고객이 그대로 따라 하면 똑같이 실패하고, 우리가 시킨 헛수고가 된다
        (실제로 @zoommaracha 가 그렇게 2차 문의를 했다).
        """
        copy = build_frontend_action(
            status=STATUS, error_code="200", error_subcode="", error_message=DISABLED_MSG
        )
        blob = " ".join(
            str(copy.get(k) or "") for k in ("title", "cause", "next_step", "description")
        )
        for forbidden in ("다른 게시물", "새 게시물", "게시물로 캠페인"):
            assert (
                forbidden not in blob
            ), f"계정 차단 문구에 '{forbidden}' 안내가 들어 있다 — 따라 해도 실패한다.\n{blob}"

    def test_account_disabled_copy_points_at_the_setting(self):
        """고객이 **혼자 고칠 수 있는 유일한 길**(설정 켜기)을 반드시 담는다."""
        copy = build_frontend_action(
            status=STATUS, error_code="200", error_subcode="", error_message=DISABLED_MSG
        )
        blob = " ".join(
            str(copy.get(k) or "") for k in ("title", "cause", "next_step", "description")
        )
        assert "메시지 액세스" in blob, f"설정 이름이 문구에 없다:\n{blob}"
        assert "연결된 도구" in blob, f"설정 경로가 문구에 없다:\n{blob}"

    def test_post_restricted_copy_still_suggests_another_post(self):
        """반대로 **진짜 게시물 제한**에서는 다른 게시물 안내가 맞는 조치다."""
        copy = build_frontend_action(
            status=STATUS,
            error_code="200",
            error_subcode="2534066",
            error_message=POST_BLOCKED_MSG,
        )
        blob = " ".join(
            str(copy.get(k) or "") for k in ("title", "cause", "next_step", "description")
        )
        assert "게시물" in blob


# ── 3. 운영자 사전과 축이 같은가 ──────────────────────────────────────


class TestAdminCatalogAgrees:
    def test_admin_entry_is_account_scoped(self):
        entry = describe(code="200", subcode="", status=STATUS)
        assert (
            entry["reason"] == "dm_access_disabled"
        ), f"운영자 사전이 {entry['reason']!r} 로 읽는다 — 유저 화면과 축이 갈린다."

    def test_admin_action_warns_against_moving_posts(self):
        """운영자가 고객에게 '다른 게시물로 옮기라' 고 안내하지 않도록 경고가 있어야 한다."""
        entry = describe(code="200", subcode="", status=STATUS)
        action = str(entry.get("action") or "")
        assert "메시지 액세스" in action, f"조치문에 설정 안내가 없다:\n{action}"

    def test_admin_post_block_entry_unchanged(self):
        """2534066 은 종전대로 게시물 차단으로 읽혀야 한다."""
        entry = describe(code="200", subcode="2534066", status=STATUS)
        assert entry["reason"] == "post_blocked"


# ── 4. 실제 로그 모양 그대로 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        DISABLED_MSG,
        f"{DISABLED_MSG} | http=403 | code=200",  # 우리가 꼬리를 붙여 저장하는 형태
    ],
)
def test_real_log_shapes_resolve_to_account_disabled(msg):
    copy = build_frontend_action(
        status=STATUS, error_code="200", error_subcode="", error_message=msg
    )
    assert copy["user_reason"] == U_DM_ACCESS_DISABLED
