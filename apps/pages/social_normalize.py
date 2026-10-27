"""social 블록 값 정규화 — 핸들을 full URL 로 편다.

왜 필요한가:
    렌더러(``SocialBlock.tsx``)는 ``block.data[<플랫폼 id>]`` 를 **그대로**
    ``<a href>`` 에 박는다. 그래서 ``"macaron3846"`` 이 저장되면 스킴이 없어
    브라우저가 상대경로로 읽고 ``https://turnflow.link/macaron3846`` 으로 간다
    (= 2026-10-28 CS #69ad2ebe "sns 링크 연결시 해당 sns로 안 넘어갑니다").

    이 계약은 ``social_registry`` 모듈 docstring 에 이미 적혀 있었고 외부
    임포터 3종(인포크·리틀리·링크트리)은 지켜 왔다. 지키지 않던 것은 **우리
    에디터와 AI 생성 경로**뿐이다. → 모든 블록 쓰기가 지나는 단 하나의
    초크포인트 ``validators.validate_block_data`` 에서 편다.

⚠️ 패턴이 없는 플랫폼은 **건드리지 않는다.**
    ``normalize_social_value`` 는 핸들 패턴이 없는 플랫폼(facebook, naver_* 등)에
    대해 ``'https://' + 핸들`` 로 폴백한다. 임포터는 그 플랫폼에서 항상 full URL
    을 받으므로 문제가 없지만, 사람이 손으로 치는 에디터에서는 ``https://john``
    같은 **호스트가 아닌 주소**가 만들어진다. 지금(상대경로)보다 나을 게 없고
    되돌리기만 어려워지므로, 결과가 진짜 URL 로 보일 때만 채택한다.
"""

from __future__ import annotations

from urllib.parse import urlparse

from apps.pages.services.external_importers.social_registry import SOCIAL_FIELD_IDS, handle_url

# 값이 URL 이 아닌 필드 — 렌더러가 mailto:/tel:/wa.me 를 직접 조립한다.
NON_URL_FIELDS: frozenset = frozenset({"email", "phone", "whatsapp"})

# 정규화 대상 = social 레지스트리 키 중 URL 계열.
NORMALIZABLE_FIELDS: frozenset = SOCIAL_FIELD_IDS - NON_URL_FIELDS

# 핸들이라는 개념 자체가 없는 필드 — 값은 언제나 주소다.
# 그래서 ``llldesign36.com`` 처럼 스킴만 빠진 값에 스킴을 채워도 모호하지 않다.
# (핸들 패턴이 있는 플랫폼에 이 규칙을 쓰면 점 든 핸들이 도메인으로 오인된다.)
URL_ONLY_FIELDS: frozenset = frozenset({"homepage"})

_ALREADY_LINKED_PREFIXES = ("http://", "https://", "mailto:", "tel:", "//")


def is_social_block(data: dict) -> bool:
    """social 블록인가. 블록 ``type`` 은 ``single_link`` 이고 서브타입으로 구분한다."""
    if not isinstance(data, dict):
        return False
    return data.get("_type") == "social" or data.get("is_social") is True


def _usable_url(value: str) -> bool:
    """정규화 결과를 채택해도 되는가 — 스킴이 있고 호스트에 점이 있어야 한다.

    ``https://john`` (핸들 패턴이 없는 플랫폼의 폴백)을 걸러내는 것이 목적이다.
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and "." in parsed.netloc


def normalize_social_value_safe(field_id: str, raw: str) -> str:
    """핸들 → full URL. 펼 수 없으면 **원본을 그대로** 돌려준다.

    핸들이냐 주소냐의 판별은 레지스트리(``normalize_social_value``)에 맡기지 않고
    여기서 한다. 레지스트리는 ``host.tld`` 모양이면 도메인으로 보는데, 인스타 핸들은
    점을 포함하는 경우가 흔해서(``misunny.daily``·``coolmood.zip``·``jian.42life``)
    **실제로 쓰이는 핸들 다수가 도메인으로 오인된다.** 그러면 링크가 404 에서
    "엉뚱한 도메인으로 확신 있게 가는" 상태가 돼 더 나빠진다.

    우리는 필드가 어느 플랫폼인지 이미 알고 있으므로 더 단순한 규칙이 가능하다:

    1. 스킴이 있으면 그대로 둔다.
    2. ``@`` 로 시작하면 **무조건 핸들**이다 (레지스트리는 ``@`` 를 떼고 나서
       도메인 판별을 해서 ``@o_os.ra`` 를 ``https://o_os.ra`` 로 만든다).
    3. ``/`` 가 있으면 경로가 붙은 주소다 → 스킴만 채운다.
    4. 그 외는 핸들로 본다. 패턴이 없는 플랫폼이면 건드리지 않는다.
    """
    value = (raw or "").strip()
    if not value:
        return raw
    if value.lower().startswith(_ALREADY_LINKED_PREFIXES):
        return raw
    if any(ch.isspace() for ch in value):
        # 공백이 든 값은 핸들도 주소도 아니다 — 카카오 공유 문구를 통째로 붙여넣은
        # 경우가 실제로 있었다("카카오톡 오픈채팅을 ... https://open.kakao.com/o/XXX").
        # 스킴만 붙이면 "보세요." 의 점이 호스트의 점으로 읽혀 검사를 통과해 버린다.
        # 무엇을 의도했는지 알 수 없으므로 추측하지 않는다.
        return raw

    if value.startswith("@"):
        candidate = handle_url(field_id, value[1:].strip("/"))
    elif "/" in value:
        candidate = "https://" + value.lstrip("/")
    elif field_id in URL_ONLY_FIELDS:
        candidate = "https://" + value
    else:
        candidate = handle_url(field_id, value)

    if candidate and candidate != value and _usable_url(candidate):
        return candidate
    return raw


def normalize_social_block_data(data: dict) -> list[tuple[str, str, str]]:
    """social 블록 ``data`` 를 **제자리에서** 고친다.

    Returns:
        실제로 바뀐 ``(플랫폼 id, 이전 값, 새 값)`` 목록. 백필 스크립트가
        무엇을 왜 고쳤는지 찍기 위해 쓴다.
    """
    if not is_social_block(data):
        return []

    changes: list[tuple[str, str, str]] = []
    for field_id in NORMALIZABLE_FIELDS:
        raw = data.get(field_id)
        if not isinstance(raw, str):
            continue
        new_value = normalize_social_value_safe(field_id, raw)
        if new_value != raw:
            data[field_id] = new_value
            changes.append((field_id, raw, new_value))
    return changes
