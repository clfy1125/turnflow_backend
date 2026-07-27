"""apps/admin_api/pii.py — 마케팅 대시보드 응답의 개인정보 마스킹 (RBAC-3).

**서버측 마스킹만이 실효**하다 — 프론트에서 ``***`` 로 그려도 원문이 JSON 에 남아 있으면
네트워크 탭에서 그대로 보인다. 그래서 제한 역할(marketing_viewer)의 응답은 직렬화가 끝난
dict 를 여기서 한 번 더 변형한 뒤 내보낸다.

적용 순서 주의: **캐시에는 원본을 저장**하고, 캐시에서 꺼낸 뒤 요청자 역할에 따라 마스킹한다
(마스킹본을 캐시에 넣으면 full 역할이 마스킹된 값을 받는다).

마스킹 대상은 마케팅 응답의 회원 행 6종 — 모두 ``{user_id, email, link}`` 축을 공유한다.
``ref``(HMAC 기반 비가역 참조값)는 **두 역할 모두** 채워 넣는다: 프론트 리스트 key 가
역할에 따라 사라지지 않도록 계약을 고정하기 위함.
"""

from __future__ import annotations

import copy

from apps.admin_api.roles import ROLE_MARKETING_VIEWER, user_ref

# 마스킹 이메일의 고정 별표 수 — 실제 길이만큼 찍으면 로컬파트 길이가 유출되어
# 다른 필드와 조합한 재식별 단서가 된다 (프론트 요청 그대로 3개 고정).
_EMAIL_STARS = "***"
_EMAIL_KEEP = 2  # 로컬파트 앞 N자 유지 (2자 미만이면 있는 만큼)

# (경로, ...) → 응답 dict 안의 회원 행 배열. 마지막 요소가 배열 키.
_ROW_PATHS = (
    ("upsell_candidates",),
    ("subscription_retention", "recent_cancellations"),
    ("customer_actions", "payment_failed"),
    ("customer_actions", "dormant"),
    ("customer_actions", "recent_churn"),
)


def mask_email(email: str) -> str:
    """``hongildong@gmail.com`` → ``ho***@gmail.com`` (도메인 유지, 별표 3개 고정).

    도메인은 개인 식별력이 낮은 반면 "기업 도메인 vs 무료 메일" 구분은 마케팅 분석에
    실제로 쓰이므로 유지한다 (프론트 Q1 기본 가정과 동일).
    """
    if not email:
        return ""
    local, sep, domain = email.partition("@")
    head = local[:_EMAIL_KEEP] if len(local) >= _EMAIL_KEEP else local[:1]
    if not sep:  # 이메일 형태가 아니면 통째로 가린다
        return f"{head}{_EMAIL_STARS}"
    return f"{head}{_EMAIL_STARS}@{domain}"


def _rows_at(data: dict, path: tuple) -> list:
    node = data
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return []
    rows = node.get(path[-1]) if isinstance(node, dict) else None
    return rows if isinstance(rows, list) else []


def _iter_member_rows(data: dict):
    """마케팅 응답 안의 '회원 1명 = 1행' 배열을 전부 순회 (PII 6곳)."""
    for path in _ROW_PATHS:
        yield from _rows_at(data, path)
    for segment in _rows_at(data, ("onboarding_dropoffs", "segments")):
        if isinstance(segment, dict):
            samples = segment.get("samples")
            if isinstance(samples, list):
                yield from samples


def apply_pii_policy(data: dict, *, role: str) -> dict:
    """역할에 따라 마케팅 응답의 PII 를 처리하고 ``pii_masked`` 를 세팅해 반환.

    - 공통: 회원 행에 ``ref``(비가역 안정 참조값) 주입.
    - marketing_viewer: ``email`` 마스킹 · ``user_id=None`` · ``link`` 비움 ·
      ``channels.referral_codes[].description``(제휴 내부 메모) 제거.

    캐시 오염을 막기 위해 마스킹 시에는 깊은 복사본을 만든다.
    """
    mask = role == ROLE_MARKETING_VIEWER
    out = copy.deepcopy(data) if mask else data

    for row in _iter_member_rows(out):
        if not isinstance(row, dict):
            continue
        row["ref"] = user_ref(row.get("user_id"))
        if mask:
            row["email"] = mask_email(row.get("email") or "")
            row["user_id"] = None
            row["link"] = {"page": None, "params": {}}

    if mask:
        # 제휴 내부 메모는 PII 는 아니지만 제휴 계약 정보 — 외주에는 코드만 노출 (Q2).
        for code_row in _rows_at(out, ("channels", "referral_codes")):
            if isinstance(code_row, dict):
                code_row["description"] = ""

    out["pii_masked"] = mask
    return out
