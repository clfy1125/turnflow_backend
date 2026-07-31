# DM 오류 처리방침 — 통일 · 패치 계획 (v2 · 2분류 체계)

작성 2026-07-30 · **개정 2026-07-31 (5분류 → 2분류)** · 기준 브랜치 `feat/toss-billing`
분류 원본 `DM_ERROR_POLICY_MATRIX.html`

---

## 0. 이번 개정의 핵심

기존 5분류(확인해야함 / 사용자 / 관리자 / 수신자 / 정상)는 **사람이 화면에서 판단하기엔 너무 많습니다.**
질문을 하나로 줄입니다.

> **"사람이 봐야 하는가, 아니면 정해진 대로 자동 처리되는가?"**

| | 뜻 | 화면 |
|---|---|---|
| 🔴 **확인해야함** | 원인이 확정되지 않았거나, 우리 쪽 조치·판단이 필요함. **사람이 열어봐야 함** | 항상 펼침 |
| ⚪ **정상 (자동 처리)** | 원인이 확정돼 있고 대응이 이미 정해져 있음. 시스템이 알아서 처리하거나 안내함 | 접힘 |

- 기존 **관리자 조치**(이미 답글 · 도착 미확인) → 결국 사람이 확인하는 것이므로 **🔴로 편입**
- 기존 **사용자 조치**(재연동 · 결제) / **수신자 사정** → 대응이 정해져 있으므로 **⚪로 편입**

대신 ⚪ 안에서 **"무엇이 자동으로 나가는가"**를 두 번째 축(`auto_action`)으로 따로 답니다.
이건 분류가 아니라 **정상 처리의 속성**입니다.

> **구현 여부는 화면에 표시하지 않습니다.** 미구현 배지를 UI 에 넣으면 안내를 구현한 뒤 UI 를 또 고쳐야 하므로,
> 진행 상황은 **이 문서 §3 체크리스트에서만** 관리합니다.

---

## 1. 데이터 모델 — 축 2개

서버 사전(`apps/admin_api/dm_error_catalog.py`) 각 항목에 두 필드를 답니다.

```python
{
  "title": "...", "cause": "...", "action": "...",
  "policy": "investigate" | "normal",          # 🔴 / ⚪ — 화면 색·필터의 유일한 기준
  "auto_action": "none" | "reconnect_notice" | "upgrade_notice"
               | "recovery_flow" | "peak_notice" | "expiry_notice",
}
```

| 필드 | API 노출 | 쓰는 곳 |
|---|---|---|
| `policy` | **노출** (어드민 응답에 추가) | 어드민 화면의 색·필터·정렬 |
| `auto_action` | 당장은 **서버 내부에만** | 유저 콘솔 안내를 구현할 때(F1~F5) 어떤 안내를 띄울지의 단일 소스. 쓰이지 않는 필드를 미리 API 에 내보내지 않습니다 |

규칙: `policy=investigate` 이면 `auto_action` 은 항상 `none` 입니다.
**자동 처리가 가능하면 그건 정상이지 조사 대상이 아닙니다.**

---

## 2. 51개 재매핑 → 🔴 20 / ⚪ 32

(Phase 2 에서 `failed_window` 내부 가드가 2줄로 쪼개져 총 52항목)

### 🔴 확인해야함 (20)

