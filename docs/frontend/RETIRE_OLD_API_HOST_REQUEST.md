# [프론트 요청] 서버측(Pages Function)이 아직 **은퇴한 API 호스트**를 호출합니다 — 교체 요청

작성 2026-08-10 · 백엔드 → 프론트
대상: `turnflow.link` (Cloudflare Pages 프로젝트 `turnflowlink`)
관련: [IG_OAUTH_RETURN_TO_FRONTEND.md](IG_OAUTH_RETURN_TO_FRONTEND.md) · [../ops/PROD_HARDENING_2026-08-04.md](../ops/PROD_HARDENING_2026-08-04.md)

---

## 요약 (30초)

| | |
|---|---|
| **무엇** | `turnflow.link` 의 **서버측 코드**가 옛 API 호스트 `api.turnflow.clfy.ai.kr` 를 호출 중 |
| **바꿀 값** | → **`https://turnflow-api.clfy.ai.kr`** |
| **지금 깨졌나** | ❌ 아니요. 308 리다이렉트로 정상 동작 중입니다 |
| **왜 지금** | 그 옛 호스트의 **DNS 레코드를 삭제할 예정**입니다. 삭제되면 리다이렉트도 사라져 실패합니다 |
| **깨지면 무슨 일** | 공개 링크페이지 `/@slug` 의 **OG 태그(카톡·인스타 공유 미리보기)** 가 기본값으로 떨어짐 |
| **클라이언트 번들** | ✅ 이미 신 호스트입니다 — **고칠 곳은 서버측뿐** |

---

## 1. 배경 — 왜 옛 호스트를 없애나

`api.turnflow.clfy.ai.kr` 는 **2단계 서브도메인**이라 Cloudflare 엣지 인증서(`*.clfy.ai.kr`)가
커버하지 못합니다. 그래서 **CF 프록시를 못 걸고 오리진 서버에 직결**됩니다. 그 상태로 API 를
서빙하는 동안은 CF 의 레이트리밋·봇 차단·DDoS 흡수가 **전부 우회**됐습니다(실측 확인).

2026-08-04 에 그 호스트에서 API 서빙을 중단하고 신 호스트로 **308 리다이렉트**만 남겼습니다.
남은 마지막 문제는 **DNS A 레코드가 오리진 IP 를 그대로 노출**한다는 것입니다 —
9개 서브도메인 중 **이것 하나만** 노출합니다. 그래서 지웠으면 합니다.

> 참고: 308 은 301 과 달리 **메소드와 본문을 보존**합니다. 처음에 301 을 썼다가 `POST` 가
> `GET` 으로 바뀌어 내부 스케줄러가 3시간 멈추는 사고가 있었습니다. 지금은 308 이라 안전합니다.

---

## 2. 증거 — 누가 호출하는지 특정했습니다

옛 호스트에 액세스 로깅을 켜고 관측했습니다(2026-08-05 16시 ~ 08-06 09시).
총 **543건**이 왔고, 요청 헤더의 `Cf-Worker` 로 출처가 그대로 찍혔습니다:

```
416건  Cf-Worker: turnflowlink.pages.dev    ← 여기입니다
 18건  Cf-Worker: clfy1125.workers.dev      ← 아래 §5 에서 별도 문의
 92건  스캐너 (l9scan · crusader-worker · MJ12bot · vuln_scanner)
 26건  기타 (실사용자 1명 포함 — §5)
```

**인과도 확정했습니다.** 제가 브라우저로 `https://turnflow.link/@sowondream` 을 여는 순간
백엔드 로그에 다음이 찍혔습니다:

```
09:46:16  GET /api/v1/pages/@sowondream/     -> 308 | Cf-Worker: turnflowlink.pages.dev
09:45:57  GET /media/pages/2026/04/...       -> 308 | Cf-Worker: turnflowlink.pages.dev
```

즉 **공개 링크페이지의 OG 태그 SSR** 이 옛 호스트로 데이터를 가져오고 있습니다.
실제로 `/@sowondream` 은 `og:title="RARA Ann-nee"` 처럼 **진짜 데이터로 렌더**됩니다
(반면 데이터를 못 받은 페이지는 기본 OG 로 떨어집니다 — 이게 DNS 삭제 후의 모습입니다).

호출 경로는 두 종류입니다:
- `GET /api/v1/pages/@{slug}/` — 페이지 데이터
- `GET /media/pages/...` — 이미지

---

## 3. 왜 번들 grep 으로는 안 보였나 (중요)

저희가 08-04 에 "프론트는 이미 신 호스트를 쓴다"고 확인했던 건 **클라이언트 번들**이었고,
그건 지금도 맞습니다:

