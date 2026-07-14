# DM 순차 발송 큐 현황 (게이지 + ETA) — 프론트엔드 연동 가이드 (v4.3 / v4.4)

> 2026-07-09 백엔드 대규모 패치와 함께 배포. 문의: 백엔드팀.
> **v4.4 (2026-07-14)**: 사람(수신자) 단위 `people` 블록 추가 — "전체 대상 N**명**" 표기는
> 이제 `gauge`(이벤트 단위)가 아니라 `people` 을 쓰세요. §3.5 참고.

## 0. 발송 메커니즘이 바뀌었습니다 (배경)

Instagram/Meta 정책 제약 때문에 DM 은 계정당 순차 발송됩니다. v4.3 부터 방식이 바뀌었습니다:

| | 이전 (v4.2 이하) | **현재 (v4.3)** |
|---|---|---|
| 방식 | 시간당 캡(700) 소진 → 다음 정시까지 일괄 대기 | **한 건씩 랜덤 간격으로 스무스 발송** |
| 오프닝 DM (댓글 트리거) | 시간당 최대 700건 몰아서 | **평균 ~5.0초/건** (3~7초 랜덤) |
| 리워드/재안내/스토리답장 | 같은 캡 공유 | **평균 ~2초/건** (1~3초 랜덤) — 별도 트랙 |
| 계정 시간당 백스톱(안전) | — | **740** (Meta 750 아래, 페이서 자연율 720 위) |
| 캠페인별 시간당 200건 설정 | 강제됨 | **폐기(deprecated)** — 값은 받되 무시 |

**UX 함의**: "정시에 몰아서 나가고 그 사이 침묵"이 아니라, 댓글이 몰려도 **일정한 속도로 줄 서서
계속 나가는** 모델입니다. 그래서 "대기 N건 / 예상 완료 시각" 게이지가 정확하게 제공됩니다.

- `max_sends_per_hour` 필드: 생성/수정 API 에서 계속 **받아주지만 무시**됩니다 (400 안 남).
  설정 UI 에서 이 입력은 **제거하거나 "자동 조절" 안내로 대체**하세요.
- 캠페인 응답의 `can_send`: 이제 "캠페인 활성 여부"만 의미 (시간당 한도 개념 없음).

## 1. 엔드포인트

```
GET /api/v1/integrations/dm-verification/queue-state/?campaign_id=<uuid>
GET /api/v1/integrations/dm-verification/queue-state/?ig_connection_id=<uuid>
```

- **둘 중 정확히 1개** 필수 (0개/2개 → 400).
- 인증: Bearer JWT + 워크스페이스 멤버십 (403/404).
- Swagger: `/api/docs/` → `DM Verification` → `queue-state`.

## 2. 응답 예시

```json
{
  "scope": "campaign",
  "campaign_id": "b1e2c3d4-…",
  "ig_connection_id": "a0c1b2d3-…",
  "external_account_id": "17841400000000000",
  "ig_username": "turnflow_official",
  "gauge": { "sent": 512, "waiting": 138, "in_flight": 2, "failed": 4, "total": 652 },
  "people": { "total": 420, "sent": 330, "waiting": 86, "failed": 4, "processed": 334 },
  "pacing": {
    "private_reply_avg_gap_s": 5.0,
    "send_api_avg_gap_s": 2.0,
    "hourly_backstop_cap": 700
  },
  "account_waiting": 190,
  "ahead_of_this_campaign": 52,
  "blocking_reason": null,
  "action_block_cooldown_seconds": 0,
  "eta_seconds": 1045.0,
  "eta_finish_at": "2026-07-09T13:25:40+09:00",
  "eta_is_estimate": false,
  "generated_at": "2026-07-09T13:08:15+09:00"
}
```

## 3. 게이지 UI 구성

- **게이지 = `gauge.sent / gauge.total`** (`total = sent + waiting + in_flight`).
  정상적으로 큐가 다 빠지면 100% 도달.
