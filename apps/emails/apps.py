from django.apps import AppConfig


class EmailsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.emails"
    verbose_name = "Emails"

    def ready(self):
        # Wire signup signals on app ready
        from . import signals  # noqa: F401
