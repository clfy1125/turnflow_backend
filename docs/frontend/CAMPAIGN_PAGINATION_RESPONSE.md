# [백엔드 → 프론트] 캠페인 목록 페이지네이션 + 「전체 선택」 응답

작성: 2026-08-16 (백엔드) · 받은 문서: `backend-campaign-pagination.md`
대상 서버: **dev** (`https://dev-api.turnflow.link`) — **이미 반영돼 있습니다.**

> **요약**
> 1. 페이지네이션 열었습니다. **opt-in** 이라 프론트와 동시 배포할 필요가 없습니다.
> 2. 「전체 선택」은 프론트가 id 를 모으는 방식이 아니라 **서버가 필터로 전체를 잡는 방식**으로
>    지원합니다 — 페이지를 넘겨가며 300개를 긁어모으지 않으셔도 됩니다.
> 3. 「이 페이지 전체 선택」으로 문구를 바꾸실 필요 **없습니다.** 진짜 전체 선택이 됩니다.

---

## 1. 목록 페이지네이션 — `page` 를 주면 봉투, 안 주면 그대로

| 요청 | 응답 |
|---|---|
| `?workspace_id=..` (지금과 동일) | **평면 배열** — 하나도 안 바뀝니다 |
| `?workspace_id=..&page=1&page_size=20` | `{ count, next, previous, results }` |

```
GET /api/v1/integrations/auto-dm-campaigns/?workspace_id={ws}&page=1&page_size=20
    &status=active,paused&source=dm_migration&search=룩북
    &created_after=2026-08-01&ordering=-created_at
→ 200 { "count": 300, "next": "...&page=2&page_size=20", "previous": null, "results": [ ... ] }
```

- **`page_size` 기본 20, 최대 100.** 범위 밖은 400 이 아니라 **clamp** 합니다(요청하신 대로).
  `page_size=9999` → 100개, `page_size=0` → 1개, `page_size=abc` → 20개.
- 기존 필터·정렬 **전부 같이 동작**합니다. 새로 배운 파라미터는 `page`/`page_size` 둘뿐입니다.
- **`count` 는 "현재 필터에 걸린 전체 수"** 입니다. 화면의 `캠페인 N개` 와 `N개 선택됨` 은
  이 값 하나로 그리시면 됩니다.
- 썸네일 확보 예약도 **현재 페이지 항목만** 겁니다(예전엔 300개 계정에서 목록 한 번에 300건이
  큐로 갔습니다).

### 배포 타이밍 — 맞출 필요 없습니다

`page` 를 안 보내면 **응답이 지금과 100% 동일**합니다. 프론트는 준비되는 화면부터 하나씩
옮기시면 되고, 백엔드가 언제 나가는지 신경 쓰지 않으셔도 됩니다. 나중에 봉투로 통일하고
싶어지면 그때 따로 합의하시죠.

---

## 2. 「전체 선택」 — id 를 모으지 마시고 `all: true` 를 보내세요

첨부해주신 화면(하단 `1개 선택됨 · 활성화 · 일시정지 · 삭제`)이 페이지네이션 후에도
**전체에 걸리게** 하는 게 핵심이라, 서버가 필터로 대상을 잡도록 만들었습니다.

```
POST /api/v1/integrations/auto-dm-campaigns/bulk-resume/     ← 활성화
POST /api/v1/integrations/auto-dm-campaigns/bulk-pause/      ← 일시정지
POST /api/v1/integrations/auto-dm-campaigns/bulk-delete/     ← 삭제

  ?workspace_id={ws}&status=paused&source=dm_migration&search=룩북&created_after=...
      ↑ **목록 GET 에 쓰신 쿼리스트링을 그대로 붙이세요**

  { "all": true, "exclude_ids": ["<사용자가 체크를 푼 id>"] }
```

- **대상 계산은 목록과 같은 코드**를 탑니다. 그래서 `GET` 의 `count` 와 처리 건수가
  **항상 일치**합니다("300개 선택됨" 을 눌렀는데 20개만 처리되는 일이 구조적으로 안 생깁니다).
- 개별 선택은 지금처럼 `{ "ids": [...] }` 그대로 쓰시면 됩니다.
  **`all` 과 `ids` 를 같이 보내면 400** 입니다(둘 중 하나만).
- **상한 1000건.** 넘으면 `400` + `code: "too_many_targets"` 와 `count`·`max` 를 함께 드립니다.
  일부만 조용히 처리하지 않습니다 — 필터를 좁혀 나눠 실행하도록 안내해주세요.
- 응답은 기존과 동일한 **건별 부분 성공** 입니다.

```json
{ "succeeded": ["<uuid>", ...], "failed": [{"id": "...", "reason": "duplicate_active_campaign"}] }
```

