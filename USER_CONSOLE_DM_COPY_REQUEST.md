# 유저 콘솔 요청서 (백엔드 → 프론트엔드)

작성 2026-08-07 · **UC-1 ~ UC-6 (6건)** · 대상: 유저 콘솔 `자동 DM > 발송 로그`

> **이 문서 하나로 작업이 됩니다** — 문구 전문(UC-1)이 안에 들어 있어 다른 문서를 찾아보실
> 필요가 없습니다.

| 번호 | 요청 | 대상 |
|---|---|---|
| **UC-1** | `frontend_action.user_reason` 기준으로 문구 그리기 | `DMResultModal` · i18n |
| **UC-2** | **하드코딩 문구 우회 제거** (더 이상 필요 없음) | `DMResultModal.tsx:46~53` |
| **UC-3** | 자가 점검 체크리스트 3개로 감소 대응 | `DMResultModal` |
| **UC-4** | 기술 정보(code/subcode) 화면 비노출 | 로그 상세 |
| **UC-5** | **죽은 CTA 버튼 2건 수정** | `DMResultModal.handleCtaClick` |
| **UC-6** | 배지 색을 `user_reason` 기준으로 (다음 라운드 준비) | `dmStatusShared.tsx` |

**배포 상태**: 백엔드는 **하위호환**입니다. `title`/`description` 을 계속 채우므로
프론트를 안 고쳐도 화면은 깨지지 않고, **문구만 새 것으로 바뀝니다.** 급하지 않게 진행하셔도 됩니다.

---

## 개발 서버 확인용 계정 · 데이터 (준비 완료)

**dev 에 사유별 로그를 하나씩 넣어둔 캠페인을 만들어 뒀습니다.** 27개 행이 각각 다른
`user_reason` 이라, 행을 하나씩 눌러 문구를 대조하시면 됩니다.
**프로·무료 두 계정에 각각** 넣었습니다.

### 로그인 · 캠페인

```
POST /api/v1/auth/login/          ← 끝슬래시 필수
```

| 플랜 | 이메일 / 비밀번호 | 카탈로그 campaign_id |
|---|---|---|
| **프로** | `dmdummy-pro@turnflow.dev` / `Test1234!` | `211c4a9d-267a-4a9a-b8be-affd835fa81d` |
| **무료** | `dmdummy-free@turnflow.dev` / `Test1234!` | `bf8f4911-4afe-470b-8258-9dc452e285fe` |

캠페인 이름은 각각 `[DUMMY] 문구 카탈로그(프로) — 사유별 1건씩` / `…(무료)…` 입니다.

> ⚠️ **UUID 는 시드를 다시 돌리면 바뀝니다.** 안 맞으면 캠페인 목록에서
> **이름에 `문구 카탈로그`** 가 들어간 것을 찾으세요.

```
GET /api/v1/integrations/dm-verification/?campaign_id=<위 id>
    → {count, page, page_size, results}   ※ next 키가 없는 페이저입니다(27건 = 2페이지)
```

### 두 플랜에 같은 데이터를 둔 이유

**문구 응답 자체는 두 플랜이 완전히 같습니다** — 서버의 `build_frontend_action` 은 플랜을
모릅니다. 같은 데이터를 양쪽에 둔 것은 **플랜에 따라 갈리는 화면**을 나란히 비교하시라는
뜻입니다.

| 확인 지점 | 프로 | 무료 |
|---|---|---|
| `hidden_request` 의 `enable_recovery` CTA | 실제 기능(복구 설정) | 업그레이드 안내로 가야 함 |
| `monthly_dm_limit` 행 | 한도 여유 | 무료 한도 맥락과 함께 |
| 플랜 게이트 배너·업셀 | 없음 | 노출 |

### 행 이름만 보고 사유를 알 수 있게 해뒀습니다

`dmdummy_<번호>_<user_reason>` 형식입니다.

