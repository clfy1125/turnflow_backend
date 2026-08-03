# 인스타 성장 리포트 — 프론트엔드 연동 가이드

> **대상**: 유저 콘솔 프론트엔드
> **백엔드 상태**: 구현 완료(2026-08-03) · **dev 반영 완료(FAKE 모드 ON, 약 10초)** · 테스트 36건 통과
> **2026-08-03 변경**: 산출물을 **PDF → 자기완결 HTML 파일**로 통일. 필드명도 함께 바뀌었습니다
>   (`pdf_ready`→`download_ready`, `pdf_download_url`→`download_url`, `pdf_bytes`→`html_bytes`).
> **API 스펙 원본**: `/api/schema/` (사내 MCP 문서 서버가 읽는 것과 동일). 이 문서는 그 위의 **흐름·UX 계약** 설명서다.
> API tag: **`insta-reports`** · operationId 접두: `insta_reports_*`

---

## 0. 한눈에

| 항목 | 값 |
|---|---|
| 기능 | 연동된 인스타 계정의 최근 게시물(최대 100개)을 분석해 **성장 리포트(HTML 파일)** 생성 |
| 권한 | **프로 플랜 전용** (`insta_report`). 무료·베이직은 403 |
| 이용 횟수 | **IG 계정 1개당 캘린더월 1회.** 추가 IG 계정(9,900원)마다 1회씩 늘어남(연동 2개 = 각 1회 = 월 총 2회). `admin` 플랜·관리자 계정은 무제한(`limit: -1`) |
| 소요 시간 | **평균 15~18분** (최대 30분). 서버 응답의 `estimated_minutes` 를 그대로 쓸 것 |
| 진행 표시 | 10단계 · `progress` 0~100 · 3초 폴링 |
| 완료 알림 | 프론트 팝업 + **이메일 자동 발송** (창을 닫아도 결과를 받는다) |
| 산출물 | **자기완결 HTML 1개 파일** (썸네일 data-URI + 차트 라이브러리 인라인 → 인터넷 없이 열림, 탭·차트 그대로 동작). 인증 다운로드 엔드포인트로만 접근 |
| 보관 | 리포트 파일·집계 **계속 보관** (자동 삭제 없음) |
| 실패 시 | **이용 횟수 미차감** → "다시 시도" 버튼을 열어 줘도 된다 |

엔드포인트 4개 (모두 `Authorization: Bearer <access>` 필요):

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `/api/v1/insta-reports/targets/` | 분석 팝업 데이터 (계정 카드 + 버튼 활성 여부 + 남은 횟수) |
| POST | `/api/v1/insta-reports/` | 생성 시작 → **202** |
| GET | `/api/v1/insta-reports/{id}/` | 진행 상태 폴링 / 완료 결과 |
| GET | `/api/v1/insta-reports/` | 내 리포트 목록(히스토리, 페이지당 20) |
| GET | `/api/v1/insta-reports/{id}/download/` | 리포트 다운로드 (`text/html`, 첨부) |

---

## 1. 사용자 흐름

```
[분석 탭] ──GET targets/──▶ 계정 카드 N개 렌더 (프로필사진·이름·팔로워·게시물수)
    │
    └─ [분석 시작] 클릭 ──POST /──▶ 202 {id}
                                     │
                        ┌────────────┴─────────────┐
                        │ 진행 모달 (3초 폴링)      │  ← steps[] 체크리스트 + progress 바
                        │ "평균 15분 걸려요.        │
                        │  창을 닫아도 이메일로     │
                        │  알려드려요."             │
                        └────────────┬─────────────┘
                                     │ status=succeeded
                          완료 팝업 ─┴─▶ [리포트 다운로드]  (+ 서버가 이메일 발송)
```

**중요**: 사용자가 모달/브라우저를 닫아도 생성은 계속된다. 다시 들어오면 `targets/` 의
`running_report_id` 로 진행 화면을 복구할 수 있다.

