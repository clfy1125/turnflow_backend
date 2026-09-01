# 백엔드 회신 — 발송 쿨다운(계정 제한) 상태: `user_reason` + dev 재현

회신 2026-08-27 · 요청 `프론트 요청 — 발송 쿨다운(계정 제한) 상태` (2026-08-27)
관련 사건: prod CS #66027015 (2026-08-26, @use.ai.likejimin)

---

## 요약

| 요청 | 상태 |
|---|---|
| 1. 대기 로그에 `user_reason` | ✅ **완료** — 새 코드 `account_send_paused` |
| 2. dev 재현 수단 | ✅ **완료** — 고정 계정 대신 **토글 커맨드**(요청하신 "더 편한 방법") |
| 3-(1) 쿨다운 대기자의 `status_group` | ✅ **`waiting`** — 걱정하신 `attention` 아닙니다 |
| 3-(2) 정지 중 대기 건이 없어지나 | ⚠️ **첫 DM 은 안전 · 2번째 DM 은 없어집니다** → 신규 필드로 뺄 수 있게 했습니다 |

**3-(2) 때문에 요청하신 문구를 그대로 쓰시면 안 됩니다.** 다만 "숫자를 빼고 뭉뚱그리는"
대신, 안전한 숫자를 계산해 내려보내도록 응답을 늘렸습니다 — 아래 §4 를 먼저 봐주세요.

---

## 1. `user_reason` — `account_send_paused` 신규 (요청대로)

`send_delayed` 재사용 대신 **새 코드**로 만들었습니다. 지적하신 그대로 "개별 요청 속도
조절"과 "계정 전체가 멈춤"은 사용자에게 전혀 다른 사건이고, 실제로 이번 CS 의 20분 통화가
그 구분이 화면에 없어서 생겼습니다.

```jsonc
// GET /api/v1/integrations/dm-verification/?campaign_id=...&status=queued
{
  "status": "queued",
  "status_group": "waiting",          // 그대로 '대기중'
  "frontend_action": {
    "user_reason": "account_send_paused",   // ← 신규
    "severity": "warning",                  // 기존 queued 는 info(파랑)였습니다
    "type": "wait"
  }
}
```

**알아두실 것 3가지**

1. **`severity` 를 `warning` 으로 올렸습니다.** `queued` 는 원래 `info`(파랑)입니다. 그런데
   `status_group=waiting` 을 프론트가 amber 로 그리고 있어서 **배지는 amber, 모달 헤더는
   파랑**으로 어긋나 있었습니다. 이번 변경으로 둘이 일치합니다.
2. **정지가 풀리면 값이 저절로 사라집니다.** 로그에 표식을 쓰는 방식이 아니라 **읽는 시점에
   계정 상태를 보고 판정**합니다. 그래서 (a) 마이그레이션이 없고 (b) 이미 쌓여 있던 과거
   로그에도 소급 적용되며 (c) 재개 후 별도 정리가 필요 없습니다.
3. **종결된 건은 절대 덮이지 않습니다.** 정지 중이어도 이미 실패한 로그(`failed_window`,
   `failed_param` …)는 자기 사유를 유지합니다. 덮으면 "기다리면 가겠지"로 오해하니까요.
   덮이는 status 는 `queued` / `submitting` / `pending` / `rate_limited` 넷뿐입니다.

문구(`title`/`cause`/`next_step`)는 요청대로 **안 쓰셔도 됩니다.** 다만 응답에서 빼지는
않았습니다 — 어드민 콘솔이 "고객이 보는 화면"을 미리보기로 그릴 때 같은 함수를 쓰고 있어서,
비우면 그쪽이 빈칸이 됩니다. i18n 키는 `account_send_paused` 로 잡으시면 됩니다.

수신자 목록 배지(`resolveLogBadge`)도 같은 값을 받습니다. 어드민 로그 상세(`user_view`)에도
동일하게 반영했습니다.

