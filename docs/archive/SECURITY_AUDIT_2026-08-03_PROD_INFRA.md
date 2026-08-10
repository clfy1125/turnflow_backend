# Prod 서버 인프라 보안 점검 보고서 (실측)

> **작성:** 2026-08-03
> **대상:** colo prod `121.126.99.70` (호스트명 `clfy`, Ubuntu 24.04 LTS, 64c/251G) — TurnFlow prod 스택 전체
> **계기:** 마케팅 비용 집행 직전, 트래픽·공격면 동시 증가에 대비한 사전 점검
> **방법:** SSH 실접속 read-only 실측(설정/로그/컨테이너/런타임 값) + 외부 관점 포트스캔·HTTP 프로브 + 코드 감사(`../ops/SECURITY_AUDIT_2026-06.md`) 대조 검증
> **성격:** 이 보고서는 **인프라/배포 계층** 중심입니다. 애플리케이션 코드 취약점은 `../ops/SECURITY_AUDIT_2026-06.md`(100건) 가 원본이며, 여기서는 그중 **prod 실값으로 검증된 것만** 상태를 갱신했습니다.

---

## ⚠️ 2026-08-04 갱신 — 조치 완료 및 이 문서의 정정 사항

**대부분의 항목이 2026-08-04 에 조치됐습니다. 실행 기록·검증 결과·롤백 절차는
[PROD_HARDENING_2026-08-04.md](../ops/PROD_HARDENING_2026-08-04.md) 를 보세요.**

조치 전 사전검증(read-only, 적대적 반증 포함) 과정에서 **이 문서의 다음 내용이 틀렸음**이 확인됐습니다.
아래 항목을 읽을 때 반드시 함께 보세요.

| 이 문서의 위치 | 틀린 내용 | 정정 |
|---|---|---|
| **P0-1 조치 예시** | 하드닝 드롭인을 `99-hardening.conf` 로 만들라고 안내 | ❌ **그러면 조용히 무효가 된다.** `sshd_config` 12행의 `Include` + OpenSSH 의 "먼저 얻은 값 우선" 규칙 때문에 `50-cloud-init.conf` 의 `PasswordAuthentication yes` 가 이긴다. **`00-` 접두사여야 함**(실측 증명) |
| **P1-4 전체** | "자동 보안 업데이트가 masked 되어 꺼져 있다" | ❌ **이미 켜져서 동작 중.** masked 된 `unattended-upgrades.service` 는 *종료 시 인터록*이고 드라이버는 `apt-daily-upgrade.timer`(enabled·active). origins 도 이미 security-only. **실제 위험은 `needrestart` 가 containerd 를 자동 재시작하는 것**(2026-07-28 실제 발생) |
| **P1-4** | "security 포켓 0건은 내 grep 오류" | ❌ **grep 이 맞았다** — security 포켓이 실제로 비어 있음. 대기 188개는 전부 비보안 `-updates`·서드파티 |
| **§1 P0-4 / llm.clfy.ai.kr** | "llm 호스트에 Caddy 레벨 인증이 없다" | ❌ **저장소 사본만 보고 판단한 오류.** 서버의 실제 Caddyfile 에는 2026-07-08 부터 `@blocked not client_ip …` 허용목록이 **활성**이었다 |
| **P2-4** | "마지막 full 백업 2026-06-30, 34일 경과" | ❌ **2026-07-06, 29일 경과** (`pgbackrest info` 출력이 잘려 뒷부분을 놓쳤음) |
| **P1-1** | `pgbackrest.conf` 도 600 으로 조치 대상 | ⚠️ **600 으로 바꾸면 WAL 아카이빙이 멈춘다.** 이 파일은 DB 컨테이너에 `:ro` 마운트되어 컨테이너 내부 `postgres` 유저가 읽어야 한다. 실제로 시도해 3건 실패 후 644 로 원복(상세: 하드닝 기록 §5) |
| **§4 침해 흔적** | `pg_stat_archiver.failed_count` 등을 절대값으로 판정 | ⚠️ 이 카운터는 **누적값**이며 `pg_stat_reset_shared('archiver')` 없이는 0 으로 돌아가지 않는다. 앞으로 아카이버 점검은 **직전 기준선 대비 증가분**으로 판정할 것 |

추가로, 진단 시점 이후 **2026-08-04 03:10 에 `086aea9` 배포**가 있었고 03:28 에 `celery_reports` 만
`8e1558d` 로 따로 올라가 **이미지 스큐 + `.deploy.prev` 불일치**가 생겼습니다. `rollback.sh` 를 검증 없이
실행하면 16커밋을 되돌립니다 — 실행 이미지 표를 `/root/rollback_pointer_20260804.txt` 에 고정해 두었습니다.

---

## 0. 한눈에 보기

### 결론

마케팅 집행 전에 **P0 5건은 반드시 처리**해야 합니다. 이 중 1·2번은 "언젠가 위험"이 아니라 **지금 진행 중인 공격에 노출된 상태**입니다(7일간 SSH 무차별 시도 42,738건 실측).

반면 **애플리케이션 계층은 생각보다 건강합니다.** 6월 감사의 최대 이슈였던 웹훅 HMAC 은 실제로 enforce 되어 있고(`WEBHOOK_HMAC_ENFORCED=True` 런타임 확인), 인증 스로틀도 실동작합니다(11번째 요청 429 실측). 문제는 거의 전부 **호스트/네트워크 경계와 운영 위생**에 몰려 있습니다.

### 심각도 분포 (이번 실측 기준)

| 심각도 | 건수 | 내용 |
|:---:|:---:|---|
| 🔴 **P0 (집행 전 차단)** | 5 | SSH root 비번노출·무인증 Redis 인접·호스트 방화벽 부재·엣지 방어 부재·admin 브루트포스 |
| 🟠 **P1 (1~2주)** | 6 | 시크릿 파일 권한·netdata 공개·유휴 특권계정·자동패치 masked·의존성 CVE·전역 스로틀 부재 |
| 🟡 **P2 (1개월)** | 5 | HSTS/쿠키·pg_hba trust·컨테이너 root·백업 정지·SSH 부수 하드닝 |
| ✅ **양호(검증됨)** | 12 | 아래 §5 |

### P0 요약표