- `failed` 는 분모에 **포함되지 않음** — 원하면 빨간 별도 세그먼트/뱃지로.
- `in_flight` 는 순간값(발송 API 호출 중) — sent 쪽 색으로 묶어도 무방.
- **카운트다운**: `eta_seconds` 사용. `generated_at` 기준으로 클라이언트에서 초 단위 보간.
  - `eta_is_estimate=true` → **"약 12분"** 처럼 근사 표기.
  - `eta_is_estimate=false` → 확정 슬롯 기반이라 그대로 표기 가능.
  - `eta_seconds=0` → "대기 없음 / 모두 발송됨".

## 3.5 사람 단위 게이지 `people` (v4.4) — "N명" 표기는 반드시 이걸로

`gauge` 는 **발송 이벤트(로그) 단위**입니다. 팔로우게이트 캠페인은 1명에게 오프닝+리워드
(+재안내) 여러 건이 나가므로, `gauge` 수치에 "명"을 붙이면 사람 수의 ~2배로 보입니다
(실측: 대상 802명 캠페인이 "전체 대상 1,256명"으로 표기된 사례).

`people` 은 **루트 DM(오프닝/단독 — 리워드·재안내 제외) 기준으로 수신자를 중복 제거한
사람 수**입니다. 한 사람이 댓글을 2번 달아 오프닝이 2건 나가도 1명으로 셉니다.

| 필드 | 의미 |
|---|---|
| `people.total` | 전체 대상 사람 수 (실패 포함, = sent+waiting+failed) |
| `people.sent` | DM 이 실제 발송된 사람 (Meta 접수 이상) |
| `people.waiting` | 발송 차례 대기/발송 중인 사람 |
| `people.failed` | 아무것도 받지 못하고 종결·정체된 사람 (하드실패·복구 대기/만료·한도 스킵) |
| `people.processed` | 처리 완료 = sent + failed (진행바 분자) |

- **진행바**: `people.processed / people.total`, 헤드라인 "처리 완료 {processed}명".
- **ETA·발송중 판정은 기존 `gauge`/`eta_*` 그대로** 사용하세요 (페이서 큐는 이벤트 단위로
  돌기 때문에 남은 시간은 이벤트 수가 정확합니다). 드물게 진행바가 100%인데 ETA 가 잠깐
  남을 수 있습니다(이미 받은 사람에게 가는 리워드/2번째 DM 잔여분) — 정상입니다.
- `people.failed` 는 "확인 필요" 성격의 수치입니다. stats 의 `unique_failed` 와 **동일 정의**
  (루트 DM 기준 사람 수)입니다. 단, **수치가 정확히 같으려면 집계 구간이 같아야** 합니다 —
  queue-state 의 `people` 은 **전 기간**, stats 는 기본 **최근 30일**(`?since=` 로 조정)이라,
  30일을 넘겨 운영한 캠페인은 두 화면이 어긋날 수 있습니다. 같은 화면에서 두 값을 나란히
  비교한다면 stats 를 `?since=` 로 캠페인 시작일에 맞추세요.

## 4. blocking_reason 문구 매핑

| 값 | 권장 문구 |
|---|---|
| `null` | (정상 — 표시 없음, 게이지만) |
| `action_block_cooldown` | "Instagram 이 일시적으로 발송을 제한했어요. {action_block_cooldown_seconds 상대시간} 후 자동 재개됩니다." (ETA 에 이미 반영됨) |
| `monthly_quota_reached` | "이번 달 DM 한도에 도달했어요. 플랜 업그레이드 시 이어서 발송됩니다." |

## 5. 다중 캠페인 안내

발송 대기열은 **계정 단위 공유**입니다. 같은 IG 계정에 캠페인이 여러 개면:
- `account_waiting`: 계정 전체 대기 수
- `ahead_of_this_campaign` > 0 → "다른 캠페인 대기 {N}건이 먼저 발송돼요" 안내 노출 권장.
- 캠페인 스코프의 `eta_*` 는 **타 캠페인 선행분을 이미 반영**한 값입니다.

