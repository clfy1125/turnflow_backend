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

## 루트 문서

- [TURNFLOW_사업_기술_개요.md](TURNFLOW_사업_기술_개요.md) — ⭐ **회사·제품 소개(백엔드 기준)** —
  "TurnFlow가 무슨 사업인지" 처음 접하는 사람용. 사업 모델·요금제·기능 전체·개발 자산 규모·운영 성숙도.
  기업가치 평가/투자 자료용 원천

---

## frontend/ — 프론트 연동 계약서

작성 시점의 API 계약입니다. 구현이 바뀌면 이 문서도 갱신하세요.

- [CAMPAIGN_ACTIVATION_DROPOFF_ANALYSIS_2026-09-10.md](frontend/CAMPAIGN_ACTIVATION_DROPOFF_ANALYSIS_2026-09-10.md) — **연동은 했는데 캠페인을 안 만드는 원인 분석 + 개선 아이디어**(액세스 로그 퍼널 + DB + 프론트 코드 리딩). ★모바일 21.0% vs 데스크톱 44.8% · 첫 캠페인의 55.9%가 **연동 1시간 내** · 비활성 저장버튼이 사유를 안 알려준다(`isSubmitDisabled` 7조건·안내 0개) · 모바일 저장버튼이 폼 맨 끝(sticky 아님) · **캠페인 0개 계정 460개 중 415개(90.2%)가 이미 이전 분석 완료 → 즉시 켤 초안 542건이 방치** · apply 가 INACTIVE 라 이전초안 활성률 20.3%(직접생성 84.9%)

