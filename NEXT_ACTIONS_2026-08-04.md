# 다음 할 일 (우선순위) — 2026-08-04 기준

보안 하드닝(08-03~04)과 그 여파로 발생한 장애 3건이 모두 해결된 시점의 인수인계 문서.
배경·검증·롤백 절차는 [PROD_HARDENING_2026-08-04.md](PROD_HARDENING_2026-08-04.md),
애플리케이션 취약점 원본은 [SECURITY_AUDIT_2026-06.md](SECURITY_AUDIT_2026-06.md) 참고.

---

## 0. 지금 상태 — 해결된 것 (재확인 불필요)

| 항목 | 상태 |
|---|---|
| SSH 무차별 대입 (7일간 42,738회) | ✅ 키 전용 인증 + IP 허용목록, 실측 0회 |
| Redis 무인증 브로커 | ✅ 무중단 인증 전환(오류 창 0) |
| Cloudflare 우회 경로(오리진 직타) | ✅ `@not_cf` 403 + `api.turnflow` API 미서빙 |
| Django `/admin` 무제한 노출 | ✅ IP 허용목록 (단 `/api/v1/admin/*` 는 외주 사용 중이라 의도적 미차단) |
| HSTS · netdata · vastai 특권 · gemma 네트워크 · LiteLLM 키 | ✅ |
| **어드민 콘솔 로그인 (301 → CORS 프리플라이트 차단)** | ✅ CF 빌드변수 교체 + 재배포, 브라우저 실증 |
| **주기잡 32개 3시간 정지 (301 → POST→GET 변환)** | ✅ 308 전환 + 워커 `ORIGIN` 변수, 결제 유실 0 |

> ⚠️ 이 세 장애의 뿌리가 모두 **하나의 301 리다이렉트**였다. 부록 A의 함정 목록을 먼저 읽을 것.

### ✅ 2026-08-05 추가 완료 (`bd42da1`)

| 항목 | 내용 |
|---|---|
| **Redis 인증 내구성** | 08-04 의 `CONFIG SET` 은 **런타임 전용**이어서 재시작하면 무인증으로 돌아가는 상태였다(옛 healthcheck 가 NOAUTH 에도 exit 0 이라 이를 가렸다). redis 재생성으로 커맨드라인에 `--requirepass` 를 박아 해결 — **키 손실 0**(db0 14,769 / db1 34 그대로) |
| **prod compose 드리프트** | 실서버에만 있던 Redis 인증 설정을 git 에 반영(바이트 일치 확인 후). `git checkout .` 한 번에 날아갈 위험 해소 |
| **`celery_reports` 배포 누락** | `deploy.sh`/`rollback.sh` 에 조건부 추가(리포트 1건 13~18분이라 큐 0 + 진행중 0 일 때만) + 두 스크립트에 **이미지 스큐 자동 점검** |
| ~~`rollback.sh` 가 celery_beat 를 켠다~~ | **오정보였다** — 07-30 `f91f5b9` 에서 이미 해결. 교훈만 유효: compose 서비스명을 명시하면 `profiles:[fallback]` 를 무시하고 기동한다 |

---

## P0 — 지금/오늘 (나만 할 수 있는 것)

코드 배포가 필요 없고, 미루면 위험이 그대로 남는 것들.

### P0-1. 🔴 `TELEGRAM_BOT_TOKEN` 회전 + 시크릿 타입으로 이전

- **어디**: Cloudflare → Workers → `turnflow-scheduler-tick` → 설정 → 변수 및 비밀
- **문제**: "일반 텍스트" 변수로 저장돼 대시보드 열람만으로 토큰이 그대로 보인다.
  같은 워커의 `SCHEDULER_TICK_SECRET` 은 올바르게 "비밀"인데 이것만 평문.
- **왜 P0**: 그 토큰이면 **DR 알림을 위조**할 수 있다. 오늘 그 채널이 실제 장애(주기잡 정지)를
  잡아냈다 — 이 채널의 신뢰성이 사실상 마지막 안전망이다. 가짜 "정상" 메시지로 진짜 장애를
  묻어버릴 수 있다.