---

## 2. GET `/insta-reports/targets/` — 분석 팝업

팝업을 열 때마다 호출한다. 프로필 통계(팔로워·게시물 수)는 서버가 6시간 캐시로 관리하고,
만료됐으면 이 호출이 인스타에서 새로 받아 갱신한다(실패해도 캐시값으로 응답 → **팝업은 절대 안 깨진다**).

```jsonc
{
  "plan_required": "pro",
  "has_feature": true,              // 현재 사용자가 프로 기능 보유 중인지
  "estimated_minutes": 18,          // 안내 문구에 그대로 사용
  "estimated_seconds": 1090,
  "quota": {
    "per_account_limit": 1,         // 계정당 월 횟수 (-1 = 무제한/관리자)
    "total_limit": 2,               // 활성 연동 수 × per_account_limit
    "total_used": 1,
    "total_remaining": 1,
    "period_end": "2026-08-01T00:00:00+09:00"   // 횟수 초기화 시각
  },
  "accounts": [
    {
      "connection_id": "3f1c9b2e-…",            // POST 에 그대로 넣는 값
      "username": "reels_drgn",
      "name": "이지용 | 릴스 드래곤",
      "profile_picture_url": "https://media.turnflow.clfy.ai.kr/…webp",
      "followers_count": 98293,
      "media_count": 672,
      "display_line": "@reels_drgn · 팔로워 98,293 · 게시물 672개",  // 그대로 렌더 가능
      "is_active": true,
      "can_generate": true,
      "reason": null,                            // 아래 표 참고
      "reason_message": "",                      // 사용자에게 그대로 노출 가능
      "used": 0, "limit": 1, "remaining": 1,
      "next_available_at": null,                 // QUOTA_EXCEEDED 일 때만 값이 있음
      "running_report_id": null,                 // 값이 있으면 = 지금 생성 중
      "last_report": { "id": "c0de…", "created_at": "…", "period_from": "2026-02-03", "period_to": "2026-07-11" }
    }
  ]
}
```

### 카드 렌더 규칙

| UI | 데이터 |
|---|---|
| 프로필 이미지 | `profile_picture_url` (빈 문자열이면 이니셜 placeholder) |
| 제목 | `name` (없으면 `@username`) |
| 부제 | `display_line` — 예: `@reels_drgn · 팔로워 98,293 · 게시물 672개` |
| 분석 버튼 | `disabled = !can_generate`, 툴팁/헬퍼텍스트 = `reason_message` |
| 생성 중 배지 | `running_report_id !== null` → 버튼 대신 "생성 중" + 그 id 로 폴링 화면 복구 |
| 헤더 우측 | `이번 달 남은 리포트 {quota.total_remaining}회` (`-1` 이면 "무제한") |
| 소요 안내 | `평균 {estimated_minutes}분 정도 걸려요` |

### `reason` 코드

| reason | 의미 | 권장 처리 |
|---|---|---|
| `PLAN_REQUIRED` | 프로 아님 | 업그레이드 CTA |
| `QUOTA_EXCEEDED` | 이번 달 이 계정 몫 사용 완료 | `next_available_at` 로 "8월 1일부터 가능" 표기 |
| `ALREADY_RUNNING` | 워크스페이스에 생성 중 리포트 있음 | `running_report_id` 로 진행 화면 이동 |
| `CONNECTION_INACTIVE` | 소프트 비활성 계정 | 계정 활성화 화면 링크 |
| `TOKEN_EXPIRED` | 인스타 연결 만료 | 재연결 화면 링크 |

---

## 3. POST `/insta-reports/` — 생성 시작

```http
POST /api/v1/insta-reports/
Content-Type: application/json

{ "connection_id": "3f1c9b2e-0a44-4a3c-9d0e-1b2c3d4e5f60" }
```

