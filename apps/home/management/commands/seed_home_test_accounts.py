"""홈 화면(V2) 프론트 확인용 테스트 계정 시드 — dev 전용.

    python manage.py seed_home_test_accounts                # 전부 (43개)
    python manage.py seed_home_test_accounts --only H01,H02  # 일부만 재시드
    python manage.py seed_home_test_accounts --list          # 코드 목록만
    python manage.py seed_home_test_accounts --only A04 --ig-user-id 17841400000000000

상태 정의는 :mod:`apps.home.dev_seed_specs`, 작업대는 :mod:`apps.home.dev_seed`.
재시드는 그 계정의 데이터를 지우고 다시 만든다 — 알림을 닫았거나 버튼을 눌러 상태가
바뀌었어도 한 번 돌리면 원점으로 돌아간다.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.home import dev_seed
from apps.home.dev_seed_specs import SPECS


class Command(BaseCommand):
    help = "홈 화면 상태별 dev 테스트 계정 시드 (DEBUG=True 전용)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            default="",
            help="쉼표로 구분한 코드 목록 (예: H01,H02,P02). 생략하면 전부.",
        )
        parser.add_argument("--list", action="store_true", help="코드 목록만 출력하고 끝낸다")
        parser.add_argument(
            "--ig-user-id",
            default="",
            help="A04 전용 — '이미 다른 계정에 연결된' IG 계정의 user_id",
        )

    def handle(self, *args, **options):
        if options["list"]:
            for code, title, _fn in SPECS:
                self.stdout.write(f"  {code:<5} {title}")
            return

        codes = [c.strip() for c in options["only"].split(",") if c.strip()] or None
        try:
            results = dev_seed.run(codes, ig_user_id=options["ig_user_id"])
        except (RuntimeError, ValueError) as exc:
            raise CommandError(str(exc)) from None

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"=== {len(results)}개 시드 완료 ==="))
        for r in results:
            self.stdout.write(f"  {r.code:<5} {r.email:<36} {r.title}")
            self.stdout.write(f"        └ {r.summary}")
            for note in r.notes:
                self.stdout.write(self.style.WARNING(f"        ! {note}"))
        self.stdout.write("")
        self.stdout.write(f"비밀번호: 전부 '{dev_seed.PASSWORD}'")
        self.stdout.write(
            f"dev 제휴코드: {dev_seed.REFERRAL_CODE} (보너스 {dev_seed.REFERRAL_BONUS_DAYS}일)"
        )
        self.stdout.write("확인: POST /api/v1/auth/login/ → GET /api/v1/home/alerts/")
