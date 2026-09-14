"""팝업 노출 상태 — ``GET/PATCH /api/v1/auth/me/popup-state/``.

기기를 바꿔도 "이 팝업 몇 번 봤나"가 유지돼야 노출 규칙(최대 3회 등)이 실제로 지켜진다.
브라우저 저장소만 쓰면 기기 수만큼 곱해진다 — 대행사 요청서 §06 의 지적.

⚠️ 경로 주의: 프론트 요청서는 ``/api/v1/users/me/popup-state/`` 를 제안했으나, 이 저장소의
   "나" 관련 엔드포인트는 전부 ``/api/v1/auth/me/`` 아래에 있다(``auth/me/``,
   ``auth/me/delete/``). ``users/`` 라는 새 prefix 를 하나 더 만들면 같은 리소스가 두
   위치에 생긴다. 프론트는 상수 한 곳만 바꾸면 된다고 했으므로 기존 규칙을 따랐다.
"""

from __future__ import annotations

import json
import logging

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

# 한 사용자의 팝업 상태 전체 크기 상한 (직렬화 바이트). 팝업 4종 × 카운터 몇 개면 1KB 미만이다.
POPUP_STATE_MAX_BYTES = 8192
# 최상위 팝업 키 개수 상한 — 프론트 버그로 키가 무한히 늘어나는 것만 막는다.
POPUP_STATE_MAX_KEYS = 32
POPUP_KEY_MAX_LENGTH = 40


class PopupStatePatchSerializer(serializers.Serializer):
    """PATCH 바디 = 팝업 키 → 상태 객체의 **부분** 맵.

    ⚠️ 필드를 고정하지 않는다. 팝업이 늘어날 때마다 백엔드 배포를 기다리게 하면
    프론트가 결국 localStorage 로 되돌아가고, 그러면 기기별 초기화 문제가 그대로 남는다.
    """

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({"detail": "본문은 객체여야 합니다."})
        if len(data) > POPUP_STATE_MAX_KEYS:
            raise serializers.ValidationError(
                {"detail": f"팝업 키는 최대 {POPUP_STATE_MAX_KEYS}개까지 보낼 수 있습니다."}
            )
        cleaned: dict = {}
        for key, value in data.items():
            if not isinstance(key, str) or not key or len(key) > POPUP_KEY_MAX_LENGTH:
                raise serializers.ValidationError(
                    {"detail": f"팝업 키는 1~{POPUP_KEY_MAX_LENGTH}자 문자열이어야 합니다."}
                )
            if value is not None and not isinstance(value, dict):
                raise serializers.ValidationError(
                    {"detail": f"'{key}' 의 값은 객체이거나 null(삭제)이어야 합니다."}
                )
            cleaned[key] = value
        return cleaned


class PopupStateView(APIView):
    """내 팝업 노출 상태 조회/갱신. 병합은 **최상위 키 단위**(shallow merge)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["authentication"],
        summary="팝업 노출 상태 조회",
        description="""
## 개요
성장 팝업(프로 체험 유도 등)의 **노출 이력을 서버에서** 읽습니다. 팝업별로 몇 번
보여줬는지·언제 닫았는지·전환됐는지를 담습니다.

## 사용 시나리오
- 앱 진입 시 1회 호출해 "이 팝업을 지금 띄워도 되는가"를 판단
- 브라우저 저장소(`localStorage`)만 쓰면 **기기를 바꿀 때 초기화**되어, '최대 3회' 같은
  노출 규칙이 기기 수만큼 곱해집니다. 그 문제를 없애기 위한 엔드포인트입니다.

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직
- 값은 **프론트가 정의하는 자유 JSON** 입니다. 서버는 키 이름이나 내부 필드를 강제하지
  않습니다 — 팝업이 추가될 때마다 백엔드 배포를 기다리게 하지 않기 위해서입니다.
- 한 번도 저장한 적이 없으면 빈 객체 `{}` 를 돌려줍니다.
- **노출 규칙 판정은 서버가 하지 않습니다.** 저장소 역할만 합니다.
  (팝업을 띄울지 말지는 프론트 `popupState.ts` 의 몫)

## 응답
```json
{
  "popup_state": {
    "signup":      {"shows": 2, "dismisses": 1, "lastShownAt": "2026-09-11T02:10:00Z", "converted": false},
    "pro_trial":   {"shows": 1, "dismisses": 0, "lastShownAt": "2026-09-12T01:00:00Z", "converted": true},
    "trial_top_bar": {"shows": 5}
  }
}
```

## 주의사항
- 이 값은 **현재 상태**만 담습니다. 분석용 이벤트(노출/클릭/닫기 각각의 시점)는
  `POST /api/v1/track/funnel-event/` 로 따로 보내세요 — 그쪽이 append-only 기록입니다.
- 전체 크기 상한 8KB, 최상위 키 32개.
        """,
        responses={
            200: OpenApiResponse(
                description="조회 성공",
                examples=[
                    OpenApiExample(
                        "저장된 상태",
                        value={
                            "popup_state": {
                                "pro_trial": {
                                    "shows": 2,
                                    "dismisses": 1,
                                    "lastShownAt": "2026-09-11T02:10:00Z",
                                    "lastDismissedAt": "2026-09-11T02:10:20Z",
                                    "converted": False,
                                }
                            }
                        },
                    ),
                    OpenApiExample("한 번도 저장 안 함", value={"popup_state": {}}),
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def get(self, request):
        return Response({"popup_state": request.user.popup_state or {}})

    @extend_schema(
        tags=["authentication"],
        summary="팝업 노출 상태 갱신",
        description="""
