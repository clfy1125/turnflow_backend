# 홈 화면 상태 확인용 dev 테스트 계정 — 백엔드 회신

2026-10-09 · 백엔드 → 프론트
요청서: `backend-home-test-accounts.md`(2026-10-08)
후속: **`HOME_TEST_ACCOUNTS_FOLLOWUP_RESPONSE.md`(2026-10-11)** — 프론트 확인 결과 9건 회신 (§2-2 `media_url` 설명과 H16·H17 값이 **그 문서에서 정정**됐습니다)

---

## 0. 요약

- **43개 계정 전부 dev 에 만들어 뒀습니다.** 지금 바로 로그인하면 표의 상태가 보입니다.
- **비밀번호는 전부 `Test1234!`** (A01 포함 — A01 도 로그인은 됩니다).
- **게시물 API 500 은 고쳐졌습니다.** 이 계정들의 인스타 토큰은 백엔드가 목(mock)으로 인식하는
  접두어라 Meta 로 나가지 않고, `GET .../media/` 가 **썸네일이 들어 있는 고정 픽스처**를 돌려줍니다.
  (`HTTP 200 · 게시물 6건 · 썸네일 6건` 으로 실제 확인했습니다.)
- **재시드는 프론트가 직접 하세요** — 인증 없이 호출하는 dev 전용 엔드포인트를 열었습니다(§3).
- **못 만든 상태는 A04 하나**이고, A02·A03 은 절반만 만들었습니다(§5).

> 요청하신 우선순위 **H01 · H02 · H11 · H12 · P01 · P02 · I02** 는 전부 포함돼 있고, 응답을
> 하나하나 눈으로 확인했습니다.

---

## 1. 계정 목록

로그인: `POST https://dev-api.turnflow.link/api/v1/auth/login/` `{ "email": …, "password": "Test1234!" }`
(끝 슬래시 필수)

### 1-1. 홈 알림 (H)

`GET /api/v1/home/alerts/` 의 `alerts[].code` 를 **실제 응답에서 뽑아** 적었습니다.
괄호 안은 `rank` 입니다. 큰 카드는 rank 가 가장 작은 하나입니다.

| 번호 | 이메일 | 실제로 나오는 알림 (rank 순) |
|---|---|---|
| H01 | `home-h01@test.turnflow.link` | `recent_post_no_campaign`(200, 2시간 전) · `report_ready`(230) · `migration_review_pending`(240, 3건) · `create_campaign`(999, 기본) |
| H02 | `home-h02@test.turnflow.link` | `payment_failed`(10) · `ig_disconnected`(20, @sample_studio) · `campaign_post_restricted`(60) · `dm_send_blocked`(70) · `create_campaign`(999) → **멈춤 4** |
| H03a | `home-h03a@test.turnflow.link` | `ig_disconnected` · `reason=token_invalidated` · `status=error` |
| H03b | `home-h03b@test.turnflow.link` | `ig_disconnected` · `reason=account_checkpoint` · `status=error` |
| H03c | `home-h03c@test.turnflow.link` | `ig_disconnected` · `reason=app_removed` · `status=error` |
| H03d | `home-h03d@test.turnflow.link` | `ig_disconnected` · `reason=reconnect_required` · `status=expired` |
| H04 | `home-h04@test.turnflow.link` | `comment_stream_down`(40) · `target_id` = IG 연결 id |
| H05 | `home-h05@test.turnflow.link` | `dm_quota_exhausted`(50) · `used=200/limit=200` · `blocked_count=37` · `resumable_count=31` · `resets_at`=다음 달 1일 00:00 KST |
| H06 | `home-h06@test.turnflow.link` | `campaign_post_restricted`(60) × 2 — 「가을 신상 미리 알림」 · 「팝업 스토어 예약(게시물 차단)」 |
| H07 | `home-h07@test.turnflow.link` | `dm_send_blocked`(70) · `seconds_remaining`≈10,800(3시간) · `waiting_count=12` · `auto_resumes=true` |
| H08 | `home-h08@test.turnflow.link` | `ig_account_disabled`(80) · `count=1`(@sample_studio) · `allowance=2` · `active=1` |
| H09 | `home-h09@test.turnflow.link` | `dm_quota_warning`(100) · `used=170/limit=200`(85%) · `remaining=30` |
| H10 | `home-h10@test.turnflow.link` | `subscription_ending`(110) · `days_left=3` |
| H11 | `home-h11@test.turnflow.link` | `create_campaign`(210, `is_default=false`) · `link_page_empty`(220, `reason=no_page`) |
| H12 | `home-h12@test.turnflow.link` | `create_campaign`(999, `is_default=true`) **하나뿐** |
| H13 | `home-h13@test.turnflow.link` | H12 와 같음 + 작성 중 자동 DM 「말차 신메뉴 안내」 1개 |
| H14 | `home-h14@test.turnflow.link` | `link_page_empty`(220, `reason=no_blocks`) |
| H15 | `home-h15@test.turnflow.link` | `link_page_empty`(220, `reason=private`) |
| H16 | `home-h16@test.turnflow.link` | `report_running`(231) · `progress=45` · `stage=extracting`(영상 분석 중) |
| H17 | `home-h17@test.turnflow.link` | `report_failed`(232) · `error_code=EXTRACT_FAILED` · `stage=extracting` |
| H18 | `home-h18@test.turnflow.link` | `create_campaign`(999) **하나뿐** + 자동 DM 8개 · 링크 페이지 2개 |

