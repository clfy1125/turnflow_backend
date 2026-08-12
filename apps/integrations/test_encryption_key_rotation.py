"""저장 필드 암호화 키 분리·회전 테스트 (감사 M-3/M-4).

## 이 테스트가 지키는 불변식

가장 중요한 건 **"기존 암호문이 계속 열린다"** 이다. 이게 깨지면 모든 고객의 IG 연동과
토스 빌링키가 한꺼번에 죽는다 — 되돌릴 수도 없다(복호화 불가는 복구가 없다).

그래서 순서가 이렇다:
1. 키를 안 넣으면 **종전과 100% 동일** (코드 배포와 키 투입을 분리할 수 있어야 한다)
2. 키를 넣어도 **옛 파생 키로 잠긴 값이 그대로 열린다**
3. 새로 저장한 값은 **새 키로 잠긴다** (실제로 옮겨가고 있는지)
4. 잘못된 키를 넣어도 **기존 값은 계속 열린다** (설정 실수가 장애로 번지지 않게)
"""

import base64
import hashlib

import pytest
from cryptography.fernet import Fernet, InvalidToken

from apps.integrations.encryption import TokenEncryption, _legacy_key

SECRET = "test-secret-key-for-encryption-rotation"
NEW_KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()

PLAIN = "IGQVJXa1b2c3d4e5f6-instagram-access-token"


def _legacy_ciphertext(secret: str, plaintext: str) -> str:
    """전용 키 도입 **이전** 방식으로 만든 암호문 (= prod 에 이미 쌓여 있는 형태)."""
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest)).encrypt(plaintext.encode()).decode()


@pytest.fixture(autouse=True)
def _fixed_secret(settings):
    settings.SECRET_KEY = SECRET
    settings.FIELD_ENCRYPTION_KEYS = ""
    yield


# ──────────────────────────────────────────────────────────────────────────────
# 1. 키 미설정 = 종전과 동일
# ──────────────────────────────────────────────────────────────────────────────


def test_without_new_key_behaviour_is_unchanged(settings):
    """키를 안 넣으면 파생 키만 쓴다 — 배포해도 아무것도 안 바뀐다."""
    token = TokenEncryption.encrypt(PLAIN)
    assert TokenEncryption.decrypt(token) == PLAIN

    # 예전 방식으로 만든 암호문도 그대로 열려야 한다
    assert TokenEncryption.decrypt(_legacy_ciphertext(SECRET, PLAIN)) == PLAIN

    # 실제로 파생 키로 잠겼는지 직접 확인
    assert Fernet(_legacy_key()).decrypt(token.encode()).decode() == PLAIN


# ──────────────────────────────────────────────────────────────────────────────
# 2. ★ 핵심 — 새 키를 넣어도 기존 암호문이 열린다
# ──────────────────────────────────────────────────────────────────────────────


def test_legacy_ciphertext_still_decrypts_after_key_is_added(settings):
    """이게 깨지면 전 고객의 IG 연동·빌링키가 죽는다."""
    legacy = _legacy_ciphertext(SECRET, PLAIN)

    settings.FIELD_ENCRYPTION_KEYS = NEW_KEY

    assert (
        TokenEncryption.decrypt(legacy) == PLAIN
    ), "새 키를 넣자 기존 암호문이 안 열린다 — 배포하면 전 고객 연동이 끊긴다"


def test_new_writes_use_the_new_key(settings):
    """새로 저장되는 값은 새 키로 잠겨야 한다 (실제로 옮겨가고 있는가)."""
    settings.FIELD_ENCRYPTION_KEYS = NEW_KEY

    token = TokenEncryption.encrypt(PLAIN)

    # 새 키로 열린다
    assert Fernet(NEW_KEY.encode()).decrypt(token.encode()).decode() == PLAIN
    # 옛 파생 키로는 안 열린다 = 정말 새 키를 썼다는 증거
    with pytest.raises(InvalidToken):
        Fernet(_legacy_key()).decrypt(token.encode())
    # 그래도 우리 API 로는 당연히 열린다
    assert TokenEncryption.decrypt(token) == PLAIN


def test_rotation_order_first_key_encrypts(settings):
    """'새키,옛키' 순서 — 첫 번째가 암호화용이고 나머지는 복호화 폴백이다."""
    settings.FIELD_ENCRYPTION_KEYS = f"{NEW_KEY},{OTHER_KEY}"

    old_ct = Fernet(OTHER_KEY.encode()).encrypt(PLAIN.encode()).decode()
    assert TokenEncryption.decrypt(old_ct) == PLAIN, "두 번째 키로 잠긴 값이 안 열린다"

    new_ct = TokenEncryption.encrypt(PLAIN)
    assert Fernet(NEW_KEY.encode()).decrypt(new_ct.encode()).decode() == PLAIN


# ──────────────────────────────────────────────────────────────────────────────
# 3. 설정 실수가 장애로 번지지 않게
# ──────────────────────────────────────────────────────────────────────────────


def test_malformed_key_is_ignored_and_data_still_readable(settings):
    """잘못된 키를 넣어도 기존 값은 계속 열려야 한다 (오타 하나로 서비스가 죽으면 안 된다)."""
    legacy = _legacy_ciphertext(SECRET, PLAIN)

    settings.FIELD_ENCRYPTION_KEYS = "not-a-valid-fernet-key"

    assert TokenEncryption.decrypt(legacy) == PLAIN
    assert TokenEncryption.decrypt(TokenEncryption.encrypt(PLAIN)) == PLAIN


def test_empty_values_are_passthrough(settings):
    settings.FIELD_ENCRYPTION_KEYS = NEW_KEY
    assert TokenEncryption.encrypt("") == ""
    assert TokenEncryption.decrypt("") == ""


def test_undecryptable_value_raises_rather_than_returning_garbage(settings):
    """어떤 키로도 못 열면 조용히 빈 값을 주는 대신 터져야 한다 — 조용한 손상이 더 위험하다."""
    settings.FIELD_ENCRYPTION_KEYS = NEW_KEY
    foreign = Fernet(OTHER_KEY.encode()).encrypt(PLAIN.encode()).decode()
    with pytest.raises(InvalidToken):
        TokenEncryption.decrypt(foreign)


# ──────────────────────────────────────────────────────────────────────────────
# 4. 실제 모델 경로 (디스크립터)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_model_descriptor_round_trips_with_new_key(settings):
    """IGAccountConnection 의 토큰 필드가 실제로 왕복하는지."""
    from apps.integrations.models import IGAccountConnection

    settings.FIELD_ENCRYPTION_KEYS = NEW_KEY

    conn = IGAccountConnection()
    conn.access_token = PLAIN
    assert conn._encrypted_access_token, "암호문이 저장되지 않았다"
    assert conn._encrypted_access_token != PLAIN, "평문이 그대로 들어갔다"
    assert conn.access_token == PLAIN
