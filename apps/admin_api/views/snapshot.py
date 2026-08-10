"""apps/admin_api/views/snapshot.py — 전체 현황 타일의 회원 명단 (18차 SNAP-1/2).

  - ``GET /api/v1/admin/snapshot/paying/``  실결제 회원 명단  (타일: 실제 결제 인원)
  - ``GET /api/v1/admin/snapshot/trial/``   체험 회원 명단    (타일: 프로 체험 인원)

**타일을 만드는 그 쿼리가 명단도 만든다** — 모수 판정은
:mod:`apps.admin_api.snapshot_rosters` 한 곳에 있고 마케팅 대시보드의 ``_snapshot`` /
``_trial_now`` 와 이 뷰가 함께 쓴다. 프론트에서 조립하면 타일과 명단이 조용히 어긋난다
(DM-17 의 ``not_sent_people`` 과 같은 이유).

**캐시 정합 (요청서 §공통 ② → 1번안 채택)**: 명단은 라이브로 재계산하지 않고, 타일을 만든
그 순간의 **id 집합**(마케팅 스냅샷 캐시에 함께 저장)을 읽어 그 위에서 페이지네이션한다.
그래서 `타일 숫자 == 명단 count` 가 캐시 TTL(900초) 과 무관하게 성립한다. 응답 top-level
``as_of`` 는 그 집합이 계산된 시각이다(타일의 ``snapshot.as_of`` 와 동일 값).
집합이 상한(``SNAPSHOT_ROSTER_ID_CACHE_MAX``)을 넘으면 라이브로 폴백하고 ``as_of`` 가 지금
시각이 되어 불일치가 시각 차이로 드러난다(``is_live=true``).

**권한**: 최고 관리자 전용. ``/api/v1/admin/snapshot/**`` 는 RBAC 화이트리스트에 없으므로
:class:`apps.admin_api.middleware.AdminRoleGuardMiddleware` 가 marketing_viewer 를 자동으로
403 처리한다. 이 명단은 회원 식별이 목적이라 **이메일 마스킹(RBAC-3)을 적용하지 않는다**.
"""

from __future__ import annotations

import logging

from django.db.models import Count, Max, Q
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status as http_status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.admin_api.dashboard_cache import CACHE_BYPASS, CACHE_HEADER, CACHE_HIT, wants_cache_bypass
from apps.admin_api.dashboard_constants import SNAPSHOT_ROSTER_MAX_PAGE_SIZE
from apps.admin_api.serializers.snapshot import (
    AdminPayingMemberSerializer,
    AdminTrialMemberSerializer,
)
from apps.admin_api.snapshot_rosters import (
    BUCKET_CANCELLED,
    TRIAL_BUCKETS,
    bucket_of,
    paying_subscriptions_qs,
    trial_roster_qs,
)
from apps.billing.models import PaymentStatus, UserSubscription

logger = logging.getLogger(__name__)

TAG = "admin-dashboard"


class _RosterPagination(PageNumberPagination):
    """표준 page_size=20. CSV 내보내기용으로 ``?page_size=`` 를 상한 500 까지 허용."""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = SNAPSHOT_ROSTER_MAX_PAGE_SIZE


def _bad_request(message: str, *, field: str, allowed=None) -> Response:
    """공통 오류 포맷 (apps/core/exceptions 규약과 동일한 모양)."""
    details: dict = {"field": field}
    if allowed is not None:
        details["allowed"] = allowed
    return Response(
        {"success": False, "error": {"code": 400, "message": message, "details": details}},
        status=http_status.HTTP_400_BAD_REQUEST,
    )


