# 어드민 18차 요청 회신 (SNAP-1 · SNAP-2) — 전체 현황 타일 명단

작성 2026-08-10 · 백엔드 → 어드민 콘솔팀
요청서: `10_turnflow_admin/docs/ADMIN_API_REQUESTS.md` 18차

**표에 적힌 2건(SNAP-1, SNAP-2) 모두 구현했습니다.** 본문 개수도 2건으로 일치했습니다(누락 없음).
공통 요청 5건(①항등 ②캐시 ③정렬 400 ④page_size ⑤권한)도 전부 반영했고, ②는 **1번안(같은 캐시를
읽는다)** 을 택했습니다.

| 번호 | 엔드포인트 | 상태 |
|---|---|---|
| SNAP-1 | `GET /api/v1/admin/snapshot/paying/` | ✅ 구현 |
| SNAP-2 | `GET /api/v1/admin/snapshot/trial/` | ✅ 구현 |

> "타일을 만드는 그 코드가 명단도 만들어야 한다"는 진단이 정확합니다. 모수 판정을
> `apps/admin_api/snapshot_rosters.py` 한 파일로 뽑고, **마케팅 대시보드의 `_snapshot` /
> `_trial_now` 가 그 함수들을 호출하도록 바꿨습니다.** 두 곳에 같은 조건을 복제한 게 아니라
> 한 곳을 공유합니다 — 한쪽만 고쳐서 갈라지는 경로가 없습니다.

---

## SNAP-1. `GET /api/v1/admin/snapshot/paying/`

표준 `PageNumberPagination` + top-level `as_of` · `is_live`.

```jsonc
{
  "count": 58,
  "next": "...", "previous": null,
  "as_of": "2026-08-10T19:40:02+09:00",   // 집계 기준 시각 (타일의 snapshot.as_of 와 동일 값)
  "is_live": false,                        // §캐시 참고
  "results": [
    {
      "user_id": 1042,                     // /admin/users/{id}/ 이동 키
      "email": "grower@example.com",
      "full_name": "김성장",
      "plan_name": "pro",
      "plan_display_name": "프로",
      "monthly_amount": 24800,             // UserSubscription.renewal_amount (서버 계산)
      "extra_ig_accounts": 1,
      "last_paid_at": "2026-08-05T09:12:44+09:00",
      "next_billing_at": "2026-09-04T09:12:44+09:00",
      "paid_count": 3,
      "date_joined": "2026-05-21T11:02:10+09:00"
    }
  ]
}
```

요청하신 필드에 **`extra_ig_accounts` 하나를 추가**했습니다 — `monthly_amount` 가 24,800원인
이유(14,900 + 9,900×1)를 화면에서 설명할 수 없으면 "왜 프로가 24,800원이냐"는 문의가 백엔드로
오게 됩니다. 쓰지 않으셔도 됩니다.

### `monthly_amount`

`UserSubscription.renewal_amount` **프로퍼티 값 그대로** 입니다. 요청대로 서버 계산값만 씁니다.
이 값에는 세 규칙이 이미 반영돼 있습니다:

- 가입 시점 가격 스냅샷(그랜드파더링) — 현재 판매가가 아니라 그 회원의 계약 가격
- 추가 IG 계정 `+9,900원 × N` (프로만)
- 리텐션 할인 대기 중이면 **다음 1회 50%** 반영
- 다운그레이드/추가계정 축소가 예약돼 있으면 그 예약값 기준

### `paid_count` — 정의 회신

> "저희는 `status=paid` 인 건수(환불된 건 제외)로 적을 생각인데, 환불 건을 어떻게 세는 게
> 맞는지는 그쪽 정의를 따르겠습니다."

**그 정의가 맞습니다.** 다만 우리 데이터 구조에서 그게 자동으로 성립하는 이유를 적어둡니다:

- 전액 환불은 **원래 행의 `status` 를 `refunded` 로 뒤집습니다**(새 행을 만들지 않음) →
  `status=paid` 필터에서 자동 제외됩니다. 별도 제외 로직이 필요 없습니다.
- 부분취소는 **음수 금액의 별도 행**을 만듭니다 → `amount > 0` 조건으로 제외했습니다.

즉 `paid_count` = `status=paid AND amount>0` 인 건수. "실제로 돈이 들어와 있는 결제 횟수"입니다.

### 필터 / 정렬

