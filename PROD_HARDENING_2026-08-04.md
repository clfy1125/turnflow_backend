# Prod 보안 하드닝 실행 기록 (2026-08-04)

> **대상:** colo prod `121.126.99.70` (Ubuntu 24.04, Dell PowerEdge R760)
> **전제:** 실서비스 운영 중 — 유료·체험 구독 **117건**(active 89 / trialing 28), 자동 DM 상시 발송, 토스 정기결제 10분 beat
> **원본 진단:** [SECURITY_AUDIT_2026-08-03_PROD_INFRA.md](SECURITY_AUDIT_2026-08-03_PROD_INFRA.md)
> **방법:** 변경 전 10개 영역을 read-only 로 사전검증(적대적 반증 포함) → 단계별 게이트를 두고 직렬 적용 → 매 단계 양방향 검증
> **결과:** P0 5건 전부 완료 · P1 6건 중 5건 완료 · P2 5건 중 2건 완료 · **서비스 중단 0**

---

## 0. 한눈에 보기

| # | 항목 | 상태 | 서비스 영향 |
|:--|---|:--:|:--:|
| **P0-1** | SSH 비밀번호 인증 폐지 → 키 전용 | ✅ | 없음 |
| **P0-2** | Redis 인증(무인증 Celery 브로커 폐쇄) | ✅ | 없음(무중단 전환) |
| **P0-3** | 호스트 방화벽: SSH IP 제한 + INPUT 기본거부 | ✅ | 없음 |
| **P0-4** | 엣지 방어 — **진단 정정 후** CF 웹훅 Skip 확장 · 인증 레이트리밋 · **CF 우회 경로 폐쇄**(`@not_cf` + api.turnflow 은퇴) | ✅ | 없음 |
| **P0-5** | Django `/admin` 운영자 IP 제한 | ✅ | 없음 |
| **P1-1** | 시크릿 파일 권한 600 + 구 사본 격리 | ✅ | 없음 |
| **P1-2** | netdata 무인증 공개 차단 | ✅ | 없음 |
| **P1-3** | `vastai_kaalia` 유휴 특권 회수 | ✅ | 없음 |
| **P1-4** | 자동 보안업데이트 — **진단 정정 후** needrestart 하드닝 | ✅ | 없음 |
| **P1-5** | 의존성 CVE (gunicorn/Django/DRF/Pillow) | ⏳ 스테이징 | 재빌드 필요 |
| **P1-6** | AI·공개 엔드포인트 스로틀 | ⏳ 스테이징 | 재빌드 필요 |
| **P2-1** | HSTS 헤더 | ✅ | 없음 |
| **P2-2** | PostgreSQL `pg_hba` trust 제거 | ⏸ 보류 | 아래 §6 |
| **P2-3** | 컨테이너 비루트화 / cap_drop | ⏸ 보류 | — |
| **P2-4** | DB 백업 정례화 | ✅ | 없음 |
| **P2-5** | SSH 부수 하드닝 | ✅ | 없음 |
| 추가 | gemma-vllm 네트워크 분리 | ✅ | 없음 |
| 추가 | LiteLLM 마스터키 교체 | ✅ | 없음 |
| 추가 | 진짜 롤백 포인터 대역외 고정 | ✅ | 없음 |

### 가장 큰 성과 — 진행 중이던 공격이 멈췄다

조치 전 7일간 SSH 인증 실패 **42,738건**(root 대상 29,247건, 분당 약 74회). 조치 후 **20분간 0건**.
방화벽 `TF_INPUT` 체인이 SSH 93건·전체 344건을 누적 차단하며 실제로 동작 중입니다.

---

## 1. 사전검증에서 잡아낸 것 — 이게 사고를 막았다

변경 전에 10개 영역을 독립 검증했고, 그 결과 **원래 계획대로 했으면 실패했을 3가지**를 발견했습니다.

### ① sshd 드롭인 파일명이 `99-` 면 조용히 무효가 된다 (원 보고서 §P0-1 오류)

- `/etc/ssh/sshd_config` **12행**이 `Include /etc/ssh/sshd_config.d/*.conf` 이고, OpenSSH 는 **"먼저 얻은 값"** 을 씁니다.
- `/etc/ssh/sshd_config.d/50-cloud-init.conf` 에 `PasswordAuthentication yes` 가 있습니다.
- 따라서 `99-hardening.conf`(50 보다 뒤)에 `PasswordAuthentication no` 를 써도 **50 이 먼저 읽혀 무시**됩니다.
- 실측 증명(임시 설정으로 검증): `99-` → `sshd -T` 가 `passwordauthentication yes`, `00-` → `no`.
- → 실제 적용 파일명은 **`00-turnflow-hardening.conf`**. 적용 후 `sshd -T` 로 `no` 를 확인하고 나서야 reload 했습니다.
- 부수 이득: `cloud-init` 은 현재 `disabled-by-marker-file` 이지만 `/etc/cloud/cloud.cfg.d/99-installer.cfg` 에 `ssh_pwauth: true` 가 남아 있어, 누군가 `cloud-init clean` 을 하면 50 파일이 되살아납니다. `00-` 접두사는 그 부활에도 면역입니다.

### ② `docker compose up` 이 의도치 않은 코드 배포를 일으킬 상태였다

- compose 는 `${APP_IMAGE:-turnflow_instagram_web:latest}` 이고 `APP_IMAGE` 는 `.env.production` 에 없습니다.
- 그런데 오늘 03:10 에 `086aea9` 가 배포되고, 03:28 에 `celery_reports` 만 `8e1558d` 로 따로 올라가면서 **`latest` 가 `8e1558d`** 를 가리키게 됐습니다.
- → Redis 작업을 위해 `docker compose up -d` 를 그냥 실행했다면 **web 전 티어가 8e1558d 로 조용히 업그레이드**됐을 것입니다(검증 안 된 16커밋 분량).
- → 모든 재생성에서 `APP_IMAGE` 를 서비스별로 명시 고정했고, 재생성 후 이미지를 전수 확인했습니다.

### ③ `CELERY_BROKER_URL` 이 별도로 하드코딩돼 있었다

- `base.py` 는 `CELERY_BROKER_URL = config("CELERY_BROKER_URL", default=f"{REDIS_URL}/0")` 입니다.
- `.env.production` 에 `CELERY_BROKER_URL=redis://redis:6379/0` 이 **명시적으로** 있어 `REDIS_URL` 파생 기본값을 덮어씁니다.
- → `REDIS_HOST` 만 고쳤을 때 캐시는 인증정보를 얻었지만 **브로커는 얻지 못했습니다**(`broker 인증 포함: False` 로 실측 포착).
- 이 상태로 `requirepass` 를 걸면 **Celery 브로커가 전면 차단** = DM·결제 파이프라인 정지였습니다.
- → `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` 도 함께 수정 후 3경로(broker·result·cache) 모두 ping 검증.

---

## 2. P0 상세

### P0-1. SSH 키 전용 인증

```
적용: /etc/ssh/sshd_config.d/00-turnflow-hardening.conf (600)
  PasswordAuthentication no        KbdInteractiveAuthentication no
  PermitRootLogin prohibit-password  PubkeyAuthentication yes
  MaxAuthTries 3                   LoginGraceTime 20
  X11Forwarding no                 AllowUsers root clfy
```

- 키: **새로 발급** `~/.ssh/turnflow_prod_admin_ed25519` (`SHA256:8P7JBN9sEm9eXQrP9AUxrvd7nLJnnejsaxWXY8nmLTQ`)
  - 기존 `turnflow_prod_ed25519`(7/14 생성)는 **패스프레이즈가 걸려 있어 자동화에 사용 불가** → 새 키 발급. 옛 공개키도 함께 등록해 두었습니다(패스프레이즈를 아신다면 예비 수단).
  - `/root/.ssh/authorized_keys`, `/home/clfy/.ssh/authorized_keys` 둘 다 등록(각 600, 디렉터리 700).
- 검증: root 키 OK · clfy 키 OK · **비밀번호 인증 거부**(`allowed types: ['publickey']`) · 서버가 제공하는 방법은 `publickey` 뿐.
- `sshd -T` 게이트를 통과한 뒤에만 `systemctl reload ssh`(`ExecReload` 가 자체적으로 `sshd -t` 를 하고 `KillMode=process` 라 기존 세션 유지).
- **`Port`·`AddressFamily` 는 건드리지 않았습니다** — 2222 리스너는 systemd generator(`ssh.socket`)가 만들고 `daemon-reload` 가 필요하며, `AddressFamily` 는 소켓 활성화와 충돌 위험이 있습니다.