- **방법**: BotFather → `/revoke` 로 새 토큰 발급 → 기존 변수 **삭제** → 유형 "비밀"로 재등록 → 배포
- **검증**: `?test=alert` 또는 워커 수동 실행으로 알림 1건 수신 확인

### P0-2. 🔴 `root` / `clfy` 비밀번호 교체

- 원격 인증은 폐지됐지만 **콘솔·`sudo` 용으로는 유출된 값이 그대로 유효**하다.
- `passwd` 는 대화형이라 대신 실행이 불가능하다.
- 교체 후 SSH 키 로그인이 여전히 되는지 **다른 터미널을 열어둔 상태에서** 확인할 것.

### P0-3. 🟠 SSH 키 백업

- `~/.ssh/turnflow_prod_admin_ed25519` — 패스프레이즈 없음, **이 PC에 유일본**.
- 이 PC가 죽으면 원격 접속 수단이 사라진다(콘솔은 콜로 업체 경유 = 느림).
- 암호화된 보관소(1Password 등)에 사본 + 가능하면 패스프레이즈 추가.

### P0-4. 🟠 `deploy/backups/.env.backup` 시크릿 회전

- 6월 감사 **P0-7**. 하드닝 때 파일 권한만 600 으로 바꿨고 **회전은 안 했다**.
- 대상: R2 액세스 키, Telegram 토큰(P0-1과 함께 처리) → 각 서비스 콘솔에서만 가능.

---

## P1 — 마케팅 집행 전 (코드 배포 1회로 묶기)

### 배포 블로커는 사실상 없다 (실측)

- `git log 086aea9..HEAD` = **4커밋**, 전부 `insta_reports`. HEAD = `4fa34f1`.
- **`celery_reports` 는 이미 `4fa34f1` 로 운영 중** — 뒤처진 건 web 티어 3개뿐.
- 그 4커밋이 건드린 건 `pipeline/*` · `service.py` · 템플릿 · 테스트뿐.
  **views / serializers / urls / settings / models / migrations 변경 0건** → web 티어 무영향.
- `deploy/scripts/deploy.sh:27` 은 **서버에서** `git pull` 후 빌드 → 로컬 워킹트리의 미커밋 변경
  (현재 billing 리퍼럴 WIP 5파일)은 **배포에 안 들어간다**.
- ⚠️ **`.deploy.prev` 를 신뢰하지 말 것** — `deploy.sh` 가 무조건 덮어쓰고 `rollback.sh` 가 그 값을 쓴다.
  실제 실행 이미지 표는 `/root/rollback_pointer_20260804.txt`(600)에 대역외로 고정해 뒀다.

아래 5개를 **한 배포**로 묶는 것을 권한다. 이 배포로 이미지 스큐도 자연히 수렴된다.

### P1-1. `NUM_PROXIES = 2` — 다른 스로틀보다 **먼저**

- **어디**: `config/settings/base.py` 의 `REST_FRAMEWORK`
- **왜**: 없으면 CF 프록시 뒤에서 DRF 스로틀 키가 `X-Forwarded-For` 전체 문자열이 되어 **희석**된다.
  실측: 직결 호스트는 11번째 요청에서 429, CF 경유는 60번 중 46번 통과.
- 이게 안 들어가면 아래 스로틀 작업이 **전부 무의미**하다.

### P1-2. `Throttled` → `RATE_LIMITED` 분기 — 스로틀보다 **먼저**

- **어디**: `apps/core/exceptions.custom_exception_handler`
- **왜**: HTTP 429 는 이미 `PlanLimitExceededError`(`error.code = "PLAN_LIMIT_EXCEEDED"`)가 쓰고 있고,
  프론트 계약이 **429 → 유료 제한 모달 + `paywall_viewed` 분석 이벤트**로 분기하도록 문서화돼 있다.
  그냥 스로틀을 켜면 두 429 가 구분 불가해져 **결제 분석 데이터가 되돌릴 수 없게 오염**된다.
- **방법**: `error.details.code = "RATE_LIMITED"` + `retry_after` 를 내보내고, 프론트가 그 값으로 분기.

