# 캠페인 신규 요청자 시계열 — 프론트 연동 가이드

캠페인 분석 화면의 **진행/모멘텀 차트**용 API. x축 = 시간, y축 = **신규 요청자 수**(그 시간대에 처음 요청한 사람 수). 범위 토글: **전체 기간 · 최근 24시간 · 최근 7일**.

> 관련: DM 통계는 `.../stats/`, 큐 현황은 `DM_QUEUE_STATE_FRONTEND.md`. 이 문서는 "시간에 따른 신규 유입 추이"만 다룬다.

---

## 1. 엔드포인트

```
GET /api/v1/integrations/auto-dm-campaigns/{id}/timeseries/?range=all|24h|7d
Authorization: Bearer <JWT>
```

- 인증: JWT 필수. 해당 워크스페이스 멤버만. 타 워크스페이스 캠페인은 **404**.
- `range` 기본값 **`all`**. 잘못된 값은 **400**(`{"success": false, "error": {...}}` 통일 포맷).

| range | 의미 | 버킷 단위(granularity) | 포인트 수 |
|---|---|---|---|
| `all` | 전체 기간 (최초 요청일~오늘) | `day` | 캠페인 나이만큼 |
| `24h` | 최근 24시간 | `hour` | 24 |
| `7d` | 최근 7일 | `day` | 7 |

버킷 경계는 **Asia/Seoul(KST)** 기준. 버킷 시각은 `+09:00` ISO8601.

---

## 2. 응답 스키마

```jsonc
{
  "campaign_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "campaign_status": "active",         // active/paused/completed ...
  "is_active": true,                    // status === active
  "range": "7d",
  "granularity": "day",                 // day | hour
  "timezone": "Asia/Seoul",
  "totals": {
    "lifetime_unique_requesters": 1280, // 전 기간 고유 요청자 (stats people.total 과 동일)
    "window_new_requesters": 342,       // 이 range 안의 신규 요청자 = sum(series[].new_requesters)
    "first_request_at": "2026-07-01T09:12:03+09:00", // 없으면 null
    "last_request_at":  "2026-07-15T18:44:51+09:00"  // 반복 댓글 포함 최신 (진행 여부 신호), 없으면 null
  },
  "series": [
    { "bucket": "2026-07-09T00:00:00+09:00", "new_requesters": 51 },
    { "bucket": "2026-07-10T00:00:00+09:00", "new_requesters": 63 },
    { "bucket": "2026-07-11T00:00:00+09:00", "new_requesters": 0 },   // 빈 버킷도 0 으로 채워짐
    // ...
    { "bucket": "2026-07-15T00:00:00+09:00", "new_requesters": 81 }   // 마지막 = 진행 중(partial)
  ],
  "history_complete": true
}
```

---

## 3. 집계 정의 (중요 — 숫자 해석)

- **사람 단위**입니다. y축은 "이벤트(댓글) 수"가 아니라 **사람 수**입니다.
- 한 사람은 **최초 트리거 댓글(루트 DM) 시각**에 **딱 한 번** 집계됩니다. 같은 사람이 여러 번 댓글을 달거나 복구 재댓글을 보내도 **재집계되지 않습니다**. (reward·후속 DM 제외 — `stats` 응답의 `people.total` 과 정확히 같은 사람 키공간.)
- 그래서 `range=24h`의 "신규 요청자"는 **최근 24시간에 처음 유입된 사람**입니다. 3일 전에 이미 요청한 사람이 오늘 또 댓글을 달아도 24h/7d 뷰의 신규에는 잡히지 않습니다(정상).

### 카피 매핑 (권장)
- `range=all` → "전체 기간 신규 요청자 `{totals.lifetime_unique_requesters}`명"
- `range=24h` → "최근 24시간 신규 요청자 `{totals.window_new_requesters}`명"
- `range=7d` → "최근 7일 신규 요청자 `{totals.window_new_requesters}`명"

### 불변식 (검증용)
- `sum(series[].new_requesters) === totals.window_new_requesters` (항상)
- `range=all` 이면 위 합계 `=== totals.lifetime_unique_requesters`
- `totals.lifetime_unique_requesters` 는 `.../stats/` 또는 큐 상태의 `people.total` 과 일치

---

## 4. 렌더링 팁

- **막대/영역 차트**로 그리세요. `series` 는 이미 시간순 + 빈 버킷 0-채움이라 그대로 x축에 매핑하면 됩니다.
- **마지막 버킷은 진행 중(partial)** 입니다(현재 시/일). 흐리게 or "진행 중" 표기 권장.
- 진행 여부 배지: `is_active` + `last_request_at` 로 "최근 유입 있음/멈춤"을 표시할 수 있습니다.
- `first_request_at`/`last_request_at`/`bucket` 은 **로그 수신 시각(≈댓글 시각)** 기준입니다. 웹훅 경로는 수 초, 폴링 보정 건은 최대 ~1시간 늦을 수 있는 **근사값**이라 "댓글 정확 시각"으로 표기하지 마세요.
- `history_complete === false` 면(현재는 항상 true) 로그 보존정책으로 과거 구간이 잘렸을 수 있으니 "전체 기간" 차트에 안내 배지를 띄우세요.

## 5. 예시 (fetch)

```js
const res = await fetch(
  `/api/v1/integrations/auto-dm-campaigns/${campaignId}/timeseries/?range=7d`,
  { headers: { Authorization: `Bearer ${jwt}` } }
);
const data = await res.json();
// data.series -> 차트 x축(bucket) / y축(new_requesters)
```