**성공: 202 Accepted** — 본문은 4장의 상태 객체와 동일(`status: "queued"`, `progress: 0`).
받는 즉시 진행 화면으로 전환하고 폴링을 시작한다.

### 에러

| HTTP | `error.details.code` / `error.code` | 처리 |
|---|---|---|
| 403 | `PLAN_REQUIRED` (+`plan_required: "pro"`) | 업그레이드 유도 |
| 409 | `ALREADY_RUNNING` (+`running_report_id`) | 그 id 로 진행 화면 이동 |
| 429 | `PLAN_LIMIT_EXCEEDED` (`error.code`) | "다음 달 1일에 다시 가능" 안내 |
| 400 | `CONNECTION_INACTIVE` / `TOKEN_EXPIRED` | 계정 설정으로 유도 |
| 404 | — | 내 소유가 아닌 `connection_id` |

에러 본문은 서비스 표준 포맷이다:

```json
{ "success": false,
  "error": { "code": 409,
             "message": "리포트를 만들고 있어요. 완료된 뒤에 다시 시도해 주세요.",
             "details": { "code": "ALREADY_RUNNING", "running_report_id": "8f14…" } } }
```

> ⚠️ 429 만 `error.code` 가 문자열(`"PLAN_LIMIT_EXCEEDED"`)이고 나머지는 숫자다(서비스 전역 규칙).
> 분기는 **HTTP status + `error.details.code`** 로 하는 게 안전하다.

---

## 4. GET `/insta-reports/{id}/` — 진행 폴링

**권장 간격 3초.** 응답:

```jsonc
{
  "id": "8f14e45f-…",
  "status": "running",                  // queued | running | succeeded | failed | cancelled
  "stage": "extracting",
  "stage_label": "영상 분석 중",        // 그대로 노출 가능
  "progress": 44,                        // 0~100, 절대 역행하지 않음
  "message": "영상 분석 12/30",         // 현재 세부 진행
  "eta_seconds": 640,                    // 남은 예상 시간(완료/실패 시 null)
  "stage_started_at": "2026-07-29T20:41:02+09:00",
  "stage_expected_seconds": 360,
  "steps": [                             // 체크리스트로 그대로 렌더
    { "key": "collecting", "label": "게시물 모으는 중", "status": "done",
      "detail": "", "progress_start": 3, "progress_end": 15, "expected_seconds": 120 },
    { "key": "extracting", "label": "영상 분석 중", "status": "active",
      "detail": "영상 분석 12/30", "progress_start": 30, "progress_end": 65, "expected_seconds": 360 }
    // …총 10개
  ],
  "account": { "connection_id": "3f1c…", "username": "reels_drgn",
               "name": "이지용 | 릴스 드래곤", "followers_count": 98293, "media_count": 672 },
  "posts_analyzed": 0, "reels_with_views": 0, "videos_analyzed": 0, "comments_analyzed": 0,
  "period_from": null, "period_to": null,
  "download_ready": false, "download_url": null, "html_bytes": 0,
  "error_code": "", "error_message": "",
  "created_at": "…", "started_at": "…", "finished_at": null, "elapsed_seconds": 0
}
```

### 10단계와 진행률 구간

| # | key | label | 진행률 | 평균 |
|---|---|---|---|---|
| 1 | `queued` | 대기 중 | 0→3 | 10s |
| 2 | `collecting` | 게시물 모으는 중 | 3→15 | 120s |
| 3 | `metrics` | 숫자 계산 중 | 15→20 | 5s |
| 4 | `preparing` | 영상 내려받는 중 | 20→30 | 150s |
| 5 | `extracting` | 영상 분석 중 | 30→65 | **360s** ← 가장 긴 구간, `message` 에 n/N |
| 6 | `comments` | 댓글 분석 중 | 65→72 | 50s |
| 7 | `synthesizing` | 인사이트 쓰는 중 | 72→88 | **260s** ← 서버 이벤트 없음 |
| 8 | `verifying` | 검수하는 중 | 88→93 | 120s |
| 9 | `rendering` | 리포트 만드는 중 | 93→97 | 5s |
| 10 | `exporting` | 파일로 저장하는 중 | 97→100 | 8s |