### 1-2. 요금제 · 결제 · 체험 (P)

`GET /api/v1/billing/my-subscription/` 응답을 그대로 적었습니다.

| 번호 | 이메일 | plan / status | 카드 | 비고 |
|---|---|---|---|---|
| P01 | `home-p01@test.turnflow.link` | free / active | 없음 | `trial_used_at=null` → 체험 시작 가능 |
| P02 | `home-p02@test.turnflow.link` | pro / trialing | **없음** | 25일 남음 · `trial_kind=auto` · **IG 연결 0** |
| P03 | `home-p03@test.turnflow.link` | pro / trialing | **없음** | 3일 남음 · `trial_kind=auto` |
| P04 | `home-p04@test.turnflow.link` | pro / trialing | 현대 | 22일 남음 · `trial_kind=card` · 첫 결제일 = `current_period_end` |
| P05 | `home-p05@test.turnflow.link` | pro / active | 신한 | `extra_ig_accounts=2` → 허용량 3 / 연결 3(전부 켜짐) |
| P06 | `home-p06@test.turnflow.link` | basic / active | 국민 | 다음 결제 20일 뒤 |
| P07 | `home-p07@test.turnflow.link` | pro / past_due | 신한 | 유예 7일 뒤 종료 · 재시도 2일 뒤 |
| P08 | `home-p08@test.turnflow.link` | pro / cancelled | 현대 | 3일 뒤 종료 (H10 과 같은 상태) |
| P09 | `home-p09@test.turnflow.link` | pro / paused | 현대 | 2개월 정지 · 59일 뒤 자동 재개 |
| P10 | `home-p10@test.turnflow.link` | free / active | 없음 | `trial_used_at` 있음 → 체험 대신 바로 결제 |
| P11 | `home-p11@test.turnflow.link` | pro / trialing | 없음 | 20일 남음 + **제휴코드 `DEVHOME14`** |

**P11 제휴코드** — dev 에 유효한 코드가 없다고 하신 건 맞았고, 하나 만들었습니다.

| | 값 |
|---|---|
| 코드 | **`DEVHOME14`** |
| 보너스 | 14일 (`trial_days`) |
| 확인 | `GET /api/v1/billing/referral/validate/?code=DEVHOME14` (**GET 입니다**, POST 아님) |
| 응답 | `{"valid": true, "trial_days": 14, "base_trial_days": 30, "total_trial_days": 44, …}` |
| 「쓸 수 없음」 | 아무 문자열이나 넣으세요 — 예: `?code=NOPE0000` → `{"valid": false, "reason": "존재하지 않는 코드입니다."}` (HTTP 200) |

> ⚠️ 표기는 **`total_trial_days`(44)** 를 쓰세요. `trial_days`(14) 는 보너스분이라
> 그대로 노출하면 혜택이 1/3 로 축소돼 보입니다 (`REFERRAL_COUPON_FRONTEND.md` 와 같은 주의).