### P1-3. H-8 — AI 엔드포인트 스로틀 + 토큰 차감 🔴

- **어디**: `apps/ai_jobs/views.py` (classify-posts), `apps/integrations/views.py` (ai_suggest)
- **현재 코드 재확인 결과**: 두 경로 모두 **throttle 0개, 토큰 차감 0**. `AiTokenBalance` 는
  무료 플랜 `/ai/jobs/` 에만 적용되고 이 둘은 토큰을 아예 건드리지 않는다.
- **왜 마케팅 전**: 로그인만 하면 무제한 LLM 비용을 태울 수 있다. **유입이 늘 때 가장 먼저 터질 곳.**

### P1-4. M-14 — 공개 엔드포인트 스로틀

- **어디**: `apps/pages/views.py` — 재확인 결과 `throttle_classes` **0개**
- 공개 제출/기록 4개 경로. slug 열거 + 대시보드 스팸/PII 오염 통로.

### P1-5. 인프라 부채 정리

- `config/settings/base.py` 에 `REDIS_PASSWORD` 정식 지원 추가 →
  현재 `.env.production` 의 `REDIS_HOST=default:<pw>@redis` 우회를 제거:
  ```python
  REDIS_PASSWORD = config("REDIS_PASSWORD", default="")
  _auth = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
  REDIS_URL = f"redis://{_auth}{REDIS_HOST}:{REDIS_PORT}"
  ```
- gunicorn 21.2.0 → 23.0.0 (호환 검증 완료: compose 플래그 13개 전부 존재,
  `SECURE_PROXY_SSL_HEADER` 설정됨, `FORWARDED_ALLOW_IPS` 미설정), 이어서 Django 5.0.14 / DRF 3.15.2 / Pillow 11

### 🚫 절대 하지 말 것

**전역 `AnonRateThrottle` 을 넣지 말 것.** IG 웹훅(`AllowAny`)과 토스 웹훅에 적용되어 Meta 버스트를
429 로 막고, **Meta 는 반복 실패 시 웹훅 구독을 auto-disable** 한다(과거 실제 발생). 대상
엔드포인트에 `ScopedRateThrottle` 만 붙이는 방식으로 갈 것.

---

## P2 — 그다음 배포 (애플리케이션 취약점)

전부 오늘 현재 코드로 **미해결 재확인** 완료. 심각도 순.

| # | 항목 | 위치 | 현재 상태 |
|---|---|---|---|
| P2-1 🔴 | **M-1** Google 로그인 `email_verified` 미검증 + 이메일로 기존 계정 자동통합 | `apps/authentication/views.py` | `email_verified` 검증 없음, `is_email_verified` 를 무조건 True 승격 → **계정 탈취** |
| P2-2 🔴 | **M-5** 보안메일 템플릿 HTML 을 staff 가 자유 편집 | `apps/emails/views_admin.py` | `IsAdminUser` 만, sanitize 없음 → **정식 도메인 피싱 메일 발송 가능**. 외주 어드민 계정이 있어 더 위험 |
| P2-3 🟠 | **M-9** 공개 링크페이지 `custom_css`·블록 `data` 서버 sanitize 없음 | `apps/pages/serializers.py` | 원본 반환, 저장 검증 없음 → 저장형 XSS |
| P2-4 🟠 | **M-2** Access 토큰 1일 + 폐기 불가 | `config/settings/base.py` | `timedelta(days=1)`, `token_version` 없음 → 비번재설정·탈퇴 후에도 최대 24h 유효 |
| P2-5 🟠 | **M-3/M-4** `SECRET_KEY` 가 JWT 서명 + IG 토큰 암호화 겸용, Fernet 키를 `sha256()` 로 파생 | `apps/integrations/encryption.py` | **키 회전 불가**. `JWT_SIGNING_KEY`/`FIELD_ENCRYPTION_KEY` 분리 + `MultiFernet` |
| P2-6 🟡 | **M-20** 컨테이너 root 실행 | `Dockerfile`, `docker-compose.prod.yml` | `USER` 지시어 0, `no-new-privileges` 0 |