| # | 문제 | 현재 상태 (실측) | 조치 난이도 |
|:---:|---|---|:---:|
| **1** | SSH root **비밀번호** 인증이 인터넷에 열림 + 공격 진행 중 | `PermitRootLogin yes`, `PasswordAuthentication yes`, fail2ban **미설치**, 7일 실패 42,738건 | 중 (키 등록 선행 필수) |
| **2** | 무인증 Redis 가 **침해 이력 컨테이너와 같은 네트워크** → Celery 브로커 장악 | `requirepass` **빈 값**, litellm-proxy 에서 무인증 접속 **실증 성공** | 중 |
| **3** | 호스트 방화벽 없음 + 내부포트 보호가 **inactive 유닛**에 의존 | `ufw inactive`, `INPUT policy ACCEPT`, `turnflow-fw-hardening` = `inactive (dead)` | 하 |
| **4** | **API 오리진이 Cloudflare 를 경유하지 않음** → 엣지 DDoS/봇 방어 0 | `api.turnflow.clfy.ai.kr` → `121.126.99.70` 직결 (CF-RAY 없음) | 하 (DNS 토글) |
| **5** | Django admin 무제한 브루트포스 | `/admin/login/` 15연타 전부 200, django-axes 미설치, superuser 2명 | 하 |

---

## 1. 🔴 P0 — 마케팅 집행 전 차단

### P0-1. SSH root 비밀번호 인증이 인터넷에 열려 있고, 실제로 공격받는 중

**실측 증거**

```
sshd -T:  port 2222 · permitrootlogin yes · passwordauthentication yes
          maxauthtries 6 · logingracetime 120 · (AllowUsers 없음)
/root/.ssh/authorized_keys        → 0 바이트 (키 미등록)
/home/clfy/.ssh/authorized_keys   → 0 바이트 (키 미등록)
fail2ban-client → command not found  (미설치)
ufw status      → inactive
iptables -P INPUT ACCEPT

최근 7일 sshd 로그 (journalctl):
  Failed password : 42,738건
  Invalid user    : 12,798건
  Accepted        :    228건
  시도 대상 계정 top: root 29,247 / user 2,946 / admin 2,254 / test 1,866 / postgres 83
  공격 top IP: 45.198.224.237(9,463) 43.108.48.236(5,563) 159.89.132.35(5,511) …
lastb 누적 실패: 20,755건
```

**왜 P0인가.** 이 서버 한 대에 prod PostgreSQL(고객 데이터), 전 테넌트 **IG 액세스 토큰**, **토스 빌링키**, R2 키, LLM 키가 모두 있습니다. 그리고 그 전체가 **root 비밀번호 하나**로 방어됩니다 — 키가 등록돼 있지 않으므로 비밀번호가 유일한 인증 요소입니다. 계정 잠금(fail2ban/pam_faillock)도, 방화벽 레이트리밋도 없어서 공격자는 **무제한 시도**가 가능합니다. `MaxAuthTries 6`은 세션당 제한일 뿐 재접속을 막지 않습니다.

**추가로 시급한 사유 2가지**

1. **현재 비밀번호가 이번 작업 요청 과정에서 평문으로 공유되었습니다** → 전사 기록·로그·클립보드 등 원 저장 위치를 벗어났다고 가정하고 **회전 대상**으로 취급해야 합니다.
2. 현재 비밀번호는 **한글 두벌식 키보드 패턴 + 연속 숫자 + 기호** 구조입니다. 한국 대상 공격자가 쓰는 사전에 이 패턴군이 포함돼 있어, 길이만으로 안전을 가정할 수 없습니다.

**조치 (순서 엄수 — 순서 틀리면 락아웃)**

```bash
# ① 먼저 키 등록 (이걸 건너뛰고 ②를 하면 접속 불가)
#   로컬에서:  ssh-keygen -t ed25519 -C "operator@turnflow"
#   서버에서:  mkdir -p /root/.ssh && chmod 700 /root/.ssh
#             echo "<공개키>" >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
# ② 새 세션에서 키 접속 성공을 확인한 뒤에만 아래 적용
#   /etc/ssh/sshd_config.d/99-hardening.conf
#     PasswordAuthentication no
#     PermitRootLogin prohibit-password
#     KbdInteractiveAuthentication no
#     MaxAuthTries 3
#     LoginGraceTime 20
#     X11Forwarding no
#     AllowUsers root clfy
#   ⚠️ /etc/ssh/sshd_config.d/50-cloud-init.conf 가 PasswordAuthentication yes 로
#      덮어쓰고 있으므로, 99- 파일이 뒤에 오도록 하거나 50- 파일을 수정할 것
#   sshd -t && systemctl reload ssh
# ③ root/clfy 비밀번호 즉시 교체 (키 전환 후에도 sudo/콘솔용으로 필요)
# ④ fail2ban 설치 (키 전용 전환 후에도 로그 노이즈·자원 소모 차단용)
#   apt install fail2ban  →  [sshd] enabled, port 2222, maxretry 3, bantime 1h
```

> **권장 추가:** 2222 포트를 운영자 IP/VPN 으로 제한(ufw — P0-3 과 함께). 콜로라서 콘솔 접근 수단이 있는지 먼저 확인하세요.

---

### P0-2. 무인증 Redis 가 침해 이력 컨테이너와 같은 네트워크에 있음 → Celery 브로커 장악 = 사실상 RCE

**실측 증거**

```
redis-cli CONFIG GET requirepass  →  (빈 값)      # 인증 없음
redis-cli CONFIG GET bind         →  * -::*       # 컨테이너 내 전 인터페이스
CELERY_BROKER_URL = redis://redis:6379/0          # 인증정보 없음

turnflow_instagram_net (172.18.0.0/16) 동일 네트워크 구성원:
  turnflow_instagram_redis 172.18.0.3    ← 무인증 브로커
  turnflow_instagram_db    172.18.0.2
  pgbouncer                172.18.0.4
  web_* / celery_* (7개)
  litellm-proxy            172.18.0.14   ← 2026-07-07 크립토마이너 침해 당사자
  gemma-vllm               172.18.0.13   ← 0.0.0.0:8088 바인딩
  caddy                    172.18.0.15

▶ 실증: litellm-proxy 컨테이너 안에서 무인증 접속 성공
   docker exec litellm-proxy python3 -c "socket→turnflow_instagram_redis:6379, 'INFO server'"
   → "$627 # Server redis_version:7.4.8"   (응답 정상 수신)
```

**공격 경로.** litellm-proxy 는 **이미 한 번 침해된 서비스**이고(2026-07-07), 현재 상태가 `Up 3 weeks (unhealthy)` 로 3주간 갱신되지 않은 `main-stable` 이미지입니다. 이 컨테이너(또는 gemma-vllm, netdata 중 하나)가 다시 뚫리면:

