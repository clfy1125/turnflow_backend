"""DM 캠페인 이전(다른 서비스에서 불러오기) — 프론트 UI 상태 검증용 더미 시더.

화면에 나올 수 있는 **모든 상태**를 계정별로 갈라 담는다. 프론트가 로그인만 바꿔 가며
각 상태를 바로 볼 수 있게 하는 것이 목적이라, 한 계정에 한 상태만 둔다.

  - ⚠️ DEBUG=True 가 아니면 실행 거부 (운영 데이터 보호).
  - 실제 인스타 호출 0건 · DM 발송 0건. 전부 DB 에만 만든다.
  - 재실행 안전(더미만 지우고 다시 만든다).

만드는 계정 (비밀번호는 전부 SEED_PASSWORD)

    mig-ready@turnflow.dev      분석 완료 — 후보 16개(자동채택 8 · 확인필요 5 · DM 못찾음 3)
                                가장 흔한 상태. 목록·필터·페이지네이션·일괄적용 화면용.
                                밴드 3종이 다 섞여 있다(excluded 는 이름·문구가 빈 후보).
    mig-running@turnflow.dev    분석 진행 중 — 진행률 42%, 예상시간 있음. 진행바 화면용.
    mig-estimating@turnflow.dev 예상시간 계산 중 — progress 8, estimate 아직 null.
                                (프론트의 null 체크가 되는지 확인용)
    mig-prefetched@turnflow.dev 연동 직후 **자동 선분석**이 이미 끝나 있는 상태.
                                "불러오기" 누르면 기다림 없이 결과가 뜨는 흐름 검증용.
    mig-partial@turnflow.dev    일부만 분석됨(partial) — 경고 배너 + 결과 동시 표시.
    mig-failed@turnflow.dev     토큰 오류로 실패 — 에러 화면/재시도 버튼용.
    mig-empty@turnflow.dev      분석은 끝났는데 **후보 0개** — 빈 상태 화면용.
    mig-fresh@turnflow.dev      잡이 아예 없음 — 설문/시작 화면용(prompt_answer 미응답).
    mig-firsttime@turnflow.dev  설문에 "처음이에요" 답함 — 안내가 다시 안 뜨는지 확인용.
    mig-applied@turnflow.dev    후보를 **전부 적용 완료**한 상태 — "이미 다 불러왔어요" 화면용.
                                (프론트가 다른 계정에서 apply 를 눌러 이 상태를 만들 필요가 없다)
    mig-bulk@turnflow.dev       **일시정지 캠페인 300개** — 캠페인 목록 스케일 확인용.
                                목록이 페이지네이션 없는 평면 배열이라 건수 많을 때를 봐야 한다.

사용 (dev 컨테이너 안):
    python manage.py seed_dm_migration_dummy            # 전체 생성
    python manage.py seed_dm_migration_dummy --cleanup  # 전체 제거
    python manage.py seed_dm_migration_dummy --only prefetched,partial
                                                        # 지정한 계정만 재생성
                                                        # (나머지 계정의 테스트 상태는 건드리지 않는다)
"""

from __future__ import annotations

import random
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.ai_jobs.services.dm_campaign_assistant import sample_replies
from apps.integrations.models import (
    AutoDMCampaign,
    DMCampaignCandidate,
    DMMigrationJob,
    IGAccountConnection,
)
from apps.workspace.models import Membership, Workspace

User = get_user_model()

SEED_PASSWORD = "Test1234!"  # noqa: S105 — dev 전용 테스트 계정
EMAIL_PREFIX = "mig-"
EMAIL_DOMAIN = "@turnflow.dev"
WS_SLUG_PREFIX = "mig-dummy-"
# 워크스페이스 UUID 를 계정 키로부터 **결정적으로** 만든다.
# 시더를 다시 돌려도 id 가 그대로라, 프론트에 전달한 문서/북마크가 썩지 않는다.
WS_NAMESPACE = uuid.UUID("d3f0a1c2-0000-4000-8000-000000000000")


def _ws_id(key: str) -> uuid.UUID:
    return uuid.uuid5(WS_NAMESPACE, f"dm-migration-dummy:{key}")