def _frozen_ids(request, key: str) -> tuple[dict | None, str, str]:
    """타일이 쓴 id→축 매핑 + 그 계산 시각 + 캐시 상태. 상한 초과면 매핑이 None.

    마케팅 스냅샷 캐시를 **같은 함수**(``_snapshot_cached``)로 읽는다 — 캐시가 비어 있으면
    여기서 계산되고 그 값이 곧 다음 타일 조회의 소스가 되므로 양쪽이 어긋나지 않는다.
    """
    from django.utils import timezone

    from apps.admin_api.views.dashboard_marketing import _snapshot_cached

    bypass = wants_cache_bypass(request)
    snap = _snapshot_cached(timezone.now(), bypass=bypass)
    id_map = (snap.get("_roster_ids") or {}).get(key)
    return id_map, snap.get("as_of"), (CACHE_BYPASS if bypass else CACHE_HIT)


def _apply_search(qs, term: str):
    """이메일·이름 부분일치 (회원 목록 ``?search=`` 와 같은 축)."""
    if not term:
        return qs
    return qs.filter(Q(user__email__icontains=term) | Q(user__full_name__icontains=term))


def _ordering_or_400(params, allowed: dict, default: str):
    """``?ordering=`` 화이트리스트 → (정렬 필드 리스트, 오류 Response).

    허용값 밖이면 **조용히 무시하지 않고 400** 이다 (요청서 §공통 ③) — 조용히 버리면 화면이
    "정렬이 걸린 척" 하고, 프론트에서 그것을 알아낼 방법이 없다.
    """
    raw = (params.get("ordering") or "").strip()
    if not raw:
        return allowed[default], None
    if raw not in allowed:
        return None, _bad_request(
            f"ordering 값이 올바르지 않습니다: {raw!r}",
            field="ordering",
            allowed=sorted(allowed),
        )
    return allowed[raw], None


# ──────────────────────────────────────────────
# SNAP-1 — 실결제 회원 명단
# ──────────────────────────────────────────────


class AdminPayingSnapshotView(APIView):
    """실결제 회원 명단 (전체 현황 `실제 결제 인원` 타일의 그 사람들)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminPayingMemberSerializer

    # ?ordering= 화이트리스트 → 실제 정렬 필드. tie-break 로 pk 를 붙여 페이지 경계 안정.
    ORDERING = {
        "-last_paid_at": ["-last_paid_at", "-pk"],
        "last_paid_at": ["last_paid_at", "pk"],
        "-next_billing_at": ["-current_period_end", "-pk"],
        "next_billing_at": ["current_period_end", "pk"],
        "-monthly_amount": ["-monthly_amount_snapshot", "-pk"],
        "monthly_amount": ["monthly_amount_snapshot", "pk"],
        "-date_joined": ["-user__date_joined", "-pk"],
        "date_joined": ["user__date_joined", "pk"],
    }

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 실결제 회원 명단",
        description="""
## 개요
마케팅 대시보드 상단 `전체 현황` 의 **`실제 결제 인원`** 타일을 구성하는 회원들의 명단입니다.
타일 숫자를 눌러 명단으로, 행을 눌러 회원 상세로 들어가는 동선용입니다.

## 모수 정의
카드로 **실제 결제(PAID)가 승인된 이력**이 있고 **현재 구독이 정상 유지 중(ACTIVE)** 인 회원.
- 무료체험·어드민 수동 부여 제외 (전자는 PAID 이력이 없고, 후자는 `admin` 플랜 제외로 걸러짐)
- **결제 실패(재시도 중, `past_due`) 제외** — `customer_actions.payment_failed` 에 별도로 잡힙니다
- `free` / `admin` 플랜 제외

판정은 대시보드 타일과 **같은 함수**(`apps/admin_api/snapshot_rosters.py`)를 씁니다.

## 지켜지는 항등
```
count                  == snapshot.paying.total
?plan=<플랜> 의 count   == snapshot.paying.by_plan[해당].count
```
명단은 **타일을 만든 그 순간의 id 집합**을 읽어 페이지네이션하므로, 900초 캐시가 살아 있어도
타일과 명단이 어긋나지 않습니다. `as_of` 가 그 집합의 계산 시각입니다.

## 인증
`Authorization: Bearer <staff_access_token>` (is_staff=True)
**최고 관리자 전용** — `marketing_viewer` 역할은 미들웨어가 403 (`section_forbidden`).
이 명단은 회원 식별이 목적이라 **이메일 마스킹을 적용하지 않습니다.**