| # | 항목 | 왜 사람이 봐야 하나 |
|---|---|---|
| 1 | `window_stalled` *(Phase 2 신규)* | 발송 방치 = 우리 서버 문제 확정 |
| 2 | `100/2534022` | 트리거~발송 갭 원인 불명 (웹훅 지연이면 우리 문제) |
| 3 | `10/2534022` | 〃 |
| 4 | `10/2018278` | 〃 |
| 5 | `10 + failed_window` (세부번호 없음) | 〃 |
| 6 | `100/2534023` 이미 답글 | 중복 발송인지 타사툴 충돌인지 사람이 확인 |
| 7 | `failed_no_trace` 도착 미확인 | 재검증 판단·고객 설정 안내 판단 |
| 8 | `200/2534066` 게시물 차단 | 사내 대응 정책 논의 중 |
| 9 | `code 10` (세부번호 없음) | 권한/윈도우 두 뜻이 섞임 |
| 10 | `code 100` (세부번호 없음) | 7일 초과인지 다른 사유인지 미확정 |
| 11 | `code 200` (세부번호 없음) | 권한 계열이나 정확한 사유 불명 |
| 12 | `failed_param` (코드 없음) | 〃 |
| 13 | `-1` | Meta 가 사유를 안 줌 |
| 14 | 기타 4xx (미분류) | 우리가 모르는 코드 |
| 15 | code·http 둘 다 없음 | 단서 0 · 무한 재시도 위험 |
| 16 | 사전 미등록 조합 | 화면에 설명이 빈칸으로 나감 |
| 17 | `failed` (legacy) | 신규 발생 시 코드 결함 |
| 18 | `failed_api` (legacy) | 〃 |
| 19 | 건너뜀 `other` | 사전에 없는 문구 |
| 20 | `2534001` | 우리 시스템에 등록조차 안 됨 (실측 확인 필요) |

### ⚪ 정상 — 자동 처리 (32)

| 항목 | `auto_action` |
|---|---|
| `190` · `102` · `failed_token`(pre-send) | `reconnect_notice` |
| 월 DM 한도 소진 | `upgrade_notice` |
| `100/2534025` 숨김함 유입 · `recovery_pending` | `recovery_flow` |
| `window_peak` *(Phase 2 신규)* | `peak_notice` |
| 창 만료(큐 대기) · 댓글 7일 초과 확정분 | `expiry_notice` |
| `2534014` 수신자 없음 · `551` 도달 불가 | `expiry_notice` (사유 표시만) |
| 건너뜀 7종 (캠페인 꺼짐 · 시간대 밖 · 계정 비활성 · 자기 댓글 · 연결 해제 · 중복 캠페인 정리 · 유령 오프닝 정리) | `none` |
| `recovery_expired` 복구 만료 | `none` |
| 레이트리밋 계열 (`613` · `4` · `1/2/17/32/368` · 5xx · 200-무-message_id) | `none` (자동 재시도) |
| `no_trace` 센티넬 (미사용 표시) | `none` |
| 정상 상태 9종 (queued~recovery_delivered) | `none` (애초에 오류 아님) |

---

## 3. 자동 조치 구현 현황 (이 문서에서만 관리하는 체크리스트)

> ⚪로 분류한다는 건 **"자동으로 뭔가 나간다"는 약속**입니다. 지금 그 약속이 지켜지지 않는 곳 —
> **화면에는 표시하지 않고**, 구현이 끝나면 이 표의 상태만 갱신합니다.

| `auto_action` | 무엇이 나가야 하나 | 현재 | 상태 |
|---|---|---|---|
| `recovery_flow` | 안내 답글 → 재댓글 → 자동 재발송 | 구현됨 (프로 전용) | ✅ |
| `reconnect_notice` | 재연동 안내 | 로그 상세에 CTA 만 있음. **대시보드 상단 배너 없음** | ⚠️ 부분 |
| `expiry_notice` | 창/댓글 만료 안내 | 문구는 있으나 **"24시간"으로 단일화돼 부정확** (댓글 경로는 7일) | ⚠️ 문구 오류 |
| `upgrade_notice` | 한도 소진 → 결제 유도 | **고객에겐 아무것도 안 감.** 텔레그램 운영자 알림만 (`dm_limits.py:126`) | ❌ 미구현 |
| `peak_notice` | "요청이 몰려 제때 못 보냈다" 안내 | 없음 | ❌ 미구현 |
| `none` | — | — | — |

`2534014`(수신자 없음)는 지금 **"댓글 7일 초과" 문구로 뭉뚱그려져** 잘못 안내됩니다 (`dm_frontend_actions.py:136`).

---

## 4. 어드민 "발송 안 됨" 화면