| 파라미터 | 동작 |
|---|---|
| `?plan=` | 플랜 코드명 (칩) |
| `?search=` | 이메일·이름 부분일치 |
| `?ordering=` | `last_paid_at` · `next_billing_at` · `monthly_amount` · `date_joined` (± 부호). **기본 `-last_paid_at`**(최근 결제 최신순) |
| `?page_size=` | 기본 20, **상한 500** |

요청하신 정렬 4개 전부 열었습니다. tie-break 로 pk 를 붙여 동률 시 페이지 경계에서 행이
중복/누락되지 않습니다.

---

## SNAP-2. `GET /api/v1/admin/snapshot/trial/`

```jsonc
{
  "count": 21,
  "as_of": "2026-08-10T19:40:02+09:00",
  "is_live": false,
  "results": [
    {
      "user_id": 1188,
      "email": "trialer@example.com",
      "full_name": "박체험",
      "plan_name": "pro",
      "plan_display_name": "프로",
      "trial_started_at": "2026-07-12T10:00:00+09:00",
      "trial_ends_at": "2026-08-25T10:00:00+09:00",
      "trial_total_days": 44,
      "bucket": "will_charge",
      "expected_amount": 14900,
      "conversion_consent_required": true,
      "card_company": "현대",
      "card_number_masked": "433012******123*",
      "date_joined": "2026-07-12T09:58:03+09:00"
    }
  ]
}
```

### `trial_started_at` — 요청서와 축이 다릅니다 (⚠️ 확인 부탁)

> 요청서: `trial_started_at` | 체험 시작 (**`trial_used_at`**)

**`current_period_start` 를 씁니다.** `trial_used_at` 은 "카드등록 무료 체험은 1인 1회"를
막는 **어뷰징 방어용 내구 필드**라 다운그레이드에도 지워지지 않습니다. 재체험한 회원은 이 값이
**과거 체험의 시작 시각**으로 남아 있어, 그 값으로 기간을 재면 "체험 90일" 같은 행이 나옵니다.
`current_period_start` 는 이번 체험 기간의 시작이라 `trial_ends_at` 과 짝이 맞습니다.

같은 이유로 **`trial_total_days`** 를 추가했습니다(`trial_ends_at − trial_started_at`, 반올림).
쿠폰 연장 체험이면 44 로 나옵니다 — 30 과 44 를 구분해서 봐야 하는 이유가 아래에 있습니다.

### `trial_ends_at` = `current_period_end` — 회신 확인

USR-2 회신대로 맞습니다. 이 값이 곧 **첫 결제 시각**입니다.

### `bucket` — 서버 판정이 정본

15차의 판정(`cancelled_during_trial_at >= current_period_start`)을 그대로 씁니다. 다만
**행의 `bucket` 은 라이브 재판정이 아니라 타일이 만든 매핑에서 읽습니다** — 캐시 창(15분)
안에 누군가 취소해도 `?bucket=will_charge` 의 count 가 타일과 어긋나지 않습니다(§캐시 참고).

### `expected_amount`

`renewal_amount` 서버 계산값. **`bucket=cancelled` 면 `null`** 입니다 — 취소자는 과금되지
않으므로 금액을 주면 "결제 예정"으로 오독됩니다.

### `conversion_consent_required` — 새 필드 (오늘 추가된 기능)

같은 날 프론트(유저 콘솔)팀 요청으로 **유료전환 2차 동의**가 들어갔습니다. 전자상거래법
제13조 제6항 + 시행령 제20조의2 에 따라, **체험이 30일을 초과하는 회원**(= 쿠폰 연장 체험)은
첫 결제 전에 동의를 한 번 더 받아야 하고, **동의가 없으면 결제하지 않고 무료 플랜으로
전환**됩니다.

이 필드가 `true` 인 행은 **"지금 상태로 체험이 끝나면 매출이 발생하지 않는" 회원**입니다.
`bucket=will_charge` 인데 이 값이 `true` 인 사람은 운영에서 미리 안내할 대상입니다
(D-14/D-3 자동 메일이 나가지만, 명단에서도 보이는 게 나을 것 같아 넣었습니다).

30일 체험자는 항상 `false` 입니다(결제 화면 동의로 요건 충족).
상세: `docs/frontend/PAYMENT_CONSENT_FRONTEND.md`

### 모수 — `no_card` 제외 (요청서 정의대로)

```
count == trial_now.will_charge + trial_now.cancelled
```