1. 무인증 Redis 에 접속 → Celery 브로커(`db 0`)에 **임의 태스크 페이로드 삽입**
2. celery 워커가 그 태스크를 실행 — 워커는 **root 로 실행**되며 DB·IG 토큰 복호화 키·토스 키에 접근 가능
3. 동시에 캐시 DB(`db 1`) 조작 가능 → `rate_governor` 센티넬 오염으로 **DM 전면 정지**(기존 메모: 캐시 flush 시 1h DM 정지 함정)도 가능

즉 **컨테이너 하나의 침해가 서비스 전체 장악으로 직결**됩니다. Redis 가 호스트에 퍼블리시되지 않았다는 점(외부 스캔에서 6379 closed 확인)이 유일한 완화책이며, 이는 내부 lateral movement 를 전혀 막지 못합니다.

**조치**

```bash
# ① Redis 인증 켜기
#   docker-compose.prod.yml: command: redis-server --requirepass ${REDIS_PASSWORD} --appendonly no
#   .env.production: REDIS_PASSWORD=<48자 랜덤>
#                    CELERY_BROKER_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
#                    (django_redis CACHES LOCATION 도 함께 갱신 — 누락 시 캐시 전면 실패)
#   ⚠️ 순서: redis 재시작 → web/celery 전체 재시작. 중간에 브로커 인증 불일치 창이 생기므로
#      DM 큐가 한가한 시간대에. 재시작 후 dm-backlog-alert / queue-state 로 즉시 확인.
# ② 네트워크 분리 (더 근본적)
#   litellm-proxy / litellm-db / gemma-vllm / netdata 를 turnflow_instagram_net 에서 제거하고
#   별도 llm_net 으로. Caddy 만 양쪽에 붙이고, 앱→LLM 은 명시적 필요 경로만 허용.
# ③ litellm-proxy 이미지 갱신 + unhealthy 원인 해소 (또는 미사용이면 제거)
```

---

### P0-3. 호스트 방화벽이 없고, 내부 포트 보호가 `inactive` 상태인 oneshot 유닛에 의존

**실측 증거**

```
ufw status              → inactive
iptables -P INPUT       → ACCEPT      (기본 허용)
0.0.0.0 바인딩 컨테이너 : gemma-vllm 0.0.0.0:8088→8000 · caddy 0.0.0.0:80/443
0.0.0.0 바인딩 호스트   : netdata 0.0.0.0:19999 · sshd 0.0.0.0:2222

유일한 보호막 = /usr/local/sbin/turnflow-fw-hardening.sh (2026-07-07 침해 대응으로 작성)
  iptables -I DOCKER-USER -i eno8303 --ctorigdstport 8088 -j DROP
  iptables -I DOCKER-USER -i eno8303 --ctorigdstport 4040 -j DROP
  iptables -I INPUT -i eno8303 --dport 19999 -j DROP

systemctl status turnflow-fw-hardening
  Loaded: enabled ;  Active: inactive (dead)   ← RemainAfterExit=yes 인데 active 가 아님
  ExecMainStartTimestamp = (빈 값)             ← 이번 부팅에서 실행된 기록이 없음
현재 규칙은 실재함 (iptables -S DOCKER-USER 로 3줄 모두 확인)
외부 스캔 결과: 80/443/2222 만 OPEN — 8088·4040·19999·6379·5432 전부 차단 ✅
```

**평가.** 지금 이 순간은 막혀 있습니다. 문제는 **보호 방식이 취약하다는 것**입니다.

- 기본 정책이 `ACCEPT` 이므로 "명시적으로 DROP 한 3개 포트만" 안전합니다. 앞으로 누가 컨테이너를 하나 띄우며 `-p 9000:9000` 을 쓰면 **즉시 인터넷에 공개**됩니다. 이번 마케팅 준비로 컨테이너/서비스를 추가할 가능성이 높아 특히 위험합니다.
- 규칙이 인터페이스명 `eno8303` 하드코딩에 의존합니다.
- 유닛이 `inactive` 이고 이번 부팅 실행 기록이 없습니다 → 규칙이 사라지는 이벤트(docker 재시작, 수동 flush) 뒤에 **자동 복구가 보장되지 않습니다**. 사라지면 정확히 **2026-07-07 크립토마이너 침해 경로(4040·8088)가 재개방**됩니다.

**조치**

```bash
# ① 포트 바인딩부터 고치기 (방화벽보다 근본적)
#   gemma-vllm: -p 8088:8000  →  -p 127.0.0.1:8088:8000
#   netdata:    19999 을 127.0.0.1 로 (또는 컨테이너 네트워크 전용)
# ② ufw default deny 도입
#   ufw default deny incoming ; ufw default allow outgoing
#   ufw allow 2222/tcp comment 'ssh'   ← ★ 먼저! 안 하면 즉시 락아웃
#   ufw allow 80,443/tcp
#   ufw enable
#   ⚠️ ufw 는 DOCKER-USER 를 거치는 컨테이너 퍼블리시 포트를 막지 못한다 →
#      ①(127.0.0.1 바인딩)과 반드시 병행. ufw-docker 도입도 검토.
# ③ 규칙 영속화 검증: iptables-persistent 또는 유닛을 docker.service 재시작에 연동
```

---

### P0-4. API 오리진이 Cloudflare 를 경유하지 않음 → 엣지 DDoS/봇/레이트리밋 방어가 0

**실측 증거**

```
DNS 해석 + 응답 헤더:
  api.turnflow.clfy.ai.kr   → 121.126.99.70                 ★오리진 직결 (CF-RAY 헤더 0개)
  turnflow-api.clfy.ai.kr   → 104.21.6.32, 172.67.154.158    CF 프록시
  link.turnflow.clfy.ai.kr  → 104.21.6.32, 172.67.154.158    CF 프록시
  llm.clfy.ai.kr            → 104.21.6.32, 172.67.154.158    CF 프록시
  turnflow.link             → 104.21.81.229, 172.67.165.105  CF 프록시
  admin.turnflow.link       → 104.21.81.229, 172.67.165.105  CF 프록시
  monitor.clfy.ai.kr        → 121.126.99.70                 ★오리진 직결

Caddy: rate_limit 모듈 없음 (스톡 바이너리 — Caddyfile 주석에 명시)
       @not_cf 차단 블록 = 주석 처리 상태
Django: DEFAULT_THROTTLE_CLASSES = None  (전역 스로틀 없음)
```