# (키, 이메일 로컬파트, 표시명, 설명)
ACCOUNTS = [
    ("ready", "ready", "이전-완료", "후보 13개 · 목록/필터/일괄적용"),
    ("running", "running", "이전-진행중", "진행률 42% · 예상시간 있음"),
    ("estimating", "estimating", "이전-예상계산중", "estimate 아직 null"),
    ("prefetched", "prefetched", "이전-선분석완료", "연동 직후 자동 분석이 끝나 있음"),
    ("partial", "partial", "이전-부분완료", "일부만 분석 + 결과 있음"),
    ("failed", "failed", "이전-실패", "토큰 오류"),
    ("empty", "empty", "이전-후보없음", "분석 끝, 후보 0개"),
    ("fresh", "fresh", "이전-시작전", "잡 없음 · 설문 미응답"),
    ("firsttime", "firsttime", "이전-처음이에요", "설문에 first_time 응답"),
    ("applied", "applied", "이전-전부적용됨", "후보 전부 applied · '이미 다 불러왔어요'"),
    ("bulk", "bulk", "캠페인-300개", "일시정지 캠페인 300개 · 목록 스케일 확인용"),
]
ACCOUNT_KEYS = [a[0] for a in ACCOUNTS]

_OFFERS = [
    ("가을 신상 룩북", "https://example.com/lookbook-2026fw", "룩북 받기", "룩북"),
    ("무료 체험 신청", "https://example.com/trial", "신청하기", "체험"),
    ("할인 쿠폰 코드", "https://example.com/coupon", "쿠폰 받기", "쿠폰"),
    ("사이즈 가이드", "https://example.com/size", "가이드 보기", "사이즈"),
    ("재입고 알림 신청", "https://example.com/restock", "알림 신청", "재입고"),
    ("스타일링 팁 PDF", "https://example.com/styling", "PDF 받기", "스타일"),
]