```bash
curl -s https://turnflow.link/assets/index-*.js | grep -c 'api\.turnflow\.clfy\.ai\.kr'   # 0
curl -s https://turnflow.link/assets/index-*.js | grep -c 'turnflow-api\.clfy\.ai\.kr'    # 1  ✅
```

**그런데 Pages Function 의 서버측 fetch 는 번들에 안 나타납니다.** 설정이 별개입니다.
CF Pages 빌드 변수 `VITE_API_BASE_URL` 도 이미 신 호스트(`https://turnflow-api.clfy.ai.kr`)인데,
서버측은 그 값을 안 쓰고 있는 것으로 보입니다.

**찾아보실 곳** (저희는 그 레포에 접근 권한이 없어 추정입니다):
- `functions/` 디렉터리 안에 하드코딩된 URL
- Pages **런타임** 환경변수(빌드 변수와 별개 — 대시보드에서 다른 탭입니다)
- SSR/미들웨어 코드에서 `import.meta.env` 가 아닌 다른 경로로 주입되는 값

---

## 4. 요청

서버측이 쓰는 API base 를 **`https://turnflow-api.clfy.ai.kr`** 로 바꿔 주세요.
(끝에 슬래시·경로 없음. 클라이언트가 쓰는 값과 동일합니다.)

### 확인 방법

배포 후 아래를 확인해 주시면 저희가 로그로 교차검증하겠습니다.

```bash
# 1) OG 태그가 여전히 실제 데이터인가 (기본값으로 떨어지면 안 됨)
curl -s https://turnflow.link/@sowondream | grep -oE '<meta property="og:title"[^>]*>'
#    기대: content="RARA Ann-nee"  (기대 아님: "Turnflow — 인스타 DM 자동화 · 링크인바이오")

# 2) 이미지가 나오는가
curl -s https://turnflow.link/@sowondream | grep -oE 'og:image[^>]*'
```

저희 쪽에서는 옛 호스트 로그의 `Cf-Worker: turnflowlink.pages.dev` 건수가
**0** 이 되는지로 확인합니다. 0 이 2~3일 유지되면 DNS 레코드를 지우겠습니다.

### 급하지 않습니다

지금 308 로 정상 동작 중이라 **깨진 것은 없습니다.** 다만 저희가 DNS 를 지우기 전에
반드시 선행돼야 하는 작업이라, 일정만 알려주시면 그에 맞춰 삭제 시점을 잡겠습니다.
**저희가 먼저 지우는 일은 없습니다** — 이 문서에 대한 회신을 받고 진행하겠습니다.

---

## 5. 함께 확인 부탁드릴 것 2건

### ① `clfy1125.workers.dev` 에서 18건 (`/api/v1/admin/me/` 등)

어드민 콘솔 Worker 의 **서버측 fetch** 로 보입니다. 어드민 콘솔의 클라이언트 쪽
`NEXT_PUBLIC_API_URL` 은 08-04 에 신 호스트로 교체했는데, 서버측(SSR/Route Handler)이
별도 값을 쓰고 있는 것 같습니다. 같은 방식으로 확인 부탁드립니다.

### ② 실사용자 1명이 옛 호스트로 `POST /api/v1/track/visit/` (9건)

한국 가정용 IP + Windows Chrome 에서 왔습니다. 둘 중 하나로 추정합니다:
- 랜딩 페이지의 방문 추적 스니펫이 옛 호스트를 가리킴, 또는
- 그 사용자 브라우저에 **캐시된 옛 번들**이 남아 있음

전자라면 고쳐야 하고, 후자라면 시간이 해결합니다. 랜딩 스니펫의 엔드포인트 주소만
확인해 주시면 판별됩니다.

---

## 부록 — 호스트 정리표

| 호스트 | 상태 | 용도 |
|---|---|---|
| **`turnflow-api.clfy.ai.kr`** | ✅ 정상 (CF 프록시) | **유일한 API 호스트. 이걸 쓰세요** |
| `api.turnflow.clfy.ai.kr` | ⚠️ 은퇴 — 308 리다이렉트만 | 곧 DNS 삭제 예정 |
| `dev-api.turnflow.link` | ✅ 정상 | dev API |
| `turnflow.link` | ✅ prod 프론트 | — |
| `app.turnflow.link` | ⚠️ **dev 프론트** | prod 아님. 둘 다 200 이고 `<title>` 도 같아 육안 구분이 안 되니 주의 |

문의 주시면 로그에서 바로 확인해 드리겠습니다.
