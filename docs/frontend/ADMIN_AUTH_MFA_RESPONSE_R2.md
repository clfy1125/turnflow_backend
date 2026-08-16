# 어드민 2단계 로그인 — 백엔드 회신 (2차)

회신: 백엔드 → 어드민 콘솔팀 · 2026-08-16
대상: `10_turnflow_admin/docs/ADMIN_AUTH_MFA_REPLY.md` (2차)

---

## 1. 🟢 prod 배포 완료했습니다 — 올리셔도 됩니다

**§9 의 1-b 신호입니다.** 막고 계셨던 조건이 해제됐습니다.

```
배포 커밋  79375a7  (이후 3c277dd 까지 반영)
전 컨테이너 동일 이미지 · 이미지 스큐 0 · DB/Redis 무중단
마이그레이션  admin_api 0008·0009 · emails 0009
```

엔드포인트 8종 전부 prod 에서 응답합니다. 컨테이너 내부 스모크 결과입니다.

```
GET  /api/v1/healthz                    -> 200
POST /api/v1/admin/auth/login/          -> 400   (빈 body 검증 — 500 아님)
POST /api/v1/admin/auth/refresh/        -> 400   (빈 body 검증)
GET  /api/v1/admin/auth/mfa/status/     -> 401   (무인증)
GET  /api/v1/admin/me/preferences/      -> 401   (무인증)
```

`admin_device_code` 메일 템플릿 시드도 완료했습니다(이게 없으면 신규 기기 승인 코드가 안 나갑니다).

**아직 강제는 꺼져 있습니다** — `ADMIN_MFA_ENFORCED=False`. 구·신 경로가 동시에 사는
§6 롤아웃 2번 구간입니다. 플래그를 켜기 전에 미리 알려드릴 테니, 말씀하신 대로 그 30분 안에
`admin_token_required` 경로를 확인해 주세요.

> ⚠️ 배포 전에 **`ADMIN_DEPLOY_NOTICE_2026-08-16.md` §3-b 를 먼저 봐 주세요.** 실제 코드를
> 대조해 보니 v2 미적용 지점이 남아 있었습니다(2차 회신 시점에 이미 고치셨다면 무시하셔도
> 됩니다). 특히 **`rate_limited` → `RATE_LIMITED`(대문자)** 는 429 분기가 조용히 안 걸립니다.

## 2. `setup_token` 은 **서로 다른 값**입니다 — 지금 구현이 맞습니다

| 어디서 | 무엇 |
|---|---|
| 1단계 403 `details.setup_token` | `setup/` 을 부를 자격 증명 |
| `setup/` 200 응답의 `setup_token` | `confirm/` 에 넘길 **새 티켓** |

서버가 `setup/` 에서 **들어온 토큰을 소비하고**([views_manage.py:207](apps/admin_api/auth/views_manage.py#L207))
**새 토큰을 발급**해([views_manage.py:150](apps/admin_api/auth/views_manage.py#L150)) 응답에 담습니다.
1회용이라 같은 값을 두 번 쓸 수 없습니다 — `confirm/` 에 1단계 토큰을 넣으면
`challenge_expired` 가 납니다.

**`setup/` 응답의 `setup_token` 은 항상 채워집니다.** 빈 값이 오는 경로가 없으니 그
"처음부터 다시" 분기는 오작동하지 않습니다. 다만 방어로 남겨두셔도 무해합니다.

## 3. 백업코드는 **양쪽 다 10개**입니다 (단, 서버 설정값)

`confirm/` 과 `regenerate/` 가 **같은 함수**를 부르고, 개수는 설정 하나
(`ADMIN_BACKUP_CODE_COUNT = 10`)에서 옵니다. 지금은 둘 다 10개입니다.

다만 **10 을 레이아웃에 하드코딩하지는 마세요.** 설정값이라 서버에서 바꾸면 배포 없이
개수가 달라집니다. `backup_codes.length` 로 2열을 계산하시면 그때도 안 깨집니다.
(바꿀 계획은 없지만, 하드코딩은 바뀌는 날 조용히 깨지는 쪽입니다.)

## 4. 백업코드로 들어와도 `remember_device` **먹습니다** — 체크박스 유지하세요

판단이 맞습니다. 서버도 그렇게 동작합니다 — 신뢰 등록은 **어떤 수단으로 통과했는지와 무관**하게
적용됩니다([views.py:370](apps/admin_api/auth/views.py#L370)).

```python
if data.get("remember_device") or device.is_trusted:
    device_store.trust_device(device, ip)
```

인증앱을 잃은 상황에서 기기까지 매번 새로 승인받게 하면 복구가 더 어려워집니다. 그리고
백업코드로 들어와도 **신규 기기면 이메일 코드를 이미 통과**한 뒤라, 신뢰 등록의 근거는
동일합니다.

참고로 그 로그인은 감사로그에 `amr: ["pwd", "backup_code"]` 로 남습니다 — 나중에 "누가
백업코드로 들어왔나"를 조회할 수 있습니다.

## 5. 페이지네이션 — 그대로 두겠습니다

상한 없이 배열로 계속 내려드립니다.

**자동 정리를 붙일 때 `is_trusted: false` 만 대상으로 하라는 것, 동의합니다.** 신뢰 기기가
목록에서 조용히 사라지면 "내가 해제한 건가"를 확인할 방법이 없다는 지적이 정확합니다.
붙이게 되면 그 조건으로 하고, 미리 알려드리겠습니다.

---

## 참고 답신 — `admin` 을 필수로 두지 않은 것

좋은 방어입니다. 서버는 `verify/`·`confirm/` 응답의 `admin` 을 `GET /admin/me/` 와
**같은 시리얼라이저**로 만들어(`AdminMeSerializer`) 항상 완전한 형태로 보냅니다 — 두 곳이
갈라질 수 없는 구조입니다.

그래도 "토큰은 이미 발급됐는데 parse 실패로 로그인이 안 된다"는 실패 모양은 확실히 나쁩니다.
그 방어는 유지해 주세요. 계약을 느슨하게 해달라는 요청으로 받지 않았습니다.
