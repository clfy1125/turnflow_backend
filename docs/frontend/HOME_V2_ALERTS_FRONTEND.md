# 홈 화면 알림 API — 프론트 연동 가이드

작성 2026-09-10 · 백엔드 → 프론트엔드
대상 문서: 프론트 전달문 `backend-home-v2.md`(2026-09-04) 홈 개편(V2)

---

## 0. 세 줄 요약

1. **새 API 2개**가 dev 에 올라가 있습니다 — `GET /api/v1/home/alerts/`(현재 알림 상태) ·
   `POST /api/v1/home/alerts/dismiss/`(권유 알림 닫기).
   **요청하신 N4(게시물 건너뛰기 저장)는 이 두 개로 대체됩니다** — 별도 엔드포인트를 만들지
   않았습니다(§6).
2. 시안의 **7가지 상태 중 4가지는 그대로 쓰시고, ④「자동 DM N개가 멈춰 있어요」는 빼 주세요**
   (서버가 세는 숫자와 뜻이 다릅니다 — §7). 대신 **시안에 없던 「멈춤」 6종이 새로 내려갑니다.**
3. **문장은 프론트가 만듭니다.** 서버는 `code`(머신 키) + `data`(숫자·시각)만 줍니다.
   §5 에 문구 초안 JSON 을 넣어 뒀으니 그대로 i18n 에 넣으시면 됩니다.

**테스트 계정 4개**를 dev 에 만들어 뒀습니다 → §9.

---

## 1. 무엇이 바뀌었나

기존 홈은 「지금 할 일 한 가지」 7종만 보여주는 설계였습니다. 서버 쪽을 전수 조사해 보니
**자동화가 실제로 멈춘 상태 6가지가 시안에 통째로 빠져 있었습니다.**

| 시안에 없던 것 | 사용자에게 일어나는 일 |
|---|---|
| 이번 달 DM 한도 소진 | 한도를 넘긴 뒤 댓글이 **조용히 버려집니다** |
| 인스타가 발송을 막음 | 계정 전체 DM 정지 |
| 게시물이 막혀 캠페인 자동 정지 | 그 캠페인만 정지 |
| 결제 실패 | 7일 뒤 무료 플랜으로 강등 |
| 새 댓글이 안 들어옴 | 실시간 수신이 끊겨 DM 도 안 나감 |
| 쓸 계정을 골라야 함 | **강제 모달** (배너 아님) |

그래서 알림을 **세 등급**으로 나눴습니다.

- `critical` — **자동화가 멈춰 있음.** 닫을 수 없습니다(해결되면 저절로 사라집니다)
- `warning` — 완전히 멈추진 않았지만 새고 있음. 닫을 수 없습니다
- `todo` — 장애가 아니라 권유. **닫을 수 있습니다** (= 시안의 7가지가 대부분 여기)

화면을 어떻게 나눌지(배너 1층/2층, 카드 개수)는 **프론트 판단**입니다. 서버는 `rank` 만
정해 내려보냅니다.

---

## 2. `GET /api/v1/home/alerts/`

### 요청

```
GET /api/v1/home/alerts/[?workspace_id={uuid}]
Authorization: Bearer <access_token>
```

| 쿼리 | 필수 | 설명 |
|---|---|---|
| `workspace_id` | 조건부 | 사용자의 워크스페이스가 **여러 개일 때만** 필수. 하나면 생략(자동 결정). 여러 개인데 생략하면 400. |

### 호출 주기

**홈 진입 시 1회 + 이후 60초 폴링**을 권장합니다.
서버가 30초 캐시를 두므로 그보다 자주 불러도 값이 안 바뀝니다.

> **마음 놓고 폴링하셔도 됩니다.** 이 API 는 **인스타(Meta Graph)를 부르지 않습니다.**
> 최신 게시물·웹훅 상태는 서버가 1시간마다 미리 조회해 DB 에 적어 둔 값을 읽습니다.
> (홈에서 게시물 목록 API 를 직접 부르면 홈 진입 1회 = Graph 호출 1회가 되어
> 회사 전체가 나눠 쓰는 호출 한도를 태웁니다 — 그래서 알림 판정에서 분리했습니다.)

