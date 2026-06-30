# GCP cold-VM DR — 운영 런북 (Phase B)

콜로 장애 시 **GCP에 cold VM을 띄워 R2 백업으로 복구**하고, 사람 승인 후 Cloudflare DNS를 GCP로 전환한다.
평상시 비용 ≈ **$0** (VM은 장애/드릴 때만 생성). 감지·경보는 Phase A(CF Worker)가 이미 담당.

```
[Phase A 감지] colo CONFIRMED_DOWN Telegram
     ↓ (사람)
provision.sh live ──▶ VM 부팅 ──▶ startup.sh:
     Secret Manager 시크릿 → 핀 clone → build → R2 PITR 복구 → migrate → 스택 기동
     → dr_catchup → mark_restore_complete --promote → (새 타임라인 full backup) → Caddy(Origin cert)
     → ready=200 에서 멈춤 + Telegram
     ↓ (사람 승인)
cf_origin.sh <GCP_IP> ──▶ api.turnflow → GCP (트래픽 전환) ──▶ 콜로 네트워크 펜스
```

## 파일
| 파일 | 역할 | 실행 위치 |
|---|---|---|
| `gcp.env(.example)` | 중앙 설정(프로젝트/리전/VM/CF/시크릿이름). 비밀값 X | — |
| `setup_gcp.sh` | 일회성: API 활성화 + SA + 방화벽(443=CF만, 22=운영자만) | 운영자 laptop |
| `setup_secrets.sh` | 일회성: 로컬 시크릿 파일 → Secret Manager + SA 바인딩 | 운영자 laptop |
| `provision.sh [live\|drill] [PITR]` | 정적IP 예약 + VM 생성(startup 주입) | 운영자 laptop |
| `startup.sh` | 온부팅: 복구 전 과정 → ready → 멈춤(사람 스왑) | VM(자동) |
| `restore_from_r2.sh` | 컨테이너 기반 PITR 복구(드릴=archive off) | VM(startup이 호출) |
| `Caddyfile.gcp` | colo api 블록 + CF Origin cert `tls` | VM |
| `cf_origin.sh <IP>` | CF DNS A레코드 스왑(proxied). 전환/복귀 양방향 | 운영자 laptop |
| `drill.sh [PITR]` | 안전 드릴(gcp-drill, DNS 무변경) + 검증/teardown 안내 | 운영자 laptop |
| `teardown.sh` | VM 삭제 + IP 해제 → $0 | 운영자 laptop |
| `failback_from_gcp.sh` | gcp→colo 복귀(수동 승인 골격) | 운영자 laptop |
| `dr-overlay.env.example` | (선택) <32GB 박스 자원 축소 | — |

---

## 사전 준비물 (오늘 드릴 전에)
1. **GCP 프로젝트 + 결제 활성화**, 로컬에 `gcloud` 설치 + `gcloud auth login`.
   - 운영자 계정 권한: 프로젝트 `roles/compute.admin`(또는 owner) + 생성할 SA에 `roles/iam.serviceAccountUser`.
2. **Cloudflare Origin Certificate** 발급(대시보드 → SSL/TLS → Origin Server → Create): `api.turnflow.clfy.ai.kr`(또는 `*.turnflow.clfy.ai.kr`) → `origin.pem` + `origin.key` 저장. **SSL 모드 Full(strict) 확인.**
3. **CF API 토큰**: My Profile → API Tokens → Zone.DNS:Edit, 존 `clfy.ai.kr` 한정 → `cf-api-token.txt`(한 줄).
4. **시크릿 파일 모으기** `deploy/dr/gcp/secrets/` 에:
   - `.env.production` ← **콜로 것 복사 후 `SITE_ID=gcp`** (TELEGRAM_*, SCHEDULER_TICK_SECRET, DB비번, META/IG, R2 등 동일)
   - `pgbackrest.conf` ← 콜로 것 그대로(R2 키 + cipher, `pg1-path=/var/lib/postgresql/data`)
   - `origin.pem` / `origin.key` / `cf-api-token.txt`
5. `cp gcp.env.example gcp.env` 후 채우기: `GCP_PROJECT`, `OPERATOR_CIDR`(내 공인 IP/32), `GIT_REF`(콜로 `git rev-parse HEAD`), `COLO_IP`.