> 감사 보고서의 라인 번호는 6월 기준이라 이동했을 수 있다 — 파일 기준으로 찾을 것.
> **M-1 과 M-5 가 단독으로 가장 위험하다**(외부인이 계정을 가져가거나, 내부 계정 하나로 고객에게 피싱).

---

## P3 — 오늘 작업의 마무리 (며칠 뒤)

### P3-1. `api.turnflow` DNS A 레코드 삭제 — **로그로 판단**

오리진 IP `121.126.99.70` 을 노출하는 **유일한** 레코드. 지금은 308 리다이렉트만 서빙한다.

이번에 그 호스트 블록에 액세스 로깅을 켜뒀으니, 며칠 관찰해 0건이면 안전하게 지울 수 있다:
```bash
docker logs caddy --since 24h 2>&1 | grep -c 'api\.turnflow\.clfy\.ai\.kr'
```
- 스케줄러 워커는 이미 `ORIGIN` 변수로 신 호스트에 직행하므로 **삭제해도 안전**하다(실증됨).
- 0건 확인 후 삭제 → 그 뒤에 `log retired_host` 블록도 제거하면 된다.

### P3-2. Meta 앱의 옛 OAuth 리디렉션 URI 제거

`https://api.turnflow.clfy.ai.kr/api/v1/integrations/instagram/connect/callback/` →
신규 URI 로 며칠 정상 동작 확인 후 제거.

### P3-3. `turnflowlink-review` Pages 프로젝트가 API 를 못 쓴다

- `VITE_API_BASE_URL` 빌드 변수가 **없어서** 빈 값 → 같은 오리진으로 요청 → 전부 404.
- 내 변경과 무관한 **기존 상태**지만, 마케팅 전 QA 를 여기서 할 계획이면 지금은 못 쓴다.
- 용도에 맞게 `https://dev-api.turnflow.link` 또는 `https://turnflow-api.clfy.ai.kr` 를 넣고 재배포.

### P3-4. 레포 `deploy/caddy/Caddyfile` 을 라이브와 동기화

배포 스크립트는 Caddy 를 건드리지 않으므로(확인함) 자동 원복 위험은 없다. 다만 이 사본을 라이브에
복사하면 HSTS·`@not_cf`·`/admin` 허용목록·api.turnflow 은퇴가 **전부 무효화**된다.
현재는 파일 상단에 경고 헤더를 넣어 막아 뒀다 — 여유 생기면 라이브 내용으로 동기화.

### P3-5. Cloudflare Tunnel SSH break-glass

IP 변동에 완전 면역인 유일한 접속 수단(outbound 443만 사용). 이미 같은 패턴을 쓰고 있다
(`~/.ssh/config` 의 `goldngoose` 가 `ProxyCommand cloudflared access ssh`).
서버 측 `cloudflared tunnel login` 이 브라우저 OAuth를 요구해 대신 할 수 없다.

**현재 break-glass 수단**
| 순위 | 수단 | 상태 |
|:--:|---|---|
| 1 | `turnflow-fw-allow-ip.sh add <새IP>` | ✅ 다른 허용 IP 에서 접속 가능할 때만 |
| 2 | `turnflow-fw-panic.sh` | ✅ 콘솔에서 방화벽 제한 전면 해제 |
| 3 | iDRAC9 SOL | ⚠️ 콜로 내부망 전용(`192.168.0.120`) → 업체 경유 |
| 4 | CF Tunnel SSH | ⏳ 미구성 (이 항목) |

---

## P4 — 관측성 / 구조 개선 (권장, 급하지 않음)

### P4-1. 🟠 CF cron tick 이 모든 주기잡의 **단일 장애점**

이번 장애의 구조적 원인. **이 서버에는 celery beat 가 없고**, 주기 실행이 전부
`CF cron 워커(매분) → POST /api/v1/internal/scheduler/tick → ScheduledJob.next_due_at` 을 지난다.
tick 하나가 죽으면 **결제 갱신·체험 만료·DM 재투입 32개가 전부 멈춘다.**

