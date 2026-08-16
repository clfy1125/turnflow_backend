"""사람이 직접 검수하는 HTML 리포트 — "이 DM 이 정말 이 게시물 것인가".

파이프라인이 낸 판정을 **사람이 눈으로 대조**할 수 있게, 게시물(캡션·링크·댓글 표본)과
복원된 DM 원문을 나란히 놓는다. 자동 판정만 믿고 고객에게 내보내기 전에 반드시 한 번은
사람이 봐야 한다 — 정밀도 지표는 표본에 대한 것이지 개별 건에 대한 보증이 아니다.

    docker exec <web> python manage.py dm_migration_report <job_id> -o /tmp/report.html

산출물은 자기완결 HTML 1파일(외부 요청 없음). 원본 파기(7일) 전에만 만들 수 있다.
"""

from __future__ import annotations

import html
import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from apps.integrations.models import DMMigrationJob

BAND_LABEL = {
    "auto_draft": ("자동채택", "#0a7"),
    "needs_review": ("검수필요", "#c80"),
    "template_only": ("템플릿만", "#68a"),
    "excluded": ("문구 못 살림", "#999"),
}
REASON_LABEL = {
    "repetition": "댓글 복붙",
    "caption_cta": "캡션 행동유도",
    "tiny_comments": "이모지·초단문",
    "caption_offer": "캡션 제공약속",
    "owner_reply_sent": "대댓글 '보냈다'",
}


def esc(t) -> str:
    return html.escape(str(t or ""))


class Command(BaseCommand):
    help = "DM 이전 분석 결과를 사람이 검수할 HTML 로 출력한다"

    def add_arguments(self, parser):
        parser.add_argument("job_id")
        parser.add_argument("-o", "--out", default="/tmp/dm_migration_report.html")
        parser.add_argument(
            "--all", action="store_true", help="excluded 까지 전부 (기본은 후보만)"
        )

    def handle(self, *args, **o):
        try:
            job = DMMigrationJob.objects.select_related("ig_connection").get(id=o["job_id"])
        except (DMMigrationJob.DoesNotExist, ValueError) as exc:
            raise CommandError(f"잡을 찾을 수 없습니다: {o['job_id']}") from exc

        recs = {r["media_id"]: r for r in (job.stage_data or {}).get("recoveries") or []}
        cands = list(job.candidates.all())
        rows = []
        for c in cands:
            rows.append((c, recs.get(c.media_id) or {}))
        # 검수 효율: 확신도 낮은 것부터 위로 올린다 (사람이 볼 가치가 큰 순서).
        order = {"needs_review": 0, "excluded": 1, "template_only": 2, "auto_draft": 3}
        rows.sort(key=lambda x: (order.get(x[0].band, 9), -(x[0].support_hits or 0)))

        htmlout = self._render(job, rows)
        with open(o["out"], "w", encoding="utf-8") as f:
            f.write(htmlout)
        self.stdout.write(f"저장: {o['out']} · 후보 {len(rows)}건")

    # ── 렌더 ──
    def _render(self, job, rows) -> str:
        conn = job.ig_connection
        sd = job.stage_data or {}
        bands = Counter(c.band for c, _ in rows)
        n_url = sum(1 for c, _ in rows if c.offer_url)
        n_msg = sum(1 for c, _ in rows if c.draft_opening_message)
        attribution = sd.get("attribution") or {}

        cards = "\n".join(self._card(i, c, r) for i, (c, r) in enumerate(rows, 1))
        chips = " ".join(
            f'<span class="chip" style="--c:{BAND_LABEL.get(b, ("", "#888"))[1]}">'
            f"{esc(BAND_LABEL.get(b, (b, ''))[0])} {n}</span>"
            for b, n in bands.most_common()
        )
        return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DM 이전 검수 — @{esc(conn.username)}</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e5e5e5;--card:#fafafa}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e8e8e8;--mut:#9aa;--line:#2c3038;--card:#1d2026}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;font:15px/1.65 -apple-system,'Segoe UI','Malgun Gothic',sans-serif;
