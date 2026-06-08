"""apps/admin_api/serializers/dashboard.py — 어드민 대시보드 지표 시리얼라이저.

라우팅: ``GET /api/v1/admin/metrics/overview/`` (``IsAdminUser``, is_staff=True).
이 모듈의 시리얼라이저는 **OpenAPI 응답 문서화 전용**이다 — 뷰가 만든 plain dict 의
중첩 구조를 drf-spectacular 스키마로 그대로 노출하기 위한 것이며, 실제 직렬화/검증
로직(쿼리·집계)은 :mod:`apps.admin_api.views.dashboard` 가 담당한다.

집계 범위는 **전 워크스페이스(GLOBAL)** 이다. request.user 의 소속 워크스페이스로
필터링하지 않는다 (백오피스 전수 집계).
"""

from __future__ import annotations

from rest_framework import serializers


class _UsersMetricsSerializer(serializers.Serializer):
    """회원(계정) 지표 — 전역 카운트."""

    total = serializers.IntegerField(help_text="전체 회원 수")
    active = serializers.IntegerField(help_text="활성 회원 수 (is_active=True)")
    new_today = serializers.IntegerField(
        help_text="오늘(Asia/Seoul 기준) 가입한 회원 수 (date_joined 기준)"
    )
    new_7d = serializers.IntegerField(help_text="최근 7일간 가입한 회원 수")
    verified = serializers.IntegerField(
        help_text="이메일 인증 완료 회원 수 (is_email_verified=True)"
    )


class _WorkspacesMetricsSerializer(serializers.Serializer):
    """워크스페이스 플랜별 분포.

    ⚠️ DEPRECATED — 레거시 Workspace.plan(starter/pro/enterprise) 카운트로 실제 과금과
    무관하다. 실제 구독 분포는 :class:`_SubscriptionsMetricsSerializer` (``subscriptions.by_plan``).
    """

    by_plan = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="⚠️ DEPRECATED. 레거시 Workspace.plan 별 워크스페이스 수 "
        "(키: starter/pro/enterprise). 실제 구독 분포는 `subscriptions.by_plan` 사용.",
    )


class _SubscriptionByPlanItemSerializer(serializers.Serializer):
    """구독 플랜 1건의 회원 수 카운트."""

    name = serializers.CharField(help_text="SubscriptionPlan.name (예: free/pro/admin)")
    display_name = serializers.CharField(
        help_text="SubscriptionPlan.display_name (예: 무료/프로/관리자)"
    )
    count = serializers.IntegerField(help_text="해당 플랜의 UserSubscription 수")


class _SubscriptionsMetricsSerializer(serializers.Serializer):
    """실제 구독(UserSubscription→SubscriptionPlan) 플랜별 분포.

    플랜이 DB-driven(가변)이라 고정 키 대신 **동적 리스트**로 노출한다.
    모든 플랜(비활성 포함)을 sort_order 오름차순으로 포함하며, 카운트는 UserSubscription 기준.
    """

    by_plan = _SubscriptionByPlanItemSerializer(
        many=True,
        help_text="플랜별 회원 구독 수 [{name, display_name, count}], sort_order 오름차순.",
    )


class _PagesMetricsSerializer(serializers.Serializer):
    """페이지 지표 — 전역 카운트."""

    total = serializers.IntegerField(help_text="전체 페이지 수")
    public = serializers.IntegerField(help_text="공개 페이지 수 (is_public=True)")
    active = serializers.IntegerField(help_text="활성 페이지 수 (is_active=True)")


class _CampaignsMetricsSerializer(serializers.Serializer):
    """자동 DM 캠페인 상태별 카운트 (AutoDMCampaign.Status)."""

    active = serializers.IntegerField(help_text="활성 캠페인 수 (status=active)")
    paused = serializers.IntegerField(help_text="일시정지 캠페인 수 (status=paused)")
    completed = serializers.IntegerField(help_text="완료 캠페인 수 (status=completed)")
    total = serializers.IntegerField(help_text="전체 캠페인 수")


