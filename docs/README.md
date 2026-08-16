# 문서 색인

2026-08-10 정리. 루트에 47개가 흩어져 있던 문서를 용도별로 옮겼습니다(`git mv` — 이력 보존).
루트에 남긴 것은 `README.md` · `CLAUDE.md` · `프로젝트 지침서.md` 셋뿐입니다.

| 폴더 | 용도 | 대상 |
|---|---|---|
| [frontend/](frontend/) | **API 계약·연동 가이드** — 프론트/어드민 콘솔팀에 전달한 문서 | 프론트 개발자 |
| [ops/](ops/) | 운영·인프라·보안·배포 | 백엔드/운영 |
| [system/](system/) | 백엔드 내부 동작 설명 | 백엔드 |
| [archive/](archive/) | **완료·대체된 일회성 문서** (참고용, 최신 아님) | — |
| [legal/](legal/) | 법무 자료 (**수정 금지**, 요청 시에만) | — |

---

## frontend/ — 프론트 연동 계약서

작성 시점의 API 계약입니다. 구현이 바뀌면 이 문서도 갱신하세요.

**결제·구독**
- [TOSS_BILLING_FRONTEND.md](frontend/TOSS_BILLING_FRONTEND.md) — 토스 빌링 연동(카드등록 → prepare/confirm, 체험·해지·카드변경)
- [CANCEL_RETENTION_FRONTEND.md](frontend/CANCEL_RETENTION_FRONTEND.md) — 해지 리텐션(일시정지·50% 할인·윈백)
- [REFERRAL_COUPON_FRONTEND.md](frontend/REFERRAL_COUPON_FRONTEND.md) — 쿠폰/제휴코드(무카드 redeem 폐지 + 결제 전 미리보기)
- [PAYMENT_CONSENT_HANDOVER.md](frontend/PAYMENT_CONSENT_HANDOVER.md) — ⭐ **프론트 전달본** — 2차 동의 폐기 결정 + 붙일 API 2개 + dev 테스트 계정 5개
- [PAYMENT_CONSENT_FRONTEND.md](frontend/PAYMENT_CONSENT_FRONTEND.md) — 결제 전 고지·동의 상세 계약(견적 preview · 동의 원장 · 2차 동의는 dormant)
- [MARKETING_OPT_IN_FRONTEND.md](frontend/MARKETING_OPT_IN_FRONTEND.md) — 마케팅 수신동의

**IG 연동**
- [IG_OAUTH_RETURN_TO_FRONTEND.md](frontend/IG_OAUTH_RETURN_TO_FRONTEND.md) — 같은-탭 복귀(`return_to`) · postMessage 계약 (iOS 앱 가로채기 대응)
- [IG_ACCOUNT_ACTIVATION_FRONTEND.md](frontend/IG_ACCOUNT_ACTIVATION_FRONTEND.md) — 추가 계정 축소·활성 계정 선택
- [WEBHOOK_HEALTH_FRONTEND.md](frontend/WEBHOOK_HEALTH_FRONTEND.md) — 연결 헬스 진단·웹훅 재구독
- [CONNECT_CONFLICT_WARNING_FRONTEND.md](frontend/CONNECT_CONFLICT_WARNING_FRONTEND.md) — 타 DM 툴 충돌 경고 배너
- [DISCONNECT_OTHER_DM_TOOLS_GUIDE.md](frontend/DISCONNECT_OTHER_DM_TOOLS_GUIDE.md) — 매니챗 등 연결 해제 안내

