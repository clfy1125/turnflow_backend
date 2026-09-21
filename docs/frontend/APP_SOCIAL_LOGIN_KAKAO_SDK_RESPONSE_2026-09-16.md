# [백엔드 회신 · 2026-09-16] 카카오 콘솔 2건 완료 · 확인 2건 답변 · 앱 인스타 재시도 실측

새 설계(웹뷰 내 인가 + 302 가로채기) 잘 봤습니다. **백엔드 변경 없음**에 동의합니다.
요청하신 4건 전부 처리했고, 그 과정에서 **프론트가 아직 모르는 실측 2건**이 나와 같이 담습니다.

| # | 요청 | 상태 |
|---|---|---|
| (가) | app_id 판정 방식 | ✅ **우려하신 방식 아닙니다** — app_id 비교가 맞습니다 |
| (나) | dev-api 배포 여부 | ✅ **배포돼 있습니다**(실측) |
| 콘솔 ① | Android 플랫폼 등록 | ✅ **완료** |
| 콘솔 ② | 네이티브 앱 키 전달 | ✅ 아래 §4 |
| — | 앱 인스타 01:26 재시도 2건 | ⚠️ **둘 다 미완** (§5) |
| — | 409 실사용자 첫 발동 | ⚠️ UI 필요 (§6) |

---

## 1. (가) 키 종류로 판정하지 않습니다 — `app_id` 비교가 맞습니다

`apps/authentication/kakao.py` 현재 코드 그대로입니다:

```python
def assert_token_belongs_to_us(access_token: str) -> None:
    app_id = settings.KAKAO_APP_ID
    if not app_id:
        raise KakaoError("KAKAO_NOT_CONFIGURED", ..., status=503)   # fail-closed

    info = _get_json(f"{KAPI_BASE}/v1/user/access_token_info", access_token)
    if str(info.get("app_id")) != str(app_id):
        raise KakaoError("KAKAO_TOKEN_FOREIGN_APP", ..., status=403)
```

- 판정은 **`/v1/user/access_token_info` 의 `app_id` 를 `KAKAO_APP_ID` 와 문자열 비교** 한 줄이 전부입니다.
- **키 종류(REST·JS·네이티브)는 보지 않습니다.** 같은 앱에서 발급된 토큰이면 어느 키로 받았든
  `app_id` 가 같으므로, **네이티브 앱 키로 받은 SDK 토큰도 그대로 통과**합니다.
  → 우려하신 403 `KAKAO_TOKEN_FOREIGN_APP` 은 나지 않습니다.

운영·dev 컨테이너 실측값:

```
운영  KAKAO_APP_ID = 1573264  ✅
dev   KAKAO_APP_ID = 1573264  ✅
```

> ⚠️ `KAKAO_APP_ID` 가 비면 카카오에 물어보지도 않고 **503 `KAKAO_NOT_CONFIGURED` 로 막습니다**
> (fail-open 하면 설정 누락 하나로 남의 앱 토큰이 먹히므로 의도된 동작입니다).
> 지금은 양쪽 다 채워져 있으니 무관하지만, **400 이 아니라 503 이 오면 토큰 문제가 아니라
> 서버 설정 문제**로 보시면 됩니다.

### 참고 — SDK 경로에서도 이메일은 여전히 필수입니다

`access_token` 경로도 `/v2/user/me` 를 똑같이 타므로, 이메일 동의가 없으면 400 `KAKAO_EMAIL_REQUIRED` 입니다.
콘솔에서 필수 동의로 바꿔도 **이미 예전 동의 범위로 연결된 계정은 자동으로 다시 묻지 않습니다** —
테스트 중 이 오류가 나오면 카카오 계정 연결 해제 후 재시도하시면 됩니다.

---

## 2. (나) dev-api 에도 배포돼 있습니다

같은 가짜 토큰으로 두 환경 동시 실측 (2026-09-16 01:28 KST):

```bash
POST https://turnflow-api.clfy.ai.kr/api/v1/auth/kakao/   {"access_token":"probe-invalid-token"}
→ 400 {"code":"KAKAO_TOKEN_INVALID"}

POST https://dev-api.turnflow.link/api/v1/auth/kakao/      {"access_token":"probe-invalid-token"}
→ 400 {"code":"KAKAO_TOKEN_INVALID"}
```

둘 다 **400**(= `app_id` 검증 단계까지 도달해 카카오가 토큰을 거절)이고 **503 이 아니므로**,
경로·설정 모두 살아 있습니다. 스테이징 시험 바로 가능합니다.

---

## 3. 콘솔 ① Android 플랫폼 등록 — 완료

카카오 콘솔이 개편되어 플랫폼 정보가 **키 단위**로 들어갑니다
(`앱 설정 > 앱 > 플랫폼 키 > 네이티브 앱 키 > 수정`). 등록값:

```
패키지명    link.turnflow.app
키 해시     4TJaUAcWOJWFir6uzi+Vyocp6Wg=      (디버그)
스토어 URL  없음 (기본값 유지)
iOS 번들 ID 미입력
```

저장 후 다시 열어 **반영 확인했습니다.** 릴리스 키 해시는 Play Console 단계에서 추가하면 됩니다
(키 해시는 여러 개 등록 가능합니다).

**기존 Redirect URI 5개는 그대로입니다** — 제가 콘솔을 건드렸으므로 직접 재확인했습니다:

```
https://app.turnflow.link/auth/kakao/callback
https://turnflow.link/auth/kakao/callback
http://localhost:3000/auth/kakao/callback
http://localhost:5173/auth/kakao/callback
http://localhost:3006/auth/kakao/callback
```

동의항목·OIDC 도 손대지 않았습니다.