### 1-3. 인스타그램 연결 (I)

| 번호 | 이메일 | 상태 |
|---|---|---|
| I01 | `home-i01@test.turnflow.link` | 연결 0 · 자동 DM 0 · 링크 페이지 0 |
| I02 | `home-i02@test.turnflow.link` | 연결 3 — @sample_creator(active·켜짐·**사진 O**) / @sample_studio(**expired**·켜짐·**사진 X**) / @sample_archive(active·**꺼짐**) |
| I03 | `home-i03@test.turnflow.link` | 연결 1 · `status=expired` · `is_active=true` → 쓰는 계정인데 「다시 연결 필요」 |
| I04 | `home-i04@test.turnflow.link` | 연결 3 · 허용량 1 → `blocking.code = "ig_account_selection_required"` |
| I05 | `home-i05@test.turnflow.link` | 허용량 2 / 켜짐 2 · 꺼짐 1 · `GET /api/v1/billing/ig-account-activation/` → **`can_change_today: false`** |
| I06 | **`ig_devhome06@ig.invalid`** | `email_is_placeholder=true` · `instagram_user_id` 있음 |
| I07 | `home-i07@test.turnflow.link` | `full_name=""` |

> **I06 의 로그인 이메일만 규칙이 다릅니다.** 인스타로 가입하면 인스타가 이메일을 주지 않아
> 백엔드가 `ig_<id>@ig.invalid` 자리표시를 발급합니다 — 그게 이 상태의 본질이라
> `home-i06@…` 으로 두면 확인하려던 것을 못 보게 됩니다.

### 1-4. 가입 · 로그인 (A)

| 번호 | 이메일 | 상태 |
|---|---|---|
| A01 | `home-a01@test.turnflow.link` | `is_email_verified=false` (로그인은 **됩니다** — 아래 참고) |
| A02 | `home-a02@test.turnflow.link` | `kakao_id` 있음 (절반만 — §5) |
| A03 | `home-a03@test.turnflow.link` | 일반 이메일 계정과 서버 상태가 **같음** (§5) |
| A04 | `home-a04@test.turnflow.link` | IG 선점 역할만. 409 재현은 추가 정보 필요 (§5) |

> **A01**: 로그인 API 는 이메일 인증을 막지 않습니다. 토큰은 정상 발급되고,
> `GET /api/v1/auth/me/` 의 `is_email_verified: false` 로 6자리 인증 화면을 띄우시면 됩니다.

---

## 2. 공통 예시 데이터 — 실제로 들어간 값

### 2-1. 사진은 **고정 data URI** 입니다 (외부 호스트 0)

요청하신 「고정 사진 URL」 을 **`data:image/svg+xml;utf8,…`** 로 넣었습니다. `<img src>` 에
그대로 들어가고, 재시드해도 같은 값이 나옵니다.

- **게시물 썸네일**: 640×640 색 블록 + 키워드 라벨 (`thumbnail_url`)
- **프로필 사진**: 320×320 원형 + 첫 글자 (`profile_picture_url`)

외부 이미지 호스트를 쓰지 않은 이유는 두 가지입니다 — IG CDN URL 은 서명 만료라 저장 자체가
불가능하고(그래서 운영은 우리 스토리지에 재호스팅합니다), 더미 이미지 호스트는 죽는 순간
dev 화면이 통째로 깨집니다. **사진이 없는 상태**가 필요한 곳(@sample_studio, 「오류」 캠페인)은
빈 문자열 `""` 로 내려갑니다.

### 2-2. 게시물

`GET /api/v1/integrations/instagram/workspaces/{workspace_id}/media/` 로 조회하세요.
(워크스페이스 id 는 `GET /api/v1/workspaces/` 에서 — 계정마다 워크스페이스는 **1개**입니다.)

응답은 `{ success, data: [...], paging, count, connection, query, mock: true }` 모양이고,
기본 **10건** · `?limit=N` 으로 최대 **100건**까지 나옵니다(픽스처 풀이 100건).