### 응답 200

```jsonc
{
  "generated_at": "2026-09-10T17:12:03+09:00",
  "workspace_id": "0f2c…",

  // null 이 아니면 "반드시 선택해야 넘어가는 모달". 배너가 아닙니다. §4 참고
  "blocking": null,

  // rank 오름차순으로 이미 정렬돼 있습니다. 프론트에서 다시 정렬하지 마세요.
  "alerts": [
    {
      "code": "dm_quota_exhausted",   // 머신 키 — i18n 키로 그대로 사용
      "level": "critical",            // critical | warning | todo
      "rank": 50,                     // 작을수록 위
      "scope": "workspace",           // workspace | ig_connection | campaign | page | report
      "target_id": null,              // scope 대상 식별자
      "target_label": "",             // 표시용 이름(@핸들·캠페인명). 없으면 ""
      "dismissible": false,           // 닫기 버튼을 그릴지
      "dismiss_key": "",              // 닫기 요청에 그대로 넣을 값
      "since": null,                  // 이 상태가 시작된 시각(ISO8601) 또는 null
      "data": {                       // 문구에 넣을 숫자·시각. code 마다 키가 다릅니다
        "used": 200,
        "limit": 200,
        "blocked_count": 37,
        "resumable_count": 31,
        "resumes_on_upgrade": true,
        "resets_at": "2026-10-01T00:00:00+09:00",
        "period_start": "2026-09-01T00:00:00+09:00"
      }
    }
  ],

  "counts": { "critical": 1, "warning": 0, "todo": 1 }
}
```

### 에러

| 코드 | 언제 | 본문 |
|---|---|---|
| 400 | 워크스페이스가 여러 개인데 `workspace_id` 미지정 / 잘못된 id / 소속 워크스페이스 없음 | 아래 공통 포맷 |
| 401 | 토큰 없음·만료 | DRF 기본 |
| 403 | 그 워크스페이스 멤버가 아님 | 공통 포맷 |
| 500 | 서버 오류 | 공통 포맷 |

```jsonc
{ "success": false, "error": { "code": 400, "message": "워크스페이스가 여러 개입니다. workspace_id 를 지정하세요.", "details": {} } }
```

> 판정 하나가 실패해도 **200 으로 나머지를 내려보냅니다.** 홈 첫 화면이라 한 항목 때문에
> 전체가 비면 안 되기 때문입니다. 그래서 500 은 사실상 나오지 않습니다.

---

## 3. 알림 코드 전체 목록

`rank` 는 서버가 정합니다. 원칙은 **돈이 새는 순 → 사용자가 지금 손쓸 수 있는 순**
(기다리는 것 말고 할 게 없는 「발송 제한」이 아래로 내려간 이유입니다).

### 3-1. `critical` — 자동화가 멈춰 있음 (닫기 불가)

| rank | code | scope | data 키 | CTA 제안 |
|---|---|---|---|---|
| 10 | `payment_failed` | workspace | `plan` `amount` `grace_ends_at` `next_retry_at` `card_masked` | 결제 수단 확인 |
| 20 | `ig_disconnected` | ig_connection | `status` `reason` `revives_on_reconnect` | 다시 연결하기 |
| 40 | `comment_stream_down` | ig_connection | `checked_at` | 연결 점검하기 |
| 50 | `dm_quota_exhausted` | workspace | `used` `limit` **`blocked_count`** **`resumable_count`** `resumes_on_upgrade` `resets_at` `period_start` | 플랜 변경하기 |
| 60 | `campaign_post_restricted` | campaign | `campaign_id` `name` `media_id` `permalink` `paused_at` `other_posts_unaffected` | 캠페인 보기 |
| 70 | `dm_send_blocked` | ig_connection | `seconds_remaining` `resumes_at` `waiting_count` `auto_resumes` | (없음 — 기다리면 자동 재개) |
| 80 | `ig_account_disabled` | workspace | `count` `allowance` `active` `accounts[]`(`id`·`username`) | 계정 켜기 |

