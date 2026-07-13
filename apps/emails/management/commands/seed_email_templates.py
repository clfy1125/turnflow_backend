"""
Seed / upgrade the built-in email templates.

Usage:
    python manage.py seed_email_templates           # create missing, leave edits alone
    python manage.py seed_email_templates --force   # overwrite all bodies with defaults

Template content lives in `apps/emails/templates_content.py` (Django-free) so the
standalone preview generator can share it. Edit designs there, then re-run with
`--force` to push the new design into the DB.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.emails.constants import AVAILABLE_VARIABLES
from apps.emails.models import EmailTemplate
from apps.emails.templates_content import DEFAULTS


class Command(BaseCommand):
    help = "Seed default email templates into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Overwrite subject/html_body of existing templates with defaults",
        )

    def handle(self, *args, force: bool = False, **opts):
        created, updated, skipped = 0, 0, 0
        for key, body in DEFAULTS.items():
            obj, was_created = EmailTemplate.objects.get_or_create(
                key=key,
                defaults={
                    "subject": body["subject"],
                    "html_body": body["html_body"],
                    "text_body": "",
                    "is_active": True,
                    "available_variables": AVAILABLE_VARIABLES.get(key, {}),
                },
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  + created  {key}"))
                continue

            if force:
                obj.subject = body["subject"]
                obj.html_body = body["html_body"]
                obj.available_variables = AVAILABLE_VARIABLES.get(key, {})
                obj.save(update_fields=["subject", "html_body", "available_variables", "updated_at"])
                updated += 1
                self.stdout.write(self.style.WARNING(f"  ~ overwrote {key}"))
            else:
                # Still keep the variable catalogue fresh
                if obj.available_variables != AVAILABLE_VARIABLES.get(key, {}):
                    obj.available_variables = AVAILABLE_VARIABLES.get(key, {})
                    obj.save(update_fields=["available_variables", "updated_at"])
                skipped += 1
                self.stdout.write(f"  = kept     {key} (admin edits preserved)")

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. created={created} overwritten={updated} preserved={skipped}"
            )
        )