> **fail2ban 은 의도적으로 설치하지 않았습니다.** IP 허용목록 + 키 전용 인증이 이미 더 강한 방어이고(무차별 대입이 아예 도달 못 함), fail2ban 은 이제 ⓐ apt 트랜잭션이 needrestart 를 통해 containerd 를 재시작할 위험, ⓑ 허용 IP 인 운영자 자신을 밴할 위험만 남깁니다. 순손실이라 판단했습니다. 필요하면 2분 작업입니다(`ignoreip` 에 운영자 IP 필수).

### P0-2. Redis 인증 — 무중단 전환

**핵심 발견:** Redis `nopass` 유저에게 **2-arg AUTH**(`AUTH default <pw>`)를 보내면 **통과**합니다(Redis 7.4.8 실측, `nopass` 는 어떤 비밀번호도 수락). 이 성질 덕분에 순서를 이렇게 잡아 **에러 창 0초**를 만들었습니다.

```
1) .env.production 수정 — REDIS_PASSWORD 추가,
   REDIS_HOST / CELERY_BROKER_URL / CELERY_RESULT_BACKEND 에 default:<pw>@ 삽입
2) compose 수정 — x-app-env 의 REDIS_HOST 주석 처리(environment 가 env_file 을 덮으므로),
   redis 서비스에 --requirepass ${REDIS_PASSWORD:?...} + 인증형 healthcheck
3) 앱 컨테이너 9개 재생성  ← 이 시점에 Redis 는 아직 무인증. 새 설정은 2-arg AUTH 라 통과한다
4) CONFIG SET requirepass  ← Redis 재시작 없음 → 큐 보존
```

- 재생성 순서: celery_ai·celery_reports → 나머지 워커 4개 → web_external → web_dashboard → web_webhook (각 단계 healthz 게이트)
- 검증: 무인증 `ping` → **NOAUTH** · 인증 `ping` → PONG · **litellm-proxy(2026-07-07 침해 당사자)에서 NOAUTH 로 차단** · broker/result/cache 3경로 ping OK · celery 6노드 · 큐 보존(db0 15,125키) · 큐 적체 0
- `${REDIS_PASSWORD:?...}` 형태를 쓴 이유: `--env-file` 없이 compose 를 돌리면 **즉시 실패**합니다. 무인증 Redis 가 조용히 다시 뜨는 사고를 구조적으로 막습니다.
- ⚠️ **남은 부채:** `REDIS_HOST=default:<pw>@redis` 는 `base.py` 가 비밀번호 필드를 지원하지 않아 쓴 **우회**입니다. 다음 배포에서 아래 3줄을 넣고 제거하세요.
  ```python
  REDIS_PASSWORD = config("REDIS_PASSWORD", default="")
  _auth = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
  REDIS_URL = f"redis://{_auth}{REDIS_HOST}:{REDIS_PORT}"
  ```
  또한 `apps/integrations/management/commands/loadtest_dm.py` 는 `REDIS_HOST` 를 순수 호스트명으로 가정해 현재 형태에서 동작하지 않습니다(dev 전용 명령이라 무해).

### P0-3. 호스트 방화벽

**ufw 는 쓰지 않았습니다** — 라이브 박스에서 Docker 의 FORWARD/DOCKER-USER 체인과 충돌 위험이 있고, 애초에 컨테이너 퍼블리시 포트를 막지도 못합니다.

```
전용 체인 TF_INPUT (INPUT 1행에서 점프) — 순서가 중요하므로 -A 로 쌓는다
  1  ! -i eno8303                        RETURN   ← 내부/loopback/docker 브리지 미적용
  2  ctstate ESTABLISHED,RELATED          RETURN   ← 기존 세션·아웃바운드 응답
  3  -p icmp                              RETURN
  4  -p tcp --dport 80                    RETURN
  5  -p tcp --dport 443                   RETURN
  6  -s 121.133.95.25 --dport 2222        RETURN   ← 사무실(고정)
  7  -s 14.52.76.113  --dport 2222        RETURN   ← 재택
  8  -p tcp --dport 2222                  DROP
  9  (전체)                                DROP    ← eno8303 인바운드 기본거부, 반드시 마지막
```

- 안전장치: ⓐ 스크립트가 자기 SSH 출처 IP 가 허용목록에 없으면 **실행 거부**, ⓑ 최종 DROP 을 맨 뒤에 붙여 부분 실패 시 **fail-open**, ⓒ 이미 완전하면 손대지 않아 타이머가 카운터를 리셋하지 않음
- 적용 **전에** 데드맨 스위치 무장: `systemd-run --unit=fw-deadman --on-active=15min /usr/local/sbin/turnflow-fw-panic.sh` (`at` 은 미설치, `systemd-run` 사용). 검증 완료 후 해제, PANIC 발동 0회.
- 유닛 정정: 기존 `turnflow-fw-hardening.service` 는 **부팅 후에 설치돼서** 한 번도 실행된 적이 없었습니다(`ExecMainStartTimestamp` 비어 있음). `RemainAfterExit` 를 제거하고 **5분 타이머**를 추가해 docker 재시작·부팅에도 규칙이 복원되게 했습니다.
- ⚠️ **DOCKER-USER 에는 기본거부를 넣지 않았습니다.** 컨테이너 아웃바운드 응답(토스 승인·Meta Graph·DM)이 FORWARD→DOCKER-USER 를 거치므로, 거기에 DROP 을 넣으면 **즉시 매출 장애**입니다.
- 검증: 새 SSH 연결 OK · 3개 gunicorn 200 · 오리진 443 외부 도달 · Caddy→netdata 정상 · 컨테이너→Meta(400)/토스(404) 도달 · billing beat 성공 · 멱등성(재실행 시 카운터 유지)

### P0-5. Django `/admin` IP 제한

```caddy
@django_admin_denied {
    path /admin /admin/*
    not client_ip 121.133.95.25 14.52.76.113
}
handle @django_admin_denied { respond "Forbidden" 403 }
```

- **`/api/v1/admin/*` 은 절대 포함하지 않았습니다.** 어드민 SPA 와 **외주 marketing_viewer 가 실제 활동 중**(user id 92, 2026-07-31 로그인, `admin_action_logs` 에 channel_link 작업 이력)이며, 그 경로는 JWT + RBAC deny-by-default + `auth_login 10/min` 스로틀로 이미 보호됩니다.
- 매처는 **`client_ip`**(`remote_ip` 아님): 나중에 이 호스트를 Cloudflare 프록시로 옮겨도(P0-4) 허용목록이 그대로 동작합니다. `remote_ip` 면 전환 순간 깨집니다.
- **양방향 검증:** 허용 IP → 200 · 비허용(더미 목록으로 일시 전환) → 403 · **위조 `Cf-Connecting-Ip`/`X-Forwarded-For` 로 우회 시도 → 403**(비신뢰 피어의 헤더는 `trusted_proxies` 밖이라 무시) · `/static/admin/*` CSS 무영향 · `/api/v1/admin/me/` → 401(차단 아님)

---

## 3. P1·P2 상세

### P1-1. 시크릿 파일 권한

- `600` 적용: `.env.production`, `/root/vllm-server/{.env,docker-compose.yml,litellm-config.yaml}`, `/root/moderatube-backend/.env*`, `/opt/netdata/docker-compose.yml`, `origin.pem`
- 구 백업 사본 **7개**를 `/root/secrets-archive/`(700)로 격리 — 회전 이전 시크릿이 담긴 `*.prebak.secrot*` 포함
- ⚠️ **`deploy/backups/pgbackrest.conf` 는 644 로 되돌렸습니다.** 이 파일은 DB 컨테이너에 `:ro` 로 마운트되어 **컨테이너 내부 `postgres` 유저가 읽어야** 합니다. 600 으로 바꾼 순간 `archive_command` 가 죽었고(아래 §5 사고 기록), 즉시 원복했습니다. R2 키·백업 암호화 패스프레이즈가 평문으로 들어 있으므로, 근본 해결은 **pgbackrest 를 환경변수(`PGBACKREST_REPO1_S3_KEY` 등)로 전환**하는 것입니다 — 유지보수 창에서 진행하세요.

