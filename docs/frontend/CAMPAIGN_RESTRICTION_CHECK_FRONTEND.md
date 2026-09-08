# 게시물 제한 점검 · 캠페인 자동 정지 — 프론트엔드 연동 가이드

작성 2026-09-07 · **개정 2026-09-08 (프론트 확인요청 6건 반영 — 아래 §0-1)** · 대상: 프론트엔드 개발자

관련 문서
- 원인 조사 원본: `../system/DM_2534066_MEDIA_BLOCK_CENSUS_2026-09-07.md`
- **고객 안내 문구 원본**: `IG_AGE_RESTRICTION_CREATOR_GUIDE.md` ← 화면 문구는 여기서 가져오세요
- 오류 사유 머신 키 체계: `DM_USER_COPY_MAPPING.md` (`post_restricted` = U5)

---

## 0-1. 2026-09-08 변경 — 프론트 확인요청 6건 반영

프론트에서 올려주신 B1~B6 전부 실제 결함이 맞았습니다. **6건 모두 백엔드에서 고쳤습니다.**

| # | 지적 | 조치 | 프론트가 할 일 |
|---|---|---|---|
| **B5** | 제한 게시물의 **복사**가 409로 막힘 | ✅ **복사 허용.** 게이트를 **활성화 시점**으로 옮김 | 복사 버튼 **잠그지 마세요** |
| **B1** | 409의 `restriction` 값이 파이썬 문자열(`"True"`, `"None"`) | ✅ 200과 **완전히 같은 직렬화**. `user_message`·`how_to_check`·`next_steps`도 포함 | **정규화 함수 제거하세요** |
| **B2** | `suspected`인데 `can_create_campaign:false`·`blocking:true` | ✅ `blocking`은 이제 `state==restricted`와 **동치**. suspected → `can_create_campaign:true` | 두 필드 **써도 됩니다** |
| **B3** | 실패 수가 시도 횟수라 "명"으로 못 씀 | ✅ `opening_failed_unique_users`·`opening_success_unique_users` 추가 | **"N명" 표기 가능** |
| **B4** | 임계치가 뭔지 | ✅ 아래 §11에 규칙 전체 명시 | CS 답변에 사용 |
| **B6** | `?auto_paused=true` 무시됨, 개수 없음 | ✅ 필터 동작 + `summary.counts.auto_paused` 추가 | **개수 표기 가능**, 목록 조회 1회 절약 |

그리고 지적해 주신 문서 오류(§9 표의 `…002` 행)도 고쳤습니다. 제한 검사가 중복 검사보다 앞이 맞습니다.

**활성화 게이트가 새로 생겼습니다 — 아래 §4-1을 꼭 보세요.**

---

## 0. 3줄 요약

1. **캠페인 만들기 전** `GET /auto-dm-campaigns/inspect-media/` 로 그 게시물이 쓸 수 있는지 확인하세요.
2. 막힌 게시물을 **생성하거나 활성화**하면 **HTTP 409 `media_content_restricted`** 가 옵니다. **복사는 막지 않습니다.**
3. 서버가 이상징후를 감지하면 캠페인을 **자동 정지**합니다. 목록/상세 응답의 **`auto_paused_at`** 이 채워져 있으면 "인스타그램 제한으로 멈춤" 배지를 띄우세요.

---

## 1. 왜 필요한가 (배경)

2026년 9월 4일부터 인스타그램이 **게시물 하나 단위로 '연령 제한 콘텐츠'** 분류를 시작했습니다. 걸린 게시물에서는 세 가지가 동시에 일어납니다.

```
① 로그아웃 사용자·10대에게 게시물이 안 보임
② 그 게시물의 댓글 알림(웹훅)이 우리 서버로 안 옴
③ 그 게시물 댓글에 자동 DM 발송이 거부됨   ← 사용자가 체감하는 증상
```

**계정 문제가 아닙니다.** 같은 계정의 다른 게시물은 같은 시각에 정상 발송됩니다. 실제로 한 고객은 하루 차이로 올린 릴스 중 하나만 막혔습니다.

**재연결·권한 재승인으로 풀리지 않습니다.** 실제 CS에서 고객이 재연결과 권한 전체 재승인을 했지만 그대로였습니다. 이 안내를 화면에 넣지 않으면 고객이 계속 헛수고합니다.