`ig_disconnected` 의 `reason` 은 4종입니다 — 문구를 원인마다 다르게 하실 수 있습니다.

| reason | 뜻 |
|---|---|
| `token_invalidated` | 인스타 쪽에서 접속 권한이 무효화됨 |
| `account_checkpoint` | 인스타가 본인확인을 요구하는 상태 |
| `app_removed` | 사용자가 인스타 설정에서 앱 연결을 해제 |
| `reconnect_required` | 그 외 (기본값) |

`campaign_post_restricted` 는 **캠페인마다 1건**씩 내려갑니다(최대 20건).
`other_posts_unaffected: true` 는 「다른 게시물에서는 정상 발송된다」는 뜻입니다 —
범위를 안 밝히면 "내 계정 전체가 막혔나" 오해가 생기므로 문구에 꼭 넣어 주세요.

### 3-2. `warning` — 새고 있음 (닫기 불가)

| rank | code | data 키 | CTA 제안 |
|---|---|---|---|
| 100 | `dm_quota_warning` | `used` `limit` `remaining` `resets_at` | 플랜 보기 |
| 110 | `subscription_ending` | `ends_at` `plan` `days_left` | 구독 유지하기 |

### 3-3. `todo` — 권유 (닫기 가능)

| rank | code | scope | data 키 | dismiss_key |
|---|---|---|---|---|
| 200 | `recent_post_no_campaign` | ig_connection | `ig_connection_id` `media_id` `published_at` `permalink` | media_id |
| 210 / 999 | `create_campaign` | workspace | `campaign_total` `is_default` | (닫기 불가) |
| 220 | `link_page_empty` | page | `reason` `page_id` `slug` `blocks_count` `is_public` | page_id 또는 `"none"` |
| 230 | `report_ready` | report | `report_id` `status` `progress` `stage` `error_code` | report_id |
| 231 | `report_running` | report | 〃 | report_id |
| 232 | `report_failed` | report | 〃 | report_id |
| 240 | `migration_review_pending` | workspace | `count` `job_id` | job_id |

**`recent_post_no_campaign`** = 시안 ①. 판정은 **최근 7일 이내에 올린 게시물인데
그 게시물로 만든 캠페인이 없음**입니다. 카드 이미지가 필요하면 `media_id` 로 기존
게시물 API(`…/media/?media_ids=`)를 부르세요 — 알림 응답에는 이미지 URL 이 없습니다
(인스타 서명 URL 이라 캐시해 두면 깨집니다).

**`create_campaign`** = 시안 ⑥의 확장입니다. **항상 내려갑니다.**

- 캠페인 0건 → `rank: 210`, `data.is_default: false` (정상 권유)
- 캠페인 1건 이상 → `rank: 999`, `data.is_default: true` (**기본 상태**)

「기본 상태」란 알릴 게 하나도 없을 때 홈이 비지 않도록 두는 마지막 카드입니다.
그래서 **이 항목만 `todo` 인데도 닫을 수 없습니다**(`dismissible: false`).

**`link_page_empty`** 의 `reason` 3종:

| reason | 뜻 | `target_id` |
|---|---|---|
| `no_page` | 페이지가 하나도 없음 | `"none"` |
| `no_blocks` | 페이지는 있는데 블록 0개 | 그 페이지 id |
| `private` | 내용은 있는데 비공개 | 그 페이지 id |

> 공개돼 있고 블록이 있는 페이지가 **하나라도** 있으면 이 알림은 내려가지 않습니다.

---

## 4. `blocking` — 강제 모달

`blocking` 이 `null` 이 아니면 **배너가 아니라 반드시 선택해야 넘어가는 모달**을 띄웁니다.
바이오링크 페이지가 플랜 축소 때 쓰는 것과 같은 성격입니다.

