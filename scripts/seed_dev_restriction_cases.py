"""dev 시드: 게시물 '연령 제한' 점검 UI 를 화면에서 확인하기 위한 계정·캠페인.

프론트가 `docs/frontend/CAMPAIGN_RESTRICTION_CHECK_FRONTEND.md` 를 구현할 때
**모든 state 를 실제로 볼 수 있게** 만든다. 판정은 전부 `SentDMLog` 이력에서 파생되므로
로그만 심으면 실제 API 가 그대로 판정한다(모킹 없음).

만들어지는 것
─────────────────────────────────────────────────────────────────────
계정  restriction@test.com / Test1234!
캠페인 5개 + 이력 없는 media 1개 → inspect 결과가 각각 다르게 나온다

  1. [ok]         정상 발송 캠페인            → 배지 없음
  2. [restricted] 제한 확정 (활성)            → 🔴 배너, 복사 시 409
  3. [restricted] 제한 확정 + 자동정지됨       → 🔴 "인스타그램 제한으로 자동 정지됨" 배지
  4. [suspected]  제한 의심 (조기경보)         → 🟡 경고 배너, 생성은 허용
  5. [ok]         실패가 섞였지만 최근 성공     → 배지 없음 (오탐 확인용)
  6. [unknown]    이력 없는 media (캠페인 없음) → 아무 것도 안 뜸

멱등: 다시 돌리면 같은 상태로 덮어쓴다(기존 캠페인·로그 삭제 후 재생성).

실행:
    docker compose exec -T web python manage.py shell < scripts/seed_dev_restriction_cases.py
"""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.integrations.ig_content_restriction import (
    HISTORY_MIN_FAILURES,
    RUNTIME_MIN_FAILURES,
    inspect_media,
)
from apps.integrations.models import AutoDMCampaign, IGAccountConnection, SeenComment, SentDMLog
from apps.workspace.models import Membership, Workspace

EMAIL = "restriction@test.com"
PASSWORD = "Test1234!"
SUB = "2534066"
OKS = SentDMLog.Status.DELIVERED
FAIL = SentDMLog.Status.FAILED_NO_TRACE

now = timezone.now()
U = get_user_model()

# ── 1. 유저 ───────────────────────────────────────────────────────────
user, created = U.objects.get_or_create(
    email=EMAIL, defaults={"full_name": "게시물제한 테스트", "is_active": True}
)
user.full_name = "게시물제한 테스트"
user.is_active = True
if hasattr(user, "is_email_verified"):
    user.is_email_verified = True
user.set_password(PASSWORD)
user.save()
print(f"USER {'created' if created else 'updated'}: {user.email}")

# ── 2. 워크스페이스 ────────────────────────────────────────────────────
ws = Workspace.objects.filter(owner=user).first()
if not ws:
    ws = Workspace.objects.create(
        name="게시물제한 테스트", slug=f"restrict-{uuid.uuid4().hex[:8]}", owner=user
    )
Membership.objects.get_or_create(workspace=ws, user=user, defaults={"role": Membership.Role.OWNER})
print(f"WORKSPACE: {ws.id}")

# ── 3. IG 연동 (mock) ─────────────────────────────────────────────────
conn = IGAccountConnection.objects.filter(workspace=ws).first()
if not conn:
    conn = IGAccountConnection.objects.create(
        workspace=ws,
        external_account_id=f"1784{uuid.uuid4().int % 10**13:013d}",
        username="restriction_demo",
        account_type="BUSINESS",
        status=IGAccountConnection.Status.ACTIVE,
        last_verified_at=now,
    )
conn.status = IGAccountConnection.Status.ACTIVE
conn.is_active = True
conn.last_verified_at = now
conn.token_expires_at = now + timedelta(days=60)
conn.access_token = "mock_token_restriction_demo"
conn.save()
print(f"IG CONNECTION: {conn.id} @{conn.username}")

# ── 4. 기존 시드 정리 (멱등) ────────────────────────────────────────────
old = AutoDMCampaign.objects.filter(ig_connection=conn)
old_media = list(old.values_list("media_id", flat=True))
SentDMLog.objects.filter(campaign__in=old).delete()
SeenComment.objects.filter(ig_connection=conn).delete()
n_del = old.count()
old.delete()
print(f"CLEANUP: 기존 캠페인 {n_del}개 · 로그 삭제")

# ── 헬퍼 ─────────────────────────────────────────────────────────────
PERMALINK = "https://www.instagram.com/reel/SEED{n}/"


