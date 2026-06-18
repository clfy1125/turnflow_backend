# Auto DM 캠페인 조회 고도화 (백엔드 완료 보고)

> 대상: 프론트엔드 — 캠페인 목록/대시보드 화면
> 상태: **구현 완료 (dev 반영)** · 마이그레이션 없음 · Breaking change 없음
> Base URL: `/api/v1/integrations/auto-dm-campaigns/`
> 인증: 전 엔드포인트 `Authorization: Bearer <ACCESS_TOKEN>` (본인 멤버 워크스페이스만)
> Swagger: `/api/docs/` (`Auto DM` 태그)

프론트 조회 고도화 요청 사항을 모두 반영했습니다. 우선순위별로 정리합니다.

---

## 1. (최우선) 대시보드 요약 — `GET /summary/`

목록을 전부 받아 프론트에서 합산할 필요 없이, **집계 한 방**으로 받습니다.

```
GET /api/v1/integrations/auto-dm-campaigns/summary/?ig_connection_id=<uuid>
```

### 스코프 결정
| 쿼리 | 동작 |
|---|---|
| `ig_connection_id` (권장) | 그 IG 계정의 캠페인으로 `counts`·`delivery` 집계, `usage` 는 그 계정의 **워크스페이스** 기준 |
| `workspace_id` | 그 워크스페이스 전체 |
| 둘 다 생략 | 사용자의 워크스페이스가 **1개면** 자동 사용. 여러 개면 **400** |

### 응답 (200)
```jsonc
{
  "counts": { "active": 3, "paused": 1, "completed": 2, "inactive": 0, "total": 6 },
  "usage": {
    "sent_this_month": 42,                 // 이번 캘린더월(Asia/Seoul) 실제 발송(접수) DM 수 — 정확
    "monthly_free_limit": 100,             // 플랜 한도. starter 100 / pro 1000 / enterprise -1(무제한)
    "remaining_this_month": 58,            // max(limit - sent, 0). 무제한이면 null
    "is_over_limit": false,                // sent >= limit. 무제한이면 항상 false
    "period_start": "2026-06-01T00:00:00+09:00",
    "period_end":   "2026-07-01T00:00:00+09:00"   // 미포함(exclusive)
  },
  "delivery": {
    "total_sent": 40,                      // 도착확인(delivered)+읽음(read) 합
    "delivery_rate": 0.95,                 // ACCEPTED 진입 건 중 도착확인 비율 (0~1)
    "success_rate": 0.93,                  // 전체 로그 중 도착(또는 legacy sent) 비율 (0~1)
    "needs_attention_total": 2             // 조치 필요 로그 합 (아래 정의)
  },
  "last_activity_at": "2026-06-17T18:20:00+09:00"  // 가장 최근 DM 로그 시각. 없으면 null
}
```

### 에러
| 코드 | 상황 |
|---|---|
| 400 | 워크스페이스 결정 불가(여러 개인데 id 미지정) / 잘못된 `ig_connection_id`·`workspace_id` |
| 401 | 인증 필요 |

> **usage 정확도**: 사용량은 발송 로그(SentDMLog)에서 이번 달치를 직접 세므로 정확합니다.
> (내부 UsageCounter 는 발송 시 갱신되지 않아 사용하지 않습니다.)
> 한도는 워크스페이스 **플랜(starter/pro/enterprise)** 기준입니다.
> **관리자(is_staff/superuser) 계정은 플랜과 무관하게 무제한**(`monthly_free_limit=-1`,
> `remaining_this_month=null`, `is_over_limit=false`) 으로 내려갑니다.

---

## 2. (핵심) 목록 항목 통계 enrichment — `GET /` (N+1 제거)

목록 응답의 **각 캠페인 항목에 통계 필드가 함께** 내려갑니다. 이제 항목마다
`/{id}/stats/` 를 따로 부르지 마세요(N+1 제거).

```
GET /api/v1/integrations/auto-dm-campaigns/?ig_connection_id=<uuid>
```

각 항목에 추가된 read-only 필드:
| 필드 | 타입 | 의미 |
|---|---|---|
| `delivered_count` | int | 도착확인(delivered)+읽음(read) DM 수 |
| `delivery_rate` | float (0~1) | ACCEPTED 진입 건 중 도착확인 비율 |
| `needs_attention_count` | int | 조치 필요 로그 수 (아래 정의) |
| `last_sent_at` | datetime\|null | 가장 최근 발송 로그 시각 |
| `thumbnail_url` | string\|null | 게시물 썸네일 (= `media_url` 미러, best-effort 보강) |

> 응답은 기존과 동일하게 **페이지네이션 없는 평면 배열**입니다. 기존 필드는 그대로,
> 위 5개만 추가됐습니다(Breaking 아님).

---

## 3. 검색 — `?search=`

