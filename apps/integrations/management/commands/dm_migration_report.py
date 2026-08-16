"""사람이 직접 검수하는 HTML 리포트 — "이 DM 이 정말 이 게시물 것인가".

파이프라인이 낸 판정을 **사람이 눈으로 대조**할 수 있게, 게시물(캡션·링크·댓글 표본)과
복원된 DM 원문을 나란히 놓는다. 자동 판정만 믿고 고객에게 내보내기 전에 반드시 한 번은
사람이 봐야 한다 — 정밀도 지표는 표본에 대한 것이지 개별 건에 대한 보증이 아니다.

    docker exec <web> python manage.py dm_migration_report <job_id> -o /tmp/report.html

산출물은 자기완결 HTML 1파일(외부 요청 없음). 원본 파기(7일) 전에만 만들 수 있다.
"""

from __future__ import annotations

import html
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
        parser.add_argument("--all", action="store_true", help="excluded 까지 전부 (기본은 후보만)")

    def handle(self, *args, **o):
        try:
            job = DMMigrationJob.objects.select_related("ig_connection").get(id=o["job_id"])
        except (DMMigrationJob.DoesNotExist, ValueError) as exc:
            raise CommandError(f"잡을 찾을 수 없습니다: {o['job_id']}") from exc

        sd = job.stage_data or {}
        recs = {r["media_id"]: r for r in sd.get("recoveries") or []}
        rows = [(c, recs.get(c.media_id) or {}) for c in job.candidates.all()]
        # 검수 효율: 확신도 낮은 것부터 위로 올린다 (사람이 볼 가치가 큰 순서).
        order = {"needs_review": 0, "excluded": 1, "template_only": 2, "auto_draft": 3}
        rows.sort(key=lambda x: (order.get(x[0].band, 9), -(x[0].support_hits or 0)))

        # 탈락분 — 점수가 높은 순(= 아슬아슬하게 떨어진 것)으로. 미탐 검수의 핵심.
        rejected = list(sd.get("rejected") or [])
        if not rejected:
            # 탈락 기록을 남기기 전(2026-08-17 이전)에 돈 잡 — 미디어 목록에서 역산한다.
            # 점수·근거는 없지만 "무엇이 떨어졌나" 는 보여줄 수 있다.
            rejected = [
                {
                    "media_id": m.get("id"),
                    "caption": (m.get("caption") or "")[:300],
                    "comments_count": m.get("comments_count") or 0,
                    "permalink": m.get("permalink", ""),
                    "content_score": None,  # 기록 없음
                    "content_reasons": [],
                    "repetition": None,
                }
                for m in (sd.get("media") or [])
                if m.get("id") not in recs and (m.get("comments_count") or 0) >= 8
            ]
        rejected.sort(
            key=lambda r: (-(r.get("content_score") or 0), -(r.get("comments_count") or 0))
        )
        # 아예 조사하지 않은 것(댓글 8개 미만).
        seen = set(recs) | {r["media_id"] for r in rejected}
        skipped = [
            m
            for m in (sd.get("media") or [])
            if m.get("id") not in seen and (m.get("comments_count") or 0) < 8
        ]
        skipped.sort(key=lambda m: -(m.get("comments_count") or 0))

        htmlout = self._render(job, rows, rejected, skipped)
        with open(o["out"], "w", encoding="utf-8") as f:
            f.write(htmlout)
        self.stdout.write(f"저장: {o['out']} · 후보 {len(rows)}건")

    # ── 렌더 ──
    def _render(self, job, rows, rejected, skipped) -> str:
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
.tbl{{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--card)}}
.tbl th{{text-align:left;font-size:12px;color:var(--mut);padding:8px 10px;
border-bottom:1px solid var(--line);white-space:nowrap}}
.tbl td{{padding:7px 10px;border-bottom:1px solid var(--line);width:auto}}
.tbl tr:last-child td{{border-bottom:0}}
.tbl .num{{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--fg);width:auto}}
.cap2{{color:var(--mut);max-width:520px}}
h2{{border-top:2px solid var(--line);padding-top:20px}}
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
<h2 style="font-size:18px;margin:26px 0 4px">1. 캠페인으로 본 것 — {len(rows)}건</h2>
<div class="sub">👀 왼쪽 게시물 내용과 오른쪽 DM 문구가 <b>서로 맞는 이야기</b>인지 보세요.
캡션이 "AI 자료 드려요" 인데 DM 이 "다이어트 특강" 이면 잘못 붙은 겁니다.
확신이 낮은 것(검수필요)부터 위에 놓았습니다.</div>
{cards}

<h2 style="font-size:18px;margin:34px 0 4px">2. 캠페인이 아니라고 본 것 — {len(rejected)}건</h2>
<div class="sub">👀 <b>여기가 놓친 게 있는지 보는 자리입니다.</b> 판정 점수가 높은 순
(= 아슬아슬하게 떨어진 순)으로 놓았습니다. 위쪽 몇 개만 봐도 기준이 맞는지 감이 옵니다.
캡션에 "댓글 남기면 드려요" 가 있는데 여기 있으면 잘못 떨어진 겁니다.</div>
{self._reject_table(rejected)}

<h2 style="font-size:18px;margin:34px 0 4px">3. 아예 조사하지 않은 것 — {len(skipped)}건</h2>
<div class="sub">댓글이 8개 미만이라 판정 자체를 못 합니다(표본이 안 나옴).
댓글 수 많은 순으로 놓았습니다.</div>
{self._skip_table(skipped)}
</div></body></html>"""

    @staticmethod
    def _link(url: str) -> str:
        return f'<a href="{esc(url)}" rel="noreferrer">열기 ↗</a>' if url else "—"

    @staticmethod
    def _tags(reasons) -> str:
        if not reasons:
            return "—"
        return " ".join(f'<span class="tag">{esc(REASON_LABEL.get(x, x))}</span>' for x in reasons)

    def _reject_table(self, rejected) -> str:
        if not rejected:
            return '<div class="sub">없음</div>'
        body = []
        for r in rejected:
            score = r.get("content_score")
            rep = r.get("repetition")
            body.append(
                "<tr>"
                f'<td class="num">{"—" if score is None else f"{score:.2f}"}</td>'
                f'<td class="num">{r.get("comments_count", 0)}</td>'
                f'<td class="num">{"—" if rep is None else f"{rep:.0%}"}</td>'
                f"<td>{self._tags(r.get('content_reasons'))}</td>"
                f'<td class="cap2">{esc((r.get("caption") or "")[:160])}</td>'
                f"<td>{self._link(r.get('permalink', ''))}</td>"
                "</tr>"
            )
        return (
            '<table class="tbl"><thead><tr><th>점수</th><th>댓글</th><th>복붙</th>'
            "<th>걸린 신호</th><th>캡션</th><th></th></tr></thead><tbody>"
            + "\n".join(body)
            + "</tbody></table>"
        )

    def _skip_table(self, skipped) -> str:
        if not skipped:
            return '<div class="sub">없음</div>'
        body = []
        for m in skipped[:200]:
            body.append(
                "<tr>"
                f'<td class="num">{m.get("comments_count", 0)}</td>'
                f'<td class="cap2">{esc((m.get("caption") or "")[:160])}</td>'
                f"<td>{self._link(m.get('permalink', ''))}</td>"
                "</tr>"
            )
        more = f'<div class="sub">…외 {len(skipped) - 200}건</div>' if len(skipped) > 200 else ""
        return (
            '<table class="tbl"><thead><tr><th>댓글</th><th>캡션</th><th></th>'
            "</tr></thead><tbody>" + "\n".join(body) + "</tbody></table>" + more
        )

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