## 쿼리 파라미터
| 파라미터 | 설명 |
|---|---|
| `plan` | 플랜 코드명으로 필터 (칩) |
| `search` | 이메일·이름 부분일치 |
| `ordering` | 화이트리스트: `last_paid_at` · `next_billing_at` · `monthly_amount` · `date_joined` (앞에 `-` = 내림차순). 기본 `-last_paid_at`. **허용값 밖은 400** |
| `page` / `page_size` | 기본 20, 상한 500 (CSV 내보내기용) |

## 응답
표준 `PageNumberPagination` + top-level `as_of` · `is_live`:
```jsonc
{
  "count": 58,
  "next": "...", "previous": null,
  "as_of": "2026-08-10T19:40:02+09:00",   // 집계 기준 시각 (타일과 동일)
  "is_live": false,                        // true 면 캐시 집합 미사용(상한 초과) → 타일과 다를 수 있음
  "results": [ ... ]
}
```

## 필드 주의사항
- `monthly_amount` 는 **서버 계산값**(`UserSubscription.renewal_amount`)입니다. 스냅샷가·추가
  IG 계정·리텐션 할인이 모두 반영돼 있으니 프론트에서 재계산하지 마세요 (USR-1 과 같은 이유).
- `paid_count` = **`status=paid` 인 건수**. 환불된 건은 status 가 `refunded` 로 바뀌므로 자동
  제외되고, 부분취소로 생기는 음수 금액 행도 제외합니다.

