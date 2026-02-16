"""
Usage tracking and limit checking utilities
"""

from django.db import transaction
from rest_framework.exceptions import ValidationError
from apps.core.exceptions import PlanLimitExceededError
from .models import UsageCounter, PlanLimits


class UsageTracker:
    """
    Utility class for tracking and checking usage limits
    """

    @staticmethod
    def check_and_increment(workspace, metric: str, amount: int = 1) -> bool:
        """
        Check if usage is within limit and increment if allowed

        Args:
            workspace: Workspace instance
            metric: Metric name (e.g., 'comments_collected', 'dm_sent')
            amount: Amount to increment (default: 1)

        Returns:
            bool: True if successful

        Raises:
            PlanLimitExceededError: If limit would be exceeded
        """
        with transaction.atomic():
            counter = UsageCounter.get_current_period(workspace)

            # Check limit
            if not counter.check_limit(metric, amount):
                limit = PlanLimits.get_limit(workspace.plan, f"{metric}_per_month")
                current = getattr(counter, metric)
                raise PlanLimitExceededError(metric, limit, current, workspace.plan)

            # Increment
            counter.increment(metric, amount)
            return True

    @staticmethod
    def check_limit(workspace, metric: str, amount: int = 1) -> bool:
        """
        Check if usage increment would exceed limit (without incrementing)

        Args:
            workspace: Workspace instance
            metric: Metric name
            amount: Amount to check (default: 1)

        Returns:
            bool: True if within limit, False otherwise
        """
        counter = UsageCounter.get_current_period(workspace)
        return counter.check_limit(metric, amount)

    @staticmethod
    def get_usage(workspace, year: int = None, month: int = None) -> dict:
        """
        Get usage for a specific period or current month

        Args:
            workspace: Workspace instance
            year: Year (optional, defaults to current)
            month: Month (optional, defaults to current)

        Returns:
            dict: Usage data with metrics and limits
        """
        if year is None or month is None:
            counter = UsageCounter.get_current_period(workspace)
        else:
            counter, _ = UsageCounter.objects.get_or_create(
                workspace=workspace,
                year=year,
                month=month,
                defaults={"comments_collected": 0, "dm_sent": 0},
            )

        plan_limits = PlanLimits.get_all_limits(workspace.plan)

        return {
            "period": {"year": counter.year, "month": counter.month},
            "plan": workspace.plan,
            "usage": {
                "comments_collected": counter.comments_collected,
                "dm_sent": counter.dm_sent,
            },
            "limits": {
                "comments_collected_per_month": plan_limits.get("comments_collected_per_month"),
                "dm_sent_per_month": plan_limits.get("dm_sent_per_month"),
            },
            "remaining": {
                "comments_collected": counter.get_remaining("comments_collected"),
                "dm_sent": counter.get_remaining("dm_sent"),
            },
        }

    @staticmethod
    def increment_usage(workspace, metric: str, amount: int = 1):
        """
        Increment usage without checking limit (for admin/background tasks)

        Args:
            workspace: Workspace instance
            metric: Metric name
            amount: Amount to increment
        """
        counter = UsageCounter.get_current_period(workspace)
        counter.increment(metric, amount)


def require_usage_check(metric: str, amount: int = 1):
    """
    Decorator to check usage limits before executing a function

    Usage:
        @require_usage_check('comments_collected', 1)
        def collect_comment(workspace, comment_data):
            # function implementation
            pass
    """

    def decorator(func):
        def wrapper(*args, **kwargs):
            # Try to find workspace in args or kwargs
            workspace = None
            if args and hasattr(args[0], "workspace"):
                workspace = args[0].workspace
            elif "workspace" in kwargs:
                workspace = kwargs["workspace"]

            if workspace is None:
                raise ValueError("Workspace not found in function arguments")

            # Check and increment
            UsageTracker.check_and_increment(workspace, metric, amount)

            # Execute function
            return func(*args, **kwargs)

        return wrapper

    return decorator
