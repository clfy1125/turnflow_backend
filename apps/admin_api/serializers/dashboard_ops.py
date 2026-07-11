"""apps/admin_api/serializers/dashboard_ops.py — 운영 대시보드 응답 시리얼라이저.

라우팅: ``GET /api/v1/admin/dashboard/operations/`` (``IsAdminUser``, is_staff=True).
이 모듈의 시리얼라이저는 **OpenAPI 응답 문서화 전용**이다 — 뷰가 만든 plain dict 의
중첩 구조를 drf-spectacular 스키마로 그대로 노출하기 위한 것이며, 실제 집계 로직은
:mod:`apps.admin_api.views.dashboard_ops` 가 담당한다.

임계값 상수(help_text 에 명시)는 :mod:`apps.admin_api.dashboard_constants` 가 단일
소스다 — 프론트 색상 매핑 계약. 집계 범위는 전 워크스페이스(GLOBAL).
"""

from __future__ import annotations

from rest_framework import serializers


class _LinkHintSerializer(serializers.Serializer):
    """백오피스 화면 라우팅 힌트 — 프론트가 드릴다운 링크를 구성하는 데 사용."""

    page = serializers.CharField(
        allow_null=True,
        help_text="백오피스 라우트 (예: /auto-dm/logs). null 이면 이동 화면 없음.",
    )
    params = serializers.DictField(
        help_text="쿼리 파라미터 힌트 (예: {status: failed_param, since: ISO}). "
        "expiring_within_hours 는 목록 API 필터 추가 전까지 안내용."
    )


class _DmSubsystemStatusSerializer(serializers.Serializer):
    """DM 발송 품질 서브시스템 신호등."""

    status = serializers.CharField(
        help_text="ok/warning/critical. rate < DM_DELIVERY_WARNING_THRESHOLD(0.9) → warning, "
        "rate < DM_DELIVERY_CRITICAL_THRESHOLD(0.75) → critical (strict <). "
        "표본 < DM_MIN_SAMPLE_FOR_STATUS(20) 이면 판정 안 함(ok)."
    )
    delivery_rate = serializers.FloatField(
        help_text="윈도우 도착률 (0~1). = (delivered+read) / "
        "(accepted+delivered+read+failed_no_trace)"
    )
    sample = serializers.IntegerField(help_text="판정 표본 수 (accepted_or_after)")
    insufficient_sample = serializers.BooleanField(
        help_text="표본 < DM_MIN_SAMPLE_FOR_STATUS(20) — true 면 status 는 항상 ok"
    )


class _IgSubsystemStatusSerializer(serializers.Serializer):
    """IG 연동 서브시스템 신호등."""

    status = serializers.CharField(
        help_text="ok/warning/critical. expired >= IG_EXPIRED_CRITICAL_COUNT(10) → critical, "
        "expired+expiring_24h >= 1 → warning."
    )
    expired = serializers.IntegerField(help_text="status=expired 연동 수 (전역)")
    expiring_24h = serializers.IntegerField(
        help_text="TOKEN_EXPIRING_SOON_HOURS(24h) 내 토큰 만료 예정 ACTIVE 연동 수 "
        "(token_expires_at <= now+24h, 경계 포함)"
    )


class _SpamSubsystemStatusSerializer(serializers.Serializer):
    """스팸 필터 서브시스템 신호등."""

    status = serializers.CharField(
        help_text="ok/warning/critical. 윈도우 내 숨김 실패(FAILED) >= "
        "SPAM_HIDE_FAILED_WARNING_COUNT(1) → warning, >= "
        "SPAM_HIDE_FAILED_CRITICAL_COUNT(10) → critical."
    )
    hide_failed = serializers.IntegerField(help_text="윈도우 내 숨김 처리 실패 건 수")


class _BillingSubsystemStatusSerializer(serializers.Serializer):
    """빌링 서브시스템 신호등."""

    status = serializers.CharField(
        help_text="ok/warning/critical. failed_payments >= PAYMENT_FAILED_WARNING_COUNT(1) "
        "or past_due >= 1 or webhook_backlog >= 1 → warning. "
        "past_due >= PAST_DUE_CRITICAL_COUNT(10) or "
        "WEBHOOK_BACKLOG_CRITICAL_MINUTES(30분)+ 미처리 웹훅 존재 → critical."
    )
    failed_payments = serializers.IntegerField(help_text="윈도우 내 FAILED 결제 건 수")
    past_due = serializers.IntegerField(help_text="past_due 구독 수 (전역)")
    webhook_backlog = serializers.IntegerField(
        help_text="WEBHOOK_BACKLOG_STALE_MINUTES(10분)+ 미처리 토스 웹훅 수"
    )


class _SubsystemStatusSerializer(serializers.Serializer):
    """서브시스템별 신호등 컨테이너."""

    dm = _DmSubsystemStatusSerializer(help_text="DM 발송 품질")
    ig_connections = _IgSubsystemStatusSerializer(help_text="IG 연동")
    spam_filter = _SpamSubsystemStatusSerializer(help_text="스팸 필터")
    billing = _BillingSubsystemStatusSerializer(help_text="빌링")


