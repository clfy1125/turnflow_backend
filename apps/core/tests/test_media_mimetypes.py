"""`.webp` MIME 등록 회귀 방어.

django-storages 는 업로드 시 ``mimetypes.guess_type(name)`` 으로 Content-Type 을 정하고
못 맞히면 ``application/octet-stream`` 으로 폴백한다. python:3.11-slim 이미지에는
``/etc/mime.types`` 가 없고 파이썬 내장표에도 ``.webp`` 가 없어서, settings 에서
``mimetypes.add_type`` 을 하지 않으면 **R2 의 webp 가 전부 깨진 타입으로 나간다.**

2026-09-22 실측: prod R2 의 ``.webp`` 1,947개가 전부 ``application/octet-stream`` 이었다.
``.jpg``·``.png`` 는 내장표에 있어 멀쩡했기 때문에 **webp 만 골라서** 깨졌고 오래 안 보였다.

이 테스트가 깨지면 settings 의 등록이 사라진 것이다 — 지우지 말고 등록을 되살릴 것.
"""

import mimetypes


def test_webp_is_registered():
    """`.webp` → `image/webp`. 이게 None 이면 업로드가 octet-stream 으로 저장된다."""
    assert mimetypes.guess_type("x.webp")[0] == "image/webp"


def test_avif_is_registered():
    assert mimetypes.guess_type("x.avif")[0] == "image/avif"


def test_builtin_types_still_work():
    """내장표에 원래 있던 것들이 등록 과정에서 망가지지 않았는지."""
    assert mimetypes.guess_type("x.png")[0] == "image/png"
    assert mimetypes.guess_type("x.jpg")[0] == "image/jpeg"


def test_storage_backend_would_send_image_webp():
    """django-storages 가 실제로 참조하는 경로를 그대로 흉내 낸다.

    백엔드 구현이 바뀌어도 '우리가 고친 그 함수'를 보고 있는지 확인하기 위해
    ``guess_type`` 을 직접 호출하는 대신 폴백 로직까지 재현한다.
    """
    guessed, _ = mimetypes.guess_type("pages/2026/09/abc123.webp")
    content_type = guessed or "application/octet-stream"
    assert content_type == "image/webp"