**핵심 모순.** `deploy/caddy/Caddyfile` 은 `Cloudflare(엣지) → Caddy → Django` 3계층을 전제로 작성돼 있고, `trusted_proxies static <CF CIDR>` + `client_ip_headers Cf-Connecting-Ip` 까지 설정해 뒀습니다. 그런데 **실제로 api 호스트 앞에 Cloudflare 가 없습니다.** 다른 호스트는 전부 프록시인데 api 만 직결입니다.

결과: 백엔드 API 의 방어층이 **DRF scoped throttle 하나**뿐입니다. 그 위의 계층(엣지 DDoS 흡수, Bot Fight, 국가/ASN 규칙, WAF, 챌린지, 오리진 IP 은닉)이 전부 비어 있습니다. 게다가 **오리진 IP 가 DNS 로 그대로 공개**되어 있어 나중에 CF 를 붙여도 직접 타격이 가능합니다(그래서 `@not_cf` 블록이 필요).

**마케팅 맥락에서 왜 중요한가.** 광고 트래픽에는 봇·스크래퍼·경쟁사 정찰이 함께 옵니다. 랜딩/공개 링크페이지(`/api/v1/pages/@slug`)와 방문추적(`track_visit 120/hour`) 경로가 전부 이 오리진으로 직접 들어옵니다. L7 플러딩 한 번에 gunicorn 12×4 워커가 포화되면 **DM 자동화(핵심 기능)까지 같이 죽습니다**.

> 참고로 현재 용량 자체는 여유롭습니다(§5). 문제는 용량이 아니라 **악성 트래픽을 걸러낼 지점이 없다는 것**입니다.

**조치**

```
① Cloudflare 대시보드: api.turnflow.clfy.ai.kr 레코드를 프록시(오렌지 구름) 전환
   - Full(strict) TLS + 이미 보유한 Origin cert 활용 (/root/caddy/certs/ 에 존재)
   - 웹훅 경로 주의: Meta 웹훅이 CF 를 통과해야 함 → 전환 후 IG 웹훅 수신 즉시 검증
     (/api/v1/integrations/instagram/webhook* 은 챌린지·레이트리밋 예외 규칙 필수)
② WAF/보안 설정: Bot Fight Mode, /api/v1/auth/* 와 /admin/* 에 Rate Limiting Rule,
   민감 경로 Managed Challenge
③ 전환·검증 후 Caddyfile 의 @not_cf 403 블록 활성화 (오리진 직접 타격 차단)
   ⚠️ 활성화 전 필수: 전 레코드 프록시 확인 + ufw 에 2222 허용 + 8001~8003 로 스모크
④ 서버 IP 교체는 콜로라 어려우므로, 최소한 monitor 레코드도 정리 (P1-2)
```

---

### P0-5. Django admin 이 무제한 브루트포스에 열려 있음

**실측 증거**

```
/admin/login/ 15연타 → 200 200 200 200 200 200 200 200 200 200 200 200 200 200 200  (차단 없음)
비교: /api/v1/auth/login/ 12연타 → 401×10, 11번째부터 429  ✅ (auth_login 10/min 정상 동작)

DEFAULT_THROTTLE_CLASSES = None       (전역 스로틀 없음 → Django admin 은 DRF 스로틀 미적용)
django-axes 설치 여부 = False
Caddyfile @admin_block IP 허용목록 = 주석 처리
계정 현황: 전체 사용자 115명 · is_staff 5명 · is_superuser 2명
최근 48h /admin 요청 4건 (아직 표적화되지 않음)
```

**평가.** DRF 스로틀은 DRF 뷰에만 걸립니다. Django 기본 admin 로그인은 그 밖이라 **완전히 무제한**입니다. superuser 2명·staff 5명이 있고, staff 토큰 하나의 blast radius 가 크다는 점(6월 감사 M-6: 단일 `is_staff` 등급이 전 테넌트 파괴적 액션 수행)이 이미 지적돼 있습니다. 아직 공격 표적이 아닌 건 운이며, 마케팅으로 도메인 노출이 늘면 스캐너에 걸립니다.

**조치** (택1 이상, ①이 가장 저렴하고 효과적)

```
① Caddyfile @admin_block 활성화 — /admin, /api/v1/admin 을 운영자 IP/VPN 으로 제한
   (동적 IP 면 Cloudflare Access(Zero Trust) 로 SSO 게이트 — P0-4 프록시 전환과 함께)
② django-axes 도입: 실패 5회 → 계정+IP 잠금
③ staff/superuser 계정 전수 점검 + 강한 비밀번호 강제 + 2FA(django-otp) 검토
④ superuser 를 실사용 계정과 분리 (일상 작업은 staff 로)
```

---

## 2. 🟠 P1 — 1~2주 내

### P1-1. `.env.production` 이 world-readable(644) + 백업 사본 12개가 전부 644

```
-rw-r--r-- root:root /opt/turnflow_backend/.env.production          ← 644
그 외 동일 권한 사본 12개:
  .env.production.bak.1782874915 / .bak.1782894898 / .bak-hmac
  .prebak.1782913428 / .prebak.1783352053 / .prebak.resend.1783396821
  .prebak.secrot.1783413678 / .bak-cfemail-20260710
  .prebak.toss-billing.1783956756 / .prebak.rs256.1783959412 / .prebak.cors.1783991362
  (+ /root/moderatube-backend/.env, /root/vllm-server/.env 도 644)
포함 시크릿(값 미열람, 존재·길이만 확인):
  SECRET_KEY(50) DB_PASSWORD(48) META_APP_SECRET(32) INSTAGRAM_APP_SECRET(32)
  TOSS_SECRET_KEY(37) R2_SECRET_ACCESS_KEY(64) LLM_API_KEY(67)
  GEMINI_API_KEY(53) DEEPSEEK_API_KEY(35) APIFY_API_KEY(46) IG_WEBHOOK_VERIFY_TOKEN(26)
```

`clfy` 계정(sudo·docker 그룹), `vastai_kaalia`(P1-3), 그리고 컨테이너 탈출 시 **읽기만으로 전체 시크릿 획득**입니다. JWT 는 RS256 으로 전환돼 SECRET_KEY 가 서명키는 아니지만, `apps/integrations/encryption.py` 가 **Fernet 키를 `sha256(SECRET_KEY)` 로 파생**하므로 SECRET_KEY 유출 = **전 테넌트 IG 토큰 복호화**입니다.

또한 과거 시크릿이 담긴 사본이 11개나 남아 있어, 회전한 키가 **구 파일에 그대로 보존**되어 있습니다(예: `.prebak.secrot` = secret rotation 직전 스냅샷).

