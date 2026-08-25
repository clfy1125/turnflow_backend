"""인증 리다이렉트 리퍼러로 잘못 파생된 채널 교정 (2026-08-25).

배경
----
웹 구글 로그인은 ``accounts.google.com`` 으로 전체 페이지를 떠났다가 ``/login`` 으로
돌아온다. 그 순간 ``document.referrer`` 가 "https://accounts.google.com/" 이 되는데,
``derive_channel`` 의 REFERRER_CHANNEL_MAP 이 이를 ``google.com`` suffix 로 매칭해
**search_organic(자연 검색)** 으로 분류했다.

prod 실측(2026-08-25): 가입 귀속 160건 중 **105건**이 이 경로였고, 대시보드의
"검색 유입 가입 77건" 중 실제 검색은 5건뿐이었다.

채널은 **저장 시점에 확정**되므로 :data:`AUTH_REDIRECT_DOMAINS` 를 추가해도 과거 행은
그대로 남는다. 이 명령이 그 행들을 현재 규칙으로 다시 파생한다.

무엇을 고치고 무엇을 못 고치나
------------------------------
- ✅ ``channel`` 재파생 (search_organic → direct). 대시보드의 '검색' 줄이 정직해진다.
- ❌ **소실된 UTM 은 복구 불가.** 프론트가 덮어쓴 뒤 서버로 보낸 적이 없어 어디에도
  남아 있지 않다. 원천 차단은 프론트의 캡처 가드 (docs/frontend/UTM_ATTRIBUTION_FIX.md).

사용법::

    python manage.py fix_auth_referrer_channels            # dry-run (기본, 쓰기 없음)
    python manage.py fix_auth_referrer_channels --apply    # 실제 반영

``referrer`` 원문은 건드리지 않는다 — 사후 조사의 유일한 근거라 지우면 안 된다.
"""

from __future__ import annotations

from collections import Counter
from urllib.parse import urlparse

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.analytics.channels import AUTH_REDIRECT_DOMAINS, derive_channel
from apps.analytics.models import LandingVisit, SignupAttribution

# (모델, 화면 표기) — 두 테이블 모두 같은 derive_channel 로 채널을 파생한다.
_TARGETS = ((SignupAttribution, "가입 귀속"), (LandingVisit, "랜딩 방문"))


def _is_auth_referrer(referrer: str) -> bool:
    """리퍼러 호스트가 인증 제공자인가 (www./포트 제거 후 정확일치)."""
    if not referrer:
        return False
    try:
        host = urlparse(referrer).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        return False
    return host in AUTH_REDIRECT_DOMAINS


class Command(BaseCommand):
    help = "인증 리다이렉트(accounts.google.com 등) 리퍼러로 잘못 파생된 channel 을 재파생한다."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="실제로 DB 에 반영한다 (기본은 dry-run — 무엇이 바뀔지만 출력).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        mode = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(f"[{mode}] 인증 리다이렉트 리퍼러 채널 재파생")
        self.stdout.write(f"  대상 도메인: {', '.join(sorted(AUTH_REDIRECT_DOMAINS))}\n")

        grand_total = 0
        for model, label in _TARGETS:
            # 인덱스가 없는 컬럼이라 전체 스캔을 피하려고 referrer 비어있지 않은 것만 훑는다.
            rows = model.objects.exclude(referrer="").only(
                "id", "utm_source", "utm_medium", "referrer", "channel"
            )
            changes: list = []
            moves: Counter = Counter()
            for row in rows.iterator(chunk_size=2000):
                if not _is_auth_referrer(row.referrer):
                    continue
                new_channel = derive_channel(row.utm_source, row.utm_medium, row.referrer)
                old_channel = row.channel
                if new_channel == old_channel:
                    continue
                row.channel = new_channel
                changes.append(row)
                moves[(old_channel, new_channel)] += 1

            self.stdout.write(f"■ {label} ({model.__name__}) — 교정 대상 {len(changes)}건")
            for (old, new), n in moves.most_common():
                self.stdout.write(f"    {old} → {new}: {n}건")
            if not changes:
                self.stdout.write("    (변경 없음)")
                continue

            if apply_changes:
                with transaction.atomic():
                    model.objects.bulk_update(changes, ["channel"], batch_size=500)
                self.stdout.write(self.style.SUCCESS(f"    ✔ {len(changes)}건 반영 완료"))
            grand_total += len(changes)

        self.stdout.write("")
        if apply_changes:
            self.stdout.write(self.style.SUCCESS(f"총 {grand_total}건 교정 완료."))
            self.stdout.write(
                "⚠️ 마케팅 대시보드는 응답 캐시(기본 60초)를 쓴다 — 즉시 확인하려면 ?refresh=1"
            )
        else:
            self.stdout.write(f"총 {grand_total}건이 바뀝니다. 반영하려면 --apply 를 붙이세요.")
