# [백엔드 → 프론트] 게시물 연령 제한 판정 API 확인 요청 6건 — 회신

2026-09-08 · 배포 완료 `428913b` (실서버 반영됨) · 계약 문서 개정본 `CAMPAIGN_RESTRICTION_CHECK_FRONTEND.md`

---

## 결론

**B1~B6 여섯 건 전부 실제 결함이 맞았습니다.** 전부 백엔드에서 고쳤고 실서버에 반영했습니다.
프론트에서 만드신 우회 코드(정규화 함수, 복사 버튼 잠금, `blocking` 무시)는 **전부 걷어내셔도 됩니다.**

특히 B5(복사 차단)는 저희 설계 실수였습니다. 복사본은 `INACTIVE` 로 생기니 한 건도 안 나가는데
막고 있었습니다. 지적 감사합니다.

| # | 지적 | 결과 | 프론트가 할 일 |
|---|---|---|---|
| B1 | 409의 `restriction` 값이 문자열(`"True"`·`"None"`) | 고침 | 정규화 함수 **삭제** |
| B2 | `suspected`인데 `blocking:true`·`can_create:false` | 고침 | 두 필드 **그대로 사용** |
| B3 | 실패 수가 시도 횟수라 "명"으로 못 씀 | 필드 추가 | `*_unique_users` 로 "N명" 표기 |
| B4 | 임계치가 뭔지 | 문서화 | 계약서 §11 |
| B5 | 복사가 409로 막힘 | **막지 않음** | 복사 버튼 **잠그지 마세요** |
| B6 | `?auto_paused=true` 무시, 개수 없음 | 고침 | 목록 조회 1회 절약 |

**새로 생긴 것이 하나 있습니다 — 활성화 게이트(§B5).** 이것만 추가 작업이 필요합니다.

---

## B1. 409 응답의 타입 — 200과 완전히 동일해졌습니다

### 원인

DRF 의 기본 예외 핸들러는 `exc.detail` 을 재귀적으로 훑으면서 **말단값을 전부 `ErrorDetail`(str 서브클래스)로
바꿉니다.** 그래서 `True → "True"`, `40 → "40"`, `None → "None"` 이 됐습니다. 저희가 dict 를 `detail` 에
넣은 것이 잘못이었습니다.

### 조치

예외 객체에 원본 payload 를 따로 달고, **DRF 핸들러를 타기 전에** 저희가 직접 응답을 만들도록 바꿨습니다.
같은 김에 200 과 409 가 **같은 직렬화 함수** 하나를 쓰도록 통일했습니다. 이제 두 응답의 `restriction`
블록은 글자 단위로 동일합니다.

### 실서버 실측 (dkrl520 계정, 실제 제한 게시물)

```
blocking            : True                              (bool)
failures            : 108                               (int)
last_success_at     : '2026-09-06T12:10:19.500437+00:00' (ISO str)
live_check_enabled  : False                             (bool)
user_message        : 있음
next_steps          : 4개
```

`user_message` · `how_to_check` · `next_steps` 도 409 에 함께 들어갑니다. 200 에서 쓰시던 배너
컴포넌트를 그대로 재사용하시면 됩니다.

> **왜 이 함정이 흔한가** — 저희 코드베이스에서 `detail` 에 dict 를 넣는 커스텀 예외는 전부 같은
> 문제를 갖고 있습니다. 다른 API 에서도 비슷한 걸 발견하시면 알려주세요.

---

## B2. `blocking` 이 실제 게이트와 어긋나 있었습니다

지적하신 그대로입니다. `suspected` 에 `blocking:true` 를 주면서 실제 생성은 201 로 통과시키고
있었습니다. **필드 이름만 보고 배선하면 정상 캠페인이 막히는** 상태였습니다.

`blocking` 은 이제 `state == "restricted"` 와 **동치**입니다.

