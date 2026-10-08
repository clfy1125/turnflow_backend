"""휴대폰 번호 수집 현황 — ``GET /api/v1/admin/phone-collection/``.

⭐ **왜 별도 엔드포인트인가**: 마케팅 대시보드(``dashboard_marketing.py``)는 이미 수백 줄의
단일 호출 집계라 블록 하나를 더 끼우면 그 전체의 p95 를 끌어올린다. 이 지표는 알림톡
캠페인을 **언제 시작할 수 있는가**(= 발송 가능 모수가 충분한가)만 보면 되고, 대시보드와
갱신 주기도 다르다.

⚠️ **번호 원문은 절대 내보내지 않는다.** 여기는 집계만 돌려준다. 마케팅 대행사가
   채널톡에 올릴 번호 목록이 필요하다면 그건 **개인정보 처리위탁**이라
   ①수탁자 명시(처리방침) ②위탁 계약 ③안전성 확보조치가 먼저다. 그 절차 없이
   내려받을 수 있는 경로를 만들면 그 자체가 유출 경로가 된다.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

User = get_user_model()


class AdminPhoneCollectionView(APIView):
    """번호 수집 진척도 — 알림톡 발송 가능 모수."""

    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["admin"],
        summary="휴대폰 번호 수집 현황",
        description="""
## 개요
알림톡을 **몇 명에게 보낼 수 있는가**를 한 번에 보여줍니다. 수집 경로별·가입수단별
분해와 최근 14일 일별 추이를 함께 돌려줍니다.

## 사용 시나리오
- 알림톡 캠페인 개시 시점 판단 (발송 가능 모수가 임계치를 넘었는가)
- 필수화 시행 후 신규 가입자의 인증 완료율 모니터링
- 카카오 전화번호 동의항목 승인 후 카카오 경로 유입량 확인

## 인증
`Authorization: Bearer <admin access token>` + `is_staff=True`. 미인증 401 / 권한 없음 403.

## 비즈니스 로직
- `reachable.informational` = **정보성 알림톡** 발송 가능 인원
  (본인확인된 번호 보유 · 수신동의 불요 — 체험 종료·결제 실패 등 거래성 고지).
- `reachable.advertising` = **광고성**(브랜드메시지·프로모션) 발송 가능 인원
  (`sms_marketing_opt_in=true` — 정보통신망법 §50 사전 동의).
  ⚠️ 두 숫자를 섞지 마세요. 광고성을 정보성 모수로 보내면 과태료 대상입니다.
- `by_source` — `sms`(우리 문자 인증) / `kakao`(카카오 동의항목).
  kakao 가 늘기 시작하면 동의항목 심사가 통과·반영됐다는 뜻입니다.
- `coverage_pct` 분모는 **살아있는 계정**(`is_active=True`, 탈퇴 유예 제외)입니다.

## 응답 (200)
```json
{
  "total_active_users": 2631,
  "with_phone": 412,
  "coverage_pct": 15.7,
  "reachable": { "informational": 412, "advertising": 118 },
  "by_source": { "sms": 390, "kakao": 22 },
  "by_signup_kind": { "kakao": 180, "google": 120, "instagram": 70, "email": 42 },
  "reward_granted": 305,
  "recent_daily": [ { "date": "2026-10-08", "count": 31 } ],
  "sms_last_7d": { "sent": 420, "failed": 3, "mocked": 0 }
}
```

## 주의사항
- **번호 원문은 포함되지 않습니다** (설계상 — 파일 상단 주석 참고).
- 캐시 없음 — 호출 즉시 집계합니다(COUNT 몇 개라 가볍습니다).