### P1-2. netdata 공개 차단

`monitor.clfy.ai.kr` 에 `client_ip` 허용목록 추가. netdata 는 `pid: host`·`network_mode: host`·`SYS_ADMIN`·`apparmor:unconfined`·`/:/host/root`·`docker.sock` 을 가진 root 컨테이너이므로, 무인증 공개 자체가 위험했습니다.
(특권 축소는 미실행 — 그 마운트들이 호스트 메트릭에 실제로 필요합니다. 접근 제한으로 대응.)

### P1-3. `vastai_kaalia` 특권 회수

- **NOPASSWD 그랜트는 `/etc/sudoers` 59행**에 있었고, `@includedir /etc/sudoers.d` 가 57행이라 **sudoers.d 드롭인으로는 회수 불가**(last-match-wins)였습니다 → 본 파일을 직접 수정.
- `visudo -c -f` 로 임시 파일을 먼저 검사하는 하드 게이트 후 `cat >` 로 in-place 기록(inode·0440 보존), 다시 `visudo -c` 재검사.
- **주 그룹이 docker(988)** 였으므로 `usermod -g nogroup` → `gpasswd -d docker` → `gpasswd -d libvirt` 순서. 셸은 `nologin`.
- 계정은 삭제하지 않았습니다(uid 111 이 `/var/lib/vastai_kaalia_disabled` 파일을 소유).
- 검증: `sudo -l -U vastai_kaalia` → *not allowed* · `docker` 그룹 = `root,clfy` · `clfy` 권한 무변경 · 컨테이너 17개 무영향

### P1-4. 자동 보안업데이트 — **원 진단이 틀렸습니다**

원 보고서는 "자동 보안 업데이트가 masked 되어 꺼져 있다"고 했습니다. **사실이 아닙니다.**

- masked 되어 있던 `unattended-upgrades.service` 는 `unattended-upgrade-shutdown --wait-for-signal` = **종료 시 인터록**이며, 업그레이드 드라이버가 아닙니다.
- 실제 드라이버는 `apt-daily-upgrade.timer` 로 **enabled·active** 상태이고, `Allowed-Origins` 는 이미 **security-only**(`noble-security`, ESM apps/infra security; `noble-updates` 없음)입니다.
- `Automatic-Reboot` 는 이미 false(바이너리 기본값).
- 원 보고서의 "security 포켓 0건"은 **오류가 아니라 사실**입니다 — 대기 중 188개는 전부 비보안 `-updates` 와 서드파티입니다.
- 마스크를 만든 것은 vast.ai 설치 스크립트입니다(`/root/vast_host_install.log` 1786행).

**그래서 실제 위험은 다른 곳이었습니다:** `needrestart` 가 설치돼 있고 Ubuntu 24.04 기본값에서 **비대화형 세션에서 서비스를 자동 재시작**합니다. 2026-07-28 libc6 업그레이드 때 `containerd.service` 가 실제로 자동 재시작됐습니다(dockerd 는 기본 제외 규칙 `qr(^docker)=>0` 로 무사). containerd 재시작은 한 번 살아남았지만 단일 데이터포인트이고, 재발하면 17개 컨테이너 전체가 위험합니다.

조치:
- `/etc/needrestart/conf.d/99-turnflow.conf` — `$nrconf{override_rc}{qr(^containerd)} = 0;` (`perl -c` 게이트 통과, override_rc 44항목에 반영 확인)
- `/etc/apt/apt.conf.d/52turnflow-unattended-upgrades` — `Automatic-Reboot "false"` 명시(기본값 고정, 동작 변경 아님)
- 종료 인터록 유닛 unmask + enable (dpkg 가 종료로 중간에 끊기는 것 방지)
- **`50unattended-upgrades` 는 건드리지 않았습니다** — 이미 security-only 이고, 수정은 개선이 아니라 되돌릴 수 없는 회귀 위험입니다.
- 검증: `ipmitool` 설치 트랜잭션 후 **containerd 재시작 없음**(마지막 시작 시각 07-28 그대로)

### P2-1. HSTS

`Strict-Transport-Security "max-age=31536000"` 을 api 두 호스트에 추가. `includeSubDomains` 는 보류(서브도메인 전수 HTTPS 확인 후).
> Caddyfile 주석의 "HSTS 는 Caddy TLS 가 자동" 은 **사실이 아닙니다** — 실측으로 헤더가 없었고, 주석도 정정했습니다.

### P2-4. DB 백업

- 마지막 full 백업이 **2026-07-06**(29일 경과)이었습니다 → 새 full 백업 생성(`20260804-040646F`, DB 621MB → repo 108MB)
- `/etc/cron.d/turnflow-pgbackrest` 등록: 월 18:00 UTC(화 03:00 KST) full + 그 외 매일 18:30 UTC diff. 기존 `deploy/backups/pgbackrest_backup.sh` 사용, **cron 경로로 diff 백업 1회 실증**.
- ⚠️ pgbackrest 는 **`-u postgres` 로 실행**해야 합니다(root 로 하면 `role "root" does not exist`).

### 추가. gemma-vllm 네트워크 분리

`gemma-vllm` 을 `turnflow_instagram_net` 에서 분리(무중단, 컨테이너 재시작 없음).
근거: litellm 은 `vllm-server_default` 에서 `http://vllm:8000` 으로 gemma 를 부르므로 gemma 가 prod 망에 있을 이유가 없었고, 그 망에는 무인증 Redis 가 있었습니다. 앱 코드에 gemma 직접 참조는 없습니다(grep 확인).
`/root/vllm-server/docker-compose.yml` 에도 반영해 영속화(주석 포함).
- 전환 순간 litellm 의 **스테일 커넥션 풀로 인해 테스트 호출 1건이 타임아웃**했고, 다음 요청부터 정상(200)이었습니다.
- Docker 가 **DNAT 를 자동으로 새 IP(172.19.0.3)로 갱신** — 8088 은 계속 동작하며 외부는 여전히 DROP.
- **포트 재바인딩(0.0.0.0 → 127.0.0.1)은 미실행**: compose 파일에는 이미 `127.0.0.1:8088` 로 고쳐져 있지만 실행 중 컨테이너가 옛 바인딩입니다. entrypoint 가 기동 시 **PyPI 에서 `transformers==5.13.0` 을 내려받아** 재시작에 2~3분 + 외부 네트워크 의존이 생기므로, DOCKER-USER DROP 으로 이미 차단된 상태에서 위험을 감수할 이유가 없습니다. **다음 계획된 gemma 재시작 때 자동 반영**됩니다.

### 추가. LiteLLM 마스터키 교체

작업 중 조사 과정에서 마스터키 전체 값이 노출됐습니다(= `.env.production` 의 `LLM_API_KEY`와 동일). 교체했습니다.
- `.env.production` `LLM_API_KEY` + `/root/vllm-server/docker-compose.yml` + `litellm-config.yaml` 3곳 갱신 후 `litellm-proxy` 만 재생성(`--no-deps` 로 gemma 미접촉, 21초)
- 검증: 새 키 200(실제 완성 응답) · **옛 키 401** · dev virtual key(`dev-pc`, `dev-pc-2`) 정상 — dev 는 마스터키가 아닌 virtual key 를 쓰므로 영향 없음
- 노출 위험 자체는 낮았습니다: `llm.clfy.ai.kr` 은 **이미 `client_ip` 허용목록이 활성**이었고 4040 은 외부 DROP 이라 인터넷에서 사용 불가였습니다.

---

## 3.5 P0-4 (엣지 방어) — 진단이 틀렸고, 실제로 필요한 일은 달랐다

### 원 진단의 오류

원 보고서는 "API 오리진이 Cloudflare 를 경유하지 않음 → 엣지 DDoS/봇/레이트리밋 방어가 0" 이라고 했습니다.
**틀렸습니다.** 실제 프로덕션 API 호스트는 `turnflow-api.clfy.ai.kr` 이고 **이미 CF 프록시 뒤**였습니다.
저는 `ALLOWED_HOSTS` 와 Caddyfile 첫 블록 이름만 보고 `api.turnflow.clfy.ai.kr` 을 라이브 호스트로 오판했습니다.

실측 근거:
- **프론트엔드**: `turnflow.link` 번들이 `turnflow-api.clfy.ai.kr` 을 호출(브라우저 네트워크 로그 +
  번들 문자열 2건). 즉 **마케팅 유입 트래픽 경로는 이미 CF 뒤.**