```bash
chmod 600 /opt/turnflow_backend/.env.production
chmod 600 /root/moderatube-backend/.env* /root/vllm-server/.env
# 구 사본은 안전한 곳(암호화 볼트)으로 이동 후 서버에서 삭제 — 특히 .prebak.secrot 계열
# SECRET_KEY 는 회전 난도가 높음(IG 토큰 재암호화 필요) → 유출 정황 없으므로 우선순위는 권한 수정
```

또한 6월 감사 **P0-7(`deploy/backups/.env.backup` R2·Telegram 자격증명 회전)** 은 해당 파일을 서버에서 찾지 못했습니다(삭제된 것으로 보임). 다만 **자격증명 회전이 실제로 됐는지는 미확인** → R2 키·Telegram 봇 토큰 회전 여부를 확인하세요.

---

### P1-2. netdata 가 `monitor.clfy.ai.kr` 로 무인증 공개 (docker.sock + 호스트 루트 마운트 컨테이너)

```
DNS: monitor.clfy.ai.kr → 121.126.99.70 (오리진 직결)
Caddyfile:  monitor.clfy.ai.kr { reverse_proxy 172.19.0.1:19999 }   ← 인증 설정 전무
외부 접근:  https (인증서 검증 실패, 000) / https -k → 200 / http → 308 리다이렉트
무인증 노출 확인: /api/v1/info, /api/v1/charts(3,145개), /api/v1/functions, /api/v2/functions

netdata 컨테이너 보안 컨텍스트:
  user=root(uid 0) · SecurityOpt=[apparmor:unconfined, label=disable] · CapDrop=[]
  마운트: /:/host/root  /var/run/docker.sock  /proc  /sys  /etc/passwd  /etc/group
```

**정정(초기 우려 대비 완화).** netdata 의 특권 function(`processes`, `systemd-journal`)은 Netdata Cloud SSO 게이트에 막혀 **412** 를 반환합니다 — **프로세스 cmdline·journal 로그 유출은 없습니다.**

**그래도 남는 문제 2가지**

1. **정찰 정보 유출:** 무인증으로 커널 버전(`6.8.0-134-generic` — 미패치), OS(24.04), 코어 64/RAM 251G, **전체 컨테이너 이름**(`cgroup_turnflow_instagram_db` 등 → DB·Redis·pgbouncer·LLM 구성 전부), 3,145개 차트의 실시간 부하가 읽힙니다. 공격자에게 "무슨 CVE 를 쓸지"와 "언제 때릴지"를 알려줍니다.
2. **노출된 컨테이너의 권한이 과도:** `docker.sock` + `/:/host/root` + apparmor unconfined + root 인 컨테이너를 인터넷에 노출한 구조 자체가 위험합니다. netdata 에 인증우회/RCE 급 CVE 가 하나 나오면 **즉시 호스트 전체 장악**입니다.

인증서 검증이 실패해서(체인 불완전) 브라우저는 경고를 띄우지만, `curl -k` 로 정상 응답하므로 **공격자에겐 아무 장벽이 아닙니다.**

```
① 가장 저렴: Caddyfile monitor 블록에 basic_auth 추가
   docker exec caddy caddy hash-password  → basic_auth { turnflow <bcrypt> }
② 권장: monitor 레코드를 CF 프록시 + Cloudflare Access(SSO) 뒤로
③ 또는 블록 삭제 후 SSH 터널로만 접근 (ssh -L 19999:127.0.0.1:19999)
④ netdata 마운트 축소: docker.sock 제거(컨테이너 차트 포기), /:/host/root → ro
```

---

### P1-3. 유휴 특권 계정 `vastai_kaalia` (NOPASSWD sudo + docker 그룹)

```
/etc/sudoers: vastai_kaalia ALL=(ALL) NOPASSWD:ALL     ← 비밀번호 없이 전권
getent group docker: docker:x:988:root,clfy,vastai_kaalia
/etc/passwd: vastai_kaalia:x:111:...:/var/lib/vastai_kaalia:/bin/bash   ← 로그인 셸 보유
systemctl: vastai.service = loaded failed failed (Vast.ai Host Daemon)  ← 미가동
Vast.ai 관련 컨테이너: 없음
```

Vast.ai GPU 대여 호스트 데몬의 잔재입니다. **현재 서비스는 failed 상태이고 대여 컨테이너도 없어 능동적 위험은 낮습니다.** 문제는 남아 있는 계정이 **비밀번호 없는 완전 root 권한 + docker 그룹(= root 등가)** 을 갖고 있다는 것입니다. 어떤 경로로든 이 계정 컨텍스트를 얻으면 즉시 호스트 장악이고, `docker` 그룹만으로도 `docker run -v /:/host` 로 root 획득이 가능합니다.

또한 이 계정은 P1-1 의 644 `.env.production` 을 그냥 읽을 수 있습니다.

```bash
# 쓰지 않는다면 정리 (Vast.ai 재개 계획을 먼저 확인)
systemctl disable --now vastai.service
gpasswd -d vastai_kaalia docker
rm /etc/sudoers.d/* 해당 라인 (또는 /etc/sudoers 에서 제거 — visudo 로)
usermod -s /usr/sbin/nologin vastai_kaalia
# 완전 제거 시: userdel -r vastai_kaalia  (홈 데이터 확인 후)
```

---

### P1-4. 자동 보안 업데이트가 `masked` + 165개 패키지 대기 + 재부팅 대기

```
/etc/systemd/system/unattended-upgrades.service → /dev/null (masked, 2026-03-03)
/etc/apt/apt.conf.d/20auto-upgrades → Update-Package-Lists "1"; Unattended-Upgrade "1";
   (설정은 켜져 있으나 서비스가 masked 라 무효)
apt-get -s upgrade: 업그레이드 가능 165개
/var/run/reboot-required → 존재
/var/run/reboot-required.pkgs → linux-image-6.8.0-136-generic, linux-base, libc6
실행 커널 6.8.0-134-generic (28일 가동) / 설치된 최신 6.8.0-136
마지막 apt 작업: 2026-08-01
```

커널과 **libc6** 갱신이 대기 중이며 재부팅이 필요합니다. 자동 패치가 의도적으로 masked 되어(운영 안정성 목적으로 보임) 165개가 누적됐습니다. 마케팅 트래픽 유입 후에는 재부팅 창을 잡기 더 어려워지므로, **집행 전에** 처리하는 편이 낫습니다.

