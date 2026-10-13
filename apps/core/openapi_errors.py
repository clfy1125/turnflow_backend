"""4xx 응답을 **기계가 읽을 수 있게** 만드는 헬퍼.

## 왜 필요한가 — 실측으로 걸린 문제 (2026-10-13)

지침 §7 은 "400/401/403/404/500 모두 문서화 + examples" 를 강제하고, 저장소 전반이
그렇게 쓰고 있다. 그런데 **실제 스키마에는 4xx 예시가 하나도 들어가지 않는다.**
dev 스키마 실측: ``examples_present=0``, ``no_content=827``.

원인은 drf-spectacular 의 동작이다 — ``OpenApiResponse(description=..., examples=[...])``
처럼 **``response=`` 를 주지 않으면** 그 응답에 `content` 자체가 생기지 않고, 예시는
붙을 자리가 없어 **조용히 버려진다.** 설명 텍스트만 남는다.

그래서 프론트(와 사내 API MCP)가 보는 것은 "400: 코드가 틀렸습니다" 같은 산문뿐이고,
**분기에 쓸 `code` 값과 응답 모양은 코드를 열어야만** 알 수 있었다. 에러 분기를
generic 으로 뭉개는 사고가 반복된 배경이기도 하다(CS #d34572b3 · #247995c5).

## 쓰는 법

```python
from apps.core.openapi_errors import error_response

responses={
    400: error_response(
        "번호 형식 오류",
        {"PHONE_INVALID": "올바른 휴대폰 번호가 아닙니다. (예: 010-1234-5678)"},
    ),
}
```

``code -> detail`` 매핑만 주면 **머신 키별로 예시 하나씩**을 자동으로 만든다.
프론트는 Swagger/MCP 에서 바로 분기할 `code` 목록을 볼 수 있다.

⚠️ 이 모듈은 **응답 모양을 선언할 뿐 만들지 않는다.** 실제 응답은 각 뷰의 `_error()` 가
   만들고, 그 모양은 ``apps/core/exceptions.custom_exception_handler`` 의 envelope 과
   같아야 한다. 둘이 어긋나면 문서가 거짓이 된다.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, inline_serializer
from rest_framework import serializers


def _envelope_serializer(name: str):
    """지침 §6 의 통일 에러 envelope + 이 저장소가 함께 내보내는 ``detail``/``code``.

    ⚠️ ``detail``/``code`` 는 **envelope 과 함께** 나간다(둘 중 하나가 아니다).
       DRF 예외 핸들러를 우회해 Response 를 직접 만드는 뷰들이 기존 프론트 호환을 위해
       두 포맷을 동시에 내기 때문이다 — 지우면 결제·인증 화면 문구가 한 번에 사라진다.
    """
    return inline_serializer(
        name=name,
        fields={
            "detail": serializers.CharField(help_text="사람이 읽을 문구. **화면에 그대로 쓰세요**"),
            "code": serializers.CharField(help_text="머신 키. **분기는 이 값으로 하세요**"),
            "success": serializers.BooleanField(default=False),
            "error": inline_serializer(
                name=f"{name}Envelope",
                fields={
                    "code": serializers.IntegerField(help_text="HTTP 상태 코드"),
                    "message": serializers.CharField(),
                    "details": serializers.DictField(),
                },
            ),
        },
    )


_counter = {"n": 0}


def error_response(
    description: str,
    codes: dict[str, str],
    *,
    http_status: int,
    extra: dict[str, dict] | None = None,
) -> OpenApiResponse:
    """머신 키별 예시가 **실제로 스키마에 들어가는** 4xx 응답.

    Args:
        description: 이 상태 코드가 언제 나오는지 한 줄.
        codes: ``{머신키: 사람이 읽을 detail}``. 키 하나당 예시 하나가 만들어진다.
        http_status: ``responses={400: ...}`` 의 그 숫자. 예시 안의 ``error.code`` 에
            들어간다. ⚠️ **실제 응답과 같아야 한다** — 틀린 예시는 없느니만 못하다.
        extra: ``{머신키: {추가 필드}}`` — ``retry_after``·``attempts_left`` 처럼
            그 코드에만 붙는 필드가 있을 때.
    """
    extra = extra or {}
    _counter["n"] += 1
    name = f"ErrorResponse{_counter['n']}"

    examples = []
    for code, detail in codes.items():
        value = {
            "detail": detail,
            "code": code,
            "success": False,
            "error": {"code": http_status, "message": detail, "details": {"code": code}},
        }
        value.update(extra.get(code, {}))
        examples.append(OpenApiExample(code, value=value, response_only=True))

    return OpenApiResponse(
        response=_envelope_serializer(name),
        description=description,
        examples=examples,
    )
