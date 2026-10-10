"""dev 전용 — 홈 테스트 계정 목록 조회 / 재시드 API.

프론트가 **스스로 되돌릴 수 있어야** 쓸모가 있다. 알림을 닫거나 버튼을 눌러 상태가
바뀌면 그 계정은 그 화면을 다시는 못 보여주는데, dev 스택은 백엔드 PC 의 도커라
프론트가 ``manage.py`` 를 돌릴 수 없다. 그래서 같은 시더를 HTTP 로도 연다.

⚠️ **게이트는 ``DEBUG`` 하나다** — 운영은 ``DEBUG=False`` 라 이 경로가 통째로 404 가 된다
   (:func:`_dev_only`). 인증을 요구하지 않는 이유는 A01(이메일 미인증)·I01(가입 직후)처럼
   **로그인 전 화면**을 되돌릴 때도 써야 하기 때문이다. 비밀번호는 어차피 고정 공개값이다.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import dev_seed
from .dev_seed_specs import ALL_CODES, SPECS

logger = logging.getLogger(__name__)

# 재시드는 계정당 수백 행을 지우고 다시 만든다 — 동시에 두 번 돌면 서로를 지운다.
_LOCK_KEY = "home:dev-seed:lock"
_LOCK_TTL = 300


def _is_prod_settings() -> bool:
    return "prod" in (getattr(settings, "SETTINGS_MODULE", "") or "")


def _dev_only():
    """운영에서는 존재 자체를 숨긴다 (404).

    ⚠️ 이 뷰는 **인증이 없다**. 게이트가 뚫리면 누구나 계정을 만들고 지울 수 있으므로
    ``DEBUG`` 하나에만 기대지 않는다 — 설정 모듈이 prod 면 ``DEBUG`` 가 어떤 값이든 거부한다
    (``seed_billing_test_accounts`` 와 같은 2중 잠금). 그 뒤에도 :func:`dev_seed.guard` 가
    한 번 더 막는다.
    """
    if settings.DEBUG and not _is_prod_settings():
        return None
    return Response(
        {
            "success": False,
            "error": {"code": 404, "message": "Not found.", "details": {}},
        },
        status=status.HTTP_404_NOT_FOUND,
    )


def _error(message, code=status.HTTP_400_BAD_REQUEST, details=None):
    """프로젝트 공통 에러 포맷(apps/core/exceptions.py)과 같은 모양."""
    return Response(
        {
            "success": False,
            "error": {"code": code, "message": message, "details": details or {}},
        },
        status=code,
    )


class DevTestAccountListView(APIView):
    """dev 테스트 계정 목록."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        summary="[dev] 홈 테스트 계정 목록",
        description=(
            "홈 화면 상태 확인용 dev 테스트 계정의 **코드·이메일·비밀번호·기대 상태**를 돌려줍니다.\n\n"
            "**사용 시나리오**\n"
            "- 프론트가 상태별 화면을 확인하기 전에 어떤 계정으로 로그인할지 고를 때\n"
            "- 재시드(`POST .../reseed/`)에 넣을 코드를 확인할 때\n\n"
            "**인증**: 필요 없습니다(로그인 전 화면도 확인 대상이라 열어 둡니다).\n\n"
            "**주의**\n"
            "- `DEBUG=True` 인 환경(dev)에서만 동작합니다. 운영에서는 **404** 입니다.\n"
            "- 여기 나오는 비밀번호는 고정 공개값이며 실사용자 계정과 무관합니다.\n"
            "- 이 목록은 **정의**이고, 실제 DB 에 계정이 있는지는 보장하지 않습니다 — "
            "한 번도 시드하지 않았다면 먼저 재시드를 호출하세요."
        ),
        responses={
            200: {
                "type": "object",
                "properties": {
                    "password": {"type": "string", "example": "Test1234!"},
                    "referral_code": {"type": "string", "example": "DEVHOME14"},
                    "accounts": {"type": "array", "items": {"type": "object"}},
                },
            },
            404: {"description": "운영 환경(DEBUG=False)"},
        },
        examples=[
            OpenApiExample(
                "요청 (fetch)",
                value="await fetch('https://dev-api.turnflow.link/api/v1/home/dev/test-accounts/')",
                request_only=True,
            ),
            OpenApiExample(
                "응답",
                value={
                    "password": "Test1234!",
                    "referral_code": "DEVHOME14",
                    "accounts": [
                        {
                            "code": "H01",
                            "title": "대표 화면 — 새 게시물 + 리포트 + 알림 센터",
                            "email": "home-h01@test.turnflow.link",
                        }
                    ],
                },
                response_only=True,
            ),
        ],
        tags=["Home"],
    )
    def get(self, request):
        blocked = _dev_only()
        if blocked:
            return blocked
        return Response(
            {
                "password": dev_seed.PASSWORD,
                "referral_code": dev_seed.REFERRAL_CODE,
                "referral_bonus_days": dev_seed.REFERRAL_BONUS_DAYS,
                "accounts": [
                    {
                        "code": code,
                        "title": title,
                        "email": dev_seed.SPECIAL_EMAILS.get(
                            code, f"home-{code.lower()}@{dev_seed.EMAIL_DOMAIN}"
                        ),
                    }
                    for code, title, _fn in SPECS
                ],
            }
        )