- **Meta 웹훅**: Caddy 로그의 실제 Meta 요청 —
  `host=turnflow-api.clfy.ai.kr`, `User-Agent=Webhooks/1.0 (https://fb.me/webhooks)`,
  `remote_ip=172.68.22.64`(CF 대역). 즉 **Meta 웹훅도 이미 CF 를 통과하고 있었고**,
  `브라우저 무결성 검사(BIC)`가 켜진 상태에서 정상 동작 중이었습니다 → "BIC 가 웹훅을 막을 것"이라는
  제 우려는 근거가 없었습니다.
- `api.turnflow.clfy.ai.kr` 의 실제 용도는 IG OAuth 리다이렉트 URI(`INSTAGRAM_REDIRECT_URI`).
- ⚠️ Caddy 는 **오류만 로깅**합니다(`log` 지시어 없음) → "48시간에 웹훅 1건"으로 보이는 것은
  그 1건이 **실패(502)** 했기 때문입니다. 그 502 는 **2026-08-04 03:10 UTC = 사용자 배포 시각**으로,
  컨테이너 재생성 중 Meta 웹훅 1건이 502 를 받은 것입니다(제 작업 이전).

### 그래서 실제로 한 일

**① 웹훅 Skip 규칙의 대상 호스트가 틀려 있었습니다 (실질적 구멍)**

기존 사용자 지정 규칙 `Skip CF security for server-to-server (webhook/tick/health)` 는
사용자 지정 규칙·속도 제한·관리 규칙·Super Bot Fight·**BIC**·보안 수준을 모두 건너뛰도록
잘 설정돼 있었지만, 대상이 `http.host eq "api.turnflow.clfy.ai.kr"` **하나뿐**이었습니다.
그 호스트는 프록시가 아니라 CF 를 지나지 않으므로 **이벤트 0건 — 한 번도 발동한 적이 없었고**,
정작 Meta 웹훅이 오는 `turnflow-api` 는 아무 예외 없이 CF 보안을 통과하고 있었습니다.

→ 표현식을 확장했습니다(Skip 은 차단이 아니라 면제이므로 깨질 위험 없음):
```
(http.host in {"api.turnflow.clfy.ai.kr" "turnflow-api.clfy.ai.kr"}) and (
  starts_with(http.request.uri.path, "/api/v1/integrations/instagram/webhook") or
  starts_with(http.request.uri.path, "/api/v1/billing/toss/webhook") or   ← 신규
  starts_with(http.request.uri.path, "/api/v1/internal/") or
  starts_with(http.request.uri.path, "/api/v1/healthz") )
```

**② 인증 엔드포인트 속도 제한 규칙 신설** (free 플랜 1/1 슬롯)

```
표현식: starts_with(http.request.uri.path, "/api/v1/auth/")
        and not starts_with(http.request.uri.path, "/api/v1/auth/token/")
        and not starts_with(http.request.uri.path, "/api/v1/auth/me")
카운팅: IP    임계값: 50건 / 10초    동작: 차단 10초
```
- `token/refresh`·`me` 제외 이유: SPA 가 상시 호출하는 경로라, 통신사 CGNAT 뒤 다수 사용자가
  임계값을 소모할 수 있습니다. 공격 표적(login·register·google·password/reset-*·email/verify)만 남겼습니다.
- **free 플랜 제약(실측)**: 기간 `10초`만, 동작 `차단`만, 지원 필드에 **`http.host` 가 없음**
  (`URI 경로`·검증된 봇·암호 유출됨 등만). 그래서 경로 기반으로 작성했습니다 —
  `llm`·`monitor` 호스트에는 `/api/v1/auth/` 경로가 없어 안전합니다.
- 순서상 Skip 규칙이 먼저 평가되므로 **웹훅은 이 레이트리밋에서 면제**됩니다.
- 관리형 규칙(Managed Rules)은 Pro 필요 → 미적용.

### 검증 (전부 실측)

| 항목 | 결과 |
|---|---|
| 정상 단건 `/auth/login/` | 401 (Django 도달) |
| 버스트 60회 | 46×401 + **14×429** → 레이트리밋 발동 |
| **차단 중 IG 웹훅** | **403**(Django HMAC 거부 = 도달) |
| **차단 중 토스 웹훅** | **200** |
| 차단 중 healthz | 200 |
| 제외 경로 `token/refresh` / `me` | 400 / 401 (차단 안 됨) |
| 12초 후 | 401 복귀 (자동 해제) |
| `cf-mitigated` 헤더 | 없음 · `cf-cache-status: DYNAMIC`(API 미캐싱) |
| SSL/TLS 모드 | `Full (Strict)` — 확인만, 변경 없음 |

### 🔴 이 검증 중에 발견한 새 결함 — DRF 스로틀이 CF 뒤에서 희석된다

버스트 60회 중 **46건이 Django 에 도달해 401** 을 받았습니다. `auth_login = 10/min` 이라면
11번째부터 Django 429 여야 하고, **직결 호스트에서는 실제로 그랬습니다**(11번째 429 실측).

원인: DRF `BaseThrottle.get_ident()` 는 `NUM_PROXIES` 가 없으면 **X-Forwarded-For 전체 문자열**을
스로틀 키로 씁니다. CF 뒤에서는 XFF 가 `클라이언트IP, CF엣지IP` 이고 **CF 엣지 IP 는 요청마다 달라져**
키가 흩어집니다 → **인증 스로틀이 사실상 무력화**. 실측 확인: `settings.REST_FRAMEWORK["NUM_PROXIES"] = None`.

- 영향: 프론트가 쓰는 CF 프록시 호스트(`turnflow-api`)에서 로그인·가입·비번재설정·이메일코드
  스로틀이 모두 약해진 상태였습니다. 오늘 추가한 CF 레이트리밋이 그 공백을 메우고 있습니다.
- 수정: `config/settings/base.py` 의 `REST_FRAMEWORK` 에 `"NUM_PROXIES": 2` 추가.
  DRF 는 `addrs[-min(NUM_PROXIES, len(addrs))]` 를 쓰므로 **CF 경유(2단)·직결(1단) 양쪽에서 모두**
  올바른 클라이언트 IP 를 고릅니다. 재빌드가 필요해 스테이징으로 넘겼습니다(§6).

---

## 4. 원 보고서 정정 사항

| 위치 | 원 기술 | 정정 |
|---|---|---|
| P0-1 조치 예시 | `99-hardening.conf` 사용 | **`00-` 이어야 함.** `99-` 는 `50-cloud-init.conf` 에 져서 무효(실측 증명) |
| P1-4 전체 | "자동 보안 업데이트가 masked 로 꺼져 있음" | **이미 켜져 동작 중.** masked 유닛은 종료 인터록. 실제 위험은 needrestart 의 containerd 자동 재시작 |
| P1-4 | "그중 security: 0 은 제 grep 오류" | **grep 이 맞았음** — security 포켓이 실제로 비어 있음 |
| §1 llm.clfy.ai.kr | "Caddy 레벨 인증 없음" | 저장소 사본 기준 오류. **서버 실 Caddyfile 에는 07-08 부터 `client_ip` 허용목록이 활성** |
| P2-4 | "마지막 full 백업 06-30, 34일 경과" | **07-06, 29일 경과**(`info` 출력이 잘려 뒷부분을 놓쳤음) |
| §5 양호 | "SECRET_KEY 50자" | 맞음. 단 prod 가드 임계값(`< 50`)의 **경계값**이라 여유 없음 |
| P1-5 | gunicorn/Django 등 위험 서술 | 유효. 단 gunicorn 23 은 compose 의 13개 플래그 전부 존재하고 `SECURE_PROXY_SSL_HEADER` 설정·`FORWARDED_ALLOW_IPS` 미설정이라 **호환 위험 낮음**으로 확인 |
| **P0-4 전체** | "API 오리진이 CF 를 경유하지 않음 → 엣지 방어 0" | ❌ **실 API 호스트는 `turnflow-api.clfy.ai.kr` 이고 이미 CF 프록시 뒤였다.** 프론트·Meta 웹훅 모두 CF 경유(실측). 남은 실제 문제는 ⓐ 웹훅 Skip 규칙의 대상 호스트 오설정 ⓑ 레이트리밋 부재 ⓒ 오리진 IP 노출 (§3.5) |
| §1 "Meta 웹훅이 BIC 에 막힐 위험" | 켜기 전 우려로 기술 | ❌ **Meta 웹훅은 BIC 가 켜진 상태에서 이미 정상 통과 중이었다**(실 로그: CF 대역 출처 + Meta UA) |
| §5 양호 "인증 스로틀 실동작" | 11번째 429 로 정상 판정 | ⚠️ **직결 호스트에서만 맞다.** CF 프록시 호스트에서는 DRF `NUM_PROXIES` 미설정 탓에 XFF 전체가 스로틀 키가 되어 **희석**된다(§3.5 말미) |