```
발송 안 됨  1,234건
├ 🔴 확인해야함   42건   ← 항상 펼침 · 카드 8
└ ⚪ 정상 처리  1,192건   ← 기본 접힘 · 펼치면 카드 8
```

운영자가 평소 보는 것은 **🔴 8장뿐**입니다.

### 🔴 카드 8

| 카드 | 흡수 항목 | 버튼 |
|---|---|---|
| 발송 방치 (우리 문제) | `window_stalled` | 큐/워커 점검 |
| 창 만료 · 원인 확인 | `100/2534022` · `10/2534022` · `10/2018278` · `10+window` | 웹훅 지연 확인 |
| 이미 답글 있음 | `100/2534023` | 중복 캠페인·타사툴 점검 |
| 도착 미확인 | `failed_no_trace` | **재검증** |
| 게시물 자동 DM 차단 | `200/2534066` | 게시물 교체 안내 |
| 파라미터 오류 · 원인 미확정 | `100`·`failed_param` 중 7일 초과 아닌 것 | 원문 확인 |
| 원인 불명 | `-1` · `10`/`200`(세부없음) · 미분류 4xx · 단서0 · 사전 미등록 · legacy 2 | 원문 확인 |
| 분류 안 된 건너뜀 | `other` | 원문 확인 |

### ⚪ 카드 8 (접힘)

자동 안내 4 — 재연동 필요 / 월 한도 소진 / 숨김함 유입·복구 / 몰려서 지연
조치 없음 4 — 댓글 7일 초과 / 수신자 사정 / 설정·정리로 건너뜀 / 복구 만료

카드에는 **건수와 사유 이름만** 둡니다. 구현 여부 배지는 넣지 않습니다 —
안내를 구현하면 배지를 다시 걷어내야 하므로, 진행 상황은 §3 체크리스트로만 관리합니다.

---

## 5. 창 만료(24시간/7일) 판정 — 확정 기준

> **서버가 고장 나서 창이 끝난 것 = 🔴. 순간 피크로 밀려서 끝난 것 = ⚪ (유저 안내만).**

### 5-1. 창 길이는 경로마다 다릅니다

```python
# apps/integrations/models.py:1647
return timedelta(days=7) if self.comment_id else timedelta(hours=24)
```
댓글 트리거 오프닝 = **7일**, user_id 경로(리워드·스토리답장·복구) = **24시간**.
→ 지금 화면의 "24h 창 만료" 라벨은 오프닝 DM 에 대해 **틀린 문구**입니다.

### 5-2. 피크 vs 방치는 기계적으로 갈립니다

| 상황 | `retry_count` | `next_retry_at` | 판정 |
|---|---|---|---|
| 페이서 슬롯 · 시간당 백스톱 · Action Block 쿨다운 | 안 오름 | **계속 미래로 갱신** (`tasks.py:1591`) | 피크 ⚪ |
| Meta 일시 오류 재시도 | +1 (상한 24) | 갱신 (`tasks.py:345`) | 피크 ⚪ |
| 워커 다운 · 태스크 유실 · requeue 정지 | 멈춤 | **과거에 멈춘 채 방치** | 방치 🔴 |

판정식: **예약된 재시도 시각이 임계(2h, `DM_BACKLOG_OLDEST_ALERT_HOURS`)를 넘겼는데 아직 큐에 있다 = 방치.**

### 5-3. Meta 가 준 2534022 는 "우리 방치"가 아닙니다 — 그래도 🔴

내부 가드는 `send_dm_task` **진입부**에 있어(`tasks.py:1554`) 창 지난 건은 Meta 를 부르기 전에 종결됩니다.
→ Meta 2534022 를 받았다 = **우리 기준으론 창 안**이었다는 뜻.

원인은 **기준점 차이**입니다. 우리는 `log.created_at`(웹훅 수신)부터, Meta 는 **수신자의 마지막 상호작용**부터 잽니다. 갭이 생기는 경우: ① 웹훅 지연 도착(구독 auto-disable 이력 있음 → **우리 문제일 수 있음**) ② 보정 폴러가 오래된 상호작용 수거 ③ 수동 재발송.
①인지 아닌지 코드로 확정 불가 → **확인해야함(🔴) 유지**.