- id 규약은 `mm-{ig_user_id}-{n}`, 캠페인형은 `-camp{k}` 가 붙습니다. 최신순입니다.
- **홈 알림의 `media_id` 는 이 목록 안의 id 입니다** — 큰 카드에서 썸네일을 조회해도
  반드시 나옵니다. (종전 더미 계정은 여기서 어긋나 사진이 비어 있었습니다.)
- ⚠️ **`media_url` 의 뜻은 엔드포인트마다 다릅니다** (2026-10-11 정정 — 아래 문단은
  앞서 틀리게 적었던 것을 바로잡은 것입니다):
  - **게시물 목록 `GET .../media/` 의 `media_url`** = Graph 값 그대로 = **렌더 가능한
    미디어 URL**(IG CDN 이미지/영상). 서명 만료가 있으니 **저장하지 마세요.**
    목 픽스처도 이제 운영과 같게 렌더 가능한 값을 줍니다.
  - **캠페인 응답의 `media_url`**(`AutoDMCampaign.media_url`) = **permalink(게시물 링크)**
    이고 이미지가 아닙니다. 캠페인 썸네일은 반드시 `thumbnail_url`(우리 스토리지 사본)을
    쓰세요 (`DM_CAMPAIGN_THUMBNAIL_FRONTEND.md`: 이 둘을 섞어 prod 77건 중 68건이 깨졌습니다).
- 「새 게시물」 = `alerts[].data.published_at` 이 **정확히 2시간 전**입니다.
  목록 API 쪽 `timestamp` 는 픽스처가 자체 계산한 값이라 1~2시간 차이가 날 수 있습니다 —
  **큰 카드 문구는 알림의 `published_at` 을 쓰세요.**

### 2-3. 자동 DM 7개 — status 매핑

서버에는 **`draft` 와 `error` 라는 status 값이 없습니다.** 실제로 들어간 값은 이렇습니다.

| 요청서 표기 | 서버 값 | 비고 |
|---|---|---|
| 작동 중 | `status="active"` | |
| 중지됨 | `status="paused"`, `auto_paused_reason=""` | 사용자가 직접 멈춤 |
| 작성 중 | **`status="inactive"`** | 초안. 「불러온 캠페인 적용」도 이 상태로 생성됩니다 |
| 종료됨 | `status="completed"` | |
| 오류 | **`status="paused"` + `auto_paused_reason="post_restricted"`** | `thumbnail_url=""` |

7개 목록(최근 수정 순 = 응답 순서):
신제품 이벤트(작동 중) → 팝업 스토어 예약(중지됨) → 말차 신메뉴 안내(작성 중) →
가을 신상 미리 알림(오류·사진 없음) → 여름 코디 할인(종료됨) →
「10월 한정 신상품 출시 기념 댓글 이벤트 자동 안내 메시지」(작동 중) → 가을 피드 리뷰 이벤트(작동 중)

> ⚠️ **「오류」 캠페인은 H06 에서 보세요.** 오류 = `post_restricted` 뿐이고, 그 상태의 캠페인은
> **반드시 `campaign_post_restricted`(rank 60) 알림을 동반합니다.** 그 알림이 rank 60 이라
> H01 의 「새 게시물」(200)·H18 의 「정상 작동 중」(999) 큰 카드를 밀어냅니다.
> 그래서 **H01·H18 에서는 그 캠페인을 「중지됨 + 사진 없음」으로 바꿔 넣었습니다.**
> H06 은 공통 7개를 그대로 넣고 오류 1건을 더해 **9개 + 오류 2건** 으로 만들었습니다 —
> 오류 카드와 긴 이름 줄바꿈을 한 계정에서 같이 보실 수 있습니다.

### 2-4. 링크 페이지 2개

- `샘플 크리에이터` — 공개, 블록 4개(프로필 1 + 링크 3)
- `샘플 스튜디오` — 비공개, 블록 3개

slug 는 계정마다 다릅니다(`sample-creator-h01` 처럼 코드가 붙습니다).

---

## 3. 재시드 — 프론트가 직접 하세요

