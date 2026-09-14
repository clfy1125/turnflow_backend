"""홈 알림 직렬화 — 응답 스키마 문서화용(값은 alerts.py 가 만든 dict 를 그대로 낸다)."""

from rest_framework import serializers


class HomeAlertSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="알림 머신 키. 문장은 프론트 i18n 이 만든다.")
    level = serializers.ChoiceField(
        choices=["critical", "warning", "todo"],
        help_text="critical=자동화가 멈춤 / warning=새는 중 / todo=권유",
    )
    rank = serializers.IntegerField(help_text="작을수록 위. 서버가 정한 노출 순서.")
    scope = serializers.ChoiceField(
        choices=["workspace", "ig_connection", "campaign", "page", "report"],
        help_text="target_id 가 무엇을 가리키는지",
    )
    target_id = serializers.CharField(allow_null=True, help_text="scope 대상 식별자")
    target_label = serializers.CharField(
        allow_blank=True, help_text="표시용 이름(@핸들·캠페인명). 없으면 빈 문자열"
    )
    dismissible = serializers.BooleanField(help_text="사용자가 닫을 수 있는가 (todo 만 true)")
    dismiss_key = serializers.CharField(
        allow_blank=True, help_text="닫기 요청에 그대로 넣을 대상 키"
    )
    since = serializers.DateTimeField(allow_null=True, help_text="이 상태가 시작된 시각(ISO8601)")
    data = serializers.DictField(help_text="문구에 넣을 숫자·시각. 코드마다 키가 다르다.")


class HomeAlertBlockingSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="강제 모달 코드")
    data = serializers.DictField()


class HomeAlertsResponseSerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
    workspace_id = serializers.CharField()
    blocking = HomeAlertBlockingSerializer(
        allow_null=True,
        help_text="null 이 아니면 **반드시 선택해야 넘어가는 모달**을 띄운다(배너 아님).",
    )
    alerts = HomeAlertSerializer(many=True)
    counts = serializers.DictField(child=serializers.IntegerField())


class HomeAlertDismissRequestSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="닫을 알림의 code")
    target_key = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="알림의 dismiss_key 를 그대로. 대상이 없으면 생략.",
    )
    workspace_id = serializers.UUIDField(
        required=False, help_text="워크스페이스가 여러 개일 때만 필수"
    )


class HomeAlertDismissResponseSerializer(serializers.Serializer):
    code = serializers.CharField()
    target_key = serializers.CharField(allow_blank=True)
    dismissed_at = serializers.DateTimeField()
