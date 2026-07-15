# 자동 DM 발송 라이프사이클 — 웹훅 수신부터 발송 확정·실패 처리까지

> **목적**: 캠페인(AutoDMCampaign) 기반 자동 DM이 **① 인스타그램 웹훅 수신 → ② 트리거 매칭/디스패치 → ③ 실제 발송 → ④ "보내졌음" 확정 → ⑤ 실패 시 처리**되는 전 과정을 한 문서로 정리한다.
> **범위**: `apps/integrations` (웹훅·캠페인·DM 발송), Meta Instagram Graph API **v25.0**, 에러 분류 **v3.2**, 발송 보증 **v3.10(무손실 하드닝)**.
> **핵심 키워드**: `SentDMLog` 상태머신 / `EventInbox` 멱등성 / echo 웹훅 + Conversations API 2중 검증 / 35분 cutoff / **rate_governor 계정별 안전속도** / **defer-not-drop** / **메시징 윈도우 만료 단일 종결** / **SeenComment 댓글 누락 보정** / **revive 제자리 되살림** / **Action Block 쿨다운** / **웹훅 HMAC**.
>
> **v3.9 기반 (요약)**
> 1. **rate-limit/transient은 "드랍·5회 후 사망"이 아니라 무한 defer**(QUEUED+`next_retry_at`) → `requeue_deferred_dms`가 FIFO 재투입. 유일한 graceful 종결 = **메시징 윈도우 만료**(comment 7일 / user_id 24h)뿐(`send_dm_task` 진입부 age 가드 단일 지점).
> 2. **시간당 한도 초과 = 드랍이 아니라 지연**. `rate_governor`(계정당 750/hr Private Reply 안전마진 + 분당 버스트)를 `send_dm_task`에 연결. enqueue 단계의 `SKIPPED` 드랍 제거.
> 3. **능동 조회 폴링 축소**(5→10분, [10·35분] 2회) — stuck 1건당 GET ~30회 → ~2회.
> 4. **`FAILED_NO_TRACE`는 '실패'가 아니라 '미확인'**(`total_unconfirmed`) — `success_rate`/`total_failed`와 분리 집계.
> 5. **댓글 웹훅 누락 보정**: `SeenComment` 장부 + `poll_missed_comments`(매시간) — 웹훅이 유실돼도 1시간 내 자동 발송(§9).
>
> **v3.10 무손실 하드닝 (요약 — 상세 [§11](#11-무손실-하드닝-v310))**
> 1. **제자리 되살림(revive)**: `FAILED_TOKEN`/`SKIPPED` 종결 건을 **같은 row·같은 idempotency_key 로** QUEUED 복귀 → 중복키로 영영 재발송 못 하던 영구 손실 해소. 토큰 갱신 성공 시 자동 되살림 + 프리미엄 수동 `retry-failed` API.
> 2. **Action Block 서킷 브레이커**: Meta `code 368` 등 차단 신호 시 그 계정 발송을 **에스컬레이팅 쿨다운**(24h→×2, 상한 7일)으로 일시정지 후 자동 재개 — 차단 중 재시도로 차단이 연장되는 것을 방지.
> 3. **웹훅 HMAC 검증**(`X-Hub-Signature-256`) — 위조 페이로드 차단(`WEBHOOK_HMAC_ENFORCED`).
> 4. **검증 후 재발송**: 200-no-msgid anomaly·SUBMITTING 크래시는 Conversations 조회로 '이미 보냈는지' 확인 후 재발송 → 중복 방지.
> 5. **거버너 fail-closed**: Redis flush 감지 시 그 시각 동안 차단(과발송·밴 방지). **백로그 모니터링** Admin API + Telegram 경고. **재연동 고아화** 방지(unique 제약). **위험고지** 필드(any_media/story_reply).
>
> 이 문서는 코드를 직접 대조해 작성했으며, 사실 검증을 거친 **알려진 결함**은 [§8](#8-알려진-갭--주의사항)에 별도로 정리한다.

---

## 0. 한눈에 보기 — 전체 파이프라인

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Instagram (Meta) ──webhook POST──▶  /api/v1/integrations/instagram/webhook/  │
│                                       instagram_webhook (views.py)            │
└─────────────────────────────────────────────────────────────────────────────┘
                │ payload.object == "instagram"
        ┌───────┴────────────────────────────────┐
        ▼                                         ▼
  changes[].field=="comments"            entry[].messaging[]  (echo / read / postback / story)
        │ delay()                               │
        ▼                                        │ EventInbox 멱등 INSERT ("{type}:{mid}")
  process_comment_and_send_dm  ◀── (Celery)      │ created=True 1회만 → process_messaging_event
        │                                        ▼
        │ ① parent_id 있으면 skip(is_reply)   echo  → SentDMLog.mark_delivered(via=echo)  → DELIVERED
        │ ② self-comment skip                 read  → SentDMLog.mark_read()              → READ
        │ ③ 스팸 게이트                        postback "fg:{log_id}" → follow-gate 분기
        ▼
  AutoDMCampaign 매칭 (active + 예약창 + media + keyword)
        │ 매칭된 캠페인마다 1건씩
        ▼
  _enqueue_send_dm  ──▶  SentDMLog(status=QUEUED) INSERT (idempotency_key UNIQUE)
        │ send_dm_task.delay(log.id)
        ▼
┌──────────────────────────── send_dm_task (Celery, dm_send 큐) ───────────────────────┐
│  ① status QUEUED/SUBMITTING 아니면 skip(멱등)                                          │
│  ② 예약창(is_within_schedule) 밖 → SKIPPED  ③ ig_conn ACTIVE 아님 → FAILED_TOKEN       │
│  ④ ★메시징 윈도우 age 가드: created_at+7d(comment)/24h(user_id) 경과 → FAILED_WINDOW    │
│  ⑤ ★_rate_defer: 캠페인 시간당 한도 / 계정 거버너(750/hr) 초과 → QUEUED+next_retry_at   │
│     (드랍 아님 — requeue_deferred_dms 가 FIFO 재투입)                                   │
│  ⑥ mark_submitting → comment_id&parent없음 → Private Reply / 그 외 → user_id(24h)       │
│         ▼  _post_message → Meta Graph API POST                                         │
│   ┌─────────────┬───────────────────────────────────────────────────────┐            │
│   │ 200 + msgid │  4xx/5xx (에러) → _defer_or_fail                         │            │
│   │ +recipient  │     │                                                    │            │
│   │   ▼         │     ▼  classify → DMSendError 하위                       │            │
│   │ mark_accepted     window/token/param/551/transient                     │            │
│   │ (ACCEPTED)  │   retriable? ─ yes ─▶ ★defer(QUEUED+next_retry_at, 무한)  │            │
│   │   │         │     └── no ──▶ token/param/window→FAILED · 551·기타4xx→   │            │
│   │   ▼         │                  FAILED_NO_TRACE(=미확인, unconfirmed)    │            │
│   │ +10분 verify_dm_delivery 예약, public_reply 예약(best-effort)           │            │
│   └─────────────┴───────────────────────────────────────────────────────┘            │
└────────────────────────────────────────────────────────────────────────────────────┘
                │
                ▼  "발송 확정"은 2단계
   ① 접수 확정(ACCEPTED): Meta 200 + message_id + recipient_id
   ② 도착 확정(DELIVERED): echo 웹훅(1차)  OR  GET /{message_id} 능동 조회(2차 안전망)
                │
        echo/조회 모두 35분간 미확인 ──▶ FAILED_NO_TRACE (미확인=total_unconfirmed, 자가 점검 안내)

  [Beat 안전망]  reconcile_accepted_dms(60s)·reconcile_stuck_submitting(30s)·
                 requeue_deferred_dms(30s, defer 재투입)·dead_letter_alerter(10m)·
                 enforce_campaign_schedules(60s)·poll_missed_comments(1h, 댓글 누락 보정)·
                 cleanup_comment_ledger(daily)
```

**상태 전이 요약**: `QUEUED → SUBMITTING → ACCEPTED → DELIVERED → READ`
실패/지연 분기: `→ FAILED_TOKEN / FAILED_WINDOW / FAILED_PARAM / FAILED_NO_TRACE(미확인) / RATE_LIMITED·QUEUED(무한 defer) / SKIPPED`
> ⚠️ **v3.9부터 유일한 graceful 종결은 메시징 윈도우 만료(FAILED_WINDOW)뿐.** rate-limit/transient은 횟수 상한으로 죽지 않고 윈도우가 닫힐 때까지 계속 defer된다.

---

## 1. 웹훅 수신 & 멱등성

### 1.1 엔드포인트
- 단일 함수형 뷰 `instagram_webhook`이 GET/POST를 모두 처리 (`apps/integrations/views.py:3501`).
- 최종 경로: **`/api/v1/integrations/instagram/webhook/`** (`apps/integrations/urls.py` + `config/api_urls.py`의 `integrations/` prefix).
- DRF `@api_view(["GET", "POST"])` + `@permission_classes([AllowAny])` — 인증 없이 외부에서 호출.

### 1.2 GET — 구독 검증(핸드셰이크)
- `hub.mode`, `hub.verify_token`, `hub.challenge`를 읽어, `settings.INSTAGRAM_WEBHOOK_VERIFY_TOKEN`과 문자열 비교.
- `mode == "subscribe"` AND 토큰 일치 → `hub.challenge`를 `text/plain` 200으로 반환, 아니면 **403 "Forbidden"**.
- ⚠️ 토큰 비교가 일반 `==` (타이밍-세이프 비교 아님). `verify_token`은 **구독 검증 전용**이며 이벤트 인증 수단이 아니다.

### 1.3 POST — 이벤트 수신
- `json.loads(request.body)`로 파싱. `payload["object"] != "instagram"`이면 처리하지 않고 `EVENT_RECEIVED` 200 반환.
- 응답 규약: 정상 **200 `EVENT_RECEIVED`** / JSON 파싱 실패 **400** / 그 외 예외 **500**. (Meta가 200을 못 받으면 재전송하므로, 처리 실패와 무관하게 빠르게 200을 돌려주고 실제 작업은 Celery로 넘긴다.)
- `entry[].changes[]`를 순회:
  - `field == "comments"` → `webhook_data`(field/value/entry_id/time) 구성 후 **`process_comment_and_send_dm.delay()`** 비동기 enqueue.
  - `field in ["mentions", "messaging_postbacks"]` → 현재 **로깅만** 하고 미처리.
- 각 `entry`마다 `_process_messaging_events(entry)`를 추가 호출하여 `entry.messaging[]`(echo/read/postback/inbound)을 처리.

### 1.4 메시징 이벤트 분기 (`_process_messaging_events`, views.py:3313~)
| 신호 | 조건 | 처리 |
|---|---|---|
| **echo** | `message.is_echo == true` 또는 `sender.id == 페이지 IG user id` | `EventInbox("echo:{mid}")` 멱등 INSERT → `process_messaging_event` → SentDMLog **DELIVERED** 승격 |
| **read** | `read.mid` 존재 | `EventInbox("read:{mid}")` → SentDMLog **READ** 승격 |
| **postback** | payload가 `fg:{log_id}` | follow-gate 라우팅(`process_follow_gate_postback`) |
| **inbound(스토리 답장)** | `reply_to.story.id` 존재 | `process_story_reply_and_send_dm.delay` |
| inbound(일반 DM) | 그 외 | 로깅만 |

### 1.5 멱등성(중복 방어)

**(A) echo/read 메시징 이벤트 — `EventInbox` 2중 방어** (`apps/integrations/models.py:1288`)
- `event_key = "{event_type}:{mid}"` (예: `"echo:abc123"`), DB **UNIQUE**.
- `get_or_create()` INSERT → IntegrityError(동시 INSERT 레이스 패자)는 흡수하고, `created=True`인 **최초 1회만** 후속 태스크 enqueue.
- 소비 태스크 `process_messaging_event`는 `evt.processed_at`이 이미 있으면 `already_processed`로 즉시 종료(2차 방어). 처리 완료 후 `processed_at = now()` 기록.
- 도입 배경: 3-tier 전환으로 웹훅 동시성이 올라가면 echo/read가 여러 워커에 동시 도달해 같은 `SentDMLog` row를 락 없이 UPDATE하는 레이스가 발생 → `EventInbox`로 "최초 1회"만 직렬화.

**(B) comments 트리거 이벤트 — `SentDMLog.idempotency_key`**
- `EventInbox`를 쓰지 않는다. 대신 발송 단계에서 `idempotency_key = sha256(workspace_id : ig_user_id : comment_id : campaign_id)` UNIQUE 제약으로 **같은 댓글 + 같은 캠페인의 중복 발송**을 차단(§2.4).
- 추가로 `parent_id`(대댓글) skip, self-comment skip, 동일 수신자 60초 쿨다운 가드가 보조한다.

> **롤백 스위치**: `settings.WEBHOOK_ASYNC_MESSAGING`(기본 `True`)이 `False`면 EventInbox/Celery를 건너뛰고 웹훅 스레드에서 동기로 echo/read를 직접 처리한다. 이 레거시 inline 경로엔 멱등 가드가 없다.

---

## 2. 트리거 매칭 & 디스패치

진입: `process_comment_and_send_dm` (`apps/integrations/tasks.py:51~`)

### 2.1 댓글 사전 가드
1. `comment_id / from_user_id / from_username / media_id` 중 하나라도 없으면 `error` 반환.
2. **`parent_id`가 있으면 무조건 `skip = is_reply`** — 대댓글(우리가 단 공개답글이 다시 webhook으로 재유입되는 경우 포함) 차단 → **DM 무한루프 방지**, top-level 댓글만 트리거.
3. `from_user_id == entry_id`(비즈니스 본인이 자기 글에 댓글) → `skip = self_comment`.

### 2.2 스팸 게이트 (`_check_and_handle_spam`)
- `media_id`로 캠페인을 찾아 해당 `IGAccountConnection`의 `SpamFilterConfig` 조회. 필터가 없거나 비활성이면 통과.
- `SpamDetectionService.is_spam()`: URL 정규식 + 키워드 부분일치(기본 키워드 `['아이돌','주소창','사건','원본영상','실시간검색']`).
- 스팸이면 `SpamCommentLog(status=DETECTED)` 기록 + 댓글 숨김(`hide_comment`) 후 **캠페인 매칭 전에 종료**(DM 미발송).
- ⚠️ 스팸 게이트는 `media_id`로 캠페인 1건만 찾으므로 `any_media`/`next_media`(media_id=='') 트리거 댓글에는 적용되지 않을 수 있음.

### 2.3 캠페인 매칭
- 후보 쿼리: `AutoDMCampaign.filter(status=ACTIVE).filter(예약창)` + 같은 IG 계정(`ig_connection.external_account_id == entry_id`) 소유 캠페인.
- 캠페인별 `matches_media(media_id) AND matches_keyword(comment_text)` 모두 True여야 매칭.

| 항목 | 값 | 의미 |
|---|---|---|
| `TriggerType` | `specific_media` / `any_media` / `next_media` / `story_reply` | 트리거 모드. `next_media`는 활성 후 첫 신규 게시물에 webhook으로 attach되며 `specific_media`로 전환 |
| `KeywordMode` | `any`(기본) / `all` / `exact` | `any`=하나라도 부분포함, `all`=모두 부분포함, `exact`=댓글 전체가 키워드와 정확히 일치. 모두 소문자 비교. `keyword_filter`가 비면 전체 매칭 |

### 2.4 디스패치 (`_enqueue_send_dm`, tasks.py:459~)
- **매칭된 캠페인마다 각각** `_enqueue_send_dm` 호출 → 한 댓글이 N개 캠페인에 매칭되면 **N개 DM**이 발송됨(캠페인 단위 독립 발송, 의도된 동작).
- 발송 전 가드(순서대로):
  1. `is_runnable_now() == False` → `skip = outside_schedule_window` (TOCTOU 안전망, 로그도 안 남김).
  2. self-comment → `skip = self_comment`.
  3. 같은 캠페인+수신자로 **60초 내** 발송 로그 존재 → `skip = recipient_cooldown_60s` (같은 사람의 단시간 다중 댓글 차단. `idempotency_key`는 comment_id별로 달라 이걸로는 못 막으므로 별도 가드).
  4. `idempotency_key` 계산.
  5. follow-gate 사용 캠페인이면 `dm_kind=OPENING / gate_status=PENDING`, 아니면 `STANDALONE / NONE`.
  6. `transaction.atomic` 안에서 **`SentDMLog(status=QUEUED)` INSERT** → IntegrityError(중복 `idempotency_key`)면 `duplicate` 반환.
  7. `send_dm_task.delay(log.id)`.

> **v3.9 변경**: 시간당 한도/계정 거버너 평가는 **enqueue가 아니라 `send_dm_task` 단일 지점**으로 이동했다. 과거엔 한도 초과 시 `SentDMLog(SKIPPED)`로 **드랍**했지만, 이제는 항상 `QUEUED`로 적재하고 발송 직전에 초과 판정되면 **defer**한다(드랍 없음). `can_send_more()`도 `QUEUED`를 세지 않도록 고쳐 "큐에 쌓인 것이 한도를 먹어 영원히 못 나가는" 데드락을 방지한다.

---

## 3. DM 발송 실행

태스크: `send_dm_task` (`apps/integrations/tasks.py:590`, `@shared_task(bind=True, max_retries=5)`, `dm_send` 큐)
서비스: `InstagramMessagingService` (`apps/integrations/services.py:436`, `GRAPH_API_BASE = .../v25.0`, `DEFAULT_TIMEOUT = 10s`)

### 3.1 발송 직전 체크포인트 (send_dm_task) — v3.9 순서
1. `log.status`가 `QUEUED/SUBMITTING`이 아니면 skip(멱등).
2. **`campaign.is_within_schedule() == False` → `mark_skipped`** — opening/reward/follow재안내/reconcile재큐/수동재시도 **모든 발송 경로가 거치는 단일 권위 차단점**.
3. `ig_conn.status != ACTIVE` → `mark_failed(FAILED_TOKEN)` + `increment_failed`.
4. **★ 메시징 윈도우 age 가드(graceful 종결의 단일 지점)**: `now - created_at >= _messaging_window(log)` (comment Private Reply 7일 / user_id DM 24h)면 `mark_failed(FAILED_WINDOW)`. rate-limit으로 아무리 오래 defer돼도 Meta가 어차피 거부할 시점이 오면 여기서만 종결한다.
5. **★ 발송 속도 제어 `_rate_defer`(재진입 포함)**: ⓐ `campaign.can_send_more()==False`(캠페인 시간당 `max_sends_per_hour`) → 300초 후 재평가. ⓑ `DM_GOVERNOR_ENABLED`면 `rate_governor.check(ig_account, plan)` — 계정당 **750/hr(안전마진 700) Private Reply + 분당 버스트** 초과 시 다음 시각/분 경계까지 defer. 어느 쪽이든 `status=QUEUED + next_retry_at` 기록 후 반환(**드랍/실패 아님**).
6. `mark_submitting()` → 상태 `SUBMITTING`, `submitted_at` 기록.

### 3.2 메시지 종류·버튼 결정
- **OPENING + gate PENDING** → postback 버튼 `[{type:postback, title:follow_gate_button_label, payload:'fg:{log_id}'}]`.
- **STANDALONE / REWARD** → `campaign.get_link_buttons()`(web_url 링크 버튼, **최대 3개**).
  - `get_link_buttons()`는 `campaign.link_buttons`(list, `[{"url","label"}]`) 에 유효 항목이 있으면 그것을 우선 사용하고, 비어있으면 legacy `link_button_url`/`link_button_label`(단일)로 fallback 한다. url 은 http/https 만, label 은 20자로 잘리며, 최대 3개(초과분 무시).
- 버튼이 있으면 **button template**(text 640자 + web_url/postback 버튼 1~3개)으로, 없으면 plain text(+quick_replies)로 전송한다. **버튼 개수(1~3)는 640자 본문 한도에 영향 없음**(버튼 title 은 개당 20자 별도 한도). quick_replies 최대 13개.

### 3.2.1 공개 답글(대댓글) 상한
- `public_reply_enabled` 캠페인은 DM 성공 시 댓글에 공개 답글을 게시한다(`post_public_reply`, best-effort).
- **누적 상한** `public_reply_limit`(기본 200, 0=무제한): `post_public_reply`(비복구)는 배치 스로틀·API 호출 **전에** `campaign.public_reply_limit_reached()`를 검사하고, 도달했으면 `verification_log`에 `limit_skipped`를 남기고 게시하지 않는다(로그는 failed 로 만들지 않음 — DM 은 이미 발송됨). `send_dm_task` 의 enqueue 지점에도 동일 프리체크가 있어 무의미한 태스크 적재를 막는다(best-effort).
- 성공 게시 시에만 `campaign.increment_public_reply_posted()`로 `public_reply_posted_count`를 원자적 증가한다.
- **복구 안내 대댓글(recovery=True)은 이 상한과 무관** — 항상 게시되고 카운트되지 않는다(배치 윈도우만 `public_reply_posted_at`로 공유).

### 3.3 실제 API 호출 (2가지 경로)
| 함수 | 엔드포인트 | recipient | 사용 시점 |
|---|---|---|---|
| `send_dm_via_comment` | `POST /{ig_user_id}/messages` | `{"comment_id": ...}` | **Private Reply** — `log.comment_id`가 있을 때(댓글 트리거). 댓글 7일 이내 |
| `send_dm_via_user_id` | `POST /{ig_user_id}/messages` | `{"id": recipient_id}` | **24h 윈도우** — comment_id가 없을 때(스토리 답장·reward DM) |

- **24시간 메시징 윈도우 정책**은 별도 사전 검증 레이어가 아니라, `send_dm_via_user_id` 사용 시 Meta가 윈도우 밖이면 `code 10 / subcode 2534022·2018278`로 거절하고 이를 `FAILED_WINDOW`로 분류하는 **사후 분류** 방식이다(§5). Private Reply 경로는 댓글 기반이라 24h 윈도우 대신 7일 제한(`code 100 → FAILED_PARAM`)이 적용된다.
- **Follow-gate 검증**(`check_user_follow_business`, `GET /{IGSID}?fields=is_user_follow_business`)은 발송 흐름이 아니라 **opening DM의 버튼 postback이 돌아왔을 때** 별도 태스크(`process_follow_gate_postback`)에서 수행되고, 통과 시 reward DM을 새로 enqueue한다.
- 토큰: `ig_conn.access_token`을 헤더 `Authorization: Bearer ...`로 사용(복호화는 모델/연동 레이어가 담당).

### 3.4 응답 파싱 (`_post_message`, services.py:715~)
- **2xx 본문 강검증**: `message_id`와 `recipient_id`가 **둘 다 존재**해야 성공. 하나라도 없으면 `DMAnomalyError`. Non-JSON 200 본문도 `DMAnomalyError`.
- **4xx/5xx**: 에러 코드별로 `DMSendError` 하위 예외로 분류(§5).
- 타임아웃 10초, `requests.Timeout`/`ConnectionError`는 `DMTransientError`(재시도 대상)로 변환.
- Mock 모드(`DEBUG=True + INSTAGRAM_MOCK_MODE`) 분기가 `_post_message` 최상단(`if cls._is_mock()`)에 있으며 **v3.9에서 복구됨** — `InstagramMessagingService._is_mock()` classmethod를 자체 정의해 과거 `AttributeError`를 해소(§8.1).

---

## 4. "발송 확정"의 정의 — `SentDMLog` 상태머신 (핵심)

`SentDMLog` (`apps/integrations/models.py:791`)는 DM 1건의 전 생애를 추적하는 **단일 진실 원천**이다. "보내졌음"은 **두 단계**로 확정된다.

### 4.1 상태 값 (`SentDMLog.Status`)
| 상태 | 값 | 의미 | 종결? |
|---|---|---|---|
| 큐 대기 | `queued` | INSERT 직후, 발송 전 | ✗ |
| API 호출 중 | `submitting` | `mark_submitting` 이후 | ✗ |
| **Meta 접수됨** | `accepted` | **200 + message_id + recipient_id 수신 (1차 확정)** | ✗ |
| **도착 확인** | `delivered` | **echo 웹훅 또는 능동 조회로 도착 확정 (2차 확정)** | ✔ |
| 읽음 확인 | `read` | `messaging_seen` 수신 | ✔ |
| 토큰 만료 | `failed_token` | 토큰/세션/권한 실패 | ✔ |
| 24h 윈도우 만료 | `failed_window` | 메시징 윈도우 밖 | ✔ |
| 파라미터 오류 | `failed_param` | 잘못된 파라미터(7일 초과 등) | ✔ |
| 도착 미확인 | `failed_no_trace` | 200 받았으나 35분간 도착 미확인 / 551 / 분류불가 4xx. **'실패'가 아니라 '미확인'** → `total_unconfirmed`로 집계(success_rate 안 깎음) | ✔ |
| Meta 응답 대기 | `rate_limited`·`queued` | rate limit·transient. **횟수 상한 없이 무한 defer**(`next_retry_at`) → requeue 워커가 재투입. 윈도우 만료 시에만 종결 | ✗ |
| 건너뜀 | `skipped` | 예약창 밖·self 등 (**v3.9: 시간당 한도는 더 이상 skip 아님 — defer**) | ✔ |
| (legacy) | `sent`/`failed`/`failed_api`/`pending` | 구 데이터/외부 통합 호환 | — |

- `TERMINAL_STATUSES` = `delivered, read, failed_token, failed_window, failed_param, failed_no_trace, skipped` (+ legacy `sent`/`failed`). **이 상태가 되면 워커가 더 이상 손대지 않는다.**
- `DELIVERED_STATUSES` = `delivered, read` (+ legacy `sent`). **사용자에게 "도착함"이라고 보고 가능한 상태.**
- **카운터 3종**(child=parent_log 있는 reward/retry는 모두 제외): `increment_sent`(ACCEPTED) / `increment_failed`(token·window·param) / **`increment_unconfirmed`(no_trace 전용, 신규 `total_unconfirmed` 필드, migration 0023)**.

### 4.2 "확정"의 2단계

**① 접수 확정 (ACCEPTED) — "Meta가 받았다"**
- 조건: `_post_message`가 **HTTP 200 + 본문에 `message_id` AND `recipient_id`**를 반환 (services.py:778~792).
- 이 조건을 만족하면 `mark_accepted(message_id, api_response)` → `status=ACCEPTED`, `meta_message_id` 저장, `accepted_at` 기록, legacy 호환용 `sent_at`도 같이 기록.
- **주의: ACCEPTED는 "제출 성공"일 뿐 "수신자에게 도착"을 의미하지 않는다.** 그래서 한 단계 더 검증한다.

**② 도착 확정 (DELIVERED) — "실제로 도착했다" (99.9% 보증의 핵심)**
ACCEPTED를 도착으로 끌어올리는 신호는 **독립된 2개 경로**다:

| 경로 | 신호 | 처리 | `verified_via` |
|---|---|---|---|
| **1차 (push)** | `messages` 웹훅의 `is_echo:true` mid가 `meta_message_id`와 매칭 | `mark_delivered(via=echo)` | `echo` |
| **2차 (pull, 안전망)** | `GET /v25.0/{message_id}`(`fetch_message`) 조회 성공 | `mark_delivered(via=conv_api)` | `conv_api` |

- 두 경로가 모두 들어오면 `verified_via`를 `both`로 승격. echo mid 매칭 실패 시 `recipient_user_id + status=ACCEPTED + 최근 1건` fallback 매칭.
- `messaging_seen`을 받으면 `mark_read()` → `READ`(도착보다 강한 상태).

### 4.3 상태 전이 그래프
```
QUEUED ──send_dm_task──▶ SUBMITTING ──200+msgid──▶ ACCEPTED ──echo/conv_api──▶ DELIVERED ──seen──▶ READ
   ▲   ▲                      │                        │
   │   │ defer(next_retry_at) │ 60s 정체               │ 35분간 미확인
   │   │ (rate-limit/transient│ ▼ reconcile_stuck       ▼
   │   │  /한도/거버너, 무한) │ (QUEUED 회귀)        FAILED_NO_TRACE (=미확인, total_unconfirmed)
   │   └──── requeue_deferred_dms (next_retry_at 도래분 FIFO 재투입) ◀─┘
   │
진입부 age 가드 ──메시징 윈도우 만료(comment 7d/user 24h)──▶ FAILED_WINDOW  ← 유일한 graceful 종결
SUBMITTING ──분류된 비재시도 실패──▶ FAILED_TOKEN / FAILED_WINDOW / FAILED_PARAM / FAILED_NO_TRACE(551·기타4xx)
디스패치/실행 단계 ──▶ SKIPPED (예약창 밖·self) · 중복은 duplicate(INSERT 안 됨)
```

### 4.4 멱등성 / 중복 방지 정리
- `idempotency_key = sha256(workspace : ig_user : comment_id : campaign)` **DB UNIQUE** → 같은 댓글+같은 캠페인의 중복 INSERT를 막아 `duplicate`로 흡수.
- reward DM은 `reward:{campaign}:{opening}` 시드, 수동 재시도는 timestamp 포함이라 키가 매번 달라짐(재발송 허용).
- 서로 다른 캠페인 간 중복은 막지 않음(각각 별도 DM).

---

## 5. 실패 처리 시나리오 — 에러 분류표

분류기: `classify_api_error` / `exception_to_classification` (`apps/integrations/dm_exceptions.py`). 우선순위 순서대로 매칭한다(위에서 매칭되면 종료).

| Meta 응답 | 예외 클래스 | `SentDMLog` 상태 | 재시도 | 사후 처리 |
|---|---|---|---|---|
| `code 10` + `subcode 2534022/2018278` | `DMWindowExpiredError` | `failed_window` | ✗ | 24h 윈도우 만료. 사용자 상호작용 재유발 필요. **`increment_failed`** |
| `code in {102, 190, 200}` | `DMTokenError` | `failed_token` | ✗ | **`ig_conn.mark_as_error`** → 사용자 IG 재연동 필요. dead-letter 알림 대상. `increment_failed` |
| `code 100` | `DMInvalidParamError` | `failed_param` | ✗ | 파라미터 오류(Private Reply 7일 초과 포함). `increment_failed` |
| `code 551` | `DMRecipientUnreachableError` | `failed_no_trace` | ✗ | 수신자 도달 불가(차단/옵트아웃 등). 자가 점검. **`increment_unconfirmed`(미확인)** |
| **`code in {1,2}`** ("error-but-delivered") | `DMTransientError` | `queued`(defer) | **✓** | ⚠️ Meta가 **실제로 전달하고도** 이 코드를 반환하는 사례 잦음(2026-07-01 실측). 재발송 전 `has_recent_message_to_recipient`(Conversations 조회)로 전달 흔적 확인(§5.1·§8.2·§11 P12): recent=True→**도착확정·재발송 안 함**, None→**defer**(무손실 우선), False→defer |
| `code in {4,17,32,613}` | `DMTransientError` | `queued`(defer) | **✓** | 명시적 rate limit/transient(=요청 거부, 전달 없음). 검증 없이 **무한 defer**(횟수 상한 없음) |
| **`code 368`** (Action Block) | `DMTransientError` | `queued`(defer) | **✓** | transient defer + **계정 에스컬레이팅 쿨다운**(§11 P4): 그 계정 발송을 24h→×2(상한 7일) 동안 Meta로 보내지 않고 자동 재개. 차단 중 재시도로 인한 차단 연장 방지 |
| HTTP `5xx` | `DMTransientError` | `queued`(defer) | **✓** | 서버 오류. 무한 defer |
| 그 외 `4xx` | `DMApiError` | `failed_no_trace` | ✗ | 분류 불가 클라이언트 오류. **`increment_unconfirmed`** |
| 200인데 `message_id`/`recipient_id` 누락 | `DMAnomalyError` | `queued`(defer) | **✓** | §8.2 — 능동검증이 아니라 **재발송(defer)** 경로 |
| 미지의 케이스 | (폴백) | `queued`(defer) | ✓ | 보수적으로 transient 처리 |

### 5.1 재시도/지연 동작 (`send_dm_task` → `_defer_or_fail`, tasks.py:148~)
- **v3.9 핵심: retriable은 더 이상 5회 후 죽지 않는다.** `retriable == True`면:
  - `retry_count += 1`, `backoff = min(60 * 2^min(retry_count,10), 3600)` → **60s … 최대 1h**(상한 1h, 쿼터는 시각 경계로 풀리므로).
  - `next_retry_at` 기록 + **상태를 `QUEUED`로 회귀** → `requeue_deferred_dms`(Beat 30s)가 도래분을 FIFO로 재투입(§6).
  - 종료는 오직 **메시징 윈도우 만료**(진입부 age 가드)로만 일어난다 → `FAILED_WINDOW`.
- **code 1/2("error-but-delivered") 재발송 전 검증 (v3.10.1, P12)**: `except DMSendError` 블록(tasks.py:947)이 `maybe_delivered = isinstance(e, DMAnomalyError) or e.code in (1, 2)`로 anomaly(§8.2)와 **code 1/2를 동일 취급** — 재발송 전 `has_recent_message_to_recipient(since_seconds=900)`로 이미 보냈는지 확인한다.
  - `recent=True` → `mark_accepted("") + increment_sent(opening만) + mark_delivered(CONV_API)`, **재발송 안 함**(오프닝 DM 2개 중복 방지).
  - `recent=None`(조회 자체가 타임아웃/5xx로 불확실) → **code 1/2는 defer+retry**(무손실; `reconcile_stuck_submitting`의 None→requeue와 대칭). ⚠️ **anomaly(200-no-mid)만** None에서 `FAILED_NO_TRACE`로 종결(재발송 안 함) — code 1/2를 여기서 종결하면 유실 위험이라 비대칭이 의도적.
  - `recent=False` → 기존 defer 재발송.
  - **명시적 rate-limit `{4,17,32,368,613}`은 이 검증을 건너뜀** — 요청 거부라 전달이 없어 확인 불필요.
  - ⚠️ 잔여 한계: 전달됐는데 Conversations 인덱싱 지연으로 `recent=False`면 재시도 중복 가능(기존과 동일=회귀 아님). 완전 제거는 '재시도 직전 재확인' 후속 개선.
- 비재시도(`token/param/window`) → 즉시 `mark_failed` + `increment_failed`. `551`·`기타 4xx`(no_trace) → `mark_failed` + **`increment_unconfirmed`**(실패 아님).
- `FAILED_TOKEN`이면 추가로 `ig_conn.mark_as_error`.
- `parent_log`가 있는 child(reward/retry)는 **모든** 카운터(sent/failed/unconfirmed)에서 제외 — opening/standalone만 집계.

### 5.2 영구 실패의 운영 노출
- 종결된 실패 로그는 `error_code`/`error_subcode`/`error_message`/`api_response`/`verification_log`에 원인을 보존.
- `dead_letter_alerter`(Beat 10분)가 최근 10분 내 `FAILED_TOKEN`·`FAILED_NO_TRACE` 누적을 집계해 `logger.error`로 알림 → 운영자가 토큰 만료/도달 불가 캠페인을 즉시 인지.
- 어드민 API(`apps/admin_api/views/autodm.py`)가 상태별 카운트·실패 건을 모니터링.

---

## 6. 재시도 & 능동 검증 메커니즘 (Beat 안전망)

> 핵심 설계: **push(웹훅)는 유실될 수 있다고 가정**하고, pull(능동 조회) + 주기 워커로 모든 미확정 건을 반드시 종결시킨다.

### 6.1 `verify_dm_delivery` (tasks.py, `max_retries=3`, `verify` 큐) — v3.9 쿼터 절약
- 호출 시점: ① ACCEPTED **10분 후**(send_dm_task가 `countdown=600`으로 예약; v3.9: 5→10분) ② `reconcile_accepted_dms`가 누락 건 재호출.
- 이미 `is_delivered()`/`is_terminal()`이면 skip. `status != ACCEPTED` 또는 `meta_message_id` 없으면 skip.
- `fetch_message`(GET /{message_id}) 결과:
  - **발견** → `mark_delivered(via=conv_api)`.
  - **미발견 + 35분 미만** → 남은 시간 후 **딱 1회만** 더 재예약(`next_retry_at` 기록 → reconcile이 중복 재큐 안 하게 게이트). 즉 정상 케이스 GET은 **~2회**(10분·35분)로 sparse.
  - **미발견 + 35분 경과** → `mark_failed(FAILED_NO_TRACE)` + **`increment_unconfirmed`**(실패 아님).
  - 조회 중 `DMTransientError` → 120초 후 재시도.

### 6.2 Beat 스케줄 (`config/settings/base.py:358~`)
| 태스크 | 주기 | 역할 |
|---|---|---|
| `reconcile_accepted_dms` | **60초** | ACCEPTED로 **10분+** 머물고 예약(`next_retry_at`)이 없거나 2분+ 지난 '고아'만 `verify_dm_delivery` 재가동 (1회 최대 200건). v3.9: 매분 무조건 재큐(건당 GET ~30회) → 고아만 재가동 |
| `reconcile_stuck_submitting` | **30초** | SUBMITTING로 60초+ 정체된 건을 QUEUED로 되돌려 재발송 (워커 크래시·타임아웃 복구, 최대 100건) |
| **`requeue_deferred_dms`** | **30초** | **(신규)** `next_retry_at` 도래한 defer(QUEUED) 건을 `created_at` 오름차순(FIFO)으로 `send_dm_task` 재투입. `next_retry_at=None`인 채 2분+ 정체된 QUEUED(초기 dispatch 유실)도 안전 재투입. `select_for_update(skip_locked)`로 동시 픽업 방지 (최대 200건) |
| `dead_letter_alerter` | **10분** | 최근 10분 FAILED_TOKEN/FAILED_NO_TRACE 누적 알림 (+ **v3.10: Telegram**) |
| `enforce_campaign_schedules` | **60초** | 종료 예약 시각 경과 active 캠페인을 completed로 전환 |
| **`poll_missed_comments`** | **1시간** | 댓글 웹훅 누락 보정 — specific_media 캠페인 게시물 댓글 재조회(§9) |
| **`cleanup_comment_ledger`** | **매일 04:30 KST** | 만료된 `SeenComment` 장부 정리(§9) |
| **`dm_backlog_alert`** | **30분** | **(v3.10)** QUEUED 적체·윈도우 만료 임박 시 Telegram 경고(§11 P7) |
| `refresh_ig_tokens_pending_expiry` | **6시간** | **(v3.10: daily→6h)** 만료 D-14 토큰 갱신 + 성공 시 FAILED_TOKEN 자동 되살림(§11 P2) |
| **`resubscribe_all_webhooks`** | **6시간** | **(v3.10.1)** ACTIVE 연동 계정별 `subscribed_apps`(comments,messages) 재확정 — Meta auto-disable 로 인한 댓글 웹훅 무음 복구(§8.5·§11 P13). 활성 사이트에서만 실변경 |
| `maintain_partitions` | 매일 02:00 KST | `EventInbox` 일별 파티션 유지 + `SentDMLog` 아카이브(멱등/로그 계층 durability) |
| `backup-health-check` | 30분 | `pg_stat_archiver` 점검(WAL 아카이빙 정상성 — DR RPO 보증) |

> ⚠️ **스케줄 발동 경로 주의**: DR Step2 이후 **celery-beat 은퇴**. 스케줄링은 **CF tick + `core.ScheduledJob` DB 행(next_due_at)**으로 동작하며 `settings.CELERY_BEAT_SCHEDULE`는 tick이 **안 읽는다**. 새 주기잡은 `ScheduledJob` 시드 마이그레이션이 필요하다(예: `core 0003_seed_resubscribe_webhooks_job`). beat 항목만 추가하면 절대 안 돎.

> deprecated되어 Beat에서 제거됨(코드는 잔존): `expire_gate_pending`(follow-gate deprecate), `poll_new_media`(next_media가 webhook 기반 전환), `check_polling_anomalies`(폴링 제거).

---

## 7. 관측 / 통계

### 7.1 도착률(delivery_rate)
- 공식(소수 4자리 반올림): **`(delivered + read) / (accepted + delivered + read + failed_no_trace)`**.
  - 분모는 "Meta가 접수한 이후"의 결과만 셈(보내지도 못한 큐/스킵 제외).
- `_build_stats`(admin), `compute_campaign_enrichment`/`build_delivery_summary`(`campaign_stats.py`), dashboard `_delivery_rate`가 **동일 공식**, 분모 0이면 `0.0`.
- 단, `recent_delivery_rate_24h`(admin serializer / `verification_views.py`)는 같은 공식이지만 **분모 0이면 `None`(null)** 반환.

### 7.2 사용량 카운트 주의
- `billing.UsageCounter.dm_sent`는 발송 시 증가하지 않아 **stale**. **월 DM 사용량은 `SentDMLog`로 직접 집계**하고, 한도는 `PlanLimits` 기준이다.
- child(reward/retry) 로그는 `increment_sent`/`increment_failed`/`increment_unconfirmed` 모두에서 제외된다.

### 7.3 도착 미확인(unconfirmed) 분리 집계 — v3.9
- `total_unconfirmed`(신규 필드, migration 0023)는 **`FAILED_NO_TRACE`(200 접수 후 35분 미확인 · 551 · 분류불가 4xx) 전용 카운터**다.
- 이는 "보낸 게 실패한 것"이 아니라 "도착을 **확인 못 한 것**"이므로 `total_failed`·`success_rate`에서 **분리**한다 → no_trace 과집계로 성공률이 부당하게 깎이지 않는다.
- stats API / admin 시리얼라이저(`apps/admin_api/serializers/autodm.py`)에 별도 노출.

### 7.4 테스트
- 관리 커맨드 `apps/integrations/management/commands/loadtest_dm.py`로 발송/확정/실패 경로를 시뮬레이션할 수 있다(부하·파이프라인 점검용).
- 무손실 하드닝 테스트: `apps/integrations/tests_lossless_hardening.py`(P1·P2·P4·P6·P8·P11) + `tests_rate_defer.py`. (`tests_*.py`는 자동수집 안 됨 → 경로 명시 실행.)

---

## 8. 알려진 갭 / 주의사항

### 8.1 [해결됨 ✅] Mock 모드 발송 분기 — `cls._is_mock()` 미정의(AttributeError)
- (과거) `_post_message`가 `if cls._is_mock():`를 호출했으나 `InstagramMessagingService`에 `_is_mock`가 없고, 실제 헬퍼 `is_mock_mode`는 `InstagramOAuthService`에만 있어 상속도 안 됐다 → `DEBUG=True + INSTAGRAM_MOCK_MODE=True` 발송 시 `AttributeError`.
- **v3.9 수정**: `InstagramMessagingService._is_mock()` classmethod를 자체 정의(services.py:447~). prod은 `DEBUG=False`라 항상 False. Mock 발송 경로 복구 완료.

### 8.1b [부분 해결] `rate_governor` 고정 윈도우 카운터는 발송 시도마다 소비된다
- `rate_governor.check()`는 **호출 즉시 `INCR`** 한다 — 발송 전에 카운터를 먹는다. defer 재시도 시 같은 1건이 카운터를 **여러 번** 소비할 수 있다(보수적 과소발송. 밴 위험↓).
- **v3.10 (P8)**: Redis flush/재시작으로 카운터가 0으로 **리셋**되는 별개 위험은 fail-closed 로 해소(센티넬 소멸 감지 → 그 시각 동안 차단). 시도당 소비(과소발송) 특성 자체는 의도된 보수적 페이싱으로 유지.
- **v3.10.1 보완 — Redis 유실 즉시 복구**: fail-closed 는 최대 1h 동결이 유일 회복이 아니다. `rate_governor.rehydrate_from_db()`가 `SentDMLog.submitted_at` 윈도우에서 계정별 시/분 카운터를 재구성 **AND** `dmrate:reset_until`을 삭제(동결 즉시 해제)한다 → `dr_catchup` STEP0 + 활성 사이트 Celery `worker_ready`에서 호출. 따라서 Redis 유실 후 반드시 1h 멈추지 않고 재수화하여 즉시 재개할 수 있다.

### 8.2 [해결됨 ✅] 200-no-msgid anomaly + code 1/2 재발송 시 중복 위험
- (과거) `DMAnomalyError`(200인데 message_id 없음)는 `rate_limited`(retriable)로 분류돼 그냥 재발송 → Meta가 이미 보냈으면 중복 DM.
- **v3.10 (P6)**: anomaly(200-no-mid)·SUBMITTING 크래시(F2) 재발송 전 `has_recent_message_to_recipient`(Conversations 조회)로 '이미 보냈는지' 확인. found=True→도착확정(재발송 안 함)·None(불확실)→`FAILED_NO_TRACE`(미확인, 재발송 안 함)·False→기존 defer 재발송.
- **v3.10.1 (P12)**: 검증-후-재발송을 **Meta code 1/2("error-but-delivered")** 까지 확장(오프닝 DM 2개 중복 방지, 커밋 d03a101). **단 None 처리가 비대칭**: anomaly는 None→`FAILED_NO_TRACE`(종결)지만 code 1/2는 None→**defer**(무손실 우선 — "please retry" 시맨틱이라 미전달 가능성 + 조회 자체가 None을 낼 수 있어 종결하면 유실). 상세 분기는 §5.1.

### 8.3 [해결됨 ✅] 웹훅 POST HMAC 서명 검증
- (과거) `instagram_webhook` POST 에 `X-Hub-Signature-256` 검증이 없어 `AllowAny` 위조 페이로드로 DM 트리거 가능.
- **v3.10 (P3)**: `_verify_webhook_signature`로 앱 시크릿 HMAC-SHA256 검증 추가. `WEBHOOK_HMAC_ENFORCED=True`면 불일치 403, False(기본)면 경고만(롤아웃 관측). `get_instagram_app_secret()` 재사용.

### 8.4 기타
- comments 경로는 `EventInbox` 멱등성을 쓰지 않음 — 중복 방어는 `idempotency_key` UNIQUE + parent/self skip + 쿨다운(`DM_RECIPIENT_COOLDOWN_SECONDS`, 기본 300s) 조합에 의존.
- 스팸 게이트가 `media_id`로 캠페인 1건만 찾으므로 `any_media`/`next_media` 트리거엔 누락될 수 있음(§2.2).
- `mentions`/`messaging_postbacks`(changes 내) 필드는 현재 미처리(로깅만).
- **`ANY_MEDIA`/`STORY_REPLY`는 댓글 누락 보정(§9) 대상이 아님** — 전자는 폴링 비용, 후자는 댓글이 아니라 messages 이벤트(재조회할 소스 없음). 웹훅 유실 시 보정망이 없어 **프론트 위험고지**로 대응(§11 P11 `miss_recovery` 필드).

### 8.5 웹훅 구독 auto-disable → 재구독 자동화 (v3.10.1, P13)
- **문제**: Meta는 콜백 엔드포인트가 **반복 실패**(CF 엣지 장애·DR 컷오버·5xx)하면 IG 계정별 `subscribed_apps`(comments/messages) 구독을 **조용히 auto-disable** 한다. 서버가 정상 복구돼도 **댓글 웹훅이 안 와 캠페인이 무음으로 정지**한다(2026-07-01 DR 훈련에서 실측).
- **진단 신호**: 웹훅 경로는 정상(POST→`EVENT_RECEIVED` 200, GET 잘못된토큰→403)인데 `web_webhook` 로그에 `facebookexternalua` POST **0건** → Meta가 안 보내는 것 → 계정 구독 확인.
- **자동화**:
  - `manage.py resubscribe_webhooks [--check-only]` — ACTIVE 연동 전부 점검·재구독(수동/DR).
  - Beat `resubscribe_all_webhooks`(6h) → `resubscribe_active_connections(check_only)` — 계정별 best-effort 멱등, `REQUIRED_WEBHOOK_FIELDS=("comments","messages")`, 활성 사이트에서만 실변경, 변화 시 Telegram.
  - **DR startup.sh(live)**: promote 직후 즉시 전 계정 재구독 — "서버 이전 후 바로 웹훅 켜기".
- **함정**: (1) 이 6h 잡은 `ScheduledJob` 시드(`core 0003`)로만 돎(§6.2 주의). (2) 게시물 **소유 계정** 댓글은 애초에 comments 웹훅이 안 옴 + 코드도 스킵 → 댓글-DM 테스트는 반드시 **다른 계정**으로.

---

## 9. 댓글 웹훅 누락 보정 (Missed-Comment Compensation) — v3.9 신규

> **목적**: Instagram comments 웹훅이 서버 다운·배포·5xx·Meta 측 드롭(Meta는 미수신 시 36h 후 폐기)으로 유실돼도, **1시간 내 자동으로 누락 DM을 발송**하는 안전망. 핵심 정확성 보증은 §4.4의 `idempotency_key`(웹훅·폴링 겹쳐도 중복 불가)와 §3.1의 `rate_governor`(폴링이 한꺼번에 쏟아내지 않음)에 그대로 얹힌다.

### 9.1 구성요소
| 구성 | 내용 |
|---|---|
| `SeenComment` (신규 모델, migration 0025) | 댓글 본문 저장 ✗, `comment_id`만 최소 기록(`ig_connection`+`comment_id` UNIQUE). `expires_at`로 **TTL 10일** 자동 만료. `source`=`webhook`/`poll`, `triggered` 플래그 |
| 웹훅 경로 | 댓글 수신 시 `_record_seen_comment`로 장부에 멱등 기록 추가(별도 Meta API 호출 없음 — payload만으로). 기록 실패는 DM 흐름을 막지 않음 |
| `poll_missed_comments` (Beat 1h) | active·예약창 내 `specific_media`(attach된 next_media 포함) 캠페인 게시물의 최근 댓글을 newest-first로 재조회 → 장부에 없는 누락분만 `_enqueue_send_dm`(웹훅과 동일 경로) |
| `cleanup_comment_ledger` (Beat daily) | 만료 장부 배치 삭제 |

### 9.2 폴링 종료 규칙 (`_poll_one_media`, 먼저 만나는 것)
1. **앵커** — 이미 장부에 있는 댓글(`created=False`)을 만나면 그보다 오래된 건 모두 관측됨 → 중단(인플루언서 수천 댓글이어도 첫 페이지에서 멈춤).
2. **7일 창 밖**(`window_floor`) 댓글 — Private Reply 불가(어차피 Meta code=100) → 중단.
3. **댓글 소진**(`paging_after` 없음).
4. **폭주 방지 상한**(`MAX_PAGES`, 기본 20p) 도달 → backfill gap 가능성 경고 + Telegram 알림.

### 9.3 cold-start 방지
- per-campaign baseline: `eff_start = started_at | scheduled_start_at | created_at`보다 **이전** 댓글은 보정 발송하지 않음 → 오래된 게시물에 캠페인을 새로 켰을 때 기존 댓글 대량 발송 차단.

### 9.4 토글 / 안전장치
- `MISSED_COMMENT_POLL_ENABLED`(기본 True)로 즉시 끌 수 있음. `MISSED_COMMENT_LEDGER_TTL_DAYS`(10)·`MISSED_COMMENT_POLL_PAGE_SIZE`(50)·`MISSED_COMMENT_POLL_MAX_PAGES`(20)·`MISSED_COMMENT_POLL_MAX_TARGETS`(1000) 조정 가능.
- 발송 폭주는 `rate_governor`(§3.1)가 throttle, 중복은 `idempotency_key`(§4.4)가 하드 차단.

---

## 10. 부록 — 핵심 코드 위치

| 역할 | 위치 |
|---|---|
| 웹훅 엔드포인트(GET/POST) | `apps/integrations/views.py:3501` `instagram_webhook` |
| 메시징 이벤트 분기(echo/read/postback/story) | `apps/integrations/views.py:3313~` `_process_messaging_events` |
| 멱등 장부 모델 | `apps/integrations/models.py:1288` `EventInbox` |
| 댓글 트리거 진입 | `apps/integrations/tasks.py:51` `process_comment_and_send_dm` |
| 발송 디스패치(가드+INSERT) | `apps/integrations/tasks.py` `_enqueue_send_dm` |
| 발송 태스크(상태머신) | `apps/integrations/tasks.py:707` `send_dm_task` |
| **발송 속도 제어(defer 판정)** | `apps/integrations/tasks.py:121` `_rate_defer` / `:148` `_defer_or_fail` / `:101` `_messaging_window` |
| **계정별 안전속도 거버너** | `apps/integrations/rate_governor.py` `check` (750/hr cap + 분당 버스트, Redis 고정 윈도우) |
| **defer 재투입 워커(FIFO)** | `apps/integrations/tasks.py:999` `requeue_deferred_dms` |
| **댓글 누락 보정 폴링** | `apps/integrations/tasks.py:1514` `poll_missed_comments` / `:1391` `_poll_one_media` |
| **댓글 관측 장부 모델** | `apps/integrations/models.py:1412` `SeenComment` (migration 0025) |
| **도착미확인 카운터** | `apps/integrations/models.py:760` `increment_unconfirmed` / `:538` `total_unconfirmed` (migration 0023) |
| Private Reply 발송 | `apps/integrations/services.py:473` `send_dm_via_comment` |
| 24h 윈도우 발송 | `apps/integrations/services.py:508` `send_dm_via_user_id` |
| API 호출·응답 강검증·에러 분류 | `apps/integrations/services.py:715~` `_post_message` |
| 멱등 키 생성 | `apps/integrations/services.py:456` `build_idempotency_key` |
| 능동 검증(GET /{message_id}) | `apps/integrations/services.py:800` `fetch_message` |
| 발송 로그/상태머신 모델 | `apps/integrations/models.py:791` `SentDMLog` |
| 에러 분류기 | `apps/integrations/dm_exceptions.py` `classify_api_error` / `exception_to_classification` |
| 10분/35분 도착 검증 | `apps/integrations/tasks.py:742` `verify_dm_delivery` |
| Beat 안전망 | `apps/integrations/tasks.py:814~` `reconcile_accepted_dms` / `reconcile_stuck_submitting` / `dead_letter_alerter` |
| Beat 스케줄 등록 | `config/settings/base.py:358~` `CELERY_BEAT_SCHEDULE` |
| 도착률·통계 | `apps/integrations/campaign_stats.py`, `apps/admin_api/serializers/autodm.py` |
| **제자리 되살림(revive)** | `apps/integrations/models.py` `SentDMLog.revive` / `REVIVABLE_STATUSES` / `messaging_window` (P1) |
| **토큰 복구 자동 되살림** | `apps/integrations/tasks.py` `revive_failed_token_logs` (P2) |
| **프리미엄 수동 재시도 API** | `apps/integrations/views.py` `AutoDMCampaignViewSet.retry_failed` (`POST .../auto-dm-campaigns/{id}/retry-failed/`, P2) |
| **웹훅 HMAC 검증** | `apps/integrations/views.py` `_verify_webhook_signature` (P3) |
| **Action Block 쿨다운** | `apps/integrations/rate_governor.py` `trip_action_block` / `action_block_cooldown_remaining`; `tasks.py` `_ACTION_BLOCK_CODES`, `_defer_or_fail`/`_rate_defer` 연동 (P4) |
| **거버너 fail-closed 센티넬** | `apps/integrations/apps.py` `ready()` + `rate_governor.check`(`dmrate:alive`/`dmrate:reset_until`, P8) |
| **검증 후 재발송** | `apps/integrations/services.py` `has_recent_message_to_recipient`; `tasks.py:947` anomaly·**code 1/2** 분기(`maybe_delivered`)·`reconcile_stuck_submitting` (P6·**P12**) |
| **Action Block DB 영속** | `apps/integrations/models.py` `DMAccountBlock`; `rate_governor.py` `_persist_action_block_to_db`/`_restore_action_block_from_db`/`rehydrate_from_db` (P4·P8) |
| **웹훅 재구독 자동화** | `apps/integrations/tasks.py` `resubscribe_all_webhooks`/`resubscribe_active_connections`; `services.py` `subscribe_to_webhooks`/`get_webhook_subscriptions`; `management/commands/resubscribe_webhooks.py`; `core 0003_seed_resubscribe_webhooks_job` (P13) |
| **백로그 모니터링** | `apps/admin_api/views/autodm.py` `AdminDMBacklogView` (`GET .../admin/auto-dm/backlog/`); `tasks.py` `dm_backlog_alert` (P7) |
| **재연동 고아화 방지** | `apps/integrations/models.py` `IGAccountConnection` `uq_igconn_ws_account`; migration `0026` dedupe (P5) |
| **위험고지 필드** | `apps/integrations/serializers.py` `AutoDMCampaignSerializer.miss_recovery` (P11) |

---

## 11. 무손실 하드닝 (v3.10)

> v3.9(defer·거버너·누락 보정) 위에, [실패 케이스 전수 분석](#)에서 사용자가 채택한 항목을 패치한 묶음. 각 패치의 신규 설정은 [§11.2](#112-신규-설정) 참조.

### 11.1 패치 요약
| ID | 무엇 | 해소한 손실/위험 |
|---|---|---|
| **P1** | **제자리 되살림(revive)** — `FAILED_TOKEN`/`SKIPPED` 종결 건을 **같은 row·같은 idempotency_key** 로 QUEUED 복귀(`SentDMLog.revive`). 윈도우(7d/24h) 내인 것만. admin 재시도 엔드포인트도 이 경로로 통합 | 종결 건이 키를 점유해 폴링/재시도가 되살리지 못하던 **영구 손실(C1/C2/H1)** |
| **P2** | 토큰 갱신 **6h 주기**로 강화 + 갱신 성공 시 `revive_failed_token_logs` 로 그 계정 FAILED_TOKEN 자동 되살림 + **프리미엄 전용** `POST .../auto-dm-campaigns/{id}/retry-failed/`(무료=403) | 토큰 만료 구간 누락(C1·G1) |
| **P3** | 웹훅 **HMAC**(`X-Hub-Signature-256`) 검증. `WEBHOOK_HMAC_ENFORCED`(기본 False=관측 → True=403) | 위조 페이로드 주입(A5) |
| **P4** | **Action Block 서킷 브레이커** — `code 368` 감지 시 그 계정 발송을 **에스컬레이팅 쿨다운**(24h→×2, 상한 7일) 동안 Meta로 보내지 않고 자동 재개. cache(`dm:ab:*`) + **DB 이중기록**(`DMAccountBlock` 모델): 매 트립을 DB에 dual-write, 캐시 미스 시 `_restore_action_block_from_db`로 재프라임, `rehydrate_from_db()`가 Redis 유실/DR failover 후 쿨다운+레벨 재시드 → **차단이 Redis flush/컷오버로 조용히 풀리지 않음** | 과발송 차단의 **차단 중 재시도→기간 연장**(I1) + Redis/DR 유실로 인한 차단 소실 |
| **P5** | `IGAccountConnection` `(workspace, external_account_id)` **UNIQUE**(`uq_igconn_ws_account`) + migration 0026 dedupe(캠페인/SeenComment/SpamFilter repoint 후 스테일 삭제) | 재연동 시 **캠페인 고아화**(G2) |
| **P6** | **검증 후 재발송** — 200-no-msgid anomaly(C4)·SUBMITTING 크래시(F2) 재발송 전 `has_recent_message_to_recipient`(Conversations 조회). found=True→도착확정, None→미확인(재발송 안 함), False→재발송 | **중복 발송**(C4/F2) |
| **P7** | **백로그 모니터링** `GET .../admin/auto-dm/backlog/`(QUEUED 적체·윈도우 임박·throughput/inflow·상위 계정) + `dm_backlog_alert`(30분 Telegram) | 유입>처리량 적체로 인한 **윈도우 만료 손실(E1)** 가시화 |
| **P8** | 거버너 **fail-closed** — `AppConfig.ready()`가 센티넬(`dmrate:alive`)을 심고, check 에서 센티넬 소멸=Redis flush 로 보아 그 시각까지 차단(`dmrate:reset_until`). 콜드스타트(배포)는 ready 가 재시드해 안 막힘. **v3.10.1: `rehydrate_from_db()`**가 `SentDMLog.submitted_at`에서 카운터 재구성 + `reset_until` 삭제로 **동결 즉시 해제**(dr_catchup STEP0 / worker_ready) → 1h 강제 동결 불필요(§8.1b) | Redis 리셋 후 **순간 과발송→밴**(E3) |
| **P9** | `dead_letter_alerter`·`dm_backlog_alert`·Action Block 트립 **Telegram** 표준화 | 장애 인지 지연(G1) |
| **P10** | 동일 수신자 쿨다운 60s→**설정화**(`DM_RECIPIENT_COOLDOWN_SECONDS`, 기본 300s) | 도배·계정 보호(B4) |
| **P11** | `AutoDMCampaignSerializer.miss_recovery` — any_media/story_reply 는 `auto_recovery_supported=False`+경고 문구 노출(프론트 고지) | 보정 불가 트리거 **위험고지**(A1·any_media) |
| **P12** | **code 1/2 "error-but-delivered" 중복 방지**(커밋 d03a101) — P6의 검증-후-재발송을 Meta code 1/2까지 확장. recent=True→도착확정, code 1/2 None→**defer**(무손실, anomaly와 비대칭), False→defer. rate-limit `{4,17,32,368,613}`은 검증 제외(§5.1·§8.2) | 오프닝 DM **2개 중복 발송**(2026-07-01 실측) |
| **P13** | **웹훅 구독 재구독 자동화**(커밋 e538a9b·986be99) — Meta auto-disable 로 댓글 웹훅 무음이 되는 것을 `resubscribe_all_webhooks`(Beat 6h·`ScheduledJob` 시드) + DR startup + `manage.py resubscribe_webhooks`로 복구(§8.5) | 엣지 장애/DR 후 **캠페인 무음 정지** |

### 11.2 신규 설정
| 키 | 기본 | 패치 |
|---|---|---|
| `WEBHOOK_HMAC_ENFORCED` | False | P3 |
| `DM_ACTION_BLOCK_BASE_COOLDOWN_HOURS` | 24 | P4 |
| `DM_ACTION_BLOCK_MAX_COOLDOWN_DAYS` | 7 | P4 |
| `DM_RECIPIENT_COOLDOWN_SECONDS` | 300 | P10 |
| `DM_BACKLOG_RISK_HOURS` | 6 | P7 |
| `DM_BACKLOG_OLDEST_ALERT_HOURS` | 2 | P7 |

### 11.3 범위 외(미패치, 근거)
- **B2 next_media**: attach 는 webhook 전용이며 timestamp fetch 실패는 다음 댓글에 자가치유 → 전용 패치 불요.
- **C5 네트워크 장기 단절**: 기존 무한 defer + 윈도우 만료로 이미 처리.
- **F1 Beat 감시/하트비트**: 별도 처리 예정(이 묶음 범위 외).
- **스토리 답장 능동 복구**: 메시지 기반이라 재조회 소스가 없어 미구현 → P11 위험고지로 대응.

---

*작성 기준: `hardening/dm-surge` 브랜치 · 발송 보증 **v3.10.1**(2026-07-01: P12 code 1/2 중복방지 + P13 웹훅 재구독 자동화 + Action Block/거버너 DR 영속·재수화). 코드 변경 시 본 문서의 §4 상태머신 / §5 분류표 / §6 스케줄 / §8 갭 / §9 누락 보정 / §11 하드닝을 함께 갱신할 것.*