---

## 2. dev 재현 — 고정 계정 대신 토글 커맨드

말씀하신 대로 **커맨드가 낫다**고 판단해 그쪽으로 만들었습니다. 상태가 소모되지 않고,
아무 계정에나 걸었다 풀 수 있습니다.

```bash
# 정지 걸기 + 대기 47명 만들기  (계정 A)
docker compose exec web python manage.py dm_account_pause \
    --account dmdummy_pro_cool --pause --hours 21 --queue 47

# 정지 풀기
docker compose exec web python manage.py dm_account_pause \
    --account dmdummy_pro_cool --release

# 지금 정지 중인 계정 보기
docker compose exec web python manage.py dm_account_pause --list
```

`--queue` 는 재실행하면 이전 더미만 지우고 다시 만듭니다(재시드 = 같은 명령 재실행).
`--pause` / `--queue` 는 **DEBUG=True 에서만** 동작합니다.

### 접속 정보 (dev-api.turnflow.link)

기존 DM 더미 계정을 그대로 씁니다 — 새로 만들지 않았습니다.

```
이메일   dmdummy-pro@turnflow.dev
비밀번호 Test1234!
```

없으면 먼저: `python manage.py seed_dm_dev_dummy`

### 계정 A — cooldown-active (실제 응답 실측값)

```
ig_connection_id = 14fa29c4-32ab-4aa6-a5c3-32320d8c66cc
campaign_id      = f40ae8ab-0cca-4d14-bd8e-67fee713f0e3
```

```jsonc
// GET /dm-verification/queue-state/?campaign_id=f40ae8ab-...
{
  "blocking_reason": "action_block_cooldown",
  "action_block_cooldown_seconds": 75039,
  "gauge":  { "waiting": 66, "sent": 6, "total": 72 },   // 이벤트 단위
  "people": { "waiting": 57, "sent": 6, "total": 63 },   // 사람 단위
  "waiting_window_risk": { "people": 0, "events": 4, "followup_events": 4, "horizon_s": 75039 },
  "generated_at": "..."
}
```

`gauge.waiting`(66) ≠ `people.waiting`(57) 로 **일부러 벌려 놨습니다.** 요청하신 대로
"47명"은 `people.waiting` 을 쓰셔야 하는데, 두 값이 같으면 잘못된 필드를 배선해도 화면이
맞아 보여서 버그를 못 잡습니다. 더미 47명 중 9명에게 2번째 DM 도 함께 대기시켰습니다.

### 계정 C — queue-normal (대조군)

```
ig_connection_id = e4cb5b83-b16c-4150-8891-c7aa1e9e0445
campaign_id      = 108302fe-f173-46fe-adf4-538283ba1962
blocking_reason  = null · cooldown_seconds = 0 · gauge.waiting = 26 · user_reason = ""
```

### ⚠️ 계정 B — cooldown-notime 은 **만들 수 없습니다**

`blocking_reason: "action_block_cooldown"` 이면서 `action_block_cooldown_seconds: 0` 인
상태는 **서버에 존재하지 않습니다.** `blocking_reason` 자체가 "잔여 초 > 0" 일 때만 세팅되기
때문입니다(둘이 같은 값에서 파생됩니다). 쿨다운은 항상 절대 만료 시각을 갖고 있어서 "정지
중인데 언제 풀릴지 모름" 이 될 수 없습니다.

**다만 "재개 시각을 모르는 화면"은 실제로 있습니다** — 로그 상세 모달입니다. 거기서 받는
`user_reason=account_send_paused` 에는 잔여 시간이 없습니다(계정 축이 아니라 로그 축이라).
그 화면에서 시각을 보여주려면 `queue-state` 를 함께 부르셔야 하고, 안 부르실 거면 그때
"재개 시각 없음" 문구를 쓰시면 됩니다. 그 분기는 살려두시는 게 맞습니다.

