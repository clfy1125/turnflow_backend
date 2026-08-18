"""이미 나가고 있는 DM 의 ``[링크]`` **글자**를 고친다.

무슨 일이 있었나 — 이전 초안 생성은 LLM 이 URL 을 상상하지 않도록 본문에 ``[링크]``
자리표시자를 쓰게 되어 있었다(``llm.py`` 프롬프트). **그런데 그것을 치환하는 코드가 어디에도
없었다.** 발송 경로(``AutoDMCampaign.get_opening_message``)는 템플릿을 그대로 내보내므로
수신자가 "[링크]" 라는 글자를 그대로 봤다.

실측(2026-08-19 prod): 후보 1,829건 · **active 캠페인 42개** · 실제 발송 2건(둘 다 읽음).

무엇으로 바꾸나 — ``[링크]`` → ``링크`` (대괄호만 뗀다). 이유는
:func:`~apps.integrations.dm_migration.analyze.unwrap_link_placeholder` 참조. 링크는
이미 **버튼**이 나르므로 "아래 링크를 눌러" 가 사실이 되고, 조사도 깨지지 않는다.

⚠️ ``--apply`` 는 **살아있는 캠페인의 발송 문구를 바꾼다.** 미리보기로 먼저 확인할 것.
   버튼 URL 이 없는 캠페인은 본문에 링크가 아예 없다는 뜻이라 따로 표시한다(그건 문구
   수정만으로 해결되지 않는다).

사용법::

    manage.py dm_migration_fix_link_placeholder                      # 전체 미리보기
    manage.py dm_migration_fix_link_placeholder --username 3dragon_pd
    manage.py dm_migration_fix_link_placeholder --apply              # 반영
    manage.py dm_migration_fix_link_placeholder --candidates-only --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.integrations.dm_migration.analyze import unwrap_link_placeholder
from apps.integrations.models import AutoDMCampaign, DMCampaignCandidate

# 캠페인에서 손볼 문구 필드 — 전부 수신자에게 그대로 나가는 것들.
CAMPAIGN_FIELDS = (
    "opening_message_template",
    "message_template",  # legacy 별칭 — 옛 캠페인은 여기에만 값이 있다
    "reward_message_template",
    "follow_gate_retry_message",
    "public_reply_template",  # deprecated 단일값
)
CAMPAIGN_LIST_FIELDS = (
    "opening_message_templates",
    "follow_gate_prompt_templates",
    "public_reply_templates",
    "recovery_reply_templates",
)
CANDIDATE_FIELDS = ("draft_opening_message", "gate_message")
CANDIDATE_LIST_FIELDS = ("draft_public_reply_templates", "follow_up_candidates")


def _fix_obj(obj, fields, list_fields) -> list[str]:
    """객체의 문구 필드를 고치고 **바뀐 필드 이름**을 돌려준다."""
    changed = []
    for f in fields:
        old = getattr(obj, f, None)
        if not isinstance(old, str) or not old:
            continue
        new = unwrap_link_placeholder(old)
        if new != old:
            setattr(obj, f, new)
            changed.append(f)
    for f in list_fields:
        old = getattr(obj, f, None)
        if not isinstance(old, list) or not old:
            continue
        new = [unwrap_link_placeholder(x) if isinstance(x, str) else x for x in old]
        if new != old:
            setattr(obj, f, new)
            changed.append(f)
    return changed


def _button_url(c: AutoDMCampaign) -> str:
    lb = c.link_buttons or []
    if isinstance(lb, list) and lb and isinstance(lb[0], dict) and lb[0].get("url"):
        return str(lb[0]["url"])
    return str(getattr(c, "link_button_url", "") or "")


class Command(BaseCommand):
    help = "이전으로 만든 DM 문구의 '[링크]' 자리표시자를 '링크' 로 고친다"

    def add_arguments(self, parser):
        parser.add_argument("--username", help="이 IG 계정만")
        parser.add_argument(
            "--candidates-only",
            action="store_true",
            help="초안 후보만 고친다(살아있는 캠페인은 건드리지 않음)",
        )
        parser.add_argument("--apply", action="store_true", help="실제로 저장한다")

    def handle(self, *args, **opts):
        user = opts.get("username")

        # ── 후보(초안) ──
        cq = DMCampaignCandidate.objects.all()
        if user:
            cq = cq.filter(ig_connection__username=user)
        cand_todo = []
        for c in cq.select_related("ig_connection"):
            if _fix_obj(c, CANDIDATE_FIELDS, CANDIDATE_LIST_FIELDS):
                cand_todo.append(c)
        self.stdout.write(f"후보 {cq.count()}건 중 고칠 것 {len(cand_todo)}건")

        # ── 살아있는 캠페인 ──
        camp_todo = []
        no_url = []
        if not opts["candidates_only"]:
            mq = AutoDMCampaign.objects.exclude(media_id="")
            if user:
                mq = mq.filter(ig_connection__username=user)
            for c in mq.select_related("ig_connection"):
                if _fix_obj(c, CAMPAIGN_FIELDS, CAMPAIGN_LIST_FIELDS):
                    camp_todo.append(c)
                    if not _button_url(c):
                        no_url.append(c)
            from collections import Counter

            self.stdout.write(
                f"캠페인 {mq.count()}건 중 고칠 것 {len(camp_todo)}건 "
                f"{dict(Counter(c.status for c in camp_todo))}"
            )
            if no_url:
                self.stdout.write(
                    self.style.WARNING(
                        f"  ⚠️ 그중 {len(no_url)}건은 링크 버튼 URL 이 없다 — 본문에 링크가 "
                        "아예 없으므로 문구 수정만으로는 부족하다(수동 확인 필요):"
                    )
                )
                for c in no_url[:10]:
                    self.stdout.write(f"     {c.ig_connection.username} · {c.name[:40]}")

        self.stdout.write("")
        self.stdout.write("=== 바뀌는 모습 (앞 5건) ===")
        for c in (camp_todo + cand_todo)[:5]:
            txt = getattr(c, "opening_message_template", None) or getattr(
                c, "draft_opening_message", ""
            )
            self.stdout.write(f"  {txt[:110]}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("미리보기입니다 — 반영하려면 --apply"))
            return

        with transaction.atomic():
            if cand_todo:
                DMCampaignCandidate.objects.bulk_update(
                    cand_todo, list(CANDIDATE_FIELDS) + list(CANDIDATE_LIST_FIELDS)
                )
            if camp_todo:
                AutoDMCampaign.objects.bulk_update(
                    camp_todo, list(CAMPAIGN_FIELDS) + list(CAMPAIGN_LIST_FIELDS)
                )
        self.stdout.write(
            self.style.SUCCESS(f"반영 완료 — 후보 {len(cand_todo)}건 · 캠페인 {len(camp_todo)}건")
        )