---

## 5. 작업 중 발생한 사고 3건 (모두 해결)

### 사고 ③(가장 심각) — api.turnflow 은퇴가 **주기잡 32개 전부를 3시간 정지**시켰습니다

DR 감지기가 텔레그램으로 알렸습니다(감지기가 제 역할을 했습니다):

```
🟠 DR 감지 — colo SUSPECTED_DOWN (3분 지속, class=STALL)
사유: deferreddmage=3801s
```

**메커니즘 — 301 은 POST 를 GET 으로 바꿉니다.**

```
CF cron 워커(매분) --POST--> api.turnflow.../api/v1/internal/scheduler/tick
                               ↓ 은퇴 조치로 넣은 301
                            turnflow-api.../...  ← 메소드가 GET 으로 변환, 본문 소실
                               ↓
                            405 Method Not Allowed
```

HTTP 301/302 는 리다이렉트 시 메소드를 GET 으로 바꾸고 본문을 버립니다(역사적 브라우저 동작이
표준화된 것). 실측: 20분간 tick 요청 60건이 전부 `GET → 405`.

**이 서버에는 celery beat 가 없습니다.** 주기 실행은 전부
`CF cron 워커 → POST /api/v1/internal/scheduler/tick → ScheduledJob.next_due_at 기준 발사` 구조입니다.
따라서 tick 하나가 죽으면 **모든 주기 작업이 죽습니다**:

| 잡 | 주기 | 정지 시간 |
|---|---|---|
| `process-due-renewals` (토스 정기결제) | 10분 | 3시간 6분 |
| `reconcile-pending-payments` | 30분 | 3시간 31분 |
| `handle-trial-expiry` / `grace-period` / `cancelled-expiry` | 1시간 | 3시간 47분 |
| `dm-requeue-deferred` (알림의 직접 원인) | 30초 | 3시간 6분 |
| 그 외 27개 | — | 3~23시간 |

**피해는 지연뿐, 유실 0.** 확인: 활성/체험 구독 120개 중 `next_billing_at` 이 지난(미처리) 건 **0개**,
복구 직후 `process_due_renewals → {'dispatched': 0}` = 공백 동안 **도래한 갱신이 애초에 없었습니다.**
DM 발송(웹훅 구동)·웹훅 수신은 정상이었기 때문에 어제 검증에서 걸리지 않았습니다.

**조치 (2단계)**

1. **응급 — Caddy `301` → `308`.** 308(RFC 7538)은 **메소드와 본문을 보존**합니다.
   검증: `curl -X POST -L` 최종 응답이 `405`(GET 변환) → **`403`**(POST 도달·시크릿 없음)로 바뀜.
   복구 순간이 로그에 그대로 남았습니다:
   ```
   19:03:44  GET  /internal/scheduler/tick → 405   ← 마지막 실패
   19:04:44  POST /internal/scheduler/tick → 200   ← 실제 cron, 정상
   ```
   보안 후퇴 없음 — 이 호스트는 여전히 API 를 서빙하지 않습니다(`reverse_proxy` 0개).

2. **근본 — CF 워커(`turnflow-scheduler-tick`)에 `ORIGIN` 변수 추가.**
   이 워커는 호출 URL 을 코드에서 결정하고 있었고 `ORIGIN` 변수가 **없었습니다**(레포
   `deploy/dr/cloudflare/wrangler.toml` 은 신 호스트인데 배포본은 구 호스트 — git 연동이 없어
   수동 `wrangler deploy` 로만 배포되다 생긴 드리프트).
   `ORIGIN=https://turnflow-api.clfy.ai.kr` 를 넣자 **구 호스트 요청이 0건**이 됐습니다(아래 로깅으로 실증)
   → 리다이렉트 의존 제거.

**부수 성과 — 은퇴 호스트에 액세스 로깅을 켰습니다.** `api.turnflow` 블록에만
`log retired_host { output stderr; format json }` 를 추가했습니다. 이것으로
① 워커가 정말 신 호스트로 직행하는지 실증했고(3분간 tick 0건, 제 테스트 1건만),
② **DNS A 레코드 삭제를 "0건 확인" 근거로 안전하게 결정**할 수 있게 됐습니다.

**교훈 (재발 방지)**

> **호스트 은퇴 검증에 `curl -L` 만 쓰면 안 된다 — `curl -L` 은 301 에서 스스로 GET 으로 바꿔
> 따라가므로, 실제 클라이언트가 겪는 실패를 재현하면서도 화면에는 `200 OK` 로 보인다.**
> 반드시 `curl -X POST -L` 로 **메소드 보존까지** 확인하고, 은퇴 리다이렉트는 처음부터
> **308**(또는 307)을 쓸 것. 그리고 **은퇴 대상 호스트에는 은퇴 *전에* 액세스 로깅을 켜서**
> 소비자 목록을 먼저 확정할 것 — 사후에는 알 방법이 없다.
> 또한 이 서버는 **beat 가 없고 CF cron tick 하나가 모든 주기잡의 단일 장애점**이라는 것을
> 반드시 기억할 것(§blast radius).

---

## 5-1. 사고 ①·② (동일 조치 창)

### 사고 ②(더 중요) — api.turnflow 은퇴가 **어드민 콘솔 로그인을 깼습니다**

증상(사용자 보고):

```
Access to XMLHttpRequest at 'https://api.turnflow.clfy.ai.kr/api/v1/auth/login/'
from origin 'https://admin.turnflow.link' has been blocked by CORS policy:
Response to preflight request doesn't pass access control check:
Redirect is not allowed for a preflight request.
```

**원인**: CORS 프리플라이트(`OPTIONS`)는 **리다이렉트를 따라갈 수 없습니다**(Fetch 표준). 은퇴 조치로
`api.turnflow` 가 301 을 반환하게 되자, 그 호스트를 호출하던 어드민 콘솔은 프리플라이트 단계에서
브라우저에 의해 차단됐습니다. 301 은 "in-flight OAuth 콜백을 살리는 안전망"으로 택한 것이었는데,
**리다이렉트가 안전망이 되는 건 브라우저 내비게이션뿐이고 XHR 프리플라이트에는 통하지 않습니다.**

**제 검증의 구멍**: 은퇴 직후 저는 `curl` 로 301·`-L` 추적·실 Meta 웹훅 50건까지 확인했지만,
**어떤 클라이언트가 그 호스트를 호출하는지는 확인하지 않았습니다.** 서버만 검증하고 소비자를 검증하지
않은 것입니다. `curl -L` 은 통과하는데 브라우저 프리플라이트는 실패하므로, curl 검증만으로는 원리적으로
잡히지 않는 결함이었습니다.

**피해 범위 (실측으로 확정)**

| 프론트 | 배포 번들이 쓰는 API 호스트 | 영향 |
|---|---|---|
| `turnflow.link` (고객용, Vite) | `turnflow-api` (신규) | ✅ **무영향 — 결제 고객은 정상** |
| `admin.turnflow.link` (Next.js Worker) | `api.turnflow` (구) | ❌ 로그인 불가 |
| `turnflow-admin-dev` (dev 워커) | `dev-api.turnflow.link` | ✅ 무영향 |
| `turnflow.clfy.ai.kr` | → 301 → `turnflow.link` | ✅ 무영향 |

고객 영향이 0 이었기 때문에 **보안 구멍을 되돌리지 않고**(api.turnflow 복구 안 함) 어드민만 고쳤습니다.

**조치**: 값의 출처는 레포가 아니라 **Cloudflare Workers 빌드 변수**였습니다(그래서 `grep` 으로 안 나왔습니다).

