"""WS-2 ④-A — 전역 dedup 레저(DMDedupKey) + SentDMLog.create_idempotent() 테스트.

검증:
  - 신규: create_idempotent → (log, True) + SentDMLog 1행 + DMDedupKey 1행
  - 중복: 같은 idempotency_key 2회 → 2번째 (기존log, False), 새 행 안 생김
  - 무손실(orphan claim 없음): SentDMLog INSERT 가 실패하면(IntegrityError 아님) 같은
    트랜잭션이라 DMDedupKey claim 도 롤백 → 키가 남지 않음
  - 레저-우선 충돌: DMDedupKey 에 키가 이미 있으면(로그는 아카이브돼 없음) → (None, False) 로
    안전 스킵, SentDMLog 새 행 안 생김 (파티션 아카이브 후 재트리거 시나리오)

NOTE(test-db-not-clean): 내가 만든 행 기준으로만 단언(고유 키 사용).
NOTE(pytest-tests-prefix): tests_*.py 는 자동수집 안 됨 → 파일 경로 명시 실행.
"""

import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.integrations.models import (
    AutoDMCampaign,
    DMDedupKey,
    IGAccountConnection,
    SentDMLog,
)
from apps.workspace.models import Membership, Workspace


@pytest.fixture
def campaign(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.create_user(
        email=f"dd_{uuid.uuid4().hex[:8]}@example.com", password="pw12345!", full_name="DD"
    )
    ws = Workspace.objects.create(name="DD WS", slug=f"dd-{uuid.uuid4().hex[:8]}", owner=user)
    Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"ig_{uuid.uuid4().hex[:10]}",
        username="dduser",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=timezone.now(),
    )
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        trigger_type=AutoDMCampaign.TriggerType.ANY_MEDIA,
        name="dd-campaign",
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
    def test_first_create_makes_log_and_dedup_key(self, campaign):
        key = uuid.uuid4().hex
        log, created = SentDMLog.create_idempotent(idempotency_key=key, **_fields(campaign))
        assert created is True
        assert log is not None
        assert log.idempotency_key == key
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 1
        assert DMDedupKey.objects.filter(idempotency_key=key).count() == 1

    def test_duplicate_returns_existing_no_new_rows(self, campaign):
        key = uuid.uuid4().hex
        log1, c1 = SentDMLog.create_idempotent(idempotency_key=key, **_fields(campaign))
        log2, c2 = SentDMLog.create_idempotent(idempotency_key=key, **_fields(campaign))
        assert c1 is True and c2 is False
        assert log2 is not None and log2.id == log1.id
        # 새 행이 생기지 않았다
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 1
        assert DMDedupKey.objects.filter(idempotency_key=key).count() == 1

    def test_rollback_no_orphan_dedup_key_on_failure(self, campaign):
        """SentDMLog INSERT 가 실패(여기선 TypeError)하면 DMDedupKey claim 도 롤백돼야 한다.

        그렇지 않으면 키만 claim 되고 로그가 없어 그 DM 이 영구히 막힘(=손실).
        """
        key = uuid.uuid4().hex
        with pytest.raises(TypeError):
            SentDMLog.create_idempotent(
                idempotency_key=key, bogus_unexpected_field=1, **_fields(campaign)
            )
        # orphan claim 이 없어야 한다
        assert DMDedupKey.objects.filter(idempotency_key=key).count() == 0
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 0

    def test_ledger_hit_when_log_archived_skips_safely(self, campaign):
        """레저에 키가 이미 있고 로그는 없을 때(파티션 아카이브 후 재트리거) → 안전 스킵."""
        key = uuid.uuid4().hex
        DMDedupKey.objects.create(idempotency_key=key)  # 로그는 일부러 안 만듦
        log, created = SentDMLog.create_idempotent(idempotency_key=key, **_fields(campaign))
        assert created is False
        assert log is None  # 아카이브돼 없음 — 그래도 재발송 안 함
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 0

    def test_non_key_integrity_error_propagates_not_silent_dup(self, campaign):
        """키 충돌이 '아닌' 진짜 DB 오류(여기선 NOT NULL FK 위반)는 '중복'으로 둔갑하지 말고 전파해야 한다.

        무손실 핵심: 파티셔닝으로 SentDMLog 전역 UNIQUE 가 제거되면 create_idempotent 가 단일 보증.
        비-키 IntegrityError 를 (None, False)='중복' 으로 삼키면 발송 누락(silent drop)이 무성히 침투.
        → 예외가 전파되고(=에러/재시도로 가시화), claim 도 롤백돼 orphan 이 없어야 한다.
        """
        key = uuid.uuid4().hex
        fields = _fields(campaign)
        fields["campaign"] = None  # NOT NULL FK 위반 유도 (idempotency_key 충돌 아님)
        with pytest.raises(IntegrityError):
            # 바깥 atomic 으로 감싸 테스트 트랜잭션이 broken 상태로 남지 않게 한다.
            with transaction.atomic():
                SentDMLog.create_idempotent(idempotency_key=key, **fields)
        # '중복'으로 둔갑하지 않았고(예외 전파), claim 도 롤백돼 orphan 이 없다.
        assert DMDedupKey.objects.filter(idempotency_key=key).count() == 0
        assert SentDMLog.objects.filter(idempotency_key=key).count() == 0
