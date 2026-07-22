"""
랜딩 방문 트래킹 공개 엔드포인트.

패턴 소스: apps/pages/views.py PageViewRecordView — AllowAny + authentication_classes=[]
(JWT 파싱/Session CSRF 모두 스킵) + 204 응답 + hash_ip/get_country 재사용.

**silent-204 원칙**: 이 엔드포인트는 랜딩 페이지의 fire-and-forget 비콘이다.
어떤 실패(잘못된 페이로드·봇·캡 초과·DB 오류)도 방문자에게 에러를 노출하지 않고
204 로 답한다. 유일한 비-204 는 스로틀 429 (DRF 생성) 뿐이다.
"""

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.pages.stats import get_country, hash_ip

from .channels import classify_ua, derive_channel
from .models import CancellationEvent, CheckoutEvent, LandingVisit, UAClass
from .serializers import CancellationEventSerializer, CheckoutEventSerializer, TrackVisitSerializer

logger = logging.getLogger(__name__)

# 동일 fingerprint 재전송 무시 창(초) — SPA 재마운트/새로고침 burst 흡수
_DEDUP_TTL_SECONDS = 30 * 60


class TrackVisitView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []  # JWT 토큰 파싱 불필요 + SessionAuth CSRF 미적용
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "track_visit"

    @extend_schema(
        tags=["analytics"],
        summary="랜딩 방문 기록",
        description="""
## 개요
랜딩 사이트(turnflow.link)의 트래킹 스니펫이 **브라우저 세션당 1회** 자동 호출하는
방문 기록 엔드포인트입니다. 방문자 UUID·UTM 파라미터·리퍼러를 받아 유입 채널을
서버에서 파생(`meta_ads`, `instagram_organic`, `search_organic` 등)해 저장하며,
이 데이터가 마케팅 대시보드의 **방문자 수 / 채널별 방문→가입 전환율** 의 원천이 됩니다.

## 사용 시나리오
- 랜딩 페이지가 로드될 때 트래킹 스니펫이 `fetch(..., { keepalive: true })` 로 1회 전송
- 상세 통합 코드는 `SIGNUP_ATTRIBUTION_FRONTEND.md` 참고 (스니펫 전문 포함)
- 서비스 앱(로그인 후 화면)에서는 호출하지 않습니다 — 랜딩 전용

## 인증
**불필요** — JWT 토큰 없이 누구나 호출 가능합니다 (공개 비콘).

## 비즈니스 로직 (silent-204)
이 엔드포인트는 fire-and-forget 비콘이라 **모든 실패 경로가 204** 입니다:
1. 봇 User-Agent(`bot|crawler|spider|headless|curl` 등) → 기록 없이 204
2. 페이로드 검증 실패(visitor_id 누락/비UUID, 길이 초과) → 기록 없이 204
3. 방문자(visitor_id)당 시간당 기록 상한(기본 6회) 초과 → 기록 없이 204
4. 동일 fingerprint(visitor_id+utm+경로) 30분 내 재전송 → 기록 없이 204 (burst dedup)
5. 정상 → `LandingVisit` 1행 기록 후 204

IP 는 **SHA-256 해시만** 저장하고 원본은 저장하지 않습니다. 국가는 `CF-IPCountry`
헤더(Cloudflare) 우선으로 판별합니다.

## 요청 바디 필드
| 필드 | 필수 | 타입 | 설명 |
|------|:----:|------|------|
| `visitor_id` | ✅ | uuid | localStorage `tf_vid` (클라이언트 생성, 영구) |
| `utm_source` | 선택 | string(≤100) | 광고/캠페인 소스 (예: `meta`, `google`) |
| `utm_medium` | 선택 | string(≤100) | 매체 (예: `cpc`, `influencer`) |
| `utm_campaign` | 선택 | string(≤150) | 캠페인명 |
| `utm_content` | 선택 | string(≤150) | 소재 구분 |
| `referrer` | 선택 | string(≤500) | `document.referrer` (외부 리퍼러만) |
| `landing_path` | 선택 | string(≤300) | `location.pathname` (기본 `/`) |

## 주의사항
- 응답은 항상 바디 없는 204 — 프론트는 응답을 검사할 필요가 없고, 실패해도
  방문자 UX 에 어떤 에러도 표출하면 안 됩니다 (`.catch(() => {})`).
- IP 당 스로틀(기본 120/hour)이 있으나 한국 모바일 CGNAT 특성상 느슨하게 설정 —
  실질 어뷰즈 방어는 visitor_id 당 시간당 캡(6회)이 담당합니다.
- CORS: 랜딩 오리진이 백엔드 `CORS_ALLOWED_ORIGINS` 환경변수에 등록돼 있어야 합니다.

## 사용 예시
```bash
curl -X POST https://api.turnflow.link/api/v1/track/visit/ \\
  -H "Content-Type: application/json" \\
  -d '{
    "visitor_id": "3f1c2b74-9a1e-4f7b-8f52-1d2c3e4a5b6c",
    "utm_source": "meta",
    "utm_medium": "cpc",
    "utm_campaign": "launch_2026_07",
    "referrer": "",
    "landing_path": "/"
  }'
# → HTTP 204 (바디 없음)
```
""",
        request=TrackVisitSerializer,
        examples=[
            OpenApiExample(
                "광고 유입 (utm 있음)",
                request_only=True,
                value={
                    "visitor_id": "3f1c2b74-9a1e-4f7b-8f52-1d2c3e4a5b6c",
                    "utm_source": "meta",
                    "utm_medium": "cpc",
                    "utm_campaign": "launch_2026_07",
                    "utm_content": "video_a",
                    "referrer": "",
                    "landing_path": "/",
                },
            ),
            OpenApiExample(
                "오가닉 유입 (리퍼러만)",
                request_only=True,
                value={
                    "visitor_id": "3f1c2b74-9a1e-4f7b-8f52-1d2c3e4a5b6c",
                    "referrer": "https://l.instagram.com/",
                    "landing_path": "/pricing",
                },
            ),
            OpenApiExample(
                "직접 방문",
                request_only=True,
                value={"visitor_id": "3f1c2b74-9a1e-4f7b-8f52-1d2c3e4a5b6c"},
            ),
        ],
        responses={
            204: OpenApiResponse(description="기록 완료(또는 조용히 스킵) — 바디 없음"),
            400: OpenApiResponse(
                description=(
                    "발생하지 않음 — 잘못된 페이로드는 조용히 무시되고 204 로 응답합니다 "
                    "(silent-204 원칙)"
                )
            ),
            429: OpenApiResponse(
                description="IP 당 스로틀 초과 (기본 120/hour). 표준 에러 포맷",
                examples=[
                    OpenApiExample(
                        "스로틀 초과",
                        value={
                            "success": False,
                            "error": {
                                "code": 429,
                                "message": "요청이 지연(throttled)되었습니다.",
                                "details": {"detail": "Request was throttled."},
                            },
                        },
                    )
                ],
            ),
            500: OpenApiResponse(
                description="서버 내부 오류 (DB 기록 실패는 내부에서 삼켜 204 — 사실상 발생하지 않음)"
            ),
        },
    )
    def post(self, request):
        # 1) 봇 UA 게이트 — 기록 없이 204
        user_agent = request.META.get("HTTP_USER_AGENT", "")
        ua_class = classify_ua(user_agent)
        if ua_class == UAClass.BOT:
            return Response(status=status.HTTP_204_NO_CONTENT)

        # 2) 페이로드 검증 — 실패해도 400 대신 조용한 204 (봇 쓰레기 입력에 정보 노출 없음)
        serializer = TrackVisitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(status=status.HTTP_204_NO_CONTENT)
        data = serializer.validated_data
        visitor_id = data["visitor_id"]
        utm_source = data.get("utm_source", "")
        utm_medium = data.get("utm_medium", "")
        utm_campaign = data.get("utm_campaign", "")
        utm_content = data.get("utm_content", "")
        referrer = data.get("referrer", "")
        landing_path = data.get("landing_path", "") or "/"

        # 3~4) 캐시 기반 어뷰즈 방어 — 캐시 장애 시엔 기록 허용(fail-open, 비콘이라 안전)
        try:
            # 3) 방문자별 시간당 기록 캡
            hour_bucket = timezone.now().strftime("%Y%m%d%H")
            cap_key = f"lv:cap:{visitor_id}:{hour_bucket}"
            cache.add(cap_key, 0, timeout=3600)
            if cache.incr(cap_key) > settings.TRACK_VISIT_MAX_WRITES_PER_VISITOR_HOUR:
                return Response(status=status.HTTP_204_NO_CONTENT)

            # 4) burst dedup — 동일 fingerprint 30분 내 재전송 스킵
            fingerprint = hashlib.sha1(
                f"{visitor_id}|{utm_source}|{utm_medium}|{utm_campaign}|{landing_path}".encode()
            ).hexdigest()
            if not cache.add(f"lv:dedup:{fingerprint}", 1, timeout=_DEDUP_TTL_SECONDS):
                return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception:
            logger.warning("track_visit: cache guard failed — writing anyway", exc_info=True)

        # 5) 기록 — 실패해도 204 (silent success)
        try:
            LandingVisit.objects.create(
                visitor_id=visitor_id,
                utm_source=utm_source,
                utm_medium=utm_medium,
                utm_campaign=utm_campaign,
                utm_content=utm_content,
                referrer=referrer,
                landing_path=landing_path,
                channel=derive_channel(utm_source, utm_medium, referrer),
                country=get_country(request),
                ip_hash=hash_ip(request),
                ua_class=ua_class,
            )
        except Exception:
            logger.warning("track_visit: insert failed visitor_id=%s", visitor_id, exc_info=True)

        return Response(status=status.HTTP_204_NO_CONTENT)


