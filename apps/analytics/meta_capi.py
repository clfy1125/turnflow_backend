"""Meta 전환 API(CAPI) 클라이언트 — 서버에서 Meta 로 전환 이벤트를 직접 보낸다.

왜 필요한가
-----------
지금은 고객 브라우저의 픽셀만 전환을 알린다. 그 신호는 광고차단기·iOS ATT·Safari ITP·
**인앱 브라우저에서 외부 브라우저로 탈출**하는 경로에서 자주 끊긴다. 우리 광고 트래픽은
실측 **77%가 인스타/페북 인앱 브라우저**라 손실이 평균보다 클 수 있다. 서버에서 같은
이벤트를 한 번 더 보내면 브라우저 신호가 끊겨도 Meta 가 전환을 인식한다.

⚠️ **이건 우리 대시보드 숫자를 바꾸지 않는다.** Meta 쪽 최적화·리포트 전용이다.
   (대행사 요청서의 '완료 기준 1번'이 이 둘을 섞어 적었다 — 대시보드 귀속은 별건이고
   이미 해결됐다.)

중복 제거 — 이게 제일 중요하다
------------------------------
같은 전환을 브라우저와 서버가 둘 다 보내므로, **`event_id` 가 양쪽에서 같아야** Meta 가
하나로 합친다. 어긋나면 전환이 **2배로 집계**되고 알고리즘이 잘못 학습한다.

프론트 배포본에서 확인한 규약 (2026-08-26) — 이 값을 바꾸면 안 된다:

=====================  ===========================  ==================
이벤트                  event_id                     프론트 발사 여부
=====================  ===========================  ==================
CompleteRegistration   ``str(user.id)``             ✅ 발사 중
Purchase               ``str(payment.id)`` (UUID)   ✅ 발사 중
StartTrial             ``str(subscription.id)``     ❌ **미발사** (프론트 요청 완료)
=====================  ===========================  ==================

개인정보
--------
- 이메일·전화·회원ID 는 **SHA-256 해시**로만 보낸다 (Meta 규격: 소문자·공백제거 후 해시).
- ``fbc``/``fbp``/IP/UA 는 **평문**이어야 한다 (해시하면 Meta 가 매칭에 못 쓴다).
- **원본 IP 는 우리 DB 에 저장하지 않는다** — 요청에서 뽑아 태스크 인자로만 넘기고
  전송 후 버린다 (LandingVisit 이 ip_hash 만 남기는 것과 같은 원칙).

안전장치
--------
- ``META_CAPI_ENABLED`` 가 False 이거나 토큰/데이터세트가 비면 **조용히 no-op**.
  토큰 없이 배포해도 아무 일도 일어나지 않는다.
- 어떤 예외도 호출측으로 던지지 않는다 — 광고 계측 실패가 가입·결제를 깨뜨리면 안 된다.
- 토큰은 **URL 쿼리가 아니라 요청 본문**(``access_token`` 필드)으로 보낸다. URL 에 실으면
  httpx 로거·프록시 액세스로그에 토큰이 남는다 (토스 빌링키와 같은 이유).
"""

from __future__ import annotations

import hashlib
import logging
import re

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

# 지원 이벤트 — 이 목록 밖의 이름은 보내지 않는다(오타로 엉뚱한 이벤트가 생기는 것 방지)
EVENT_COMPLETE_REGISTRATION = "CompleteRegistration"
EVENT_START_TRIAL = "StartTrial"
EVENT_PURCHASE = "Purchase"
SUPPORTED_EVENTS = frozenset({EVENT_COMPLETE_REGISTRATION, EVENT_START_TRIAL, EVENT_PURCHASE})

_NON_DIGIT = re.compile(r"\D")


