"""SentDMLog.create_idempotent() 테스트 — 전역 UNIQUE(idempotency_key) 기반 멱등 INSERT.

검증:
  - 신규: create_idempotent → (log, True) + SentDMLog 1행
  - 중복: 같은 idempotency_key 2회 → 2번째 (기존log, False), 새 행 안 생김
  - 무손실(부분행 없음): SentDMLog INSERT 가 실패하면(IntegrityError 아님=TypeError) 트랜잭션 롤백 → 부분 행 없음
  - 무손실(silent drop 방지): 키 충돌이 '아닌' 진짜 IntegrityError(NOT NULL FK)는 '중복'으로
    둔갑하지 않고 전파(에러/재시도로 가시화) — 파티셔닝 미적용으로 전역 UNIQUE 가 단일 보증이라 특히 중요.

NOTE(test-db-not-clean): 내가 만든 행 기준으로만 단언(고유 키 사용).
NOTE(pytest-tests-prefix): tests_*.py 는 자동수집 안 됨 → 파일 경로 명시 실행.
"""

import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.integrations.models import (
    AutoDMCampaign,
    IGAccountConnection,
    SentDMLog,
)
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def campaign(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"ci_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="CI"
    )
    ws = Workspace.objects.create(name="CI WS", slug=f"ci-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="ciuser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
        name="ci-campaign",
        message_template="안녕하세요!",
        status=AutoDMCampaign.Status.ACTIVE,
    )


def _fields(campaign, **kw):
    f = {
        "campaign": campaign,
        "comment_id": f"cmt_{uuid.uuid4().hex[:10]}",
        "comment_text": "가격 문의",
        "recipient_user_id": f"rcpt_{uuid.uuid4().hex[:8]}",
        "recipient_username": "buyer",
        "message_sent": "안녕하세요!",
        "status": SentDMLog.Status.QUEUED,
    }
    f.update(kw)
    return f


class TestCreateIdempotent:
    def test_first_create(self, campaign):
        key = uuid.uuid4().hex
        log, created = SentDMLog.create_idempotent(idempotency_key=key, **_fields(campaign))
        assert created is True
        assert log is not None and log.idempotency_key == key
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 1

    def test_duplicate_returns_existing_no_new_row(self, campaign):
        key = uuid.uuid4().hex
        log1, c1 = SentDMLog.create_idempotent(idempotency_key=key, **_fields(campaign))
        log2, c2 = SentDMLog.create_idempotent(idempotency_key=key, **_fields(campaign))
        assert c1 is True and c2 is False
        assert log2 is not None and log2.id == log1.id
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 1

    def test_rollback_no_partial_row_on_failure(self, campaign):
        """SentDMLog INSERT 가 실패(TypeError)하면 트랜잭션 롤백 → 부분 행이 남지 않는다."""
        key = uuid.uuid4().hex
        with pytest.raises(TypeError):
            SentDMLog.create_idempotent(
                idempotency_key=key, bogus_unexpected_field=1, **_fields(campaign)
            )
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 0

    def test_non_key_integrity_error_propagates_not_silent_dup(self, campaign):
        """키 충돌이 '아닌' 진짜 DB 오류(NOT NULL FK)는 '중복'으로 둔갑하지 말고 전파해야 한다.

        무손실 핵심: SentDMLog 가 파티셔닝 없이 전역 UNIQUE 단일 보증이므로, 비-키 IntegrityError 를
        (existing, False)='중복' 으로 삼키면 발송 누락(silent drop)이 무성히 침투. 같은 키 row 가
        없으면(=UNIQUE 충돌 아님) 예외가 전파되고(에러/재시도로 가시화), 부분 행도 없어야 한다.
        """
        key = uuid.uuid4().hex
        fields = _fields(campaign)
        fields["campaign"] = None  # NOT NULL FK 위반 유도 (idempotency_key 충돌 아님)
        with pytest.raises(IntegrityError):
            # 바깥 atomic 으로 감싸 테스트 트랜잭션이 broken 상태로 남지 않게 한다.
            with transaction.atomic():
                SentDMLog.create_idempotent(idempotency_key=key, **fields)
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 0