dev 스택은 백엔드 PC 의 도커라 프론트가 `manage.py` 를 돌릴 수 없습니다. 그래서 **같은 시더를
HTTP 로 열었습니다.** 인증 불필요(A01·I01 처럼 로그인 전 화면도 되돌려야 해서), `DEBUG=True` 인
dev 에서만 열립니다 — 운영은 이 경로가 통째로 404 입니다.

### 목록 조회

```bash
curl https://dev-api.turnflow.link/api/v1/home/dev/test-accounts/
```
```json
{ "password": "Test1234!", "referral_code": "DEVHOME14", "referral_bonus_days": 14,
  "accounts": [ { "code": "H01", "title": "대표 화면 — …", "email": "home-h01@test.turnflow.link" }, … ] }
```

### 재시드

```js
await fetch('https://dev-api.turnflow.link/api/v1/home/dev/test-accounts/reseed/', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ codes: ['H01', 'H02'] }),   // 생략하면 43개 전부 (1분+)
});
```
```json
{ "seeded": 2, "password": "Test1234!",
  "accounts": [ { "code": "H01", "email": "…", "title": "…", "summary": "…", "notes": [] }, … ] }
```

- **코드는 대소문자를 가리지 않습니다** (`h01`, `H03A` 전부 됩니다).
- 알 수 없는 코드 → **400** + `details.valid_codes` 에 전체 목록
- 재시드가 이미 돌고 있으면 → **409** (동시에 돌면 서로의 데이터를 지웁니다)
- `ig_user_id` 필드는 A04 전용입니다(§5)

### 재시드가 되돌리는 것

그 계정의 **IG 연결 · 자동 DM · 발송 로그 · 링크 페이지 · 리포트 · 닫은 알림 · 결제 기록 · 구독**을
전부 지우고 다시 만듭니다. 알림을 닫았든 계정을 껐든 한 번 호출하면 원점입니다.
남은 일수 · 결제 유예일 · 「2시간 전」 같은 시간 값은 **호출 시점 기준으로 다시 계산**됩니다.

### 백엔드 쪽에서 돌릴 때

```bash
docker compose exec web python manage.py seed_home_test_accounts            # 전부
docker compose exec web python manage.py seed_home_test_accounts --only H01,P02
docker compose exec web python manage.py seed_home_test_accounts --list
```

---

## 4. 확인할 때 걸릴 만한 것 4가지

1. **홈 알림은 30초 캐시입니다.** 재시드 직후 캐시는 서버가 지우지만, 직접 상태를 바꾼 뒤
   (알림 닫기 등) 바로 다시 조회하면 최대 30초 옛 값이 나올 수 있습니다.
2. **시간이 지나면 저절로 사라지는 시드 상태가 둘 있습니다.** 둘 다 재시드하면 돌아옵니다.
   - **H16(리포트 만드는 중) — 1시간.** `insta_reports.sweep_stale`(30분 주기)이 생성 60분이
     지난 `running` 리포트를 실패로 확정합니다.
   - **H02 · H07 의 `dm_send_blocked` — 3시간.** 이 알림은 "인스타가 발송을 막았고 N초 뒤
     자동 재개된다"는 뜻이라 쿨다운이 끝나면 **사라지는 것이 정상 동작**입니다.
     시드는 3시간짜리 쿨다운을 심으므로, 그 뒤 H02 의 칩이 「멈춤 4」 → 「멈춤 3」이 됩니다.
3. **H05 의 `blocked_count`/`resumable_count` 는 매월 1~7일에 같아집니다.**
   정의가 달라서입니다 — `blocked_count`=이번 달, `resumable_count`=최근 7일.
   「막혔지만 바로 못 살리는」 건은 *이번 달 안이면서 7일보다 오래된* 구간에만 존재하는데,
   월초에는 그 구간이 없습니다. 버그가 아닙니다.
4. **한도 알림(H05·H09)은 free 플랜 계정입니다.** 프로는 DM 한도가 무제한(`-1`)이라
   `dm_quota_exhausted`·`dm_quota_warning` 을 프로에서 재현할 방법이 없습니다.

---

## 5. 못 만든 / 절반만 만든 상태

