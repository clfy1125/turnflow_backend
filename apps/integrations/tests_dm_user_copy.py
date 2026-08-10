"""유저 콘솔 DM 문구(v4) 회귀 테스트 — `../../docs/frontend/DM_USER_COPY_MAPPING.md` 구현분.

이 파일이 지키는 것은 세 가지다.

1. **드리프트 방지** (`TestAdminCatalogParity`)
   운영 사유 사전(`apps.admin_api.dm_error_catalog`)과 유저 사유 테이블
   (`apps.integrations.dm_user_reasons`)이 **두 벌**로 존재한다 — 역방향 의존
   (integrations → admin_api)을 만들지 않기 위한 의도적 중복이다. 그래서 사전에 사유를
   추가하고 유저 매핑을 빠뜨리면 **여기서 실패**해야 한다.

2. **금지어 차단** (`TestForbiddenWords`)
   내부 용어·내부 처리 사정이 유저 문구로 새는 것을 막는다. 사람 리뷰로는 새 문구가
   추가될 때마다 놓친다 — 실제로 v3.2 의 "Private Reply · 파라미터 오류"가 그렇게
   1년 넘게 살아남았고, 프론트가 런타임에 덮어쓰는 지경까지 갔다.

3. **화면 자기모순 방지** (`TestStatusGroupConsistency`)
   탭/배지를 정하는 `status_group` 과 본문을 정하는 `user_reason` 이 어긋나면
   "숨겨진 요청·스팸 탭인데 본문은 댓글 7일 초과" 같은 화면이 나온다.

실행:
    docker compose exec web pytest apps/integrations/tests_dm_user_copy.py
"""

from __future__ import annotations

import pytest

from apps.admin_api.dm_error_catalog import (
    _BY_CODE,
    _BY_CODE_STATUS,
    _BY_CODE_SUBCODE,
    _BY_STATUS,
    SKIPPED_REASONS,
    reason_for,
)
from apps.integrations.dm_frontend_actions import SELF_CHECK_CHECKLIST, build_frontend_action
from apps.integrations.dm_status_groups import HIDDEN_SPAM, status_group
from apps.integrations.dm_user_reasons import (
    _SKIPPED_NEEDLES,
    NO_REASON,
    U_ALREADY_REPLIED,
    U_CONNECTION_LOST,
    U_DELIVERY_UNCONFIRMED,
    U_HIDDEN_REQUEST,
    U_POST_RESTRICTED,
    U_RECIPIENT_UNAVAILABLE,
    U_SEND_DELAYED,
    U_SEND_INCOMPLETE,
    U_WINDOW_EXPIRED,
    user_reason_for,
)

# ── 운영 사유 24종 → 유저 사유 9종 (문서 §4 매핑표를 코드로 옮긴 것) ──────────
# ⚠️ 사전에 새 reason 이 생기면 여기에도 추가해야 한다. 안 하면 아래 테스트가 실패한다.
ADMIN_REASON_TO_USER: dict[str, str] = {
    # U1 연결 해제
    "connection_lost": U_CONNECTION_LOST,
    "session_expired": U_CONNECTION_LOST,
    "token_invalid": U_CONNECTION_LOST,
    # U2 수신자 수신 불가
    "recipient_not_found": U_RECIPIENT_UNAVAILABLE,
    "conversation_deleted": U_RECIPIENT_UNAVAILABLE,
    "recipient_unreachable": U_RECIPIENT_UNAVAILABLE,
    # U3 발송 가능 시간 경과 (내부 사정 2종을 여기로 접는다)
    "window_after_close": U_WINDOW_EXPIRED,
    "window_expired_legacy": U_WINDOW_EXPIRED,
    "no_target_or_expired": U_WINDOW_EXPIRED,
    "window_peak_backlog": U_WINDOW_EXPIRED,
    "stalled_by_us": U_WINDOW_EXPIRED,
    # U4 숨겨진 요청·스팸함
    "hidden_spam_inbox": U_HIDDEN_REQUEST,
    "recovery_pending": U_HIDDEN_REQUEST,
    "recovery_expired": U_HIDDEN_REQUEST,
    # U5 게시물 단위 제한
    "post_blocked": U_POST_RESTRICTED,
    "scoped_permission": U_POST_RESTRICTED,
    # U6 이미 답장
    "already_replied": U_ALREADY_REPLIED,
    # U7 도착 미확인
    "no_trace": U_DELIVERY_UNCONFIRMED,
    "no_trace_unused": U_DELIVERY_UNCONFIRMED,  # 사전의 죽은 항목 (호출 경로 없음)
    # U8 지연
    "rate_limited": U_SEND_DELAYED,
    "app_rate_limited": U_SEND_DELAYED,
    # U8 종결
    "no_reason_given": U_SEND_INCOMPLETE,
    "permission_or_window_unknown": U_SEND_INCOMPLETE,
    "legacy_failure": U_SEND_INCOMPLETE,
    "unclassified": U_SEND_INCOMPLETE,
}