1. `NEXT_PUBLIC_API_URL` : `https://api.turnflow.clfy.ai.kr/api/v1` → `https://turnflow-api.clfy.ai.kr/api/v1`
2. **빌드 캐시 삭제** — `NEXT_PUBLIC_*` 는 컴파일 시 인라인되므로 캐시를 두면 값만 바꿔도 낡은 번들이
   재사용될 수 있습니다. 빌드 로그의 `⚠ No build cache found` 로 삭제가 먹은 것을 확인했습니다.
3. 같은 커밋(`3cdff0c`, 로컬 == `origin/main`)으로 재빌드 → 코드 변경 0, 환경변수만 교체.

**검증 (전부 실측)**

| 항목 | 결과 |
|---|---|
| 새 호스트 `OPTIONS` (Origin: admin.turnflow.link) | ✅ **200** + `access-control-allow-origin: https://admin.turnflow.link` |
| 새 호스트 실제 `POST` | ✅ 401 (오답 자격증명의 정답) + CORS 헤더 동반 |
| 구 호스트 `OPTIONS` | 301 ← **차단 지점 재현** |
| 재배포 후 라이브 번들 | ✅ 구 호스트 **0건** / 새 호스트 2건 |
| 브라우저 실측(로그인 폼 제출) | ✅ `POST https://turnflow-api.clfy.ai.kr/api/v1/auth/login/ → 401`, CORS 오류 없음 |

**교훈 (재발 방지)**

> **호스트를 은퇴시키기 전에 "누가 이 호스트를 호출하는가"를 먼저 확정하라.**
> 서버측 `curl` 검증은 이 부류의 결함을 원리적으로 잡지 못한다 — `curl -L` 은 리다이렉트를 따라가지만
> 브라우저 프리플라이트는 따라가지 않는다. 최소한 다음을 확인할 것:
> ① 각 프론트의 **배포된 번들**을 직접 grep(레포 grep 으로는 부족 — 값이 CI/대시보드 빌드 변수에 있다),
> ② 대상 오리진으로 실제 `OPTIONS` 프리플라이트 발사.
> 또한 Caddy 에 **액세스 로깅이 없어** 사후에 "그 호스트로 누가 왔는가"를 집계할 수 없었다(§6 참조).

### 사고 ① — `pgbackrest.conf` 를 600 으로 바꿔 WAL 아카이빙이 3건 실패했습니다

- 원인: 이 파일은 DB 컨테이너에 `:ro` 마운트되어 컨테이너 내부 **`postgres` 유저**가 읽어야 하는데, 600 root:root 로 만들어 `archive_command`(pgbackrest archive-push)가 `Permission denied` 로 죽었습니다.
- 영향: `pg_stat_archiver.failed_count` 0 → **3**, `last_failed_wal = 00000001000000C000000001` (04:05:32~34 UTC)
- 복구: ~90초 내 감지 후 644 로 원복. PostgreSQL 이 실패 WAL 을 자동 재시도해 정상 아카이빙됐고, `archive_status/*.ready` 대기 큐 **0**, 데이터 손실 없음.
- 잔여 흔적: `failed_count` 는 누적값이라 `pg_stat_reset_shared('archiver')` 없이는 0 으로 돌아가지 않습니다. **앞으로 아카이버 상태를 점검할 때 "failed_count == 0" 같은 절대 조건을 쓰지 마세요** — 반드시 직전 기준선 대비 **증가분**으로 판정해야 합니다.

---

## 6. 남은 작업 — 제가 할 수 없거나 하지 않은 것

### ✅ CF 우회 경로 폐쇄 + 오리진 IP 은닉 (완료)

CF 대시보드가 스스로 경고했습니다: *"DNS 전용 레코드가 프록시된 레코드에 의해 숨겨진 IP 주소를
노출하고 있습니다."* 검증 과정에서 이것이 **단순 정보 노출이 아니라 실제 CF 우회 경로**임을 실증했습니다.

**실증한 우회 (조치 전):** 오리진 IP + 올바른 SNI + 인증서 검증 무시(`curl -k`)로
`turnflow-api.clfy.ai.kr` 의 전 API 에 도달 — `healthz 200` / `auth/login 401` / `IG웹훅 403`.
즉 **CF 의 레이트리밋·BIC·DDoS 흡수가 전부 우회**되고 있었습니다.
(다만 그 경로에서도 Django 스로틀은 작동 — 8번째부터 429.)

| 레코드 | 조치 | 결과 |
|---|---|---|
| `api.moderatube` | 프록시 전환 | ✅ 기존 `*.moderatube.clfy.ai.kr` 고급 인증서가 커버. CF→오리진은 525(Caddy 사이트 블록 없는 죽은 호스트) 지만 IP 는 은닉 |
| `monitor` | 프록시 전환 | ✅ **TLS 가 오히려 고쳐졌다** — 전엔 CF Origin 인증서를 직결로 내보내 브라우저가 거부(`curl -k` 필요)했는데, 전환 후 `ssl_verify=0` 정식 검증 통과 + netdata 200 + `client_ip` 허용목록 정상 |
| `api.turnflow` | 프록시 시도 → **90초 만에 원복** → **호스트 은퇴(301)** | ✅ 아래 |
| `turnflow-api` | **`@not_cf` 403 활성** | ✅ 오리진 직타 차단 |

**`api.turnflow` 는 프록시가 불가능합니다.** 프록시 직후 **000(TLS 핸드셰이크 실패)** → 즉시 원복.
원인은 엣지 인증서 커버리지이고, 이 존의 엣지 인증서는 3개뿐입니다:

```
*.moderatube.clfy.ai.kr, clfy.ai.kr, moderatube.clfy.ai.kr   고급   ~2026-10-17
*.clfy.ai.kr, clfy.ai.kr                                     범용   ~2026-10-10
*.clfy.ai.kr, clfy.ai.kr                                     백업   ~2026-08-27
```

`api.turnflow` 는 **2단계 서브도메인**이라 `*.clfy.ai.kr` 이 커버하지 않고 `*.turnflow.clfy.ai.kr`
인증서는 없습니다. **바로 이 이유로 과거에 1단계 호스트 `turnflow-api` 를 만들어 실 API 호스트로
옮긴 것입니다.** `고급 인증서 주문` 버튼은 disabled 이고 ACM 은 비활성입니다(유료).

**그래서 이렇게 마무리했습니다 (삭제도, 403 도 아닌 301):**

1. **`INSTAGRAM_REDIRECT_URI` 를 `turnflow-api` 로 이전** — Meta 앱의 OAuth 리디렉션 URI 에
   신규 값을 **먼저 추가**(Meta 는 복수 등록 허용)해 두었기 때문에 **끊김 0**. env 변경 후
   **web 티어 3개만** 재생성(이 값은 `views.py` 에서만 쓰이고 celery 는 안 씀 → 블라스트 반경 축소).
   검증: `settings.INSTAGRAM_REDIRECT_URI` 와 실제 생성되는 authorize URL 의 `redirect_uri` 가
   모두 새 호스트.
2. **`turnflow-api` 블록에 `@not_cf` 403 활성** — CF 대역 + **루프백/도커 브리지**를 허용.
   ⚠️ `127.0.0.1` 을 반드시 포함해야 합니다 — DR 드릴이
   `curl --resolve turnflow-api.clfy.ai.kr:443:127.0.0.1` 로 로컬 Caddy 준비 상태를 확인하므로
   빼면 드릴이 403 으로 깨집니다(`deploy/dr/gcp/drill.sh`).
   (`/api/v1/healthz*` 는 `@health` handle 이 `@not_cf` 보다 앞이라 계속 도달 — CF LB 모니터·DR 용으로 의도된 동작.)
3. **`api.turnflow` 사이트 블록(171줄)을 301 리다이렉트(19줄)로 은퇴** —
   `redir https://turnflow-api.clfy.ai.kr{uri} permanent`.
   삭제나 403 이 아니라 301 을 택한 이유: 리다이렉트 URI 를 방금 옮겼으므로 Meta 가 **옛 URI 로
   브라우저를 돌려보내는 in-flight OAuth 플로우**가 남아 있을 수 있고, 301 이면 쿼리스트링
   (`?code=...`)까지 보존되어 **그 사용자도 성공**합니다. 동시에 API 가 이 호스트에서 서빙되지
   않으므로 우회로는 닫힙니다.

### 검증 (전부 실측)