```jsonc
"blocking": {
  "code": "ig_account_selection_required",
  "data": { "max_ig_accounts": 1, "total_accounts": 3, "active_accounts": 3 }
}
```

현재 코드는 이 하나뿐이며, 다음 셋 중 하나라도 참일 때 뜹니다.

1. 켜진 계정 수 > 허용량 (플랜 축소·추가계정 축소)
2. 연동은 있는데 **켜진 계정이 0개** (전면 정지 상태)
3. 서버가 재선택을 요구하는 플래그가 서 있을 때

**모달에서 쓰실 화면은 기존 것 그대로입니다** —
`GET/POST /api/v1/billing/ig-account-activation/`.
판정을 그 엔드포인트와 **같은 함수**로 통일해 뒀으므로,
「모달은 떴는데 조정할 게 없다」 같은 어긋남은 생기지 않습니다.

---

## 5. 문구 초안 (i18n 에 그대로 넣으실 수 있게)

구조는 기존 DM 문구와 같은 **제목 → 이유 → 다음 행동** 3부입니다.
조치할 게 없는 항목은 `next` 키를 **아예 쓰지 않습니다**(없는 게 곧 "할 일 없음").

```json
{
  "payment_failed": {
    "title": "결제가 완료되지 않았어요",
    "cause": "등록하신 카드로 결제가 승인되지 않았어요. 한도 초과, 카드 유효기간 만료 등이 원인일 수 있어요.",
    "next": "{{grace_ends_at}}까지 결제가 확인되지 않으면 무료 플랜으로 전환돼요. 그때까지는 현재 플랜을 그대로 사용하실 수 있어요.",
    "cta": "결제 수단 확인하기"
  },
  "ig_disconnected": {
    "title": "{{target_label}} 연결이 끊겼어요",
    "cause_token_invalidated": "인스타그램에서 이 앱의 접속 권한이 해제됐어요.",
    "cause_account_checkpoint": "인스타그램이 계정 본인확인을 요청한 상태예요.",
    "cause_app_removed": "인스타그램 설정에서 이 앱 연결이 해제됐어요.",
    "cause_reconnect_required": "인스타그램 연결이 끊어졌어요.",
    "next": "다시 연결하시면 댓글 작성 후 7일 이내의 건은 별도 설정 없이 자동으로 다시 발송돼요.",
    "cta": "다시 연결하기"
  },
  "comment_stream_down": {
    "title": "새 댓글을 받지 못하고 있어요",
    "cause": "인스타그램에서 실시간 수신 연결이 끊어졌어요.",
    "next": "버튼을 누르시면 바로 복구돼요.",
    "cta": "실시간 수신 복구하기"
  },
  "dm_quota_exhausted": {
    "title": "이번 달 DM 발송 한도를 모두 사용했어요",
    "cause": "지금 {{blocked_count}}건의 요청이 발송되지 못하고 기다리고 있어요. 한도는 {{resets_at}}에 초기화돼요.",
    "next": "플랜을 올리시면 아직 시간이 남은 {{resumable_count}}건은 바로 다시 발송돼요.",
    "cta": "플랜 변경하기"
  },
  "campaign_post_restricted": {
    "title": "‘{{target_label}}’ 발송이 멈췄어요",
    "cause": "이 게시물에 자동 DM 발송이 제한되어 있어요. 다른 게시물에서는 정상적으로 발송돼요.",
    "next": "다른 게시물로 캠페인을 만들어 보세요.",
    "cta": "캠페인 보기"
  },
  "dm_send_blocked": {
    "title": "발송이 일시적으로 멈췄어요",
    "cause": "인스타그램 요청 제한으로 {{resumes_at}}까지 발송이 중단됐어요. 대기 중인 {{waiting_count}}건은 재개되면 순서대로 발송돼요."
  },
  "ig_account_disabled": {
    "title": "사용하지 않는 계정이 {{count}}개 있어요",
    "cause": "연결과 데이터는 그대로 보관돼 있지만, 지금은 자동 DM에서 제외돼 있어요.",
    "next": "사용할 계정으로 켜시면 발송이 시작돼요.",
    "cta": "계정 켜기"
  },
  "dm_quota_warning": {
    "title": "이번 달 발송 한도의 {{percent}}%를 사용했어요",
    "cause": "남은 {{remaining}}건을 모두 사용하면 이후 작성되는 댓글에는 발송되지 않아요.",
    "cta": "플랜 보기"
  },
  "subscription_ending": {
    "title": "{{days_left}}일 뒤 구독이 종료돼요",
    "cause": "{{ends_at}}에 무료 플랜으로 전환되며, 그때부터 유료 기능을 사용하실 수 없어요.",
    "cta": "계속 이용하기"
  },
  "recent_post_no_campaign": {
    "title": "이 게시물에 자동 DM을 만들까요?",
    "cause": "{{published_at}}에 올리신 게시물에 아직 자동 DM이 없어요.",
    "cta": "자동 DM 만들기",
    "cta_secondary": "이 게시물 건너뛰기"
  },
  "create_campaign": {
    "title": "게시물 댓글에 자동으로 안내해 보세요",
    "cause": "댓글에 키워드가 달리면 DM으로 안내 메시지를 보내드려요.",
    "cta": "자동 DM 만들기"
  },
  "link_page_empty": {
    "title_no_page": "링크 페이지를 만들어 보세요",
    "title_no_blocks": "링크 페이지가 아직 비어 있어요",
    "title_private": "링크 페이지가 비공개 상태예요",
    "cta": "링크 페이지 편집하기"
  },
  "report_ready": { "title": "계정 분석 리포트가 완성됐어요", "cta": "리포트 보기" },
  "report_running": { "title": "계정 분석 리포트를 만들고 있어요", "cause": "평균 15분 정도 걸려요." },
  "report_failed": { "title": "리포트 생성이 완료되지 않았어요", "cta": "다시 시도하기" },
  "migration_review_pending": {
    "title": "불러온 캠페인 {{count}}개가 확인을 기다리고 있어요",
    "cta": "확인하러 가기"
  }
}
```

