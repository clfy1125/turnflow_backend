"""어드민 2단계 인증 비상 초기화 — SSH 로 들어가서 쓰는 최후의 복구 수단.

## 언제 쓰나

인증앱을 지운 폰을 잃어버렸고 백업코드도 없을 때. 이 커맨드가 없으면 그 계정은 **영구히
잠긴다** — 슈퍼유저가 API 로 리셋해 줄 수 있지만, 슈퍼유저 본인이 잠기면 방법이 없다.
그래서 MFA 를 도입하는 배포와 **같은 배포**에 들어가야 한다.

    docker compose -f docker-compose.prod.yml exec web \\
        python manage.py admin_mfa_reset admin@turnflow.ai.kr

리셋 후 다음 로그인은 ``mfa_setup_required`` 로 떨어져 인증앱 등록부터 다시 한다.
기기 신뢰는 남긴다 — 분실한 것은 인증앱이지 기기가 아니다.

## 왜 확인 프롬프트가 없나

터미널 입력이 막힌 환경(비대화형 exec)에서도 돌아야 하기 때문이다. 대신 대상 이메일을
정확히 적어야 하고, 실행 결과를 감사 로그(AdminActionLog)에 남긴다.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.admin_api.auth.totp import reset_mfa
from apps.admin_api.models import AdminActionLog

User = get_user_model()


class Command(BaseCommand):
    help = "관리자 계정의 2단계 인증(TOTP)과 백업코드를 초기화한다 (비상 복구용)."

    def add_arguments(self, parser):
        parser.add_argument("email", help="초기화할 관리자 이메일")
        parser.add_argument(
            "--revoke-devices",
            action="store_true",
            help="신뢰 기기까지 전부 해제 (기기를 통째로 잃어버린 경우)",
        )

    def handle(self, *args, email: str, revoke_devices: bool = False, **opts):
        user = User.objects.filter(email__iexact=email.strip()).first()
        if user is None:
            raise CommandError(f"계정을 찾을 수 없습니다: {email}")
        if not user.is_staff:
            raise CommandError(f"스태프 계정이 아닙니다: {email}")

        reset_mfa(user)
        self.stdout.write(self.style.SUCCESS(f"  ~ 2단계 인증 초기화: {user.email}"))

        if revoke_devices:
            from django.utils import timezone

            from apps.admin_api.models import AdminDevice

            n = AdminDevice.objects.filter(user=user, revoked_at__isnull=True).update(
                revoked_at=timezone.now()
            )
            self.stdout.write(self.style.WARNING(f"  ~ 기기 {n}대 해제"))

        # request 가 없으므로 헬퍼(log_admin_action)를 쓰지 않고 직접 적재한다.
        AdminActionLog.objects.create(
            actor=None,  # CLI 실행 — 수행자를 특정할 수 없다
            action=AdminActionLog.Action.ADMIN_MFA_RESET,
            target_type="admin",
            target_id=str(user.pk),
            target_repr=user.email,
            changes={"via": "manage.py admin_mfa_reset", "revoke_devices": revoke_devices},
        )
        self.stdout.write("  → 다음 로그인에서 인증앱을 다시 등록하게 됩니다 (mfa_setup_required).")
