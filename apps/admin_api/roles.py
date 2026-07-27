"""apps/admin_api/roles.py — 어드민 역할(RBAC) 단일 소스.

기존 권한 축은 ``is_staff`` 하나뿐이라 스태프 계정 하나로 회원 이메일·워크스페이스·
DM 로그·결제 이력이 전부 열렸다. 외주(에이전시)에게 **마케팅 대시보드만** 열어주기 위해
역할 축을 추가한다 (RBAC-1).

역할
- ``full``              — 기존 스태프. 동작 100% 그대로 (회귀 없음).
- ``marketing_viewer``  — 마케팅 대시보드 **조회 전용**. 그 외 ``/api/v1/admin/**`` 전부 403.

역할 부여는 **Django Group** 이름으로 한다 — 새 모델/필드/마이그레이션 없이 Django admin
화면에서 즉시 부여·회수할 수 있고, 회수가 다음 요청부터 바로 반영된다(JWT claim 미사용 —
토큰에 넣으면 만료까지 권한 회수가 지연됨).

차단은 **deny-by-default** — 화이트리스트에 없는 경로는 전부 거부한다
(:mod:`apps.admin_api.middleware`). 개별 뷰에 permission 을 다는 방식은 새 엔드포인트가
추가될 때 누락되어 조용히 열리므로 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import hmac
import re

from django.conf import settings

# ── 역할 ─────────────────────────────────────────────────────────────
ROLE_FULL = "full"
ROLE_MARKETING_VIEWER = "marketing_viewer"

# 역할 부여용 Django Group 이름 (Group.name == 역할 머신값).
ROLE_GROUPS = {ROLE_MARKETING_VIEWER}

# ── 백오피스 섹션 (프론트 사이드바 키와 1:1) ──────────────────────────
SECTION_MARKETING = "marketing"
ALL_SECTIONS = [
    SECTION_MARKETING,
    "operations",
    "users",
    "pages",
    "auto_dm",
    "support",
    "system",
]

ROLE_SECTIONS = {
    ROLE_FULL: ALL_SECTIONS,
    ROLE_MARKETING_VIEWER: [SECTION_MARKETING],
}

# ── 경로 화이트리스트 (제한 역할 전용) ────────────────────────────────
# 어드민 API 마운트 지점 (config/urls.py "api/v1/" + config/api_urls.py "admin/").
ADMIN_API_PREFIX = "/api/v1/admin/"
# Django admin(세션) — 마케팅 전용 역할은 is_staff=True 라 그냥 두면 로그인이 되므로 함께 차단.
DJANGO_ADMIN_PREFIX = "/admin/"

CHANNEL_LINKS_PATH = f"{ADMIN_API_PREFIX}marketing/channel-links/"

# {역할: {(METHOD, 절대경로), ...}} — 여기 없으면 거부. 경로는 끝슬래시 포함 정본.
ROLE_ALLOWED_ENDPOINTS: dict[str, set[tuple[str, str]]] = {
    ROLE_MARKETING_VIEWER: {
        ("GET", f"{ADMIN_API_PREFIX}me/"),
        ("GET", f"{ADMIN_API_PREFIX}dashboard/marketing/"),
        # RBAC-4-a: UTM 링크 생성은 캠페인 운영의 기본 작업이라 조회+생성까지 허용.
        # (직전 라운드 Q3 "전체 불허" 결정을 프론트 요청으로 철회)
        ("GET", CHANNEL_LINKS_PATH),
        ("POST", CHANNEL_LINKS_PATH),
    },
}

# {역할: [(METHOD, 컴파일된 경로 정규식), ...]} — pk 가 들어가는 상세 경로용.
# ⚠ 여기서 통과해도 **객체 소유자 검사는 뷰가** 한다(RBAC-4-b) — 미들웨어는 경로 게이트일 뿐.
ROLE_ALLOWED_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    ROLE_MARKETING_VIEWER: [
        # 자기가 만든 링크만 삭제 가능 (소유자 판정은 can_delete_channel_link)
        ("DELETE", re.compile(rf"^{re.escape(CHANNEL_LINKS_PATH)}\d+/$")),
    ],
}

# 프리플라이트/메타 — 화이트리스트 경로에 한해 함께 허용 (CORS 차단 방지).
_SAFE_META_METHODS = ("OPTIONS", "HEAD")

# 403 응답 사유 코드 (프론트가 401 세션만료와 구분해 다른 화면을 띄운다).
FORBIDDEN_CODE = "section_forbidden"
FORBIDDEN_MESSAGE = "이 계정에 허용되지 않은 어드민 영역입니다."
# RBAC-4-b: 경로는 허용됐지만 **남의 링크**라 거부 — 프론트가 다른 문구를 띄운다.
NOT_LINK_OWNER_CODE = "not_link_owner"
NOT_LINK_OWNER_MESSAGE = "다른 관리자가 만든 링크는 삭제할 수 없습니다."


def admin_role(user) -> str:
    """사용자의 어드민 역할 — 제한 Group 이 없으면 ``full``.

    슈퍼유저는 안전 밸브로 항상 ``full`` (역할 그룹이 실수로 붙어도 잠기지 않도록).
    비인증/비스태프는 이 함수의 관심사가 아니다(호출 측이 IsAdminUser 로 먼저 거른다).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return ROLE_FULL
    if getattr(user, "is_superuser", False):
        return ROLE_FULL
    names = set(user.groups.values_list("name", flat=True))
    for role in ROLE_GROUPS:
        if role in names:
            return role
    return ROLE_FULL


