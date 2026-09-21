# [백엔드 회신 · 2026-09-21] dev AI 잡 워커 복구 + 더미 연결 게시물 목 지원

두 건 다 처리했습니다. **지금 바로 테섭에서 캠페인 만들기 흐름 끝까지 됩니다.**
작업 중에 **같은 원인으로 다음 단계에서 또 막힐 자리 하나**를 더 찾아 같이 막았습니다(§3).

| # | 요청 | 상태 |
|---|---|---|
| 1 | dev AI 잡 소비 워커 | ✅ **복구** — 안 떠 있던 게 아니라 **크래시 루프**였습니다 |
| 2 | 더미 연결 게시물 목 | ✅ **적용** — 목록 · id 배치 조회 둘 다 |
| 3 | (추가) `ai-suggest` 게시물 조회 | ✅ **같이 막음** — 안 했으면 고르는 순간 404 |

주신 `job_id bcb8ae6f-d76d-4a30-aa94-5bfdb373f4c8` 는 워커를 살리자마자
**16:40:04 에 succeeded(progress 100)** 로 끝났습니다. 적체 큐도 0건입니다.

---

## 1. AI 잡 워커 — 죽어 있던 게 아니라 **7,103번 재시작 중**이었습니다

```
instagram_backend_celery        Restarting (1)     restarts=7103
instagram_backend_celery_beat   Restarting (1)
```

크래시 원인:

```
File "/app/apps/admin_api/auth/totp.py", line 26, in <module>
    import pyotp
ModuleNotFoundError: No module named 'pyotp'
```

어드민 2단계 로그인(TOTP) 때 들어온 `pyotp`·`qrcode` 가 워커 컨테이너에 없었습니다.
Celery 는 기동할 때 URLConf 를 통째로 임포트하므로, **어드민 URL 하나가 못 열리면
워커 전체가 못 뜹니다.** 그래서 `ai_jobs` 큐 설정은 멀쩡한데 소비자가 0 이었습니다
(= 202 는 잘 받고 영원히 `queued`).

### 왜 web 은 멀쩡했나 — 이게 진짜 원인입니다

컨테이너 3개가 전부 **2026-08-03 옛 이미지**로 떠 있었고, 누군가 **web 컨테이너 안에서만**
`pip install` 로 때워놨습니다. 컨테이너 쓰기 레이어는 컨테이너마다 따로라 celery 엔 없었습니다.

```
recreate 전 :  web / celery / beat  →  85f9af4b (2026-08-03)   ← 태그보다 옛날
태그가 가리킨 것 :                      c52da8f7 (2026-08-16)   ← pyotp 있음
```

**세 컨테이너를 현재 이미지로 재생성**했습니다(재빌드 불필요 — 이미지엔 이미 있었음).

```
web / celery / beat  →  c52da8f7  (전부 동일)
dev-api /healthz     →  200
pyotp                →  이미지에서 제공(수동 설치 아님)
```

> ⚠️ web 도 같이 재생성한 이유: 그대로 뒀으면 **PC 를 재부팅하는 순간 수동 설치분이
> 날아가 dev-api 가 celery 와 똑같이 죽습니다.** 지금 고쳐두는 게 맞다고 봤습니다.
> 그 과정에서 dev-api 가 20초쯤 끊겼습니다 — 혹시 그 사이 요청이 실패했다면 그 때문입니다.

---

## 2. 더미 연결 게시물 — 목으로 돌려드립니다

`GET /api/v1/integrations/instagram/workspaces/{ws}/media/`

실측 (실제 dev-api, `dmdummy_pro`):

```
HTTP 200   success: true   mock: true   count: 3
  mm-dmdummy_pro_main-0-camp3   VIDEO  likes=795  cmts=64
  mm-dmdummy_pro_main-1         IMAGE  likes=561  cmts=3
  mm-dmdummy_pro_main-2-camp1   VIDEO  likes=172  cmts=82
```

- **고정입니다** — 계정 id 로 시드를 만들어 몇 번을 불러도 같은 게시물·같은 숫자입니다.
- `?media_ids=a,b` **배치 조회도 같이** 됩니다. 목록과 **완전히 같은 객체**를 돌려주고,
  풀에 없는 id 는 지어내지 않고 `query.missing_media_ids` 로 알려줍니다.
- 응답에 **`"mock": true`** 를 넣었습니다 — "왜 좋아요 수가 매번 같지?" 로 헤매지 않으시도록.
- 캠페인형 게시물(`-camp` 붙은 것)은 캡션에 키워드가 들어 있어 **AI 초안 품질 확인에 적합**합니다.

### 썸네일은 인라인 SVG `data:` URI 입니다

```
thumbnail_url: "data:image/svg+xml;utf8,%3Csvg…"
```

외부 이미지 호스트에 의존하면 그 호스트가 막힐 때 dev 화면이 통째로 깨지고, IG CDN URL 은
서명 만료라 애초에 못 씁니다. `<img src>` 에 그대로 넣으면 렌더됩니다.