| 행 | 확인 포인트 |
|---|---|
| `dmdummy_01_connection_lost` | `type=reconnect` · CTA `ig_reconnect` |
| `dmdummy_02_recipient_unavailable` | 상대방 사정 (지금은 severity `error`) |
| `dmdummy_03_window_expired` | 7일 초과 |
| `dmdummy_04_hidden_request` | 복구 **미사용** → `next` = 복구 켜기 안내 · CTA `enable_recovery` (UC-5 죽은 버튼) |
| `dmdummy_05_hidden_request` | 복구 **대기** → `next_pending` |
| `dmdummy_06_hidden_request` | 복구 **만료** → `next_expired` |
| `dmdummy_07_post_restricted` ~ `11_send_incomplete` | 오류 나머지 5종 |
| `dmdummy_12` ~ `21` | 건너뜀 10종 (`monthly_dm_limit` … `other`) |
| `dmdummy_22` ~ `27` | 성공·진행 6종 (`user_reason` 이 **빈 문자열**) |

**`09_delivery_unconfirmed` 행에만 체크리스트 3개**가 붙어 있습니다(UC-3 확인용).

### 확인한 것

시드 후 실제 API 응답으로 **두 캠페인 각 27건 전수**를 대조해 `user_reason` 불일치 **0건**,
빈 문구 **0건**을 확인했습니다. 실제 DM 발송은 **0건**입니다(Meta 미호출).

> 데이터가 이상해지면 알려주세요 — `python manage.py seed_dm_dev_dummy` 로 재생성합니다.

---

## 0. 배경 — 왜 바꾸나

유저 콘솔에 나가던 문구가 **운영자용 사전에서 새어 나온 것**이었습니다. "Private Reply",
"파라미터 오류", "35분 동안 확인했지만" 같은 내부 용어가 그대로 노출됐고, 실제로 프론트가
런타임에 덮어쓰고 계셨습니다(`DMResultModal.tsx:46`).

그 우회를 없애는 게 이번 작업의 목표입니다. 서버가 **사유 머신 키**만 주고 **문구는 프론트가
소유**하는 구조로 갑니다 — 문구 수정에 백엔드 배포가 필요 없고, 영어 대응도 열립니다.

---

## UC-1. `user_reason` 기준으로 문구 그리기

`frontend_action` 에 **세 필드가 추가**됐습니다. 기존 필드는 그대로 있습니다.

```jsonc
"frontend_action": {
  "user_reason": "connection_lost",   // ★ 신규 — 사유 머신 키
  "cause":       "인스타그램 설정에서 연결이 해제되었거나…",     // ★ 신규 — 발생 이유
  "next_step":   "다시 연결하시면 이후 작성되는 댓글부터…",       // ★ 신규 — 다음 행동
  "title":       "인스타그램 계정 연결이 해제되어 발송되지 않았어요",
  "description": "…",        // 하위호환 — cause + " " + next_step
  "type":        "reconnect",
  "severity":    "error",
  "checklist":   null,
  "cta":         { "label": "인스타그램 다시 연결하기", "action": "ig_reconnect" }
}
```

### 문구 구조 — 세 부분

**현재 상태(title) → 발생 이유(cause) → 다음 행동(next_step)** 순서입니다.
**세 부분을 한 덩어리로 합치지 말아 주세요** — 이유와 다음 행동이 뭉개지면 "그래서 내가 뭘
해야 하나"가 안 보입니다. 지금 `description` 은 둘을 이어 붙인 하위호환 값입니다.

### 문구 전문 — 그대로 쓰시면 됩니다
(아래 문구는 유저 입장에서 생각하며, 의논하여 나온 말들임)
i18n 에 붙여넣고 `user_reason` 으로 조회하시면 됩니다. (오류 9종 + 건너뜀 10종 + 성공·진행 중)