class _DmMetricsSerializer(serializers.Serializer):
    """DM 발송 지표 (SentDMLog, created_at >= since).

    필드명/의미는 ``/api/v1/integrations/dm-verification/stats/`` 와 동일하다.
    """

    accepted = serializers.IntegerField(help_text="Meta 접수 건 수 (status=accepted)")
    delivered = serializers.IntegerField(
        help_text="도착 확인 건 수 (status=delivered, READ 미포함)"
    )
    delivery_rate = serializers.FloatField(
        help_text=(
            "도착 확정 비율 (0~1). "
            "= (delivered+read) / (accepted+delivered+read+failed_no_trace)"
        )
    )
    failed_token = serializers.IntegerField(
        help_text="토큰 만료/세션 무효로 실패 (status=failed_token)"
    )
    failed_window = serializers.IntegerField(
        help_text="24h 메시징 윈도우 만료로 실패 (status=failed_window)"
    )
    failed_param = serializers.IntegerField(help_text="파라미터 오류로 실패 (status=failed_param)")
    failed_no_trace = serializers.IntegerField(
        help_text="200 접수 후 도착 미확인 (status=failed_no_trace)"
    )
    since = serializers.DateTimeField(
        help_text="집계 시작 시각 (ISO 8601). 이 시각 이후 created_at 로그만 집계."
    )


class _IgConnectionsMetricsSerializer(serializers.Serializer):
    """IG 계정 연동 상태별 카운트 (IGAccountConnection.Status)."""

    active = serializers.IntegerField(help_text="정상 연동 수 (status=active)")
    expired = serializers.IntegerField(help_text="토큰 만료 수 (status=expired)")
    revoked = serializers.IntegerField(help_text="연동 해제 수 (status=revoked)")
    error = serializers.IntegerField(help_text="오류 상태 수 (status=error)")


class _AttentionExpiredTokenSerializer(serializers.Serializer):
    """주의 — 토큰 만료된 IG 계정 1건."""

    id = serializers.UUIDField(help_text="IGAccountConnection PK")
    username = serializers.CharField(help_text="Instagram username")
    owner_email = serializers.EmailField(
        help_text="해당 계정이 속한 워크스페이스 소유자(owner) 이메일"
    )


class _AttentionLowDeliverySerializer(serializers.Serializer):
    """주의 — 최근 24h 도착률이 낮은 활성 IG 계정 1건."""

    id = serializers.UUIDField(help_text="IGAccountConnection PK")
    username = serializers.CharField(help_text="Instagram username")
    delivery_rate = serializers.FloatField(
        help_text="최근 24시간 도착률 (0~1). 임계값 0.9 미만이면 노출."
    )


class _AttentionMetricsSerializer(serializers.Serializer):
    """운영자가 즉시 조치해야 할 항목 모음."""

    expired_tokens = _AttentionExpiredTokenSerializer(
        many=True, help_text="토큰 만료된 IG 계정 (status=expired, 최대 50건)"
    )
    low_delivery_accounts = _AttentionLowDeliverySerializer(
        many=True,
        help_text=(
            "최근 24h 도착률 < 0.9 인 활성 IG 계정 (accepted-or-after >=1 건 보유, " "최대 50건)"
        ),
    )
    stuck_submitting = serializers.IntegerField(
        help_text="SUBMITTING 상태로 10분 넘게 정체된 DM 로그 수"
    )


class AdminMetricsOverviewSerializer(serializers.Serializer):
    """어드민 대시보드 단일 집계 응답.

    전역(GLOBAL) 집계 결과를 중첩 구조로 노출한다. 모든 카운트는 워크스페이스
    필터 없이 전수 집계된 값이다.
    """

    users = _UsersMetricsSerializer(help_text="회원 지표")
    workspaces = _WorkspacesMetricsSerializer(
        help_text="⚠️ DEPRECATED — 레거시 워크스페이스 플랜 분포. `subscriptions` 를 사용할 것."
    )
    subscriptions = _SubscriptionsMetricsSerializer(
        help_text="실제 구독(UserSubscription) 플랜별 분포 (동적 리스트)"
    )
    pages = _PagesMetricsSerializer(help_text="페이지 지표")
    campaigns = _CampaignsMetricsSerializer(help_text="자동 DM 캠페인 상태별 지표")
    dm = _DmMetricsSerializer(help_text="DM 발송 지표 (since 이후)")
    ig_connections = _IgConnectionsMetricsSerializer(help_text="IG 계정 연동 상태별 지표")
    attention = _AttentionMetricsSerializer(help_text="즉시 조치 필요 항목")