def _catalog_combinations() -> list[tuple[str, str, str]]:
    """사전에 등록된 (code, subcode, status) 조합 전부 + 미등록 폴백 몇 개."""
    combos: list[tuple[str, str, str]] = []
    for code, subcode in _BY_CODE_SUBCODE:
        # status 는 사전 1레벨 판정에 안 쓰이므로 대표값 하나로 훑는다.
        combos.append((code, subcode, "failed_param"))
    for code, status in _BY_CODE_STATUS:
        combos.append((code, "", status))
    for code in _BY_CODE:
        combos.append((code, "", "failed_no_trace"))
    for status in _BY_STATUS:
        combos.append(("", "", status))
    # 사전에 없는 조합 (양쪽 다 폴백으로 떨어져야 함)
    combos += [("9999", "", "failed"), ("", "8888", "failed_no_trace")]
    return combos


class TestAdminCatalogParity:
    """운영 사유 ↔ 유저 사유 매핑이 빠짐없이 성립하는가."""

    def test_every_admin_reason_has_user_mapping(self):
        """사전의 모든 reason 이 유저 사유로 접혀야 한다.

        빠지면 그 사유의 로그에서 유저 화면 문구가 빈칸이 된다.
        """
        seen = set()
        for entry in (
            list(_BY_CODE_SUBCODE.values())
            + list(_BY_CODE_STATUS.values())
            + list(_BY_CODE.values())
            + list(_BY_STATUS.values())
        ):
            seen.add(entry["reason"])
        seen.add("unclassified")  # 사전 미등록 조합의 폴백

        missing = sorted(seen - set(ADMIN_REASON_TO_USER))
        assert not missing, (
            f"운영 사유에 유저 매핑이 없습니다: {missing}\n"
            "→ docs/frontend/DM_USER_COPY_MAPPING.md §4 에 어느 U 버킷인지 정하고 "
            "ADMIN_REASON_TO_USER 에 추가하세요."
        )

    @pytest.mark.parametrize("code,subcode,status", _catalog_combinations())
    def test_resolver_agrees_with_catalog(self, code, subcode, status):
        """같은 로그를 두 사전이 같은 뜻으로 읽는가.

        어드민이 '창이 닫힌 뒤 도착한 요청'이라고 본 건을 유저 쪽이 '연결 해제'로 읽으면
        CS 가 두 화면을 대조할 수 없다.
        """
        admin_reason = reason_for(code, subcode, status)
        expected = ADMIN_REASON_TO_USER.get(admin_reason)
        actual = user_reason_for(status, code, subcode)
        assert actual == expected, (
            f"(code={code!r}, subcode={subcode!r}, status={status!r}) "
            f"어드민={admin_reason!r} → 기대 {expected!r} / 실제 {actual!r}"
        )

    def test_skipped_reasons_match(self):
        """건너뜀 사유는 **같은 키**를 써야 한다 (`?error_reason=` 네임스페이스 공유)."""
        for reason, _label, _actionable, needles in SKIPPED_REASONS:
            for needle in needles:
                actual = user_reason_for("skipped", error_message=f"... {needle} ...")
                assert actual == reason, f"needle={needle!r}: {actual!r} != {reason!r}"

    def test_skipped_needles_match_both_directions(self):
        """양방향으로 대조한다.

        위 테스트는 admin 의 needle 만 훑으므로, **integrations 에만** 사유를 추가하면
        드리프트를 못 잡는다(어드민은 '기타', 유저는 정식 사유로 갈린다). 실제로
        `messaging_window_skip` 을 추가하며 이 구멍을 발견해 메웠다.
        """
        admin_pairs = {(r, n) for r, _l, _a, needles in SKIPPED_REASONS for n in needles}
        user_pairs = {(r, n) for r, needles in _SKIPPED_NEEDLES for n in needles}
        assert user_pairs == admin_pairs, (
            "건너뜀 사전이 갈라졌습니다.\n"
            f"  유저에만: {sorted(user_pairs - admin_pairs)}\n"
            f"  어드민에만: {sorted(admin_pairs - user_pairs)}"
        )

    def test_unknown_skip_message_falls_back_to_other(self):
        assert user_reason_for("skipped", error_message="처음 보는 문구") == "other"

    def test_every_skipped_reason_has_copy(self):
        """건너뜀 사유마다 문구가 있어야 한다 (없으면 화면이 status 문자열로 뜬다)."""
        for reason, _label, _actionable, needles in SKIPPED_REASONS:
            act = build_frontend_action("skipped", error_message=f"x {needles[0]} x")
            assert act["user_reason"] == reason
            assert act["title"] and act["title"] != "skipped", reason
            assert act["cause"], reason