오늘은 DR 감지기가 3분 만에 알려 살았지만, 그건 `deferreddmage` 라는 **간접 지표**에 걸린 것이다.
tick 자체의 성공/실패를 직접 감시하는 장치를 권한다:
- `ScheduledJob.last_run_at` 최댓값이 N분 이상 정체되면 경보(`check_missed_payments` 와 유사한 형태)
- 또는 tick 응답을 Healthchecks.io 같은 dead-man switch 에 연결

### P4-2. Caddy 액세스 로깅을 전 호스트로 확대

현재는 은퇴 호스트(`api.turnflow`) 블록에만 켜져 있다. 다른 블록엔 여전히 없어서
"누가 어디로 왔는가"를 사후에 알 수 없다 — 이번 장애 조사에서 실제로 걸림돌이었고,
번들 grep 으로 우회해야 했다. 마케팅 트래픽 유입 전에 최소한
(호스트·경로·상태·UA·Origin) 을 남기고 `roll_size`/`roll_keep` 으로 용량을 제한할 것.

---

## ⏸ 보류 (유지보수 창 필요 / 저위험)

- **`pg_hba` trust 제거**: 편집 자체는 무해해 보이나(그 4줄의 소비자를 못 찾음), pgbackrest 가 unix
  소켓으로 붙으므로 잘못 건드리면 `archive_command` → WAL 적체 → 디스크 → **DB 정지**다.
  08-04 에 이 경로에서 사고를 한 번 냈으므로(권한 600), **절대값이 아닌 증가분 게이트**로 진행할 것.
- **컨테이너 비루트화 / `cap_drop: [ALL]`**: 17개 재생성 필요 + entrypoint 의 chown/collectstatic 을
  깨뜨릴 수 있다. `no-new-privileges:true` 만 먼저 넣는 것이 안전.
- **gemma 포트 재바인딩**: compose 는 이미 올바름 — 다음 gemma 재시작 시 자동 반영.
- **IPv6**: `sshd` 가 `[::]:2222` 를 듣지만 `eno8303` 에 글로벌 v6 주소·기본경로가 없어 도달 불가.
  콜로가 RA/DHCPv6 를 켜면 **IPv4 허용목록이 즉시 우회**된다 → `ip6tables` 미러링 또는
  `AddressFamily inet` 을 계획(후자는 소켓 활성화와 충돌 검토 필요).
- **pgbackrest R2 키를 env 로 이전**: 그러면 `pgbackrest.conf` 를 644 로 둘 필요가 없어진다.

---

## 부록 A. 오늘 배운 함정 — 반복하지 말 것

1. **HTTP 301/302 는 POST 를 GET 으로 바꾸고 본문을 버린다.** 은퇴 리다이렉트는 **308**(영구) 또는
   **307**(임시)을 쓸 것. Caddy 의 `permanent` = 301 이므로 쓰지 말 것.
2. **`curl -L` 은 이 결함을 못 잡는다** — curl 도 301 에서 스스로 GET 으로 바꿔 따라가므로 화면엔
   `200 OK` 로 보인다. 반드시 `curl -X POST -L` 로 메소드 보존을 확인
   (`405`=GET 변환됨 / `403`=POST 도달).
3. **CORS 프리플라이트(`OPTIONS`)는 리다이렉트를 아예 따라갈 수 없다.** 308 로도 해결되지 않는다
   (`Redirect is not allowed for a preflight request`). 프론트가 쓰는 API 호스트는 리다이렉트 없이
   직결이어야 한다.
4. **프론트의 API 호스트는 레포에 없다 — Cloudflare 빌드 변수에 있다.**
   `grep` 으로 안 나오면 "안 쓴다"가 아니라 "대시보드에 있다"는 뜻이다.
   호스트를 바꿀 땐 **배포된 번들을 직접 grep** 해서 소비자를 확정할 것:
   ```bash
   html=$(curl -sL https://<front>/); echo "$html" | grep -oE '/(assets|_next/static)/[^"]+\.js'
   # 각 청크를 curl 해서 호스트 문자열을 센다
   ```
5. **`NEXT_PUBLIC_*` / `VITE_*` 는 빌드 시점 인라인**이다. 대시보드 변수만 바꾸면 부족하고,
   **빌드 캐시까지 지워야** 낡은 값이 재사용되지 않는다. 지워졌는지는 빌드 로그의
   `⚠ No build cache found` 로 확인.
