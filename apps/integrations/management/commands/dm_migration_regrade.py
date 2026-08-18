"""이미 끝난 DM 이전 잡의 **등급만** 다시 매긴다 (Graph/LLM 호출 0).

왜 필요한가 — 등급 규칙은 판정 로직이지 수집 결과가 아니다. 규칙을 고쳤을 때 3~4시간
걸린 수집을 처음부터 다시 돌리는 것은 낭비고, 대형 계정에서는 Meta 쿼터를 또 쓰는 일이라
다른 워크스페이스에 피해를 준다(CLAUDE.md §1). ``stage_data["recoveries"]`` 에 판정에
필요한 수치가 다 들어 있으므로 여기서 다시 채점하고 후보 행에 반영한다.

⚠️ **한계**: 귀속 정리(:func:`attribute.resolve`)는 근거 원본(``users``)을 지운다.
   그래서 '누가 몇 초 차이로 받았나' 를 다시 세는 규칙 변경(예: 지문 창의 부호)은 소급이
   안 된다 — 저장된 ``auto_hits``/``gap_median`` 이 옛 정의로 계산된 값이기 때문이다.
   그런 변경은 다음 실행부터 적용된다. 이 명령이 소급할 수 있는 것은 저장된 수치를
   **다시 해석**하는 규칙뿐이다.

사용법::

    manage.py dm_migration_regrade <job_id>            # 미리보기(기본, 쓰지 않음)
    manage.py dm_migration_regrade <job_id> --apply    # 실제 반영
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.integrations.dm_migration import attribute
from apps.integrations.dm_migration.analyze import content_match
from apps.integrations.models import DMCampaignCandidate, DMMigrationJob


def _backfill_content_match(sd: dict, recs: list) -> int:
    """옛 기록에 없는 ``content_match`` 를 **저장된 캡션·DM 으로** 계산해 채운다.

    자동채택 ⑤(캡션↔DM 일치)는 2026-08-18 에 생겼다. 그 전 실행에는 이 값이 없어서 문이
    조용히 안 열린다 — 그러면 계정마다 몇십 분짜리 재실행을 해야 소급된다.
    캡션은 ``stage_data["media"]`` 에 전문이 있고 DM 문구는 offer 에 있으니, 여기서
    **추가 호출 없이** 계산할 수 있다. 이미 값이 있는 기록은 건드리지 않는다.
    """
    media = {m.get("id"): (m.get("caption") or "") for m in (sd.get("media") or [])}
    n = 0
    for r in recs:
        if r.get("content_match"):
            continue
        text = ((r.get("offer") or {}).get("text") or "").strip()
        if not text:
            continue
        cap = media.get(r.get("media_id")) or r.get("caption") or ""
        m = content_match(cap, text, r.get("trigger"))
        if m:
            r["content_match"] = m
            n += 1
    return n


class Command(BaseCommand):
    help = "끝난 DM 이전 잡의 등급을 현재 규칙으로 다시 매긴다 (외부 호출 0)"

    def add_arguments(self, parser):
        parser.add_argument("job_id", help="DMMigrationJob UUID")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="실제로 저장한다. 없으면 미리보기만 한다.",
        )

    def handle(self, *args, **opts):
        try:
            job = DMMigrationJob.objects.get(id=opts["job_id"])
        except (DMMigrationJob.DoesNotExist, ValueError, TypeError) as exc:
            raise CommandError(f"잡을 찾을 수 없습니다: {opts['job_id']}") from exc

        sd = job.stage_data or {}
        recs = sd.get("recoveries")
        if not recs:
            raise CommandError(
                "recoveries 가 없습니다 — 복원 단계가 끝나지 않은 잡입니다"
                f" (stage={job.stage}, status={job.status})"
            )

        before = Counter(r.get("grade") for r in recs)
        filled = _backfill_content_match(sd, recs)
        if filled:
            self.stdout.write(f"  캡션↔DM 일치 계산 {filled}건 (옛 기록에 없던 값 — 호출 0)")
        # regrade 는 recs 를 그 자리에서 고친다. 미리보기여도 DB 에 쓰지 않으므로 안전하다.
        changed = attribute.regrade(recs)
        after = Counter(r.get("grade") for r in recs)

        self.stdout.write(f"잡 {job.id} · 복원 {len(recs)}건 · 후보 {job.candidates.count()}건")
        self.stdout.write(f"  이전: {dict(before)}")
        self.stdout.write(f"  이후: {dict(after)}")
        self.stdout.write(f"  등급 변경 {changed}건")

        by_media = {r["media_id"]: r for r in recs if r.get("media_id")}
        cands = list(job.candidates.all())
        moves: Counter = Counter()
        todo = []
        for c in cands:
            r = by_media.get(c.media_id)
            if not r:
                continue
            band, cr = r.get("grade"), bool(r.get("confirm_required"))
            score = float(r.get("score") or 0.0)
            if c.band == band and c.confirm_required == cr:
                continue
            moves[(c.band, band)] += 1
            todo.append((c, band, cr, score))

        self.stdout.write(f"  후보 행 갱신 대상 {len(todo)}건")
        for (a, b), n in moves.most_common():
            self.stdout.write(f"    {a} → {b}: {n}")

        if not opts["apply"]:
            self.stdout.write(self.style.WARNING("미리보기입니다 — 반영하려면 --apply"))
            return

        with transaction.atomic():
            for c, band, cr, score in todo:
                c.band = band
                c.confirm_required = cr
                c.support_score = score
            if todo:
                DMCampaignCandidate.objects.bulk_update(
                    [c for c, _b, _cr, _s in todo],
                    ["band", "confirm_required", "support_score"],
                )
            sd["recoveries"] = recs
            sd["regraded_at_rule"] = {"changed": changed, "after": dict(after)}
            job.stage_data = sd
            job.save(update_fields=["stage_data", "updated_at"])

        self.stdout.write(self.style.SUCCESS(f"반영 완료 — 후보 {len(todo)}건 갱신"))
