"""apps/admin_api/serializers/snapshot.py — 전체 현황 타일의 명단 (SNAP-1/2).

``실제 결제 인원`` · ``프로 체험 인원`` 타일을 눌러 들어가는 명단의 행 스키마.
모수 판정은 :mod:`apps.admin_api.snapshot_rosters` (타일과 공유), 여기서는 표기 필드만.

금액은 **서버 계산값만** 내려보낸다 (USR-1 과 같은 이유) — 스냅샷가·추가 IG 계정·리텐션
할인 규칙이 프론트에 복제되면 화면 금액과 실제 청구액이 갈라지고, 그 차이는 회원이 결제
문자를 받아야 발견된다.

이 명단은 **최고 관리자 전용**이라 이메일 마스킹(RBAC-3)을 적용하지 않는다 — 마스킹하면
회원 식별이 안 돼 명단의 용도 자체가 사라진다. 접근 차단은 미들웨어가 경로 프리픽스로
처리한다(``/api/v1/admin/snapshot/**`` 는 marketing_viewer 화이트리스트에 없다 → 403).
"""

from __future__ import annotations

from rest_framework import serializers


class AdminPayingMemberSerializer(serializers.Serializer):
    """SNAP-1 — 실결제 회원 1행."""

    user_id = serializers.IntegerField(
        help_text="회원 상세(/admin/users/{id}/) 로 이동하는 키. 행 전체 클릭 대상"
    )
    email = serializers.CharField(help_text="회원 이메일 (이 명단은 마스킹하지 않는다)")
    full_name = serializers.CharField(help_text="회원 이름 (미입력이면 빈 문자열)")
    plan_name = serializers.CharField(help_text="현재 구독 플랜 코드명 (basic/pro)")
    plan_display_name = serializers.CharField(help_text="현재 구독 플랜 표시명 (베이직/프로)")
    monthly_amount = serializers.IntegerField(
        help_text="월 결제액(원) — `UserSubscription.renewal_amount` 서버 계산값. "
        "가입 시점 스냅샷가 + 추가 IG 계정 + 리텐션 할인(다음 1회)이 모두 반영된 값"
    )
    extra_ig_accounts = serializers.IntegerField(help_text="추가 IG 계정 수 (프로)")
    last_paid_at = serializers.DateTimeField(
        allow_null=True, help_text="최근 결제 완료 시각 (PaymentHistory status=paid 중 최신)"
    )
    next_billing_at = serializers.DateTimeField(
        allow_null=True, help_text="다음 결제 예정 (= current_period_end)"
    )
    paid_count = serializers.IntegerField(
        help_text="결제 완료 횟수 — **status=paid 인 건수**(환불된 건은 status 가 refunded 로 "
        "바뀌므로 자동 제외). 부분취소 음수 행도 제외"
    )
    date_joined = serializers.DateTimeField(help_text="가입일")


class AdminTrialMemberSerializer(serializers.Serializer):
    """SNAP-2 — 체험 회원 1행."""

    user_id = serializers.IntegerField(help_text="회원 상세로 이동하는 키")
    email = serializers.CharField(help_text="회원 이메일 (마스킹하지 않음)")
    full_name = serializers.CharField(help_text="회원 이름")
    plan_name = serializers.CharField(help_text="체험 중인 플랜 코드명")
    plan_display_name = serializers.CharField(help_text="체험 중인 플랜 표시명")
    trial_started_at = serializers.DateTimeField(
        allow_null=True,
        help_text="체험 시작 (`current_period_start` — 이번 체험 기간의 시작). "
        "`trial_used_at` 은 '1인 1회' 어뷰징 방어용 내구 필드라 재체험 이력이 섞일 수 있어 쓰지 않는다",
    )
    trial_ends_at = serializers.DateTimeField(
        allow_null=True, help_text="체험 종료 = 첫 결제 예정 (`current_period_end`)"
    )
    trial_total_days = serializers.IntegerField(
        allow_null=True, help_text="이번 체험의 총 일수 (쿠폰 보너스 포함 — 44 등)"
    )
    bucket = serializers.CharField(
        help_text="`will_charge`(기간말 과금 예정) / `cancelled`(체험 중 취소 — 과금 없음). "
        "서버 판정이 정본이며 프론트에서 재판정하지 않는다"
    )
    expected_amount = serializers.IntegerField(
        allow_null=True,
        help_text="체험 종료 후 결제 예정액(원, 서버 계산). `cancelled` 이면 null",
    )
    conversion_consent_required = serializers.BooleanField(
        help_text="유료전환 2차 동의 대기 중인가 (30일 초과 체험 + 미동의 + 30일 창 안). "
        "true 인데 동의가 안 들어오면 체험 종료 시 **결제되지 않고 무료 전환**된다"
    )
    card_company = serializers.CharField(help_text="카드사 (표시용, 이미 마스킹된 값)")
    card_number_masked = serializers.CharField(help_text="마스킹된 카드번호 (예: 433012******123*)")
    date_joined = serializers.DateTimeField(help_text="가입일")