⚠️ **`trial_now.total` 과는 다릅니다.** `total` 에는 `no_card`(쿠폰 무카드 체험, prod 실측 9명)가
포함되지만 이 명단은 카드 등록 체험자만 담습니다. 요청서 정의와 같고, 화면에서 타일 숫자로
`trial_now.total` 을 쓰고 있으면 명단 count 와 어긋납니다 — 타일에는
**`will_charge + cancelled`** 를 쓰거나, `no_card` 를 별도 배지로 빼 주세요.

### 필터 / 정렬

| 파라미터 | 동작 |
|---|---|
| `?bucket=` | `will_charge` / `cancelled` (칩). 허용값 밖은 **400** |
| `?search=` | 이메일·이름 부분일치 |
| `?ordering=` | `trial_ends_at` · `trial_started_at` · `date_joined` (± 부호). **기본 `trial_ends_at`**(종료 임박순) |
| `?page_size=` | 기본 20, 상한 500 |

원본 카드번호·빌링키는 응답에 없습니다(마스킹된 표시값만).

---

## 공통 ① 항등 — 테스트로 고정했습니다

```
SNAP-1 count                  == snapshot.paying.total
SNAP-1 ?plan=X count          == snapshot.paying.by_plan[X].count
SNAP-2 count                  == trial_now.will_charge + trial_now.cancelled
SNAP-2 ?bucket=will_charge    == trial_now.will_charge
SNAP-2 ?bucket=cancelled      == trial_now.cancelled
```

`apps/admin_api/tests_snapshot_rosters.py` 가 **대시보드 응답을 실제로 호출해서** 타일 숫자와
명단 count 를 대조합니다(하드코딩 상수 비교가 아님). 판정 쿼리를 일부러 망가뜨려(will_charge 가
카드 유무를 안 보게) 돌려본 결과 이 테스트가 실패하는 것까지 확인했습니다 — 회귀를 잡습니다.

---

## 공통 ② 캐시 — **1번안 채택** (명단도 같은 캐시를 읽습니다)

선호하신 1번안으로 갔습니다. 구현은 제안하신 것과 같습니다:

**타일을 계산할 때 그 행들의 id 를 캐시 항목에 함께 담고**(`admin:dash:mkt:snapshot`, TTL 900초),
명단은 그 집합 위에서 페이지네이션합니다. 그래서 `타일 숫자 == 명단 count` 가 **캐시 지연과
무관하게 항상** 성립합니다. 15분 창에 58 vs 59 가 생기는 구간이 없습니다.

한 가지 더 얼렸습니다: **id 만이 아니라 축까지** 함께 저장합니다
(paying → `{id: plan_name}`, trial → `{id: bucket}`). 이유는 `?plan=` · `?bucket=` 의 **부분합**
때문입니다. id 집합만 얼리고 축을 라이브로 읽으면, 캐시 창 안에 체험자가 취소하는 순간
`?bucket=will_charge` 가 1 줄어들어 **칩 숫자와 명단이 어긋납니다**. 축까지 얼리면 부분합도
타일과 정확히 일치합니다.

- **`as_of`** 는 그 집합이 계산된 시각이고, 타일 응답의 `snapshot.as_of` 와 **같은 값**입니다.
  화면 우측 상단에 그대로 쓰시면 됩니다.
- **`is_live`** 는 폴백 표시입니다. 집합이 상한(5,000)을 넘으면 캐시에 담지 않고 명단을
  라이브로 계산합니다 — 그때 `is_live: true` 이고 `as_of` 가 지금 시각이 되어, 타일과 다를 수
  있다는 게 시각 차이로 드러납니다(=2번안으로 자동 강등). 현재 규모(수십~수백)에서는 항상
  `false` 입니다. Redis 를 DM 발송 등 다른 기능과 공유하기 때문에 무한히 커지는 캐시 항목을
  만들 수 없어 둔 안전장치입니다.
- `?refresh=1` 도 동작합니다(최고 관리자 전용). 대시보드와 **같은 캐시 항목**을 재계산하므로,
  명단에서 새로고침하면 타일도 함께 갱신됩니다.

### 남는 한 가지 (작지만 적어둡니다)

캐시 창 안에 회원이 **탈퇴(계정 삭제)** 하면 그 id 의 행이 사라져 명단 count 가 타일보다 1 작게
나올 수 있습니다. 얼린 id 집합에서 실제 행을 읽어오기 때문입니다. 이 경우는 `as_of` 로 설명되는
범위이고, 반대 방향(명단이 더 큼)은 발생하지 않습니다.

---

## 공통 ③ 정렬 화이트리스트 밖 → 400

요청대로 **조용히 무시하지 않습니다.**