---

## 6. 어드민 사이트가 백엔드와 다른 지점 (조사 결과 · 변경 없음)

### 버그

| # | 위치 | 내용 | 증상 |
|---|---|---|---|
| B1 | `logs/page.tsx:71` | `failed_no_trace → "hidden_spam"` 매핑. 백엔드는 `attention` (`dm_status_groups.py:74`) | 대시보드 "도착 미확인" 링크를 눌러도 **목록이 빔** |
| B2 | `logs/page.tsx:81` | 딥링크가 오류 코드 2차 필터를 자동 프리셋 | code 가 비었거나 다른 값인 행이 잘려 **대시보드 숫자 ≠ 목록 개수** |
| B3 | `lib/status.ts:113` | 코드 맵에 **subcode** `2534025` 가 섞임. 반대로 `102/200/551/-1` 은 없음 | 죽은 항목 + 폴백 라벨 |

### 문구 드리프트

| # | 항목 | 백엔드 | 어드민 |
|---|---|---|---|
| D1 | 상태 그룹 라벨 | 대기중/전송됨/읽음/숨겨진 요청·스팸/**확인 필요** | 대기/발송됨/읽음/**스팸함 유입**/**오류** |
| D2 | `failed_window` | "24h 윈도우 만료" | "24h 창 만료" — **둘 다 부정확** (§5-1) |
| D3 | `mocks/dmErrorCatalog.ts` | 서버 전수 | `100/2534023`·`551`·`4`·`no_trace`·legacy 2 **누락**, `613` recoverable 오기 |

### 잘 되어 있는 것 (건드리지 말 것)

오류 상세 팝업(`ErrorBlock`)과 운영 대시보드는 **이미 서버 사전이 1순위**이고 로컬은 폴백뿐입니다.
`failure_breakdown` 의 group 분리, KPI 합계 대조 경고, `skipped_breakdown.actionable` 도 서버 판정을 그대로 씁니다.

---

## 7. 패치 계획

### 지켜야 할 3원칙 (prod DM 캠페인 가동 중)

1. **`SentDMLog.status` 값 추가·변경 금지** — 집계·큐·페이서가 전부 여기 묶여 있음 → 이번 작업은 전부 "설명·분류" 레이어. **마이그레이션 0건.**
2. **캐시는 선별 삭제** — `admin:dash:ops:*` (+`admin:dash:mkt:*`)만. **전체 flush 금지** (rate_governor 센티넬 소실 = DM 1시간 정지).
3. **집계 정의(실패율·KPI) 변경 금지** — 숫자는 그대로, 라벨만 붙임.

### 작업 분담 — 백엔드만 우리가, 어드민 화면은 제안서

| 범위 | 담당 | 산출물 |
|---|---|---|
| Phase 0·1·2·3·5 | **우리(백엔드)** | 다음 패치에 포함 |
| Phase 4 (어드민 화면) | **어드민팀** | 이 문서 §6 + §4 를 제안서로 전달 |

### 백엔드만 배포했을 때 어드민 화면은 어떻게 되나 (검증 완료)

어드민이 배포를 안 해도 **깨지지 않고, 오히려 문구가 좋아집니다.**

| 변경 | 어드민 화면 영향 | 근거 |
|---|---|---|
| `policy` 필드 추가 | **아무 변화 없음.** zod 가 모르는 키를 조용히 버림 (`.strict()` 사용처 0건) | 무배포 안전 |
| 사전 문구 보강 (Phase 3) | 오류 팝업 문구가 **더 정확해짐** — 서버 `error_title/cause/action` 을 이미 1순위로 쓰고 있음 | `ErrorBlock`(`logs/page.tsx:560`) |
| 창 만료 2분할 (Phase 2) | 운영 대시보드 오류 분포에서 `failed_window` 1행 → **2행**(`window_peak` / `window_stalled`)으로 갈림. 라벨은 서버 사전 문구가 그대로 뜸 | `failure_breakdown` 은 (code, subcode, status) 그룹 |
| 〃 | 로그 상세 팝업의 subcode 칩에 **숫자가 아닌 값**(`window_peak`)이 표시됨 | 표시상 어색할 뿐 기능 영향 없음 — **제안서에 명시** |