**결제·구독**
- [TOSS_BILLING_FRONTEND.md](frontend/TOSS_BILLING_FRONTEND.md) — 토스 빌링 연동(카드등록 → prepare/confirm, 체험·해지·카드변경)
- [EXTRA_IG_ACCOUNT_TRIAL_FRONTEND.md](frontend/EXTRA_IG_ACCOUNT_TRIAL_FRONTEND.md) — ⭐ **v2** · 체험 중 추가 IG 계정 **0원 즉시 추가**(체험 400 폐지 + `trial` 플래그) · 견적 400 사유 노출 요청 · dev 테스트 카드 실측표 · **체험 중 상한 없음 = 의도된 결정**
- [EXTRA_IG_ACCOUNT_TRIAL_RESPONSE.md](frontend/EXTRA_IG_ACCOUNT_TRIAL_RESPONSE.md) — 위 문서에 대한 프론트 회신 답변(에러 두 포맷 동시 송출로 수정·배포, 상한 감수 결정, 400 문구 전문)
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
- [DM_ACCOUNT_PAUSE_RESPONSE.md](frontend/DM_ACCOUNT_PAUSE_RESPONSE.md) — 계정 제한 발송 정지 표시(`user_reason=account_send_paused`) + `waiting_window_risk` + dev 재현 커맨드
- [DM_RECOVERY_FRONTEND.md](frontend/DM_RECOVERY_FRONTEND.md) — 실패 DM 복구(재댓글 방식)
- [CAMPAIGN_RESUME_REVIVE_FRONTEND.md](frontend/CAMPAIGN_RESUME_REVIVE_FRONTEND.md) — 재개 시 정지 중 밀린 DM 자동 재발송(`revive_queued`)
- [CAMPAIGN_TIMESERIES_FRONTEND.md](frontend/CAMPAIGN_TIMESERIES_FRONTEND.md) — 신규 요청자 시계열
- [DM_CAMPAIGN_DUPLICATE_PREVENTION_FRONTEND.md](frontend/DM_CAMPAIGN_DUPLICATE_PREVENTION_FRONTEND.md) — 게시물당 활성 캠페인 1개(409)
- [CAMPAIGN_RESTRICTION_CHECK_FRONTEND.md](frontend/CAMPAIGN_RESTRICTION_CHECK_FRONTEND.md) — 게시물 '연령 제한' 점검 API 계약. 생성·**활성화** 409 `media_content_restricted` (복사는 허용) + 자동정지 `auto_paused_at`
- [CAMPAIGN_RESTRICTION_CHECK_RESPONSE.md](frontend/CAMPAIGN_RESTRICTION_CHECK_RESPONSE.md) — 위 API 확인요청 6건(B1~B6) 회신. ★ 복사 허용·활성화 게이트·증거 유효기간 14일
- [DM_ACCOUNT_PAUSE_RECHECK_RESPONSE.md](frontend/DM_ACCOUNT_PAUSE_RECHECK_RESPONSE.md) — 계정 발송 정지(Meta 368) 제한 확인·재개 API 회신. ★쿼다운을 **풀고** 시험발송하면 정지가 24h→48h 로 늘어난다 · 인스타 제한 종료 시각은 API 로 모른다 · 368 증거 미기록 결함
- [IG_AGE_RESTRICTION_CREATOR_GUIDE.md](frontend/IG_AGE_RESTRICTION_CREATOR_GUIDE.md) — 연령 제한을 피하는 게시물 작성요령(고객 안내용) + 감지 레시피
- [DM_CAMPAIGN_MIGRATION_FRONTEND.md](frontend/DM_CAMPAIGN_MIGRATION_FRONTEND.md) — 타 툴에서 캠페인 이전
- [DM_MIGRATION_VISIBLE_BANDS_2026-08-18.md](frontend/DM_MIGRATION_VISIBLE_BANDS_2026-08-18.md) — ⚠️ 후보는 `auto_draft` 만 내려간다(검수필요는 DB 에만). 밴드 탭·`confirm-link` 는 렌더 안 됨
- [DM_CAMPAIGN_THUMBNAIL_FRONTEND.md](frontend/DM_CAMPAIGN_THUMBNAIL_FRONTEND.md) — 썸네일 재호스팅
- [DM_USER_COPY_MAPPING.md](frontend/DM_USER_COPY_MAPPING.md) · [USER_CONSOLE_DM_COPY_REQUEST.md](frontend/USER_CONSOLE_DM_COPY_REQUEST.md) — 유저 콘솔 DM 문구
- [USER_NOTIFICATION_PLAN.md](frontend/USER_NOTIFICATION_PLAN.md) — 사용자 알림 계획(구현 전)

**어드민 콘솔**
- [ADMIN_20TH_ROUND_RESPONSE.md](frontend/ADMIN_20TH_ROUND_RESPONSE.md) — 20차 회신(OPS-5/6/7·UI-1, 구현 완료·prod 미배포). 🔴 **OPS-6: 프론트의 '대기' 공식이 `rate_limited`·legacy `pending` 을 빠뜨려 이미 8배 어긋나 있었다**(dev 2 vs 16) → `pending_total` 신설 · OPS-5 `action_required` 응답에서 제거 · OPS-7 `status_summary` 를 window 비종속(24h 고정)+`basis` · UI-1 `/admin/me/preferences/`
- [ADMIN_AUTH_MFA_FRONTEND.md](frontend/ADMIN_AUTH_MFA_FRONTEND.md) — **v2 계약(구현 완료·prod 미배포)** · 2단계 로그인(비번 → TOTP) + 어드민 전용 토큰(access 2h / 신뢰기기 refresh 7d). `/api/v1/admin/**` 에 일반 토큰은 403 `admin_token_required`. 마케팅 전용 계정은 제외(1단계 로그인 유지, **갱신 URL 이 갈린다**)
- [ADMIN_AUTH_MFA_RESPONSE.md](frontend/ADMIN_AUTH_MFA_RESPONSE.md) — 위 계약 Q1~Q5 회신 + **v1→v2 변경점 4건**(백업코드 별도 필드·12자 / confirm 의 setup_token 필수 / 재등록에 현재 코드 / password_changed 삭제)
- [ADMIN_SNAPSHOT_ROSTER_RESPONSE.md](frontend/ADMIN_SNAPSHOT_ROSTER_RESPONSE.md) — 18차 회신(SNAP-1/2) · 전체 현황 타일 → 회원 명단(`/admin/snapshot/paying|trial/`, 타일-명단 항등·id 집합 캐시)