```jsonc
GET /admin/snapshot/paying/?ordering=-last_sent_at
400 {
  "success": false,
  "error": {
    "code": 400,
    "message": "ordering 값이 올바르지 않습니다: '-last_sent_at'",
    "details": {
      "field": "ordering",
      "allowed": ["-date_joined","-last_paid_at","-monthly_amount","-next_billing_at",
                  "date_joined","last_paid_at","monthly_amount","next_billing_at"]
    }
  }
}
```

`details.allowed` 를 함께 주므로 프론트에서 허용 목록을 하드코딩하지 않아도 됩니다.
`?bucket=` 도 같은 방식으로 400 + `details.allowed` 입니다.

> 말씀하신 캠페인 목록의 `ordering=-last_sent_at` 조용한 무시는 **이 두 엔드포인트 밖의 별건**
> 입니다(`AdminDMRecipientListView` 는 지금도 허용값 밖이면 기본 정렬로 되돌립니다). 그 쪽도
> 400 으로 바꾸는 게 맞다고 보는데, 이미 프론트가 그 값을 보내고 있는 상태라 바꾸는 순간 그
> 화면이 400 을 받습니다. **다음 라운드에 프론트 수정과 함께 묶어서 처리**하는 게 안전할 것
> 같습니다 — 원하시면 요청 주세요.

---

## 공통 ④ CSV 용 `page_size`

`?page_size=` 열었습니다. **상한 500** (요청하신 값). 상한을 넘겨 보내면 400 이 아니라 500 으로
클램프합니다 — CSV 내보내기 중에 400 으로 끊기는 것보다 낫다고 판단했습니다.

지금 규모(58명)는 `?page_size=500` 한 번으로 전부 받습니다.

---

## 공통 ⑤ 권한 — 예상대로 자동 403 입니다 (확인 완료)

`/api/v1/admin/snapshot/**` 는 RBAC 화이트리스트(`me/` · `dashboard/marketing/` ·
`marketing/channel-links/`)에 없으므로 `AdminRoleGuardMiddleware` 가 **deny-by-default** 로
막습니다. 새 경로라 확인 요청하신 것 맞게, 테스트로 고정했습니다:

```
marketing_viewer → 403  {"error": {"details": {"code": "section_forbidden", ...}}}
비스태프         → 403
비로그인         → 401
```

차단은 `AdminActionLog` 에 `admin.access_denied` 로 남습니다.

**이메일 마스킹(RBAC-3)은 적용하지 않았습니다** — 요청대로입니다. 파트너가 볼 수 없는 화면이고,
마스킹하면 회원 식별이 안 돼 명단의 용도가 사라집니다. `apply_pii_policy` 를 이 두 응답에는
통과시키지 않습니다.

---

## 배포 메모

| 항목 | 값 |
|---|---|
| 신규 파일 | `apps/admin_api/snapshot_rosters.py`(모수 단일 소스) · `views/snapshot.py` · `serializers/snapshot.py` |
| 변경 | `views/dashboard_marketing.py` — `_snapshot` / `_trial_now` 가 위 모수 함수를 호출하도록 (타일 숫자 자체는 불변) |
| 마이그레이션 | **없음** (조회 전용). 단 같은 배포에 `billing 0025` · `emails 0008` · `core 0014` 가 함께 들어갑니다(유료전환 동의 기능) |
| 테스트 | `apps/admin_api/tests_snapshot_rosters.py` 24건. 기존 `tests_dashboard_marketing.py` · `tests_rbac_and_spam.py` · `tests_admin_16th_round.py` 259건 회귀 없음 |
| Swagger | `/api/docs/` 에 `admin-dashboard` 태그로 노출 (파라미터·응답 예시 포함) |

배포 직후 확인 권장: 대시보드를 한 번 열어 스냅샷 캐시를 만든 뒤 두 명단을 열어 `count` 와
타일 숫자, `as_of` 일치를 육안으로 대조.

---

## 프론트가 알아서 하시는 것 — 이견 없습니다

타일 전체 클릭 영역(투명 링크 오버레이) · 하단 칩 → 필터된 명단 · **명단에 기간 컨트롤 두지
않음**(전체 현황은 기간 무관 현재값이므로 맞습니다) · 행 전체 클릭 · 마케팅 파트너 화면 불변.

`bucket` 칩(`프로 41` · `취소 3`)에 쓰실 숫자는 타일의 `trial_now.will_charge` /
`trial_now.cancelled` 이고, 명단의 `?bucket=` count 와 정확히 같습니다.
