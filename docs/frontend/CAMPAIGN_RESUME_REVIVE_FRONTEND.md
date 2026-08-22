# 캠페인 재개 시 「밀린 DM」 자동 재발송 — 프론트 연동

**작성: 2026-08-22 · 마이그레이션 없음 · 응답에 필드 1개 추가(additive)**

---

## 1. 무엇이 바뀌었나

캠페인을 **일시정지**하면, 그 순간 발송 큐에 남아 있던 DM 은 발송되지 않고 `skipped` 로
종결됩니다. 여기까지는 종전과 같습니다. 문제는 **재개해도 그 건들이 살아나지 않았다는 것**
입니다 — `resume` 은 캠페인 status 만 `active` 로 올렸을 뿐이라, 정지 중 큐에서 빠져나온
사람들은 캠페인을 다시 켜도 영구 미수신이었습니다.

> 실제 사고(2026-08-22, prod): 소급 발송으로 238건이 큐에 쌓인 직후 사용자가 4분 만에
> 일시정지 → 53건만 발송 → 1시간 36분 뒤 재개. 재개 후에도 **191명은 DM 을 못 받았고**,
> 운영자가 수동으로 되살려야 했습니다.

**이제 재개하는 모든 경로가 정지 동안 밀린 DM 을 발송 큐로 되돌립니다.**
즉 일시정지의 의미가 「취소」가 아니라 **「보류」**로 확정되었습니다.

---

## 2. 계약 — `revive_queued`

재개 응답에 정수 필드 `revive_queued` 가 추가됩니다. **되돌린(=앞으로 발송될) 건수**입니다.

| 엔드포인트 | 응답 |
|---|---|
| `POST /api/v1/integrations/auto-dm-campaigns/{id}/resume/` | 기존 캠페인 객체 + `revive_queued` |
| `POST .../auto-dm-campaigns/bulk-resume/` | `{succeeded, failed, revive_queued}` — **대상 전체 합계** |
| `PATCH .../auto-dm-campaigns/{id}/` (`status: "active"` 로 전이) | 기존 캠페인 객체 + `revive_queued` |
| `POST .../auto-dm-campaigns/{id}/schedule/` (`activate: true`) | 기존 캠페인 객체 + `revive_queued` |
| `POST /api/v1/admin/auto-dm/campaigns/{id}/resume/` (어드민) | `{id, status, revive_queued}` |

```jsonc
// POST .../auto-dm-campaigns/132ec3f6.../resume/  → 200
{
  "id": "132ec3f6-418c-4977-b209-e02e470b9e72",
  "name": "가을 맞이 추천 스카프 구매 정보",
  "status": "active",
  // ... (목록 항목과 동일한 형태, 통계 enrichment 포함)
  "revive_queued": 191        // ★ 추가
}
```

`bulk-pause` / `pause` 응답에는 **포함되지 않습니다**(재개 액션에만 존재).

### 권장 UX

- `revive_queued > 0` → 재개 토스트에 **"밀려 있던 N건도 순차 발송됩니다"**를 덧붙여 주세요.
  사용자가 「정지했더니 안 나간 사람들」을 신경 쓰고 있었다는 것이 이 사고의 출발점입니다.
- `revive_queued == 0` → 기존 재개 문구 그대로. (밀린 게 없거나, 있었지만 메시징 창이 지났음)
- **일괄 재개**에서 합계가 크면(예: 100건 이상) 확인 모달을 권합니다 — 그만큼의 DM 이 실제로
  나갑니다. 취소 수단은 「다시 일시정지」이고, 그 시점에 아직 큐에 있던 건은 다시 보류됩니다.
- 발송은 즉시 몰아치지 않습니다. 페이서가 **평균 5초 간격**으로 분산하므로 191건이면 약
  16분에 걸쳐 나갑니다. 진행 상황은 기존 `GET .../queue-state/` 게이지로 그대로 보입니다.

---

## 3. 되살림 규칙 (프론트가 알아야 할 만큼만)

| 항목 | 규칙 |
|---|---|
| 대상 | **정지 때문에** 스킵된 건만 |
| 제외 | 월 DM 한도 초과·본인 계정 수신·계정 비활성·예약창 밖으로 스킵된 건 (재개와 인과가 없음) |
| 메시징 창 | 댓글 기반 **7일** / user_id 기반 **24시간** 안에 있는 건만. 지난 건은 Meta 가 어차피 거부하므로 되살리지 않습니다 |
| 순서 | 오래된 것 먼저 (= 창이 먼저 닫히는 것 먼저) |
| 상한 | 1회 최대 **1000건** |
| 중복 | 같은 DM 로그를 제자리에서 되살리므로(동일 `idempotency_key`) **재개를 여러 번 눌러도 중복 발송되지 않습니다** |
| 요금제 | **전 플랜**. 프리미엄 전용인 `retry-failed` 와 달리 게이트가 없습니다 (무료 플랜은 월 200건 한도가 자연히 상한 역할) |
| 실행 | 비동기(Celery). 응답의 숫자는 「되돌릴 예정 건수」이며, 실제 전이는 수초 내에 끝납니다 |

### 활성 캠페인 문구 수정은 영향 없음

`revive_queued` 는 **비활성→active 전이일 때만** 0 이 아닙니다. 이미 `active` 인 캠페인에
문구/이름을 PATCH 하는 흔한 경우는 항상 `0` 이며 백로그 스캔도 돌지 않습니다.

---

## 4. 창이 지나 되살릴 수 없는 건

메시징 창(댓글 7일)이 지난 건은 되살아나지 않습니다. 이 사람들에게 닿는 방법은 기존
**실패 DM 복구(재댓글 방식)** 뿐입니다 — [DM_RECOVERY_FRONTEND.md](DM_RECOVERY_FRONTEND.md) 참고.
따라서 "정지했다가 오래 뒤에 재개"는 여전히 손실이 있고, 그건 정책상 정상입니다.

프리미엄 `POST .../{id}/retry-failed/` 는 그대로 유지됩니다(토큰 끊김으로 실패한 건까지
포함해 더 넓게 되살리는 별도 기능).

---

## 5. 참고 — 서버 구현 위치

| 무엇 | 어디 |
|---|---|
| 대상 판정 단일 소스 | `SentDMLog.revivable_paused_logs()` (apps/integrations/models.py) |
| 재개 경로 단일 진입점 | `AutoDMCampaign.enqueue_paused_backlog_revive()` |
| 실제 되살림 | Celery `integrations.revive_paused_skipped_logs` |
| 스킵 사유 문구 상수 | `SentDMLog.SKIP_REASON_CAMPAIGN_INACTIVE` — 문구를 리터럴로 복제하면 되살림이 조용히 0건이 된다 |
| 테스트 | `apps/integrations/test_resume_revive.py` |