실측 피해: 한 고객은 **257명**, 다른 고객은 **96명**이 실패로 쌓이는 동안 아무 알림도 못 받았습니다. 한 고객은 이미 막힌 게시물에 캠페인을 **복사**해서 42건을 더 태웠습니다.

---

## 2. API 1 — 캠페인 만들기 전 점검

```
GET /api/v1/integrations/auto-dm-campaigns/inspect-media/
```

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `workspace_id` | ✅ | 워크스페이스 UUID |
| `media_id` | ✅ | 점검할 게시물 ID |
| `permalink` | ❌ | 게시물 permalink. 있으면 정확도가 올라갑니다(서버 설정이 켜져 있을 때만 사용) |

**항상 200입니다** (report-only). 제한이어도 200이고 `restriction.state` 로 판단하세요.

### 응답

```json
{
  "success": true,
  "data": {
    "media_id": "18302724844305013",
    "permalink": "https://www.instagram.com/reel/Dc8dtWQpB10/",
    "can_create_campaign": false,
    "restriction": {
      "state": "restricted",
      "source": "history",
      "media_id": "18302724844305013",
      "user_reason": "post_restricted",
      "blocking": true,
      "user_message": "이 게시물은 인스타그램이 '연령 제한 콘텐츠'로 분류해 댓글 자동 DM을 보낼 수 없습니다. 계정 문제가 아니라 이 게시물 하나의 문제이며, 재연결로는 해결되지 않습니다.",
      "how_to_check": "시크릿 창(로그아웃 상태)에서 게시물 링크를 열어보세요. '연령 제한 콘텐츠'라고 뜨면 인스타그램이 제한한 것이 맞습니다. 휴대폰 인스타 앱은 이미 로그인돼 있어 확인되지 않습니다.",
      "next_steps": [
        "다른 게시물로 캠페인을 만들어 주세요. 가장 확실하고 빠른 방법입니다.",
        "인스타그램 앱 → 설정 → 계정 상태에서 이의(검토 요청)를 넣을 수 있습니다. 통과 보장은 없습니다.",
        "재연결·권한 재승인은 효과가 없습니다. 토큰 문제가 아닙니다.",
        "같은 게시물에 캠페인을 다시 만들거나 복사하면 실패만 쌓입니다."
      ],
      "evidence": {
        "history": { "failures": 96, "successes_after_first_failure": 0, "first_failure_at": "2026-09-06T12:27:00+00:00" },
        "runtime": { "failures_2534066": 92, "successes": 0, "webhook_comments_total": 19, "last_webhook_at": "2026-09-06T12:10:34+00:00", "poll_only_comments_after_last_webhook": 122, "window_hours": 24 },
        "live": {},
        "live_check_enabled": false
      }
    }
  }
}
```

### `state` 4종 — 이게 전부입니다

| `state` | 뜻 | `blocking` | 화면 처리 |
|---|---|---|---|
| **`ok`** | 이상 없음 | `false` | 아무 것도 표시하지 않음 |
| **`restricted`** | **제한 확정.** 생성 시 409 | `true` | 🔴 생성 버튼 비활성 + `user_message` 노출 + "다른 게시물 선택" 유도 |
| **`suspected`** | 제한 의심 | `true` | 🟡 **생성은 허용.** 경고 배너만 노출 |
| **`unknown`** | 판정 불가 | `false` | **아무 것도 표시하지 않음** |

> ⚠️ **`unknown` 을 오류로 취급하지 마세요.** 새 게시물은 대부분 이력이 없어 `unknown` 입니다. 정상 흐름입니다.

> ⚠️ **`suspected` 는 생성을 막지 않습니다.** 서버도 막지 않습니다(오탐으로 정상 캠페인을 막는 쪽이 손해가 큽니다). 경고만 띄우고 사용자가 진행하게 두세요. `blocking: true` 지만 생성 API는 통과합니다 — **생성 차단 여부는 `state === "restricted"` 로 판단하세요.**

### `source` — 무엇으로 판정했나 (디버깅·어드민용)

| 값 | 뜻 |
|---|---|
| `history` | 그 게시물의 과거 실패 이력 |
| `runtime` | 최근 24시간 웹훅 침묵 + 연속 실패 |
| `live` | 인스타 공개 페이지 실시간 조회 (서버 설정 ON 일 때만) |
| `none` | 판정 근거 없음 |

