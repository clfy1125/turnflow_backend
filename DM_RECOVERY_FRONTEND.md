# 실패 DM 복구(Recovery) — 프론트 연동 가이드 (v2)

> 자동 DM 캠페인에서 **비팔로워 대상 opening DM 이 확정 실패**했을 때, 완전 실패로 버리지 않고
> 게시물에 **"DM이 숨겨진 요청/스팸함으로 갔어요 — 수락 후 다시 댓글 달아주세요"** 안내 대댓글을
> 자동으로 달고, 사용자가 **다시 댓글을 달면** 일반 발송 경로로 재발송하는 기능(성공 시 이전 실패
> 건은 자동으로 성공 처리). **프로 전용**, 캠페인 단위로 켜고 끈다.

> **⚠️ v2 변경(2026-07-14)**: v1 의 "아무 DM 이나 보내주세요"(인바운드 DM 감지) 방식은
> **폐기**됐다 — 'DM 먼저 받기'를 꺼둔 사용자에게 동작하지 않았다. 재발송 트리거가
> **사용자의 재댓글**로 바뀌었고, 안내 문구·상태 라벨 문구도 그에 맞게 바뀌었다.
> **API 필드/상태값 이름은 그대로**라 프론트 코드 변경은 필수 아님(라벨 문구 갱신만 권장).
> `recovery_keyword` 는 deprecated(값 무시, 필드는 하위호환 유지).

---

## 0. 왜 필요한가 (1줄 배경)

댓글만 단 비팔로워에게는 비공개답글(opening DM)이 인스타 정책상 실패한다(`code=100 / subcode=2534025`).
이때 DM 은 대부분 상대의 **숨겨진 요청/스팸함**에 있거나 요청 수락 전 상태다. 이 실패건을
**대댓글 안내(수락+재댓글 유도) → 사용자가 재댓글 → 일반 경로 재발송 → 성공 시 자동 성공 처리**로 살린다.
프론트가 관여할 부분은 **① 캠페인 폼의 복구 설정 UI**와 **② 로그 화면의 상태 3종** 두 가지다.

---

## 1. 캠페인 생성/수정 필드

`POST /api/v1/integrations/auto-dm-campaigns/?workspace_id=...`
`PATCH /api/v1/integrations/auto-dm-campaigns/{id}/` 의 복구 필드. **모두 선택(생략 가능).**

| 필드 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `recovery_reply_enabled` | boolean | **`true`** | 복구 기능 on/off. **프로 전용** — 미보유 플랜은 켜도 동작 안 함(§3). |
| `recovery_reply_templates` | string[] | `[]` | 안내 대댓글 문구 목록(무작위 1개 사용). **비우면 서버가 매번 무한 변형 자동 생성**(§2). |
| `recovery_keyword` | string | `""` | **(deprecated v2)** 값은 무시된다. 폼에서 입력란을 제거해도 됨(보내도 무해). |
| `recovery_ttl_seconds` | int | `604800`(7일) | 안내 대댓글 게시 후 이 시간까지 재댓글 발송 성공이 없으면 만료. 범위 3600~2592000(1시간~30일). |

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
    "DM이 숨겨진 요청함으로 들어갔어요 🥲 메시지 요청 수락하시고 다시 댓글 남겨주시면 바로 보내드릴게요!",
    "메시지가 요청함으로 분류된 것 같아요 😢 요청함에서 수락 후 댓글 한 번만 더 달아주시면 다시 보내드려요! 😊",
    "DM이 스팸함으로 들어간 것 같아요 🙈 수락하신 다음 재댓글 남겨주시면 곧바로 다시 보내드릴게요!"
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
- **v1 시절 문구("DM 아무거나 보내주세요" 류)가 저장돼 있던 캠페인은 서버 마이그레이션이
  자동 정리**했다(죽은 행동 지시 방지). 사용자 커스텀 문구(재댓글 유도/일반 문구)는 보존됨.

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

## 4. 로그 화면 — 상태 3종

발송 로그(SentDMLog)의 `status` 복구 상태 3개. **상태값 문자열은 v1 과 동일** — 라벨 문구만 갱신 권장.