## 6. 폴링

- **5~10초 간격** 권장 (폴링 주기는 UX 취향 — 발송 간격과 무관하게 프론트가 정함).
- **5초 미만 금지**: queue-state 는 응답 캐시가 없어 매 호출이 DB 집계 → 대량 계정에서 부하.
- 카운트다운은 `eta_seconds`를 `generated_at` 기준으로 클라이언트에서 보간(폴링 사이 초 감소) → 폴링이 잦지 않아도 매끄러움.
- 활성 캠페인 상세 화면에서만 폴링, 이탈 시 중단.

## 6.5 캠페인 일시중지 / 삭제 시 (v4.3 Fix)

- **일시중지(pause)**: 이제 **대기중이던 DM도 즉시 멈춥니다.** 새 댓글은 물론, 이미 대기열에 있던
  DM도 발송되지 않고 `SKIPPED` 처리됩니다(재개 시 되살아날 수 있음). → 게이지의 `waiting`이
  곧 0으로 떨어지고 ETA도 0이 됩니다. UI: 일시중지 직후 게이지가 "대기 0"으로 정리되는 게 정상.
- **삭제(delete)**: 대기중 DM 전부 함께 삭제되어 발송되지 않습니다(캠페인 조회는 404).
- 두 경우 모두 그 캠페인이 잡고 있던 발송 슬롯은 백그라운드에서 자동 회수되어(최대 ~1분),
  같은 계정의 **다른 캠페인이 그만큼 빨리 발송**됩니다. 삭제/중지 직후 다른 캠페인 게이지의
  ETA가 앞당겨질 수 있습니다(폴링으로 자연 반영).

## 7. 참고 — 대략의 처리 속도 감

- 오프닝(댓글 트리거): 시간당 ~720건 (Meta 사설답장 한도 750/hr 아래로 자동 유지)
- 리워드/재안내/스토리답장: 시간당 ~1,800건 (Meta 별도 트랙 — 사실상 병목 아님)
- 예: 댓글 1,000개 몰림 → 오프닝 전량 발송에 약 1.4시간. 게이지가 이걸 그대로 보여줍니다.

---

# v4.5 (2026-07-14) — 통계 헤드라인 정정 · '숨겨진 요청 · 스팸' 분리 · 상태 그룹

> DM 분석(통계) 화면과 캠페인 DM 로그 리스트의 상태 표기·필터가 바뀝니다.
> 백엔드는 **추가·정정만** 했고 기존 필드는 유지됩니다(하위호환). 문의: 백엔드팀.

## A. 통계 헤드라인 "N% 전송" (100% 오표기 → 실제 전송률)

`GET /api/v1/integrations/dm-verification/stats/?campaign_id=<uuid>`

- 헤드라인 퍼센트는 **`unique_sent_rate`** (신규, = `unique_sent / unique_targets`)를 쓰세요.
  기존에 쓰던 `delivery_rate` 는 Meta **접수건만 분모**라 하드실패가 빠져 **100%로 부풀어** 보입니다.
- 헤드라인 문구 예시(권장):
  - 큰 숫자: `unique_sent_rate` → "**84.2%** 메시지가 성공적으로 전송됐어요"
  - 보조 문구: "DM 요청 댓글 **{unique_targets}**개 중 **{unique_sent}**개가 전송됐어요 ·
    **{unique_targets − unique_sent}**명은 아직 받지 못했어요"
  - (예: 827개 중 696개 전송 · 131명 미수신)

## B. 카드: '확인 필요' → '숨겨진 요청 · 스팸' 분리 (+ CTR 위치 스왑)

신규 필드 (모두 사람 단위, `unique_failed` 의 하위 분해):

