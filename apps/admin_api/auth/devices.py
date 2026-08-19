"""apps/admin_api/auth/devices.py — 기기 식별·신뢰 등록·회수.

기기 ID 는 클라이언트가 만든 UUID 이고 **비밀이 아니다**(웹 localStorage / 앱 SecureStore).
위조하면 남의 기기인 척할 수 있지만 그것만으로 얻는 것은 없다 — 비밀번호와 TOTP 를 통과해야
토큰이 나오고, 위조가 우회하는 것은 "신규 기기 이메일 승인" 한 단계뿐이다. 그 한 단계를
지키려고 User-Agent 지문 같은 보조 신호를 섞지 않는다: 브라우저 업데이트마다 기기가
바뀌어 이메일 코드가 쏟아지고, 정작 위조는 헤더 복사 한 줄로 뚫린다(프론트 Q5 답).

안드로이드 WebView 셸에서 앱 데이터가 지워지면 기기 ID 도 사라져 같은 폰이 신규 기기로
잡힌다. 이건 설계상 정상 동작이다 — 그때 필요한 것은 이메일 코드 한 번이다.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.utils import timezone

from apps.admin_api.models import AdminDevice

# 라벨은 사용자 입력이라 컬럼 상한에서 자른다(호출부가 아니라 여기서 — 단일 소스).
LABEL_MAX = 100


def normalize_device_id(raw: str | None) -> str:
    """클라이언트가 준 기기 ID 정리. 없거나 형식이 이상하면 **새로 발급**한다.

    형식 검증을 하는 이유는 위조 방지가 아니라(막을 수 없다) 길이 폭주·제어문자 유입을
    막기 위해서다. 컬럼이 64자라 그 이상은 애초에 들어오면 안 된다.
    """
    value = (raw or "").strip()
    if not value or len(value) > 64 or not all(c.isalnum() or c in "-_" for c in value):
        return str(uuid.uuid4())
    return value


def get_or_create_device(user, device_id: str, label: str = "") -> tuple[AdminDevice, bool]:
    """(기기 행, 신규 여부). 회수된 기기는 **되살리지 않고** 그 상태 그대로 돌려준다.

    되살리면 "해제" 버튼이 다음 로그인 한 번으로 무효가 된다 — 해제는 그 기기를 다시
    승인할 때까지 유지되어야 한다(재승인 = 이메일 코드 재통과, :func:`trust_device`).
    """
    device, created = AdminDevice.objects.get_or_create(
        user=user, device_id=device_id, defaults={"label": (label or "")[:LABEL_MAX]}
    )
    if not created and label and not device.label:
        device.label = label[:LABEL_MAX]
        device.save(update_fields=["label"])
    return device, created


def needs_email_verification(device: AdminDevice) -> bool:
    """이 기기가 이메일 코드를 받아야 하는가 — **판정 단일 소스**.

    로그인(2단계)과 인증앱 등록(setup/confirm)이 같은 답을 써야 한다. 한쪽만 끄면
    "로그인은 코드를 안 묻는데 등록은 묻는" 상태가 되고, 등록 화면에는 그 입력칸이
    안 떠서 재등록이 통째로 막힌다(실제로 그 사고가 한 번 있었다 — useAdminAuth.ts 주석).

    ``ADMIN_MFA_EMAIL_DEVICE_CODE_ENABLED`` 가 꺼져 있으면(기본) 항상 False —
    2단계는 인증앱 코드 하나로 끝난다. 근거는 settings/base.py 의 그 플래그 주석.
    """
    if not settings.ADMIN_MFA_EMAIL_DEVICE_CODE_ENABLED:
        return False
    return not device.is_trusted


def trust_device(device: AdminDevice, ip: str | None = None) -> None:
    """이메일 코드 통과 후 신뢰 등록. 해제됐던 기기는 여기서 되살아난다."""
    device.trusted_at = timezone.now()
    device.revoked_at = None
    device.last_seen_at = timezone.now()
    if ip:
        device.last_seen_ip = ip
    device.save(update_fields=["trusted_at", "revoked_at", "last_seen_at", "last_seen_ip"])


def revoke_device(device: AdminDevice) -> None:
    """신뢰 해제 — 다음 갱신부터 막히고, 다음 로그인은 이메일 코드를 다시 요구한다.

    이미 발급된 access 토큰(최대 2시간)까지 즉시 죽이지는 않는다. 즉시 끊어야 하는
    상황이라면 그건 기기 문제가 아니라 계정 문제이므로 ``is_active=False`` 를 쓴다.
    """
    device.revoked_at = timezone.now()
    device.save(update_fields=["revoked_at"])


def active_devices(user):
    """보안 화면에 보여줄 기기 목록 — 해제된 것은 뺀다."""
    return AdminDevice.objects.filter(user=user, revoked_at__isnull=True)


def find_live_device(user_id: int, device_id: str) -> AdminDevice | None:
    """갱신 시 회수 여부 확인용. 해제된 기기는 None 을 돌려 갱신을 막는다."""
    if not device_id:
        return None
    return AdminDevice.objects.filter(
        user_id=user_id, device_id=device_id, revoked_at__isnull=True
    ).first()