| status | 의미 (v2) | 성격 | 권장 배지 |
|---|---|---|---|
| `recovery_pending` | 안내 대댓글 게시함, **요청함 수락·재댓글 대기 중** | 진행 중(성공/실패 미정) | ⏳ 정보(회색/파랑) |
| `recovery_delivered` | 사용자가 재댓글을 달아 **재발송 성공** | 종결·성공 (도착으로 집계됨) | ✅ 성공(초록) |
| `recovery_expired` | TTL 내 재댓글 발송 성공 없음 → 만료 | 종결·실패 | ⚠️ 실패(주황) |

- `recovery_pending` 은 **아직 실패가 아님** — "완전 실패" 카운트/발송률에 넣지 말 것(서버도 그렇게 집계).
- `recovery_delivered` 는 도착(delivered)으로 집계되어 발송 성공률에 포함된다.
- **v2 정산 방식**: 사용자가 재댓글을 달면 **새 로그**(일반 발송)가 생기고, 그 발송이 접수(accepted)되는
  순간 같은 사용자의 이전 `recovery_pending` 로그가 `recovery_delivered` 로 자동 승격된다.
  즉 로그 목록에서 같은 사용자에게 (구)복구성공 + (신)발송성공 두 줄이 보이는 게 정상.
- 상태 표시 문자열/프론트 액션은 서버가 `_STATUS_DISPLAY` / frontend-action 으로도 내려주므로 그대로 써도 됨.

---

## 5. 전체 흐름 (참고)

1. 비팔로워가 댓글 → opening DM 시도 → `2534025` **확정** 실패
   (전달 흔적이 있거나 이미 답글이 달린 댓글에는 복구 안내를 달지 않는다 — 이중 댓글 방지).
2. (프로 + enabled 면) 상태 `recovery_pending` + 게시물에 "숨김함 수락 후 재댓글" 안내 대댓글 자동 게시.
   같은 사용자에게 안내는 **1회만** — 수락 전에 재댓글이 또 실패해도 안내를 반복 게시하지 않는다.
3. 사용자가 요청함에서 메시지 요청 **수락** 후 **다시 댓글** → 복구 재발송.
   - **스레드 답글도 인정** — 안내가 사용자 댓글의 답글로 달리므로, 그 스레드에 답글을 달아도
     복구 전용 라우팅이 잡아서 재발송한다(새 top-level 댓글도 물론 동작).
   - **캠페인 키워드와 무관** — "수락했어요" 같은 재댓글도 트리거된다(복구 대기 중인 사용자
     본인에게만 적용되므로 오남용 불가).
   - 복구 대기 건은 5분 수신자 쿨다운에서 면제되므로 안내를 보고 바로 재댓글해도 발송된다.
4. 재발송이 접수되면 이전 건이 `recovery_delivered` 로 승격. TTL(기본 7일) 내 성공이 없으면 `recovery_expired`.

프론트는 **①폼 설정 + ②추천 문구 프리필 + ③상태 배지**만 처리하면 되고, 2~4 는 전부 서버/웹훅이 자동 처리한다.

---

## 6. 요약 체크리스트

- [ ] 캠페인 생성 폼: `recovery-reply-suggestions` 호출 → `recovery_reply_templates` 프리필.
- [ ] `available`/`recovery_reply_available`/`features.dm_recovery` 로 프로 여부 판단 → 토글 잠금·업그레이드 CTA.
- [ ] 생성/수정 payload 에 `recovery_reply_enabled`, `recovery_reply_templates`, `recovery_ttl_seconds` 포함(모두 선택).
- [ ] `recovery_keyword` 입력란 제거 가능(deprecated — 보내도 무해).
- [ ] 로그 화면 상태 매핑에 `recovery_pending` / `recovery_delivered` / `recovery_expired` (라벨 문구 v2 로 갱신).
- [ ] `recovery_pending` 을 실패로 표기하지 말 것.

상세 스키마/예시는 Swagger(`/api/docs/`)의 **Auto DM** 태그 참고 (MCP `api-mcp` 로도 검색 가능).
