"""이미 탈퇴한 계정의 **카카오 앱 연결을 뒤늦게 끊는다** (1회성 정리 + 재시도용).

## 왜 필요한가

탈퇴 시 unlink 를 넣은 것은 2026-10-15 이고, 그전에 탈퇴한 사람들은 **카카오 쪽 연결이
그대로 남아 있다.** 그 상태로 재가입하면 카카오가 "이미 연결된 사용자"로 보고 **동의
화면을 건너뛰므로** 닉네임·이메일·전화번호를 받을 기회가 사라진다.

평소에도 쓸 데가 있다 — unlink 는 외부 호출이라 **실패해도 탈퇴를 막지 않게** 설계돼 있다.
그래서 네트워크 실패로 빠진 건이 남을 수 있고, 이 커맨드가 그 재시도 창구다.

## 대상

``is_pending_deletion`` (유예 중) 이면서 ``kakao_id`` 가 남아 있는 계정.
**이미 파기(purge)된 계정은 행이 없어 대상이 못 된다** — 그건 되돌릴 수 없다.

## 쓰는 법

```bash
# 먼저 대상만 세어 본다 (외부 호출 없음)
python manage.py kakao_unlink_deleted --dry-run

# 실제 실행 (기본 50건씩, 호출 간 0.2초)
python manage.py kakao_unlink_deleted --limit 50
```

⚠️ 카카오 호출이 사용자당 1회 나간다. 한 번에 다 돌리지 말고 ``--limit`` 로 나눠서
확인하며 진행할 것. 되돌릴 수 없는 동작이다.
"""

from __future__ import annotations

import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "탈퇴(유예 중) 계정의 카카오 앱 연결을 끊는다"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=50, help="한 번에 처리할 최대 건수 (기본 50)"
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="대상만 보여주고 호출하지 않는다"
        )
        parser.add_argument("--sleep", type=float, default=0.2, help="호출 간 대기 초 (기본 0.2)")

    def handle(self, *args, **opts):
        from apps.authentication.kakao import unlink_by_kakao_id

        User = get_user_model()
        qs = (
            User.objects.filter(deletion_requested_at__isnull=False)
            .exclude(kakao_id__isnull=True)
            .exclude(kakao_id="")
            .order_by("deletion_requested_at")
        )
        total = qs.count()
        self.stdout.write(f"유예 중 + 카카오 연결이 남은 계정: {total}건")

        if opts["dry_run"]:
            for u in qs[: opts["limit"]]:
                self.stdout.write(
                    f"  id={u.id} kakao_id={u.kakao_id} 탈퇴요청={u.deletion_requested_at:%Y-%m-%d}"
                )
            self.stdout.write(self.style.WARNING("dry-run — 아무것도 호출하지 않았다"))
            return

        ok = fail = 0
        for u in qs[: opts["limit"]]:
            if unlink_by_kakao_id(u.kakao_id):
                ok += 1
            else:
                fail += 1
                self.stdout.write(self.style.WARNING(f"  실패 id={u.id}"))
            if opts["sleep"]:
                time.sleep(opts["sleep"])

        self.stdout.write(self.style.SUCCESS(f"완료: 성공 {ok} / 실패 {fail}"))
        remaining = total - ok
        if remaining > 0:
            self.stdout.write(f"남은 대상(추정): {remaining}건 — 다시 실행하면 이어서 처리된다")