일반 사용자 화면에는 노출하지 마세요.

### 호출 예시

```javascript
async function inspectMedia({ workspaceId, mediaId, permalink }) {
  const qs = new URLSearchParams({ workspace_id: workspaceId, media_id: mediaId });
  if (permalink) qs.set("permalink", permalink);

  const res = await fetch(
    `/api/v1/integrations/auto-dm-campaigns/inspect-media/?${qs}`,
    { headers: { Authorization: `Bearer ${accessToken}` } }
  );
  const { data } = await res.json();
  return data.restriction;
}

// 게시물 선택 직후 호출
const r = await inspectMedia({ workspaceId, mediaId, permalink });
if (r.state === "restricted") {
  setBlocked(true);
  setNotice({ level: "error", message: r.user_message, steps: r.next_steps, howToCheck: r.how_to_check });
} else if (r.state === "suspected") {
  setBlocked(false);
  setNotice({ level: "warning", message: r.user_message, steps: r.next_steps });
} else {
  setBlocked(false);
  setNotice(null);   // ok · unknown 둘 다 아무 것도 안 띄움
}
```

**호출 시점 권장** — 게시물을 고른 직후 1회. 입력할 때마다 부르지 마세요. 같은 `media_id` 는 세션 내 캐시해도 됩니다(단, 화면을 다시 열면 재조회 — 게시 18분 뒤에 제한으로 바뀐 사례가 있습니다).

| 상태 | 응답 |
|---|---|
| 200 | 점검 결과 (제한이어도 200) |
| 400 | `workspace_id` 또는 `media_id` 누락 |
| 401 | 인증 필요 |
| 403 | 워크스페이스 멤버 아님 |
| 404 | 워크스페이스 없음 |

---

## 3. API 2 — 이미 만든 캠페인 점검

```
GET /api/v1/integrations/auto-dm-campaigns/{id}/inspect/
```

캠페인 상세 화면의 **"점검하기"** 버튼, 그리고 실패가 쌓였을 때 원인 배너에 쓰세요.
**report-only 입니다 — 이 API는 캠페인 상태를 바꾸지 않습니다.**

```json
{
  "success": true,
  "data": {
    "campaign": {
      "id": "…",
      "name": "라라랜드 가을신상 라방 알림 및 혜택",
      "status": "paused",
      "media_id": "18302724844305013",
      "permalink": "https://www.instagram.com/reel/Dc8dtWQpB10/",
      "auto_paused": true,
      "auto_paused_at": "2026-09-07T09:00:00+00:00",
      "auto_paused_reason": "post_restricted"
    },
    "stats": {
      "opening_success": 20,
      "opening_failed_2534066": 96,
      "opening_success_unique_users": 20,
      "opening_failed_unique_users": 74
    },
    "restriction": { "state": "restricted", "...": "위와 동일 구조" }
  }
}
```

`restriction` 블록 구조는 API 1과 **완전히 같습니다.** 컴포넌트를 재사용하세요.

---

## 4. API 3 — 생성·복사 시 409

점검 API를 건너뛰거나, 점검 후 시간이 지나 상태가 바뀐 경우 서버가 최종적으로 막습니다.

| 엔드포인트 | 게이트 | 비고 |
|---|---|---|
| `POST /auto-dm-campaigns/?workspace_id=…` | ✅ | 생성 캠페인은 항상 ACTIVE 로 시작 |
| `POST /auto-dm-campaigns/{id}/resume/` | ✅ | **신규(09-08)** |
| `PATCH /auto-dm-campaigns/{id}/` `status=active` | ✅ | **신규(09-08)** |
| `POST /auto-dm-campaigns/{id}/schedule/` `activate=true` | ✅ | **신규(09-08)** |
| `POST /auto-dm-campaigns/bulk/` `op=resume` | ✅ | **신규(09-08)** — 건별 `failed[].reason` |
| `POST /auto-dm-campaigns/{id}/copy/` | ❌ **막지 않음** | 복사본은 INACTIVE — 발송 0건 |

### 409 응답