---

## 3. 질문 (1) — 쿨다운 대기자의 `status_group`

### **`waiting`** 입니다. amber "대기중" 으로 나갑니다.

걱정하신 `attention`("전송 실패 47") 이 **아닙니다.** 근거:

- `dm_status_groups._STATUS_TO_GROUP` 에서 `queued` / `submitting` / `rate_limited` /
  `pending` 이 전부 `WAITING` 으로 접힙니다. 정지는 status 를 바꾸지 않으므로 그대로입니다.
- 운영 계정 실측(2026-08-26 @use.ai.likejimin, 정지 중 394건): 수신자 롤업 전 행이
  `waiting` 이었습니다. 실패로 집계된 건 0입니다.
- 회귀 테스트로 고정했습니다 — 누가 나중에 이 매핑을 바꾸면 테스트가 깨집니다
  (`test_account_send_paused.py::TestStatusGroupStaysWaiting`).

---

## 4. 질문 (2) — 정지 중 대기 건이 없어질 수 있습니까

**세 가지 중 하나가 걸립니다.** 정직하게 나눠 답합니다.

### (a) 정지 중 새로 달리는 댓글도 큐에 들어갑니까 → **네, 놓치는 구간 없습니다**

정지는 **발송 직전 게이트**라 그 앞 단계(웹훅 수신 → 댓글 판정 → 로그 적재)는 정상
동작합니다. 정지 중 들어온 댓글도 `queued` 로 쌓입니다.

운영 실측이 그대로 증명합니다 — 8/26 사건에서 정지는 03:08 KST 에 걸렸고 큐에 쌓인 394건의
`created_at` 은 **03:08 부터 13:24(해제 직전)까지 끊김 없이 분포**했습니다. 10시간 내내
계속 적재됐다는 뜻입니다.

### (b) 큐 보관 만료가 따로 있습니까 → **없습니다**

- 정지로 인한 대기는 **재시도 횟수를 소모하지 않습니다**(오류 재시도 경로와 다른 분기라
  `retry_count` 가 올라가지 않습니다). 그래서 "N회 초과로 폐기" 가 없습니다.
- 로그 보존 삭제 배치(`SENTDMLOG_ARCHIVE_RETENTION_DAYS`)는 **운영에서 0 = 비활성**입니다
  (방금 prod 런타임 값 확인). 켜져 있지 않습니다.

### (c) 24시간 붙잡는 동안 인스타그램 창을 넘겨 영구히 못 보내는 건이 생깁니까

## → **첫 DM 은 안 생깁니다. 2번째 DM 은 생깁니다.**

| | 창 | 24h 정지를 견디나 |
|---|---|---|
| **첫 DM** (댓글 답장) | **7일** | ✅ 견딤 (여유 6일) |
| **2번째 DM** (자료·게이트 리워드) | **24시간** | ❌ **구조적으로 못 견딤** |

2번째 DM 은 댓글이 아니라 사용자 ID 로 보내는 경로라 창이 **24시간**인데, 기본 쿨다운도
**24시간**입니다. 그래서 **정지가 걸리는 순간 대기 중이던 2번째 DM 은 재개 시각과 만료
시각이 사실상 같아집니다.**

실측(8/26, 가장 오래된 1건):

```
창 만료  2026-08-26 18:08:15.536Z
재개 예정 2026-08-26 18:08:16.131Z     ← 0.6초 늦음 → 자동 재개를 기다렸으면 확정 소실
```

우연이 아니라 설계상 그렇습니다. 이번엔 저희가 **조기 해제**해서 29건을 전부 살렸지만,
수동 개입이 없으면 못 살립니다. (별도 과제로 잡아 두었습니다 — 정지 중에도 2번째 DM 만
예외 처리하거나, 창 임박분을 우선 투입하는 방향.)

### 그래서 문구를 낮추는 대신 — **뺄 수 있는 숫자를 드립니다**

