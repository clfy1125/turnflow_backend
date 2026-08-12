"""저장 필드 암호화 (IG 액세스 토큰 · 토스 빌링키).

## 무엇이 문제였나 (감사 M-3 / M-4)

암호화 키를 ``sha256(SECRET_KEY)`` 로 파생해 쓰고 있었다. 두 가지가 걸린다:

1. ``SECRET_KEY`` 하나가 여러 용도를 겸한다 → 어느 한쪽이 새면 저장 데이터까지 열린다.
   (JWT 서명은 2026-07-14 RS256 전환으로 이미 분리됐고, 지금 남은 겸용이 이 암호화다)
2. **키를 바꿀 수가 없었다.** 바꾸는 순간 기존 암호문이 전부 복호화 불가가 되어
   모든 고객의 IG 연동과 빌링키가 한꺼번에 죽는다. 즉 "유출돼도 대응 수단이 없는" 상태.

## 어떻게 고쳤나 — MultiFernet

``FIELD_ENCRYPTION_KEY`` 라는 **전용 키**를 도입하되, 복호화는 **새 키와 옛 키 모두** 받는다.

    암호화: 항상 첫 번째 키(새 키)
    복호화: 첫 번째로 성공하는 키 (새 키 → 옛 키 순)

그래서 **대량 재암호화가 필요 없다.** 기존 행은 옛 키로 계속 열리고, 새로 저장되는 값부터
새 키로 잠긴다. 서비스 중단 0 · 고객 재로그인 0 · IG 연동 끊김 0.

되돌리기도 쉽다 — ``FIELD_ENCRYPTION_KEY`` 를 지우면 옛 동작 그대로다. 단, **그 사이 새 키로
저장된 값은 못 읽는다**(그 시점 이후 저장된 토큰만 해당). 그래서 키를 뺄 때는 롤백이 아니라
"새 키를 두 번째 자리로 내리는" 방식으로 가야 한다.

## 운영 메모

- ``FIELD_ENCRYPTION_KEY`` 미설정이면 **종전과 100% 동일**하게 동작한다(파생 키만 사용).
  키를 넣기 전까지 아무것도 바뀌지 않으므로 코드 배포와 키 투입을 분리할 수 있다.
- 키 생성: ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``
- 키를 실제로 회전할 때는 ``FIELD_ENCRYPTION_KEYS`` 에 ``새키,옛키`` 순으로 넣는다
  (쉼표 구분, 첫 번째가 암호화용).
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings

logger = logging.getLogger(__name__)


def _legacy_key() -> bytes:
    """``sha256(SECRET_KEY)`` 파생 키 — 기존 암호문을 계속 열기 위해 유지한다."""
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _configured_keys() -> list[bytes]:
    """settings 에 명시된 전용 키들을 순서대로 반환 (첫 번째 = 암호화용)."""
    keys: list[bytes] = []
    raw = getattr(settings, "FIELD_ENCRYPTION_KEYS", None) or getattr(
        settings, "FIELD_ENCRYPTION_KEY", ""
    )
    if not raw:
        return keys
    if isinstance(raw, str):
        candidates = [k.strip() for k in raw.split(",")]
    else:
        candidates = [str(k).strip() for k in raw]
    for k in candidates:
        if not k:
            continue
        try:
            Fernet(k.encode())  # 형식 검증 — 잘못된 키가 조용히 섞이면 안 된다
        except Exception:
            logger.error("FIELD_ENCRYPTION_KEY 형식이 올바르지 않아 무시했다 (길이=%d)", len(k))
            continue
        keys.append(k.encode())
    return keys


def _build_fernet() -> MultiFernet:
    """암호화 1순위 = 전용 키(있으면), 복호화 폴백 = 파생 키.

    캐시하지 않는다 — ``override_settings`` 를 쓰는 테스트와 키 교체 직후를 위해서다.
    Fernet 객체 생성은 값싸고(HMAC/AES 키 슬라이싱뿐) 요청당 1회 수준이라 무시할 만하다.
    """
    keys = _configured_keys()
    legacy = _legacy_key()
    if legacy not in keys:
        keys.append(legacy)
    return MultiFernet([Fernet(k) for k in keys])


class TokenEncryption:
    """토큰·빌링키 등 민감 문자열의 대칭 암호화.

    ⚠️ 평문을 로그로 남기지 말 것 — 예외 메시지에도 값을 담지 않는다.
    """

    @staticmethod
    def _get_fernet_key() -> bytes:
        """하위 호환용. 파생 키를 그대로 돌려준다(외부에서 참조하는 곳이 있을 수 있어 유지)."""
        return _legacy_key()

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        if not plaintext:
            return ""
        return _build_fernet().encrypt(plaintext.encode()).decode()

    @classmethod
    def decrypt(cls, encrypted: str) -> str:
        if not encrypted:
            return ""
        try:
            return _build_fernet().decrypt(encrypted.encode()).decode()
        except InvalidToken:
            # 어떤 키로도 못 열었다 = 키 설정 사고. 값은 절대 로그에 남기지 않는다.
            logger.error(
                "복호화 실패 — 설정된 키(%d개) 중 어느 것으로도 열리지 않는다. "
                "FIELD_ENCRYPTION_KEYS 에서 옛 키가 빠졌는지 확인할 것.",
                len(_configured_keys()) + 1,
            )
            raise


class EncryptedTextField:
    """암·복호를 투명하게 처리하는 필드 디스크립터.

    Usage:
        class MyModel(models.Model):
            _encrypted_token = models.TextField()
            token = EncryptedTextField('_encrypted_token')
    """

    def __init__(self, field_name):
        self.field_name = field_name

    def __get__(self, instance, owner):
        if instance is None:
            return self

        encrypted_value = getattr(instance, self.field_name)
        if not encrypted_value:
            return ""
        return TokenEncryption.decrypt(encrypted_value)

    def __set__(self, instance, value):
        if not value:
            encrypted_value = ""
        else:
            encrypted_value = TokenEncryption.encrypt(value)
        setattr(instance, self.field_name, encrypted_value)