class DevTestAccountReseedView(APIView):
    """dev 테스트 계정 재시드."""

    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        summary="[dev] 홈 테스트 계정 재시드",
        description=(
            "지정한 테스트 계정을 **처음 상태로 되돌립니다**. 알림을 닫았거나 버튼을 눌러 "
            "상태가 바뀌었을 때 쓰세요.\n\n"
            "**동작**\n"
            "- 그 계정의 IG 연결·자동 DM·발송 로그·링크 페이지·리포트·닫은 알림·구독을 전부 지우고 다시 만듭니다.\n"
            "- 남은 일수·결제 유예일·「2시간 전 게시물」처럼 시간에 따라 바뀌는 값은 **호출 시점 기준**으로 다시 계산됩니다.\n"
            "- 계정이 아직 없으면 새로 만듭니다(멱등).\n\n"
            "**인증**: 필요 없습니다. `DEBUG=True` 인 dev 에서만 열립니다.\n\n"
            "**요청 필드**\n"
            "- `codes` (선택, string[]): 되돌릴 코드. 생략하면 **전부**(43개, 1분 이상 걸립니다).\n"
            "- `ig_user_id` (선택, string): A04 전용 — 「이미 다른 계정에 연결된」 IG 계정의 user_id.\n\n"
            "**주의**: 동시에 두 번 돌면 서로의 데이터를 지우므로 진행 중이면 **409** 를 돌려줍니다."
        ),
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "example": ["H01", "H02"],
                    },
                    "ig_user_id": {"type": "string", "example": "17841400000000000"},
                },
            }
        },
        responses={
            200: {
                "type": "object",
                "properties": {
                    "seeded": {"type": "integer", "example": 2},
                    "password": {"type": "string"},
                    "accounts": {"type": "array", "items": {"type": "object"}},
                },
            },
            400: {"description": "알 수 없는 코드"},
            404: {"description": "운영 환경(DEBUG=False)"},
            409: {"description": "다른 재시드가 진행 중"},
            500: {"description": "시드 중 오류"},
        },
        examples=[
            OpenApiExample(
                "요청 (fetch)",
                value={"codes": ["H01", "H02", "P02"]},
                request_only=True,
            ),
            OpenApiExample(
                "응답",
                value={
                    "seeded": 1,
                    "password": "Test1234!",
                    "accounts": [
                        {
                            "code": "H01",
                            "email": "home-h01@test.turnflow.link",
                            "password": "Test1234!",
                            "title": "대표 화면 — 새 게시물 + 리포트 + 알림 센터",
                            "summary": "recent_post_no_campaign(200, 2시간 전) + report_ready(230) …",
                            "notes": [],
                        }
                    ],
                },
                response_only=True,
            ),
        ],
        tags=["Home"],
    )
    def post(self, request):
        blocked = _dev_only()
        if blocked:
            return blocked

        codes = request.data.get("codes") or None
        if codes is not None:
            if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
                return _error("codes 는 문자열 배열이어야 합니다.")
            # H03a~d 만 소문자 꼬리를 쓴다 — 입력은 대소문자를 가리지 않게 받는다.
            canon = {c.upper(): c for c in ALL_CODES}
            codes = [canon.get(c.strip().upper(), c.strip()) for c in codes if c.strip()]

        if not cache.add(_LOCK_KEY, "1", _LOCK_TTL):
            return _error(
                "다른 재시드가 진행 중입니다. 잠시 후 다시 시도해 주세요.",
                status.HTTP_409_CONFLICT,
            )
        try:
            results = dev_seed.run(codes, ig_user_id=str(request.data.get("ig_user_id") or ""))
        except ValueError as exc:
            return _error(str(exc), details={"valid_codes": ALL_CODES})
        except Exception:  # noqa: BLE001 - dev 도구라 원인을 그대로 보여주는 편이 낫다
            logger.exception("home dev seed 실패 codes=%s", codes)
            return _error("시드 중 오류가 발생했습니다. 서버 로그를 확인하세요.", 500)
        finally:
            cache.delete(_LOCK_KEY)

        return Response(
            {
                "seeded": len(results),
                "password": dev_seed.PASSWORD,
                "referral_code": dev_seed.REFERRAL_CODE,
                "accounts": [r.as_dict() for r in results],
            }
        )