class _StatusSummarySerializer(serializers.Serializer):
    """신호등 요약 — 프론트 색상 매핑 계약 (dashboard_constants 도크스트링 참고)."""

    overall = serializers.CharField(help_text="worst-of(subsystems): ok < warning < critical")
    subsystems = _SubsystemStatusSerializer(help_text="서브시스템별 상태")


class _ActionItemSerializer(serializers.Serializer):
    """즉시 조치 항목 1건 — 고정 순서 배열의 원소 (count=0 도 포함)."""

    key = serializers.CharField(
        help_text="고정 키: expired_tokens / expiring_tokens_24h / failed_param_recent / "
        "failed_no_trace_recent / stuck_submitting / queued_window_risk / "
        "past_due_subscriptions / ig_activation_review / unprocessed_webhooks"
    )
    label = serializers.CharField(help_text="표시용 한국어 라벨")
    count = serializers.IntegerField(help_text="해당 항목 건 수")
    severity = serializers.CharField(help_text="count == 0 → ok, count >= 1 → warning")
    link = _LinkHintSerializer(help_text="드릴다운 화면 힌트")


class _DmSeriesBucketSerializer(serializers.Serializer):
    """DM 시계열 버킷 1개 (제로필 — 빈 구간도 0 으로 포함)."""

    ts = serializers.DateTimeField(help_text="버킷 시작 시각 (Asia/Seoul ISO 8601)")
    requested = serializers.IntegerField(help_text="버킷 내 생성된 DM 로그 수 (전 상태)")
    succeeded = serializers.IntegerField(help_text="delivered+read (+legacy sent)")
    failed = serializers.IntegerField(
        help_text="failed_token+failed_window+failed_param+failed_no_trace (+legacy failed)"
    )
    skipped = serializers.IntegerField(help_text="skipped (한도 초과 등)")


class _DmSeriesSerializer(serializers.Serializer):
    """DM 발송 시계열."""

    granularity = serializers.CharField(help_text='"hour" (24h/today) 또는 "5m" (1h 윈도우)')
    buckets = _DmSeriesBucketSerializer(many=True, help_text="제로필된 버킷 리스트 (ts 오름차순)")


class _DmQualitySerializer(serializers.Serializer):
    """DM 발송 품질 블록 (SentDMLog, created_at >= since)."""

    requested = serializers.IntegerField(help_text="윈도우 내 생성된 DM 로그 수 (전 상태)")
    succeeded = serializers.IntegerField(help_text="delivered+read (+legacy sent)")
    accepted_pending = serializers.IntegerField(
        help_text="accepted — Meta 접수 후 도착 미확정(in-flight)"
    )
    failed = serializers.IntegerField(
        help_text="failed_token+failed_window+failed_param+failed_no_trace (+legacy failed)"
    )
    skipped = serializers.IntegerField(help_text="skipped (한도 초과 등)")
    queued = serializers.IntegerField(help_text="queued (발송 대기)")
    submitting = serializers.IntegerField(help_text="submitting (API 호출 중)")
    delivery_rate = serializers.FloatField(
        help_text="(delivered+read) / (accepted+delivered+read+failed_no_trace) — "
        "dm-verification/stats 와 동일 공식"
    )
    series = _DmSeriesSerializer(help_text="시계열 (제로필)")


class _IgConnectionsSerializer(serializers.Serializer):
    """IG 연동 현황 (전역, 도넛 데이터)."""

    total = serializers.IntegerField(help_text="전체 연동 수")
    by_status = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="상태별 카운트 (키: active/expired/revoked/error)",
    )
    expiring_24h = serializers.IntegerField(
        help_text="TOKEN_EXPIRING_SOON_HOURS(24h) 내 토큰 만료 예정 ACTIVE 연동 수"
    )
    soft_deactivated = serializers.IntegerField(
        help_text="is_active=False (플랜 축소 등으로 소프트 비활성) 연동 수"
    )


class _SpamSeriesBucketSerializer(serializers.Serializer):
    """스팸 시계열 버킷 1개 (제로필)."""

    ts = serializers.DateTimeField(help_text="버킷 시작 시각 (Asia/Seoul ISO 8601)")
    detected = serializers.IntegerField(help_text="감지 건 수 (detected+hidden+failed)")
    hidden = serializers.IntegerField(help_text="숨김 처리 건 수 (hidden)")


class _SpamSeriesSerializer(serializers.Serializer):
    """스팸 필터 시계열."""

    granularity = serializers.CharField(help_text='"hour" 또는 "5m"')
    buckets = _SpamSeriesBucketSerializer(many=True, help_text="제로필된 버킷 리스트")


class _SpamTopCategorySerializer(serializers.Serializer):
    """스팸 분류 상위 항목 1건."""

    category = serializers.CharField(
        help_text='spam_category (rule/scam/adult/phishing/promo/abuse 등). 빈 문자열은 "uncategorized"'
    )
    count = serializers.IntegerField(help_text="윈도우 내 건 수")


