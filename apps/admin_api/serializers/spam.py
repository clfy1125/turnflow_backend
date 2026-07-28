"""apps/admin_api/serializers/spam.py — 어드민 스팸 차단 댓글 로그 시리얼라이저 (OPS-3).

``GET /api/v1/admin/spam/logs/`` 응답 문서화 전용 — 실제 직렬화는 뷰가 dict 로 조립한다
(cross-workspace 조인 + 커서 페이지네이션 때문).

기존 사용자용 ``/integrations/spam-filters/.../logs/`` 와 달리
- 워크스페이스 스코프가 아니라 **전역**,
- ``webhook_payload`` / ``api_response`` 원본은 **제외**(용량 + 불필요한 원본 PII),
- ``comment_text`` 는 500자 상한 + truncated 플래그.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.admin_api.serializers.dashboard_ops import _LinkHintSerializer

COMMENT_TEXT_MAX = 500


class AdminSpamLogRowSerializer(serializers.Serializer):
    """스팸 차단 댓글 로그 1행."""

    id = serializers.CharField(help_text="SpamCommentLog UUID")
    created_at = serializers.CharField(help_text="감지 시각 (Asia/Seoul ISO)")
    hidden_at = serializers.CharField(
        allow_null=True, help_text="숨김 처리 완료 시각 (Asia/Seoul ISO, 미처리면 null)"
    )
    status = serializers.CharField(
        help_text="detected(감지) | hidden(숨김 완료) | failed(숨김 실패). "
        "clean(정상, 멱등 장부)은 이 목록에서 항상 제외"
    )
    ig_username = serializers.CharField(allow_blank=True, help_text="대상 IG 계정 username")
    ig_connection_id = serializers.CharField(
        allow_blank=True, help_text="IG 연결 UUID (필터 ig_connection_id 와 동일 값)"
    )
    owner_email = serializers.CharField(allow_blank=True, help_text="워크스페이스 소유자 이메일")
    workspace_name = serializers.CharField(allow_blank=True, help_text="워크스페이스 이름")
    commenter_username = serializers.CharField(allow_blank=True, help_text="댓글 작성자 username")
    comment_id = serializers.CharField(allow_blank=True, help_text="IG 댓글 ID")
    comment_text = serializers.CharField(
        allow_blank=True, help_text=f"댓글 원문 (최대 {COMMENT_TEXT_MAX}자로 자름)"
    )
    comment_text_truncated = serializers.BooleanField(
        help_text=f"원문이 {COMMENT_TEXT_MAX}자를 넘어 잘렸는지"
    )
    media_id = serializers.CharField(allow_blank=True, help_text="게시물(미디어) ID")
    media_permalink = serializers.CharField(
        allow_blank=True,
        help_text="게시물 원본 URL — 같은 media_id 의 DM 캠페인이 보유한 permalink 를 "
        "best-effort 로 조인. 없으면 빈 문자열(스팸 로그 자체는 permalink 를 저장하지 않음)",
    )
    spam_reasons = serializers.ListField(
        child=serializers.CharField(),
        help_text="판정 이유 목록 (예: ['contains_url', 'keyword:수익인증'])",
    )
    spam_category = serializers.CharField(
        allow_blank=True, help_text="스팸 분류 (rule/scam/adult/phishing/promo/abuse 등)"
    )
    confidence = serializers.FloatField(allow_null=True, help_text="LLM 판정 신뢰도 0.0~1.0")
    engine = serializers.CharField(
        allow_blank=True, help_text="판정 엔진 (rule / llm / llm_failopen / rule_trivial 등)"
    )
    error_message = serializers.CharField(
        allow_blank=True, help_text="숨김 처리 실패 사유 (status=failed 일 때)"
    )
    link = _LinkHintSerializer(help_text="백오피스 드릴다운 힌트 ({page, params})")


class AdminSpamLogListSerializer(serializers.Serializer):
    """스팸 로그 목록 응답 — 커서 페이지네이션."""

    total = serializers.IntegerField(
        help_text="현재 필터 조건의 전체 건수(커서 무관). status 미지정 시 같은 기간 "
        "`dashboard/operations` 의 `spam.detected` 와 **일치**한다"
    )
    next_cursor = serializers.CharField(
        allow_null=True, help_text="다음 페이지 커서 (마지막 페이지면 null)"
    )
    results = AdminSpamLogRowSerializer(many=True, help_text="created_at desc 고정 정렬")