## 개요
팝업 노출 상태를 **부분 갱신**합니다. 보낸 최상위 키만 교체되고 나머지는 그대로 남습니다.

## 사용 시나리오
- 팝업을 띄운 직후: `{"pro_trial": {"shows": 3, "lastShownAt": "..."}}`
- 사용자가 닫았을 때: `{"pro_trial": {"shows": 3, "dismisses": 1, "lastDismissedAt": "..."}}`
- 전환됐을 때: `{"pro_trial": {"converted": true}}`

## 인증
`Authorization: Bearer <access_token>` 필수. 미인증 401.

## 비즈니스 로직 — 병합 규칙 (중요)
병합은 **최상위 키 단위(shallow)** 입니다. 한 팝업의 객체를 보내면 그 팝업의 상태가
**통째로 교체**됩니다. 내부 필드를 하나만 보내면 나머지 필드는 사라집니다.

```
저장된 값: {"pro_trial": {"shows": 2, "dismisses": 1}}
PATCH     {"pro_trial": {"converted": true}}
결과      {"pro_trial": {"converted": true}}        ← shows/dismisses 가 사라짐
```

→ 프론트는 **읽은 객체를 펼쳐서 통째로 다시 보내세요**:
`{...prev.pro_trial, converted: true}`.
(깊은 병합을 하지 않는 이유: 서버가 필드 의미를 모르는 상태에서 깊게 병합하면
"카운터를 0으로 되돌리는" 의도적인 리셋을 표현할 방법이 사라집니다.)

키를 **삭제**하려면 값으로 `null` 을 보내세요: `{"pro_trial": null}`.

## 요청 바디
팝업 키 → 상태 객체(또는 `null`)의 맵. 키 이름과 내부 필드는 프론트가 정의합니다.

| 제한 | 값 |
|------|-----|
| 전체 크기 | 8KB (초과 시 400) |
| 최상위 키 개수 | 32개 |
| 키 길이 | 1~40자 |

## 응답
갱신 후의 **전체** 상태를 돌려줍니다 (`GET` 과 같은 모양).

## 주의사항
- 응답의 `popup_state` 를 그대로 클라이언트 캐시에 반영하세요. 다른 탭/기기가 먼저
  갱신했을 수 있어, 보낸 값과 결과가 다를 수 있습니다.
- 동시 요청의 마지막 쓰기가 이깁니다(last-write-wins). 카운터 정확도가 1 틀리는 것은
  노출 규칙에 영향이 없어 락을 걸지 않습니다.

## 사용 예시
```javascript
const res = await fetch('/api/v1/auth/me/popup-state/', {
  method: 'PATCH',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ pro_trial: { ...prev.pro_trial, shows: (prev.pro_trial?.shows ?? 0) + 1,
                                      lastShownAt: new Date().toISOString() } }),
});
const { popup_state } = await res.json();
```
        """,
        request=PopupStatePatchSerializer,
        examples=[
            OpenApiExample(
                "노출 1회 기록",
                request_only=True,
                value={
                    "pro_trial": {
                        "shows": 3,
                        "dismisses": 1,
                        "lastShownAt": "2026-09-12T01:00:00Z",
                        "converted": False,
                    }
                },
            ),
            OpenApiExample("키 삭제", request_only=True, value={"pro_trial": None}),
        ],
        responses={
            200: OpenApiResponse(
                description="갱신 후 전체 상태",
                examples=[
                    OpenApiExample(
                        "갱신됨",
                        value={
                            "popup_state": {
                                "pro_trial": {
                                    "shows": 3,
                                    "dismisses": 1,
                                    "lastShownAt": "2026-09-12T01:00:00Z",
                                    "converted": False,
                                },
                                "signup": {"shows": 1},
                            }
                        },
                    )
                ],
            ),
            400: OpenApiResponse(
                description="크기/키 제한 위반 또는 본문이 객체가 아님",
                examples=[
                    OpenApiExample(
                        "크기 초과",
                        value={
                            "success": False,
                            "error": {
                                "code": 400,
                                "message": "잘못된 요청입니다.",
                                "details": {"detail": "팝업 상태가 너무 큽니다 (최대 8192 bytes)."},
                            },
                        },
                    )
                ],
            ),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="해당 없음"),
            404: OpenApiResponse(description="해당 없음"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def patch(self, request):
        serializer = PopupStatePatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        patch = serializer.validated_data

        user = request.user
        merged = dict(user.popup_state or {})
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value

        if (
            len(json.dumps(merged, ensure_ascii=False, default=str).encode())
            > POPUP_STATE_MAX_BYTES
        ):
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": status.HTTP_400_BAD_REQUEST,
                        "message": "잘못된 요청입니다.",
                        "details": {
                            "detail": f"팝업 상태가 너무 큽니다 (최대 {POPUP_STATE_MAX_BYTES} bytes)."
                        },
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.popup_state = merged
        user.save(update_fields=["popup_state"])
        return Response({"popup_state": merged})