| 필드 | 의미 | 카드 |
|---|---|---|
| `unique_hidden_spam` | **숨겨진 요청 · 스팸** 인원 (비팔로워 채널 미개설로 숨김함行) | 신규 카드(구 '확인 필요' 자리) |
| `unique_needs_attention_excl_hidden` | 숨김함 뺀 '확인 필요' 인원 | 새 '확인 필요' 카드(다른 위치) |
| `unique_needs_attention` | 기존 '확인 필요' 총합(= failed + unconfirmed) | 하위호환·필요 시 |

- 항등: `unique_needs_attention_excl_hidden = unique_needs_attention − unique_hidden_spam` (≥ 0).
- **카드 위치**: 기존 '확인 필요'(131) 자리에는 **`unique_hidden_spam`(숨겨진 요청 · 스팸)** 을 표시,
  **`unique_needs_attention_excl_hidden`** 는 다른 위치로 이동. 그리고 **CTR 카드와 '숨겨진 요청 · 스팸'
  카드 위치를 스왑**합니다(순수 레이아웃 — 백엔드 값은 그대로).

## C. DM 로그 리스트 상태 — `status_group` 단일 소스 (프론트 클라 분류 제거)

수신자(사람) 단위 리스트 `GET .../dm-verification/recipients/?campaign_id=<uuid>` 의 각 행에
아래 필드가 추가됩니다. **상태 배지/탭은 이제 `status_group` 하나로 그리세요.**
(sent/delivered/read 불리언을 조합해 직접 분류하던 로직 제거)

| status_group | 표시명(`status_group_display`) | 비고 |
|---|---|---|
| `waiting` | **대기중** | 구 "순차발송" — 명칭 변경 |
| `sent` | 전송됨 | Meta 접수·도착·복구 성공 |
| `read` | 읽음 | |
| `hidden_spam` | **숨겨진 요청 · 스팸** | 숨김함行. `is_recovering=true` 면 "복구 대기" 보조 칩 추가 |
| `attention` | 확인 필요 | 숨김함 제외한 나머지 실패 |

- `is_recovering` (bool): 복구 대기 중 → 배지를 **"숨겨진 요청 · 스팸" + "복구 대기"** 2개로.
  복구 OFF(만료 포함)면 false → **"숨겨진 요청 · 스팸"** 만.
- **서버 필터**: `?status_group=waiting|sent|read|hidden_spam|attention` (기본 all). 각 사람은
  정확히 1개 그룹이라 **탭 카운트가 total 로 분할**됩니다. `recipient_username` 부분검색과 조합 가능.
  (구 `category` 파라미터는 하위호환 유지되나 신규는 `status_group` 사용)
- 이벤트 단위 목록 `GET .../dm-verification/?...` 에도 동일한 `status_group`/`status_group_display`/
  `is_recovering` 필드 + `?status_group=` 필터가 추가됐습니다.

## D. 버그 정정 — 복구 완료가 상태에 반영됨 (needs_attention success-aware)

- 예전에는 복구/후속 도착이 끝났어도 **과거 실패 로그가 남아** 사람 단위 행이 계속 "확인 필요"로
  보였습니다. 이제 발송/도착/읽음/복구 성공이 하나라도 있으면 `status_group` 은 `sent`/`read`,
  `needs_attention` 은 `false` 가 됩니다. → **복구 완료 유저는 전송됨/읽음으로 정상 표기.**
- 따라서 배지는 반드시 `status_group` 기준으로 그리세요(불리언 조합/`needs_attention` 단독 판정 금지).

## E. 필드 정의 (요약)

- `unique_targets` 전체 대상 · `unique_sent` 전송(Meta 접수+) · `unique_read` 읽음
- `unique_sent_rate` = sent/targets (헤드라인) · `unique_reach_rate` = delivered/targets (도착률)
- `unique_hidden_spam` ⊆ `unique_failed` · `unique_needs_attention(_excl_hidden)`
- `ctr` / `ctr_basis` 는 그대로 (스왑은 레이아웃만)