### 멈춘 것처럼 보이지 않게 하기 (필수 UX)

`synthesizing`(3~5분)·`verifying` 구간은 서버가 중간 이벤트를 주지 않는다. 클라이언트에서
`stage_started_at` + `stage_expected_seconds` 로 **보간**하라. 단, `progress_end` 를 넘기지 말 것.

```ts
function displayProgress(r: Report): number {
  const step = r.steps.find(s => s.status === "active");
  if (!step) return r.progress;
  const elapsed = (Date.now() - new Date(r.stage_started_at).getTime()) / 1000;
  const ratio = Math.min(elapsed / Math.max(r.stage_expected_seconds, 1), 0.97); // 끝값은 서버가 확정
  const interpolated = step.progress_start + (step.progress_end - step.progress_start) * ratio;
  return Math.max(r.progress, Math.floor(interpolated));   // 서버 값보다 뒤로 가지 않게
}
```

### 종료 처리

| status | 처리 |
|---|---|
| `succeeded` | 완료 팝업 + `download_url` 로 다운로드. 요약 수치(`posts_analyzed`·`videos_analyzed`·`comments_analyzed`·`period_from~period_to`) 표시 |
| `failed` | `error_message`(한국어 완성 문구)를 그대로 노출 + **다시 시도** 버튼(횟수 미차감) |
| `cancelled` | 스위퍼/관리자에 의한 중단 — 다시 시도 안내 |

### `error_code` 목록

| code | 사용자 문구(서버가 `error_message` 로 제공) |
|---|---|
| `VIEWS_UNAVAILABLE` | 조회수 정보를 가져오지 못했어요. 잠시 후 다시 시도해 주세요. |
| `NOT_ENOUGH_REELS` | 조회수를 확인할 수 있는 릴스가 5개보다 적어 리포트를 만들 수 없어요. |
| `NO_POSTS` | 분석할 게시물을 찾지 못했어요. |
| `TOKEN_INVALID` | 인스타그램 연결이 만료됐어요. 계정을 다시 연결한 뒤 시도해 주세요. |
| `EXTRACT_FAILED` / `SYNTH_FAILED` / `RENDER_FAILED` | 각 단계 실패 — "잠시 후 다시 시도" |
| `TIMEOUT` | 생성 시간이 너무 오래 걸려 중단했어요. |
| `INTERNAL` | 일시적인 오류로 리포트를 만들지 못했어요. |

> `error_message` 는 이미 사람말이다. 프론트에서 별도 사전을 만들 필요 없다.
> (내부 디버그 상세는 응답에 포함되지 않는다.)

---

## 5. GET `/insta-reports/` — 히스토리

쿼리: `connection_id`, `status`, `page` (페이지당 20). 표준 페이지네이션(`count`/`next`/`previous`/`results`).

행 필드: `id`, `status`, `progress`, `ig_username`, `ig_name`, `posts_analyzed`,
`reels_with_views`, `videos_analyzed`, `comments_analyzed`, `period_from`, `period_to`,
`download_ready`, `download_url`, `html_bytes`, `error_code`, `error_message`,
`created_at`, `finished_at`.

`download_ready === true` 인 행만 다운로드 버튼을 활성화한다.

---

## 6. GET `/insta-reports/{id}/download/` — 리포트 파일

완료된 리포트를 **자기완결 HTML 파일 1개**로 내려준다.

- 썸네일은 `data:` URI, 차트 라이브러리는 파일 안에 인라인 → **인터넷 없이 열어도** 그대로 보인다
- 탭 4개(개요 / 콘텐츠 분석 / 팔로워 인사이트 / 강점&전략)와 차트 3종이 살아 있는 인터랙티브 문서
- 사용자가 브라우저에서 `Ctrl+P` 로 인쇄·PDF 저장하면 인쇄용 레이아웃으로 나온다(CSS 준비돼 있음)
- 크기: 보통 **300~500KB** (실측 355KB)