**남는 문제**: §6 의 B1(도착 미확인 딥링크가 빈 목록)·B2(코드 프리셋으로 행 잘림)는 어드민팀이 고쳐야 사라집니다. 백엔드로는 우회할 수 없습니다.

### prod 안전성 — Phase 2 영향 범위 전수 확인

`error_subcode` 를 읽는 모든 코드를 확인했고, 새 값(`window_peak` / `window_stalled`)이 닿는 곳은 없습니다.

| 소비처 | 판정 | 영향 |
|---|---|---|
| `status_group()` / `status_group_q()` | `failed_param + 2534025` 만 분기. `failed_window` 는 subcode 무관하게 attention | 없음 |
| `build_frontend_action()` | `failed_window` 분기는 subcode 를 안 봄 | 없음 |
| 유저 콘솔 시리얼라이저(`serializers.py:1350`) | `failed_param + 2534025` 만 분기 | 없음 |
| `backfill_no_trace_delivered` | `failed_no_trace` 대상 | 없음 |
| DB 컬럼 | `CharField(max_length=50)` — `window_stalled` 14자 | 없음 |
| 숫자 변환 | **`int(error_subcode)` 사용처 0건** (전수 grep) | 없음 |
| 사전 미등록 시 | `_BY_STATUS["failed_window"]` 로 폴백 = 기존 문구 | fail-safe |

또한 어드민 테스트에 **응답 키 집합을 단언하는 코드가 없어**(전수 grep) 필드 추가로 깨지는 테스트도 없습니다.

### 배포 체크리스트

- [ ] 마이그레이션 **0건** (모델 변경 없음 — 값만 채움)
- [ ] `SentDMLog.status` 값 불변 → 큐·페이서·집계·통계 전부 무영향
- [ ] Phase 2 는 **신규 종결 건에만** 적용 — 진행 중인 캠페인·대기 큐를 건드리지 않음
- [ ] 배포 후 `admin:dash:ops:*` **선별 삭제** (`admin:dash:mkt:*` 도 응답이 바뀌면 함께). **전체 flush 금지**
- [ ] 롤백 = 코드 revert 만으로 즉시 복구 (남는 subcode 문자열은 무해)

### Phase 0 — 실측 (배포 없음, 선행)

```bash
docker compose exec web python manage.py dump_dm_error_census --format csv > census.csv
```
확인: ① `2534001` 실존 여부 ② `failed_window` 중 내부 가드분 비율 ③ **code 100 원문에서 "7일 초과"가 어떤 문자열로 오는지**(🔴 "파라미터 미확정" 카드를 ⚪ "댓글 7일 초과"로 가르는 규칙의 근거) ④ `catalog=MISSING` 목록.

### Phase 1 — 백엔드: `policy` · `auto_action` 필드 추가 ✅ **구현 완료 (2026-07-31)**

- 사전에 3필드 추가 + `describe()` / `describe_for_log()` / `failure_breakdown` 에 노출
- 52항목 전수 단언 테스트 (`tests_dm_error_policy.py` 신규)
- **필드 추가만**이라 어드민 배포 전이어도 안전 (프론트 zod 가 모르는 필드 무시)
- 배포 후 `admin:dash:ops:*` 선별 삭제

### Phase 2 — 백엔드: 창 만료를 피크 / 방치로 분리 ✅ **구현 완료 (2026-07-31)**

> ⚠️ 구현 중 정정: `SentDMLog` 에는 **`updated_at` 이 없다**(auto_now 컬럼 미보유).
> 최초 판정식이 `updated_at` 을 참조해 `send_dm_task` 가 AttributeError 로 죽었고
> `tests_rate_defer` 가 잡았다. 실제 필드인 `next_retry_at` → `submitted_at` 순으로 보고,
> 둘 다 없으면(한 번도 defer·호출되지 않음) 방치로 판정한다.