`name` / `description` / 연동 IG `username` 부분일치(대소문자 무시).

```
GET /?search=여름          # 이름·설명·IG username 에 '여름' 포함
```
검색 필드: **`["name", "description", "ig_connection__username"]`** (DRF `SearchFilter`).

---

## 4. (선택) facet 필터 & 정렬

### 필터 (목록에 누적 적용)
| 쿼리 | 값 | 비고 |
|---|---|---|
| `status` | `active`/`paused`/`completed`/`inactive` | 콤마 다중: `status=active,paused` |
| `trigger_type` | `specific_media`/`any_media`/`next_media`/`story_reply` | 콤마 다중 |
| `follow_gate_enabled` | `true`/`false` | |
| `public_reply_enabled` | `true`/`false` | |
| `created_after` / `created_before` | `YYYY-MM-DD` 또는 ISO8601 | 날짜만 주면 그날 전체 포함(Asia/Seoul) |
| `ig_connection_id` | uuid | 특정 IG 계정만 |

허용 외 값(`status`/`trigger_type`/불리언/날짜 형식 오류)은 **400**.

### 정렬 — `?ordering=`
콤마 다중, `-` 접두사 내림차순, 기본 `-created_at`.
허용 필드: `created_at`, `updated_at`, `name`, `status`, `total_sent`, `total_failed`,
`started_at`, `scheduled_start_at`, `scheduled_end_at`, **`last_sent_at`**(최근 발송순 — 미발송은 항상 뒤).
허용 외 필드는 **400**.

```
GET /?status=active,paused&ordering=-last_sent_at        # 활성/일시정지를 최근 발송순
GET /?trigger_type=story_reply&follow_gate_enabled=true  # 스토리답장 + 게이트 사용
```

---

## 5. (선택) 벌크 액션

```
POST /bulk-pause/     body: { "ids": ["<uuid>", ...] }   # 최대 200개
POST /bulk-resume/    body: { "ids": [...] }
POST /bulk-delete/    body: { "ids": [...] }
```

### 응답 (200) — 건별 부분 성공
```jsonc
{
  "succeeded": ["<uuid>", "<uuid>"],
  "failed": [ { "id": "<uuid>", "reason": "not_found" } ]
}
```
- 권한 없거나 존재하지 않는 id 는 `failed`(reason=`not_found`)에 담기고, **나머지는 정상 처리**됩니다(전체 실패 아님).
- `bulk-resume` 는 단건 resume 과 동일하게 **과거가 된 종료 예약(scheduled_end_at)을 건별로 자동 해제**합니다.
- `bulk-delete` 는 되돌릴 수 없습니다.
- 에러: ids 누락/형식 오류 → **400**, 인증 → **401**.

---

## 6. (선택) pause / resume 응답 = 갱신된 항목 (인라인 토글)

`POST /{id}/pause/`, `POST /{id}/resume/` 의 응답이 **목록 항목과 동일한 형태**
(2번의 통계 enrichment 포함)의 **갱신된 캠페인 객체 1건**입니다.
→ 토글 후 목록 전체를 다시 부르지 말고 **해당 1건만 교체**하면 됩니다.

```
POST /api/v1/integrations/auto-dm-campaigns/{id}/pause/
→ 200, { ...캠페인 전체 필드 + delivery_rate/needs_attention_count/... }
```

---

## 용어 정의 (집계 기준)

- **delivery_rate** = `(delivered + read) / (accepted + delivered + read + failed_no_trace)`
  (= 기존 검증 통계 `stats` 와 동일 정의. 0~1)
- **success_rate** = `(delivered + read + legacy sent) / 전체 로그 수` (0~1)
- **needs_attention** = 사용자 조치가 필요한 로그 = 상태가
  `failed_token`(토큰 만료·재연동) / `failed_window`(24h 윈도우 만료) /
  `failed_param`(파라미터 오류) / `failed_no_trace`(도착 미확인·자가점검) 중 하나.
- **sent_this_month(quota)** = 실제 Meta 접수 이상 상태
  (`accepted`/`delivered`/`read`/`failed_no_trace`/legacy `sent`) 의 이번 캘린더월 건수.
  큐 대기/스킵/레이트리밋/거부성 실패(token/window/param)는 quota 미소진으로 제외.

---

## 변경 요약 (백엔드)
- 신규 엔드포인트: `summary/`, `bulk-pause/`, `bulk-resume/`, `bulk-delete/`
- 목록/pause/resume 응답: 통계 enrichment 5필드 추가 (기존 필드 유지)
- 목록 쿼리 파라미터: `search`, `trigger_type`, `follow_gate_enabled`, `public_reply_enabled`,
  정렬 `last_sent_at` 추가
- 마이그레이션 없음, 기존 응답 필드 제거/변경 없음 → **무중단 적용 가능**