```json
{
  "success": false,
  "error": {
    "code": 409,
    "message": "이 게시물은 인스타그램이 '연령 제한 콘텐츠'로 분류해 댓글 자동 DM을 보낼 수 없습니다. …",
    "details": {
      "message": "…",
      "code": "media_content_restricted",
      "media_id": "18302724844305013",
      "permalink": "https://www.instagram.com/reel/Dc8dtWQpB10/",
      "restriction": { "state": "restricted", "...": "API 1 과 동일 구조" }
    }
  }
}
```

### ⚠️ 같은 409를 쓰는 오류가 **둘** 있습니다 — 반드시 `details.code` 로 분기하세요

| `details.code` | 뜻 | 처리 |
|---|---|---|
| `duplicate_active_campaign` | 같은 게시물에 이미 활성 캠페인이 있음 (기존 규칙) | 기존 캠페인으로 이동 / 일시정지 CTA |
| **`media_content_restricted`** | **게시물이 인스타에 의해 제한됨 (신규)** | 다른 게시물 선택 유도 |

```javascript
if (res.status === 409) {
  const code = body.error?.details?.code;
  if (code === "media_content_restricted") {
    showRestrictionModal(body.error.details.restriction);
  } else if (code === "duplicate_active_campaign") {
    showDuplicateModal(body.error.details);
  }
}
```

### 4-1. 복사는 허용, **활성화**에서 막습니다 (2026-09-08 변경)

복사본은 항상 `INACTIVE` 로 생기므로 그 자체로는 한 건도 발송되지 않습니다. 복사해서 게시물을
바꿔 쓰는 정상 흐름을 막지 않기 위해 **복사 게이트를 뺐습니다.**

대신 실제로 발송이 시작되는 **활성화 지점**을 막습니다.

```
복사        → 201 (INACTIVE)          막지 않음
게시물 교체  → 200                     막지 않음
활성화      → 409 (제한 게시물 그대로면)  ← 여기서 막힘
```

**일괄 재개(`bulk` `op=resume`)** 는 409 를 내지 않고 건별로 실패를 담습니다.

```json
{ "failed": [{ "id": "…", "reason": "media_content_restricted" }] }
```

> **증거 유효기간** — 마지막 실패가 **14일** 넘게 지났으면 `restricted` 로 단정하지 않고
> `unknown` 을 돌려줍니다. 인스타가 제한을 풀어줬을 수 있는데 재개를 영구히 막으면 빠져나올
> 길이 없기 때문입니다. 실제로 아직 막혀 있으면 재개 직후 실패가 다시 쌓여 자동 정지됩니다.

### 게이트 순서 — 제한 검사가 **먼저**입니다

한 게시물이 "제한됨 + 이미 활성 캠페인 있음" 둘 다인 경우 **`media_content_restricted` 가 먼저** 나옵니다.

게시물이 죽었는데 "이미 활성 캠페인이 있습니다"만 알려주면, 사용자가 기존 캠페인을 정지하고 다시 시도했다가 그제서야 제한을 만납니다(왕복 2회). 더 근본적인 사유를 먼저 줍니다.

---

## 5. 자동 정지 — 목록/상세에 배지 띄우기

서버 배치(1시간 주기)가 제한된 게시물의 **활성 캠페인을 자동으로 일시정지**합니다.

캠페인 응답(`AutoDMCampaignSerializer` — 목록·상세·생성·수정 전부)에 필드 2개가 추가됐습니다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `auto_paused_at` | `datetime \| null` | 시스템이 자동 정지시킨 시각. **`null` 이면 사용자가 직접 멈춘 것** |
| `auto_paused_reason` | `string` | 머신 키. 현재는 `"post_restricted"` 하나. 정상이면 `""` |

### 사용자가 직접 누른 일시정지와 구분하세요

```javascript
function pauseBadge(campaign) {
  if (campaign.status !== "paused") return null;
  if (campaign.auto_paused_at) {
    return { tone: "danger", label: "인스타그램 제한으로 자동 정지됨", showInspectButton: true };
  }
  return { tone: "neutral", label: "일시정지" };
}
```

`status: "paused"` 만 보고 "사용자가 껐구나"로 처리하면 **왜 멈췄는지 영영 안 보입니다.** 실제 CS의 절반이 여기서 발생했습니다.

### 자동 정지된 것만 조회 · 개수 세기 (2026-09-08 추가, B6)

