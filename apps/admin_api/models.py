"""apps/admin_api/models.py — 어드민 백오피스 전용 모델.

이 앱은 기존 도메인 모델(authentication / workspace / pages / integrations)을
**읽기/제어**하는 백오피스 API 를 제공한다. 도메인 데이터의 진실의 원천은 각 앱 모델이며,
여기서는 관리자 액션 감사 로그(:class:`AdminActionLog`) 한 개만 새로 정의한다.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class AdminActionLog(models.Model):
    """관리자(스태프)가 수행한 모든 변경(mutation)에 대한 감사 로그.

    - 조회(GET)는 기록하지 않는다. 상태를 바꾸는 PATCH/POST/DELETE 만 적재.
    - 개별 도메인 모델과 느슨하게 연결(``target_type`` + ``target_id`` 문자열)하여
      User(int) / Workspace(uuid) / Page(slug) 등 이종 PK 를 모두 수용한다.
    - 적재 실패가 본 요청을 깨지 않도록, 호출은 항상 ``apps.admin_api.audit.log_admin_action``
      헬퍼(try/except 래핑)를 통해서만 한다.
    """

    class Action(models.TextChoices):
        USER_UPDATE = "user.update", "회원 정보 수정"
        USER_PASSWORD_RESET = "user.password_reset", "회원 비밀번호 재설정 발송"
        USER_SUBSCRIPTION_UPDATE = "user.subscription_update", "회원 구독(요금제) 변경"
        WORKSPACE_UPDATE = "workspace.update", "워크스페이스 수정"
        MEMBERSHIP_UPDATE = "membership.update", "멤버 역할 변경"
        MEMBERSHIP_DELETE = "membership.delete", "멤버 제거"
        PAGE_UPDATE = "page.update", "페이지 차단/공개 변경"
        CAMPAIGN_PAUSE = "campaign.pause", "캠페인 일시중지"
        CAMPAIGN_RESUME = "campaign.resume", "캠페인 재개"
        DMLOG_RETRY = "dmlog.retry", "DM 재시도"
        DMLOG_REVERIFY = "dmlog.reverify", "DM 재검증"
        REFERRAL_CREATE = "referral.create", "레퍼럴 코드 생성"
        REFERRAL_UPDATE = "referral.update", "레퍼럴 코드 수정"
        REFERRAL_DELETE = "referral.delete", "레퍼럴 코드 삭제"

    class Meta:
        db_table = "admin_action_logs"
        verbose_name = "Admin Action Log"
        verbose_name_plural = "Admin Action Logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["actor"]),
            models.Index(fields=["action"]),
            models.Index(fields=["target_type", "target_id"]),
        ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_actions",
        verbose_name="수행 관리자",
    )
    action = models.CharField(max_length=64, choices=Action.choices, verbose_name="액션")
    target_type = models.CharField(max_length=32, blank=True, verbose_name="대상 종류")
    target_id = models.CharField(max_length=64, blank=True, verbose_name="대상 ID")
    target_repr = models.CharField(max_length=255, blank=True, verbose_name="대상 표시명")
    changes = models.JSONField(default=dict, blank=True, verbose_name="변경 내역(before/after)")
    request_id = models.CharField(max_length=64, blank=True, verbose_name="X-Request-ID")
    ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="요청 IP")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="생성 시각")

    def __str__(self) -> str:
        who = self.actor.email if self.actor_id else "(deleted)"
        return f"{who} · {self.action} · {self.target_type}:{self.target_id}"