```bash
# ① 유지보수 창 확보 후
apt update && apt upgrade    # 또는 최소한 apt install --only-upgrade libc6 linux-image-generic
# ② 재부팅 — turnflow-recover.service(enabled) 가 스택을 재생성하도록 설계돼 있음.
#    단 P0-3 의 fw-hardening 유닛이 실제로 적용되는지 재부팅 후 반드시 확인:
#      iptables -S DOCKER-USER | grep -c DROP   → 2 여야 함
#      systemctl is-active turnflow-fw-hardening
# ③ 자동 패치는 security 포켓만 켜는 절충안 권장
#    unattended-upgrades unmask + Allowed-Origins 를 ${distro_id}:${distro_codename}-security 로 한정
#    Unattended-Upgrade::Automatic-Reboot "false" (재부팅은 수동 통제)
```

---

### P1-5. 의존성 CVE 미해결 — 공개 노출면 확대 직전이라 재우선순위 필요

런타임 컨테이너 실측 (`pip list`):

| 패키지 | 현재 | 문제 | 권장 |
|---|:---:|---|:---:|
| **gunicorn** | 21.2.0 | HTTP Request Smuggling **CVE-2024-1135 / CVE-2024-6827** — 리버스 프록시(Caddy) 뒤 구성에서 직접 관련 | 23.0.0+ |
| **Django** | 5.0.1 | 5.0 계열 EOL, 이후 보안패치 전부 미적용 (CVE-2024-42005 등) | 5.0.14+ 또는 LTS 5.2.x |
| **Pillow** | 10.4.0 | 업로드 이미지 디코딩 경로(실사용 기능) | 11.x + `MAX_IMAGE_PIXELS` |
| **DRF** | 3.14.0 | Django 5.x 공식지원은 3.15+ | 3.15.2+ |
| requests | 2.31.0 | 6월 감사에서 트리거 조건 부재로 오탐 판정(유지) | 낮음 |
| cryptography | 42.0.2 | 상위 버전 권고 | 중 |

**gunicorn 이 이번 맥락에서 가장 중요합니다.** 스머글링은 "프록시 뒤"가 전제 조건이고, 현 구성이 정확히 그렇습니다. 트래픽·스캐너 유입이 늘면 우연한 발견 확률도 올라갑니다.

lockfile 이 없고(`--require-hashes` 미적용) CI 취약점 스캔(`.github` 부재)도 없어, 다음 감사까지 이 상태가 유지될 구조입니다.

```bash
# 최소 조치: gunicorn 만 먼저 (호환 리스크 낮음)
#   requirements.txt: gunicorn==23.0.0  → 이미지 재빌드 → 스모크 → 배포
# 그다음: Django 5.0.14 (마이너 패치, 호환 리스크 낮음) → DRF 3.15.2 → Pillow 11
# 병행: pip-audit 를 배포 파이프라인에 추가
```

---

### P1-6. 전역 스로틀 부재 — 인증 경로만 보호됨

```
DEFAULT_THROTTLE_CLASSES = None
DEFAULT_THROTTLE_RATES (실측, 14개 scope):
  auth_login 10/min · auth_register 10/hour · auth_google 20/min
  email_verify 10/min · email_send 5/hour · password_reset 10/hour
  password_reset_confirm 10/min · external_import 30/hour · insights_sync 5/hour
  link_meta 60/min · track_visit 120/hour · checkout_event 240/hour
  ig_health 20/min · ig_resubscribe 6/hour
```

인증·외부IO 경로는 잘 덮여 있고 **실동작을 확인**했습니다(§5). 문제는 **명시적으로 scope 를 붙이지 않은 모든 경로가 무제한**이라는 점입니다. 6월 감사에서 지적된 다음 항목이 그대로 남습니다:

- **H-8 / M-11:** AI·LLM 엔드포인트(`classify-posts`, `ai-suggest`, `/ai/jobs`) 스로틀·토큰차감 없음 → **유료 LLM 비용 폭탄**. Pro 플랜은 잔액검사 자체를 skip. 광고로 유입된 신규 사용자가 스크립트로 반복 호출하면 외부 API 비용이 직접 증가합니다.
- **M-14:** 공개 제출/기록 4개 엔드포인트 무스로틀 → slug 열거, 대시보드 스팸/PII 오염.
- **M-12:** AI 소스 이미지 업로드 스로틀·쿼터 없음, `DATA_UPLOAD_MAX_MEMORY_SIZE` 미설정.

```python
# 안전망: base.py 에 전역 익명 스로틀 추가
"DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
"DEFAULT_THROTTLE_RATES": {..., "anon": "300/min",
                           "ai_generate": "10/min", "ai_classify": "20/min"}
# + AI 뷰에 ScopedRateThrottle, 유료 플랜에도 월 상한
# ⚠️ 스로틀 카운터가 앱 캐시와 같은 Redis(/1) → cache.clear() 시 한도 리셋 (감사 Info)
#    전용 캐시 alias 로 분리 권장
```

---

## 3. 🟡 P2 — 1개월 내

### P2-1. HSTS 미설정 + 쿠키/리다이렉트 설정

```
실측 응답 헤더 (https://api.turnflow.clfy.ai.kr/api/v1/healthz):
  X-Content-Type-Options: nosniff ✅ · X-Frame-Options: DENY ✅
  Referrer-Policy: strict-origin-when-cross-origin ✅
  Strict-Transport-Security: 없음 ❌
  Content-Security-Policy: 없음 ❌
Django 런타임: SECURE_HSTS_SECONDS=0 · SECURE_SSL_REDIRECT=False
              SESSION_COOKIE_SECURE=True ✅ · SESSION_COOKIE_SAMESITE=None ❌
JWT: RS256 ✅ · ACCESS_TOKEN_LIFETIME = 1 day ❌ (감사 M-2)
```

> **Caddyfile 주석 정정:** "HSTS 는 Caddy TLS 가 자동" 이라고 적혀 있으나 **사실이 아닙니다.** Caddy 는 HSTS 헤더를 기본 전송하지 않으며, 실측에서도 없습니다. 해당 주석을 지우고 헤더를 명시하세요.

조치: `Strict-Transport-Security "max-age=31536000; includeSubDomains"` 활성화(서브도메인 전부 HTTPS 확인 후), `SESSION_COOKIE_SAMESITE="Lax"`, CSP 도입(감사 H-5·M-9 의 XSS 영향 완화), ACCESS 토큰 수명 15분으로 단축.

### P2-2. PostgreSQL `pg_hba.conf` 에 `trust` 인증 라인

```
local   all  all                      trust
host    all  all  127.0.0.1/32        trust
local   replication all              trust
host    all  all  all                 scram-sha-256   ← 컨테이너 간 접속은 정상
```