---

## 4. 콘솔 ② 네이티브 앱 키

```
ad31fd72babb98e8e7a712e6d4501d2f
```

이 값은 **APK 에 실려 나가는 클라이언트 키**라 이 경로로 전달해도 괜찮습니다.
말씀대로 env 주입·레포 제외로 가시면 됩니다.

> ⚠️ 반대로 **REST API 키와 클라이언트 시크릿은 이 경로로 보내지 않습니다.**
> 그 둘은 서버 전용이고 이미 운영 `.env` 에 있습니다. 필요하시면 따로 말씀해 주세요.

---

## 5. ⚠️ 알려드릴 것 — 00:55 이후 **01:26 에 2번 더** 시도됐고, 둘 다 미완입니다

보고해 주신 00:55 건 외에 **01:26:00 · 01:26:17 두 건**이 더 기록돼 있습니다(17초 간격).
`start` 호출 자체는 **완벽합니다**:

```
[16/Sep/2026:01:26:00] GET /api/v1/auth/instagram/start/
        ?redirect_uri=https%3A%2F%2Fturnflow.link%2Fauth%2Finstagram%2Fcallback&client=app
        → 200   Referer: https://localhost/   (SM-S926N · Android 16 · wv)
[16/Sep/2026:01:26:17] 동일 → 200
```

`redirect_uri` 고정 ✅ · `client=app` ✅ · 앱 웹뷰에서 호출 ✅ — **#1 은 완전히 고쳐졌습니다.**

그런데 state 3건 모두 여전히 미소비이고, **APP state 는 누적 6건 발급 / 0건 소비**입니다.

```
09-16 00:55:37  app_WzjzH9pNxE  consumed=-
09-16 01:26:00  app_eFKwD8koAt  consumed=-
09-16 01:26:17  app_WSwfEweq2r  consumed=-
```

17초 간격 재시도는 **뭔가가 빠르게 실패해서 다시 누른** 모양새입니다.

### 가설과, 각각을 가르는 확인법

`POST /auth/instagram/` 은 여전히 **앱에서 0건**이라 서버 쪽에는 더 볼 것이 없습니다.
셋 중 하나일 텐데, 앱에서 한 번만 봐 주시면 바로 갈립니다:

| 가설 | 화면에서 보이는 것 | 가른 뒤 |
|---|---|---|
| ① Meta 가 웹뷰 인가를 거부 | 인스타 화면에 오류/차단 문구 | 예비 경로(웹 번들 복귀) 복구 필요 |
| ② 가로채기가 **302 에는 안 걸림** | `turnflow.link` 웹 페이지가 실제로 뜸 | 훅 위치 변경 |
| ③ 콜백 라우트 도달했으나 교환 미호출 | 앱 콜백 화면까지 왔는데 멈춤 | 앱 내부 버그 |

**②를 특히 의심합니다.** 확인하신 가짜 콜백은 **앱이 스스로 시작한 내비게이션**이었고,
실제 흐름은 **instagram.com 이 내려주는 302 리다이렉트**입니다. Android WebView 에서 이 둘은
같은 훅에 안 걸리는 경우가 있습니다 — 가짜 콜백이 통과했다고 302 도 통과한다고 볼 수는 없습니다.
**진짜 인가를 끝까지 한 번** 밟아 보시는 게 가장 빠릅니다.

> ⚠️ 그래서 **#2(웹 번들 복귀 경로)를 아직 지우지 마시길** 권합니다. ①·②면 그게 곧 복구 경로입니다.
> 「예비로 남긴다」고 하신 판단에 동의합니다.

### 추적은 계속 해 드립니다

**시각(분)만 주시면** 그 한 건이 어디서 끊겼는지 바로 말씀드립니다:

- `start` 가 없다 → 앱이 호출을 못 함
- state 는 있는데 `consumed_at` 이 빈다 → **인가~복귀 사이**(위 ①②③)
- `consumed_at` 이 찍힌다 → 성공

---

## 6. ⚠️ 409 `INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE` 가 실사용자에게 처음 발동했습니다

2026-09-15 22:57:36, iPhone(인스타 인앱 브라우저), **웹**:

```
instagram login: short-lived OK user_id=28144901591862221
instagram login 거부(이미 다른 계정에 연동): ig=17841443115709339 conflict_ws=102f94eb-…
POST /api/v1/auth/instagram/ → 409
```

정책대로 정상 동작한 것이지만, **실제 사용자가 이 화면을 처음 만났습니다.**
응답에 `masked_email` 과 `instagram_username` 을 담아 보내고 있으니,
"이 인스타는 `a***@example.com` 계정에 연결돼 있습니다 — 그 계정으로 로그인하시거나,
기존 계정에서 연동을 해제한 뒤 다시 시도해 주세요" 정도의 안내를 붙여 주시면 좋겠습니다.
지금은 일반 오류로 떨어지면 사용자가 다음 행동을 알 수 없습니다.

참고로 **웹 인스타 로그인은 잘 돌고 있습니다** — 누적 가입 **6명**(직전 5명 → 00:35 에 1명 추가).
**카카오 웹 로그인도 운영에서 성공 확인**했습니다(22:56 iPhone · 00:58 Windows Chrome, 둘 다 200).

---

## 정리

- 요청 4건 **전부 처리 완료**, 백엔드 코드 변경 없음
- (가) 는 **우려하신 문제가 구조적으로 발생하지 않습니다**
- 남은 막힘은 **앱 인가~복귀 구간 한 곳**이고, 진짜 인가를 한 번 끝까지 밟으면 ①②③ 중 하나로 갈립니다
- 그 시각만 주시면 state 로 즉시 추적해 드립니다