background:var(--bg);color:var(--fg);word-break:keep-all}}
.wrap{{max-width:1100px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 4px}}
.sub{{color:var(--mut);font-size:14px;margin-bottom:18px}}
.chip{{display:inline-block;padding:3px 10px;border-radius:99px;font-size:13px;font-weight:600;
border:1px solid var(--c);color:var(--c);margin-right:6px}}
.sum{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:20px}}
.sum b{{font-size:18px}}
.card{{border:1px solid var(--line);border-radius:12px;margin-bottom:16px;overflow:hidden;background:var(--card)}}
.hd{{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}}
.no{{color:var(--mut);font-variant-numeric:tabular-nums;font-size:13px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:0}}
@media(max-width:820px){{.grid{{grid-template-columns:1fr}}}}
.col{{padding:14px}}
.col+.col{{border-left:1px solid var(--line)}}
@media(max-width:820px){{.col+.col{{border-left:0;border-top:1px solid var(--line)}}}}
.lbl{{font-size:12px;font-weight:700;color:var(--mut);letter-spacing:.04em;margin-bottom:6px}}
.cap{{white-space:pre-wrap;font-size:14px;max-height:150px;overflow:auto}}
.dm{{white-space:pre-wrap;font-size:14px;background:rgba(0,150,120,.08);
border-left:3px solid #0a7;padding:10px 12px;border-radius:0 6px 6px 0}}
.none{{color:var(--mut);font-style:italic}}
.kv{{font-size:13px;color:var(--mut);margin-top:8px}}
.kv code{{background:rgba(128,128,128,.14);padding:1px 5px;border-radius:4px;font-size:12px}}
a{{color:#2b7;word-break:break-all}}
.tag{{display:inline-block;font-size:11px;padding:2px 7px;border-radius:5px;
background:rgba(128,128,128,.16);color:var(--mut);margin:2px 4px 2px 0}}
.warn{{color:#c62}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
td{{padding:3px 0;vertical-align:top}}
td:first-child{{color:var(--mut);width:92px}}
</style></head><body><div class="wrap">
<h1>DM 이전 검수 — @{esc(conn.username)}</h1>
<div class="sub">잡 {esc(job.id)} · {job.created_at:%Y-%m-%d %H:%M} · 상태 {esc(job.status)}</div>
<div class="sum">
  <b>후보 {len(rows)}건</b> &nbsp; {chips}<br>
  <span style="color:var(--mut);font-size:13px">
  게시물 {job.media_scanned}개 스캔 · 되살린 DM {job.dm_messages_collected}통 ·
  링크 확보 {n_url}건 · 문구 확보 {n_msg}건 ·
  Graph 호출 {sum((job.api_budget_state or {{}}).get('made', {{}}).values())}
  {'· 귀속 정리: 시간 짝짓기 ' + str(attribution.get('moved', 0)) + '건 이동, 문구 경쟁 ' + str(attribution.get('demoted', 0)) + '건 내림' if attribution else ''}
  </span>
</div>
<div class="sub">👀 <b>확인 방법</b> — 왼쪽 게시물 내용과 오른쪽 DM 문구가 <b>서로 맞는 이야기</b>인지 보세요.
캡션이 "AI 자료 드려요" 인데 DM 이 "다이어트 특강" 이면 잘못 붙은 겁니다.
확신이 낮은 것(검수필요)부터 위에 놓았습니다.</div>
{cards}
</div></body></html>"""

    def _card(self, i, c, r) -> str:
        label, color = BAND_LABEL.get(c.band, (c.band, "#888"))
        hits, probed = c.support_hits or 0, c.support_probed or 0
        ratio = f"{hits}/{probed}" + (f" ({hits / probed:.0%})" if probed else "")
        reasons = " ".join(
            f'<span class="tag">{esc(REASON_LABEL.get(x, x))}</span>'
            for x in (r.get("content_reasons") or [])
        )
        dm = c.draft_opening_message or ""
        gate = c.gate_message or ""
        samples = (r.get("samples") or [])[:2]
        raw = "".join(
            f'<div class="kv">원문 발췌: {esc((s.get("text") or "")[:220])}</div>' for s in samples
        )
        url_row = (
            f'<tr><td>링크</td><td><a href="{esc(c.offer_url)}" rel="noreferrer">{esc(c.offer_url[:70])}</a></td></tr>'
            if c.offer_url
            else ""
        )
        demoted = r.get("offer_demoted")
        warn = (
            f'<div class="kv warn">⚠ 같은 문구가 다른 게시물에서 {demoted.get("owner_hits")}명 지지 '
            f"→ 그쪽 캠페인으로 보고 링크를 내렸습니다</div>"
            if demoted
            else ""
        )
        dm_block = (
            f'<div class="dm">{esc(dm)}</div>'
            if dm
            else '<div class="none">복원된 문구 없음 — 사용자가 직접 작성해야 합니다</div>'
        )
        gate_block = (
            f'<div class="lbl" style="margin-top:12px">팔로우 확인 단계</div>'
            f'<div class="dm" style="background:rgba(120,120,255,.08);border-color:#77e">{esc(gate)}</div>'
            if gate
            else ""
        )
        return f"""<div class="card">
<div class="hd">
  <span class="no">#{i}</span>
  <span class="chip" style="--c:{color}">{esc(label)}</span>
  <span class="no">받은 사람 {esc(ratio)}</span>
  {'<span class="no">확인 필요</span>' if c.confirm_required else ""}
  <span style="flex:1"></span>
  {f'<a class="no" href="{esc(c.media_permalink)}" rel="noreferrer">게시물 열기 ↗</a>' if c.media_permalink else ""}
</div>
<div class="grid">
  <div class="col">
    <div class="lbl">게시물 내용</div>
    <div class="cap">{esc(c.media_caption_excerpt) or '<span class="none">캡션 없음</span>'}</div>
    <div class="kv">판정 근거: {reasons or '<span class="none">없음</span>'}
      &nbsp;점수 <code>{r.get("content_score", 0)}</code></div>
    <div class="kv">감지 키워드: {" ".join(f'<span class="tag">{esc(k)}</span>' for k in (c.suggested_keywords or [])) or '<span class="none">없음</span>'}</div>
  </div>
  <div class="col">
    <div class="lbl">복원된 DM 문구</div>
    {dm_block}{gate_block}{warn}
    <table style="margin-top:10px">
      {url_row}
      <tr><td>버튼</td><td>{esc(c.offer_button_label) or "—"}</td></tr>
      <tr><td>댓글 수</td><td>{r.get("comments_count", "—")}</td></tr>
    </table>
    {raw}
  </div>
</div></div>"""