```jsonc
{
  "dmAuto": {
    "reason": {
      "connection_lost": {
        "title": "인스타그램 계정 연결이 해제되어 발송되지 않았어요",
        "cause": "인스타그램 설정에서 연결이 해제되었거나, 계정 보안 정책에 따라 연결이 만료된 경우에 발생할 수 있어요. 연결이 해제된 동안에는 이 계정의 자동 DM 발송이 모두 중단돼요.",
        "next": "다시 연결하시면 이후 작성되는 댓글부터 발송이 재개돼요. 아직 발송 가능 기간인 댓글 작성 후 7일 이내의 건은 별도 설정 없이 자동으로 다시 발송됩니다."
      },
      "recipient_unavailable": {
        "title": "수신자가 메시지를 받을 수 없는 상태였어요",
        "cause": "수신자 계정이 삭제·비활성화되었거나, 비공개로 전환되었거나, 메시지 수신을 제한한 경우에 발생할 수 있어요. 대화방을 삭제한 경우에도 동일하게 처리돼요. 이 수신자 한 분에게만 해당되며, 다른 분들에게는 정상적으로 발송돼요."
      },
      "window_expired": {
        "title": "댓글을 삭제했거나 작성된 지 7일이 초과되었어요",
        "cause": "댓글이 삭제된 경우 자동 DM을 발송할 수 없어요. 또한 인스타그램은 댓글 작성 후 7일까지만 자동 DM 발송을 허용해서 7일이 지난 경우에도 동일하게 처리돼요."
      },
      "hidden_request": {
        "title": "수신자의 '숨겨진 요청 · 스팸함'으로 이동했을 수 있어요",
        "cause": "아직 팔로우하지 않은 분에게 보내는 첫 DM은 받은편지함 대신 '숨겨진 요청'이나 스팸함으로 분류될 수 있어요. 발송은 정상적으로 처리되었고, 수신자가 아직 확인하지 않은 상태예요. 수신자가 요청을 수락하면 이후 DM은 받은편지함으로 도착해요.",
        "next": "실패 DM 복구를 켜두시면 게시물에 안내 댓글을 남기고, 수신자가 다시 댓글을 남기면 자동으로 재발송해요.",
        "next_pending": "안내 댓글을 남겨두었어요. 수신자가 다시 댓글을 남기면 자동으로 재발송돼요.",
        "next_expired": "복구 가능 기간 내에 수신자의 새 댓글이 없어 자동 재발송이 종료되었어요."
      },
      "post_restricted": {
        "title": "이 게시물에는 자동 DM 발송이 제한되어 있어요",
        "cause": "인스타그램의 자동화 정책 또는 시스템 판단에 따라 이 게시물의 자동 DM 발송이 제한되었어요. 게시물마다 제한 여부가 다를 수 있어, 다른 게시물에서는 정상적으로 발송될 수 있어요.",
        "next": "다른 게시물로 캠페인을 만드시면 정상적으로 발송돼요."
      },
      "already_replied": {
        "title": "이 댓글에는 이미 답장이 발송되어 있어요",
        "cause": "인스타그램은 댓글 하나당 자동 DM 발송을 한 번만 허용해요. 해당 댓글 작성자에게 이미 직접 DM을 보내셨거나, 다른 DM 자동화 서비스가 함께 연결되어 있는 경우에 이렇게 처리될 수 있어요.",
        "next": "다른 DM 자동화 서비스를 함께 사용 중이시라면, 사용하지 않는 서비스의 연결을 해제해 중복 발송을 줄일 수 있어요."
      },
      "delivery_unconfirmed": {
        "title": "발송은 되었으나 도착이 확인되지 않았어요",
        "cause": "인스타그램에서 도착 확인 정보가 전달되지 않았어요. 계정의 설정 상태에 따라 발생할 수 있어요. 실제로는 전달되었을 수도 있으며, 도착 여부만 확인되지 않은 상태예요.",
        "next": "아래 항목을 확인해 보시면 도움이 될 수 있어요."
      },
      "send_delayed": {
        "title": "인스타그램 요청 제한으로 발송이 지연되고 있어요",
        "cause": "인스타그램은 짧은 시간에 요청이 많아지면 발송 속도를 일시적으로 조절해요. 발송 실패가 아니라 대기 상태이며, 제한이 해제되면 순서대로 발송돼요."
      },
      "send_incomplete": {
        "title": "발송이 완료되지 않았어요",
        "cause": "인스타그램 서버 오류로 발송이 완료되지 않았어요.",
        "next": "같은 캠페인에서 반복해서 발생한다면 문의해 주세요. 확인해 드릴게요."
      },
      "monthly_dm_limit": {
        "title": "이번 달 DM 발송 한도를 모두 사용했어요",
        "cause": "현재 플랜의 월 발송 한도에 도달해 이 건은 발송되지 않았어요. 한도는 매월 1일에 초기화돼요.",
        "next": "플랜을 업그레이드하시면 중단된 발송을 바로 이어갈 수 있어요. 댓글 작성 후 7일 이내인 건은 업그레이드 즉시 자동으로 다시 발송됩니다."
      },
      "campaign_not_active": {
        "title": "캠페인이 꺼져 있어 발송되지 않았어요",
        "cause": "댓글이 접수된 시점에 캠페인이 일시정지 상태였어요.",
        "next": "캠페인을 켜시면 이후 작성되는 댓글부터 발송돼요."
      },
      "outside_schedule_window": {
        "title": "예약된 발송 시간대가 아니어서 발송되지 않았어요",
        "cause": "댓글이 접수된 시각이 캠페인에 설정하신 발송 시간대 밖이었어요.",
        "next": "발송 시간대는 캠페인 설정에서 변경할 수 있어요."
      },
      "ig_account_inactive": {
        "title": "이 인스타그램 계정이 비활성 상태여서 발송되지 않았어요",
        "cause": "플랜에서 사용할 계정으로 선택되어 있지 않은 상태예요. 연결과 데이터는 그대로 보관돼요.",
        "next": "사용할 계정으로 선택하시면 이후 발송이 재개돼요."
      },
      "self_recipient": {
        "title": "계정 소유자 본인의 댓글이라 발송되지 않았어요",
        "cause": "자동 DM은 본인 계정에는 발송되지 않아요.",
        "next": "정상 동작이라 조치하실 일은 없어요."
      },
      "connection_disconnected": {
        "title": "인스타그램 연결이 해제되어 정리된 건이에요",
        "cause": "연결이 해제된 시점에 대기 중이던 발송 건이 함께 정리되었어요.",
        "next": "다시 연결하시면 이후 작성되는 댓글부터 발송돼요."
      },
      "duplicate_campaign_cleanup": {
        "title": "중복 발송을 방지했어요",
        "cause": "같은 게시물에 캠페인이 중복되어 있어, 같은 분께 두 번 발송되지 않도록 처리했어요."
      },
      "ghost_opening_cleanup": {
        "title": "중복 발송을 방지했어요",
        "cause": "이미 답장이 발송된 건이라, 같은 분께 두 번 발송되지 않도록 처리했어요."
      },
      "messaging_window_skip": {
        "title": "발송 가능 시간이 지나 발송되지 않았어요",
        "cause": "인스타그램이 허용하는 발송 가능 시간이 지난 뒤에 발송 순서가 되어, 발송을 시작하지 않았어요."
      },
      "other": {
        "title": "발송 중 문제가 발생했어요",
        "cause": "일시적인 오류로 메시지를 정상적으로 발송하지 못했어요.",
        "next": "계속 같은 문제가 발생하면 문의해 주세요."
      }
    },
    "checklist": {
      "recipient_account": {
        "title": "수신자 계정 상태",
        "description": "수신자가 비공개 계정이거나 메시지 수신을 제한한 경우일 수 있어요."
      },
      "ads_restriction": {
        "title": "광고 게시물 설정",
        "description": "광고 게시물이라면 광고 설정에 제한이 적용되어 있지 않은지 확인해 주세요."
      },
      "other_dm_tool": {
        "title": "다른 DM 자동화 서비스 연결",
        "description": "다른 DM 자동화 서비스가 함께 연결되어 있지 않은지 확인해 주세요. 같은 댓글에 두 서비스가 응답하면 한쪽만 발송돼요."
      }
    },
    "state": {
      "delivered": {
        "title": "수신자에게 전달됨",
        "cause": "메시지가 수신자에게 전달되었어요."
      },
      "read": {
        "title": "수신자가 읽었어요",
        "cause": "수신자가 메시지를 확인했어요."
      },
      "sent": {
        "title": "발송 완료",
        "cause": "발송이 완료되었어요."
      },
      "recovery_delivered": {
        "title": "재발송이 완료되었어요",
        "cause": "첫 발송이 전달되지 않았지만, 수신자가 안내를 보고 다시 댓글을 남겨 재발송이 완료되었어요."
      },
      "accepted": {
        "title": "발송 요청이 접수되었어요",
        "cause": "인스타그램이 발송 요청을 접수했어요. 도착 여부는 잠시 후 자동으로 확인돼요."
      },
      "queued": {
        "title": "발송을 준비하고 있어요",
        "cause": "발송 순서를 기다리고 있어요."
      },
      "submitting": {
        "title": "발송 중이에요",
        "cause": "인스타그램에 발송을 요청하고 있어요."
      },
      "pending": {
        "title": "발송을 준비하고 있어요",
        "cause": "발송 순서를 기다리고 있어요."
      }
    }
  }
}
```