컨테이너 내부/루프백 한정이라 외부에서 직접 악용은 불가하지만, DB 컨테이너 침해나 `docker exec` 획득 시 **비밀번호 없이 postgres 슈퍼유저**가 됩니다. `scram-sha-256` 으로 통일 권장(pgbackrest·백업 스크립트 영향 검토 필요).

### P2-3. 컨테이너 전부 root 실행, cap_drop/no-new-privileges 없음 (감사 M-20)

```
web_dashboard/webhook/external/celery/caddy/litellm/redis : user='' → uid 0, CapDrop=[], SecurityOpt=[]
gemma-vllm : SecurityOpt=[label=disable]
netdata    : SecurityOpt=[apparmor:unconfined, label=disable]
privileged 컨테이너 : 0건 ✅
```

Dockerfile 에 `USER` 지시어가 없습니다. 컨테이너 탈출 난도를 낮추는 요소이며, P1-2(netdata)와 결합하면 위험이 커집니다. `cap_drop: [ALL]` + `security_opt: [no-new-privileges:true]` 부터 적용 권장(비루트화는 스태틱/미디어 권한 영향 검토 필요).

### P2-4. DB 백업 base 가 34일 정지 (가용성)

```
pgbackrest info (stanza: turnflow, status: ok, cipher: aes-256-cbc)
  full backup : 20260627-072855F                      (2026-06-27)
  diff backup : ..._20260627-074600D                  (2026-06-27)
  diff backup : ..._20260630-132112D                  (2026-06-30)  ← 마지막
  WAL archive : 00000001...005D ~ 00000001000000BC000000B1  ← 계속 적재 중 ✅
호스트 cron/cron.d 에 백업 잡 없음 · ScheduledJob 목록에도 백업 잡 없음
```

WAL 아카이빙은 살아 있어 PITR 자체는 가능하지만, **base 가 34일 전이라 복구 시 34일치 WAL 재생**이 필요합니다(복구 시간 大, WAL 체인 하나만 손상되면 그 지점 이후 복구 불가). 마케팅으로 신규 고객·결제 데이터가 늘어나는 시점에 이 상태는 위험합니다.

```bash
# 즉시: 새 full 백업 1회
docker exec turnflow_instagram_db pgbackrest --stanza=turnflow --type=full backup
# 정례화: cron.d 에 주 1회 full + 일 1회 diff (deploy/backups/pgbackrest_backup.sh 존재)
# 검증: restore.sh 로 복구 리허설 1회 (백업은 복구를 확인해야 백업)
```

### P2-5. SSH 부수 하드닝

`X11Forwarding yes`(불필요), `MaxAuthTries 6`, `LoginGraceTime 120`, `AllowUsers` 미설정, `ClientAliveInterval 0`. P0-1 조치와 함께 처리.

---

## 4. 침해 흔적 점검 결과 — **현재 이상 없음**

2026-07-07 litellm 크립토마이너 침해 이력이 있어 별도 확인했습니다.

| 점검 항목 | 결과 |
|---|---|
| 마이너 프로세스(`xmrig`/`kdevtmpfsi`/`kinsing`/stratum) | **0건** |
| CPU 상위 프로세스 | containerd·dockerd·netdata·vLLM — 전부 정상 |
| 비정상 outbound ESTABLISHED | netdata→44.207.131.212:443(정상 텔레메트리), SSH 세션 2개(운영자 IP `121.133.95.25`) 뿐 |
| `/tmp`·`/var/tmp`·`/dev/shm` 실행 파일 | **0건** |
| root crontab | **비어 있음** |
| `/etc/cron.d` | `e2scrub_all`·`sysstat`·`.placeholder` — 기본값만 |
| 최근 30일 신규 systemd 유닛 | `turnflow-recover`·`turnflow-fw-hardening` — 둘 다 정상 자산 |
| privileged 컨테이너 | **0건** |
| UID 0 계정 | `root` 만 |
| 성공 SSH 로그인(30일) | 전부 `121.133.95.25`(운영자) — 무단 접속 흔적 없음 |
| 외부 개방 포트 | 80·443·2222 만 |

**단, 정찰 시도는 활발합니다.** 7일간 55,536건의 SSH 인증 실패가 있었고, 시도 계정 분포(root·admin·postgres·oracle·ftpuser)는 자동화 봇넷의 전형입니다. 지금까지 뚫리지 않은 것은 비밀번호가 사전에 없었기 때문이며, **방어 메커니즘이 작동한 결과는 아닙니다**(P0-1).

---

## 5. ✅ 양호 — 실측으로 확인된 것

6월 감사의 P0 코드 조치가 **prod 실값에서 실제로 유효함**을 확인했습니다. 이 부분은 안심해도 됩니다.

| 항목 | 실측 결과 |
|---|---|
| **웹훅 HMAC enforce** (감사 최대 이슈 C-1) | `WEBHOOK_HMAC_ENFORCED = True` — 런타임 확인. **C-1/H-3/H-6/H-7/H-10 실차단** |
| `DEBUG` | `False` |
| `SECRET_KEY` | 50자 (prod.py 부팅 가드 통과). ※ 가드 경계값이라 여유 없음 |
| Swagger/ReDoc/schema | `/api/docs/`·`/api/schema/`·`/api/redoc/` 모두 **404** (prod 비노출) |
| 인증 스로틀 | `/api/v1/auth/login/` 10회 후 **429 실측** — 감사 H-1 조치가 실동작 |
| JWT | **RS256** (대칭키 HS256 위험 해소) |
| 외부 개방 포트 | **80·443·2222 만** — 8088·4040·19999·6379·5432·6432 전부 외부 차단 확인 |
| LiteLLM 무인증 접근 | `/v1/models`·`/v1/chat/completions` → **401**, master key 71자 |
| `ALLOWED_HOSTS`/CORS/CSRF | 화이트리스트 적절, `CORS_ALLOW_ALL_ORIGINS=False` |
| `INSTAGRAM_MOCK_MODE` / `TOSS_DEV_CARD_AUTH_ENABLED` | 둘 다 `False` (운영 안전값) |
| 시크릿 플레이스홀더 잔존 | **0건** (`CHANGE_ME`·`django-insecure`·`my_verify_token_12345` 없음) |
| Caddy 하드닝 | 본문 크기 per-route 캡, 비정상 메소드 405, `-Server`, nosniff, `X-Frame-Options: DENY` |
| Origin key 권한 | `/root/caddy/certs/origin.key` = **600** ✅ |
| 침해 흔적 | §4 — 없음 |