class Command(BaseCommand):
    help = "DM 캠페인 이전 — 프론트 UI 상태별 더미 데이터 생성 (dev 전용)"

    def add_arguments(self, parser):
        parser.add_argument("--cleanup", action="store_true", help="더미 데이터 삭제")
        parser.add_argument(
            "--only",
            default="",
            help=(
                "쉼표로 구분한 계정 키만 처리 (예: --only prefetched,partial). "
                "생략하면 전체. 지정하면 나머지 계정은 손대지 않는다."
            ),
        )

    def handle(self, *args, **opts):
        if not settings.DEBUG:
            raise CommandError("거부: DEBUG=True 환경에서만 실행 가능 (운영 데이터 보호).")
        accounts = self._select(opts["only"])
        if opts["cleanup"]:
            self._cleanup(accounts)
            return
        with transaction.atomic():
            self._cleanup(accounts, quiet=True)
            rows = [self._build(*a) for a in accounts]
        self._report(rows, partial=len(accounts) != len(ACCOUNTS))

    def _select(self, only: str):
        """--only 파싱. 잘못된 키는 조용히 넘기지 않고 바로 실패시킨다(오타로 아무것도 안 되는 사고 방지)."""
        if not only.strip():
            return ACCOUNTS
        # 프론트가 계정 이메일 기준으로 "mig-prefetched" 처럼 부르므로 접두사/도메인을 벗겨 받는다.
        keys = []
        for raw in only.split(","):
            k = raw.strip().split("@")[0]
            keys.append(k[len(EMAIL_PREFIX) :] if k.startswith(EMAIL_PREFIX) else k)
        keys = [k for k in keys if k]
        unknown = [k for k in keys if k not in ACCOUNT_KEYS]
        if unknown:
            raise CommandError(
                f"알 수 없는 계정 키: {', '.join(unknown)}\n사용 가능: {', '.join(ACCOUNT_KEYS)}"
            )
        return [a for a in ACCOUNTS if a[0] in keys]

    # ── 정리 ──
    def _cleanup(self, accounts, quiet=False):
        """대상 계정만 지운다. 워크스페이스 삭제가 연결/잡/후보/생성된 캠페인까지 캐스케이드한다."""
        emails = [f"{EMAIL_PREFIX}{a[1]}{EMAIL_DOMAIN}" for a in accounts]
        slugs = [f"{WS_SLUG_PREFIX}{a[1]}" for a in accounts]
        users = User.objects.filter(email__in=emails)
        n = users.count()
        Workspace.objects.filter(slug__in=slugs).delete()  # owner PROTECT → 유저보다 먼저
        users.delete()
        if not quiet:
            self.stdout.write(self.style.SUCCESS(f"더미 계정 {n}개 및 관련 데이터 삭제 완료"))

    # ── 공통 뼈대 ──
    def _build(self, key, local, name, note):
        email = f"{EMAIL_PREFIX}{local}{EMAIL_DOMAIN}"
        user = User.objects.create_user(email=email, password=SEED_PASSWORD)
        user.full_name = name
        # 이메일 인증 완료 처리 — 안 하면 프론트가 미인증으로 판단해 로그인 후 진입을 막는다.
        user.is_email_verified = True
        user.email_verified_at = timezone.now()
        user.save(update_fields=["full_name", "is_email_verified", "email_verified_at"])
        ws = Workspace.objects.create(
            id=_ws_id(key), name=f"[더미] {name}", slug=f"{WS_SLUG_PREFIX}{local}", owner=user
        )
        Membership.objects.create(workspace=ws, user=user, role=Membership.Role.OWNER)
        conn = IGAccountConnection.objects.create(
            workspace=ws,
            external_account_id=f"1784140000000{abs(hash(key)) % 10000:04d}",
            username=f"dummy_{local}",
            account_type="BUSINESS",
            status=IGAccountConnection.Status.ACTIVE,
            is_active=True,
            token_expires_at=timezone.now() + timedelta(days=50),
        )
        conn.access_token = f"mock_token_{uuid.uuid4().hex}"
        conn.save()

        job = getattr(self, f"_state_{key}")(conn)
        return (email, ws, conn, job, note)

    def _job(self, conn, **kw):
        kw.setdefault("media_limit", 50)
        kw.setdefault("llm_model", "deepseek")
        return DMMigrationJob.objects.create(ig_connection=conn, **kw)

    def _candidate(self, job, conn, i, *, band, confirm=False, drops=None, gate=True, url=True):
        title, offer_url, label, kw = _OFFERS[i % len(_OFFERS)]
        hits, probed = (8, 10) if band == "auto_draft" else (2, 10)
        score = 0.72 if band == "auto_draft" else 0.19
        return DMCampaignCandidate.objects.create(
            job=job,
            ig_connection=conn,
            status=DMCampaignCandidate.Status.DETECTED,
            band=band,
            media_id=f"dummy-media-{job.id.hex[:6]}-{i}",
            media_permalink=f"https://www.instagram.com/p/DUMMY{i}/",
            media_caption_excerpt=f"댓글에 '{kw}' 남겨주시면 바로 보내드려요! (더미 게시물 {i})",
            media_timestamp=timezone.now() - timedelta(days=3 * i + 1),
            suggested_keywords=[kw],
            suggested_keyword_mode="any",
            confidence=score,
            support_hits=hits,
            support_probed=probed,
            support_score=score,
            offer_url=offer_url if url else "",
            offer_button_label=label if url else "",
            gate_detected=gate,
            gate_message="팔로우 확인을 위해 아래 버튼을 눌러주세요." if gate else "",
            gate_button_label="팔로우 확인" if gate else "",
            confirm_required=confirm,
            transfer_drops=drops or [],
            draft_name=f"{title} 자동 DM",
            draft_description=f"'{kw}' 댓글에 반응해 {title}을(를) 보내는 캠페인입니다.",
            draft_opening_message=f"안녕하세요! 요청하신 {title} 보내드려요 😊 아래 버튼에서 확인해주세요.",
            # 실제 파이프라인과 같은 소스로 변주 50개 (프론트 목록/편집 화면 확인용).
            draft_public_reply_templates=sample_replies(50, seed=i),
            matched_template={"source": "support", "support_hits": hits, "support_probed": probed},
            evidence_aggregates={
                "matched_comment_count": 9,
                "total_comment_count": 40 + i,
                "keyword_hit_counts": {kw: 9},
                "repetition_ratio": 0.55,
                "dm_source": "targeted",
                "support_ratio": round(hits / probed, 2),
                "has_existing_campaign": False,
            },
            evidence_raw={
                "sample_outbound_dms": [
                    {
                        "text": f"요청하신 {title} 보내드려요! {offer_url}",
                        "created_time": "2026-07-01T05:00:00+0000",
                    }
                ]
            },
        )

    def _excluded_candidate(self, job, conn, i):
        """`excluded` 후보 — DM 은 못 찾았지만 캠페인 정황(캡션 트리거·반복)만 있는 게시물.

        실제 파이프라인 산출물과 **모양을 맞춘다**: 초안 생성 대상이 아니라서
        **이름·문구가 비어 있고**, 오퍼·게이트도 없다(프론트는 캡션 발췌로 폴백해야 한다).
        """
        _t, _u, _l, kw = _OFFERS[i % len(_OFFERS)]
        return DMCampaignCandidate.objects.create(
            job=job,
            ig_connection=conn,
            status=DMCampaignCandidate.Status.DETECTED,
            band=DMCampaignCandidate.Band.EXCLUDED,
            media_id=f"dummy-media-{job.id.hex[:6]}-x{i}",
            media_permalink=f"https://www.instagram.com/p/DUMMYX{i}/",
            media_caption_excerpt=f"'{kw}' 댓글 남겨주세요! (DM 기록을 못 찾은 더미 게시물 {i})",
            media_timestamp=timezone.now() - timedelta(days=40 + i * 5),
            suggested_keywords=[kw],
            suggested_keyword_mode="any",
            confidence=0.0,
            support_hits=0,
            support_probed=6,
            support_score=0.0,
            offer_url="",
            offer_button_label="",
            gate_detected=False,
            confirm_required=False,
            transfer_drops=[],
            draft_name="",  # ← 초안 없음(실제와 동일)
            draft_description="",
            draft_opening_message="",
            draft_public_reply_templates=sample_replies(50, seed=900 + i),
            matched_template={"source": "support", "support_hits": 0, "support_probed": 6},
            evidence_aggregates={
                "matched_comment_count": 7,
                "total_comment_count": 22 + i,
                "keyword_hit_counts": {kw: 7},
                "repetition_ratio": 0.61,
                "dm_source": "targeted",
                "support_ratio": 0.0,
                "has_existing_campaign": False,
            },
            evidence_raw={"sample_outbound_dms": []},
        )

    # ── 상태별 ──
    def _state_ready(self, conn):
        job = self._job(
            conn,
            status=DMMigrationJob.Status.READY,
            stage=DMMigrationJob.Stage.COMPLETED,
            progress=100,
            message="분석이 완료되었습니다.",
            estimated_seconds=252,
            estimate_detail={"seconds": 252, "seconds_max": 420, "media_with_comments": 42},
            estimated_at=timezone.now() - timedelta(minutes=10),
            started_at=timezone.now() - timedelta(minutes=10),
            finished_at=timezone.now() - timedelta(minutes=4),
            media_scanned=57,
            raw_expires_at=timezone.now() + timedelta(days=7),
        )
        for i in range(6):  # 자동채택 — 바로 만들어도 되는 것
            self._candidate(job, conn, i, band="auto_draft")
        for i in range(6, 10):  # 링크 확인이 필요한 것
            self._candidate(job, conn, i, band="needs_review", confirm=True)
        # 못 옮기는 항목이 있는 후보(사진 첨부·카드 넘김)
        self._candidate(
            job,
            conn,
            10,
            band="auto_draft",
            drops=[{"code": "attachment_image", "count": 2}, {"code": "carousel", "count": 1}],
        )
        # 링크 없이 게이트만 복원된 후보
        self._candidate(job, conn, 11, band="needs_review", confirm=True, url=False)
        # DM 을 못 찾은 게시물 3건 — band=excluded (이름·문구 비어 있음)
        for i in range(3):
            self._excluded_candidate(job, conn, i)
        # 이미 우리 캠페인이 있는 게시물 — existing_campaign 이 채워진다
        c = self._candidate(job, conn, 12, band="auto_draft")
        AutoDMCampaign.objects.create(
            ig_connection=conn,
            name="이미 쓰고 있는 캠페인",
            trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
            media_id=c.media_id,
            status=AutoDMCampaign.Status.PAUSED,
            message_template="이미 운영 중인 캠페인입니다.",
        )
        job.candidates_created = job.candidates.count()
        job.save(update_fields=["candidates_created"])
        return job

    def _state_running(self, conn):
        return self._job(
            conn,
            status=DMMigrationJob.Status.RUNNING,
            stage=DMMigrationJob.Stage.COLLECTING_TARGETED_DMS,
            progress=42,
            message="예전 DM을 찾고 있습니다... (18/42)",
            estimated_seconds=252,
            estimate_detail={"seconds": 252, "seconds_max": 420, "media_with_comments": 42},
            estimated_at=timezone.now() - timedelta(minutes=2),
            started_at=timezone.now() - timedelta(minutes=2),
            media_scanned=57,
        )

    def _state_estimating(self, conn):
        return self._job(
            conn,
            status=DMMigrationJob.Status.RUNNING,
            stage=DMMigrationJob.Stage.ESTIMATING,
            progress=8,
            message="예상 시간을 계산하고 있습니다...",
            started_at=timezone.now() - timedelta(seconds=5),
        )

    def _state_prefetched(self, conn):
        conn.dm_migration_prompt_answer = ""
        conn.save(update_fields=["dm_migration_prompt_answer"])
        job = self._job(
            conn,
            status=DMMigrationJob.Status.READY,
            stage=DMMigrationJob.Stage.COMPLETED,
            progress=100,
            message="분석이 완료되었습니다.",
            trigger_source="auto_connect",  # ← 연동 직후 자동 선분석
            estimated_seconds=180,
            estimate_detail={"seconds": 180, "seconds_max": 300, "media_with_comments": 30},
            estimated_at=timezone.now() - timedelta(hours=2),
            started_at=timezone.now() - timedelta(hours=2),
            finished_at=timezone.now() - timedelta(hours=1, minutes=55),
            media_scanned=34,
            raw_expires_at=timezone.now() + timedelta(days=7),
        )
        for i in range(4):
            self._candidate(job, conn, i, band="auto_draft")
        job.candidates_created = job.candidates.count()
        job.save(update_fields=["candidates_created"])
        return job

    def _state_partial(self, conn):
        job = self._job(
            conn,
            status=DMMigrationJob.Status.PARTIAL,
            stage=DMMigrationJob.Stage.COMPLETED,
            progress=100,
            message="일부만 분석했습니다 (일부 데이터 수집 실패).",
            estimated_seconds=600,
            estimate_detail={"seconds": 600, "seconds_max": 1000, "media_with_comments": 100},
            estimated_at=timezone.now() - timedelta(minutes=30),
            started_at=timezone.now() - timedelta(minutes=30),
            finished_at=timezone.now() - timedelta(minutes=2),
            media_scanned=100,
            rate_limit_pauses=3,
            raw_expires_at=timezone.now() + timedelta(days=7),
        )
        for i in range(3):
            self._candidate(job, conn, i, band="auto_draft")
        self._candidate(job, conn, 3, band="needs_review", confirm=True)
        job.candidates_created = job.candidates.count()
        job.save(update_fields=["candidates_created"])
        return job

    def _state_failed(self, conn):
        return self._job(
            conn,
            status=DMMigrationJob.Status.FAILED,
            stage=DMMigrationJob.Stage.COLLECTING_TARGETED_DMS,
            progress=35,
            message="분석에 실패했습니다.",
            error_code="token_expired",
            # ⚠️ 실제 파이프라인(pipeline.run_migration)이 넣는 문장과 **글자까지 같게** 유지할 것.
            # 예전엔 여기만 "(code=190)" 꼬리가 붙은 옛 문장이라, 실제 경로를 고친 뒤에도
            # 프론트가 더미를 보고 "아직 안 고쳐졌다" 고 판단했다.
            error_message="인스타 연결이 만료되었거나 권한이 없습니다. 계정을 다시 연결해주세요.",
            started_at=timezone.now() - timedelta(minutes=8),
            finished_at=timezone.now() - timedelta(minutes=6),
            raw_expires_at=timezone.now() + timedelta(days=7),
        )

    def _state_empty(self, conn):
        return self._job(
            conn,
            status=DMMigrationJob.Status.READY,
            stage=DMMigrationJob.Stage.COMPLETED,
            progress=100,
            message="분석이 완료되었습니다.",
            estimated_seconds=90,
            estimate_detail={"seconds": 90, "seconds_max": 150, "media_with_comments": 15},
            estimated_at=timezone.now() - timedelta(minutes=6),
            started_at=timezone.now() - timedelta(minutes=6),
            finished_at=timezone.now() - timedelta(minutes=4),
            media_scanned=15,
            candidates_created=0,
            raw_expires_at=timezone.now() + timedelta(days=7),
        )

    def _state_fresh(self, conn):
        return None  # 잡 없음 — 설문/시작 화면

    def _state_firsttime(self, conn):
        conn.dm_migration_prompt_answer = "first_time"
        conn.dm_migration_prompt_answered_at = timezone.now() - timedelta(days=1)
        conn.save(update_fields=["dm_migration_prompt_answer", "dm_migration_prompt_answered_at"])
        return None

    def _state_bulk(self, conn):
        """일시정지 캠페인 300개 — 목록 화면 스케일/검색/정렬/필터 확인용.

        캠페인 목록은 **페이지네이션이 없는 평면 배열**이라 건수가 많을 때 어떻게 보이는지
        프론트가 직접 확인해야 한다. 잡·후보는 만들지 않는다(이 계정은 목록 전용).

        ⚠️ ``thumbnail_sync_attempts`` 를 상한으로 채워 둔다. 안 그러면 목록을 열 때마다
        썸네일 동기화 태스크가 **300건** 큐에 들어간다(목업 토큰이라 전부 실패한다).
        """
        rng = random.Random(20260815)
        topics = [
            "가을 신상 룩북",
            "무료 체험 신청",
            "할인 쿠폰",
            "사이즈 가이드",
            "재입고 알림",
            "스타일링 팁",
            "브랜드 소개서",
            "제휴 문의",
            "이벤트 응모",
            "레시피 모음",
            "운동 루틴",
            "여행 코스",
            "인테리어 팁",
            "촬영 노하우",
            "부트캠프 안내",
        ]
        kws = ["룩북", "체험", "쿠폰", "사이즈", "재입고", "스타일", "자료", "신청", "정보", "가격"]
        now = timezone.now()
        rows = []
        for i in range(300):
            topic = topics[i % len(topics)]
            kw = kws[i % len(kws)]
            imported = i % 3 == 0  # 1/3 은 '불러온 캠페인' — source 배지/필터 확인용
            rows.append(
                AutoDMCampaign(
                    ig_connection=conn,
                    name=f"{topic} {i + 1:03d}차 안내",
                    description=f"'{kw}' 댓글에 반응하는 더미 캠페인 #{i + 1}",
                    trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
                    media_id=f"bulk-media-{conn.id.hex[:6]}-{i:03d}",
                    media_url=f"https://www.instagram.com/p/BULK{i:03d}/",
                    status=AutoDMCampaign.Status.PAUSED,  # 전부 일시정지
                    source="dm_migration" if imported else "",
                    keyword_filter=[kw],
                    keyword_mode="any",
                    message_template=f"요청하신 {topic} 보내드려요!",
                    opening_message_template=f"안녕하세요! 요청하신 {topic} 보내드려요 😊",
                    public_reply_enabled=i % 2 == 0,
                    public_reply_templates=sample_replies(10, seed=i) if i % 2 == 0 else [],
                    follow_gate_enabled=i % 5 == 0,
                    total_sent=rng.randint(0, 900),
                    total_failed=rng.randint(0, 40),
                    # 목록 조회가 썸네일 태스크를 300건 쏘지 않게 '영구 실패' 로 표시.
                    thumbnail_sync_attempts=AutoDMCampaign.THUMBNAIL_MAX_SYNC_ATTEMPTS,
                )
            )
        created = AutoDMCampaign.objects.bulk_create(rows, batch_size=100)
        # created_at 은 auto_now_add 라 생성 후 덮어쓴다 — 날짜 범위 필터/정렬 확인용으로 10개월에 분산.
        for i, c in enumerate(created):
            c.created_at = now - timedelta(days=i)
        AutoDMCampaign.objects.bulk_update(created, ["created_at"], batch_size=100)
        return None

    def _state_applied(self, conn):
        """후보를 **전부 적용 완료**한 상태 — apply/apply-all 이후의 종착 화면.

        프론트가 다른 더미 계정에서 apply 를 눌러 이 상태를 직접 만들면 그 계정이 소진된다.
        (실제로 그렇게 mig-prefetched/mig-partial 이 한 번 소진됐다.) 전용 계정으로 분리.
        """
        job = self._job(
            conn,
            status=DMMigrationJob.Status.READY,
            stage=DMMigrationJob.Stage.COMPLETED,
            progress=100,
            message="분석이 완료되었습니다.",
            estimated_seconds=200,
            estimate_detail={"seconds": 200, "seconds_max": 330, "media_with_comments": 33},
            estimated_at=timezone.now() - timedelta(hours=3),
            started_at=timezone.now() - timedelta(hours=3),
            finished_at=timezone.now() - timedelta(hours=2, minutes=55),
            media_scanned=38,
            raw_expires_at=timezone.now() + timedelta(days=7),
        )
        for i in range(5):
            cand = self._candidate(job, conn, i, band="auto_draft")
            campaign = AutoDMCampaign.objects.create(
                ig_connection=conn,
                name=cand.draft_name,
                trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
                media_id=cand.media_id,
                # 실제 apply 와 동일하게 **비활성**으로 만든다 — 사용자가 확인 후 직접 켜는 흐름이라
                # 프론트의 "켜기 전에 보기" 배지가 비활성일 때만 뜬다(active 로 두면 배지가 안 보인다).
                status=AutoDMCampaign.Status.INACTIVE,
                message_template=cand.draft_opening_message,
                source="dm_migration",
            )
            cand.status = DMCampaignCandidate.Status.APPLIED
            cand.applied_campaign = campaign
            cand.applied_at = timezone.now() - timedelta(hours=2, minutes=50 - i)
            cand.save(update_fields=["status", "applied_campaign", "applied_at"])
        job.candidates_created = job.candidates.count()
        job.save(update_fields=["candidates_created"])
        return job

    # ── 출력 ──
    def _report(self, rows, *, partial=False):
        w = self.stdout.write
        w("")
        w(self.style.SUCCESS("=" * 96))
        title = "DM 캠페인 이전 — UI 상태별 더미 계정 생성 완료"
        if partial:
            title += f" (지정한 {len(rows)}개만 재생성 · 나머지 계정은 그대로)"
        w(self.style.SUCCESS(title))
        w(self.style.SUCCESS("=" * 96))
        w(f"비밀번호(공통): {SEED_PASSWORD}")
        w("")
        w(f"{'이메일':<34}{'workspace_id':<38}{'상태'}")
        w("-" * 96)
        for email, ws, _conn, job, note in rows:
            state = f"{job.status}/{job.stage}" if job else "잡 없음"
            w(f"{email:<34}{str(ws.id):<38}{state}  — {note}")
        w("")
        w("호출 예시:")
        w("  GET  /api/v1/integrations/dm-migration/jobs/?workspace_id={workspace_id}")
        w("  GET  /api/v1/integrations/dm-migration/jobs/prompt-answer/?workspace_id={ws}")
        for email, ws, _c, job, _n in rows:
            if job:
                w(f"  # {email}")
                w(
                    f"  GET  /api/v1/integrations/dm-migration/jobs/{job.id}"
                    f"/candidates/?workspace_id={ws.id}&view=list"
                )
                break
        w("")
        w("제거: python manage.py seed_dm_migration_dummy --cleanup")