| state | blocking | can_create_campaign | 실제 동작 |
|---|---|---|---|
| `ok` | false | true | 통과 |
| `suspected` | **false** | **true** | 통과 (🟡 경고 배너만) |
| `restricted` | true | false | 409 |
| `unknown` | false | true | 통과 |

`suspected` 를 막지 않는 것은 의도입니다. 오탐으로 멀쩡한 캠페인을 막는 손해가 더 큽니다.
경고만 띄우고 사용자가 진행하도록 두세요.

---

## B3. 고유 사용자 수 필드를 추가했습니다

`stats` 는 시도 횟수였던 게 맞습니다. 같은 사람에게 재시도한 건이 중복으로 세어졌습니다.

```json
"stats": {
  "opening_success": 20,
  "opening_failed_2534066": 66,
  "opening_success_unique_users": 20,
  "opening_failed_unique_users": 66
}
```

**"N명" 표기에는 `*_unique_users` 를 쓰세요.** 기존 두 필드는 시도 횟수 그대로 유지합니다
(다른 화면이 쓰고 있어 의미를 바꾸지 않았습니다).

---

## B4. 판정 임계치 — 계약서 §11 에 전부 적었습니다

관찰하신 게 정확합니다. **축은 "총 실패 수"가 아니라 "마지막 성공 이후 실패 수"** 입니다.
그래서 성공이 섞인 게시물은 실패 16건이어도 `ok` 로 나옵니다.

| 판정 | 조건 |
|---|---|
| `restricted` | 총 실패 5건 이상 **그리고** 마지막 성공 이후 실패 5건 이상 |
| `suspected` | 최근 24시간 실패 3건 이상 **그리고** 그 24시간 성공 0건 |
| `unknown` | 판정할 이력 없음, **또는 마지막 실패가 14일 초과** |

**화면에 숫자를 직접 쓰지는 마세요.** 인스타 정책이라 저희가 언제든 조정합니다. CS 답변 근거로만
쓰시고, 실제 수치가 필요하면 응답의 `evidence.history` 에 그대로 들어 있습니다.

`suspected` 임계(3)가 `restricted` 임계(5)보다 낮은 것은 의도입니다. 같으면 확정이 항상 먼저 떠서
조기경보가 뜰 틈이 없습니다.

---

## B5. ★ 복사는 허용합니다 — 대신 **활성화**에서 막습니다

여기가 이번 변경의 핵심입니다.

지적하신 대로 복사본은 항상 `INACTIVE` 로 생기므로 그 자체로는 발송이 0건입니다. 복사해서 게시물만
바꿔 쓰는 정상 흐름도 있습니다. **복사 게이트를 뺐습니다.**

대신 실제로 발송이 시작되는 지점을 막습니다.

```
복사        → 201 (INACTIVE)              막지 않음
게시물 교체  → 200                         막지 않음
활성화      → 409 (제한 게시물 그대로일 때)  ← 여기
```

### 새로 409 가 나는 곳 4개

| 엔드포인트 | 동작 |
|---|---|
| `POST /auto-dm-campaigns/{id}/resume/` | 409 |
| `PATCH /auto-dm-campaigns/{id}/` `status=active` | 409 |
| `POST /auto-dm-campaigns/{id}/schedule/` `activate=true` | 409 |
| `POST /auto-dm-campaigns/bulk-resume/` | **409 아님** — 건별 실패 |

일괄 재개는 전체를 실패시키지 않고 건별로 담습니다.

```json
{ "succeeded": [], "failed": [{ "id": "…", "reason": "media_content_restricted" }], "revive_queued": 0 }
```

> 이전 계약서 §4 표에 `POST /auto-dm-campaigns/bulk/` `op=resume` 라고 적었는데 **오기**였습니다.
> 실제 경로는 `bulk-resume/` 이고 본문은 `{"ids": [...]}` 입니다. 그대로 호출하면 405 가 납니다.
> 문서 고쳤습니다.

### 빠져나갈 길이 같이 들어갔습니다 — 증거 유효기간 14일