class TrackCheckoutEventView(APIView):
    """결제 진입 텔레메트리 수집 — 로그인 사용자 전용.

    유료 제한 모달 노출/결제 시작 시 서비스 프론트가 호출한다. 이 데이터가
    마케팅 대시보드의 **결제 진입 경로(entry_paths)** 원천이다.

    silent 원칙(비콘): 검증 통과 → 201, 검증 실패 → 400(개발 신호)이나
    프론트는 응답을 무시하므로 UX 에 영향 없음. 미인증은 401.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "checkout_event"

    @extend_schema(
        tags=["analytics"],
        summary="결제 진입 이벤트 기록",
        description="""
## 개요
로그인 사용자가 **유료 제한 모달을 보거나 결제를 시작**할 때 서비스 프론트가
호출하는 텔레메트리 엔드포인트입니다. `trigger_feature`(DM 한도/페이지 제한/
배지 제거/스팸 방어/가격표 직접 등)와 `entry_source` 를 기록해 마케팅 대시보드가
**유료 전환의 진입 경로(업그레이드 트리거)** 를 재구성합니다.

## 사용 시나리오
- 무료/베이직 사용자가 DM 한도 초과 모달을 볼 때 → `paywall_viewed` + `trigger_feature=dm_limit`
- 업그레이드 버튼 클릭/결제 SDK 진입 → `checkout_started` + `selected_plan=pro`
- 상세 이벤트/필드 규약은 `대시보드_구현_보고서.html` "프론트 이벤트 트래킹" 참고