### 필드 대응

| i18n 키 | 서버 응답 필드 | 화면 위치 |
|---|---|---|
| `dmAuto.reason.<user_reason>.title` | `frontend_action.title` | 모달 헤더 제목 |
| `dmAuto.reason.<user_reason>.cause` | `frontend_action.cause` | 제목 아래 첫 문단 |
| `dmAuto.reason.<user_reason>.next` | `frontend_action.next_step` | 그 아래 안내 문단 |
| `dmAuto.checklist.<id>.*` | `frontend_action.checklist[]` | 체크리스트 (UC-3) |
| `dmAuto.state.<status>.*` | `frontend_action.title`/`cause` | 성공·진행 중 (`user_reason` = `""`) |

**서버 값을 폴백으로 두세요** — `t('dmAuto.reason.' + ur + '.title', fa.title)` 형태면
나중에 새 사유가 생겨도 화면이 빈칸이 되지 않습니다.

### `hidden_request` 만 예외 — 복구 단계별로 `next` 가 갈립니다

같은 사유인데 복구 진행 상태에 따라 안내가 다릅니다. 서버 `next_step` 에는 알맞은 것이
이미 들어 있고, i18n 으로 그리실 거면 `status` 로 분기해 주세요.

| 조건 | 쓸 키 |
|---|---|
| `status === 'recovery_pending'` | `next_pending` |
| `status === 'recovery_expired'` | `next_expired` |
| 그 외 (`failed_param` + 복구 미사용) | `next` — 복구 켜기 안내 |

