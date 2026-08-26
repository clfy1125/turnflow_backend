"""계측 공백기(2026-08-24~08-25) 메타 광고 가입의 소급 귀속 — **추정치 반영**.

⚠️⚠️ 이 명령이 쓰는 값은 **측정값이 아니라 추정값**이다. 그래서 반드시
``utm_content=ESTIMATE_MARKER`` 표식을 함께 남긴다. 표식 없이 넣으면 몇 주 뒤에
어느 행이 실측이고 어느 행이 추정인지 아무도 구별할 수 없게 된다.

배경
----
구글 로그인이 ``accounts.google.com`` 으로 전체 페이지를 왕복하는 사이 프론트의
``captureAttribution`` 이 저장된 UTM 을 덮어써서, 광고 유입 가입의 UTM 이 서버에 도달하지
못했다. 2026-08-25 14:20(백엔드) / ~19:00(프론트) 에 수정됐다.

**소실된 UTM 자체는 복구 불가**다 — 서버로 온 적이 없어 어디에도 남아 있지 않다.
그래서 "누가 메타였는지"는 직접 알 수 없고, 아래 근거로 **추정**한다.

선정 근거 (2026-08-26 실측)
---------------------------
1. 공백기 가입 18명을 전수 판정 → 제휴코드 1명, 방문기록으로 타채널 확정 7명을 제외한
   **10명**이 "버그 흔적을 가진 판별 불가" 군이었다
   (``visitor_id`` 없음 + ``referrer=accounts.google.com`` + ``landing_path=/login``
   = 인앱 브라우저에서 외부 브라우저로 탈출해 가입한 경로의 지문).
2. 누락 규모를 서로 독립인 세 방법으로 추정했고 **5.6 / 6.0 / 7.3 명**으로 수렴했다:
   - 수정 후 실측 전환율(4/113 = 3.54%) × 공백기 방문자 157명 = 5.6
   - 공백기 트래픽 중 메타 점유율(121/201 = 60%) × 판별불가 10명 = 6.0
   - 버그 지문 보유율의 기저 초과분(기저 16% → 공백기 59%) = 7.3
3. 10명 중 **누구인지**는 가입 시각 직전 트래픽의 메타 점유율로 갈랐다. 결과가 자의적이지
   않게 갈렸다 — 8/25 가입 6명은 직전 6시간 메타 점유율이 43~100%(user 218 은 38명 전원
   메타)인 반면, **8/24 가입 4명은 직전 24시간 메타 방문이 0명**이라 메타일 수가 없다.
   (8/24 의 메타 트래픽은 그날 저녁에야 시작됐고 이들은 새벽·오전 가입이다.)
4. 검산: 8/25 메타 방문 170명 × 3.54% = **기대 6.0명** → 선정 6명과 일치.

되돌리기
--------
``--revert`` 는 표식이 붙은 행의 UTM 을 지우고 ``derive_channel`` 로 채널을 다시 파생한다
(= ``fix_auth_referrer_channels`` 를 거친 직후 상태와 동일). 완전 가역이다.

사용법::

    python manage.py backfill_meta_gap_attribution                 # dry-run (기본)
    python manage.py backfill_meta_gap_attribution --apply
    python manage.py backfill_meta_gap_attribution --revert --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.analytics.channels import derive_channel
from apps.analytics.models import SignupAttribution

# 추정 표식 — 이 값이 실측과 추정을 가르는 유일한 근거다. 바꾸면 --revert 가 대상을 못 찾는다.
ESTIMATE_MARKER = "estimated_20260826"

# 이동 대상 (근거는 모듈 docstring §3). **명시 목록으로 못박는다** — 조건식으로 두면
# 나중에 데이터가 바뀌었을 때 다른 사람이 조용히 딸려 들어간다.
TARGET_USER_IDS = [216, 217, 218, 219, 220, 222]

# 의도적으로 **제외**한 사람들 (같은 판별불가 군이지만 8/24 새벽·오전 가입 →
# 직전 24시간 메타 방문 0명). 기록으로 남겨야 "왜 10명이 아니라 6명인가"에 답할 수 있다.
EXCLUDED_USER_IDS = [205, 206, 207, 209]

UTM = {
    "utm_source": "meta",
    "utm_medium": "cpc",
    "utm_campaign": "턴플로우 대행 프로젝트",
    "utm_content": ESTIMATE_MARKER,
}


class Command(BaseCommand):
    help = "계측 공백기 메타 광고 가입을 추정 근거로 소급 귀속한다 (표식 포함, 가역)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="실제 DB 반영 (기본은 dry-run)")
        parser.add_argument("--revert", action="store_true", help="표식이 붙은 행을 원상 복구한다")

    # ── 반영 ──────────────────────────────────────────────
    def _forward(self, apply_changes):
        rows = list(SignupAttribution.objects.filter(user_id__in=TARGET_USER_IDS))
        found = {r.user_id for r in rows}
        missing = set(TARGET_USER_IDS) - found
        if missing:
            raise CommandError(f"대상 사용자의 귀속 행이 없다: {sorted(missing)}")

        changes = []
        for row in rows:
            # 안전장치 — 실측 UTM 이 있는 행은 절대 덮어쓰지 않는다.
            if row.utm_source and row.utm_content != ESTIMATE_MARKER:
                raise CommandError(
                    f"user={row.user_id} 에 이미 실측 UTM({row.utm_source})이 있다. 중단한다."
                )
            self.stdout.write(
                f"  user={row.user_id:<4} {row.channel:<10} → meta_ads   "
                f"(ref={row.referrer[:34]!r})"
            )
            for k, v in UTM.items():
                setattr(row, k, v)
            row.channel = "meta_ads"
            changes.append(row)

        if apply_changes:
            with transaction.atomic():
                SignupAttribution.objects.bulk_update(
                    changes, ["channel", *UTM.keys()], batch_size=100
                )
        return len(changes)

    # ── 되돌리기 ──────────────────────────────────────────
    def _revert(self, apply_changes):
        rows = list(SignupAttribution.objects.filter(utm_content=ESTIMATE_MARKER))
        changes = []
        for row in rows:
            row.utm_source = row.utm_medium = row.utm_campaign = row.utm_content = ""
            # UTM 을 지운 뒤 리퍼러만으로 다시 파생 = 이 명령을 돌리기 직전 상태
            row.channel = derive_channel("", "", row.referrer)
            self.stdout.write(f"  user={row.user_id:<4} meta_ads → {row.channel}")
            changes.append(row)
        if apply_changes and changes:
            with transaction.atomic():
                SignupAttribution.objects.bulk_update(
                    changes, ["channel", *UTM.keys()], batch_size=100
                )
        return len(changes)

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        mode = "APPLY" if apply_changes else "DRY-RUN"

        if options["revert"]:
            self.stdout.write(f"[{mode}] 추정 귀속 되돌리기 (표식={ESTIMATE_MARKER})")
            n = self._revert(apply_changes)
            self.stdout.write(
                self.style.SUCCESS(f"\n{n}건 복구{'' if apply_changes else ' 예정'}.")
            )
            return

        self.stdout.write(f"[{mode}] 계측 공백기 메타 귀속 — **추정치**")
        self.stdout.write(f"  표식: utm_content={ESTIMATE_MARKER}")
        self.stdout.write(f"  대상 {len(TARGET_USER_IDS)}명: {TARGET_USER_IDS}")
        self.stdout.write(
            f"  제외 {len(EXCLUDED_USER_IDS)}명(직전 24h 메타 0): {EXCLUDED_USER_IDS}\n"
        )
        n = self._forward(apply_changes)
        self.stdout.write("")
        if apply_changes:
            self.stdout.write(self.style.SUCCESS(f"{n}건 반영 완료."))
            self.stdout.write("되돌리기: --revert --apply")
            self.stdout.write("⚠️ 대시보드 캐시 60초 — 즉시 확인은 ?refresh=1")
        else:
            self.stdout.write(f"{n}건이 바뀝니다. 반영하려면 --apply.")