## 사용 예시
```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" \\
  https://api.turnflow.link/api/v1/admin/phone-collection/
```
        """,
        responses={
            200: OpenApiResponse(
                description="수집 현황",
                examples=[
                    OpenApiExample(
                        "현황",
                        value={
                            "total_active_users": 2631,
                            "with_phone": 412,
                            "coverage_pct": 15.7,
                            "reachable": {"informational": 412, "advertising": 118},
                            "by_source": {"sms": 390, "kakao": 22},
                            "by_signup_kind": {
                                "kakao": 180,
                                "google": 120,
                                "instagram": 70,
                                "email": 42,
                            },
                            "reward_granted": 305,
                            "recent_daily": [{"date": "2026-10-08", "count": 31}],
                            "sms_last_7d": {"sent": 420, "failed": 3, "mocked": 0},
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="해당 없음"),
            401: OpenApiResponse(description="인증 실패 — 토큰 없음/만료"),
            403: OpenApiResponse(description="어드민 권한 없음 (is_staff=False)"),
            404: OpenApiResponse(description="해당 없음"),
            500: OpenApiResponse(description="서버 오류 — X-Request-ID 와 함께 문의해 주세요"),
        },
    )
    def get(self, request):
        from apps.sms.models import SmsLog, SmsStatus

        live = User.objects.filter(is_active=True, deletion_scheduled_at__isnull=True)
        verified = live.exclude(phone="").filter(phone_verified_at__isnull=False)

        total = live.count()
        with_phone = verified.count()

        by_source = dict(
            verified.values_list("phone_source")
            .annotate(n=Count("id"))
            .values_list("phone_source", "n")
        )

        # 가입수단 판정은 memory(no-phone-numbers-alimtalk-blocked)의 실측 규칙과 같다:
        # kakao_id / instagram_user_id / 소셜(password 가 '!' 로 시작) / 나머지=이메일.
        kinds = verified.aggregate(
            kakao=Count("id", filter=Q(kakao_id__isnull=False)),
            instagram=Count("id", filter=Q(instagram_user_id__isnull=False)),
            google=Count(
                "id",
                filter=Q(kakao_id__isnull=True)
                & Q(instagram_user_id__isnull=True)
                & Q(password__startswith="!"),
            ),
            email=Count(
                "id",
                filter=Q(kakao_id__isnull=True)
                & Q(instagram_user_id__isnull=True)
                & ~Q(password__startswith="!"),
            ),
        )

        # TruncDate 는 settings.TIME_ZONE(Asia/Seoul) 기준으로 끊긴다 — 어드민이 보는
        # "어제 몇 명"과 서버 판정이 같아야 한다.
        since = timezone.now() - timedelta(days=14)
        daily_raw = (
            verified.filter(phone_verified_at__gte=since)
            .annotate(d=TruncDate("phone_verified_at"))
            .values("d")
            .annotate(n=Count("id"))
            .order_by("d")
        )
        recent_daily = [{"date": r["d"].isoformat(), "count": r["n"]} for r in daily_raw if r["d"]]

        sms_since = timezone.now() - timedelta(days=7)
        sms = SmsLog.objects.filter(created_at__gte=sms_since).aggregate(
            sent=Count("id", filter=Q(status=SmsStatus.SENT)),
            failed=Count("id", filter=Q(status=SmsStatus.FAILED)),
            mocked=Count("id", filter=Q(status=SmsStatus.MOCKED)),
        )

        return Response(
            {
                "total_active_users": total,
                "with_phone": with_phone,
                "coverage_pct": round(with_phone / total * 100, 1) if total else 0.0,
                "reachable": {
                    # 정보성(거래성) 알림톡 — 수신동의 불요
                    "informational": with_phone,
                    # 광고성 — 정보통신망법 §50 사전 동의 보유자만
                    "advertising": verified.filter(sms_marketing_opt_in=True).count(),
                },
                "by_source": {k or "unknown": v for k, v in by_source.items()},
                "by_signup_kind": kinds,
                "reward_granted": live.filter(phone_reward_granted_at__isnull=False).count(),
                "recent_daily": recent_daily,
                "sms_last_7d": sms,
            }
        )