| 항목 | 결과 |
|---|---|
| **실 Meta 웹훅 (조치 후)** | **15분간 50건 수신 / 40건 `200` 성공** — UA `facebookexternalua`, Django 로그로 확인 |
| CF 경유 웹훅이 Django 까지 도달하는가 | ✅ Django 가 `invalid X-Hub-Signature-256 → 403 (enforced)` 를 직접 기록 = Caddy 차단 아님 |
| 오리진 직타 웹훅 | ✅ **Django 로그에 아예 없음** = Caddy 가 차단 |
| 오리진 직타 `auth/login` | ✅ 403 (조치 전 401 → Django 도달했었음) |
| `api.turnflow` → 301 | ✅ 경로·쿼리 보존 (`?code=TESTCODE&state=abc` 그대로) |
| `api.turnflow` OAuth 콜백 `-L` 추적 | ✅ 200 (in-flight 안전망 작동) |
| `api.turnflow` 의 auth/webhook/admin | ✅ 전부 301 = API 미서빙 = 우회로 폐쇄 |
| CF 경유 정상 트래픽 | ✅ healthz 200 · ready 200 · track/visit 204(POST) · admin/me 401 · 토스웹훅 200 · auth 401 · OAuth콜백 200 |
| 프론트·어드민·monitor·llm | ✅ 200 / 307 / 200 / 401 |
| 정상 사용자 403 피해 | ✅ **0건** (사무실 IP 403 이력 0, Caddy 403 로그 0) |
| DR 드릴 패턴(127.0.0.1) | ✅ healthz 200 · healthz/ready 200 |
| Caddy 오류 | 6건 전부 무해 — `no OCSP stapling for cloudflare origin certificate`, `HTTP/2 skipped because it requires TLS`(포트 80) |

### 남은 항목

- **`api.turnflow` DNS A 레코드는 아직 남아 있습니다** — 이것이 오리진 IP `121.126.99.70` 을 노출하는
  **유일한** 레코드입니다. 며칠간 트래픽이 없음을 확인한 뒤 삭제하면 IP 노출까지 사라집니다.
  지금은 301 만 서빙하므로 노출의 실익은 "L7 플러딩 표적" 정도로 축소됐습니다.
