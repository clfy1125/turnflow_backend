# [어드민팀 전달] DM 오류 분류 개편 — API 변경 안내 + 반영 요청

2026-07-31 · 백엔드 → 어드민 콘솔팀
관련 백엔드 문서: `../system/DM_ERROR_POLICY_PLAN.md` (분류 원본 `../system/DM_ERROR_POLICY_MATRIX.html`)

---

## 0. 세 줄 요약

1. DM 오류 분류를 **2가지(🔴 확인해야함 / ⚪ 정상)** 로 단순화했고, 서버가 `policy` 필드로 내려줍니다.
2. **백엔드만 배포해도 어드민은 깨지지 않습니다.** 새 필드는 무시되고, 오류 문구는 오히려 정확해집니다.
3. 다만 어드민에 **버그 3건**이 있어 이건 어드민 배포가 있어야 사라집니다. (§3, B1 이 가장 시급)

---

## 1. 왜 바꾸나

기존에는 오류를 볼 때마다 "이건 누구 잘못이고 뭘 해야 하지?"를 매번 판단해야 했습니다.
운영자가 화면에서 답할 질문을 **하나로** 줄였습니다.

> **"사람이 봐야 하는가, 아니면 정해진 대로 자동 처리되는가?"**

| 값 | 뜻 | 예 |
|---|---|---|
| `investigate` 🔴 | 원인이 확정되지 않았거나 우리 판단·조치가 필요 → **사람이 열어봐야 함** | 원인 불명(-1), 이미 답글, 도착 미확인, 발송 방치 |
| `normal` ⚪ | 원인이 확정돼 있고 대응도 정해져 있음 → 자동 처리·안내 | 토큰 만료(재연동 안내), 월 한도(결제 유도), 수신자 차단 |

"재연동 안내"·"결제 유도"처럼 **대응이 이미 정해진 것은 정상(⚪)** 으로 뺐습니다.
사람 손이 필요한 것만 🔴에 남깁니다. 전체 52개 사유 중 **🔴 20 / ⚪ 32** 입니다.

---

## 2. API 변경 — 전부 **필드 추가**뿐 (기존 필드·값 불변)

### 2-1. `GET /api/v1/admin/dashboard/ops/` → `dm_quality.failure_breakdown[]`

```jsonc
{
  "code": "100", "subcode": "2534023", "status": "failed_no_trace",
  "count": 12,
  "recoverable": true,
  "group": "failed",
  "sample_error_message": "...",
  "title": "댓글에 이미 답글 있음",
  "cause": "...", "action": "...",

  "policy": "investigate",          // ★ 신규
  "policy_display": "확인해야함"      // ★ 신규 (한국어 라벨 — 프론트 하드코딩 불필요)
}
```

### 2-2. 같은 응답 → `dm_quality.skipped_breakdown[]`

```jsonc
{
  "reason": "other", "label": "기타", "count": 2, "actionable": false,
  "policy": "investigate",          // ★ 신규 — 건너뜀은 전부 normal, 미분류(other)만 investigate
  "policy_display": "확인해야함"      // ★ 신규
}
```

### 2-3. `GET /api/v1/admin/auto-dm/logs/` (목록 행)

```jsonc
{ "...": "...", "error_title": "도착 미확인", "error_policy": "investigate" }  // ★ error_policy 신규
```

### 2-4. `GET /api/v1/admin/auto-dm/logs/{id}/` (상세)

```jsonc
{
  "...": "...",
  "error_title": "...", "error_cause": "...", "error_action": "...",
  "recoverable": true,
  "error_policy": "investigate",            // ★ 신규
  "error_policy_display": "확인해야함"        // ★ 신규
}
```

### 계약

- 값은 **`investigate` | `normal` 두 가지뿐**입니다. 세 번째 값은 생기지 않습니다.
- 사전에 없는 새 오류가 나와도 서버가 `investigate` 로 떨어뜨립니다 — 프론트에서 미등록 처리를 따로 안 해도 됩니다.
- 오류가 아닌 행(delivered/read/queued 등)은 `normal` 입니다.
- **한국어 라벨은 서버가 줍니다**(`policy_display`). 프론트에 문구를 두지 마세요.