### 사유 → type · CTA 대응

| `user_reason` | `type` | `cta.action` |
|---|---|---|
| `connection_lost` | `reconnect` | `ig_reconnect` |
| `delivery_unconfirmed` | `checklist` | `reverify` |
| `hidden_request` (복구 미사용) | `info` | `enable_recovery` ⚠️ UC-5 |
| `send_delayed` | `wait` | — |
| 나머지 오류·건너뜀 | `info` | — |
| 성공·진행 중 | `success` / `wait` | — |

### 문구 작성 기준 (새 문구를 만드실 때)

문구를 다듬거나 새로 만드실 때 지켜 주시면 좋겠습니다. 백엔드 문구도 이 기준으로 썼습니다.

1. **탓하지 않고 상태로 설명** — "인스타그램이 막았어요" ✕ → "발송이 제한되어 있어요" ○
2. **관측한 것만 단정** — 우리는 Meta 응답만 압니다. 그 DM 이 상대 화면 어디에 있는지는
   모르므로 "이동했을 수 있어요"처럼 가능성으로 씁니다
3. **내부 사정 비노출** — 발송 대기열·검증 대기 시간·운영 조치는 쓰지 않습니다
4. **할 일이 없으면 `next` 줄을 아예 쓰지 않음** — "따로 조치하실 일은 없어요" 같은 문장은
   화면만 길게 하고 정보를 늘리지 않습니다. **`next` 가 없으면 그 자체로 "할 일 없음"** 입니다
   (JSON 에서 `next` 키가 빠진 사유가 그것)
