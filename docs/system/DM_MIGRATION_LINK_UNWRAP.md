# DM 이전 — 타사 래퍼 링크를 원본으로 되돌리기

**2026-08-19 · 코드: `apps/integrations/dm_migration/links.py`**
관련: `docs/frontend/DM_CAMPAIGN_MIGRATION_FRONTEND.md`(기능 계약) ·
`docs/frontend/DM_MIGRATION_VISIBLE_BANDS_2026-08-18.md`(무엇을 고객에게 넘기나)

---

## 1. 왜 하나

타사 DM 도구(매니챗·인포크링크·NHN 소셜비즈·리틀리)는 사장님이 넣은 링크를 **자기 도메인으로
감싸서** 보낸다. DM 에서 복원한 링크를 그대로 옮기면 이전된 캠페인이 남의 서비스에 계속
묶인다.

| 문제 | 결과 |
|---|---|
| 사장님이 그 도구를 해지 | 우리가 보낸 DM 의 링크가 **죽는다** |
| 클릭이 그 도구 통계로 흐름 | 우리 지표에 안 남고, 남의 대시보드에 쌓인다 |
| 소셜비즈 래퍼에 수신자 id 가 박혀 있음 | 한 사람 링크를 옮기면 **전원에게 그 한 사람의 링크**가 나간다 |

**원칙 — 되돌리기가 실패하면 원래 링크를 그대로 쓴다.** 링크가 바뀌는 것보다 링크가
없어지는 게 나쁘다. 모든 실패 경로(타임아웃·404·형식 변경·상한 도달·플래그 OFF)가 원본을
유지한다.

## 2. 실측 — prod 후보 1,597건 / 고유 URL 425개 (2026-08-18)

| 호스트 | 후보 수 | 되돌리는 방법 | 조회 |
|---|---|---|---|
| `socialbiz-c.nhndata.com` | 591 | 302 `Location` (uuid 75개) | 필요 |
| `link.inpock.co.kr` | 522 | `?url=` 파라미터 | **0** |
| `my.manychat.com` | 118 | 본문 `<a href>` (act 23개) | 필요 |
| `litt.ly` | 92 | 경로 JWT 페이로드의 `url` | **0** |
| `l.instagram.com` | 6 | `?u=` 파라미터 | **0** |
| 그 외(notion·bit.ly·buly.kr 등) | 268 | **손대지 않는다** — 사장님 본인 링크 | — |

고유 URL 425개 기준: **오프라인 196 · 조회 159(고유 키 98) · 그대로 70.**
조회 실측 **98회 · 24.7초(회당 0.25초)**, 2회차는 캐시로 **0.02초 · 0회**.

## 3. 두 층으로 나눈 이유 — 값이 링크 안에 있으면 두드리지 않는다

### ① 오프라인 `links.unwrap_url(url)` — 호출 0

- 파라미터 래퍼: 호스트별 지정 키(`url`/`u`) + 일반 키(`redirect`/`target`/`to`…).
  일반 키는 **값이 다른 호스트의 절대 URL 일 때만** 인정한다 — 그냥 `?to=cart` 가 있는
  정상 랜딩 페이지를 리다이렉터로 오인하지 않도록.
- JWT 래퍼(리틀리): 경로 토큰의 **페이로드 클레임 `url`** 을 읽는다. 서명은 검증하지
  않는다(남의 토큰이고, 읽은 값이 http(s) 절대 URL 인지 다시 확인한다).
- **재귀** — 리틀리가 인스타 래퍼를 또 감싼 실물이 있다(`litt.ly → l.instagram → 목적지`).
  최대 `MAX_HOPS=4`, 방문한 URL 은 재방문 금지(순환 방지).

### ② 네트워크 `links.Resolver` — 래퍼 **키**마다 1회

| 도구 | 두드릴 URL | 읽는 곳 |
|---|---|---|
| 소셜비즈 | `recipientId=0` 으로 바꿔서(없으면 붙여서) | 302 응답의 `Location` |
| 매니챗 | 원본 그대로 | 200 본문의 `<a href>` — `mcp_token=` 이 붙은 것 우선 |

- **`recipientId=0`** 으로 바꾸는 이유: 실제 수신자 id 로 두드리면 사장님의 타사 통계에
  우리 조회가 클릭으로 섞인다.
- 캐시 키가 **URL 이 아니라 래퍼 키**(`socialbiz:<uuid>` / `manychat:<act>`)다. 소셜비즈는
  수신자마다 URL 이 달라서, URL 로 캐시하면 591회를 두드린다 → 75회로 줄어든다.
- 실패도 1시간 캐시한다 — 죽은 래퍼를 계정마다 다시 두드리지 않는다.
- 성공은 30일 캐시. **계정 간에도 공유**되므로 두 번째 계정부터는 대부분 0회다.

## 4. 추적 파라미터만 뗀다 — 제휴 코드는 남긴다

| 뗀다 (남의 도구가 붙인 것) | 남긴다 (사장님 것) |
|---|---|
| `mcp_token`(매니챗 — pid/sid 담김) · `recipientId` · `subscriber_id` · `psid` | `refCode` · `sourceId` · `utm_*` · `source=copy_link` 등 나머지 전부 |

