"""
테스트용 계정을 생성(또는 갱신)하는 관리 명령.

이메일 인증(`is_email_verified=True`, `email_verified_at=now`)까지 완료된 상태로
즉시 로그인 가능한 계정을 만든다. 이미 같은 이메일이 있으면 비밀번호를 재설정하고
인증 상태를 맞춰주므로 몇 번을 실행해도 안전(idempotent)하다.

Usage:
    python manage.py create_test_user
    python manage.py create_test_user --email tester@turnflow.test --password "Test1234!" --name "QA 테스터"
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

User = get_user_model()

DEFAULT_EMAIL = "tester@turnflow.test"
DEFAULT_PASSWORD = "Test1234!"
DEFAULT_NAME = "QA 테스터"


class Command(BaseCommand):
    help = "이메일 인증이 완료된 테스트 계정을 생성하거나 갱신합니다."

    def add_arguments(self, parser):
        parser.add_argument("--email", default=DEFAULT_EMAIL, help="테스트 계정 이메일")
        parser.add_argument("--password", default=DEFAULT_PASSWORD, help="테스트 계정 비밀번호")
        parser.add_argument("--name", default=DEFAULT_NAME, help="표시 이름(full_name)")

    def handle(self, *args, **options):
        email = User.objects.normalize_email(options["email"])
        password = options["password"]
        name = options["name"]
        now = timezone.now()

        user = User.objects.filter(email__iexact=email).first()

        if user is None:
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=name,
                is_active=True,
                is_email_verified=True,
                email_verified_at=now,
            )
            created = True
        else:
            # 이미 있으면 로그인 가능한 상태로 맞춰준다.
            user.set_password(password)
            user.full_name = name or user.full_name
            user.is_active = True
            user.is_email_verified = True
            if user.email_verified_at is None:
                user.email_verified_at = now
            user.save()
            created = False

        action = "생성" if created else "갱신"
        self.stdout.write(self.style.SUCCESS(f"테스트 계정 {action} 완료"))
        self.stdout.write(f"  email          : {user.email}")
        self.stdout.write(f"  password       : {password}")
        self.stdout.write(f"  full_name      : {user.full_name}")
        self.stdout.write(f"  is_active      : {user.is_active}")
        self.stdout.write(f"  email_verified : {user.is_email_verified} ({user.email_verified_at})")