```python
# apps/integrations/tasks.py — 내부 가드(1554~1565)
def _window_expiry_kind(log) -> str:
    ref = log.next_retry_at or log.updated_at
    hours = getattr(settings, "DM_BACKLOG_OLDEST_ALERT_HOURS", 2)
    return "stalled" if timezone.now() - ref > timedelta(hours=hours) else "peak"

log.mark_failed(
    status=SentDMLog.Status.FAILED_WINDOW,
    error_message=(f"Messaging window expired while waiting for send capacity "
                   f"(window={'7d(comment)' if log.comment_id else '24h(user_id)'}, "
                   f"retry_count={log.retry_count})"),
    error_subcode=f"window_{_window_expiry_kind(log)}",   # window_stalled | window_peak
)
```
- 내부 가드분은 원래 subcode 가 비어 있어 **Meta subcode 와 충돌 없음 · 마이그레이션 불필요**
- 사전에 2줄 추가: `("", "window_stalled")` 🔴 / `("", "window_peak")` ⚪
- 과거 데이터는 빈 subcode 그대로 → **소급 변경 없음**

### Phase 3 — 백엔드: 사전 공백 메우기 (Phase 0 결과 반영)

- MISSING 조합 등록 (`1/2/17/32/368`, 필요 시 `2534001`)
- **code 100 원문 규칙 추가** → "댓글 7일 초과"(⚪)와 "파라미터 미확정"(🔴) 분리
- `_BY_CODE["no_trace"]` 는 삭제 대신 "미사용(디버깅용)" 주석 명시

### Phase 4 — 어드민 프론트 **(어드민팀 제안서 — 우리는 배포하지 않음)**

> 아래는 어드민팀에 전달할 내용입니다. 백엔드 배포와 **순서 의존성이 없어** 언제 반영해도 됩니다.
> 참고: Phase 2 이후 로그 상세의 subcode 칩에 `window_peak` 같은 **문자열 subcode** 가 나타납니다(정상).

1. B1 수정 (`failed_no_trace → attention`)
2. B2 수정 (코드 프리셋 제거)
3. B3 정리 (로컬 코드 맵 최소화 — 서버 `policy`/`error_title` 이 1순위)
4. D1 통일 (그룹 라벨을 서버 `status_group_display` 로)
5. D3 동기화 (목 사전 6줄)
6. **화면 재구성**: 🔴 8카드 펼침 / ⚪ 8카드 접힘 — **한 번만 고치고 끝**.
   자동 안내를 나중에 구현해도 이 화면은 다시 손대지 않습니다(배지 없음)

### Phase 5 — 모니터링

- `window_stalled` 는 **1건이라도** 텔레그램 경고 (우리 적체 신호)
- `dashboard_constants.py` 에 `policy=investigate` 건수 임계 추가 → 운영 대시보드 "처리 필요"

### Phase 6 — 어드민팀 요청서 반영 ✅ **구현 완료 (2026-07-31)**

어드민팀이 이 계획으로 화면을 설계하면서 두 라운드의 요청서를 보냈다. 회신은
`ADMIN_DM_ERROR_PROPOSAL_R11.md` / `ADMIN_DM_ERROR_PROPOSAL_R12.md`.

11차 (DM-6~12) — 화면이 쓸 **숫자**를 만드는 단계:
- `stats.follow_up` 후속 DM 사람 단위 축 (`basis=latest_per_person`)
- `stats.not_sent` · `follow_up.not_sent` 사람 단위 🔴/⚪ 분해 (`dm_policy_rollup.py`)
- 수신자 목록 `error_policy` · `latest_followup_status`
- 문구 16행 교체(내부 용어 제거) + `2534001` 등록
- `unique_accepted_pending`
- DM-12 는 **역제안** — 그룹 라벨 대신 `POLICY_DISPLAY` 를 `조사 필요`/`자동 처리` 로

