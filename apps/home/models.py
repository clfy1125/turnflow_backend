"""홈 알림 — 사용자가 닫은(확인한) 항목 기록.

알림 자체는 **쌓지 않는다**(§ alerts.py 참고). 지금 상태를 매번 계산하므로 이벤트 테이블이
없고, 해소되면 배너가 저절로 사라진다. 다만 「권유」 등급 알림은 사용자가 한 번 닫으면 다시
뜨면 안 되므로 **닫았다는 사실만** 서버에 남긴다.

⚠️ 브라우저(localStorage)에 두면 안 되는 이유: PC 에서 건너뛴 게시물이 폰·앱에서 다시 맨 위에
뜬다. 계정 단위 사실이라 서버가 맞다(프론트 요청 N4 와 같은 요구).
"""

from django.conf import settings
from django.db import models


class HomeAlertDismissal(models.Model):
    """사용자가 닫은 홈 알림 1건.

    ``target_key`` 는 "무엇을 닫았는가"를 가리키는 대상 식별자다. 같은 코드라도 대상이 달라지면
    다시 떠야 한다 — 예를 들어 게시물 A 를 건너뛰어도 새 게시물 B 는 다시 떠야 한다.
    대상이 없는 알림(코드 전체를 닫는 것)은 빈 문자열을 쓴다.
    """

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="home_alert_dismissals",
        verbose_name="사용자",
    )
    workspace = models.ForeignKey(
        "workspace.Workspace",
        on_delete=models.CASCADE,
        related_name="home_alert_dismissals",
        verbose_name="워크스페이스",
    )
    code = models.CharField(
        max_length=64,
        verbose_name="알림 코드",
        help_text="apps.home.alerts 의 머신 키 (예: recent_post_no_campaign)",
    )
    target_key = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="대상 식별자",
        help_text="게시물 id · 페이지 id · 리포트 id 등. 대상이 없으면 빈 문자열.",
    )
    dismissed_at = models.DateTimeField(auto_now_add=True, verbose_name="닫은 시각")

    class Meta:
        db_table = "home_alert_dismissals"
        verbose_name = "홈 알림 닫음"
        verbose_name_plural = "홈 알림 닫음 목록"
        ordering = ["-dismissed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "workspace", "code", "target_key"],
                name="uq_home_alert_dismissal",
            )
        ]
        indexes = [
            models.Index(fields=["user", "workspace"]),
        ]

    def __str__(self):
        target = f":{self.target_key}" if self.target_key else ""
        return f"{self.code}{target} ({self.user_id})"
