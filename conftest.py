"""
Pytest configuration and fixtures
"""

import pytest
from django.conf import settings


@pytest.fixture(scope="session")
def django_db_setup():
    """Setup test database.

    ``DATABASES["default"]`` 를 통째로 덮어쓸 땐 ``ATOMIC_REQUESTS`` 같은
    Django 가 `connections.settings` 에서 직접 lookup 하는 키를 빼먹으면
    ``make_view_atomic`` 에서 ``KeyError`` 가 터진다 (DRF view 가 시작도 못함).
    Django 의 default ``DATABASES`` 채움 로직은 settings 첫 평가 시점에만
    돌므로, 여기선 명시적으로 핵심 키들을 같이 박아둔다.
    """
    settings.DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "test_instagram_service",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "localhost",
        "PORT": "5432",
        "ATOMIC_REQUESTS": False,
        "AUTOCOMMIT": True,
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "OPTIONS": {},
        "TIME_ZONE": None,
    }