문구를 고치실 때 지켜 주셨으면 하는 것 (기존 DM 문구 방침과 동일):

- **우리 쪽 사정은 쓰지 않습니다** (대기열 밀림·검증 대기·운영 수동 조치).
- **원인을 안 밝히면서 숙제만 주는 문장도 함께 뺍니다** — 「설정을 확인해 보세요」류.
  확인시키면 결국 문의로 돌아옵니다.
- **원인을 단정하지 않습니다.** ①인스타 공식 문서에 있고 ②우리 로그에서 실제로 관측되는
  것만 씁니다. (「비밀번호를 바꾸셨거나」 「연결 유효기간 60일 만료」는 둘 다 해당 없음 —
  쓰지 마세요.)

---

## 6. `POST /api/v1/home/alerts/dismiss/` — 권유 알림 닫기

> **요청하신 N4(게시물 건너뛰기 저장)가 이것입니다.** `…/media/{id}/dismiss/` 를 따로 만들지
> 않고 알림 닫기로 통일했습니다 — 「건너뛰기」가 게시물만이 아니라 리포트·링크페이지·불러오기
> 안내에도 똑같이 필요해서, 하나로 두는 편이 화면마다 규칙이 갈리지 않습니다.

### 요청

```jsonc
POST /api/v1/home/alerts/dismiss/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "code": "recent_post_no_campaign",
  "target_key": "17912…",        // 알림의 dismiss_key 를 그대로. 없으면 생략
  "workspace_id": "0f2c…"        // 워크스페이스가 여러 개일 때만
}
```

### 응답 200

```json
{ "code": "recent_post_no_campaign", "target_key": "17912…", "dismissed_at": "2026-09-10T17:20:00+09:00" }
```

### 규칙