```
GET /auto-dm-campaigns/?ig_connection_id=...&auto_paused=true    # 자동 정지된 것만
GET /auto-dm-campaigns/?ig_connection_id=...&auto_paused=false   # 사용자가 직접 멈춘 것만
GET /auto-dm-campaigns/summary/?workspace_id=...                 # counts.auto_paused
```

`summary` 응답의 `counts` 에 `auto_paused` 가 들어갑니다. **`paused` 의 부분집합**이라
`total` 에는 더해지지 않습니다.

```json
{ "counts": { "active": 3, "paused": 5, "completed": 2, "inactive": 0,
              "total": 10, "auto_paused": 3 } }
```

이제 `?status=paused&page_size=100` 을 받아 세실 필요 없이 **"3개가 멈춰 있습니다"** 라고
바로 쓰실 수 있습니다.

### 재개하면 표식이 지워집니다

사용자가 재개하면(`POST .../resume/`, 일괄 재개, `POST .../schedule/` 의 `activate=true`) 서버가 `auto_paused_at` 을 `null` 로 만듭니다. 프론트는 별도 처리가 필요 없습니다.

> ⚠️ **2026-09-08 변경** — 게시물이 여전히 제한 상태면 재개 자체가 **409 로 거부**됩니다(§4-1).
> 재개 버튼은 그대로 두시고, 409 가 오면 제한 모달을 띄우시면 됩니다.
> 마지막 실패가 14일 넘게 지났으면 재개가 허용되며, 아직 막혀 있다면 곧 다시 자동 정지됩니다.

---

## 6. 화면 문구 (그대로 쓰셔도 됩니다)

서버가 `user_message` / `how_to_check` / `next_steps` 를 **완성된 한국어 문장**으로 내려줍니다. 자체 i18n을 쓰신다면 `user_reason` (`"post_restricted"`) 를 키로 쓰고 서버 문구를 폴백으로 두세요.

### 제한 확정 모달 예시

```
🔴 이 게시물로는 자동 DM을 보낼 수 없습니다

{user_message}

■ 확인 방법
{how_to_check}

■ 어떻게 하면 되나요
{next_steps 를 리스트로}

[다른 게시물 선택]  [게시물 열어보기]  [닫기]
```

"게시물 열어보기" 버튼은 `permalink` 를 **새 탭**으로 엽니다. 시크릿 창 안내를 함께 보여주세요.

### 문구에 반드시 포함해야 하는 것

- ✅ **"계정 문제가 아니라 이 게시물 하나의 문제"** — 고객이 계정이 죽은 줄 알고 패닉합니다
- ✅ **"재연결·권한 재승인은 효과 없음"** — 실제로 헛수고한 CS가 있습니다
- ✅ **"다른 게시물로 만들면 됩니다"** — 가장 빠른 해결책
- ❌ "잠시 후 다시 시도해 주세요" 같은 문구 금지 — 재시도해도 영원히 실패합니다

---

## 7. 화면별 적용 위치 (권장)

| 화면 | 무엇을 |
|---|---|
| **캠페인 만들기 — 게시물 선택 직후** | API 1 호출 → `restricted` 면 다음 단계 버튼 잠금 + 모달 |
| **캠페인 만들기 — 최종 제출** | 409 `media_content_restricted` 처리 (점검을 건너뛴 경로 대비) |
| **캠페인 목록** | `auto_paused_at` 있는 행에 🔴 배지 |
| **캠페인 상세 상단** | `auto_paused_at` 있으면 배너 + "점검하기" 버튼(API 2) |
| **캠페인 상세 — 발송 실패가 많을 때** | API 2 호출 → `restriction.state` 로 원인 배너 |
| **캠페인 복사 버튼** | 409 처리 (동일) |

---

## 8. 엣지 케이스

| 상황 | 동작 | 프론트 처리 |
|---|---|---|
| 점검 API가 실패(500/타임아웃) | 서버는 **fail-open** — 생성을 막지 않음 | 조용히 무시하고 진행. 오류 토스트 띄우지 마세요 |
| `state: "unknown"` | 판정 불가 | 아무 것도 표시하지 않음 |
| 점검은 `ok` 였는데 생성에서 409 | 그 사이 상태가 바뀜 | 409 모달로 처리 |
| `suspected` 인데 사용자가 생성 강행 | 서버가 허용 | 경고만 띄우고 통과 |
| `media_id` 없는 캠페인 (`any_media` / 미부착 `next_media`) | 게이트 통과(점검 대상 아님) | 점검 API 호출 자체를 생략 |
| 자동 정지된 캠페인을 사용자가 재개 | 표식 해제, 다음 배치에서 재정지 가능 | 재개 시 안내 문구 |