**기타**
- [NATIVE_APP_CORS_RESPONSE.md](frontend/NATIVE_APP_CORS_RESPONSE.md) — 네이티브 앱(Capacitor) origin CORS 허용 회신. `https://localhost`(Android)·`capacitor://localhost`(iOS)를 **env 아닌 코드**(`NATIVE_APP_CORS_ORIGINS`)로 합류 — 앱 상수라 환경마다 다를 이유가 없고 env 는 DR/새 환경에서 조용히 빠진다. `capacitor://` 는 정규식 불필요(정확 일치로 매칭됨, 실측). ⚠️ IG `return_to` 상속에서는 **의도적 제외** · 프론트가 곧 만날 것 = **CS 워커 staging↔prod 짝 어긋남으로 인한 401**
- [INSTA_REPORT_FRONTEND.md](frontend/INSTA_REPORT_FRONTEND.md) — 인스타 성장 리포트(프로 전용·월1회)
- [SIGNUP_ATTRIBUTION_FRONTEND.md](frontend/SIGNUP_ATTRIBUTION_FRONTEND.md) — 방문→가입 채널 귀속
- [AI_PAGE_GENERATION_GUIDE.md](frontend/AI_PAGE_GENERATION_GUIDE.md) — AI 페이지 생성 4단계
- [PASSWORD_RESET_GUIDE.md](frontend/PASSWORD_RESET_GUIDE.md) — 비밀번호 재설정
- [ACCOUNT_DELETION_AND_OAUTH_STATE_FRONTEND.md](frontend/ACCOUNT_DELETION_AND_OAUTH_STATE_FRONTEND.md) — ⭐ **탈퇴 차단 해제 + 소셜 로그인 state 유실**(2026-09-22, 마이그 없음. CS #247995c5 한 건에서 결함 3개). 백엔드 완료: ①`me/delete/` 409 조건에 **`has_billing_key` 추가** — 카드 없는 자동 프로체험이 탈퇴를 막던 것 (차단 근거인 '잔여기간 소멸·빌링키 고아'가 무카드 체험엔 성립 안 함). 실피해: 자동체험 217건 중 해지 5건인데 **4건이 가입 1시간 이내** = 탈퇴 대신 강제된 해지가 이탈 지표를 오염. ②409 에 `detail`+envelope **동시 송출** + `details.web_deletion_url`. 프론트 요청: **F-1** 탈퇴 모달이 **401 외 전부를 generic 문구로 뭉갠다**(배포본 확인 — 백엔드 문구가 화면에 못 닿아 고객이 스스로 짐작해 구독을 해지했다. `QuoteGate` 와 동형) · **F-2** `/delete-account` 는 멀쩡히 있는데 앱에 링크가 없어 **역대 사용 1건** · **F-3 (최대)** OAuth `state` 가 **sessionStorage** 라 탭·웹뷰가 바뀌면 `expectedState=null` → `state_mismatch` **60건/36명**(instagram 32·kakao 28). ⚠️ 55건이 `in_app=false`·android 로 찍히는 건 계측이 **콜백 도착 환경**을 기록하기 때문 — 인앱 문제로만 보면 오독한다. **인스타는 백엔드가 이미 서버 검증**하므로 프론트 로컬 비교만 지우면 32건이 사라진다(백엔드 작업 0). 카카오는 localStorage+10분 TTL. ⚠️ 앱 내 탈퇴=**즉시 하드삭제** / 웹 탈퇴=7일 유예 — 문구를 섞으면 허위 고지
- [DEV_AI_WORKER_AND_DUMMY_MEDIA_2026-09-21.md](frontend/DEV_AI_WORKER_AND_DUMMY_MEDIA_2026-09-21.md) — **dev AI 잡 워커 복구 + 더미 연결 게시물 목**(2026-09-21, 마이그 없음·운영 무영향). ①워커는 죽은 게 아니라 **크래시 루프 7,103회**였다 — `ModuleNotFoundError: pyotp`. ⚠️ **Celery 는 기동 시 URLConf 를 통째로 임포트**하므로 어드민 URL 하나가 못 열리면 워커 전체가 안 뜬다(큐 설정은 멀쩡한데 소비자 0 → 202 받고 영원히 queued). 진짜 원인은 컨테이너 3개가 **2026-08-03 옛 이미지**로 떠 있고 **web 안에서만 `pip install` 로 때워둔 것** — 쓰기 레이어는 컨테이너마다 따로라 celery 엔 없었다. 세 컨테이너를 현재 이미지로 재생성(재빌드 불필요). ②더미 연결이 Graph 로 새던 근본 원인 = **가짜 토큰 접두어가 3종**(`mock_token_`/`mock-token-`/`DEVFAKE-`)인데 판정기가 1종만 알아 **진짜 토큰으로 오인** → 400 → 우리가 500. `is_mock_token` 을 3종 모두 인식으로 넓히고 판정을 `MockInstagramProvider.should_use_mock()` 하나로 모았다(`DEBUG` 동반 요구 → 운영은 절대 목 안 됨). ⚠️ **전역 `INSTAGRAM_MOCK_MODE` 를 켜서 해결하지 말 것** — `is_mock_mode()` 하나만 보는 **인스타 로그인 경로까지 목**이 돼 OAuth 시험이 죽는다. `/media/`(목록·`media_ids` 배치)와 **`ai-suggest` 의 게시물 조회**를 함께 막았다 — 후자를 빼면 '목록엔 보이는데 고르면 404'. 목 썸네일은 인라인 SVG `data:` URI(프론트 CSP `img-src` 에 `data:` 필요)
- [APP_SOCIAL_LOGIN_KAKAO_SDK_RESPONSE_2026-09-16.md](frontend/APP_SOCIAL_LOGIN_KAKAO_SDK_RESPONSE_2026-09-16.md) — **앱 카카오 SDK 전환 회신 + 콘솔 등록**(2026-09-16, 백엔드 코드 변경 없음). 프론트가 앱 복귀를 **웹뷰 내 인가 + 302 가로채기**로 바꿔 웹 번들 배포가 앱 경로에서 빠졌다. ①「우리 앱 토큰」 판정은 `access_token_info.app_id` 와 `KAKAO_APP_ID` **문자열 비교 한 줄**이고 **키 종류를 보지 않는다** → 네이티브 앱 키로 받은 SDK 토큰도 통과(403 `KAKAO_TOKEN_FOREIGN_APP` 안 남). 운영·dev 둘 다 `1573264` 실측. ⚠️ `KAKAO_APP_ID` 가 비면 **503 fail-closed** — 400 이 아니라 503 이면 토큰이 아니라 서버 설정 문제다. ②`access_token` 경로는 **dev-api 에도 배포됨**(양쪽 400 = 검증 도달, 503 아님). 콘솔 등록: 카카오 콘솔 개편으로 Android 플랫폼이 **키 단위**로 들어간다(`앱 > 플랫폼 키 > 네이티브 앱 키 > 수정`) — 패키지명 `link.turnflow.app` · 디버그 키 해시 등록 완료, Redirect URI 5개 불변 확인. ⚠️ 앱 IG 는 **APP state 누적 6건 발급 / 0건 소비** — `start` 는 완벽해졌으나 인가~복귀 구간이 여전히 막혔고, **가짜 콜백(앱이 시작한 내비게이션)이 통과해도 instagram.com 의 302 가 통과한다는 보장은 없다**(WebView 훅이 갈린다) → 웹 번들 복귀 경로를 지우지 말 것
- [APP_SOCIAL_LOGIN_DIAGNOSIS_2026-09-16.md](frontend/APP_SOCIAL_LOGIN_DIAGNOSIS_2026-09-16.md) — **앱 소셜로그인 「안 됨」 원인 진단**(2026-09-16, 코드 변경 없음). 운영 실측 결론: **백엔드는 정상**(웹으로 5명이 이미 IG 가입 완료). 앱은 3회 시도 0회 성공 — 2건은 `redirect_uri` 가 아직 `window.location.origin`(=`https://localhost`)이라 400, 1건은 200 을 받고도 **state 가 끝내 미소비**이고 `POST /auth/instagram/` 은 **운영에 단 한 건도 온 적이 없다**. 원인은 운영 웹 번들에 `link.turnflow.app`·`appUrlOpen`·`client=app` 이 **전부 0회** — 브라우저→앱 복귀 코드가 APK 에만 있고 `turnflow.link` 에 미배포. ⚠️ **카카오도 같은 4단계에서 멈춘다**(웹 콜백→앱 복귀는 공통). 추적법: state 의 `consumed_at` 하나로 어느 단계에서 끊겼는지 판별된다
- [APP_SOCIAL_LOGIN_BACKEND_RESPONSE.md](frontend/APP_SOCIAL_LOGIN_BACKEND_RESPONSE.md) — **앱(Capacitor) 카카오·인스타 로그인**(2026-09-15, 운영 `7b4e114`). 앱은 OAuth 를 시스템 브라우저에서 돌리고 웹 콜백(turnflow.link)으로 돌아온 뒤 커스텀 스킴으로 앱 복귀 — **웹뷰 내 완결은 불가**(iOS `capacitor://localhost` 를 Meta·카카오가 안 받는다). 인스타는 **state 를 서버가 만들어** 프론트가 「앱 표시」를 심을 자리가 없으므로 `GET /auth/instagram/start/?client=app` → state `app_` 접두어. ⚠️ **웹에도 `web_` 접두어를 붙였다** — 앱에만 붙이면 난수가 우연히 `app_` 로 시작할 때(약 1/1670만) 웹 로그인이 앱으로 튕기는 유령 버그가 난다. 교환 계약은 불변. state TTL 10분·**1회용**(appUrlOpen 중복 발화 시 두 번째가 `INSTAGRAM_STATE_USED`). 카카오는 백엔드 변경 없음(state·redirect_uri 를 프론트가 소유)
- [URGENT_CONVERSION_BACKEND_RESPONSE_4.md](frontend/URGENT_CONVERSION_BACKEND_RESPONSE_4.md) — ⭐ **4차 = 운영 배포 완료분**(2026-09-14, 운영 커밋 `43ab213`). ①카카오 운영 503 = **코드만 올리고 .env 키를 빠뜨림**(배포 문제 아님) → 키 3종 투입, 503→400 KAKAO_CODE_INVALID 확인 ②운영 Meta 콜백을 `app.turnflow.link`→**`turnflow.link`** 로 정정(번들 해시 실측: app.turnflow.link 는 **dev 빌드**가 dev-api 를 부른다) ③**견적 버그 원인 2곳** — preview attach_only 가 저장값(0)으로 금액 계산 + confirm attach_only 가 `extra_ig_accounts` 를 확정조차 안 함. `renewal_amount_for(extra)` 단일 소스로 수정(견적이 정가+N×단가로 따로 계산하면 스냅샷·리텐션할인·축소예약이 빠져 고지와 실청구가 갈린다=허위고지). ⚠️ attach_only 는 무카드 레퍼럴 전용이던 드문 경로였는데 **카드 없는 30일로 전 자동체험자가 지나가는 길**이 되며 드러났다. ④무카드 체험 중 extra-accounts 400 은 **의도 유지**(0원으로 5개 잡고 첫 결제 전 이탈 가능)
- [URGENT_CONVERSION_BACKEND_RESPONSE_3.md](frontend/URGENT_CONVERSION_BACKEND_RESPONSE_3.md) — ⭐ **3차(변경분만)**(2026-09-13). ①인스타 로그인 **dev 활성화**(Meta 앱이 dev/운영 **2개**·운영 IG App ID=36036852472566509 · `http://localhost` 는 **저장 시 조용히 사라진다**=HTTPS 전용 → `turnflowlink-dev.pages.dev` 사용, 터널 불필요) ②**계정 매칭 정책 변경** — "인스타로 가입한 계정만 인스타로 로그인". 그 IG 가 다른 계정에 연동 중이면 **409 `INSTAGRAM_ALREADY_CONNECTED_ELSEWHERE`**(+`masked_email`·`instagram_username`). 종전엔 **그 워크스페이스 owner 로 그냥 로그인**돼 대행사·직원이 주인 계정(결제 포함)에 들어갔다. 연동 해제하면 다음부터 새 계정으로 가입. 같이 고친 버그: 해제 뒤에도 옛 구글 계정으로 로그인되던 것(`instagram_user_id` 를 로그인 때 박아서)
- [URGENT_CONVERSION_BACKEND_RESPONSE.md](frontend/URGENT_CONVERSION_BACKEND_RESPONSE.md) — ⭐ **긴급 전환 개선 회신서**(2026-09-12, 마이그 billing0026·auth0007/0008·analytics0008/0009). 병목 진단(방문 5,679 → 가입 525(9.2%) → 프로 체험 26(5.0%)) 후속 8건. **카드 없는 프로 30일** `POST /billing/trial/auto-grant/`(체험 미사용 전원·지급/미지급 둘 다 200·멱등) · `subscription.trial_last_day`(프론트 날짜 역산 제거) · `POST /track/funnel-event/` · `GET/PATCH /auth/me/popup-state/`(⚠️ 요청 경로 `users/me/…` 와 다름) · CAPI `trial_kind` · **인스타 로그인 3종**(기본 OFF·이메일 없음→자리표시) · register `is_new_user`. ⚠️ 자동 지급이 켜지면 44일 쿠폰이 통째로 죽어서 **제휴코드 연장 경로**를 같이 열었다
- [KAKAO_LOGIN_FRONTEND.md](frontend/KAKAO_LOGIN_FRONTEND.md) — ⭐ **카카오 로그인**(2026-09-10, 마이그 auth0006·analytics0007). `POST /api/v1/auth/kakao/` — 응답은 구글과 동일(`user`/`is_new_user`/`tokens`). **JS SDK 금지**(JS 키엔 시크릿이 없어 서버 토큰교환이 깨진다) → REST API 키로 authorize 직접 리다이렉트. 이메일·닉네임 필수 동의(비즈앱). `code` 1회용 = StrictMode 이중실행 주의. **계정 매칭은 회원번호 우선** — 카카오 이메일이 바뀌어도 계정이 안 갈라진다. 오류는 detail/code + envelope **두 포맷 동시**. dev 실계정 종단간 검증 완료
- [RATE_LIMIT_AND_GOOGLE_LOGIN_FRONTEND.md](frontend/RATE_LIMIT_AND_GOOGLE_LOGIN_FRONTEND.md) — 🔴 **429 두 종류 분기 필수**(`RATE_LIMITED` vs `PLAN_LIMIT_EXCEEDED` — 안 하면 paywall 분석 오염) + 구글 로그인 `GOOGLE_EMAIL_UNVERIFIED` 403
- [RETIRE_OLD_API_HOST_REQUEST.md](frontend/RETIRE_OLD_API_HOST_REQUEST.md) — ✅ 회신 받음(1차) · 서버측(Pages Function)이 은퇴한 API 호스트를 호출 중 → 교체 요청
- [RETIRE_OLD_API_HOST_ROUND2.md](frontend/RETIRE_OLD_API_HOST_ROUND2.md) — ✅ **완결(2026-08-12)** · `/media/` 저장 URL R2 이관(61행/101 URL) + 프론트 배포 반영 → 소비자 0건 확인 → DNS 삭제. 결과는 [ops/DNS_RETIRE_API_TURNFLOW.md](ops/DNS_RETIRE_API_TURNFLOW.md)

## ops/ — 운영·인프라·보안

- [TELEGRAM_AI_COLLAB_PLAN.md](ops/TELEGRAM_AI_COLLAB_PLAN.md) — **프론트 협업 자동화 플랜**(2026-09-16, 미실행). 텔레그램 그룹방에 Claude Code Channels(퍼스트파티 telegram 플러그인)를 붙여 프론트가 올린 요청 md 를 **즉시 받아 트리아지**하고 회신서를 파일로 되돌린다. 문서 첨부 수신(`message:document` → `download_attachment`)·그룹(`/telegram:access group add -100…`)·발신자 allowlist 지원. ⚠️ **「AI끼리 알아서 합의」는 텔레그램이 봇↔봇 메시지를 플랫폼에서 막아 불가**하고, 막힌 게 다행이다 — 회신서는 곧 계약이라 검증 없는 AI 간 합의가 그대로 운영 설정이 된다(실제로 잘못 등록한 운영 콜백을 프론트 **사람**이 잡았다). 그래서 **초안까지 자동·발송은 사람**. 운영 배포/플래그/콘솔 변경은 자동화 제외. 세션이 떠 있어야만 수신되고 클라우드 Routines 는 colo 운영 서버에 못 들어가 보조용
- [배포방법.md](ops/배포방법.md) — **prod 배포는 여기부터**(수동 compose 금지 이유 포함)
- [NEXT_ACTIONS_2026-08-04.md](ops/NEXT_ACTIONS_2026-08-04.md) — **현재 우선순위 로드맵**
- [TRIAL_LENGTH_14_VS_30_ANALYSIS_2026-09-10.md](ops/TRIAL_LENGTH_14_VS_30_ANALYSIS_2026-09-10.md) — **무료체험 기간 정책용 실데이터 분석**(카드 없이 14일 vs 30일). 가입→연동→첫 DM 소요시간, 생존분석·신뢰구간 포함. ★연동은 가입 당일 아니면 영영 안 함 · 30일로 늘려 얻는 5명의 실사용은 전체의 0.06%·매출 0원 · 반대로 무상 제공 DM 물량은 2.4배 → **14일 + 조건부 연장** 권고
- [PROD_HARDENING_2026-08-04.md](ops/PROD_HARDENING_2026-08-04.md) — 08-03~04 하드닝 실행 기록 + 사고 3건
- [DNS_RETIRE_API_TURNFLOW.md](ops/DNS_RETIRE_API_TURNFLOW.md) — 옛 API 호스트 DNS 삭제(2026-08-12) + **복구 레시피** · 오리진 IP 직노출 제거
- [SECURITY_AUDIT_2026-06.md](ops/SECURITY_AUDIT_2026-06.md) — 애플리케이션 취약점 감사(미해결 포함)
- [ADMIN_AUTH_HARDENING_PLAN.md](ops/ADMIN_AUTH_HARDENING_PLAN.md) — 🟡 **계획(미구현)** · 어드민 계정 3인 분리 + 어드민 전용 JWT(TOTP 2요소) + Django admin 세션 MFA. 지문은 서버가 검증 불가 → 실선택지는 TOTP/이메일/패스키
- [DR_IMPLEMENTATION_PLAN.md](ops/DR_IMPLEMENTATION_PLAN.md) — 재해복구 설계·결정 로그
- [META_INSIGHTS_APP_REVIEW_PLAN.md](ops/META_INSIGHTS_APP_REVIEW_PLAN.md) — 🟡 **계획(보류)** · `instagram_business_manage_insights` 앱 심사 신청. ⚠️ **Step 0 먼저**: Meta 문서가 IG Login 에서 이 스코프의 존재를 서로 다르게 적고 있어 미확인이면 계획 전체가 무효 · 데모 계정 **팔로워 100+** 필수(미만이면 지표가 빈다)
- [INSTAGRAM_OAUTH_FLOW.md](ops/INSTAGRAM_OAUTH_FLOW.md) · [INSTAGRAM_TEST_GUIDE.md](ops/INSTAGRAM_TEST_GUIDE.md)
- [CLOUDFLARE_TUNNEL_SETUP.md](ops/CLOUDFLARE_TUNNEL_SETUP.md) — dev 공개(`dev-api.turnflow.link`)
- [EMAIL_TEMPLATE_HANDOFF_FOR_CS.md](ops/EMAIL_TEMPLATE_HANDOFF_FOR_CS.md)

관련 런북은 `deploy/` 아래에도 있습니다 — `deploy/SERVER_RUNBOOK.md`, `deploy/dr/gcp/DRILL_RUNBOOK.md`.

## system/ — 백엔드 동작 설명

- [AUTODM_DELIVERY_LIFECYCLE.md](system/AUTODM_DELIVERY_LIFECYCLE.md) (+ `.html`) — 자동 DM 발송 라이프사이클
- [SPAM_FILTER_SYSTEM.md](system/SPAM_FILTER_SYSTEM.md) — 스팸 판정 체계
- [DM_ERROR_POLICY_PLAN.md](system/DM_ERROR_POLICY_PLAN.md) (+ `DM_ERROR_POLICY_MATRIX.html`) — DM 오류 2분류 정책
- [AD_COMMENT_WEBHOOK_EVIDENCE.md](system/AD_COMMENT_WEBHOOK_EVIDENCE.md) — 광고 댓글 웹훅 실측 근거(코드가 참조)
- [DM_MIGRATION_LINK_UNWRAP.md](system/DM_MIGRATION_LINK_UNWRAP.md) — 이전 시 타사 래퍼 링크(매니챗·인포크·소셜비즈·리틀리)를 원본으로 되돌리기. 실측·소급 명령·새 도구 추가법
- [AI_CAMPAIGN_ASSIST_ADOPTION_2026-08.md](system/AI_CAMPAIGN_ASSIST_ADOPTION_2026-08.md) — AI 캠페인 초안(ai-suggest) 실사용 조사(2026-08, prod 실측). 무수정 채택 0%지만 11개 중 4개만 수정·편집 95초 / 수정은 게이트문구(50% 교체)·링크URL(84% 교체) 두 곳에 집중 / 원인은 프롬프트 상투구 지시 + 프론트 맥락 미전달(업종·목표·톤 0%, link_url 16%) / **example.com 자리표시자가 실DM 53건에 발송됨**
- [SERVICE_DIFFERENTIATION.md](system/SERVICE_DIFFERENTIATION.md) — 서비스 차별점(세일즈)

## archive/ — 완료·대체됨

최신 정보가 아닙니다. 이력 참고용으로만 보세요.

- `ADMIN_DM_ERROR_PROPOSAL.md` · `_R11.md` · `_R12.md` — 어드민팀 11·12차 회신(2026-07-31 배포 완료).
  현재 정책은 [system/DM_ERROR_POLICY_PLAN.md](system/DM_ERROR_POLICY_PLAN.md) 를 보세요.
- `SECURITY_AUDIT_2026-08-03_PROD_INFRA.md` — 하드닝 **이전** 진단서.
  실제 조치 결과와 정정은 [ops/PROD_HARDENING_2026-08-04.md](ops/PROD_HARDENING_2026-08-04.md) 에 있습니다.