`queue-state` 에 `waiting_window_risk` 를 추가했습니다.

```jsonc
"people":  { "waiting": 57 },
"waiting_window_risk": {
  "people": 3,            // 첫 DM 이 창을 넘길 사람 수 — people.waiting 과 같은 모수
  "events": 7,            // 창을 넘길 대기 이벤트 총수
  "followup_events": 4,   // 그중 2번째 DM (사람 축에는 안 들어감)
  "horizon_s": 75039      // 판정에 쓴 재개까지 남은 초
}
```

> 위 숫자는 **설명용 예시**입니다. §2 의 계정 A 더미는 첫 DM 이 전부 방금 들어온 것이라
> `people: 0 / events: 4 / followup_events: 4` 로 나옵니다(2번째 DM 4건만 위험).
> 첫 DM 위험분까지 화면에서 보시려면 대기 로그의 `created_at` 을 6일 전쯤으로 당기시면
> 됩니다 — 필요하시면 그 상태로 심어 드리겠습니다.

**쓰는 법**

```
보낼 수 있는 사람 수 = people.waiting − waiting_window_risk.people
                     = 57 − 3 = 54
```

→ "제한이 풀리면 **54명**에게 순서대로 발송합니다" 라고 쓰시면 **지킬 수 있는 약속**이 됩니다.

- `people` 은 `people.waiting` 과 **같은 모수**(첫 DM 기준)로 계산했습니다. 그래서 그냥 빼면
  됩니다. 2번째 DM 을 섞지 않은 이유는, 그쪽은 애초에 `people` 블록에 안 들어가서 섞으면
  뺄셈이 음수로 갈 수 있기 때문입니다.
- `followup_events` 가 0 보다 크면 "자료 DM 일부는 시간이 지나 다시 신청이 필요할 수
  있어요" 같은 별도 안내를 붙이실 수 있습니다. 이번 사건에서 고객이 가장 아쉬워한 부분이
  정확히 이겁니다.
- 정지가 아니면 전부 0 입니다(`horizon_s`=0).
- 판정은 실제 종결 로직과 **같은 상수**를 씁니다 — 예고와 실제가 갈리지 않게 테스트로
  묶어 뒀습니다.
- 한계: 게이트 버튼 재탭으로 창이 새로 열린 건은 반영하지 않아 **위험을 조금 과대평가**할
  수 있습니다(살아날 건까지 셈). 과소평가보다 안전한 쪽으로 뒀습니다.

---

## 5. 응답 계약 변경 요약

| 위치 | 필드 | 변화 |
|---|---|---|
| `dm-verification/` (로그 목록·상세) | `frontend_action.user_reason` | `account_send_paused` 값 추가 |
| 〃 | `frontend_action.severity` | 정지 중 `queued` 는 `info` → `warning` |
| `dm-verification/queue-state/` | `waiting_window_risk` | **신규 객체** |
| `admin/auto-dm/logs/` | `user_view.*` | 위와 동일(같은 함수) |

기존 필드는 하나도 제거·변경하지 않았습니다. 마이그레이션 없습니다.

---

## 6. 남은 것 / 저희 쪽 후속

- **정지 시작 알림이 아직 없습니다.** 이번 CS 의 실질 원인입니다 — 고객은 "제한 걸림"이
  아니라 "자동화 고장"으로 인식했습니다. 요청서 §4 의 "첫 진입 1회 팝업" 이 그 자리를 메워
  주는데, 앱을 안 여는 동안은 여전히 공백입니다. 메일/알림은 저희 쪽 별도 과제로 잡았습니다.
- **2번째 DM 창 소실**(§4-c)의 구조적 해소도 별도 과제입니다.
- 조기 해제는 말씀대로 CS 티켓 경로로 받겠습니다. 운영에서 `--release` 한 줄이면 됩니다.

문의나 값이 더 필요하시면 말씀해 주세요.