class TestForbiddenWords:
    """내부 용어·내부 처리 사정이 유저 문구로 새지 않는가 (§2-2 기준 8·9)."""

    # 소문자 비교. 한국어는 그대로.
    FORBIDDEN = [
        "private reply",
        "파라미터",
        "토큰",
        "웹훅",
        "subcode",
        "대기열",
        "운영팀",
        "facebook 페이지",
        "35분",
        "meta ",  # "Meta 접수" 등 내부 표현
    ]

    def _all_user_text(self) -> list[tuple[str, str]]:
        """(출처, 문장) — 사용자에게 나가는 모든 문구."""
        out: list[tuple[str, str]] = []
        combos = _catalog_combinations() + [
            ("", "", "skipped"),
            ("", "", "delivered"),
            ("", "", "read"),
            ("", "", "accepted"),
            ("", "", "queued"),
            ("", "", "recovery_pending"),
            ("", "", "recovery_expired"),
            ("", "", "recovery_delivered"),
        ]
        for code, subcode, status in combos:
            act = build_frontend_action(status, subcode, code)
            src = f"{status}/{code}/{subcode}"
            for key in ("title", "cause", "next_step", "description"):
                out.append((f"{src}.{key}", act[key]))
            if act["cta"]:
                out.append((f"{src}.cta", act["cta"]["label"]))
        for item in SELF_CHECK_CHECKLIST:
            out.append((f"checklist.{item['id']}.title", item["title"]))
            out.append((f"checklist.{item['id']}.desc", item["description"]))
        return out

    def test_no_forbidden_words(self):
        hits = [
            (src, word, text)
            for src, text in self._all_user_text()
            for word in self.FORBIDDEN
            if word in (text or "").lower()
        ]
        assert not hits, "유저 문구에 금지어가 있습니다:\n" + "\n".join(
            f"  [{src}] {word!r} → {text}" for src, word, text in hits
        )

    def test_facebook_routing_guidance_is_gone(self):
        """B-1 — Instagram Login 이라 존재하지 않는 화면을 안내하던 항목."""
        ids = {item["id"] for item in SELF_CHECK_CHECKLIST}
        assert "default_routing_app" not in ids
        joined = " ".join(i["description"] for i in SELF_CHECK_CHECKLIST)
        assert "라우팅" not in joined

    def test_no_unverified_causes(self):
        """근거 없는 원인을 다시 넣지 못하게 막는다 (§2-4).

        두 번 걸렀다. 둘 다 "업계에서 다들 그렇게 말해서" 들어간 문구였다.
        - "연결 유효기간(60일)" — 6h 주기 자동 갱신이 있어 활성 계정엔 발생하지 않는다.
        - "비밀번호를 변경하셨거나" — Meta 의 Instagram Platform 문서는 토큰 무효화 사유를
          다루지 않는다. 이 문구는 **Facebook Login 의 subcode 460** 설명이고, 우리 로그에
          그 subcode 는 0건이다.
        """
        banned = ["비밀번호", "60일", "유효기간"]
        hits = [
            (src, word, text)
            for src, text in self._all_user_text()
            for word in banned
            if word in (text or "")
        ]
        assert not hits, "공식 문서·실측으로 확인되지 않은 원인이 문구에 있습니다:\n" + "\n".join(
            f"  [{src}] {word!r} → {text}" for src, word, text in hits
        )

    def test_skipped_copy_does_not_claim_hourly_limit(self):
        """B-2 — 건너뜀을 '시간당 한도'라고 설명하던 문구(제거된 개념)."""
        act = build_frontend_action("skipped", error_message="monthly_dm_limit_reached")
        assert "시간당" not in act["description"]
        assert "이번 달" in act["title"]