- Meta 앱에서 **옛 OAuth 리디렉션 URI(`api.turnflow...`) 제거** — 신규 URI 로 정상 동작을 며칠 확인한 뒤.
- ⚠️ **CF 대역이 갱신되면 `@not_cf` 목록도 갱신**해야 합니다(https://www.cloudflare.com/ips/).
  현재 목록은 실측된 CF 출처(`162.159.98.129`, `172.68.22.64`)를 모두 커버합니다.
- ⚠️ **`api.turnflow` DNS 레코드를 지우기 전에** 어드민 콘솔이 새 호스트를 쓰고 있는지 재확인하세요
  (2026-08-04 로 교체 완료). 이제는 301 도 없어지므로 구 호스트를 쓰는 클라이언트는 즉시 완전 실패합니다.

### ⚠️ Caddy 에 액세스 로깅이 없습니다 (이번 사고에서 실제로 걸림돌이 됐습니다)

`/root/caddy/Caddyfile` 어느 블록에도 `log` 지시자가 없어, 컨테이너 로그에는 시작/에러만 남고
**요청 로그가 전혀 없습니다.** 그래서 어드민 장애 때 "어떤 클라이언트가 `api.turnflow` 로 왔는가" 를
사후에 집계할 수 없었고, 대신 각 프론트의 배포 번들을 직접 grep 해서 범위를 확정해야 했습니다.

마케팅 트래픽이 들어오기 전에 최소한의 액세스 로깅을 켜두는 것을 권합니다(호스트·경로·상태·UA·Origin).
디스크 증가가 걱정되면 `roll_size` / `roll_keep` 으로 제한하면 됩니다.

### ⚠️ 레포의 `deploy/caddy/Caddyfile` 은 라이브와 다릅니다

배포 스크립트는 Caddy 를 건드리지 않으므로(확인함) 자동 원복 위험은 없지만, 이 사본을 라이브에
복사하면 HSTS·`@not_cf`·`/admin` 허용목록·api.turnflow 은퇴가 **전부 무효화**됩니다.
그래서 해당 파일 상단에 경고 헤더를 넣어뒀습니다. 여유가 생기면 라이브 내용으로 동기화하세요.

### ⏳ break-glass — Cloudflare Tunnel SSH (사용자 작업 필요)

SSH 를 2개 IP 로 제한했으므로 IP 가 바뀌면 접속 불가입니다. 현재 복구 수단:

| 순위 | 수단 | 현황 |
|:--:|---|---|
| 1 | `turnflow-fw-allow-ip.sh add <새IP>` | ✅ 설치·왕복 테스트 완료. **다른 허용 IP 에서 접속 가능할 때만** 유효 |
| 2 | iDRAC9 SOL 콘솔 | ⚠️ 존재하고 SOL 활성이지만 **IP `192.168.0.120`, 게이트웨이 0.0.0.0** = 콜로 내부 관리망 전용 → **업체 경유 필요**(느림). BMC `root` 는 ADMINISTRATOR |
| 3 | `turnflow-fw-panic.sh` | ✅ 방화벽 제한 전면 해제(콘솔에서 실행) |
| 4 | **Cloudflare Tunnel SSH** | ⏳ **미구성 — 권장.** outbound 443 만 쓰므로 IP 변동에 완전 면역 |

4번을 권합니다. **이미 같은 패턴을 쓰고 계십니다** — `~/.ssh/config` 에 `goldngoose` 가 `ProxyCommand cloudflared access ssh` 로 들어 있고, 로컬에 cloudflared 2026.5.2 가 설치돼 있습니다. 서버 측 설정에 `cloudflared tunnel login`(브라우저 OAuth)이 필요해 제가 대신 할 수 없습니다.

### ⏳ P1-5 / P1-6 — 코드 변경(이미지 재빌드 필요)

**오늘 하지 않은 이유:** 인프라 하드닝과 코드 배포를 섞지 않는 것이 원칙입니다.

> **↻ 정정 (2026-08-04 후반) — 배포 블로커는 제가 적은 것보다 훨씬 작습니다.**
> "미배포 커밋 17개"는 잘못된 계산이었습니다. 실측:
> - `git log 086aea9..HEAD` = **4커밋**, 전부 `insta_reports`. HEAD = `4fa34f1`.
> - **`celery_reports` 는 이미 `4fa34f1`(=HEAD)로 운영 중** — 즉 그 4커밋은 이미 프로덕션에서 돌고 있고,
>   뒤처진 건 web 티어 3개뿐입니다.
> - 그 4커밋이 건드린 파일은 `insta_reports/pipeline/*` · `service.py` · 템플릿 · 테스트뿐.
>   **views / serializers / urls / settings / models / migrations 변경 0건** → web 티어 동작에 사실상 무영향.
> - `deploy.sh:27` 은 **서버에서** `git pull` 후 그 트리로 빌드합니다 → 로컬 워킹트리의 미커밋 변경
>   (현재 billing 리퍼럴 WIP 5파일)은 배포에 **들어가지 않습니다**.
>
> 결론: 아래 코드 변경들은 지금 **낮은 위험으로 배포 가능**합니다. 이미지 스큐 수렴(선행조건 1)도
> 같은 배포로 자연히 해결됩니다.

배포 전 **선행 조건 2개**(사전검증에서 발견):

1. **이미지 스큐를 먼저 수렴시키세요.** 현재 web 전 티어 = `086aea9`, `celery_reports` = `8e1558d`, `latest` = `8e1558d`. 코드 변경 없이 `deploy.sh` 를 한 번 돌려 전부 같은 이미지로 맞춘 뒤 스로틀/의존성을 별도 배포하세요.
2. **`.deploy.prev` 를 신뢰하지 마세요.** `deploy.sh:15-16` 이 무조건 덮어쓰고 `rollback.sh` 가 그 값을 그대로 씁니다. 현재 값은 `bfc79fa`(03:10 배포 *이전* 이미지)이므로, 검증 없이 롤백하면 **16커밋을 되돌립니다**. 실제 실행 이미지 표를 `/root/rollback_pointer_20260804.txt`(600)에 대역외로 고정해 두었습니다.

**추가로 반드시 넣어야 할 1줄 — `NUM_PROXIES`.** `config/settings/base.py` 의 `REST_FRAMEWORK` 에
`"NUM_PROXIES": 2` 를 추가하세요. 없으면 CF 프록시 호스트에서 DRF 스로틀 키가 XFF 전체 문자열이 되어
**인증 스로틀이 희석**됩니다(§3.5 말미, 실측 확인). 이건 새 스로틀을 켜기 전에 들어가야 의미가 있습니다.

**P1-6 스로틀에는 선행 조건이 하나 더 있습니다.** HTTP 429 는 이미 `PlanLimitExceededError`(`error.code = "PLAN_LIMIT_EXCEEDED"`)가 쓰고 있고, 프론트 계약서(`INSTA_REPORT_FRONTEND.md:155` 등)가 429 → 유료 제한 모달 + `paywall_viewed` 분석 이벤트로 분기하도록 문서화돼 있습니다. 스로틀을 그냥 켜면 스로틀 429 와 플랜한도 429 가 **구분 불가**해져 `analytics_checkout_event` 데이터가 **되돌릴 수 없게 오염**됩니다.
→ 먼저 `apps/core/exceptions.custom_exception_handler` 에 `Throttled` 분기를 추가해 `error.details.code = "RATE_LIMITED"` + `retry_after` 를 내보내고, 프론트가 그 값으로 분기하도록 계약을 갱신한 뒤에 스로틀을 켜세요.

또한 **전역 `AnonRateThrottle` 은 넣지 마세요** — IG 웹훅(`AllowAny`)과 토스 웹훅에 적용되어 Meta 버스트를 429 로 막고, Meta 는 반복 실패 시 웹훅 구독을 auto-disable 합니다. 대상 엔드포인트에 `ScopedRateThrottle` 만 붙이는 방식으로 가세요.

### ⏸ 보류한 것

- **P2-2 `pg_hba` trust 제거**: 편집 자체는 무해(그 4줄의 소비자를 찾지 못함)하지만, pgbackrest 가 unix 소켓으로 붙으므로 잘못 건드리면 `archive_command` → WAL 적체 → 디스크 → **DB 정지**입니다. 오늘 이미 이 경로에서 사고를 한 번 냈으므로(§5) 같은 창에서 재시도하지 않았습니다. 유지보수 창에서, **절대값이 아닌 증가분 게이트**로 진행하세요.
- **P2-3 컨테이너 비루트화/`cap_drop`**: 17개 컨테이너 재생성이 필요하고 `cap_drop: [ALL]` 은 entrypoint 의 chown/collectstatic 을 깨뜨릴 수 있습니다. `no-new-privileges:true` 만 먼저 넣는 것이 안전합니다.
- **gemma 포트 재바인딩**: 위 §3 참조 (다음 gemma 재시작 시 자동 반영).
- **IPv6**: `sshd` 가 `[::]:2222`, netdata 가 `[::]:19999` 를 듣지만 `eno8303` 에 글로벌 v6 주소·기본경로가 없어 도달 불가입니다. 콜로가 RA/DHCPv6 를 켜면 IPv4 허용목록이 즉시 우회됩니다 → `ip6tables` 미러링 또는 `AddressFamily inet` 을 계획하세요(후자는 소켓 활성화와 충돌 검토 필요).
- **root/clfy 비밀번호 교체**: 원격 인증에서는 폐지됐지만 **콘솔·sudo 용으로는 아직 유효**하며, 유출된 값 그대로입니다. `passwd` 는 대화형이라 제가 실행할 수 없습니다 — **직접 교체하세요.**

---

## 7. 롤백

각 항목은 독립적으로 되돌릴 수 있습니다.

```bash
# 방화벽 (1초, 현재 동작과 바이트 단위 동일하게 복원)
iptables -D INPUT -j TF_INPUT
systemctl disable --now turnflow-fw-hardening.timer
# 잠긴 경우: 콘솔에서 /usr/local/sbin/turnflow-fw-panic.sh

# SSH 비번인증 되살리기
rm /etc/ssh/sshd_config.d/00-turnflow-hardening.conf && sshd -t && systemctl reload ssh

# Redis 인증 해제 (즉시, 무중단)
docker exec turnflow_instagram_redis redis-cli --user default --pass "$(grep '^REDIS_PASSWORD=' /opt/turnflow_backend/.env.production | cut -d= -f2-)" --no-auth-warning CONFIG SET requirepass ""
#   설정까지 되돌리려면: /opt/turnflow_backend/{.env.production,docker-compose.prod.yml}.bak.redisauth-* 복원 후
#   APP_IMAGE 를 /root/rollback_pointer_20260804.txt 대로 명시해 재생성

# Caddy
cp /root/caddy/Caddyfile.bak.pre-hardening-* /root/caddy/Caddyfile
docker exec -w /etc/caddy caddy caddy validate --config /etc/caddy/Caddyfile && \
docker exec -w /etc/caddy caddy caddy reload --config /etc/caddy/Caddyfile

# vastai 권한 복원
cat /root/backups/p1-3-vastai/sudoers.bak > /etc/sudoers && visudo -c
usermod -g 988 vastai_kaalia && gpasswd -a vastai_kaalia docker && gpasswd -a vastai_kaalia libvirt
usermod -s /bin/bash vastai_kaalia

# gemma 네트워크 되붙이기
docker network connect turnflow_instagram_net gemma-vllm

# needrestart / apt
rm -f /etc/needrestart/conf.d/99-turnflow.conf /etc/apt/apt.conf.d/52turnflow-unattended-upgrades
```

**백업 위치:** `/root/fwbak/iptables-*.v4` · `/usr/local/sbin/turnflow-fw-hardening.v1.bak` · `/root/caddy/Caddyfile.bak.pre-hardening-*` · `/root/backups/p1-3-vastai/` · `*.bak.redisauth-*` · `/root/secrets-archive/` · `/root/rollback_pointer_20260804.txt`

---

## 8. 새로 생긴 운영 자산

| 경로 | 용도 |
|---|---|
| `/usr/local/sbin/turnflow-fw-hardening.sh` | 방화벽 규칙 (멱등, 자기 IP 가드 내장) |
| `/usr/local/sbin/turnflow-fw-panic.sh` | 방화벽 제한 전면 해제 (데드맨/콘솔용) |
| `/usr/local/sbin/turnflow-fw-allow-ip.sh` | `show` / `add <ip>` / `set <ip>...` — IP 변경 시 복구 |
| `/etc/systemd/system/turnflow-fw-hardening.{service,timer}` | 5분 주기 규칙 복원 (docker 재시작·부팅 대응) |
| `/etc/cron.d/turnflow-pgbackrest` | 주1회 full + 매일 diff 백업 |
| `/etc/needrestart/conf.d/99-turnflow.conf` | containerd 자동 재시작 차단 |
| `/root/rollback_pointer_20260804.txt` | 서비스별 실행 이미지 표 (롤백 기준) |
| `~/.ssh/turnflow_prod_admin_ed25519` (로컬) | prod 접속 키 — **패스프레이즈 없음, 백업 필수** |

> ⚠️ 새 SSH 개인키는 패스프레이즈가 없습니다(자동화용). 이 PC 가 유일한 사본이므로 **안전한 곳에 백업**하고, PC 자체 보안(디스크 암호화)을 확인하세요.

---

## 9. 검증 요약 (조치 후 실측)

```
DM 발송            10분 4건 / 1시간 48건, 상태 read·delivered  → 정상 흐름
웹훅 수신          10분 2건, 오류 0
billing 태스크      15분 3건 성공, 오류 0
큐 적체            celery·dm_send·webhook_followup·verify·billing·reports 전부 0
컨테이너           17개 running, 인증오류 0
이미지             086aea9 × 8 + 8e1558d(celery_reports) — 재생성 전과 동일
SSH 비번인증       no
SSH 허용 IP        121.133.95.25, 14.52.76.113
방화벽 타이머       active,  TF_INPUT 누적 DROP ssh 93 / 전체 344
SSH 무차별 대입     최근 20분 0건  (조치 전 분당 약 74회)
Redis 무인증       NOAUTH Authentication required
litellm → Redis    NOAUTH (lateral 경로 폐쇄)
vastai sudo        not allowed,  docker 그룹 = root,clfy
.env.production    600
gemma 네트워크      vllm-server_default 만
백업               full 20260804-040646F + diff, cron 등록
HSTS               max-age=31536000
/admin             사무실·재택 200 / 그 외 403 / 헤더 위조 우회 불가
/api/v1/admin      401 (차단 아님 — 외주 뷰어 정상)
```
