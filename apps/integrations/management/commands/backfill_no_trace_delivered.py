"""도착했는데 '도착 미확인'으로 계상된 DM 을 delivered 로 정정 (백필).

배경 (2026-07-30 prod 조사):
    사설답장(Private Reply)은 '댓글당 1회' 비멱등이라, 1차 발송이 **실제로 도착했는데
    Meta 가 500/타임아웃을 반환**(성공 ack 유실)하면 재시도가 돌고 그 재시도는
    subcode **2534023**("답글을 달려는 댓글에 이미 답글이 있습니다")을 받는다. 결과적으로
    도착한 DM 이 ``failed_no_trace``(도착 미확인)로 종결됐다. 실측 76건
    (mini_ai_ 41 · 3dragon_pd 20 · reels_drgn 6 · yums__331 6 · ellisa_levelup 2 ·
    yeonhada__ 1). ``retry_count`` 분포가 패치 이력을 그대로 보여준다 — retry=24 22건
    (무한재시도 종결 d17a009 이전), retry=2 54건(즉시종결 후·멱등화 a08d093 이전).
    a08d093(2026-07-25 배포) 이후 **재발 0건**이지만, 과거 76건은 그대로 남아
    도착률을 깎고 어드민/고객 화면에 "확인 필요"로 표시된다.

판정 3단 (강한 근거부터 — 약한 근거로 상태를 바꾸지 않는다):
    tier1_child_reward
        같은 캠페인·같은 수신자에게 **child(리워드) 로그가 존재**하면 오프닝은 반드시
        도착했다. 리워드는 오프닝 DM 에 붙은 버튼 postback 으로만 발화되므로, 리워드가
        있다는 것은 사용자가 오프닝을 열고 버튼을 눌렀다는 뜻이다. **API 호출 0회.**
        ⚠️ 단, 캠페인이 ``gate_trigger_keywords`` 를 쓰면 키워드로도 리워드가 발화될 수
        있어 이 추론이 약해진다 → 그 경우는 tier1 에서 제외하고 tier2 로 넘긴다.
    tier2_conversations
        Conversations API 로 해당 수신자 스레드를 열어, **로그 시각 ±window 안에**
        우리(page) 발신 메시지가 있는지 확인한다.
        ⚠️ 오프닝과 리워드가 **같은 스레드**에 있으므로 시간창을 좁게 잡아야 한다
        (넓히면 리워드를 보고 오프닝이 갔다고 오판). 기본 ±180초.
    unresolved
        확인 불가 → **건드리지 않는다.**

정정 내용: status=delivered, verified_via=conv_api, delivered_at 설정,
    campaign.total_unconfirmed -= 1 / total_sent += 1 (루트 로그만 — child 는 원래
    카운트 제외), verification_log 에 근거 기록.

사용법 (기본 dry-run — 아무것도 바꾸지 않는다):
    python manage.py backfill_no_trace_delivered
    python manage.py backfill_no_trace_delivered --subcode 2534023 --verbose
    python manage.py backfill_no_trace_delivered --apply          # 실제 정정
    python manage.py backfill_no_trace_delivered --apply --tier1-only   # API 호출 없이
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.integrations.models import AutoDMCampaign, SentDMLog


class Command(BaseCommand):
    help = "도착 확인된 failed_no_trace DM 을 delivered 로 정정 (기본 dry-run)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="실제로 정정한다 (미지정 시 dry-run — DB 변경 없음)",
        )
        parser.add_argument(
            "--subcode",
            default="2534023",
            help="대상 error_subcode (기본 2534023). 'any' 면 subcode 무관 failed_no_trace 전체",
        )
        parser.add_argument(
            "--tier1-only",
            action="store_true",
            help="child 리워드 근거만 사용 (Conversations API 호출 안 함)",
        )
        parser.add_argument(
            "--window-seconds",
            type=int,
            default=180,
            help="tier2 시간창(초). 오프닝/리워드 혼동을 막기 위해 좁게 유지 (기본 180)",
        )
        parser.add_argument("--limit", type=int, default=0, help="처리 상한 (0=무제한)")
        parser.add_argument("--verbose", action="store_true", help="건별 상세 출력")

    # ── 판정 ────────────────────────────────────────────────────────────
    def _tier1_child_reward(self, log) -> tuple[bool, str]:
        """같은 캠페인·수신자에 리워드(child) 로그가 있으면 오프닝 도착 확정."""
        campaign = log.campaign
        kws = [k for k in (campaign.gate_trigger_keywords or []) if str(k).strip()]
        if kws:
            # 키워드로도 리워드가 발화될 수 있어 'postback = 오프닝 열림' 추론이 성립 안 함
            return False, "gate_trigger_keywords 사용 캠페인 — tier1 근거 약함"
        qs = SentDMLog.objects.filter(
            campaign=campaign,
            recipient_user_id=log.recipient_user_id,
            dm_kind=SentDMLog.DMKind.REWARD,
        ).exclude(pk=log.pk)
        if qs.exists():
            statuses = sorted(set(qs.values_list("status", flat=True)))
            return True, f"child reward 존재 (status={statuses}) → 오프닝 postback 발생"
        return False, "child reward 없음"

    def _tier2_conversations(self, log, window_s: int) -> tuple[bool | None, str]:
        """로그 시각 ±window 안에 우리 발신 메시지가 있는지 Conversations 로 확인."""
        import requests

        from apps.integrations.services import InstagramMessagingService

        conn = log.campaign.ig_connection
        if not (log.recipient_user_id and conn.external_account_id):
            return None, "recipient_user_id/계정 id 없음"
        base = InstagramMessagingService.GRAPH_API_BASE
        try:
            resp = requests.get(
                f"{base}/{conn.external_account_id}/conversations",
                params={
                    "platform": "instagram",
                    "user_id": str(log.recipient_user_id),
                    # limit(5) 는 최근 5건만 봐서 오래된 건을 놓친다 → 넉넉히
                    "fields": "messages.limit(50){from,created_time}",
                    "access_token": conn.access_token,
                },
                timeout=25,
            )
            body = resp.json() or {}
        except Exception as e:  # noqa: BLE001
            return None, f"API 예외: {type(e).__name__}"
        if "error" in body:
            return None, f"API 오류: {str(body['error'].get('message'))[:60]}"

        anchor = log.submitted_at or log.created_at
        lo = anchor - timedelta(seconds=window_s)
        hi = anchor + timedelta(seconds=window_s)
        me = str(conn.external_account_id)
        for conv in body.get("data") or []:
            for m in ((conv.get("messages") or {}).get("data")) or []:
                frm = str((m.get("from") or {}).get("id") or "")
                if frm != me:
                    continue
                ts = m.get("created_time")
                parsed = (
                    timezone.datetime.fromisoformat(str(ts).replace("+0000", "+00:00"))
                    if ts
                    else None
                )
                if parsed and lo <= parsed <= hi:
                    return True, f"창 내 발신 메시지 확인 ({ts}, anchor={anchor:%H:%M:%S})"
        return False, f"창(±{window_s}s) 내 발신 메시지 없음"

    # ── 실행 ────────────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        apply_ = opts["apply"]
        subcode = opts["subcode"]
        tier1_only = opts["tier1_only"]
        window_s = opts["window_seconds"]
        limit = opts["limit"]
        verbose = opts["verbose"]

        qs = SentDMLog.objects.filter(status=SentDMLog.Status.FAILED_NO_TRACE)
        if subcode != "any":
            qs = qs.filter(error_subcode=subcode)
        qs = qs.select_related("campaign__ig_connection").order_by("created_at")
        if limit:
            qs = qs[:limit]
        rows = list(qs)

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"대상 {len(rows)}건 (subcode={subcode}, "
                f"{'APPLY' if apply_ else 'DRY-RUN'}, "
                f"{'tier1 only' if tier1_only else f'tier1+tier2(±{window_s}s)'})"
            )
        )
        if not rows:
            return

        buckets: dict[str, list] = {"tier1": [], "tier2": [], "unresolved": []}
        reasons: dict[str, str] = {}
        for log in rows:
            ok, why = self._tier1_child_reward(log)
            if ok:
                buckets["tier1"].append(log)
                reasons[str(log.pk)] = f"tier1: {why}"
                continue
            if tier1_only:
                buckets["unresolved"].append(log)
                reasons[str(log.pk)] = f"unresolved: {why} (tier1-only 모드)"
                continue
            ok2, why2 = self._tier2_conversations(log, window_s)
            if ok2 is True:
                buckets["tier2"].append(log)
                reasons[str(log.pk)] = f"tier2: {why2}"
            else:
                buckets["unresolved"].append(log)
                reasons[str(log.pk)] = f"unresolved: {why2}"

        for name, style in (
            ("tier1", self.style.SUCCESS),
            ("tier2", self.style.SUCCESS),
            ("unresolved", self.style.WARNING),
        ):
            self.stdout.write(style(f"  {name:11s} {len(buckets[name]):4d}건"))

        if verbose:
            for name in ("tier1", "tier2", "unresolved"):
                for log in buckets[name]:
                    self.stdout.write(
                        f"    [{name}] {log.created_at:%m-%d %H:%M} "
                        f"@{log.recipient_username} retry={log.retry_count} "
                        f"ig=@{log.campaign.ig_connection.username} "
                        f"cam={log.campaign.name[:20]!r} :: {reasons[str(log.pk)]}"
                    )

        targets = buckets["tier1"] + buckets["tier2"]
        if not apply_:
            self.stdout.write(
                self.style.NOTICE(
                    f"\nDRY-RUN — 변경 없음. --apply 를 붙이면 {len(targets)}건을 정정합니다."
                )
            )
            per_cam: dict[str, int] = {}
            for log in targets:
                k = f"@{log.campaign.ig_connection.username} / {log.campaign.name[:24]}"
                per_cam[k] = per_cam.get(k, 0) + 1
            for k, v in sorted(per_cam.items(), key=lambda x: -x[1]):
                self.stdout.write(f"    {v:4d}건  {k}")
            return

        fixed = 0
        for log in targets:
            with transaction.atomic():
                fresh = SentDMLog.objects.select_for_update().get(pk=log.pk)
                if fresh.status != SentDMLog.Status.FAILED_NO_TRACE:
                    continue  # 그 사이 다른 경로가 정리함
                fresh.append_verification_log(
                    {
                        "path": "backfill_no_trace_delivered",
                        "result": "confirmed_sent",
                        "reason": reasons[str(log.pk)],
                    }
                )
                fresh.status = SentDMLog.Status.DELIVERED
                fresh.verified_via = SentDMLog.VerifiedVia.CONV_API
                fresh.delivered_at = fresh.delivered_at or timezone.now()
                # NOTE: SentDMLog 에는 updated_at 필드가 없다(auto_now 미사용) — update_fields 에
                # 넣으면 ValueError. verification_log 는 append_verification_log 가 이미 저장했다.
                fresh.save(update_fields=["status", "verified_via", "delivered_at"])
                # 카운터 보정 — 루트 로그만(child 는 애초에 카운트 제외)
                if fresh.parent_log_id is None:
                    AutoDMCampaign.objects.filter(pk=fresh.campaign_id).update(
                        total_unconfirmed=F("total_unconfirmed") - 1,
                        total_sent=F("total_sent") + 1,
                        updated_at=timezone.now(),
                    )
            fixed += 1

        self.stdout.write(self.style.SUCCESS(f"\n정정 완료 {fixed}건 / 대상 {len(targets)}건"))
        # 음수 방어 — 과거에 카운터가 어긋나 있었을 수 있다
        neg = AutoDMCampaign.objects.filter(total_unconfirmed__lt=0)
        if neg.exists():
            self.stdout.write(
                self.style.WARNING(
                    "  ⚠️ total_unconfirmed 가 음수인 캠페인: "
                    + json.dumps(
                        list(neg.values_list("name", "total_unconfirmed")), ensure_ascii=False
                    )
                    + " → 0 으로 보정"
                )
            )
            neg.update(total_unconfirmed=0, updated_at=timezone.now())