- **브라우저가 아니라 서버에 저장합니다.** PC 에서 건너뛴 게시물이 폰·앱에서 다시 뜨지 않습니다.
- **다시 뜨는 기준은 `target_key`** 입니다. 게시물 A 를 건너뛰어도 **새로 올린 게시물 B 는
  다시 뜹니다.** 리포트·불러오기도 같습니다(새 리포트 = 새 id = 다시 뜸).
- **멱등**입니다. 같은 것을 두 번 닫아도 200 이고, 처음 닫은 시각을 돌려줍니다.
- `critical`/`warning` 과 `create_campaign` 은 **400** 입니다.
  → 이 항목들에는 닫기 버튼을 그리지 마세요(`dismissible: false` 로 이미 구분됩니다).

```jsonc
// 400
{ "success": false, "error": { "code": 400, "message": "이 알림은 닫을 수 없습니다. 문제가 해소되면 자동으로 사라집니다.", "details": {} } }
```

---

## 7. 시안에서 바꿔 주셔야 할 것

| 시안 | 결정 |
|---|---|
| ① 이 게시물에 자동 DM을 만들까요? | **유지.** `recent_post_no_campaign`. 판정 기준은 **최근 7일 이내 게시물** |
| ② ‘신제품 이벤트’가 DM을 보내고 있어요 | **알림 대상 아님.** 진행 현황이라 기존 큐 상태 API 를 그대로 쓰세요(§8 N3 참고) |
| ③ Instagram 연결이 끊겼어요 | **유지.** `ig_disconnected` (+ `reason` 4종으로 문구 분기 가능) |
| ④ 자동 DM N개가 멈춰 있어요 | ❌ **빼 주세요.** 아래 설명 |
| ⑤ 자동 DM 설정을 이어서 끝내세요 | **유지 + 지금처럼 브라우저 저장.** 서버에 초안 개념을 두지 않기로 했습니다 |
| ⑥ 게시물 댓글에 자동으로 안내해 보세요 | **유지.** `create_campaign` (기본 상태 역할도 겸함) |
| ⑦ 링크 페이지를 업데이트해 보세요 | **유지.** `link_page_empty` |

**④를 빼는 이유** — 서버가 세는 「조치 필요」는 **캠페인이 멈춘 개수가 아니라, 개별 발송 중
완료 확인이 안 된 건수**입니다. 그대로 붙이면 **잘 돌아가는 캠페인에 「멈춰 있어요」가
붙습니다.** 진짜로 멈춘 것은 `campaign_post_restricted`(게시물 제한) ·
`dm_send_blocked`(계정 발송 제한) · `dm_quota_exhausted`(한도) 세 가지이고, 이건 이미
따로 내려갑니다.

---

## 8. 이번 회신에 **포함되지 않은** 것

| 요청 | 상태 |
|---|---|
| **N1** 링크 페이지 목록에 대표 이미지·블록 수 | ⏳ 미포함. 대표 이미지는 캠페인 썸네일처럼 **우리 스토리지에 재호스팅**하는 작업이 먼저라 별건으로 진행합니다. 홈의 「비어 있음」 판정은 `link_page_empty.data.blocks_count` 로 지금 가능합니다 |
| **N2** 링크 페이지 전체 합산 통계 | ⏳ 미포함 |
| **N3** 큐 상태에 「지금 발송 중인 캠페인」 | ⏳ 미포함. 시안 ②를 알림에서 빼기로 해 우선순위가 내려갔습니다. 필요하시면 알려주세요 |
| **N4** 게시물 건너뛰기 저장 | ✅ **완료** — §6 의 dismiss 로 대체 |
| **Q3** `connections` 에 `reconnect_reason` 이 실제로 오나? | ✅ **옵니다.** DB 값이고 라이브 호출 없이 내려갑니다 (알림의 `ig_disconnected.data.reason` 과 같은 값) |
| **Q8** `billing/my-subscription` 을 홈에서 매번 불러도 되나? | ✅ **됩니다.** 구독 레코드가 없으면 무료 구독을 만들지만 **최초 1회뿐**이고 이후엔 조회입니다. 다만 홈 알림에 필요한 값(한도·미납·체험·축소 예약)은 **이 알림 API 에 이미 다 들어 있어** 굳이 부르지 않으셔도 됩니다 |
| Q1·Q2·Q4·Q5·Q6·Q7·Q9 | ⏳ 별도 회신 |