class _SpamOpsSerializer(serializers.Serializer):
    """스팸 필터 운영 블록 (SpamCommentLog, created_at >= since)."""

    checked = serializers.IntegerField(help_text="윈도우 내 검사 전체 (CLEAN 포함)")
    detected = serializers.IntegerField(
        help_text="스팸 판정 건 (detected+hidden+failed, CLEAN 제외)"
    )
    hidden = serializers.IntegerField(help_text="숨김 처리 완료 건 (hidden)")
    failed = serializers.IntegerField(help_text="숨김 API 실패 건 (failed)")
    series = _SpamSeriesSerializer(help_text="시계열 (제로필)")
    top_categories = _SpamTopCategorySerializer(
        many=True, help_text="spam_category 상위 5 (스팸 판정 건 기준)"
    )


class _RecentErrorSerializer(serializers.Serializer):
    """최근 오류 1건 — DM 실패/결제 실패/스팸 숨김 실패 3종 병합."""

    type = serializers.CharField(help_text="dm_failure / payment_failure / spam_hide_failure")
    timestamp = serializers.DateTimeField(help_text="발생 시각 (Asia/Seoul ISO 8601)")
    subject = serializers.CharField(
        allow_blank=True,
        help_text="대상 식별자 — dm/spam: IG username, payment: 유저 이메일",
    )
    detail = serializers.CharField(allow_blank=True, help_text="오류 요약 (최대 200자)")
    ref_id = serializers.CharField(
        help_text="원본 레코드 PK (SentDMLog/PaymentHistory/SpamCommentLog)"
    )
    link = _LinkHintSerializer(help_text="드릴다운 화면 힌트 (spam 은 page=null)")


class _RiskMetricsSerializer(serializers.Serializer):
    """위험 계정 판정 근거 지표."""

    delivery_rate_24h = serializers.FloatField(
        allow_null=True, help_text="최근 24h 도착률 (표본 없으면 null)"
    )
    failed_param_24h = serializers.IntegerField(help_text="최근 24h failed_param 건 수")
    token_expires_at = serializers.DateTimeField(
        allow_null=True, help_text="토큰 만료 예정 시각 (없으면 null)"
    )
    status = serializers.CharField(help_text="IGAccountConnection.status")


class _RiskAccountSerializer(serializers.Serializer):
    """위험 계정 1건 — 스코어링 상위 RISK_ACCOUNTS_LIMIT(5)."""

    ig_connection_id = serializers.UUIDField(help_text="IGAccountConnection PK")
    username = serializers.CharField(allow_blank=True, help_text="Instagram username")
    owner_email = serializers.CharField(
        allow_blank=True, help_text="워크스페이스 소유자(owner) 이메일"
    )
    risk_score = serializers.IntegerField(
        help_text="token_expired=3 / critical_delivery_rate(<0.75)=3 / "
        "low_delivery_rate(<0.90)=2 / token_expiring_24h=2 / "
        "repeated_param_errors(failed_param>=RISK_REPEATED_PARAM_ERRORS_COUNT(5))=1 합산"
    )
    reasons = serializers.ListField(
        child=serializers.CharField(),
        help_text="enum: token_expired | token_expiring_24h | low_delivery_rate | "
        "critical_delivery_rate | repeated_param_errors",
    )
    metrics = _RiskMetricsSerializer(help_text="판정 근거 지표")


class AdminOpsDashboardSerializer(serializers.Serializer):
    """운영 대시보드 단일 집계 응답 (전 워크스페이스 GLOBAL, Redis 30s 캐시)."""

    window = serializers.CharField(help_text="적용된 집계 윈도우 (1h/24h/today)")
    since = serializers.DateTimeField(help_text="윈도우 시작 시각 (Asia/Seoul ISO 8601)")
    generated_at = serializers.DateTimeField(
        help_text="집계 생성 시각 — 캐시(OPS_DASHBOARD_CACHE_TTL=30s) 신선도 표시용"
    )
    status_summary = _StatusSummarySerializer(help_text="서브시스템 신호등 + overall")
    action_required = _ActionItemSerializer(
        many=True,
        help_text="고정 순서 조치 목록 (count=0 항목 포함 — 프론트 고정 레이아웃)",
    )
    dm_quality = _DmQualitySerializer(help_text="DM 발송 품질 (윈도우)")
    ig_connections = _IgConnectionsSerializer(help_text="IG 연동 현황 (전역)")
    spam = _SpamOpsSerializer(help_text="스팸 필터 운영 지표 (윈도우)")
    recent_errors = _RecentErrorSerializer(
        many=True, help_text="최근 오류 3종 병합 (timestamp desc, 최대 RECENT_ERRORS_LIMIT=20)"
    )
    risk_accounts = _RiskAccountSerializer(
        many=True, help_text="위험 계정 상위 RISK_ACCOUNTS_LIMIT=5 (score desc)"
    )
