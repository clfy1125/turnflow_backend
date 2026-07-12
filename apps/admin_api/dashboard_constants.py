"""apps/admin_api/dashboard_constants.py — 어드민 대시보드 임계값 단일 소스.

운영(Operations)·마케팅(Marketing) 대시보드가 공유하는 **네이밍된 임계값**과
서브시스템 상태(ok/warning/critical) 판정 규칙을 한 곳에 모은다.
프론트엔드 색상 매핑 계약이므로 값 변경 시 프론트와 반드시 합의할 것.

──────────────────────────────────────────────────────────────────────
상태 매핑 테이블 (status_summary.subsystems — 프론트 계약)
──────────────────────────────────────────────────────────────────────

| 서브시스템       | ok                                    | warning                                   | critical                                        |
|------------------|---------------------------------------|--------------------------------------------|--------------------------------------------------|
| dm               | sample < DM_MIN_SAMPLE_FOR_STATUS(20) | 0.75 <= delivery_rate < 0.90               | delivery_rate < DM_DELIVERY_CRITICAL_THRESHOLD   |
|                  | 이면 무조건 ok(+insufficient_sample), | (DM_DELIVERY_WARNING_THRESHOLD 미만)        | (0.75) 미만 (sample >= 20 일 때만 판정)          |
|                  | 또는 rate >= 0.90                      |                                            |                                                  |
| ig_connections   | expired + expiring_24h == 0            | expired + expiring_24h >= 1                | expired >= IG_EXPIRED_CRITICAL_COUNT(10)         |
| spam_filter      | 윈도우 내 hide_failed == 0             | hide_failed >= SPAM_HIDE_FAILED_WARNING(1) | hide_failed >= SPAM_HIDE_FAILED_CRITICAL(10)     |
| billing          | 아래 조건 전부 아님                    | failed_payments >= 1 OR past_due >= 1      | past_due >= PAST_DUE_CRITICAL_COUNT(10) OR       |
|                  |                                        | OR webhook_backlog >= 1                    | 30분+ 미처리 토스 웹훅 존재                       |

- overall = worst-of(subsystems): ok < warning < critical.
- 경계 의미론: 비율(rate) 임계값은 **strict <** (rate == 0.90 → ok, rate == 0.75 → warning).
  토큰 만료 컷오프는 **<=** (token_expires_at == now+24h → expiring_24h 포함).
- action_required 항목 severity: count == 0 → "ok", count >= 1 → "warning" (고정 규칙).
"""

# ── DM 발송 품질 ──────────────────────────────────────────────────────
# 기존 LOW_DELIVERY_THRESHOLD(apps/admin_api/views/dashboard.py) 정책 재사용.
# rate < 0.90 → warning
DM_DELIVERY_WARNING_THRESHOLD = 0.90
DM_DELIVERY_CRITICAL_THRESHOLD = 0.75  # rate < 0.75 → critical
# accepted_or_after 표본이 이 값 미만이면 상태 판정 안 함 (ok + insufficient_sample=true)
DM_MIN_SAMPLE_FOR_STATUS = 20
# 기존 정책 재사용 — views/dashboard.py::STUCK_SUBMITTING_MINUTES 와 동일 값.
# WIP 인접 파일을 리팩터링하지 않기 위해 의도적으로 중복(상호 참조 주석)한다.
STUCK_SUBMITTING_MINUTES = 10
QUEUE_WINDOW_RISK_HOURS = 6  # 기존 AdminDMBacklogView(risk_hours) 기본값 재사용

# ── IG 연동 ──────────────────────────────────────────────────────────
TOKEN_EXPIRING_SOON_HOURS = 24  # token_expires_at <= now+24h 인 ACTIVE 연동 → 주의
IG_EXPIRED_CRITICAL_COUNT = 10  # expired 연동이 이 수 이상 → critical

# ── 스팸 필터 ────────────────────────────────────────────────────────
SPAM_HIDE_FAILED_WARNING_COUNT = 1  # 윈도우 내 FAILED 숨김 1건 이상 → warning
SPAM_HIDE_FAILED_CRITICAL_COUNT = 10  # 10건 이상 → critical

# ── 빌링 ─────────────────────────────────────────────────────────────
PAYMENT_FAILED_WARNING_COUNT = 1  # 윈도우 내 FAILED 결제 1건 이상 → warning
PAST_DUE_CRITICAL_COUNT = 10  # past_due 구독 이 수 이상 → critical
WEBHOOK_BACKLOG_STALE_MINUTES = 10  # processed=False && created_at < now-10m → backlog 카운트
WEBHOOK_BACKLOG_CRITICAL_MINUTES = 30  # 30분 넘게 미처리 웹훅 존재 → critical

# ── 위험 계정 스코어링 (ops.risk_accounts) ───────────────────────────
# 최근 24h failed_param 이 이 수 이상이면 reason "repeated_param_errors" (+1점)
RISK_REPEATED_PARAM_ERRORS_COUNT = 5

# ── 업셀 후보 스코어링 (marketing.upsell_candidates) ─────────────────
# DM 쿼터 사용률 (dm_limits 실제 과금 정의 기준: (캠페인×수신자) 고유쌍 / 월 한도)
UPSELL_DM_RATIO_HIGH = 0.8  # >= 0.8 → +3 (dm_quota_80pct)
UPSELL_DM_RATIO_MID = 0.5  # >= 0.5 → +2 (dm_quota_50pct)
UPSELL_CLICKS_HIGH = 500  # 최근 30d 페이지 클릭 >= 500 → +2 (high_page_traffic)
UPSELL_CLICKS_MID = 100  # >= 100 → +1 (high_page_traffic)
UPSELL_SPAM_HEAVY = 50  # 최근 30d 스팸 차단 >= 50 → +1 (heavy_spam_filtering)
UPSELL_MULTI_IG_MIN = 2  # 활성 IG 연동 >= 2 → +2 (multiple_ig_connections)

# ── 리스트 캡 ────────────────────────────────────────────────────────
RECENT_ERRORS_LIMIT = 20
RISK_ACCOUNTS_LIMIT = 5
TOP_PAGES_LIMIT = 5
UPSELL_CANDIDATES_LIMIT = 10
# 온보딩 이탈자 각 세그먼트에 딸려 보내는 샘플 회원 수 (CS 드릴다운용)
ONBOARDING_SAMPLE_LIMIT = 5
# 구독 유지·해지 — 최근 취소 예약 고객 리스트 캡 (CS 액션용)
RECENT_CANCELLATIONS_LIMIT = 8
# 해지 사유 TOP N
CANCEL_REASONS_TOP = 5

# ── 유료 전환 분석 ───────────────────────────────────────────────────
# 결제 후 '실제 사용' 관찰 창 (일) — paid_at 이후 N일 내 기능 사용 여부.
POST_PAYMENT_WINDOW_DAYS = 7
# 결제 진입 경로 귀속 창 (일) — 유저별 첫 PAID 이전 N일 내 마지막 CheckoutEvent 트리거를
# 그 전환의 진입 경로로 귀속.
CHECKOUT_ATTRIBUTION_WINDOW_DAYS = 30

# ── 캐시 ─────────────────────────────────────────────────────────────
OPS_DASHBOARD_CACHE_TTL = 30  # 초 — 어드민 30~60s 폴링 대비
MARKETING_DASHBOARD_CACHE_TTL = 300  # 초
