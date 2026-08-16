"""apps/admin_api/auth/totp.py — TOTP·백업코드 검증의 단일 소스.

JWT 로그인 흐름과 (Phase 3a 의) Django admin 세션 로그인이 **같은 함수**를 쓰게 하려고
검증 로직을 여기 한 곳에 둔다. 두 관문이 각자 판정하면 한쪽에만 재사용 방지가 빠지는
식으로 조용히 갈라진다.

## 왜 pyotp 인가
TOTP 계산 자체는 30줄이지만, 실제로 어려운 것은 그 주변이다 — 시간 드리프트 허용,
상수시간 비교, base32 패딩. 그건 검증된 구현을 쓴다. **우리가 직접 책임지는 것은
재사용 방지(replay)** 뿐이며, 그건 라이브러리가 해주지 않는다.

## 재사용 방지
TOTP 는 같은 30초 창 안에서 같은 코드가 계속 유효하다. 어깨너머로 본 코드, 로그·프록시에
남은 코드를 그 창 안에 다시 넣으면 통과한다. 그래서 성공한 스텝 번호를 저장하고
**그보다 크지 않은 스텝을 거부**해 코드를 1회용으로 만든다.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

import pyotp
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.admin_api.models import AdminBackupCode, AdminMFADevice

logger = logging.getLogger(__name__)

# TOTP 표준 파라미터 (인증앱 기본값과 일치해야 QR 스캔 한 번으로 끝난다).
TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
# 앞뒤 1스텝(±30초)까지 허용 — 폰 시계 오차와 입력 시간을 흡수한다. 2 이상은 재사용 창을
# 불필요하게 넓힌다.
TOTP_DRIFT_STEPS = 1

# 백업코드: base32 12자 = 60비트. 오프라인 대입이 불가능한 수준이라 sha256 으로 충분하고,
# 해시 인덱스 조회 1회로 검증한다(느린 해시를 10개 순회하면 로그인이 1초씩 늦어진다).
# 사람이 옮겨 적을 값이라 혼동 문자(0/O, 1/I)를 뺀 알파벳을 쓴다.
_BACKUP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
BACKUP_CODE_LENGTH = 12
BACKUP_CODE_GROUP = 4  # 표시용 하이픈 간격 — ABCD-EFGH-JKLM


# ── TOTP ──────────────────────────────────────────────────────────────────


def generate_secret() -> str:
    """새 TOTP 시드 (base32 32자)."""
    return pyotp.random_base32()


def otpauth_url(email: str, secret: str) -> str:
    """인증앱이 QR 로 읽는 ``otpauth://`` URL.

    issuer 를 서비스명으로 두면 앱 목록에 "TurnFlow Admin: me@..." 로 뜬다 — 일반 계정과
    구분되어야 관리자가 코드를 헷갈리지 않는다.
    """
    issuer = f"{settings.SERVICE_NAME} Admin"
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_STEP_SECONDS).provisioning_uri(
        name=email, issuer_name=issuer
    )


def qr_svg(otpauth: str) -> str:
    """otpauth URL → 인라인 SVG 문자열.

    프론트에 QR 라이브러리를 넣지 않기로 해서 서버가 그린다. SVG 팩토리는 Pillow 를
    요구하지 않으므로 이미지 의존성이 늘지 않는다.
    """
    import qrcode
    from qrcode.image.svg import SvgPathImage

    img = qrcode.make(otpauth, image_factory=SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")


def _current_step(at: float | None = None) -> int:
    return int((at if at is not None else time.time()) // TOTP_STEP_SECONDS)


def _verify_with_secret(device: AdminMFADevice, secret: str, code: str) -> bool:
    """주어진 시드로 코드 검증 + 재사용 방지. 성공하면 ``last_step`` 을 전진시킨다.

    드리프트 창(±1스텝) 안의 스텝을 하나씩 확인한다. **어느 스텝이 맞았는지 알아야 한다** —
    그 값을 ``last_step`` 에 넣어야 같은 코드의 재사용이 막힌다. "맞았다"만 알고 현재
    스텝을 저장하면 창 경계에서 한 번 더 통과한다.

    ``last_step`` 은 절대 시간 기반이라 시드가 바뀌어도(재등록) 그대로 유효하다 —
    초기화하면 재등록 직후에 옛 코드 재사용 창이 열린다.

    ``select_for_update`` 로 동시 요청을 직렬화한다. 없으면 같은 코드를 병렬로 두 번
    넣었을 때 둘 다 ``last_step`` 검사를 통과한다(TOCTOU).
    """
    code = (code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != TOTP_DIGITS:
        return False

    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_STEP_SECONDS)
    now_step = _current_step()

    with transaction.atomic():
        locked = AdminMFADevice.objects.select_for_update().get(pk=device.pk)
        for offset in range(-TOTP_DRIFT_STEPS, TOTP_DRIFT_STEPS + 1):
            step = now_step + offset
            expected = totp.at(step * TOTP_STEP_SECONDS)
            if not hmac.compare_digest(expected, code):
                continue
            if step <= locked.last_step:
                # 맞는 코드지만 이미 쓴 스텝 — 재사용이다. 실패와 같은 응답을 준다
                # (구분해서 알려주면 "이 코드는 유효했다"는 정보를 주게 된다).
                logger.warning(
                    "[admin-mfa] TOTP 재사용 차단 user=%s step=%s last=%s",
                    device.user_id,
                    step,
                    locked.last_step,
                )
                return False
            locked.last_step = step
            locked.save(update_fields=["last_step"])
            device.last_step = step
            return True
    return False


def verify_totp(device: AdminMFADevice, code: str) -> bool:
    """확인 완료된 시드로 검증 (로그인 경로)."""
    return _verify_with_secret(device, device.secret, code)


def verify_pending_totp(device: AdminMFADevice, code: str) -> bool:
    """등록 중인 시드로 검증 (등록 확인 경로) — QR 이 실제로 스캔됐는지 증명."""
    return _verify_with_secret(device, device.pending_secret, code)


# ── 백업코드 ──────────────────────────────────────────────────────────────


def _normalize_backup_code(raw: str) -> str:
    """표시용 하이픈·공백·대소문자를 없앤 정본 — 사용자가 어떻게 쳐도 같은 값이 되게."""
    return "".join(ch for ch in (raw or "").upper() if ch in _BACKUP_ALPHABET)


def hash_backup_code(raw: str) -> str:
    return hashlib.sha256(_normalize_backup_code(raw).encode("utf-8")).hexdigest()


def _format_backup_code(plain: str) -> str:
    """ABCDEFGHJKLM → ABCD-EFGH-JKLM (읽고 옮겨 적기 쉽게)."""
    return "-".join(
        plain[i : i + BACKUP_CODE_GROUP] for i in range(0, len(plain), BACKUP_CODE_GROUP)
    )


@transaction.atomic
def issue_backup_codes(user, count: int | None = None) -> list[str]:
    """백업코드 재발급 — **기존 코드는 전부 폐기**하고 새로 만든다.

    남은 것에 덧붙이면 "몇 개 남았나"가 의미를 잃고, 유출됐을 수 있는 옛 코드도 계속 산다.
    평문은 이 반환값이 유일한 노출 지점이다(서버는 해시만 갖는다).
    """
    count = count or settings.ADMIN_BACKUP_CODE_COUNT
    AdminBackupCode.objects.filter(user=user).delete()
    plains: list[str] = []
    rows = []
    for _ in range(count):
        plain = "".join(secrets.choice(_BACKUP_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
        plains.append(_format_backup_code(plain))
        rows.append(AdminBackupCode(user=user, code_hash=hash_backup_code(plain)))
    AdminBackupCode.objects.bulk_create(rows)
    return plains


def consume_backup_code(user, raw: str) -> bool:
    """백업코드 1개 소모. 이미 쓴 코드·없는 코드는 False.

    ``select_for_update`` + ``used_at__isnull`` 재확인으로 같은 코드의 동시 사용을 막는다.
    """
    normalized = _normalize_backup_code(raw)
    if len(normalized) != BACKUP_CODE_LENGTH:
        return False
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    with transaction.atomic():
        row = (
            AdminBackupCode.objects.select_for_update()
            .filter(user=user, code_hash=digest, used_at__isnull=True)
            .first()
        )
        if row is None:
            return False
        row.used_at = timezone.now()
        row.save(update_fields=["used_at"])
    logger.warning("[admin-mfa] 백업코드 사용 user=%s", user.pk)
    return True


def backup_codes_remaining(user) -> int:
    return AdminBackupCode.objects.filter(user=user, used_at__isnull=True).count()


# ── 등록 상태 ─────────────────────────────────────────────────────────────


def get_confirmed_device(user) -> AdminMFADevice | None:
    """확인까지 끝난 TOTP 등록만 반환 — 미확인(QR 만 받고 이탈)은 없는 것으로 본다."""
    device = AdminMFADevice.objects.filter(user=user).first()
    return device if device and device.is_confirmed else None


def start_enrollment(user) -> AdminMFADevice:
    """새 시드를 **pending 자리에** 만든다. 기존 확인된 시드는 건드리지 않는다.

    재등록 도중 이탈해도 종전 인증앱이 그대로 살아 있어야 한다 — 여기서 정본을 덮으면
    QR 만 띄우고 창을 닫은 순간 계정이 비밀번호 하나로 떨어진다.
    """
    device, _ = AdminMFADevice.objects.get_or_create(user=user)
    device.pending_secret = generate_secret()
    device.save(update_fields=["_encrypted_pending_secret"])
    return device


def complete_enrollment(device: AdminMFADevice) -> None:
    """pending 시드를 정본으로 승격 + 확인 시각 기록."""
    device.secret = device.pending_secret
    device.pending_secret = ""
    device.confirmed_at = timezone.now()
    device.save(update_fields=["_encrypted_secret", "_encrypted_pending_secret", "confirmed_at"])


def reset_mfa(user) -> None:
    """2단계 등록·백업코드 전부 삭제 (슈퍼유저 리셋 / 비상 커맨드).

    다음 로그인은 ``mfa_setup_required`` 로 떨어져 등록부터 다시 한다. 기기 신뢰는 남긴다 —
    분실한 것은 인증앱이지 기기가 아니고, 여기서 함께 지우면 복구 중인 사람에게 이메일
    코드까지 요구하게 된다.
    """
    AdminBackupCode.objects.filter(user=user).delete()
    AdminMFADevice.objects.filter(user=user).delete()


def verify_second_factor(user, *, code: str = "", backup_code: str = "") -> str | None:
    """2요소 검증의 **단일 진입점**. 성공하면 사용된 수단("totp"/"backup_code"), 실패면 None.

    호출부(로그인 뷰·향후 Django admin 폼)가 TOTP 와 백업코드 중 무엇을 받았는지 분기하지
    않도록 여기서 흡수한다 — 분기가 호출부에 있으면 새 관문이 생길 때마다 복제된다.
    """
    device = get_confirmed_device(user)
    if device and code and verify_totp(device, code):
        return "totp"
    if backup_code and consume_backup_code(user, backup_code):
        return "backup_code"
    return None
