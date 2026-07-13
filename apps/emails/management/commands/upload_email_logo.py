"""
Upload the email header logo to media storage (R2) at a stable public key.

Usage:
    python manage.py upload_email_logo
    python manage.py upload_email_logo --src /path/to/logo.png --key branding/email-logo.png

Prints the public URL. Set `EMAIL_LOGO_URL` to that URL (settings default already
points at branding/email-logo.png on the R2 public domain, so re-uploading the same
key is usually enough — no env change needed).

Requires media storage to be R2/S3 (USE_R2=True); with local filesystem storage the
file is copied under MEDIA_ROOT and served at MEDIA_URL instead.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

DEFAULT_KEY = "branding/email-logo.png"
DEFAULT_SRC = Path(settings.BASE_DIR) / "apps" / "emails" / "branding" / "email-logo.png"


class Command(BaseCommand):
    help = "Upload the email header logo PNG to media storage (R2) and print its public URL."

    def add_arguments(self, parser):
        parser.add_argument("--src", default=str(DEFAULT_SRC), help="Local PNG path")
        parser.add_argument("--key", default=DEFAULT_KEY, help="Storage key (path in bucket)")

    def handle(self, *args, src: str, key: str, **opts):
        src_path = Path(src)
        if not src_path.exists():
            raise CommandError(f"로고 파일을 찾을 수 없습니다: {src_path}")

        data = src_path.read_bytes()
        # 고정 키로 덮어쓰기 (동일 키 재업로드 시 이름이 바뀌지 않도록 먼저 삭제)
        if default_storage.exists(key):
            default_storage.delete(key)
        saved_key = default_storage.save(key, ContentFile(data))
        url = default_storage.url(saved_key)

        self.stdout.write(self.style.SUCCESS(f"업로드 완료: {saved_key} ({len(data):,} bytes)"))
        self.stdout.write(f"공개 URL: {url}")
        if settings.EMAIL_LOGO_URL and settings.EMAIL_LOGO_URL.rstrip("/").endswith(saved_key):
            self.stdout.write(self.style.SUCCESS("EMAIL_LOGO_URL 이 이미 이 키를 가리킵니다 — 추가 설정 불필요."))
        else:
            self.stdout.write(
                self.style.WARNING(f"필요 시 .env 에 EMAIL_LOGO_URL={url} 을 설정하세요.")
            )
