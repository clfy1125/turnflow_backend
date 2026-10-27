"""제로콜(대표번호) 웹훅 수신 — 판정·발송은 ``apps.sms.zerocall`` 이 한다."""

from __future__ import annotations

import logging

from django.http import Http404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import zerocall

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class ZeroCallWebhookView(APIView):
    """제로콜 → 백엔드. 즉시 200, 문자는 Celery 로."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []

    @extend_schema(
        tags=["문자(SMS)"],
        summary="제로콜 웹훅 (서버 전용)",
        description="""
## 목적
회사 대표번호(제로콜 070)로 전화가 오면 제로콜이 호출하는 웹훅입니다.
수신 이벤트를 받으면 **발신자 휴대폰으로 문의 안내 문자 1통**(고객문의 페이지·제휴 메일)을
네이버 SENS 로 보냅니다. 상담원 연결은 없습니다.

## ⚠️ 프론트엔드에서 호출하지 마세요
**제로콜 서버 → 백엔드** 전용입니다. 제로콜 스튜디오 → 개발자 설정 → 웹훅에
`CALL_RECEIVED`·`CALL_COMPLETED` 두 이벤트로 이 URL 을 등록합니다.

## 인증 (서명 없음 → 경로 시크릿)
제로콜 웹훅에는 서명 헤더가 없어 URL 경로의 `{secret}` 이 유일한 인증입니다
(`ZEROCALL_WEBHOOK_SECRET`). 틀리거나 설정이 비어 있으면 **404** — 기능이 꺼진 것과 구별되지 않게 합니다.

## 발송 규칙
| 규칙 | 내용 |
|---|---|
| 통화당 1통 | 같은 `callHistoryId` 의 수신·완료 이벤트와 재전송은 한 번만 처리 |
| 번호당 쿨다운 | 같은 번호는 `ZEROCALL_INQUIRY_COOLDOWN_HOURS`(기본 24h)에 1통 |
| 전역 상한 | 시간당 `ZEROCALL_INQUIRY_HOURLY_CAP`(30) · 일 `ZEROCALL_INQUIRY_DAILY_CAP`(100). 도달 시 중단 + Telegram |
| 대상 | 휴대폰 번호만(유선·070·발신번호 표시 제한은 건너뜀). 발신 통화의 완료 이벤트는 무시 |

## 응답
처리 대상 여부와 무관하게 항상 **200** `{"received": true}` (제로콜 재전송 방지). 시크릿 불일치만 404.
        """,
        request=None,
        examples=[
            OpenApiExample(
                "CALL_RECEIVED 요청 예시",
                request_only=True,
                value={
                    "eventType": "CALL_RECEIVED",
                    "timestamp": "2026-10-27T09:20:11.000Z",
                    "data": {
                        "callHistoryId": "9ceb05e5-1dd4-468b-84d7-0fb0538ff7a6",
                        "groupId": 1021,
                        "fromNumber": "01012345678",
                        "toNumber": "07052364516",
                        "receivedAt": "2026-10-27T09:20:11.000Z",
                    },
                },
            ),
            OpenApiExample(
                "curl",
                request_only=True,
                description=(
                    "curl -X POST https://turnflow-api.clfy.ai.kr/api/v1/sms/zerocall/webhook/"
                    "<secret>/ -H 'Content-Type: application/json' -d '{...}'"
                ),
                value={},
            ),
        ],
        responses={
            200: OpenApiResponse(
                description="수신 확인 (발송은 비동기)",
                examples=[OpenApiExample("OK", value={"received": True})],
            ),
            404: OpenApiResponse(description="시크릿 불일치 또는 기능 꺼짐"),
        },
    )
    def post(self, request, secret: str):
        if not zerocall.webhook_secret_matches(secret):
            raise Http404

        event = zerocall.parse_call_event(request.data)
        if event is None or not zerocall.claim_call(event.call_id):
            return Response({"received": True})

        from .tasks import send_phone_inquiry_guide

        send_phone_inquiry_guide.delay(event.from_number, event.call_id)
        logger.info("zerocall_webhook: %s call=%s queued", event.event_type, event.call_id)
        return Response({"received": True})