활성화를 막으면서 교착이 하나 생깁니다. 판정은 발송 이력에서 나오는데, 재개를 막으면 새 성공 이력이
생길 수 없어 **인스타가 제한을 풀어줘도 영원히 `restricted`** 로 남습니다.

그래서 **마지막 실패가 14일을 넘으면 `unknown`** 으로 물러섭니다. 재개가 됩니다. 실제로 아직
막혀 있으면 재개 직후 실패가 다시 쌓여 자동 정지되므로 스스로 교정됩니다.

프론트에서 특별히 하실 일은 없습니다. 다만 **"영구 차단" 같은 문구는 쓰지 말아 주세요.**

---

## B6. `?auto_paused=` 필터 + 개수

필터가 무시되던 게 맞습니다. `auto_paused_at` 이 불리언이 아니라 nullable datetime 이라 기존
불리언 필터 루프에 안 걸렸습니다. 별도로 처리했습니다.

```
GET /auto-dm-campaigns/?workspace_id=…&auto_paused=true    자동 정지된 것만
GET /auto-dm-campaigns/?workspace_id=…&auto_paused=false   나머지 전부
GET /auto-dm-campaigns/summary/?workspace_id=…
  → counts: { "active": 4, "paused": 3, "inactive": 0, "total": 7, "auto_paused": 1 }
```

**`auto_paused` 는 `paused` 의 부분집합입니다.** `total` 에 더하지 마세요. 위 예시에서 paused 3건
중 1건이 자동 정지입니다.

---

## 개발 서버 확인용 시드

새 동작을 다 볼 수 있게 케이스를 갱신했습니다. `restriction@test.com` / `Test1234!`

| 캠페인 | 기대 |
|---|---|
| 정상 발송 캠페인 | 배지 없음 |
| 제한 확정 (활성) | 🔴 배너. **복사하면 201** |
| 자동 정지된 캠페인 | 🔴 자동정지 배지. **재개하면 409** |
| 제한 의심 | 🟡 경고만. 활성화 허용 |
| 실패 섞였지만 정상 | 배지 없음 (오탐 확인용) |
| 제한 확정 · 수동 정지 | **재개하면 409** — 자동정지가 아니어도 막힙니다 |
| 옛날에 제한 · 증거 만료 | **재개하면 200** — 14일 규칙 확인용 |

캠페인 없는 media 2개도 있습니다. 생성 게이트용입니다.

```
17900000000000007  → 생성 시 409 media_content_restricted
17900000000000008  → 생성 시 201 (suspected 는 경고만)
17900000000000006  → 생성 시 201 (이력 없는 새 게시물)
```

재시드: `docker compose exec -T web python manage.py shell < scripts/seed_dev_restriction_cases.py`
(멱등입니다. 실행 끝에 URL 과 id 가 전부 출력됩니다.)

---

## 검증 결과

개발 서버 e2e 8건, 실서버 실계정 6건 전부 통과했습니다.

```
[PASS] 복사 (제한 게시물)          201, status=inactive
[PASS] 재개 (수동 정지·제한)        409 media_content_restricted
[PASS] 재개 (자동 정지·제한)        409
[PASS] 재개 (증거 14일 만료)        200
[PASS] 일괄 재개                  200 + failed[].reason
[PASS] ?auto_paused=true         1건
[PASS] counts.auto_paused        1
[PASS] suspected 생성 허용         can_create=true, blocking=false
```

단위 테스트 36건, integrations 전체 749건 통과.

---

## 남은 것 (백엔드 쪽)

자동 정지 배치는 **아직 꺼둔 상태**입니다. 켜면 실서버 캠페인 13개가 한 번에 정지되어서, 해당 고객
공지와 CS 안내가 나간 뒤에 켤 예정입니다. 켜는 시점은 따로 공유드리겠습니다.

그때까지 `auto_paused_at` 은 개발 서버에서만 채워집니다. **UI 는 미리 만들어두셔도 됩니다.**
