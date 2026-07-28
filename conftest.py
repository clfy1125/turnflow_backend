"""
Pytest configuration and fixtures
"""

import os

import pytest
from django.conf import settings

# ─────────────────────────────────────────────────────────────────────────────
# 테스트가 만든 Celery 메시지를 **dev 워커로 보내지 않는다.**
#
# pytest 는 config.settings.local 을 쓰고 거기엔 CELERY_TASK_ALWAYS_EAGER 가 꺼져 있다
# (주석 처리). 그래서 .delay() 가 dev 스택과 **같은 Redis 브로커**로 실제 메시지를
# 발행하고, 돌고 있는 celery_worker 가 그걸 집어 실행했다:
#
#   - 유저를 만드는 테스트마다 emails.send_verification_email 이 큐로 갔다
#     (실측: 테스트 16개 → 32건). 유저 행은 테스트 트랜잭션과 함께 롤백되므로
#     "24시간 신규 가입 0명인데 인증메일 수백 건"이라는 유령 부하로 보인다.
#   - 워커가 그걸 처리하며 메모리를 쌓아 dev 박스를 굶기고, runserver 의 autoreload 가
#     OSError: Cannot allocate memory 로 죽어 API 가 502 가 됐다.
#
# memory:// 는 발행은 성공하지만 **소비자가 없어 아무도 실행하지 않는다** — 지금까지의
# 테스트 의미(fire-and-forget)를 그대로 보존한다. eager 로 바꾸면 뷰가 던지고 잊던
# 태스크가 인라인 실행돼 기존 단언들이 깨진다.
#
# ⚠️ **모듈 레벨이어야 한다.** conftest 는 pytest-django 가 Django 를 세팅하기 전에
# import 되므로 여기서 환경변수를 넣으면 base.py 의 config("CELERY_BROKER_URL") 이
# 이 값을 읽고, settings 와 app.conf 가 한 소스로 일치한다. fixture 안에서
# app.conf 를 고치는 방식은 이미 만들어진 conf/커넥션에 반영되지 않아 실패했다
# (실측: fixture 적용 후에도 broker 가 redis://redis:6379/0 였다).
# ─────────────────────────────────────────────────────────────────────────────
os.environ["CELERY_BROKER_URL"] = "memory://"
os.environ["CELERY_RESULT_BACKEND"] = "cache+memory://"


@pytest.fixture(scope="session", autouse=True)
def assert_celery_broker_isolated():
    """브로커 격리가 실제로 걸렸는지 하드 단언.

    이 격리가 풀리면 테스트는 계속 통과하고 **dev 워커만 조용히 얻어맞는다**(위 주석의
    502 사고). 조용한 실패라서 단언으로 못 박는다 — 누가 위 환경변수를 지우거나
    settings 로딩 순서가 바뀌면 여기서 즉시 터진다.
    """
    from config.celery import app

    broker = app.conf.broker_url or ""
    assert broker.startswith("memory://"), (
        f"테스트 Celery 브로커가 격리되지 않았습니다: {broker!r} — "
        "이 상태로 돌리면 테스트가 만든 태스크가 dev 워커로 흘러갑니다 (conftest.py 상단 참고)"
    )
    yield


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