| 번호 | 무엇이 안 되나 | 왜 | 대안 |
|---|---|---|---|
| **A04** | 409 `INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE` 를 **실제로 띄우는 것** | IG 로그인은 실제 OAuth 가 필요한데, 목 로그인은 호출마다 **랜덤 IG user_id** 를 만들어 "이미 연결됨" 판정에 걸리지 않습니다. | **테스트에 쓰실 IG 계정을 알려주세요.** 그 계정의 `instagram_user_id` 를 A04 워크스페이스에 박아 두면, 그 계정으로 IG 로그인할 때 409 가 그대로 뜹니다. 바로 적용 가능합니다 — `{"codes":["A04"], "ig_user_id":"<id>"}` 로 재시드하거나 알려만 주시면 백엔드가 돌리겠습니다. |
| **A03** | 「Google 로 가입」 **상태 자체** | 서버에 구글 가입 표식 필드가 **없습니다**. 구글은 `kakao_id`/`instagram_user_id` 같은 식별자 없이 **email 로만** 매칭합니다 — 즉 구글로 가입한 계정과 이메일로 가입한 계정은 서버 상태가 같습니다. | 구글 로그인 **동작** 자체는 dev 에서 그대로 됩니다(`GOOGLE_CLIENT_ID` 설정돼 있음). 본인 구글 계정으로 `POST /api/v1/auth/google/` 을 쓰시면 신규 가입/기존 연결이 실제로 돕니다. 「구글로 가입한 사용자」를 화면에서 구분해야 한다면 **백엔드에 필드를 추가해야 합니다** — 필요하면 말씀 주세요. |
| **A02** | 카카오 **버튼** 흐름 | 실제 카카오 계정이 있어야 합니다. | 서버 상태(`kakao_id` 가 박힌 계정)는 만들어 뒀으니 화면 확인은 가능합니다. 버튼 흐름은 dev 에 카카오 키가 설정돼 있어 본인 카카오 계정으로 바로 되지만, 그러면 **새 계정**이 생깁니다(`kakao_id` 우선 매칭). |

그 밖에 **요청서와 다르게 만든 것 2가지**는 §2-3(오류 캠페인)과 §1-3(I06 이메일)에 적어 뒀습니다.
둘 다 "요청대로 하면 확인하려던 화면이 안 보인다"는 이유입니다.

---

## 6. 우선순위 7개 — 바로 보는 경로

| 번호 | 로그인 | 볼 것 |
|---|---|---|
| H01 | `home-h01@test.turnflow.link` | 새 게시물 큰 카드 · 리포트 배너(초록) · 알림 센터 · 자원 줄(자동 DM 7 + 링크 2) |
| H02 | `home-h02@test.turnflow.link` | 멈춤 칩 「4」 · 큰 카드는 `payment_failed` |
| H11 | `home-h11@test.turnflow.link` | 처음 가입한 사용자 홈 |
| H12 | `home-h12@test.turnflow.link` | 「자동 DM이 정상적으로 작동하고 있어요」 |
| P01 | `home-p01@test.turnflow.link` | 프로 체험 팝업 · 요금제 창(무료) |
| P02 | `home-p02@test.turnflow.link` | 가입 직후 계정 분석 팝업 · 「카드 등록하고 이어서 쓰기」 |
| I02 | `home-i02@test.turnflow.link` | 계정 전환 창 3상태 · 사진 없는 계정의 첫 글자 |

---

## 7. 백엔드 쪽 구현 위치 (참고)

| 파일 | 역할 |
|---|---|
| `apps/home/dev_seed.py` | 시드 작업대 — 계정 1개를 매번 같은 상태로 다시 만든다 |
| `apps/home/dev_seed_specs.py` | **상태 정의** — H/P/I/A 코드 1:1 빌더 + 레지스트리 |
| `apps/home/dev_views.py` | dev 전용 목록/재시드 API |
| `apps/home/management/commands/seed_home_test_accounts.py` | 관리 명령 |
| `apps/home/tests/test_dev_seed.py` | 회귀 테스트 — 알림 판정이 바뀌면 여기서 깨진다 |

기존 `seed_home_alerts_dev`(계정 4개)는 그대로 두되, **이 시더가 상위 호환**입니다.