**DM 캠페인**
- [DM_QUEUE_STATE_FRONTEND.md](frontend/DM_QUEUE_STATE_FRONTEND.md) — 순차 발송 큐 현황(게이지·ETA·사람 단위 `people`)
- [DM_RECOVERY_FRONTEND.md](frontend/DM_RECOVERY_FRONTEND.md) — 실패 DM 복구(재댓글 방식)
- [CAMPAIGN_TIMESERIES_FRONTEND.md](frontend/CAMPAIGN_TIMESERIES_FRONTEND.md) — 신규 요청자 시계열
- [DM_CAMPAIGN_DUPLICATE_PREVENTION_FRONTEND.md](frontend/DM_CAMPAIGN_DUPLICATE_PREVENTION_FRONTEND.md) — 게시물당 활성 캠페인 1개(409)
- [DM_CAMPAIGN_MIGRATION_FRONTEND.md](frontend/DM_CAMPAIGN_MIGRATION_FRONTEND.md) — 타 툴에서 캠페인 이전
- [DM_CAMPAIGN_THUMBNAIL_FRONTEND.md](frontend/DM_CAMPAIGN_THUMBNAIL_FRONTEND.md) — 썸네일 재호스팅
- [DM_USER_COPY_MAPPING.md](frontend/DM_USER_COPY_MAPPING.md) · [USER_CONSOLE_DM_COPY_REQUEST.md](frontend/USER_CONSOLE_DM_COPY_REQUEST.md) — 유저 콘솔 DM 문구
- [USER_NOTIFICATION_PLAN.md](frontend/USER_NOTIFICATION_PLAN.md) — 사용자 알림 계획(구현 전)

**어드민 콘솔**
- [ADMIN_20TH_ROUND_RESPONSE.md](frontend/ADMIN_20TH_ROUND_RESPONSE.md) — 20차 회신(OPS-5/6/7·UI-1, 구현 완료·prod 미배포). 🔴 **OPS-6: 프론트의 '대기' 공식이 `rate_limited`·legacy `pending` 을 빠뜨려 이미 8배 어긋나 있었다**(dev 2 vs 16) → `pending_total` 신설 · OPS-5 `action_required` 응답에서 제거 · OPS-7 `status_summary` 를 window 비종속(24h 고정)+`basis` · UI-1 `/admin/me/preferences/`
- [ADMIN_AUTH_MFA_FRONTEND.md](frontend/ADMIN_AUTH_MFA_FRONTEND.md) — **v2 계약(구현 완료·prod 미배포)** · 2단계 로그인(비번 → TOTP) + 어드민 전용 토큰(access 2h / 신뢰기기 refresh 7d). `/api/v1/admin/**` 에 일반 토큰은 403 `admin_token_required`. 마케팅 전용 계정은 제외(1단계 로그인 유지, **갱신 URL 이 갈린다**)
- [ADMIN_AUTH_MFA_RESPONSE.md](frontend/ADMIN_AUTH_MFA_RESPONSE.md) — 위 계약 Q1~Q5 회신 + **v1→v2 변경점 4건**(백업코드 별도 필드·12자 / confirm 의 setup_token 필수 / 재등록에 현재 코드 / password_changed 삭제)
- [ADMIN_SNAPSHOT_ROSTER_RESPONSE.md](frontend/ADMIN_SNAPSHOT_ROSTER_RESPONSE.md) — 18차 회신(SNAP-1/2) · 전체 현황 타일 → 회원 명단(`/admin/snapshot/paying|trial/`, 타일-명단 항등·id 집합 캐시)

**기타**
- [INSTA_REPORT_FRONTEND.md](frontend/INSTA_REPORT_FRONTEND.md) — 인스타 성장 리포트(프로 전용·월1회)
- [SIGNUP_ATTRIBUTION_FRONTEND.md](frontend/SIGNUP_ATTRIBUTION_FRONTEND.md) — 방문→가입 채널 귀속
- [AI_PAGE_GENERATION_GUIDE.md](frontend/AI_PAGE_GENERATION_GUIDE.md) — AI 페이지 생성 4단계
- [PASSWORD_RESET_GUIDE.md](frontend/PASSWORD_RESET_GUIDE.md) — 비밀번호 재설정
- [RATE_LIMIT_AND_GOOGLE_LOGIN_FRONTEND.md](frontend/RATE_LIMIT_AND_GOOGLE_LOGIN_FRONTEND.md) — 🔴 **429 두 종류 분기 필수**(`RATE_LIMITED` vs `PLAN_LIMIT_EXCEEDED` — 안 하면 paywall 분석 오염) + 구글 로그인 `GOOGLE_EMAIL_UNVERIFIED` 403
- [RETIRE_OLD_API_HOST_REQUEST.md](frontend/RETIRE_OLD_API_HOST_REQUEST.md) — ✅ 회신 받음(1차) · 서버측(Pages Function)이 은퇴한 API 호스트를 호출 중 → 교체 요청
- [RETIRE_OLD_API_HOST_ROUND2.md](frontend/RETIRE_OLD_API_HOST_ROUND2.md) — ✅ **완결(2026-08-12)** · `/media/` 저장 URL R2 이관(61행/101 URL) + 프론트 배포 반영 → 소비자 0건 확인 → DNS 삭제. 결과는 [ops/DNS_RETIRE_API_TURNFLOW.md](ops/DNS_RETIRE_API_TURNFLOW.md)