def is_enabled() -> bool:
    """전송 가능 상태인가 (플래그 + 토큰 + 데이터세트 ID 가 모두 있어야 한다)."""
    return bool(
        getattr(settings, "META_CAPI_ENABLED", False)
        and getattr(settings, "META_CAPI_ACCESS_TOKEN", "")
        and getattr(settings, "META_CAPI_DATASET_ID", "")
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_email(email: str) -> str:
    """Meta 규격 이메일 해시 — 소문자 + 공백 제거 후 SHA-256.

    ⚠️ **정규화 뒤에** 빈 값을 판정한다. 앞에서 판정하면 공백만 든 문자열이 통과해
    ``sha256("")`` 라는 쓰레기 해시가 Meta 로 나가고, 그 값이 여러 사용자에게 동일해
    매칭 품질을 오히려 떨어뜨린다.
    """
    normalized = (email or "").strip().lower()
    return _sha256(normalized) if normalized else ""


def hash_phone(phone: str) -> str:
    """Meta 규격 전화 해시 — 숫자만 남긴 뒤 SHA-256.

    국가번호를 포함한 E.164 숫자열이 권장이지만, 우리가 보유한 값이 국내 형식(010…)이면
    그대로 숫자만 남긴다. 잘못된 정규화보다 원본 유지가 매칭률에 낫다.
    """
    digits = _NON_DIGIT.sub("", phone or "")
    return _sha256(digits) if digits else ""


def hash_external_id(value) -> str:
    """회원 ID 해시. Meta 는 external_id 도 해시를 권장한다.

    ``hash_email`` 과 같은 이유로 **정규화 뒤에** 빈 값을 판정한다.
    """
    if value is None:
        return ""
    normalized = str(value).strip()
    return _sha256(normalized) if normalized else ""


def build_user_data(
    *,
    email: str = "",
    phone: str = "",
    external_id=None,
    fbc: str = "",
    fbp: str = "",
    client_ip: str = "",
    client_user_agent: str = "",
) -> dict:
    """Meta ``user_data`` 블록. 빈 값은 **키 자체를 넣지 않는다**(빈 문자열을 보내면
    매칭 품질 점수가 떨어진다)."""
    data: dict = {}
    if em := hash_email(email):
        data["em"] = [em]
    if ph := hash_phone(phone):
        data["ph"] = [ph]
    if ext := hash_external_id(external_id):
        data["external_id"] = [ext]
    # ⚠️ 아래 넷은 평문 — 해시하면 Meta 가 못 쓴다
    if fbc:
        data["fbc"] = fbc
    if fbp:
        data["fbp"] = fbp
    if client_ip:
        data["client_ip_address"] = client_ip
    if client_user_agent:
        data["client_user_agent"] = client_user_agent
    return data


def build_event(
    *,
    event_name: str,
    event_id: str,
    event_time: int,
    user_data: dict,
    event_source_url: str = "",
    custom_data: dict | None = None,
) -> dict:
    """이벤트 1건. ``event_time`` 은 **유닉스 초**(밀리초 아님).

    ⚠️ Meta 는 ``event_time`` 이 7일보다 오래되면 **배치 전체를 거부**한다. 그래서 지연
    재시도(Celery retry backoff)가 길어져도 7일을 넘기지 않도록 태스크에서 상한을 둔다.
    """
    if event_name not in SUPPORTED_EVENTS:
        raise ValueError(f"지원하지 않는 이벤트: {event_name}")
    event: dict = {
        "event_name": event_name,
        "event_time": int(event_time),
        "event_id": str(event_id),
        "action_source": "website",
        "user_data": user_data,
    }
    if event_source_url:
        event["event_source_url"] = event_source_url
    if custom_data:
        event["custom_data"] = custom_data
    return event


def send_events(events: list[dict], *, test_event_code: str = "") -> dict:
    """이벤트 배치 전송. 반환: {"ok": bool, "status": int|None, "body": dict|str}.

    ⚠️ Meta 는 **배치 중 하나라도 잘못되면 배치 전체를 거부**한다. 그래서 호출부는
    이벤트를 1건씩 보낸다 — 한 건의 결함이 다른 전환까지 날리지 않게.

    예외를 던지지 않는다. 실패는 반환값으로만 알린다(호출 태스크가 재시도 판단).
    """
    if not events:
        return {"ok": True, "status": None, "body": "빈 배치"}
    if not is_enabled():
        logger.info("meta_capi 비활성 — 전송 생략 (events=%d)", len(events))
        return {"ok": True, "status": None, "body": "disabled"}

    version = getattr(settings, "META_CAPI_API_VERSION", "v23.0")
    dataset_id = settings.META_CAPI_DATASET_ID
    url = f"https://graph.facebook.com/{version}/{dataset_id}/events"

    payload: dict = {
        "data": events,
        # 토큰을 URL 이 아니라 본문에 싣는다 (로그 노출 방지)
        "access_token": settings.META_CAPI_ACCESS_TOKEN,
    }
    code = test_event_code or getattr(settings, "META_CAPI_TEST_EVENT_CODE", "")
    if code:
        # 테스트 코드가 있으면 [이벤트 테스트] 탭에만 뜨고 실집계에는 안 들어간다
        payload["test_event_code"] = code

    timeout = getattr(settings, "META_CAPI_TIMEOUT", 10.0)
    try:
        response = httpx.post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        logger.warning("meta_capi 전송 실패(네트워크): %s", exc)
        return {"ok": False, "status": None, "body": str(exc)}

    try:
        body = response.json()
    except ValueError:
        body = response.text[:500]

    if response.status_code >= 400:
        # 토큰이 본문에 있으므로 응답 본문만 남긴다 (요청 본문은 절대 로깅하지 않는다)
        logger.warning(
            "meta_capi 거부: status=%s body=%s events=%s",
            response.status_code,
            body,
            [e.get("event_name") for e in events],
        )
        return {"ok": False, "status": response.status_code, "body": body}

    logger.info(
        "meta_capi 전송 성공: events=%s test_code=%s received=%s",
        [e.get("event_name") for e in events],
        code or "-",
        (body or {}).get("events_received") if isinstance(body, dict) else None,
    )
    return {"ok": True, "status": response.status_code, "body": body}