⚠️ **제휴 코드를 떼면 사장님 수익이 사라진다.** 화이트리스트가 아니라 **블랙리스트**인 이유다.
뗄 게 하나도 없으면 문자열을 **그대로** 돌려준다 — 재인코딩으로 링크 모양이 괜히 바뀌면
사용자가 "우리가 링크를 바꿨다" 고 의심한다.

## 5. 어디서 도나

```
recover._pack        DM 에서 링크를 발견 (래퍼 그대로)
        ↓
pipeline._resolve_links      ← 후보 만들기 직전, 트랜잭션 **밖에서**
        ↓                      (DB 커넥션 붙잡고 남의 서버를 기다리면 안 된다)
_create_candidate
        offer_url                     = 되돌린 목적지   ← 사용자에게 나가는 것
        draft_opening_message         = 본문 안 래퍼도 치환
        gate_message                  = 같음
        matched_template.recovered_url = **관측한 래퍼 그대로**  ← 근거
        matched_template.resolved_url  = 되돌린 값(안 바뀌면 "")
```

- **본문도 바꾼다.** 버튼만 바꾸고 본문을 놔두면 한 DM 안에서 두 링크가 갈린다
  ("자료는 https://... 에서" 형태 캠페인이 많다). 게이트 문구까지 훑는다.
- `matched_template.recovered_url` 은 **건드리지 않는다.** 근거는 관측값이어야 하고,
  되돌리기가 나중에 틀렸다고 밝혀져도 원본이 있어야 복구할 수 있다.
- `stage_data["link_map"]` 에 `{원본: 최종}` 을 체크포인트로 남긴다.

### ⚠️ 이 단계는 슬라이스를 접지 않는다

`_SliceExhausted` 를 올리면 **슬라이스 1개당 링크 1개**만 처리하고 상한(`MAX_SLICES`)을
태워서, 링크 때문에 잡이 미완으로 끝난다(슬라이스 테스트가 실제로 이 사고를 잡았다).
대신 이 단계에 자체 예산 `LINK_RESOLVE_SECONDS=180` 을 두고, 넘기면 남은 링크는 원본을
유지한다. 넘겨도 `SLICE_SECONDS(1200) + 180 < 태스크 하드 한도(1800)` 라 안전하다.

## 6. 소급 — 이미 만든 후보 고치기 (재수집 0)

```bash
# 미리보기 (아무것도 쓰지 않는다)
manage.py dm_migration_resolve_links --job <job_id>
manage.py dm_migration_resolve_links --username highestlevel33

# 반영
manage.py dm_migration_resolve_links --username highestlevel33 --apply

# 조회 없이 파라미터·JWT 로 풀리는 것만
manage.py dm_migration_resolve_links --job <job_id> --offline-only --apply
```

몇 시간 걸린 수집·판정은 그대로 두고 **산출물만** 손질한다(`dm_migration_regrade` 와 같은
원칙: 다시 살 필요가 없는 것은 다시 사지 않는다). **두 번 돌려도 안전하다**(멱등).

## 7. 설정

| 이름 | 기본 | 뜻 |
|---|---|---|
| `DM_MIGRATION_RESOLVE_LINKS` | `True` | False 면 조회를 멈추고 **오프라인으로 풀리는 것만** 되돌린다 |
| `DM_MIGRATION_LINK_FETCH_MAX` | `300` | 잡 1건의 조회 상한(실측 필요량 98). 넘으면 원본 유지 |
| `LINK_RESOLVE_SECONDS` (코드) | `180` | 이 단계의 시간 예산 |

## 8. 새 도구를 추가할 때

1. **먼저 실데이터를 센다** — 어떤 호스트가 몇 건인지. 상상한 포맷으로 만들면 안 맞는다.
2. 링크 안에 목적지가 있나 확인 → 있으면 `_PARAM_WRAPPERS` / `_JWT_WRAPPERS` (호출 0).
3. 없으면 한 번 두드려 보고 302 `Location` 인지 본문 링크인지 확인 → `_SOCIALBIZ_HOSTS`
   방식(302) 또는 `_MANYCHAT_HOSTS` 방식(HTML) 중 맞는 쪽에 붙인다.
4. **캐시 키를 무엇으로 묶을지** 정한다(`cache_key_for`). 수신자마다 URL 이 다른 도구는
   URL 로 캐시하면 조회가 수백 회가 된다.
5. 그 도구가 목적지에 붙이는 추적 파라미터를 `_TRACKERS` 에 넣는다. **사장님의 제휴
   코드와 구분**할 것.
6. 테스트는 `apps/integrations/test_dm_migration_links.py` 에 **실데이터에서 뽑은 모양**으로
   추가한다.

⚠️ 판정 로직(`recover`)에 영향이 없으므로 `cache.RULES_VERSION` 은 올리지 않아도 된다 —
링크 되돌리기는 등급을 바꾸지 않는다. 단 `can_auto`(링크 있어야 자동채택)는 **되돌리기 전
원본 URL** 로 판정하므로, 되돌리기 실패가 자동채택을 취소하지도 않는다.