### 보안 — 반드시 지킬 것

리포트에는 **팔로워 댓글 원문**이 들어가고 파일 안에 스크립트가 인라인돼 있다.

- 공개 URL 은 제공하지 않는다. 이 엔드포인트만이 접근 경로이며, 내 소유가 아니면 404.
- 서버는 항상 `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff` 로 내려준다
  (브라우저가 우리 출처에서 실행하지 않고 저장하게).
- **앱 안에서 미리보기**를 붙이려면 blob URL 을 `<iframe sandbox="allow-scripts">` 안에서 띄운다.
  같은 출처에 그대로 렌더하지 말 것.

### 프론트 구현

`<a href>` 로는 Authorization 헤더를 실을 수 없으니 fetch → blob 저장을 쓴다.

```ts
async function downloadReport(reportId: string, token: string) {
  const res = await fetch(`/api/v1/insta-reports/${reportId}/download/`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (res.status === 409) { /* 아직 준비 안 됨 (FILE_NOT_READY) */ return; }
  if (!res.ok) throw new Error(`download failed: ${res.status}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `turnflow_report_${reportId}.html`;   // 서버도 Content-Disposition 을 준다
  a.click();
  URL.revokeObjectURL(url);
}
```

| HTTP | 의미 |
|---|---|
| 200 | `text/html; charset=utf-8` (+ `Content-Disposition: attachment`) |
| 409 | `FILE_NOT_READY` — 생성 중이거나 실패 |
| 404 | 없음 / 내 소유 아님 |

## 7. 이메일 알림

생성 성공 시 서버가 요청자에게 자동 발송한다 — 템플릿 `insta_report_ready`.
제목: `[TurnFlow] @{username} 인스타 분석 리포트가 완성됐어요 📊`
본문 버튼 링크: `{FRONTEND_URL}/insta-reports/{id}`

**프론트에 필요한 조치**: 위 경로(`/insta-reports/:id`)로 진입했을 때 해당 리포트의
완료 화면(또는 진행 화면)을 열어 주는 라우트가 있어야 한다.

실패 시에는 메일을 보내지 않는다(인앱 표시만).

---

## 8. 개발 중 15분 기다리지 않는 방법 (dev FAKE 모드 — **현재 dev 에 켜져 있음**)

`INSTA_REPORT_FAKE_MODE=True` 면 외부 호출 0 · 합성 데이터로 **약 10초**에 완료된다.
상태 전이·`steps`·파일 다운로드까지 실제와 동일한 경로를 탄다(숫자·문장만 더미).

**진행률은 실제와 같은 비중으로 흐른다** — 10초를 실제 단계 가중치로 쪼개 대기하므로,
프로덕션에서 볼 퍼센트 곡선을 100배 빠르게 그대로 볼 수 있다. dev 실측:

| 경과 | stage | % | eta | message |
|---|---|---|---|---|
| 0.2s | queued | 0 | 10 | 대기 중이에요 |
| 0.6s | collecting | 3 | 10 | 게시물을 모으고 있어요 |
| 1.0s | preparing | 20 | 9 | 영상을 내려받고 있어요 |
| 1.8s | extracting | 32 | 7 | 영상 분석 2/30 |
| 3.0s | extracting | 53 | 7 | 영상 분석 20/30 |
| 3.8s | comments | 65 | 4 | 댓글을 분석하고 있어요 |
| 4.2s | synthesizing | 72 | 4 | 인사이트를 쓰고 있어요 |
| 5.4s | verifying | 88 | 1 | 숫자와 표현을 검수하고 있어요 |
| 6.3s | exporting | 97 | 0 | 파일로 저장하고 있어요 |
| 9.5s | done | 100 | null | 리포트가 완성됐어요 |

- `stage_expected_seconds` / `eta_seconds` 도 같은 배율로 축소돼서 온다 → **클라이언트 보간
  코드를 그대로 검증할 수 있다**(서버는 10초에 끝나는데 "18분 남음"이 뜨는 일 없음).
  `progress_start` / `progress_end`(퍼센트 구간)는 프로덕션과 **동일**하다.
- `metrics` 단계는 전체의 0.5%라 순간에 지나간다(프로덕션도 마찬가지). 못 봐도 정상.
- 소요 시간은 `INSTA_REPORT_FAKE_DELAY_SECONDS` 로 조절한다(기본 10초). 더 천천히 보고
  싶으면 30~60초로 올려 달라고 요청하면 된다.
- **prod 는 항상 꺼져 있다**(기본값 False). 가짜 리포트가 실사용자에게 갈 경로는 없다.

### 반복 테스트 — 쿼터에 막히지 않으려면

FAKE 모드에서도 **플랜 게이트·쿼터는 실제와 똑같이 동작한다**(그래서 403/409/429 화면도
여기서 검증 가능하다). 다만 프로 계정은 **계정당 월 1회**라 두 번째부터 429가 뜬다.

→ **테스트 계정을 `admin` 플랜으로 올리면 무제한**(`per_account_limit: -1`)이다.
   프론트가 몇 번이든 눌러 볼 수 있고, `quota.total_limit/-remaining` 이 `-1` 로 오니
   "무제한" 표기 분기도 함께 검증된다. 필요하면 백엔드에 어느 계정을 올릴지 알려 주면 된다.
   (429 화면을 볼 차례가 되면 프로 계정으로 두 번 눌러 보면 된다.)

---

## 9. 구현 체크리스트

- [ ] 분석 탭: `targets/` 1회 호출로 카드 렌더 (`display_line` 그대로 사용)
- [ ] 버튼 비활성 + `reason_message` 노출 (5가지 reason 전부)
- [ ] "이번 달 남은 리포트 N회" 표기 (`quota.total_remaining`, `-1`=무제한)
- [ ] POST 202 → 진행 모달, **3초 폴링**
- [ ] `steps[]` 체크리스트 + `progress` 바 + `displayProgress()` 보간
- [ ] "평균 15분 / 창을 닫아도 이메일로 알려드려요" 안내 문구
- [ ] 모달 재진입 복구: `targets/` 의 `running_report_id`
- [ ] 완료 팝업 + fetch/blob 리포트(HTML) 다운로드
- [ ] 실패 시 `error_message` 노출 + 다시 시도 (횟수 미차감이라 즉시 재시도 가능)
- [ ] 히스토리 목록 + 지난 리포트 재다운로드
- [ ] 라우트 `/insta-reports/:id` (이메일 링크 대상)
- [ ] 403 → 업그레이드 CTA / 429 → 다음 달 안내

---

## 10. 참고 (백엔드 내부 — 프론트가 알아 두면 좋은 것)

- 조회수는 인스타 인사이트 권한이 없어 **공개 데이터 수집**으로 얻는다. 그래서 수집 실패
  (`VIEWS_UNAVAILABLE`)가 구조적으로 존재하며, 이 경우 횟수를 차감하지 않는다.
- 영상 분석은 계정별로 캐시된다 → **같은 계정을 다시 분석하면 훨씬 저렴**하지만, 소요 시간은
  비슷하다(합성·검수 단계가 그대로 돌기 때문). 사용자 안내 문구는 항상 "평균 15분"으로.
- 워크스페이스당 동시 생성은 1건이다(비용·큐 보호).
- 리포트 문장은 서버 검증 게이트를 통과한 것만 실린다(숫자 환각·전문용어 차단). 프론트에서
  후처리하거나 문장을 잘라 쓰지 말 것 — 근거 숫자와 문장이 세트로 검증돼 있다.
