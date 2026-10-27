"""이미 저장된 social 블록의 SNS 값을 full URL 로 편다.

배경 — CS #69ad2ebe. 렌더러는 ``block.data[<플랫폼>]`` 을 그대로 ``<a href>`` 에
쓰는데, 에디터·AI 생성 경로가 핸들을 그대로 저장해 와서 스킴 없는 값이 **상대경로**
로 읽혔다 (``macaron3846`` → ``https://turnflow.link/macaron3846``).

쓰기 경로는 ``validators.validate_block_data`` 에서 막았고, 이 명령은 **그전에 쌓인
것**을 고친다. 판정은 같은 함수(``social_normalize``)를 쓰므로 화면·백필이 갈리지 않는다.

    manage.py backfill_social_links                # dry-run (기본)
    manage.py backfill_social_links --apply
    manage.py backfill_social_links --apply --page-id 2128

⚠️ 되돌리기 어려운 데이터 변경이라 **기본이 dry-run** 이다.
⚠️ ``updated_at`` 은 건드리지 않는다 — 시스템 백필이 사용자 눈에 '방금 수정됨'으로
   보이면 목록 정렬이 뒤집힌다(2026-10-11 에 같은 사고가 있었다).
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.pages.models import Block
from apps.pages.social_normalize import is_social_block, normalize_social_block_data


class Command(BaseCommand):
    help = "저장된 social 블록의 SNS 핸들을 full URL 로 정규화합니다 (기본 dry-run)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="실제로 저장합니다. 지정하지 않으면 무엇이 바뀔지 보여주기만 합니다.",
        )
        parser.add_argument(
            "--page-id",
            type=int,
            default=None,
            help="특정 페이지만 처리합니다 (검증용).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="처리할 블록 수 상한 (카나리아용).",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        page_id = options["page_id"]
        limit = options["limit"]

        qs = Block.objects.filter(data__is_social=True).select_related("page")
        if page_id is not None:
            qs = qs.filter(page_id=page_id)
        qs = qs.order_by("pk")

        scanned = 0
        changed_blocks = 0
        changed_values = 0
        touched_pages: set[int] = set()
        touched_users: set[int] = set()

        for block in qs.iterator():
            if not is_social_block(block.data):
                continue
            scanned += 1

            data = dict(block.data)
            changes = normalize_social_block_data(data)
            if not changes:
                continue

            changed_blocks += 1
            changed_values += len(changes)
            touched_pages.add(block.page_id)
            touched_users.add(block.page.user_id)

            self.stdout.write(f"block {block.pk} (page {block.page_id} /@{block.page.slug})")
            for field_id, old, new in changes:
                self.stdout.write(f"    {field_id}: {old!r} -> {new!r}")

            if apply_changes:
                with transaction.atomic():
                    # updated_at 은 auto_now 라 update_fields 에서 빼면 갱신되지 않는다.
                    Block.objects.filter(pk=block.pk).update(data=data)

            if limit is not None and changed_blocks >= limit:
                self.stdout.write(self.style.WARNING(f"--limit {limit} 도달 — 중단합니다."))
                break

        mode = "APPLIED" if apply_changes else "DRY-RUN (저장하지 않음)"
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"[{mode}]"))
        self.stdout.write(f"  검사한 social 블록 : {scanned}")
        self.stdout.write(f"  바뀐 블록          : {changed_blocks}")
        self.stdout.write(f"  바뀐 값            : {changed_values}")
        self.stdout.write(f"  영향 페이지        : {len(touched_pages)}")
        self.stdout.write(f"  영향 사용자        : {len(touched_users)}")
        if not apply_changes and changed_blocks:
            self.stdout.write("")
            self.stdout.write("  실제로 고치려면 --apply 를 붙이세요.")