> ⚠️ 프론트에 CSP `img-src` 가 걸려 있으면 **`data:` 를 허용**해 주세요. 안 그러면
> 이미지 자리만 비어 보입니다(목록·선택 동작 자체는 정상).

규약대로 `media_url` 은 permalink(링크용), `thumbnail_url` 이 렌더용 이미지입니다.

---

## 3. 같이 막은 것 — `ai-suggest` 도 같은 구멍이었습니다

`/media/` 만 고치고 끝냈으면 **목록엔 보이는데 고르는 순간 404** 가 났을 겁니다:

```
POST /auto-dm-campaigns/ai-suggest/  (media_id=mm-dmdummy_pro_main-0-camp3)
→ 404 "게시물 정보를 가져올 수 없습니다: 400 Client Error … graph.instagram.com/mm-…"
```

여기도 같은 판정을 붙였습니다. 고친 뒤 실측:

```
POST ai-suggest → 202 (job d79a6391…)
  t+5s   running   40
  t+10s  succeeded 100
```

초안 결과(발췌) — 목 캡션의 키워드 "자료" 를 제대로 물었습니다:

```json
{"name": "콘텐츠 자료 배포 캠페인",
 "keyword_filter": ["자료","정보","신청","궁금","받기"],
 "follow_gate": {"follow_gate_button_label": "자료 받기", …}}
```

> 참고: 목 연결에서는 **비전 입력을 쓰지 않습니다**(`image_url` 을 비웁니다).
> 목 썸네일은 data URI 라 모델이 못 받아가고, 넘기면 다운로드 실패 경고만 쌓입니다.
> 캡션 기반으로만 도니 초안 문구는 정상이고 `vision_used: false` 로 나옵니다.

---

## 4. 근본 원인 — 가짜 토큰 관례가 **3종**인데 판정기는 1종만 알았습니다

이게 두 증상의 공통 뿌리입니다.

| 시더 | 토큰 형태 | 종전 판정 |
|---|---|---|
| dm_migration | `mock_token_…` | ✅ 인식 |
| `dmdummy_*` (지금 쓰시는 것) | `mock-token-…` | ❌ **진짜로 오인** |
| `home.*` | `DEVFAKE-…` | ❌ **진짜로 오인** |

진짜 토큰으로 오인 → 목 분기를 그냥 지나침 → Meta 로 나감 → Graph 400 → 우리가 500.

고친 내용:

- `is_mock_token` 이 **세 접두어를 전부** 인식합니다(실제 토큰은 `IGAA…`/`EAA…` 라 오탐 없음).
- 판정을 `MockInstagramProvider.should_use_mock()` **하나로** 모았습니다.
- 시더는 앞으로 정본 접두어를 씁니다(dev DB 에 남은 옛 값도 계속 인식).

부수 효과로 `dmdummy_*`·`home.*` 연결의 **연결 헬스 체크**도 같이 정상화됩니다 —
다음에 밟으셨을 같은 함정입니다.

### 운영에는 영향 없습니다

목 분기는 `DEBUG=True` 를 함께 요구합니다. 운영은 `DEBUG=False` 라 **어떤 경우에도
목으로 새지 않습니다.** 전역 `INSTAGRAM_MOCK_MODE` 는 **끈 채로 두었습니다** — 그걸 켜면
`is_mock_mode()` 하나만 보는 **인스타 로그인 경로까지 목으로 바뀌어** 지금 하고 계신
OAuth 시험이 불가능해집니다. 그래서 "토큰이 가짜인 연결만" 목으로 처리하는 방식을 썼습니다.

검증: 신규 테스트 17건 + `apps/integrations` 전체 **772 passed**.

---

## 5. 한 가지 미리 알려드립니다 — 버튼 URL 이 `example.com` 으로 나옵니다

위 초안 결과에도 그대로 있습니다:

```json
"link_button": {"link_button_url": "https://example.com", "link_button_label": "자료 확인하기"}
```

이건 목 때문이 아니라 **AI 초안의 기존 동작**입니다. 그리고 운영에서 이것 때문에
**63개 캠페인 · 795건이 `example.com` 링크로 나간 적이 있습니다.**
지금 만들기 화면을 작업 중이시니, **`link_button_url` 은 초안값을 그대로 저장하지 못하게**
(빈 값으로 두거나 사용자 입력을 강제) 막아 주시면 좋겠습니다.

---

## 확인해 보실 순서

1. `dmdummy_pro` 로 로그인 → 게시물 목록 (이제 3~10개 뜹니다)
2. `-camp` 붙은 게시물 선택 → AI 초안 → 10초 내 `succeeded`
3. 초안 확인 → 캠페인 저장까지

막히면 **시각(분)과 job_id** 주시면 워커 로그로 바로 봅니다.