12차 (DM-13~17) — 그 숫자를 눌렀을 때 **착지할 필터**를 만드는 단계:
- `?dm_axis=opening|follow_up` (축 = 그 축의 '발송 안 됨' 모수)
- `reason` 머신 키 + `?error_reason=` — 문구·code 로는 필터 불가(사유 1개 = 코드 4조합)
- **500쌍 상한 폐기** — 사전을 SQL 로 컴파일 (`dm_error_filters.py`, 마이그레이션 없음)
- `error_title` 기준을 최신 **실패** 로그로 통일 (`error_policy` 와 근거 로그 일치)
- `follow_up.accepted_pending` → `accepted_pending_in_waiting` (부모 집합이 오프닝 축과 반대)

부수 효과로 **건너뜀(skipped) 로그의 두 갈래 판정을 없앴다** — 사유표를
`views/dashboard_ops.py` → `dm_error_catalog.py` 로 옮기고 `classify()` 를 단일 판정
함수로 만들었다. 전에는 같은 건너뜀 로그가 운영 대시보드에서는 '월 DM 한도 도달'(⚪),
`not_sent` 에서는 '분류되지 않은 실패'였고 미분류 건너뜀의 policy 도 갈렸다.

> ⚠️ 판정 규칙을 파이썬(`classify`)과 SQL(`dm_error_filters`) **두 벌**로 들게 됐다.
> `tests_dm_error_filters.py::TestSqlPythonEquivalence` 가 사전 전 조합을 DB 에 넣고
> 행 단위로 대조한다 — 사전을 고칠 때 이 테스트를 반드시 통과시킬 것.

### 롤백

Phase 1·3·4·6 은 코드 되돌리면 즉시 원복. Phase 2 의 subcode 표식은 데이터에 남지만 무해하며, 사전에서 두 줄만 빼면 기존 문구로 복귀.

---

## 8. 유저 콘솔(프론트) 반영 — §3 백로그와 동일

| # | 항목 | 지금 | 바꿀 것 |
|---|---|---|---|
| F1 | 한도 소진 (`upgrade_notice`) | 고객에겐 **아무것도 안 감** | 한도 도달 배너 + 업그레이드 CTA. 건너뜀 문구의 **"시간당 한도"는 v4.3 에서 제거된 개념**이라 함께 정정 (`dm_frontend_actions.py:198`) |
| F2 | 피크 지연 (`peak_notice`) | 없음 | "요청이 몰려 제때 보내지 못했습니다" 안내 |
| F3 | 재연동 (`reconnect_notice`) | 로그 상세 CTA 만 | 대시보드 **상단 배너** (연결 status 기반) |
| F4 | 창 만료 문구 (`expiry_notice`) | "24시간" 단일 | "댓글 답장 7일 / DM 24시간" 경로별 |
| F5 | 수신자 없음(2534014) | "댓글 7일 초과…"로 뭉뚱그림 | **"수신자를 찾을 수 없음"** 분기 (`dm_frontend_actions.py:136`) |
| F6 | 게시물 차단(2534066) | 일반 실패 | "이 게시물은 자동 DM 이 막혔습니다" (사내 정책 확정 후) |
| F7 | 상태 라벨 | 콘솔·어드민 상이 | 서버 `status_group_display` 로 통일 |

---

## 9. 결정이 필요한 것

1. **`2534023`(이미 답글) 중복 판정 방법** — 같은 `comment_id` 에 우리 로그가 2건 이상이면 어드민 상세에 "중복 발송 의심" 배지. 집계 변경 없이 가능합니다.
2. **`2534066`(게시물 차단) 정책** — 사내 논의 결론이 나오면 🔴 유지 여부 확정. 함께 넣을 수 있는 것: **게시물별 실패 집중도** 패널.
3. **`upgrade_notice` 의 노출 위치** — 한도 소진 시 대시보드 배너만인지, 캠페인 상세·로그 목록에도 띄울지.