## 인증
`Authorization: Bearer <access_token>` 필수 (로그인 사용자). 미인증 401.

## 비즈니스 로직
- 항상 append-only 로 1행 기록 (`CheckoutEvent`). '무엇 때문에 결제했나'를 단정하지
  않고, **어디서 결제 화면에 진입했는지**만 남깁니다 (신뢰도는 대시보드가 해석).
- `event` 만 필수, 나머지는 선택. 알 수 없는 `trigger_feature` 문자열도 저장되며
  대시보드는 라벨 없으면 원문 표기합니다.

## 요청 바디 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `event` | ✅ | paywall_viewed / checkout_started / pricing_page_viewed / paywall_cta_clicked / plan_selected / feature_limit_reached / premium_feature_attempted |
| `entry_source` | 선택 | paywall / pricing_page / upgrade_button / direct |
| `trigger_feature` | 선택 | dm_limit / page_limit / badge_removal / spam_advanced / multi_ig / ai_page / analytics_export / pricing_direct |
| `source_page` | 선택 | 발생 화면 경로 |
| `current_plan` / `required_plan` / `selected_plan` | 선택 | 플랜 컨텍스트 |
| `usage_count` / `limit_count` | 선택 | 한도 도달 컨텍스트 |
""",
        request=CheckoutEventSerializer,
        examples=[
            OpenApiExample(
                "DM 한도 모달 노출",
                request_only=True,
                value={
                    "event": "paywall_viewed",
                    "entry_source": "paywall",
                    "trigger_feature": "dm_limit",
                    "source_page": "dm_campaign",
                    "current_plan": "free",
                    "required_plan": "pro",
                    "usage_count": 200,
                    "limit_count": 200,
                },
            ),
            OpenApiExample(
                "결제 시작",
                request_only=True,
                value={
                    "event": "checkout_started",
                    "entry_source": "paywall",
                    "trigger_feature": "dm_limit",
                    "selected_plan": "pro",
                },
            ),
        ],
        responses={
            201: OpenApiResponse(description="기록 완료 — 바디 없음"),
            400: OpenApiResponse(description="잘못된 event 값 등 (프론트는 응답 무시 권장)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            429: OpenApiResponse(description="스로틀 초과 (기본 240/hour)"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def post(self, request):
        serializer = CheckoutEventSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "잘못된 결제 이벤트 페이로드입니다",
                        "details": serializer.errors,
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = serializer.validated_data
        try:
            CheckoutEvent.objects.create(
                user=request.user,
                event=data["event"],
                entry_source=data.get("entry_source", ""),
                trigger_feature=data.get("trigger_feature", ""),
                source_page=data.get("source_page", ""),
                current_plan=data.get("current_plan", ""),
                required_plan=data.get("required_plan", ""),
                selected_plan=data.get("selected_plan", ""),
                usage_count=data.get("usage_count"),
                limit_count=data.get("limit_count"),
            )
        except Exception:
            logger.warning("checkout_event: insert failed user=%s", request.user.id, exc_info=True)
        return Response(status=status.HTTP_201_CREATED)


class TrackCancellationEventView(APIView):
    """구독 취소 텔레메트리 수집 — 로그인 사용자 전용.

    취소 버튼 클릭·해지 사유 제출·취소 예약/철회 시 서비스 프론트가 호출한다.
    이 데이터가 마케팅 대시보드의 **해지 사유 TOP N / 취소 방어율** 원천이다.
    실제 취소 예약/해지 카운트는 UserSubscription 상태에서 직접 집계한다.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "checkout_event"

    @extend_schema(
        tags=["analytics"],
        summary="구독 취소 이벤트 기록",
        description="""
## 개요
로그인 사용자가 **취소 버튼을 누르거나 해지 사유를 제출**할 때 서비스 프론트가
호출하는 텔레메트리 엔드포인트입니다. 해지 사유(`reason`)와 취소 플로우 단계(`event`)를
기록해 마케팅 대시보드가 **왜 떠나는가(해지 사유)** 와 **취소 화면 방어율**을 계산합니다.

## 인증
`Authorization: Bearer <access_token>` 필수 (로그인 사용자). 미인증 401.

## 요청 바디 필드
| 필드 | 필수 | 설명 |
|------|:----:|------|
| `event` | ✅ | cancel_button_clicked / cancel_reason_submitted / subscription_cancel_scheduled / subscription_cancel_aborted / subscription_resumed / **offer_shown** / **offer_accepted** / **offer_declined** |
| `reason` | 선택 | price / low_usage / no_effect / hard_setup / missing_feature / ig_error / switched / paused / other |
| `reason_detail` | 선택 | 자유입력 상세 |
| `offer` | 선택 | offer_* 이벤트의 대상 오퍼 키 (예: downgrade_basic / pause / discount_50) — 오퍼별 방어율 퍼널 축 |
| `from_plan` / `to_plan` | 선택 | 플랜 컨텍스트 |

## 비즈니스 로직
- append-only 로 1행 기록. 실제 구독 상태 전이는 백엔드가 알고 있으므로, 여기서는
  프론트만 아는 '사유/방어 플로우'만 보완합니다. 알 수 없는 `reason` 문자열도 저장됩니다.
""",
        request=CancellationEventSerializer,
        examples=[
            OpenApiExample(
                "해지 사유 제출",
                request_only=True,
                value={
                    "event": "cancel_reason_submitted",
                    "reason": "low_usage",
                    "from_plan": "pro",
                },
            ),
        ],
        responses={
            201: OpenApiResponse(description="기록 완료 — 바디 없음"),
            400: OpenApiResponse(description="잘못된 event 값 등 (프론트는 응답 무시 권장)"),
            401: OpenApiResponse(description="인증 누락/만료"),
            429: OpenApiResponse(description="스로틀 초과 (기본 240/hour)"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def post(self, request):
        serializer = CancellationEventSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": 400,
                        "message": "잘못된 취소 이벤트 페이로드입니다",
                        "details": serializer.errors,
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = serializer.validated_data
        try:
            CancellationEvent.objects.create(
                user=request.user,
                event=data["event"],
                reason=data.get("reason", ""),
                reason_detail=data.get("reason_detail", ""),
                offer=data.get("offer", ""),
                from_plan=data.get("from_plan", ""),
                to_plan=data.get("to_plan", ""),
            )
        except Exception:
            logger.warning(
                "cancellation_event: insert failed user=%s", request.user.id, exc_info=True
            )
        return Response(status=status.HTTP_201_CREATED)
