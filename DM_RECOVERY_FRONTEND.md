# 실패 DM 복구(Recovery) — 프론트 연동 가이드

> 자동 DM 캠페인에서 **비팔로워 대상 opening DM 이 실패**했을 때, 완전 실패로 버리지 않고
> 게시물에 "다시 보내드릴게요" **안내 대댓글**을 자동으로 달고, 사용자가 그 계정으로 **아무 DM 이나
> 보내오면 열린 채널로 opening DM 을 재전송**하는 기능. **프로 전용**, 캠페인 단위로 켜고 끈다.

---

## 0. 왜 필요한가 (1줄 배경)

댓글만 단 비팔로워에게는 비공개답글(opening DM)이 인스타 정책상 실패한다(`code=100 / subcode=2534025`,
"숨겨진 요청"으로도 안 감). 이 실패건을 **대댓글 안내 → 사용자 인바운드 DM → 재전송**의 2-hop 으로 살린다.
프론트가 관여할 부분은 **① 캠페인 폼의 복구 설정 UI**와 **② 로그 화면의 새 상태 3종** 두 가지다.

---

## 1. 캠페인 생성/수정에 추가된 필드

`POST /api/v1/integrations/auto-dm-campaigns/?workspace_id=...`
`PATCH /api/v1/integrations/auto-dm-campaigns/{id}/` 에 아래 필드가 추가됨. **모두 선택(생략 가능).**

| 필드 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `recovery_reply_enabled` | boolean | **`true`** | 복구 기능 on/off. **프로 전용** — 미보유 플랜은 켜도 동작 안 함(§3). |
| `recovery_reply_templates` | string[] | `[]` | 안내 대댓글 문구 목록(무작위 1개 사용). **비우면 서버가 매번 무한 변형 자동 생성**(§2). |
| `recovery_keyword` | string | `""` | 비우면 사용자의 **아무 DM** 이나 재전송 트리거. 값이 있으면 그 키워드 포함 DM 만. |
| `recovery_ttl_seconds` | int | `604800`(7일) | 안내 대댓글 게시 후 이 시간까지 사용자 DM 이 없으면 만료. 범위 3600~2592000(1시간~30일). |

읽기 응답(GET/list/detail)에는 아래 **읽기 전용** 필드가 추가로 내려온다:

| 필드 | 타입 | 설명 |
|---|---|---|
| `recovery_reply_available` | boolean | 이 캠페인 소유자 플랜이 복구를 **실제로 쓸 수 있는지**(프로 여부). 편집 화면 토글 잠금 판단에 사용. |

> **주의**: `recovery_reply_enabled=true` 이고 `recovery_reply_available=false` 이면 → 설정은 저장되지만
> **실제로는 아무 대댓글도 안 달린다**. 업그레이드 유도 UI 를 보여줄 것.

---

## 2. 폼 기본 문구 30개 받아오기 (추천 엔드포인트)

사용자가 문구를 직접 안 적어도 되도록, **폼을 열 때** 서버가 조합형으로 생성한 추천 문구를 받아
`recovery_reply_templates` 입력란을 미리 채운다. 사용자는 자유롭게 편집/추가/삭제.

```
GET /api/v1/integrations/auto-dm-campaigns/recovery-reply-suggestions/
      ?workspace_id={uuid}&count=30
Authorization: Bearer <access>
```

**쿼리 파라미터**
- `count` (선택, 기본 30, 1~100): 받을 문구 개수.
- `workspace_id` (선택): 넘기면 `available`(프로 여부)까지 계산해서 내려준다. 생략 시 `available: null`.

**응답 200**
```json
{
  "templates": [
    "DM 전송에 실패했어요 😢 이 계정으로 DM 아무거나 하나 보내주시면 다시 보내드릴게요!",
    "앗 전송 오류가 났어요 🥲 아무 내용이나 DM 주시면 즉시 다시 보내드릴게요! 🎁",
    "메시지가 전달되지 않았어요 😥 저희 계정으로 DM 하나만 보내주시면 다시 보내드릴게요!"
  ],
  "count": 30,
  "generator_combinations": 106704,
  "available": true,
  "plan_required": "pro"
}
```

```js
// 캠페인 생성 폼 마운트 시
const res = await fetch(
  `/api/v1/integrations/auto-dm-campaigns/recovery-reply-suggestions/?workspace_id=${wsId}&count=30`,
  { headers: { Authorization: `Bearer ${access}` } }
);
const { templates, available } = await res.json();
form.recovery_reply_templates = templates;   // 입력란 프리필
form.recovery_reply_enabled = available;      // 프로 아니면 토글 잠금/off
```