def make_campaign(name, media_id, *, status=AutoDMCampaign.Status.ACTIVE, permalink=""):
    return AutoDMCampaign.objects.create(
        ig_connection=conn,
        name=name,
        media_id=media_id,
        media_url=permalink,
        message_template="안녕하세요! 신청하신 자료 보내드릴게요 🎁",
        status=status,
        started_at=now - timedelta(days=2),
        trigger_type=AutoDMCampaign.TriggerType.SPECIFIC_MEDIA,
    )


def add_logs(campaign, *, status, count, when, subcode=""):
    for _ in range(count):
        log = SentDMLog.objects.create(
            campaign=campaign,
            media_id=campaign.media_id,
            comment_id=f"c_{uuid.uuid4().hex[:14]}",
            recipient_user_id=f"u_{uuid.uuid4().hex[:14]}",
            recipient_username=f"tester_{uuid.uuid4().hex[:5]}",
            comment_text="참여",
            idempotency_key=uuid.uuid4().hex,
            status=status,
            error_subcode=subcode,
            error_message=(
                "액세스 토큰에 Instagram 비공개 답장에 대한 세분화된 Instagram 권한이 "
                "충분한지 확인하세요. 또는 댓글 ID가 유효한지 확인하세요 | "
                "http=403 | code=200 | subcode=2534066"
                if subcode == SUB
                else ""
            ),
            error_code="200" if subcode == SUB else "",
            dm_kind=SentDMLog.DMKind.OPENING,
        )
        SentDMLog.objects.filter(pk=log.pk).update(created_at=when)


def add_seen(media_id, source, count, when):
    for _ in range(count):
        s = SeenComment.objects.create(
            ig_connection=conn,
            comment_id=f"sc_{uuid.uuid4().hex[:14]}",
            media_id=media_id,
            source=source,
            expires_at=now + timedelta(days=10),
        )
        SeenComment.objects.filter(pk=s.pk).update(created_at=when)


made = []

# ── 5-1. [ok] 정상 캠페인 ──────────────────────────────────────────────
m1 = "17900000000000001"
c1 = make_campaign("정상 발송 캠페인 (배지 없음)", m1, permalink=PERMALINK.format(n="OK01"))
add_logs(c1, status=OKS, count=30, when=now - timedelta(hours=6))
add_seen(m1, SeenComment.Source.WEBHOOK, 30, now - timedelta(hours=6))
made.append(("ok 기대", c1))

# ── 5-2. [restricted] 제한 확정 · 활성 ─────────────────────────────────
m2 = "17900000000000002"
c2 = make_campaign("제한 확정 캠페인 (활성)", m2, permalink=PERMALINK.format(n="RS02"))
# 초반엔 웹훅으로 몇 건 성공 → 웹훅 끊김 → 폴러로만 들어오며 전부 실패 (실서버 지문 그대로)
add_seen(m2, SeenComment.Source.WEBHOOK, 8, now - timedelta(days=1, hours=2))
add_logs(c2, status=OKS, count=8, when=now - timedelta(days=1, hours=2))
add_seen(m2, SeenComment.Source.POLL, 40, now - timedelta(hours=10))
add_logs(c2, status=FAIL, count=40, when=now - timedelta(hours=10), subcode=SUB)
made.append(("restricted 기대", c2))

# ── 5-3. [restricted] 제한 확정 · 시스템 자동정지됨 ─────────────────────
m3 = "17900000000000003"
c3 = make_campaign(
    "자동 정지된 캠페인 (🔴 배지)",
    m3,
    status=AutoDMCampaign.Status.PAUSED,
    permalink=PERMALINK.format(n="RS03"),
)
add_seen(m3, SeenComment.Source.POLL, 25, now - timedelta(hours=8))
add_logs(c3, status=FAIL, count=25, when=now - timedelta(hours=8), subcode=SUB)
c3.auto_paused_at = now - timedelta(hours=5)
c3.auto_paused_reason = "post_restricted"
c3.save(update_fields=["auto_paused_at", "auto_paused_reason"])
made.append(("restricted + 자동정지 기대", c3))

# ── 5-4. [suspected] 제한 의심 (조기경보) ──────────────────────────────
m4 = "17900000000000004"
c4 = make_campaign("제한 의심 캠페인 (🟡 경고)", m4, permalink=PERMALINK.format(n="SP04"))
n_susp = HISTORY_MIN_FAILURES - 1  # 확정 임계 미만 · runtime 임계 이상
assert RUNTIME_MIN_FAILURES <= n_susp < HISTORY_MIN_FAILURES
add_seen(m4, SeenComment.Source.WEBHOOK, 2, now - timedelta(hours=4))
add_seen(m4, SeenComment.Source.POLL, n_susp, now - timedelta(hours=2))
add_logs(c4, status=FAIL, count=n_susp, when=now - timedelta(hours=2), subcode=SUB)
made.append(("suspected 기대", c4))