5. **존댓 표현** — 사용자 동작에는 `-시-` ("연결하시면", "사용 중이시라면")

---

## UC-2. 하드코딩 우회 제거

```tsx
// src/app/pages/dm-auto/components/DMResultModal.tsx:22~27, 46~53
const PRIVATE_REPLY_SUBCODES = new Set(['2534022', '2534015']);
function isPrivateReplyWindowError(log) { … }
const action = isPrivateReplyWindowError(log)
  ? { ...rawAction, title: t('…sendFailedTitle'), description: t('…expiredOrDeletedDesc') }
  : rawAction;
```

**이 블록을 통째로 지워 주세요.** 해당 건은 이제 서버가 `user_reason: "window_expired"` 로
내려주고 문구도 사용자용으로 정리돼 있습니다.

> 참고: `2534022` 는 여전히 프론트 override 가 먼저 걸려서, **지우기 전까지는 새 문구가
> 안 보입니다.** 서버 배포 후 화면이 그대로여도 정상입니다.

---

## UC-3. 체크리스트 4개 → 3개

`delivery_unconfirmed` 의 자가 점검 항목이 줄었습니다. 프론트는 배열을 그대로 렌더하시므로
**코드 변경은 없을 것 같지만**, 항목 id 가 바뀌어 참조하시는 곳이 있으면 확인 부탁드립니다.

| 순서 | id | 내용 |
|---|---|---|
| 1 | `recipient_account` | 수신자가 비공개 계정이거나 메시지 수신을 제한한 경우 |
| 2 | `ads_restriction` | 광고 게시물의 광고 설정 제한 |
| 3 | `other_dm_tool` | 다른 DM 자동화 서비스 중복 연결 |

**삭제된 항목 2개**

- `message_access_allowed` — 확인 경로가 길고 실제 원인인 경우가 드물어 제외
- `default_routing_app` — ⚠️ **이건 잘못된 안내였습니다.** "Facebook 페이지 설정 > 고급 메시지
  설정 > 기본 라우팅 앱"을 안내했는데, 저희는 **Instagram Login** 방식이라 그 설정 화면이
  존재하지 않습니다. 저희 CS 안내 문서는 정반대로 "그 절차는 TurnFlow 에 해당하지 않는다"고
  안내 중이라, 두 안내가 서로 반대였습니다.

정적 엔드포인트(`GET /integrations/dm-verification/self-check-checklist/`)도 같이 바뀌었습니다.

---

## UC-4. 기술 정보 화면 비노출

로그 상세에서 **error_code / error_subcode / Meta 원문 오류 메시지를 표시하지 말아 주세요.**

- 사용자가 그 숫자로 할 수 있는 일이 없고, "코드 100"이 뜨면 불안만 커집니다
- CS 는 **로그 ID 로 어드민에서 조회**하는 편이 정확합니다

**API 필드는 당분간 그대로 둡니다** — 지금 `error_subcode` 를 UC-2 의 판정에 쓰고 계셔서,
빼면 깨집니다. UC-2 를 적용하신 뒤 알려주시면 응답에서도 제거하겠습니다.

---

## UC-5. 죽은 CTA 버튼 2건

조사하다 발견한 **기존 결함**입니다. 이번 변경으로 생긴 게 아닙니다.

### ① `enable_recovery` — 눌러도 아무 일이 없습니다

```tsx
// handleCtaClick — reverify / retry / ig_reconnect 만 처리
if (a === 'reverify') { … } else if (a === 'retry') { … } else if (a === 'ig_reconnect') { … }
// enable_recovery → 아무 분기에도 안 걸림
```

`hidden_request` + 복구 미사용 건에서 **"실패 DM 복구 켜기" 버튼이 렌더되지만 클릭이
무시**됩니다. 캠페인 편집의 복구 설정으로 보내주시면 됩니다.

> 이번에 CTA 를 새로 추가하지 않은 이유가 이것입니다. `post_restricted`(새 게시물로
> 캠페인 만들기)·`already_replied`(다른 DM 서비스 해제 가이드)에도 CTA 를 넣고 싶었지만,
> 핸들러 없이 내려보내면 죽은 버튼이 하나 더 늘어납니다. **핸들러를 붙여 주시면 그때
> 서버에서 CTA 를 추가**하겠습니다.