### 용량 — 마케팅 트래픽에 문제 없음

```
CPU 64코어 · RAM 251G(사용 17G, 여유 234G) · load avg 0.22
디스크 / 36% (540G 여유) · /var/lib/docker 12% (599G 여유)
gunicorn: dashboard 12 workers × 4 threads (t/o 30) · webhook 4×4 (t/o 10) · external 4×16
pgbouncer: pool_mode=transaction, max_client_conn=2000, default_pool_size=40, reserve=10
PostgreSQL: max_connections=300, 현재 활성 17
Redis: 사용 10MB / maxmemory 8G, connected_clients 159
최근 1시간: 총 요청 915건, 500 응답 1건
```

**용량은 충분히 여유롭습니다.** 병목 위험은 자원이 아니라 **악성 트래픽을 걸러낼 계층이 없다는 것**(P0-4)과 **AI 경로 비용 무제한**(P1-6)입니다.
한 가지 주의: Redis `maxmemory-policy = noeviction` 이며 브로커와 캐시(`/0`, `/1`)를 공유합니다. 캐시가 8G 를 채우면 **eviction 대신 쓰기 에러**가 나고 브로커까지 영향받습니다. 트래픽 급증 시 Redis 메모리 모니터링을 권장합니다.

---

## 6. 권장 실행 순서

### 즉시 (오늘~내일, 마케팅 집행 전)

| 순서 | 작업 | 소요 | 위험 | 다운타임 |
|:---:|---|:---:|:---:|:---:|
| 1 | **SSH 키 등록 → 비번인증 차단 → root/clfy 비번 교체 → fail2ban** (P0-1) | 30분 | 중 (순서 엄수·락아웃 주의) | 없음 |
| 2 | **Cloudflare 프록시 전환 + WAF/레이트리밋** (P0-4) | 30분 | 중 (웹훅 수신 검증 필수) | 없음 |
| 3 | **`/admin` IP 허용목록 또는 CF Access** (P0-5) | 15분 | 하 | 없음 |
| 4 | **`.env*` 권한 600 + 구 사본 정리** (P1-1) | 10분 | 하 | 없음 |
| 5 | **netdata basic_auth 또는 monitor 블록 제거** (P1-2) | 10분 | 하 | 없음 |
| 6 | **`vastai_kaalia` sudo/docker 권한 박탈** (P1-3) | 5분 | 하 | 없음 |
| 7 | **pgbackrest full 백업 1회** (P2-4) | 10분 | 하 | 없음 |

### 이번 주 (유지보수 창 필요)

| 순서 | 작업 | 다운타임 |
|:---:|---|:---:|
| 8 | **Redis `requirepass` + 브로커/캐시 URL 갱신** (P0-2) — DM 한가한 시간대 | 스택 재시작 (~2분) |
| 9 | **컨테이너 포트를 127.0.0.1 바인딩 + ufw default deny** (P0-3) | gemma-vllm 재시작 |
| 10 | **libc6·커널 업그레이드 + 재부팅**, unattended-upgrades security 한정 복구 (P1-4) | 재부팅 (~5분) |
| 11 | **gunicorn 23.0.0 → Django 5.0.14** (P1-5) | 롤링 배포 |

### 2주~1개월

12. LLM 컨테이너 네트워크 분리 (P0-2 ②)
13. 전역 `AnonRateThrottle` + AI 경로 스로틀·유료 상한 (P1-6)
14. HSTS·SameSite·CSP·ACCESS 토큰 수명 (P2-1)
15. `pg_hba` trust 제거 (P2-2), `cap_drop`/`no-new-privileges` (P2-3)
16. 백업 정례화 + 복구 리허설 (P2-4), pip-audit CI (P1-5)

---

## 7. 점검 방법·한계

**방법.** SSH 실접속으로 read-only 명령만 실행(설정 변경 0건). ① 호스트/커널/패치 ② sshd 유효설정·인증 로그 55,536건 집계 ③ 방화벽(ufw/iptables/nft) 전 규칙 ④ LISTEN 소켓 전수 ⑤ 컨테이너 17개의 마운트·권한·유저·네트워크 ⑥ `.env.production` 실값(**값은 열람하지 않고 존재·길이·문자군만**) ⑦ Django 런타임 설정을 컨테이너 내부에서 직접 로드 ⑧ 침해 흔적(프로세스·outbound·cron·systemd·임시디렉터리) ⑨ 외부 관점: 38포트 스캔 + 7개 호스트 DNS/TLS/헤더 + 스로틀 실측 프로브.

**검증 태도.** 자체 측정 오류를 2건 잡아 정정했습니다 — (a) `SECRET_KEY len=8` 은 `awk -F=` 가 base64 패딩의 `=` 에서 잘린 아티팩트였고, 정확 재측정 + Django 런타임 확인으로 **50자**임을 확정했습니다. (b) netdata 특권 function 이 노출됐다고 의심했으나, 실제 호출 결과 **412(Netdata Cloud SSO 필요)** 로 막혀 있어 심각도를 하향했습니다.

**한계.**
- 시크릿 **값**을 열람하지 않았으므로 개별 키의 **엔트로피 품질**은 판정 불가(길이·문자군만). `DB_PASSWORD`(48자·문자군 2)와 `SECRET_KEY`(50자·문자군 3)는 생성 방식 확인을 권장합니다.
- 6월 감사의 **코드 취약점 100건은 재검증 대상이 아닙니다.** 이번엔 prod 실값으로 확인 가능한 항목만 상태를 갱신했습니다. 미해결 코드 결함(M-5 이메일 템플릿 피싱, M-9 저장형 XSS, M-1 Google `email_verified` 미검증, H-8 AI 비용 등)은 원본 보고서를 계속 참조하세요.
- 침해 흔적 점검은 호스트 계층 지표 기반입니다. 커널 루트킷 수준 은닉은 이 방법으로 배제할 수 없습니다(현 정황상 가능성 낮음).
- Cloudflare 대시보드 설정(WAF 규칙, Access 정책)은 서버에서 확인 불가 — 별도 점검 필요.
- **DB 복구 리허설 미실시** — pgbackrest 메타데이터만 확인했습니다. 실제 복구 가능성은 리허설로만 증명됩니다.

---

*본 보고서는 자체 소유 인프라에 대한 방어적 보안 점검 결과입니다. P0 5건 중 1·2번은 이미 능동적 공격에 노출된 상태이므로, 마케팅 집행 전 처리를 강력히 권장합니다.*