def resolve_admin_role(request) -> str:
    """요청의 어드민 역할 — 미들웨어가 캐싱해둔 값을 재사용, 없으면 직접 판정.

    미들웨어를 타지 않는 경로(직접 뷰 호출 테스트 등)에서도 뷰가 같은 답을 얻는다.
    """
    cached = getattr(request, "admin_role", None)
    if cached:
        return cached
    role = admin_role(getattr(request, "user", None))
    request.admin_role = role
    return role


def allowed_sections(role: str) -> list[str]:
    """역할 → 프론트 사이드바에 노출할 섹션 키 목록."""
    return list(ROLE_SECTIONS.get(role, ALL_SECTIONS))


def is_restricted(role: str) -> bool:
    """전 구간 접근이 아닌(=화이트리스트 방식) 역할인가."""
    return role in ROLE_ALLOWED_ENDPOINTS


def is_endpoint_allowed(role: str, method: str, path: str) -> bool:
    """deny-by-default — 제한 역할은 화이트리스트(정확 일치 또는 패턴)에 걸릴 때만 허용.

    객체 단위 권한(예: 남의 채널 링크 삭제)은 여기서 판정하지 않는다 — 경로만 통과시키고
    소유자 검사는 뷰가 한다(:func:`can_delete_channel_link`).
    """
    if not is_restricted(role):
        return True
    normalized = path if path.endswith("/") else f"{path}/"
    method = (method or "GET").upper()
    # 프리플라이트/HEAD 는 대응하는 GET 이 허용된 경로에서만 통과
    probe = "GET" if method in _SAFE_META_METHODS else method
    if (probe, normalized) in ROLE_ALLOWED_ENDPOINTS[role]:
        return True
    return any(
        probe == m and pattern.match(normalized)
        for m, pattern in ROLE_ALLOWED_PATTERNS.get(role, ())
    )


def can_delete_channel_link(role: str, user, link) -> bool:
    """이 요청자가 이 채널 링크를 삭제할 수 있는가 (RBAC-4-b/c 단일 소스).

    응답의 ``can_delete`` 플래그와 DELETE 게이트가 **반드시 같은 함수**를 써야 한다 —
    갈라지면 화면의 삭제 버튼과 실제 동작이 어긋난다.

    - full: 항상 True (기존 동작 유지)
    - marketing_viewer: 자기가 만든 링크만. ``created_by`` 가 null 인 레코드(생성자 계정
      삭제 → SET_NULL)는 소유자를 확인할 수 없으므로 **불가**.
    """
    if not is_restricted(role):
        return True
    owner_id = getattr(link, "created_by_id", None)
    return bool(owner_id) and owner_id == getattr(user, "id", None)


def user_ref(user_id) -> str:
    """회원 참조용 비가역 안정 식별자 — ``u_<hmac6>`` (RBAC-3).

    마스킹 응답에서 원본 ``user_id`` 대신 내려보내는 값. 같은 회원은 어느 리스트에서도
    같은 값이라 프론트가 중복을 인지할 수 있고, SECRET_KEY 기반 HMAC 이라 역산은 불가능하다.
    """
    if user_id in (None, ""):
        return ""
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"), str(user_id).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"u_{digest[:6]}"