### ② `reverify` 버튼이 필요할 때 숨겨집니다

```tsx
// DMResultModal.tsx:206
if (cta.action === 'reverify' && !log.meta_message_id) return null;
```

`meta_message_id` 가 없으면 버튼을 숨기시는데, **2026-07-30 에 백엔드가 message_id 없이도
재검증할 수 있게 고쳤습니다**(Conversations API 로 발송 흔적 조회). 오히려 **message_id 가
없는 건이 재검증이 가장 필요한 건**입니다(성공 ack 유실 → prod 76건 전부 해당).

**이 조건을 제거해 주세요.**

---

## UC-6. 배지 색을 `user_reason` 기준으로 (다음 라운드 준비)

지금 색을 정하는 소스가 셋인데 서로 독립입니다.

| 소스 | 기준 | 쓰이는 곳 |
|---|---|---|
| `LOG_STATUS_STYLES` | `status` | 펼침 타임라인 로그 배지 |
| `STATUS_GROUP_META` | `status_group` | 수신자 행 배지 · 필터 탭 |
| `SEVERITY_STYLES` | `severity` | `DMResultModal` 헤더 |

그래서 **"상대방 사정"(계정 삭제·차단)처럼 정상 손실인 건도 빨간 실패로 보입니다.** 색을
차분하게 내리려면 목록 배지와 모달 헤더가 같은 축을 봐야 하는데, 지금은 `status` ↔ `severity`
로 갈려 있어 한쪽만 내리면 **빨간 행을 눌렀더니 파란 창이 뜨는** 상태가 됩니다.

그래서 이번 라운드에서는 **색을 하나도 바꾸지 않았습니다.** 요청은 준비 작업입니다:

> 로그 배지를 `status` 대신 **`frontend_action.user_reason`** 기준으로 그려 주세요.
> (탭·수신자 행 배지는 `status_group` 그대로 두시면 됩니다 — 그 축은 정확합니다.)

이게 되면 다음 라운드에 서버가 `severity` 를 사유 기준으로 내려보내 **목록과 모달이 함께**
차분해집니다. 색 결정(어느 사유를 info 로 내릴지)은 그때 같이 정하시죠.

---

## 참고 — 바뀌지 않는 것

혹시나 해서 명시합니다.

- **응답 필드는 하나도 제거되지 않았습니다.** 전부 추가만 했습니다
- **`severity` 는 `other` 하나만 바뀝니다** (`info` → `warning`). 나머지는 지금 색 그대로입니다.
  `other` 는 우리가 원인을 특정하지 못한 건이라 파란 `info` 로 두면 정상 처리처럼 읽혀서,
  모달 헤더를 주황·경고 아이콘으로 띄웁니다. 목록 배지는 아직 `status` 기준이라 회색으로
  남습니다 — UC-6 이 되면 함께 정리됩니다. **실데이터 발생 0건이라 화면 영향은 사실상 없습니다**
- **`status` · `status_group` · `status_group_display` 그대로**입니다
- **CTA action 값이 새로 생기지 않았습니다** (UC-5 참고)
- **통계 숫자(`needs_attention`·배달률·수신자 count)는 영향 없습니다** — 하드코딩된 status
  목록으로 집계하며 `severity`·`user_reason` 을 읽지 않습니다
- 마이그레이션 없음

## 검증

- dev 로그 **979건 전수**를 새 로직으로 돌려 사유 미매칭 0건 확인
- 회귀 테스트 50개 신규 (사유 드리프트 · 금지어 · 탭↔본문 자기모순 · 응답 모양)
- `integrations` + `admin_api` 전체 **1121 passed**, 신규 실패 0

## 질문 / 회신

UC-2·UC-5 를 적용하신 뒤 알려주시면 하위호환 필드(`title`/`description`)와 기술 정보 필드를
정리하겠습니다. UC-6 은 일정 맞춰 진행하시면 되고, 색 정책은 그때 같이 정하시죠.