# ── 5-5. [ok] 실패가 섞였지만 최근 성공 (오탐 확인용) ────────────────────
m5 = "17900000000000005"
c5 = make_campaign("실패 섞였지만 정상 (오탐 확인)", m5, permalink=PERMALINK.format(n="OK05"))
add_seen(m5, SeenComment.Source.WEBHOOK, 20, now - timedelta(days=2))
add_logs(c5, status=FAIL, count=6, when=now - timedelta(days=2), subcode=SUB)
add_logs(c5, status=OKS, count=20, when=now - timedelta(hours=3))  # 마지막이 성공
made.append(("ok 기대 (오탐 아님)", c5))

# ── 5-6. 게이트 전용 media (캠페인 없음 — 중복 게이트에 안 걸리게) ──────
# 캠페인 생성 게이트를 시험하려면 그 media 에 **활성 캠페인이 없어야** 한다
# (있으면 중복 게이트가 먼저 409 를 낸다). 로그는 c1 에 붙이되 media_id 만 따로 준다.
m7 = "17900000000000007"  # restricted — 생성 시 409 나야 함
add_logs(c1, status=FAIL, count=12, when=now - timedelta(hours=9), subcode=SUB)
SentDMLog.objects.filter(campaign=c1, media_id=m1, error_subcode=SUB).update(media_id=m7)
add_seen(m7, SeenComment.Source.POLL, 12, now - timedelta(hours=9))

m8 = "17900000000000008"  # suspected — 생성은 통과해야 함 (경고만)
add_logs(c1, status=FAIL, count=n_susp, when=now - timedelta(hours=1), subcode=SUB)
SentDMLog.objects.filter(campaign=c1, media_id=m1, error_subcode=SUB).update(media_id=m8)
add_seen(m8, SeenComment.Source.POLL, n_susp, now - timedelta(hours=1))

# ── 6. 검증 — 실제 판정기를 돌려 기대와 맞는지 확인 ──────────────────────
print("\n=== 판정 결과 (실제 inspect_media 호출) ===")
for label, c in made:
    v = inspect_media(c.media_id)
    print(
        f"  {label:26s} → state={v.state:10s} source={v.source:8s} "
        f"blocking={str(v.blocking):5s} | {c.name}"
    )

m6 = "17900000000000006"
for label, mid in (
    ("unknown 기대 (이력 없음)", m6),
    ("restricted 기대 (게이트용)", m7),
    ("suspected 기대 (게이트용)", m8),
):
    v = inspect_media(mid)
    print(f"  {label:26s} → state={v.state:10s} | media_id={mid}")

print("\n=== 프론트 확인용 정보 ===")
print(f"  로그인       : {EMAIL} / {PASSWORD}")
print(f"  workspace_id : {ws.id}")
print(f"  ig_connection_id: {conn.id}")
print("\n  [1] 생성 전 점검 API — state 별로 확인")
for mid, exp in (
    (m1, "ok"),
    (m2, "restricted"),
    (m4, "suspected"),
    (m6, "unknown"),
):
    print(
        f"      GET /api/v1/integrations/auto-dm-campaigns/inspect-media/"
        f"?workspace_id={ws.id}&media_id={mid}   → {exp}"
    )
print("\n  [2] 캠페인 점검 API")
for label, c in made:
    print(f"      GET /api/v1/integrations/auto-dm-campaigns/{c.id}/inspect/   ({label})")
print("\n  [3] 409 재현 — 생성. ⚠️ 활성 캠페인이 없는 제한 media 를 써야 한다")
print("      (활성 캠페인이 있으면 중복 게이트가 먼저 409 duplicate_active_campaign 을 낸다)")
print(
    f"      POST /api/v1/integrations/auto-dm-campaigns/?workspace_id={ws.id}\n"
    f'           {{"ig_connection_id":"{conn.id}","name":"테스트",'
    f'"media_id":"{m7}","message_template":"안녕"}}   → 409 media_content_restricted'
)
print(f'      media_id="{m8}" 로 생성하면 201 — suspected 는 통과(경고만)')
print(f"      POST /api/v1/integrations/auto-dm-campaigns/{c2.id}/copy/   → 409 (복사 차단)")
print("\n  [4] 자동정지 배지")
print(f"      GET /api/v1/integrations/auto-dm-campaigns/?workspace_id={ws.id}")
print(f"      → id={c3.id} 행에 auto_paused_at 이 채워져 있음")
print("\n  [5] 생성 성공 (정상 경로)")
print(f'      media_id="{m6}" 로 생성하면 201 (이력 없는 새 게시물)')