> 활성화(`bulk-resume`)는 같은 게시물에 이미 활성 캠페인이 있으면 **그 건만** `failed` 로
> 격리됩니다(`duplicate_active_campaign`). 전체가 실패하지 않습니다.

### 실측 (dev · `mig-bulk` 300개 계정)

```
GET  ?source=dm_migration&page=1&page_size=20   → count=100, 화면엔 20개
POST bulk-resume/?source=dm_migration {all:true} → succeeded=100  failed=0  (1,046 ms)
GET  ?status=active                              → count=100      ✔ 일치
POST {all:true, ids:[...]}                       → 400
```

---

## 3. 「서버가 열리면 프론트가 함께 고쳐야 하는 것」 — 항목별 답

| 항목 | 답 |
|---|---|
| 정렬 | 서버 `ordering` 으로 이관하세요. 가능 값: `created_at`·`updated_at`·`name`·`status`·`total_sent`·`total_failed`·`started_at`·`scheduled_start_at`·`scheduled_end_at`·`last_sent_at` (각 `-` 내림차순, 콤마 다중). 목록의 `최신순` 셀렉트는 `ordering=-created_at` 입니다. **허용 목록 밖은 400.** |
| 「전체 활성화」 | **한 번의 호출**로 됩니다 — `POST bulk-resume/?source=dm_migration&status=inactive` + `{"all": true}`. 별도 조회로 id 를 모으지 않으셔도 됩니다. |
| 「전체 선택」 | 문구 그대로 두세요. 선택 개수는 `count`, 실행은 `{"all": true}`. **체크를 푼 항목만** `exclude_ids` 로 빼면 됩니다. |
| 활성 개수 배지 | `GET .../auto-dm-campaigns/summary/?workspace_id={ws}` 를 쓰세요. 이미 있고, 상단 카드 5개를 전부 채웁니다 (아래 참고). |
| 설문 판단 | 그대로 두시면 됩니다. |

상단 카드용 `summary/` 응답 (실제 값):

```json
{ "counts": { "active": 0, "paused": 300, "completed": 0, "inactive": 0, "total": 300 },
  "usage":  { "sent_this_month": 0, "monthly_free_limit": 200, "remaining_this_month": 200 },
  "delivery": { "total_sent": 0, "delivery_rate": 0.0, "success_rate": 0.0,
                "needs_attention_total": 0 },
  "last_activity_at": null }
```

> `summary/` 는 **필터와 무관한 계정 전체 집계**입니다(상단 카드용). 필터가 걸린 개수는
> 목록의 `count` 를 쓰세요. 둘은 다른 숫자이며, 그게 의도입니다.

---

## 4. 효과 (dev 실측 · 캠페인 300개 계정)

| | 응답 크기 | 시간 |
|---|---|---|
| 지금 (평면 300건) | **555 KB** | 1,449 ms |
| `page=1&page_size=20` | **37 KB** | 918 ms |

카드별 `stats/`·`queue/` 추가 호출도 **300 → 20** 으로 줄어듭니다.

> 남은 비용 하나: 목록은 이벤트 단위 `total_sent` 만 주고 카드가 쓰는 **사람 단위
> `unique_sent`** 가 없어 카드마다 `stats/` 를 부르신다고 하셨습니다. 목록 응답에
> 사람 단위 값을 얹는 건 **별도 건으로 검토하겠습니다** — 이번 변경엔 안 들어갔습니다.
> 페이지네이션만으로도 그 호출이 페이지당 20건으로 줄어드니 급한 불은 꺼집니다.

---

## 5. 실사용 캠페인 수 분포 (p50/p90/max) — 아직 못 드립니다

운영 DB 조회에 Cloudflare Access 브라우저 인증이 필요해 지금 자리에서 못 뽑았습니다.
**뽑는 대로 따로 알려드리겠습니다.**

다만 우선순위 판단에는 크게 영향이 없을 것 같습니다 — 페이지네이션이 **opt-in** 이라
프론트가 붙이는 데 백엔드 일정이 걸려 있지 않고, 안 붙이면 지금과 완전히 동일합니다.

---

## 6. 테스트

```
apps/integrations                      354 passed   (신규 9개 포함)
apps/integrations/tests_campaign_*.py   49 passed   (기존 벌크 계약 회귀 없음)
```

신규 테스트가 고정하는 불변식은 하나입니다 — **`GET` 의 `count` == `all:true` 가 처리한 건수.**
페이지 경계에서 항목이 겹치거나 빠지지 않는 것, `page_size` clamp, 상한 초과 시 아무것도
바뀌지 않는 것, 남의 워크스페이스 캠페인은 `all:true` 라도 손대지 않는 것도 함께 고정했습니다.