## 에러
| 코드 | 원인 |
|---|---|
| 400 | `ordering` 화이트리스트 밖 (`details.field="ordering"`, `details.allowed`) |
| 401 | 토큰 없음/만료 |
| 403 | 스태프 아님 / `marketing_viewer` 역할 |
| 500 | 서버 오류 |
        """,
        parameters=[
            OpenApiParameter(
                name="plan",
                type=str,
                location=OpenApiParameter.QUERY,
                description="플랜 코드명 필터 (basic/pro). 하단 칩에서 사용.",
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description="이메일·이름 부분일치 검색.",
            ),
            OpenApiParameter(
                name="ordering",
                type=str,
                location=OpenApiParameter.QUERY,
                description="정렬. `last_paid_at`/`next_billing_at`/`monthly_amount`/`date_joined` "
                "(± 부호). 기본 `-last_paid_at`. 허용값 밖은 400.",
            ),
            OpenApiParameter(
                name="page_size",
                type=int,
                location=OpenApiParameter.QUERY,
                description="페이지 크기 (기본 20, 상한 500). CSV 내보내기용.",
            ),
            OpenApiParameter(
                name="refresh",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="1 이면 스냅샷 캐시를 재계산한 뒤 명단을 만든다 (full 역할 전용).",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=AdminPayingMemberSerializer(many=True),
                description="실결제 회원 명단",
                examples=[
                    OpenApiExample(
                        "1페이지",
                        value={
                            "count": 58,
                            "next": "https://api.example/api/v1/admin/snapshot/paying/?page=2",
                            "previous": None,
                            "as_of": "2026-08-10T19:40:02+09:00",
                            "is_live": False,
                            "results": [
                                {
                                    "user_id": 1042,
                                    "email": "grower@example.com",
                                    "full_name": "김성장",
                                    "plan_name": "pro",
                                    "plan_display_name": "프로",
                                    "monthly_amount": 24800,
                                    "extra_ig_accounts": 1,
                                    "last_paid_at": "2026-08-05T09:12:44+09:00",
                                    "next_billing_at": "2026-09-04T09:12:44+09:00",
                                    "paid_count": 3,
                                    "date_joined": "2026-05-21T11:02:10+09:00",
                                }
                            ],
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="ordering 화이트리스트 밖 (표준 에러 포맷)"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음 (스태프 아님 / marketing_viewer)"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def get(self, request):
        params = request.query_params
        order_by, err = _ordering_or_400(params, self.ORDERING, "-last_paid_at")
        if err is not None:
            return err

        id_map, as_of, cache_state = _frozen_ids(request, "paying")
        if id_map is None:
            # 상한 초과 — 라이브 계산. as_of 를 지금으로 바꿔 타일과 다를 수 있음을 드러낸다.
            from django.utils import timezone

            qs = paying_subscriptions_qs()
            as_of = timezone.localtime(timezone.now()).isoformat()
            is_live = True
        else:
            qs = UserSubscription.objects.filter(pk__in=list(id_map))
            is_live = False

        plan = (params.get("plan") or "").strip()
        if plan:
            qs = qs.filter(plan__name=plan)
        qs = _apply_search(qs, (params.get("search") or "").strip())

        # 최근 결제 / 결제 횟수 — annotate 로 한 번에 (시리얼라이저 N+1 회피).
        # paid_count 는 status=paid 인 건수: 환불 건은 status 가 refunded 로 바뀌어 자동
        # 제외되고, 부분취소로 생기는 음수 금액 행은 amount__gt=0 으로 뺀다.
        qs = (
            qs.select_related("plan", "user")
            .annotate(
                last_paid_at=Max(
                    "user__payments__paid_at",
                    filter=Q(user__payments__status=PaymentStatus.PAID),
                ),
                paid_count=Count(
                    "user__payments",
                    filter=Q(user__payments__status=PaymentStatus.PAID)
                    & Q(user__payments__amount__gt=0),
                    distinct=True,
                ),
            )
            .order_by(*order_by)
        )

        paginator = _RosterPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        rows = [
            {
                "user_id": sub.user_id,
                "email": sub.user.email,
                "full_name": sub.user.full_name or "",
                "plan_name": sub.plan.name,
                "plan_display_name": sub.plan.display_name,
                "monthly_amount": sub.renewal_amount,
                "extra_ig_accounts": sub.extra_ig_accounts,
                "last_paid_at": sub.last_paid_at,
                "next_billing_at": sub.current_period_end,
                "paid_count": sub.paid_count,
                "date_joined": sub.user.date_joined,
            }
            for sub in page
        ]
        data = AdminPayingMemberSerializer(rows, many=True).data
        response = paginator.get_paginated_response(data)
        response.data["as_of"] = as_of
        response.data["is_live"] = is_live
        response[CACHE_HEADER] = cache_state
        return response


# ──────────────────────────────────────────────
# SNAP-2 — 체험 회원 명단
# ──────────────────────────────────────────────


class AdminTrialSnapshotView(APIView):
    """체험 회원 명단 (전체 현황 `프로 체험 인원` 타일의 그 사람들)."""

    permission_classes = [IsAdminUser]
    serializer_class = AdminTrialMemberSerializer

    ORDERING = {
        "trial_ends_at": ["current_period_end", "pk"],
        "-trial_ends_at": ["-current_period_end", "-pk"],
        "trial_started_at": ["current_period_start", "pk"],
        "-trial_started_at": ["-current_period_start", "-pk"],
        "date_joined": ["user__date_joined", "pk"],
        "-date_joined": ["-user__date_joined", "-pk"],
    }

    @extend_schema(
        tags=[TAG],
        summary="[관리자] 체험 회원 명단",
        description="""
## 개요
마케팅 대시보드 상단 `전체 현황` 의 **`프로 체험 인원`** 타일을 구성하는 회원들의 명단입니다.

## 모수 정의
**카드가 등록된 상태로 지금 체험 기간 중**인 회원 = `trial_now.will_charge + cancelled`.
```
will_charge  체험 중(TRIALING) + 카드 있음 + 미취소  → 기간말에 과금된다
cancelled    체험 중 취소(기간 남음)                 → 과금 없이 free 로 내려간다
```
⚠️ `trial_now.no_card`(쿠폰 무카드 체험)는 **제외**합니다 — 자동 유료전환 대상이 아니라
"결제 예정액" 열이 거짓이 되기 때문입니다. 따라서 이 명단의 `count` 는
`snapshot.trial_now.total` 이 아니라 **`will_charge + cancelled`** 와 같습니다.