class TestStatusGroupConsistency:
    """탭/배지(`status_group`)와 본문(`user_reason`)이 같은 사건을 가리키는가."""

    @pytest.mark.parametrize(
        "code,subcode,status",
        [
            ("100", "2534025", "failed_param"),
            ("", "2534025", "failed_param"),  # code 유실 방어
            ("", "", "recovery_pending"),
            ("", "", "recovery_expired"),
        ],
    )
    def test_hidden_spam_group_gets_hidden_request_copy(self, code, subcode, status):
        assert status_group(status, subcode) == HIDDEN_SPAM
        assert user_reason_for(status, code, subcode) == U_HIDDEN_REQUEST

    def test_non_error_statuses_have_no_reason(self):
        for status in ("delivered", "read", "accepted", "queued", "submitting"):
            assert user_reason_for(status) == NO_REASON
            act = build_frontend_action(status)
            assert act["user_reason"] == ""
            assert act["title"] and act["title"] != status


class TestFrontendActionShape:
    """프론트가 의존하는 응답 모양이 유지되는가 (하위호환)."""

    REQUIRED_KEYS = {
        "type",
        "user_reason",
        "title",
        "cause",
        "next_step",
        "description",
        "checklist",
        "cta",
        "severity",
    }

    def test_keys_present(self):
        act = build_frontend_action("failed_token")
        assert set(act) == self.REQUIRED_KEYS

    def test_legacy_signature_still_works(self):
        """기존 호출부(status 만 / status+subcode)가 깨지지 않아야 한다."""
        assert build_frontend_action("read")["severity"] == "success"
        assert build_frontend_action("failed_param", "2534025")["severity"] == "warning"

    def test_severity_unchanged_from_v3(self):
        """④ 결정 — 색은 이번 라운드에서 바꾸지 않는다 (status 기준 유지).

        사유 기준으로 옮기면 목록 배지(status 기준)와 모달 헤더가 어긋난다.
        예외는 `other` 하나뿐 — `test_other_is_warning` 참고.
        """
        expected = {
            "delivered": "success",
            "read": "success",
            "recovery_delivered": "success",
            "accepted": "info",
            "queued": "info",
            "rate_limited": "warning",
            "failed_token": "error",
            "failed_param": "error",
            "failed_window": "error",
            "failed_no_trace": "warning",
            "recovery_pending": "warning",
            "recovery_expired": "info",
        }
        for status, severity in expected.items():
            assert build_frontend_action(status)["severity"] == severity, status
        # 사유가 판정된 건너뜀은 예전대로 info.
        assert (
            build_frontend_action("skipped", error_message="campaign not active")["severity"]
            == "info"
        )

    def test_other_is_warning(self):
        """미분류(`other`) 만 warning — 파란 info 면 '정상 처리'로 읽힌다.

        ⚠️ error_message 가 비어 있으면 needle 에 안 걸려 `other` 로 떨어진다.
        즉 **사유를 못 붙인 건너뜀은 전부 warning** 이 된다(의도).
        """
        for message in ("", "처음 보는 문구"):
            act = build_frontend_action("skipped", error_message=message)
            assert act["user_reason"] == "other", message
            assert act["severity"] == "warning", message

    def test_no_unhandled_cta_actions(self):
        """프론트가 처리하는 action 만 내보낸다.

        `handleCtaClick` 은 reverify/retry/ig_reconnect 만 처리한다 — 모르는 action 을
        주면 **눌러도 아무 일 없는 버튼**이 생긴다. (enable_recovery 는 v3.2 부터 있던
        기존 항목이라 유지하되, 프론트 요청서에 핸들러 추가를 넣었다.)
        """
        allowed = {"reverify", "retry", "ig_reconnect", "enable_recovery"}
        for code, subcode, status in _catalog_combinations():
            cta = build_frontend_action(status, subcode, code)["cta"]
            if cta:
                assert cta["action"] in allowed, f"{status}/{code}/{subcode}: {cta}"

    def test_checklist_only_for_delivery_unconfirmed(self):
        assert build_frontend_action("failed_no_trace")["checklist"] == SELF_CHECK_CHECKLIST
        assert build_frontend_action("failed_token")["checklist"] is None

    def test_recovery_next_step_varies_by_stage(self):
        pending = build_frontend_action("recovery_pending")["next_step"]
        expired = build_frontend_action("recovery_expired")["next_step"]
        assert "안내 댓글을 남겨두었어요" in pending
        assert "종료되었어요" in expired
        assert pending != expired