## ops/ — 운영·인프라·보안

- [배포방법.md](ops/배포방법.md) — **prod 배포는 여기부터**(수동 compose 금지 이유 포함)
- [NEXT_ACTIONS_2026-08-04.md](ops/NEXT_ACTIONS_2026-08-04.md) — **현재 우선순위 로드맵**
- [PROD_HARDENING_2026-08-04.md](ops/PROD_HARDENING_2026-08-04.md) — 08-03~04 하드닝 실행 기록 + 사고 3건
- [DNS_RETIRE_API_TURNFLOW.md](ops/DNS_RETIRE_API_TURNFLOW.md) — 옛 API 호스트 DNS 삭제(2026-08-12) + **복구 레시피** · 오리진 IP 직노출 제거
- [SECURITY_AUDIT_2026-06.md](ops/SECURITY_AUDIT_2026-06.md) — 애플리케이션 취약점 감사(미해결 포함)
- [ADMIN_AUTH_HARDENING_PLAN.md](ops/ADMIN_AUTH_HARDENING_PLAN.md) — 🟡 **계획(미구현)** · 어드민 계정 3인 분리 + 어드민 전용 JWT(TOTP 2요소) + Django admin 세션 MFA. 지문은 서버가 검증 불가 → 실선택지는 TOTP/이메일/패스키
- [DR_IMPLEMENTATION_PLAN.md](ops/DR_IMPLEMENTATION_PLAN.md) — 재해복구 설계·결정 로그
- [INSTAGRAM_OAUTH_FLOW.md](ops/INSTAGRAM_OAUTH_FLOW.md) · [INSTAGRAM_TEST_GUIDE.md](ops/INSTAGRAM_TEST_GUIDE.md)
- [CLOUDFLARE_TUNNEL_SETUP.md](ops/CLOUDFLARE_TUNNEL_SETUP.md) — dev 공개(`dev-api.turnflow.link`)
- [EMAIL_TEMPLATE_HANDOFF_FOR_CS.md](ops/EMAIL_TEMPLATE_HANDOFF_FOR_CS.md)

관련 런북은 `deploy/` 아래에도 있습니다 — `deploy/SERVER_RUNBOOK.md`, `deploy/dr/gcp/DRILL_RUNBOOK.md`.

## system/ — 백엔드 동작 설명

- [AUTODM_DELIVERY_LIFECYCLE.md](system/AUTODM_DELIVERY_LIFECYCLE.md) (+ `.html`) — 자동 DM 발송 라이프사이클
- [SPAM_FILTER_SYSTEM.md](system/SPAM_FILTER_SYSTEM.md) — 스팸 판정 체계
- [DM_ERROR_POLICY_PLAN.md](system/DM_ERROR_POLICY_PLAN.md) (+ `DM_ERROR_POLICY_MATRIX.html`) — DM 오류 2분류 정책
- [AD_COMMENT_WEBHOOK_EVIDENCE.md](system/AD_COMMENT_WEBHOOK_EVIDENCE.md) — 광고 댓글 웹훅 실측 근거(코드가 참조)
- [SERVICE_DIFFERENTIATION.md](system/SERVICE_DIFFERENTIATION.md) — 서비스 차별점(세일즈)

## archive/ — 완료·대체됨

최신 정보가 아닙니다. 이력 참고용으로만 보세요.

- `ADMIN_DM_ERROR_PROPOSAL.md` · `_R11.md` · `_R12.md` — 어드민팀 11·12차 회신(2026-07-31 배포 완료).
  현재 정책은 [system/DM_ERROR_POLICY_PLAN.md](system/DM_ERROR_POLICY_PLAN.md) 를 보세요.
- `SECURITY_AUDIT_2026-08-03_PROD_INFRA.md` — 하드닝 **이전** 진단서.
  실제 조치 결과와 정정은 [ops/PROD_HARDENING_2026-08-04.md](ops/PROD_HARDENING_2026-08-04.md) 에 있습니다.