---

## 9. 🔑 dev 확인용 계정 — 모든 상태가 이미 만들어져 있습니다

**개발 서버에 시드 완료**했습니다. 로그인만 하면 6가지 상태를 화면에서 바로 볼 수 있습니다.
목킹이 아니라 실제 발송 이력을 심어둔 것이라, **진짜 판정 로직이 그대로 돕니다.**

```
서버      https://dev-api.turnflow.link
로그인    restriction@test.com / Test1234!

workspace_id      3dba2861-6000-4145-8acf-0eae4051c902
ig_connection_id  14004ce9-97f0-4fcf-9afb-81653914a41d   (@restriction_demo)
```

### 캠페인 5개 — 목록·상세 화면에서 바로 보입니다

| 캠페인 이름 | 기대 `state` | 확인할 것 |
|---|---|---|
| 정상 발송 캠페인 (배지 없음) | `ok` | 아무 배지도 안 뜸 |
| 제한 확정 캠페인 (활성) | `restricted` | 🔴 배너. **복사 누르면 409** |
| 자동 정지된 캠페인 (🔴 배지) | `restricted` | `status=paused` + **`auto_paused_at` 채워짐** → "인스타그램 제한으로 자동 정지됨" 배지 |
| 제한 의심 캠페인 (🟡 경고) | `suspected` | 🟡 경고 배너만. 차단 아님 |
| 실패 섞였지만 정상 (오탐 확인) | `ok` | 실패 로그가 6건 있지만 **배지 안 뜸** — 오탐 확인용 |

### media_id 4종 — 생성 전 점검(API 1) 테스트용

| `media_id` | 기대 `state` | 생성 시도하면 |
|---|---|---|
| `17900000000000001` | `ok` | 409 duplicate (이미 활성 캠페인 있음) |
| `17900000000000006` | `unknown` | **201 생성됨** ← 정상 경로 |
| `17900000000000007` | `restricted` | **409 `media_content_restricted`** ← 이걸로 모달 확인 |
| `17900000000000008` | `suspected` | **201 생성됨** (경고만 뜨고 통과) |

> ⚠️ **정정(09-08)** — `17900000000000002` 로 생성하면 **`media_content_restricted`** 가 나옵니다.
> 제한 검사가 중복 검사보다 **앞**이라 그렇습니다(§4 게이트 순서). 이전 문서에 `duplicate_active_campaign`
> 이라고 적혀 있던 것은 오류입니다 — 지적 감사합니다.
> `duplicate_active_campaign` 만 따로 보시려면 **제한이 없는** `…001` 에 활성 캠페인이 있는 상태로 생성해 보세요.

### 바로 붙여넣을 수 있는 호출

```bash
WS=3dba2861-6000-4145-8acf-0eae4051c902
CONN=14004ce9-97f0-4fcf-9afb-81653914a41d
TOKEN=<로그인해서 받은 access token>

# 1) 생성 전 점검 — state 4종
for M in 17900000000000001 17900000000000006 17900000000000007 17900000000000008; do
  curl -s -H "Authorization: Bearer $TOKEN" \
    "https://dev-api.turnflow.link/api/v1/integrations/auto-dm-campaigns/inspect-media/?workspace_id=$WS&media_id=$M" \
    | python -m json.tool
done

# 2) 제한 게시물로 생성 → 409 media_content_restricted
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  "https://dev-api.turnflow.link/api/v1/integrations/auto-dm-campaigns/?workspace_id=$WS" \
  -d "{\"ig_connection_id\":\"$CONN\",\"name\":\"테스트\",\"media_id\":\"17900000000000007\",\"message_template\":\"안녕\"}"

# 3) 목록에서 auto_paused_at 확인
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://dev-api.turnflow.link/api/v1/integrations/auto-dm-campaigns/?workspace_id=$WS"
```

### 상태를 초기화하고 싶다면

서버에서 시드를 다시 돌리면 같은 상태로 되돌아갑니다(멱등 — 기존 캠페인·로그를 지우고 재생성).

