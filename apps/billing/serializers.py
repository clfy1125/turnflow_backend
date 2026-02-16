"""
Billing serializers
"""

from rest_framework import serializers
from .models import UsageCounter, PlanLimits, PlanChoices


class PlanLimitSerializer(serializers.Serializer):
    """Serializer for plan limits"""

    comments_collected_per_month = serializers.IntegerField()
    dm_sent_per_month = serializers.IntegerField()
    workspaces = serializers.IntegerField()
    team_members = serializers.IntegerField()
    automations = serializers.IntegerField()


class CurrentPlanSerializer(serializers.Serializer):
    """Serializer for current plan information"""

    plan = serializers.ChoiceField(choices=PlanChoices.choices)
    plan_display = serializers.CharField()
    limits = PlanLimitSerializer()

    def to_representation(self, instance):
        """
        instance is expected to be a workspace
        """
        plan = instance.plan
        limits = PlanLimits.get_all_limits(plan)

        return {
            "plan": plan,
            "plan_display": dict(PlanChoices.choices).get(plan, "Unknown"),
            "limits": limits,
        }


class UsageSerializer(serializers.Serializer):
    """Serializer for usage data"""

    period = serializers.DictField(child=serializers.IntegerField())
    plan = serializers.ChoiceField(choices=PlanChoices.choices)
    usage = serializers.DictField(child=serializers.IntegerField())
    limits = serializers.DictField(child=serializers.IntegerField())
    remaining = serializers.DictField(child=serializers.IntegerField())


class UsageCounterSerializer(serializers.ModelSerializer):
    """Serializer for UsageCounter model"""

    workspace_id = serializers.UUIDField(source="workspace.id", read_only=True)
    workspace_name = serializers.CharField(source="workspace.name", read_only=True)

    class Meta:
        model = UsageCounter
        fields = [
            "id",
            "workspace_id",
            "workspace_name",
            "year",
            "month",
            "comments_collected",
            "dm_sent",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