---

## 3. 어드민에서 고쳐야 할 것

### 🔴 B1. 딥링크 매핑이 틀려서 목록이 빕니다 — **가장 시급**

`src/app/(dashboard)/auto-dm/logs/page.tsx:71`

```ts
const SEND_STATUS_TO_GROUP = {
  failed_no_trace: "hidden_spam",   // ❌ 백엔드는 attention 입니다
  ...
}
```

백엔드 단일 소스(`dm_status_groups.py`)에서 `failed_no_trace → attention` 입니다.
지금은 운영 대시보드의 **"도착 미확인 N건"** 링크를 누르면 `status_group=hidden_spam` 으로 필터돼
**해당 건이 하나도 안 보입니다.**

→ `failed_no_trace: "attention"` 으로 수정.

### 🔴 B2. 딥링크가 오류 코드 필터를 자동 프리셋해서 행이 잘립니다

같은 파일 `:81`

```ts
const SEND_STATUS_TO_CODE = { failed_token: "190", failed_window: "10", failed_param: "100" };
```

실제 데이터에는 `failed_token` 이면서 code 가 `102` 이거나 **빈 문자열**(발송 전 차단)인 건,
`failed_window` 면서 code 가 `100` 인 건이 많습니다. 코드를 프리셋하면 그 행들이 사라져
**"대시보드는 12건인데 목록은 5건"** 이 됩니다.

→ 코드 프리셋 제거(그룹만 전달). 코드별로 좁히는 건 사용자가 직접 선택하게.

### 🟡 B3. 로컬 오류 코드 맵 정리

`src/lib/status.ts:113` `dmErrorCode` 에 **subcode** `2534025` 가 코드인 것처럼 들어 있습니다
(코드 2534025 인 로그는 존재하지 않아 영원히 안 잡힙니다). 반대로 실제로 오는 `102`·`200`·`551`·`-1` 은 없습니다.

→ 이제 서버가 `error_title` + `policy` 를 주므로 **로컬 맵은 폴백 최소화**가 방향입니다.
   `2534025` 항목 제거, 부족한 코드는 굳이 채우지 않아도 됩니다.

### 🟡 D1. 상태 그룹 라벨을 서버 값으로 통일

| 백엔드 `status_group_display` | 현재 어드민 |
|---|---|
| 대기중 / 전송됨 / 읽음 / **숨겨진 요청 · 스팸** / **확인 필요** | 대기 / 발송됨 / 읽음 / **스팸함 유입** / **오류** |

→ 서버가 `status_group_display` 를 이미 내려줍니다. 로컬 라벨 맵 대신 그 값을 렌더해 주세요.
   (유저 콘솔과 어드민이 같은 건에 다른 이름을 쓰고 있어 CS 때 혼선이 납니다.)

### 🟡 D2. "24h 창 만료" 문구 정정

`failed_window` 를 "24h 창 만료"로 표시하는데, **댓글 답장 경로의 실제 창은 7일**입니다
(user_id 경로만 24시간). 서버 `error_title` 을 쓰면 자동으로 맞습니다.

### 🟡 D3. 목 사전 동기화 — `src/mocks/dmErrorCatalog.ts`

서버 사전과 어긋나 있어 **화면 검증이 실제와 다른 값으로 통과**합니다.
누락: `100/2534023`, `551`, `4`, legacy `failed`/`failed_api`.
오기: `613` 에 `recoverable: true` (서버는 false).
추가 필요: 신규 2건 `("", "window_stalled")`, `("", "window_peak")` + 모든 항목에 `policy`.

---

## 4. 화면 재구성 제안 (선택)

지금은 오류가 코드별로 흩어져 있어 운영자가 뭘 먼저 볼지 알기 어렵습니다.
**2단**으로 접으면 평소 볼 것은 8장뿐입니다.

