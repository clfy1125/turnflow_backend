"""
Pytest configuration and fixtures
"""

import pytest
from django.conf import settings


@pytest.fixture(scope="session")
def django_db_setup():
    """Setup test database"""
    settings.DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "test_instagram_service",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "localhost",
        "PORT": "5432",
    }