```bash
docker compose exec -T web python manage.py shell < scripts/seed_dev_restriction_cases.py
```

**주의** — 화면에서 캠페인을 재개하면 `auto_paused_at` 이 지워져서 🔴 배지가 사라집니다(의도된 동작). 배지를 다시 보려면 시드를 재실행하세요.

---

## 11. 판정 임계치 — CS 답변용 (B4 답변)

`apps/integrations/ig_content_restriction.py` 의 실제 규칙입니다. **화면에 숫자를 쓰지는
마세요**(인스타 정책이라 언제든 바뀝니다). CS가 "몇 번 더 실패하면 막히나요" 를 받았을 때
근거로만 쓰세요.

### 축은 "총 실패 수"가 아니라 **"마지막 성공 이후 실패 수"** 입니다

관찰하신 게 정확합니다. 성공이 섞인 게시물은 실패 16건이어도 `ok` 입니다.

| 판정 | 조건 | 화면 |
|---|---|---|
| `restricted` | 총 실패 **5건 이상** **그리고** 마지막 성공 이후 실패 **5건 이상** | 🔴 차단 |
| `suspected` | 최근 24시간 실패 **3건 이상** **그리고** 그 24시간 성공 **0건** | 🟡 경고만 |
| `ok` | 위 어디에도 안 걸림 (최근에 성공한 이력이 있음) | 표시 없음 |
| `unknown` | 판정할 이력이 없음, **또는 마지막 실패가 14일 초과** | 표시 없음 |

**왜 이렇게 나눴나** — 소급발송(백필)은 실패와 성공을 뒤섞어 남깁니다. "첫 실패 이후 성공이
몇 건인가" 로 보면 실서버에서 확실히 죽은 게시물 2개가 정상으로 빠져나갔습니다. 기준을
"마지막 성공 이후" 로 바꾸니 실측 탐지율이 올랐고 오탐은 0을 유지했습니다.

`suspected` 임계(3)가 `restricted` 임계(5)보다 **낮은 것은 의도**입니다. 같으면 확정이 항상
먼저 떠서 조기경보가 뜰 틈이 없습니다.

### CS 답변 예시

> "이 게시물은 마지막으로 정상 발송된 뒤 실패가 5회 이상 연속돼서 자동으로 막아둔 상태입니다.
> 인스타그램이 게시물을 제한하면 저희가 댓글 정보를 아예 못 받아서, 몇 번을 더 시도해도
> 결과가 같습니다. 새 게시물로 캠페인을 만드시는 게 가장 빠릅니다."

**근거 수치는 응답에 그대로 옵니다** — `restriction.evidence.history` 의
`failures` / `failures_after_last_success` / `last_success_at` / `last_failure_at` / `evidence_stale`.

---

## 10. 백엔드 구현 참고 (프론트는 몰라도 됨)

- 판정 단일 소스: `apps/integrations/ig_content_restriction.py`
- 게이트: `AutoDMCampaignViewSet._assert_media_not_restricted` (`views.py`)
- 예외: `apps.core.exceptions.RestrictedMediaError` (409, `media_content_restricted`)
- 자동 정지 배치: `integrations.sweep_restricted_campaigns` (1시간 주기, 외부 호출 0)
- 마이그레이션: `integrations 0054` (`auto_paused_at`, `auto_paused_reason`)
- 테스트: `apps/integrations/test_content_restriction.py` (27건)

**판정 신호 3종**

| 신호 | 언제 잡히나 | 외부 호출 |
|---|---|---|
| `history` | 그 게시물에 과거 실패 이력이 있을 때 | 0 |
| `runtime` | 웹훅이 끊기고 24시간 내 연속 실패할 때 | 0 |
| `live` | 즉시 (새 게시물도) — **서버 설정 `IG_RESTRICTION_LIVE_CHECK_ENABLED` 기본 OFF** | HTTP 1회 |

> `live` 는 인스타 공개 페이지를 크롤러 UA로 조회해야 해서(약관 회색지대·IP 차단 위험) **기본 비활성**입니다. 꺼져 있으면 새 게시물은 `unknown` 이 나옵니다 — 정상입니다. 자동 정지 배치는 설정과 무관하게 **절대 외부 조회를 하지 않습니다.**