---

## 9. 테스트 계정 (dev)

`https://dev-api.turnflow.link` 기준. **비밀번호는 전부 `Test1234!`**

| 계정 | 재현되는 상태 |
|---|---|
| `home-clean@test.com` | **알림 0건** — `create_campaign`(기본 상태)만. 빈 홈 화면 확인용 |
| `home-critical@test.com` | **멈춤 6종 전부** — 결제 실패 · 연결 끊김 · 새 댓글 안 들어옴 · 한도 소진(막힌 건 37, 되살릴 수 있는 건 31) · 게시물 제한 · 발송 제한(대기 12건) |
| `home-todo@test.com` | **권유 5종 + 한도 경고** — 새 게시물 · 빈 링크페이지 · 리포트 완료 · 불러오기 검수 3건 · 한도 85% |
| `home-blocking@test.com` | **강제 모달** — 허용량 1개인데 계정 3개가 켜져 있음 |

실제 응답 (dev 실측):

```
home-clean       counts {critical:0, warning:0, todo:1}   → create_campaign(999)
home-critical    counts {critical:6, warning:0, todo:2}   → payment_failed(10) · ig_disconnected(20)
                                                            comment_stream_down(40) · dm_quota_exhausted(50)
                                                            campaign_post_restricted(60) · dm_send_blocked(70)
home-todo        counts {critical:0, warning:1, todo:5}   → dm_quota_warning(100) · recent_post_no_campaign(200)
                                                            link_page_empty(220) · report_ready(230)
                                                            migration_review_pending(240)
home-blocking    blocking = ig_account_selection_required
```

계정 상태가 흐트러지면 백엔드에서 다시 만들어 드립니다
(`python manage.py seed_home_alerts_dev`, 멱등이라 여러 번 돌려도 같은 상태).

> ⚠️ 이 계정들의 인스타 토큰은 **가짜**입니다. 홈 알림 API 는 인스타를 부르지 않으므로
> 응답은 완전히 재현되지만, **게시물 이미지 조회 같은 인스타 직접 호출은 실패**합니다.

---

## 10. 참고 — 서버가 안 만들기로 한 알림

문의가 올 수 있어 적어 둡니다. 아래는 **일부러 만들지 않았습니다.**

| 안 만든 것 | 이유 |
|---|---|
| 「연결이 곧 만료돼요」 예고 | 6시간마다 도는 작업이 만료 2주 전에 자동 갱신합니다. 정상 사용 계정에서는 만료가 **일어나지 않습니다** |
| 무료 체험 종료 3일 전 안내 | 결제 화면에서 이미 사전 동의를 받았습니다. 다시 알리면 해지 유도만 됩니다 |
| 추가 계정 축소 예약 | 홈 알림이 아니라 **설정 화면에서 「다음 달부터 N개」로 보여주는 것**이 맞습니다 |
| 끝난 캠페인이 켜져 있음 / 게시물 삭제됨 | 사용자가 의도적으로 그랬을 수 있어 알릴 근거가 약합니다 |
| 발송 대기열이 길어짐 | 순서대로 나가는 중이라 장애가 아닙니다(기존 큐 게이지로 충분) |
| 첫 DM 발송 성공 · 정지 구독 재개 예정 | 알림으로서 가치가 낮다고 판단했습니다 |
| 다른 DM 툴이 같이 붙어 있음 | 기술적으로 특정이 어려워 보류 |
| 지난 알림 이력 · 읽음 표시 | 현재 상태 배너로 대부분 덮입니다. 필요해지면 그때 만듭니다 |

문의: 백엔드