**동작 포인트**
- 호출할 때마다 **매번 다른 무작위 조합**을 준다(같은 요청도 다름). 총 조합 수는 `generator_combinations`.
- 사용자가 이 목록을 **비워서 저장**하면, 발송 시점에 서버가 이 생성기로 **매번 새 문구**를 만들어 쓴다
  (사실상 무한 변형 → 봇 검사에 가장 강함). 채워서 저장하면 **그 목록에서만** 무작위 선택.
  → "직접 관리 vs 서버 자동" 둘 다 가능. 기본은 30개 프리필을 권장.

**에러**: `400`(count 형식) · `401`(토큰) · `403`(워크스페이스 비멤버) · `404`(workspace_id 없음).

---

## 3. 프로 전용 게이팅

복구는 프로 플랜 기능(`features.dm_recovery = true`)이다. 판단 소스는 **두 곳** 중 편한 쪽:

1. **플랜 API** `GET /api/v1/billing/plans/` 의 각 플랜 `features.dm_recovery` (free/basic=false, pro=true).
   현재 사용자 플랜은 구독 조회 API 로 확인.
2. **캠페인 응답의 `recovery_reply_available`** (편집 화면) 또는 **추천 엔드포인트의 `available`** (생성 화면).

UX 권장:
- 프로: 토글 기본 ON, 문구 30개 프리필.
- 비프로: 토글 **잠금(disabled)** + "프로 전용" 배지 + 업그레이드 CTA. 저장은 막지 않아도 되나(무해),
  혼동을 줄이려면 저장 시 off 로 보내거나 잠금 유지.

> **서버 보장(fail-closed)**: 비프로가 어떻게든 `recovery_reply_enabled=true` 로 저장해도, 발송 실패 시
> **복구는 절대 트리거되지 않고** 해당 DM 은 기존과 동일하게 일반 실패(`failed_param`)로 종결된다. 회귀 없음.

---

## 4. 로그 화면 — 새 상태 3종

발송 로그(SentDMLog)의 `status` 에 복구 상태 3개가 추가됨. 상태 배지/문구 매핑에 반영할 것.

| status | 의미 | 성격 | 권장 배지 |
|---|---|---|---|
| `recovery_pending` | 안내 대댓글 게시함, 사용자 DM 대기 중 | 진행 중(성공/실패 미정) | ⏳ 정보(회색/파랑) |
| `recovery_delivered` | 사용자가 DM 보내와 재전송 성공 | 종결·성공 (도착으로 집계됨) | ✅ 성공(초록) |
| `recovery_expired` | TTL 내 사용자 무응답으로 만료 | 종결·실패 | ⚠️ 실패(주황) |

- `recovery_pending` 은 **아직 실패가 아님** — "완전 실패" 카운트/발송률에 넣지 말 것(서버도 그렇게 집계).
- `recovery_delivered` 는 도착(delivered)으로 집계되어 발송 성공률에 포함된다.
- 상태 표시 문자열/프론트 액션은 서버가 `_STATUS_DISPLAY` / frontend-action 으로도 내려주므로 그대로 써도 됨.

---

## 5. 전체 흐름 (참고)

1. 비팔로워가 댓글 → opening DM 시도 → `2534025` 실패.
2. (프로 + enabled 면) 상태 `recovery_pending` + 게시물에 안내 대댓글 자동 게시.
3. 사용자가 그 계정으로 **아무 DM** 전송 → 서버가 IGSID 로 매칭 → opening DM(팔로우게이트면 버튼 포함) 재전송.
4. 재전송 접수되면 `recovery_delivered`. 7일(기본) 내 무응답이면 `recovery_expired`.

프론트는 **①폼 설정 + ②추천 문구 프리필 + ③상태 배지**만 처리하면 되고, 2~4 는 전부 서버/웹훅이 자동 처리한다.

---

## 6. 요약 체크리스트

- [ ] 캠페인 생성 폼: `recovery-reply-suggestions` 호출 → `recovery_reply_templates` 프리필.
- [ ] `available`/`recovery_reply_available`/`features.dm_recovery` 로 프로 여부 판단 → 토글 잠금·업그레이드 CTA.
- [ ] 생성/수정 payload 에 `recovery_reply_enabled`, `recovery_reply_templates`, `recovery_keyword`, `recovery_ttl_seconds` 포함(모두 선택).
- [ ] 로그 화면 상태 매핑에 `recovery_pending` / `recovery_delivered` / `recovery_expired` 추가.
- [ ] `recovery_pending` 을 실패로 표기하지 말 것.

상세 스키마/예시는 Swagger(`/api/docs/`)의 **Auto DM** 태그 참고 (MCP `api-mcp` 로도 검색 가능).