`bucket` 판정은 15차에 정리한 서버 판정(`cancelled_during_trial_at >= current_period_start`)이
정본입니다 — 프론트에서 재판정하지 마세요.

## 지켜지는 항등
```
count                        == trial_now.will_charge + trial_now.cancelled
?bucket=will_charge 의 count  == trial_now.will_charge
?bucket=cancelled 의 count    == trial_now.cancelled
```
명단은 타일을 만든 그 순간의 id→bucket 매핑을 읽으므로, 캐시 창(900초) 안에 누군가 취소해도
`bucket` 별 부분합이 타일과 어긋나지 않습니다.

## 인증
`Authorization: Bearer <staff_access_token>` (is_staff=True)
**최고 관리자 전용** — `marketing_viewer` 는 미들웨어가 403. 이메일 마스킹 없음.

## 쿼리 파라미터
| 파라미터 | 설명 |
|---|---|
| `bucket` | `will_charge` / `cancelled` (칩). 허용값 밖은 400 |
| `search` | 이메일·이름 부분일치 |
| `ordering` | 화이트리스트: `trial_ends_at` · `trial_started_at` · `date_joined` (± 부호). 기본 `trial_ends_at`(종료 임박순). **허용값 밖은 400** |
| `page` / `page_size` | 기본 20, 상한 500 |

## 필드 주의사항
- `expected_amount` 는 서버 계산값(`renewal_amount`). `bucket=cancelled` 면 `null` 입니다.
- `trial_started_at` 은 **이번 체험 기간의 시작**(`current_period_start`)입니다. `trial_used_at`
  은 '1인 1회' 어뷰징 방어용 내구 필드라 재체험 이력이 섞일 수 있어 쓰지 않습니다.
- `conversion_consent_required` (2026-08-10 추가): 30일 초과 체험(쿠폰 연장)인데 유료전환
  2차 동의가 아직 없는 회원입니다. 동의 없이 체험이 끝나면 **결제되지 않고 무료로 전환**되니,
  운영에서 미리 안내할 대상 목록으로 쓸 수 있습니다.
- 원본 카드번호·빌링키는 절대 노출하지 않습니다 (마스킹된 표시값만).