6. **은퇴 대상 호스트에는 은퇴 *전에* 액세스 로깅을 켜서 소비자 목록을 먼저 확정**할 것.
   사후에는 알 방법이 없다(Caddy 에 로그가 없었다).
7. **레포와 배포본은 갈라진다.** 어드민 워커·스케줄러 워커·고객앱 모두 레포 설정과 배포 설정이
   달랐다. 특히 `turnflow-scheduler-tick` 은 **git 연동이 없어 수동 `wrangler deploy`** 로만 배포된다.
8. `sshd` 드롭인은 **`00-`** 이어야 한다(`99-` 는 `50-cloud-init.conf` 에 져서 조용히 무효).
9. `pgbackrest.conf` 는 **644 필수** — 600 이면 컨테이너의 `postgres` 유저가 못 읽어 WAL 아카이빙이 멈춘다.
10. 아카이버 점검은 `failed_count == 0` 같은 **절대 조건 금지** — 누적값이므로 **증가분**으로 판정.
11. **런타임 전용 설정은 재시작 후에도 유지되는지로 검증할 것.** `CONFIG SET` / `iptables -I` /
    `sysctl -w` 류는 프로세스·컨테이너가 살아 있는 동안만 유효하다. Redis 인증이 실제로 이 함정에
    빠져 있었다(재시작 시 무인증 복귀). 판정:
    `docker inspect <ctr> --format '{{join .Config.Cmd " "}}' | grep -c requirepass`
12. **healthcheck 가 "통과"한다고 건강한 게 아니다.** `redis-cli ping` 은 NOAUTH 를 받아도 **exit 0**
    이라 requirepass 가 걸린 서버를 healthy 로 보고한다 — 잘못된 상태를 적극적으로 가려준다.
    상태 판정 커맨드는 실패 시 **비0으로 끝나는지** 확인할 것(`| grep -q PONG` 등).

## 부록 B. 상태 점검 원커맨드

```bash
# 주기잡이 살아있나 (가장 중요 — tick 단일 장애점)
docker exec turnflow_instagram_web_dashboard python -c "
import os,django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings.prod'); django.setup()
from django.utils import timezone; from apps.core.models import ScheduledJob
now=timezone.now()
over=[(int((now-j.next_due_at).total_seconds()), j.key) for j in ScheduledJob.objects.all() if j.next_due_at and j.next_due_at<now]
print('지연된 주기잡:', len(over)); [print(' ', s,'s', k) for s,k in sorted(over, reverse=True)[:10]]"

# tick 이 POST 200 으로 오나
docker logs turnflow_instagram_web_dashboard --since 5m 2>&1 \
  | grep -oE 'POST /api/v1/internal/scheduler/tick HTTP/1.1" [0-9]{3}' | sort | uniq -c

# 구 호스트로 아직 오는 클라이언트가 있나 (DNS 삭제 판단)
docker logs caddy --since 24h 2>&1 | grep -c 'api\.turnflow\.clfy\.ai\.kr'

# 보안 조치가 그대로인가
python3 -c "
import re; s=open('/root/caddy/Caddyfile',encoding='utf-8').read()
print('@not_cf:', len(re.findall(r'^\t@not_cf not remote_ip', s, re.M)))
print('HSTS:', len(re.findall(r'^\t+Strict-Transport-Security \"max-age=31536000\"', s, re.M)))
print('308 은퇴:', len(re.findall(r'^\tredir https://turnflow-api\.clfy\.ai\.kr\{uri\} 308', s, re.M)))
print('api.turnflow reverse_proxy:', len(re.findall(r'reverse_proxy web_', s.split('api.turnflow.clfy.ai.kr {')[1].split('\n}')[0])))"
```

**롤백 포인터**: 실제 실행 이미지 표는 `/root/rollback_pointer_20260804.txt`(600).
Caddy 백업은 `/root/caddy/Caddyfile.bak.*` (가장 최근: `.bak.addlog-*`, `.bak.redir308-*`).