```
발송 안 됨  1,234건
├ 🔴 확인해야함    42건   ← 항상 펼침 · 카드 8장
└ ⚪ 정상 처리  1,192건   ← 기본 접힘 · 펼치면 8장
```

### 🔴 카드 8 (사람이 볼 것)

| 카드 | 묶는 기준 | 버튼 |
|---|---|---|
| 발송 방치 (우리 문제) | `subcode=window_stalled` | 큐/워커 점검 |
| 창 만료 · 원인 확인 | `100/2534022`, `10/2534022`, `10/2018278`, `10+failed_window` | 웹훅 지연 확인 |
| 이미 답글 있음 | `100/2534023` | 중복 캠페인·타사툴 점검 |
| 도착 미확인 | `failed_no_trace` | **재검증** |
| 게시물 자동 DM 차단 | `200/2534066` | 게시물 교체 안내 |
| 파라미터 오류 · 원인 미확정 | `100`·`failed_param` (7일 초과 아닌 것) | 원문 확인 |
| 원인 불명 | `-1`, 세부번호 없는 `10`/`200`, legacy 2종, 사전 미등록 | 원문 확인 |
| 분류 안 된 건너뜀 | `skipped/other` | 원문 확인 |

### ⚪ 카드 8 (접어둘 것)

재연동 필요 / 월 한도 소진 / 숨김함 유입·복구 / 몰려서 지연 /
댓글 7일 초과 / 수신자 사정 / 설정·정리로 건너뜀 / 복구 만료

> 카드에는 **건수와 사유 이름만** 넣어 주세요. 자동 안내의 구현 여부 같은 배지는 넣지 않습니다
> (안내를 구현하면 배지를 다시 걷어내야 하므로). 진행 상황은 백엔드 문서에서 관리합니다.

---

## 5. 배포 순서 · 호환성

**순서 의존성이 없습니다.** 백엔드가 먼저 나가고, 어드민은 편할 때 반영하면 됩니다.

### 어드민을 배포하지 않은 상태에서의 영향 (백엔드에서 검증 완료)

| 변경 | 어드민 화면 |
|---|---|
| `policy` 등 신규 필드 | **변화 없음** — zod 가 모르는 키를 버립니다(`.strict()` 사용처 없음 확인) |
| 오류 문구 보강 | **더 정확해집니다** — 이미 서버 `error_title/cause/action` 을 1순위로 쓰고 계십니다 |
| 창 만료 2분할 | 운영 대시보드 오류 분포에서 `failed_window` 1행 → **2행**으로 갈립니다(라벨은 서버 문구) |
| 〃 | 로그 상세 subcode 칩에 **숫자가 아닌 값**(`window_peak` / `window_stalled`)이 뜹니다 |

마지막 항목만 미리 알아 두시면 됩니다 — 기능 문제는 없고 표시만 낯섭니다.
숫자 subcode 를 가정한 파싱(`parseInt` 등)이 있다면 그 부분만 확인해 주세요.

### 백엔드 변경 요약 (참고)

- 마이그레이션 **없음** (모델 변경 없음)
- `SentDMLog.status` 값 불변 → 집계·KPI·큐 동작 전부 그대로
- 신규 종결 건에만 subcode 표식이 붙습니다. **과거 데이터 소급 변경 없음**
- 배포 후 `admin:dash:ops:*` 캐시 선별 삭제 예정(최대 30초, `window=all` 은 900초 지연)

---

## 6. 질문 주실 곳

- 분류 근거 전수 목록: `../system/DM_ERROR_POLICY_MATRIX.html` (52항목, 사유·원인·조치까지)
- 방침·판정 근거: `../system/DM_ERROR_POLICY_PLAN.md`
- 서버 사전 원본: `apps/admin_api/dm_error_catalog.py`
- 분류 회귀 테스트: `apps/admin_api/tests_dm_error_policy.py` (방침이 바뀌면 여기가 먼저 깨집니다)