## 에러
| 코드 | 원인 |
|---|---|
| 400 | `bucket` / `ordering` 허용값 밖 (`details.field`, `details.allowed`) |
| 401 | 토큰 없음/만료 |
| 403 | 스태프 아님 / `marketing_viewer` 역할 |
| 500 | 서버 오류 |
        """,
        parameters=[
            OpenApiParameter(
                name="bucket",
                type=str,
                location=OpenApiParameter.QUERY,
                description="`will_charge` / `cancelled`. 허용값 밖은 400.",
            ),
            OpenApiParameter(
                name="search",
                type=str,
                location=OpenApiParameter.QUERY,
                description="이메일·이름 부분일치 검색.",
            ),
            OpenApiParameter(
                name="ordering",
                type=str,
                location=OpenApiParameter.QUERY,
                description="정렬. `trial_ends_at`/`trial_started_at`/`date_joined` (± 부호). "
                "기본 `trial_ends_at`(종료 임박순). 허용값 밖은 400.",
            ),
            OpenApiParameter(
                name="page_size",
                type=int,
                location=OpenApiParameter.QUERY,
                description="페이지 크기 (기본 20, 상한 500).",
            ),
            OpenApiParameter(
                name="refresh",
                type=bool,
                location=OpenApiParameter.QUERY,
                description="1 이면 스냅샷 캐시를 재계산한 뒤 명단을 만든다 (full 역할 전용).",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=AdminTrialMemberSerializer(many=True),
                description="체험 회원 명단",
                examples=[
                    OpenApiExample(
                        "1페이지 (종료 임박순)",
                        value={
                            "count": 21,
                            "next": "https://api.example/api/v1/admin/snapshot/trial/?page=2",
                            "previous": None,
                            "as_of": "2026-08-10T19:40:02+09:00",
                            "is_live": False,
                            "results": [
                                {
                                    "user_id": 1188,
                                    "email": "trialer@example.com",
                                    "full_name": "박체험",
                                    "plan_name": "pro",
                                    "plan_display_name": "프로",
                                    "trial_started_at": "2026-07-12T10:00:00+09:00",
                                    "trial_ends_at": "2026-08-25T10:00:00+09:00",
                                    "trial_total_days": 44,
                                    "bucket": "will_charge",
                                    "expected_amount": 14900,
                                    "conversion_consent_required": True,
                                    "card_company": "현대",
                                    "card_number_masked": "433012******123*",
                                    "date_joined": "2026-07-12T09:58:03+09:00",
                                }
                            ],
                        },
                    )
                ],
            ),
            400: OpenApiResponse(description="bucket / ordering 허용값 밖 (표준 에러 포맷)"),
            401: OpenApiResponse(description="인증 실패"),
            403: OpenApiResponse(description="권한 없음 (스태프 아님 / marketing_viewer)"),
            500: OpenApiResponse(description="서버 오류"),
        },
    )
    def get(self, request):
        params = request.query_params
        bucket = (params.get("bucket") or "").strip() or None
        if bucket is not None and bucket not in TRIAL_BUCKETS:
            return _bad_request(
                f"bucket 값이 올바르지 않습니다: {bucket!r}",
                field="bucket",
                allowed=list(TRIAL_BUCKETS),
            )
        order_by, err = _ordering_or_400(params, self.ORDERING, "trial_ends_at")
        if err is not None:
            return err

        id_map, as_of, cache_state = _frozen_ids(request, "trial")
        if id_map is None:
            from django.utils import timezone

            now = timezone.now()
            qs = trial_roster_qs(now, bucket=bucket)
            as_of = timezone.localtime(now).isoformat()
            is_live = True
            bucket_map = None
        else:
            # 얼려둔 매핑에서 버킷을 읽는다 — 캐시 창 안에 취소가 일어나도 부분합이 타일과
            # 일치한다(라이브로 재판정하면 will_charge 가 1 줄어 명단이 잘린 것으로 읽힌다).
            ids = [pk for pk, b in id_map.items() if bucket is None or b == bucket]
            qs = UserSubscription.objects.filter(pk__in=ids)
            is_live = False
            bucket_map = id_map

        qs = _apply_search(qs, (params.get("search") or "").strip())
        qs = qs.select_related("plan", "user").order_by(*order_by)

        paginator = _RosterPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        rows = []
        for sub in page:
            # 얼려둔 매핑이 정본. 없으면(라이브 폴백) 현재 상태로 판정한다.
            row_bucket = (bucket_map or {}).get(str(sub.pk)) or bucket_of(sub)
            rows.append(
                {
                    "user_id": sub.user_id,
                    "email": sub.user.email,
                    "full_name": sub.user.full_name or "",
                    "plan_name": sub.plan.name,
                    "plan_display_name": sub.plan.display_name,
                    "trial_started_at": sub.current_period_start,
                    "trial_ends_at": sub.current_period_end,
                    "trial_total_days": sub.trial_total_days,
                    "bucket": row_bucket,
                    # 취소자는 과금되지 않으므로 금액을 주지 않는다(주면 '결제 예정' 오독).
                    "expected_amount": (
                        None if row_bucket == BUCKET_CANCELLED else sub.renewal_amount
                    ),
                    "conversion_consent_required": sub.conversion_consent_required,
                    "card_company": sub.card_company or "",
                    "card_number_masked": sub.card_number_masked or "",
                    "date_joined": sub.user.date_joined,
                }
            )
        data = AdminTrialMemberSerializer(rows, many=True).data
        response = paginator.get_paginated_response(data)
        response.data["as_of"] = as_of
        response.data["is_live"] = is_live
        response[CACHE_HEADER] = cache_state
        return response


__all__ = ["AdminPayingSnapshotView", "AdminTrialSnapshotView"]
