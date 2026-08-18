"""이미 만들어진 이전 후보의 링크를 **원본 목적지로 되돌린다** (Graph·LLM 호출 0).

왜 별도 명령인가 — 링크 되돌리기는 판정이 아니라 **산출물 손질**이다. 수집·판정 결과는
그대로 두고 링크만 바꿀 수 있으니, 몇 시간짜리 재수집 없이 소급할 수 있어야 한다.
(``dm_migration_regrade`` 와 같은 원칙: 다시 살 필요가 없는 것은 다시 사지 않는다.)

무엇을 바꾸나
    · ``offer_url`` → 되돌린 목적지
    · ``draft_opening_message`` / ``gate_message`` 안에 박힌 같은 래퍼 링크
    · ``matched_template.recovered_url`` 은 **건드리지 않는다** — 관측한 원문 근거다.
      되돌린 값은 ``matched_template.resolved_url`` 에 따로 남겨 감사할 수 있게 한다.

``--campaigns`` 를 주면 **이미 활성화된 캠페인**(``source=dm_migration``)의 링크 버튼·본문도
같이 되돌린다. 후보만 고치면 이미 불러오기를 누른 계정은 계속 남의 래퍼로 발송한다
(실측 2026-08-19: @3dragon_pd 42개가 active 상태로 인포크 래퍼를 물고 있었다).
**사용자가 직접 만든 캠페인은 건드리지 않는다** — 본인이 넣은 링크는 본인 선택이다.

사용법::

    manage.py dm_migration_resolve_links --job <job_id>
    manage.py dm_migration_resolve_links --username highestlevel33 --apply
    manage.py dm_migration_resolve_links --job <job_id> --offline-only --apply
    manage.py dm_migration_resolve_links --username 3dragon_pd --campaigns --apply
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.integrations.dm_migration import links
from apps.integrations.dm_migration.analyze import find_urls
from apps.integrations.models import AutoDMCampaign, DMCampaignCandidate, DMMigrationJob

# ⚠️ **우리가 만든 캠페인만 건드린다.** 사용자가 직접 인포크 링크를 넣어 만든 캠페인은
#    그게 본인 선택이라 손대면 안 된다. 설명 문자열("불러옴")로 판정하지 않는다 — 사용자가
#    설명을 지우면 판정이 깨진다. `source` 는 apply 경로가 박아주는 값이고,
#    실측(2026-08-19)에서 설명 표기 42건과 정확히 일치했다.
#    (모델이 inline choices 를 쓰므로 열거형 대신 값 그대로 쓴다 — models.py `source` 참조)
MIGRATED = "dm_migration"

# 캠페인에서 링크가 들어 있는 곳 — 버튼과 본문 양쪽.
CAMPAIGN_TEXT_FIELDS = (
    "opening_message_template",
    "message_template",
    "reward_message_template",
    "follow_gate_retry_message",
    "public_reply_template",
)
CAMPAIGN_TEXT_LISTS = (
    "opening_message_templates",
    "follow_gate_prompt_templates",
    "public_reply_templates",
    "recovery_reply_templates",
)


def _urls_of_campaign(c: AutoDMCampaign) -> list[str]:
    out: list[str] = []
    for b in c.link_buttons or []:
        if isinstance(b, dict) and (b.get("url") or "").strip():
            out.append(b["url"].strip())
    if (getattr(c, "link_button_url", "") or "").strip():
        out.append(c.link_button_url.strip())
    for f in CAMPAIGN_TEXT_FIELDS:
        out += find_urls(getattr(c, f, "") or "")
    for f in CAMPAIGN_TEXT_LISTS:
        for t in getattr(c, f, None) or []:
            if isinstance(t, str):
                out += find_urls(t)
    seen: set = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def _fix_campaign(c: AutoDMCampaign, mapping: dict) -> list[str]:
    """캠페인의 버튼·본문 링크를 되돌리고 **바뀐 필드 이름**을 돌려준다."""
    changed: list[str] = []
    buttons = c.link_buttons or []
    if isinstance(buttons, list):
        new_btns = []
        for b in buttons:
            if isinstance(b, dict) and mapping.get((b.get("url") or "").strip()):
                nb = dict(b)
                nb["url"] = mapping[b["url"].strip()]
                new_btns.append(nb)
            else:
                new_btns.append(b)
        if new_btns != buttons:
            c.link_buttons = new_btns
            changed.append("link_buttons")
    legacy = (getattr(c, "link_button_url", "") or "").strip()
    if legacy and mapping.get(legacy) and mapping[legacy] != legacy:
        c.link_button_url = mapping[legacy][:1000]
        changed.append("link_button_url")
    for f in CAMPAIGN_TEXT_FIELDS:
        old = getattr(c, f, "") or ""
        new = links.rewrite_text(old, mapping)
        if new != old:
            setattr(c, f, new)
            changed.append(f)
    for f in CAMPAIGN_TEXT_LISTS:
        old = getattr(c, f, None)
        if not isinstance(old, list) or not old:
            continue
        new = [links.rewrite_text(t, mapping) if isinstance(t, str) else t for t in old]
        if new != old:
            setattr(c, f, new)
            changed.append(f)
    return changed


def _urls_of(cand: DMCampaignCandidate) -> list[str]:
    out: list[str] = []
    mt = cand.matched_template or {}
    for u in (cand.offer_url or "", (mt.get("recovered_url") or "")):
        # 스킴 없이 적힌 링크도 대상이다 — 그대로 두면 자동채택인데 불러오기가 400 난다.
        u = (u or "").strip()
        if u and u not in out:
            out.append(u)
    for text in (cand.draft_opening_message or "", cand.gate_message or ""):
        for u in find_urls(text):
            if u not in out:
                out.append(u)
    return out


class Command(BaseCommand):
    help = "이전 후보의 타사 래퍼 링크를 원본 목적지로 되돌린다 (외부 Graph 호출 0)"

    def add_arguments(self, parser):
        parser.add_argument("--job", help="DMMigrationJob UUID (이 잡의 후보만)")
        parser.add_argument("--username", help="IG 계정 username (그 계정의 후보 전부)")
        parser.add_argument(
            "--offline-only",
            action="store_true",
            help="조회 없이 파라미터·JWT 로 풀리는 것만 되돌린다(소셜비즈·매니챗 제외)",
        )
        parser.add_argument(
            "--campaigns",
            action="store_true",
            help=(
                "⚠️ **살아있는 캠페인**(source=dm_migration)의 버튼·본문 링크도 되돌린다. "
                "사용자가 직접 만든 캠페인은 건드리지 않는다."
            ),
        )
        parser.add_argument("--apply", action="store_true", help="실제로 저장한다")

    def handle(self, *args, **opts):
        qs = DMCampaignCandidate.objects.all()
        camps: list[AutoDMCampaign] = []
        if opts.get("job"):
            try:
                job = DMMigrationJob.objects.get(id=opts["job"])
            except (DMMigrationJob.DoesNotExist, ValueError, TypeError) as exc:
                raise CommandError(f"잡을 찾을 수 없습니다: {opts['job']}") from exc
            qs = qs.filter(job=job)
        elif opts.get("username"):
            qs = qs.filter(ig_connection__username=opts["username"])
        else:
            raise CommandError("--job 또는 --username 중 하나는 필요합니다")

        cands = list(qs.select_related("ig_connection"))
        if opts["campaigns"]:
            cq = AutoDMCampaign.objects.filter(source=MIGRATED)
            if opts.get("username"):
                cq = cq.filter(ig_connection__username=opts["username"])
            elif opts.get("job"):
                cq = cq.filter(ig_connection=job.ig_connection)
            camps = list(cq.select_related("ig_connection"))
        if not cands and not camps:
            raise CommandError("대상이 없습니다")

        wanted: list[str] = []
        for c in cands:
            for u in _urls_of(c):
                if u not in wanted:
                    wanted.append(u)
        for c in camps:
            for u in _urls_of_campaign(c):
                if u not in wanted:
                    wanted.append(u)

        offline_only = bool(opts["offline_only"])
        resolver = links.Resolver(enabled=not offline_only)
        mapping: dict[str, str] = {}
        methods: Counter = Counter()
        try:
            for u in wanted:
                final, how = resolver.resolve(u)
                mapping[u] = final
                methods[how or "(안 바뀜)"] += 1
        finally:
            resolver.close()

        changed_urls = {k: v for k, v in mapping.items() if v and v != k}
        self.stdout.write(f"후보 {len(cands)}건 · 링크 {len(wanted)}개")
        self.stdout.write(f"  되돌림 {len(changed_urls)} · 그대로 {len(wanted)-len(changed_urls)}")
        self.stdout.write(f"  조회 {resolver.fetched}회 · 조회실패 {resolver.failed}")
        for how, n in methods.most_common():
            self.stdout.write(f"    {how}: {n}")

        todo = []
        for c in cands:
            mt = dict(c.matched_template or {})
            raw = (mt.get("recovered_url") or c.offer_url or "").strip()
            new_url = changed_urls.get(raw, c.offer_url or "")
            # offer_url 이 이미 되돌려진 값이면(재실행) 그대로 둔다.
            if not new_url:
                new_url = c.offer_url or ""
            new_open = links.rewrite_text(c.draft_opening_message or "", changed_urls)
            new_gate = links.rewrite_text(c.gate_message or "", changed_urls)
            resolved = new_url if raw and new_url != raw else (mt.get("resolved_url") or "")
            if (
                new_url == (c.offer_url or "")
                and new_open == (c.draft_opening_message or "")
                and new_gate == (c.gate_message or "")
                and resolved == (mt.get("resolved_url") or "")
            ):
                continue
            mt["recovered_url"] = raw  # 관측 원문을 확정해 남긴다(예전 기록엔 없을 수 있다)
            mt["resolved_url"] = resolved
            c.offer_url = new_url[:1000]
            c.draft_opening_message = new_open
            c.gate_message = new_gate
            c.matched_template = mt
            todo.append(c)

        self.stdout.write(f"  후보 행 갱신 대상 {len(todo)}건")
        for c in todo[:15]:
            mt = c.matched_template or {}
            self.stdout.write(f"    {c.media_permalink.rstrip('/').rsplit('/', 1)[-1]}")
            self.stdout.write(f"      {(mt.get('recovered_url') or '')[:96]}")
            self.stdout.write(f"   →  {c.offer_url[:96]}")

        # ── 살아있는 캠페인 ──
        camp_todo: list[AutoDMCampaign] = []
        camp_fields: set = set()
        for c in camps:
            fields = _fix_campaign(c, changed_urls)
            if fields:
                camp_todo.append(c)
                camp_fields.update(fields)
        if camps:
            from collections import Counter as _C

            self.stdout.write("")
            self.stdout.write(
                f"  캠페인(이전분만) {len(camps)}건 중 갱신 대상 {len(camp_todo)}건 "
                f"{dict(_C(c.status for c in camp_todo))}"
            )
            for c in camp_todo[:10]:
                btn = (c.link_buttons or [{}])[0].get("url", "") if c.link_buttons else ""
                self.stdout.write(f"    {c.ig_connection.username} · {c.name[:34]}")
                self.stdout.write(f"   →  버튼 {btn[:92]}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("미리보기입니다 — 반영하려면 --apply"))
            return

        with transaction.atomic():
            if todo:
                DMCampaignCandidate.objects.bulk_update(
                    todo,
                    [
                        "offer_url",
                        "draft_opening_message",
                        "gate_message",
                        "matched_template",
                    ],
                )
            if camp_todo:
                AutoDMCampaign.objects.bulk_update(camp_todo, sorted(camp_fields))
        self.stdout.write(
            self.style.SUCCESS(f"반영 완료 — 후보 {len(todo)}건 · 캠페인 {len(camp_todo)}건 갱신")
        )