## 일회성 셋업
```bash
cd deploy/dr/gcp
cp gcp.env.example gcp.env && $EDITOR gcp.env      # 값 채우기
bash setup_gcp.sh                                  # API + SA + 방화벽
bash setup_secrets.sh                              # secrets/ → Secret Manager (적재 후 secrets/ 삭제 권장)
```

## 오늘: 드릴 (실 트래픽 무영향)
```bash
bash drill.sh                       # = provision.sh drill (SITE_ID=gcp-drill, archive off, DNS 무변경)
# 안내되는 ssh 로그/검증 명령 따라가며: ready=200 + active_site=gcp-drill 확인, RTO/RPO 기록
bash teardown.sh                    # 끝나면 정리 → $0
```
드릴이 검증하는 것: R2 PITR 복구 → migrate → dr_catchup → promote → Caddy/Origin cert → ready=200.
**검증 못 하는 것(의도적)**: 실 CF DNS 스왑, 콜로 펜스 — 이건 live failover 에서만(저트래픽 창 1회 리허설 권장).

## 실제 장애 시 (live failover)
1. Phase A 가 `🔴 CONFIRMED_DOWN` Telegram → 콜로 진짜 다운 확인.
2. `bash provision.sh live ["YYYY-MM-DD HH:MM:SS+09"]` (시각 생략=최신). ssh 로그로 진행 관찰.
3. `🟢 ready=200, IP=...` Telegram 오면 → `bash cf_origin.sh <GCP_IP>` (yes 승인) → 트래픽 전환.
4. **콜로 펜스**(split-brain 1순위): 콜로 되살아나도 트래픽 받으면 안 됨 → 콜로 스택 정지 + 443 차단. CF DNS 는 이미 GCP 향함(엣지 차단). tick/웹훅도 GCP 로 자동 추종.
5. 안정화 후 failback 은 `failback_from_gcp.sh`(수동 승인, 콜로를 새 타임라인으로 재시드).

## 비용
- 평상시: **$0** (VM/IP 없음, 시크릿/방화벽/SA만 — 사실상 무료).
- 드릴/장애 중: e2-standard-8 ~ 시간당 $0.27 + pd-ssd 300GB(시간당 푼돈) + R2 egress 무료. teardown 시 즉시 0.
- live 운영 중엔 teardown 금지(IP 해제하면 DNS 타겟 사라짐). 드릴은 teardown 으로 $0 복귀.

## 안전 규칙 (요약)
- **드릴 stanza 안전**: `SITE_ID=gcp-drill` + restore `--archive-mode=off` + 복구 후 fail-closed 가드(archive_mode≠off 면 중단) → 공유 R2 `turnflow` stanza 절대 오염 안 함.
- **드릴 DM 안전**: 드릴은 celery_dm/celery_followup **미기동** + `dr_catchup --dry-run` → dm_send/verify 큐 소비자 0, send 태스크 적재 0 → **실유저 DM/공개답글 발송 0**(구조적 차단).
- **false-failover 주의**: 콜로가 실제 살아있는데 전환하면 콜로·GCP 가 같은 R2 stanza 에 분기 타임라인을 동시 push → 백업 오염. 그래서 `cf_origin.sh` 는 GCP 전환 시 '콜로 펜스 완료?'를 **강제 확인**하고 스왑 후 콜로 443 을 능동 프로브한다. 진짜 장애(콜로 다운)면 경합 없음. live promote 는 새 타임라인 생성 → startup 이 full backup+check 로 정착(알림에 `archive=OK` 표기, 실패 시 🟠).
- 자동 failback 금지. 콜로 복귀는 항상 수동 + R2(새 타임라인)에서 재시드 후(`failback_from_gcp.sh`).
- 시크릿(`secrets/`·`gcp.env`·`certs/`)은 `deploy/dr/gcp/.gitignore`(deny-all+allowlist)로 커밋 차단. setup_secrets.sh 후 `secrets/` 삭제 권장. 암호화 cipher/CF Origin key 분실 = 복구 불가 → Secret Manager + 오프사이트 이중 보관.
- 상세 설계: `DR_IMPLEMENTATION_PLAN.md`, 계획서 `purrfect-knitting-ember.md`. 콜로 배포 함정: 메모리 `colo-prod-deploy-gotchas`.
