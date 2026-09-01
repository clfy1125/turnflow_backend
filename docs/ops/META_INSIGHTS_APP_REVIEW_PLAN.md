# Meta 앱 심사 — `instagram_business_manage_insights` 신청 계획

**작성일**: 2026-08-19
**상태**: 🟡 **보류(실행 전)** — 데모용 IG 계정(팔로워 100+) 확보 대기 중
**앱**: TurnFlow (app ID `1497161828629570`)
**기승인 권한**: `instagram_business_basic` · `instagram_business_manage_messages` · `instagram_business_manage_comments`
 (2026-05-29 제출 → 승인. 원본: `TurnFlow_Meta_App_Review_Submitted_On_2026-05-29.pdf`)
**이번 목표**: `instagram_business_manage_insights` 단독 추가 승인

> 재개할 때는 **§2(특수 요구사항) 부터** 읽을 것. 현재 블로커는 **데모용 IG 계정(팔로워 100+)** 하나다.

---

## 0. 왜 이 권한이 필요한가 (배경)

| 지금 | 승인 후 |
|---|---|
| 인스타 성장 리포트의 **조회수를 Apify 스크레이핑**으로 수집 (게시물당 $0.0027, [`service.py:68`](../../apps/insta_reports/service.py)) | 공식 API 로 대체 — [`collect_apify.py`](../../apps/insta_reports/pipeline/collect_apify.py) **모듈만 교체**(docstring 에 이미 명시) |
| 공개 데이터로 얻을 수 있는 것만 (조회수·좋아요·댓글) | **도달·저장·공유·팔로우 유입·프로필 방문·평균 시청시간 + 팔로워/논팔로워 분해** |
| [`apps/insights/`](../../apps/insights/) 앱 전체가 `INSIGHTS_API_ENABLED=False` 로 잠겨 있음 | 킬스위치 해제 + beat 4개 복원 |

관련 코드 위치는 §7 참조.

---

## 1. 스코프 이름·유효성 — ✅ 확정 (조사 종결)

**`instagram_business_manage_insights` 가 우리 로그인 방식(Instagram Business Login)에서 유효하다.**
근거는 문서가 아니라 **우리 저장소의 이력**이다:

```
7e3e82b7 (2026-05-15)  "instagram_business_manage_insights",       ← REQUIRED_SCOPES 에 활성 추가
6bfd4600 (2026-05-28)  # "instagram_business_manage_insights",     ← 제품 결정으로 주석 처리
                       chore(insights): API 임시 비활성 (kill-switch + OAuth scope 제거)
```

운영 중인 서비스에서 이 스코프가 **2주간 실제 authorize URL 에 포함된 채 돌았다**
([`services.py:162`](../../apps/integrations/services.py) 가 `REQUIRED_SCOPES` 를 그대로 조립).
문자열이 유효하지 않았다면 그 기간 **모든 연동이 invalid scope 로 실패**했을 것이고, 그런 일은 없었다.
주석 처리는 버그 대응이 아니라 **기능 출시 보류라는 제품 결정**이었다.

> ⚠️ **Meta 문서를 근거로 삼지 말 것 — 서로 모순돼 있다.**
> [Insights 가이드](https://developers.facebook.com/docs/instagram-platform/insights/) 와
> [미디어 인사이트 레퍼런스](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights)
> 는 "Instagram Login = `instagram_business_basic` + `instagram_business_manage_insights`" 로 맞게 적었지만,
> [Platform Overview](https://developers.facebook.com/docs/instagram-platform/overview) 와
> [Business Login 문서](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login)
> 는 IG Login 스코프를 **4개만** 나열하고 insights 를 Facebook Login 목록에만 넣어 두었다(**stale**).
> 이 모순 때문에 한 번 잘못된 결론(=FB Login 전환 검토)에 도달한 적이 있으니, 다시 조사하지 말 것.

**남은 미확정은 하나뿐**: 앱 대시보드 → 앱 심사 → 권한 및 기능 목록에 항목이 뜨는지(제출 창구 확인).
이건 §2-① 의 성공 호출을 만들면서 같이 확인하면 된다.

> 참고: 코드에 *"조회수는 Graph 인사이트로는 못 받는다 — 지표 29종 전부 403, 앱 권한 미승인"*
> 실측 기록이 있다([`collect_apify.py:111`](../../apps/insta_reports/pipeline/collect_apify.py)).
> 스코프를 뺀 뒤의 관측이므로 당연한 결과다 — 스코프 유효성의 반증이 아니다.

---

## 2. 이번 권한의 특수 요구사항 (지난번엔 없던 것)

### ① Advanced Access 전에 "성공한 API 호출 이력" 필요

[App Review 문서](https://developers.facebook.com/docs/instagram-platform/app-review/) 요구 5번:
*"Make at least one successful API call for Advanced Access requests"*.
→ **심사 제출 전에 Standard Access 로 인사이트를 실제로 한 번 받아내야 한다.**
이 호출이 성공하는 순간 Step 0 의 답도 자동으로 나온다.

### ② 데모 계정이 팔로워 100 미만이면 화면이 빈다 ⚠️ (현재 블로커)

공식: *"Some metrics are not available on Instagram accounts with fewer than 100 followers"*.

지난 승인의 결정적 근거였던 "실시간 로그 → DM 도착"이 이번엔 없다.
**빈 화면 = 증거 0 → "We are not able to test requested permissions" 반려**
(커뮤니티에서 인사이트 반려의 가장 흔한 문구).

→ **필요 조건: 팔로워 100+ · 릴스 여러 개 · 최근 활동 있는 IG 프로페셔널 계정.**
리드타임이 있으므로 이것부터 확보한다. **← 2026-08-19 현재 여기서 대기 중**

### ③ 90일 보관 한계 — "허용 기간 초과 저장"이 인사이트 특유의 반려 사유

공식: *"User Metrics data is stored for up to 90 days"*.
커뮤니티 반려 사유: *"분석 데이터를 허용 기간을 넘겨 저장하거나 고지 없이 저장하는 경우"*.

→ 신청서에서 **"인사이트 원본은 90일 정책대로 취급 / 그보다 긴 기간의 서술은 우리가 만든
집계·파생 지표"** 를 명시적으로 가른다. 근거로 [`IGMedia.insight_stale_ttl()`](../../apps/insights/models.py)
보관 정책을 쓸 수 있다.

### ④ 개인정보처리방침에 인사이트 수집이 적혀 있어야 한다

반려 사유 상위: *"privacy policy was generic and didn't mention Meta API data"*.
지난 방침에 DM·댓글은 있어도 **인사이트/분석 데이터 항목은 없을 가능성이 높다**.
제출 전 `docs/legal/` 확인 후 없으면 보강.

### ⑤ Data Use Checkup(DUC) 재제출

새 기능 접근 요청 시 DUC 갱신 필요. **비즈니스 인증은 지난 messages 승인 때 통과** → 재확인만.

### ⑥ 기존 승인 3종은 안전

새 권한 신청은 **그 권한만 심사**된다. 단 **신청서에서 기존 권한 설명을 건드리지 말 것** —
건드리면 재심사 대상이 된다.

---

## 3. 지난번과 결정적으로 다른 점 — 영상 전략의 핵심

지난 영상이 통과한 진짜 이유는 **인과의 실시간 목격**이었다.
(댓글 입력 → 좌측 서버 로그 웹훅 도착 → 우측 DM 도착)

**인사이트에는 그 이벤트가 없다.** 리뷰어 눈에는 숫자판일 뿐이고, 숫자판은 하드코딩할 수 있다.
→ **증명 축을 "공개 데이터로는 절대 얻을 수 없는 숫자"로 바꾼다.**

| 화면 중앙에 크게 (이 권한 없이는 불가능) | 배경으로 밀 것 (공개 데이터 = 증거 못 됨) |
|---|---|
| **reach** (도달) · **saved** (저장) | 좋아요 수 |
| **follows** (이 게시물이 만든 팔로우) | 댓글 수 |
| **profile_visits** (프로필 방문) | 조회수 ← 공개로도 보임 |
| **ig_reels_avg_watch_time** (평균 시청시간) | 게시일 · 캡션 |
| **`breakdown=follow_type`** — 팔로워 vs 논팔로워 도달 분리 | |

`breakdown=follow_type`([`services.py:290-294`](../../apps/insights/services.py))은 스크레이핑으로
**원리적으로 불가능한 값** → 가장 강한 증거.

**지난 split-screen 의 인사이트 판 대응물:**
> 왼쪽 = **인스타그램 앱의 네이티브 인사이트 화면**, 오른쪽 = **우리 리포트**.
> **같은 게시물의 도달·저장 숫자가 일치**하는 장면.

이번 영상에서 가장 설득력 있는 30초. 지난번 "서버 로그" 자리에 이것을 넣는다.

---

## 4. 실행 단계

### Step 1 — 특정 계정만 insights 를 요구하는 연동 (프론트 합의)

운영 중인 서버이므로 전역 스코프 변경 금지. 대상 계정(`test123@test.com`)만 요청한다.

- **`REQUIRED_SCOPES` 상수를 함수로 전환.**
  현재 [`services.py:130`](../../apps/integrations/services.py) 는 클래스 상수이고 3곳이 참조
  (authorize URL / connection.scopes 저장 / Swagger 문서). `scopes_for(user)` 로 바꾸고,
  allowlist 는 **env** (`INSTAGRAM_INSIGHTS_SCOPE_EMAILS`) 로 둔다
  → 문제 발생 시 **배포 없이 env 만 비워 원복** 가능해야 한다.
- **⚠️ [`views.py:712`](../../apps/integrations/views.py) 를 반드시 같이 고칠 것.**
  현재 `connection.scopes = REQUIRED_SCOPES` 로 **우리가 요청한 값**을 저장한다.
  그런데 토큰 응답에 **실제 부여된 `permissions` 가 이미 온다**
  ([`services.py:176`](../../apps/integrations/services.py) — 받아놓고 버리는 중).
  안 고치면 *"요청은 했는데 Meta 가 안 준"* 상황에서 DB 는 있다고 말하고
  [`tasks.py:235`](../../apps/insights/tasks.py) 는 믿고 호출했다가 403 → 심사 기간 내내 원인 추적 불가.
- **프론트 합의 항목**: ①해당 계정에만 리포트 메뉴 노출 ②영어 UI 전환이 **리포트 화면까지** 적용
  ③나머지 사용자에겐 무변화.
- 이 단계에서 §2-① 의 **성공한 API 호출**을 확보한다.

### Step 2 — 리포트 영어판 + "리뷰어는 15분을 기다리지 않는다"

App Review 문서가 *"Use English as the app UI language"* 를 명시 권고.
지난 신청서에 *"you can change the language to English (EN)"* 로 통과했으므로 언어 전환은 이미 존재
→ **리포트 산출물 HTML 만 영어화**하면 된다.

**진짜 문제: 리포트 1건이 13~18분.** 반려 문구 *"We are not able to test"* 가 나오는 지점.

- 테스트 계정에 **완성된 리포트를 미리 1건 이상 생성**해 두고, 리뷰어 지시서 첫 줄에 명기.
- 새로 생성하는 경로도 안내하되 **선택 사항**으로 표시.
- 테스트 계정의 **프로 플랜 권한 + IG 계정당 월1회 쿼터**를 열어둘 것.
  플랜 게이트에 막히면 그대로 반려 (지난 신청서의 "payment or membership required" 칸 참고).

### Step 3 — 스크린캐스트 (목표 3~4분, 영어 자막, 마우스 하이라이트 유지)

| 시각 | 장면 | 목적 |
|---|---|---|
| 0:00 | 앱 소개 1문장 + 영어로 언어 전환 | 리뷰어 오리엔테이션 |
| 0:15 | `test123@test.com` 로그인 | 접근성 증명 |
| 0:30 | "Connect Instagram" → **OAuth 동의 화면에서 insights 항목 확대·정지** | ★ 권한 요청 순간 |
| 0:50 | 연동 완료 (@username · 프로필 사진) | basic 재확인 |
| 1:05 | "Generate Report" → 진행률 시작 → 자막 *"takes ~15 min; skipping to a completed report"* | 정직한 편집 |
| 1:20 | **완성 리포트 오픈** — 도달 → 저장 → 팔로우 유입 → 평균 시청시간 순으로 천천히 확대 | ★ 권한의 산출물 |
| 2:00 | **팔로워 vs 논팔로워 도달 분해** | ★ 공개 데이터로 불가능한 값 |
| 2:20 | **split-screen: 인스타 앱 네이티브 인사이트 ↔ 우리 리포트, 같은 게시물 숫자 일치** | ★★ 최강 증거 |
| 3:00 | 사업자에게 주는 가치 1문장 (어느 콘텐츠를 더 만들지 판단) | 사용 사례 정당화 |

**주의**: 서버 로그를 띄우려면 웹훅 로그가 아니라 **인사이트 API 요청/응답 로그**여야 한다
(`GET /{media-id}/insights?metric=reach,saved,…` → JSON). 도움은 되지만 split-screen 이 우선.

### Step 4 — 신청서 문안

지난번 통과한 **3단 구조를 그대로 재사용**한다
(`How our app uses` → `Why this is necessary and its value to the user` → `Where to see this in the screencast` + 타임코드).

#### 초안 (영문)

> **How our app uses this permission:** We use `instagram_business_manage_insights` to read
> performance metrics for the connected Instagram Professional account's own media
> (`GET /{ig-media-id}/insights`) and for the account itself (`GET /{ig-user-id}/insights` with
> `breakdown=follow_type`). Metrics read: reach, saved, shares, follows, profile visits, views, and
> for Reels the average watch time. We read only the connected account's own media. We do not read
> insights for any other account.
>
> **Why this is necessary and its value to the user:** Our users are small business owners who run
> DM automation campaigns on their posts. They need to know *which* posts actually reach new
> (non-follower) audiences and drive follows — information that is not visible from public post
> data. Our Growth Report turns these metrics into a plain-language report that tells the owner
> which content to make more of. Without this permission the report can only show public numbers
> (likes, comments), which do not answer that question. Insights data is retained in line with
> Meta's 90-day User Metrics policy; longer-range statements in the report are our own derived
> aggregates, not stored raw insights.
>
> **Where to see this in the screencast:** [00:30] The user grants this permission in the Instagram
> OAuth dialog. [01:20] The generated report displays reach, saved, follows and profile visits per
> post. [02:00] The report shows reach split by follower vs. non-follower (`breakdown=follow_type`).
> [02:20] Split-screen: the same post's reach and saves shown in the native Instagram Insights
> screen match the numbers in our report.

#### 리뷰어 지시서

지난번 12단계 형식을 유지하되 **맨 앞에 두 줄 추가**:

> - A completed Growth Report already exists on the test account — open it directly (Step N).
>   Generating a new one takes about 15 minutes.
> - The test account already has the required plan entitlement; no payment is needed.

---

## 5. 리스크

| 리스크 | 심각도 | 대응 |
|---|---|---|
| IG Login 에 insights 스코프가 아예 없음 | **치명** — 계획 전체 무효 | **§1 Step 0** (대시보드 확인 30분) |
| 데모 계정 팔로워 <100 → 빈 화면 | 높음 | 100+ 계정 확보 (**현재 대기 항목**) |
| 리뷰어가 15분 대기 못 함 → "not able to test" | 높음 | 완성 리포트 사전 생성 + 지시서 명기 |
| 테스트 계정 플랜/쿼터 게이트 | 중간 | 프로 권한 + 월1회 쿼터 해제 |
| 개인정보처리방침에 인사이트 항목 없음 | 중간 | 제출 전 `docs/legal/` 확인·보강 |
| 스코프 저장 버그로 원인 추적 불가 | 중간 | [`views.py:712`](../../apps/integrations/views.py) 를 실제 `permissions` 저장으로 |
| 인사이트 켜면 Meta 쿼터를 DM·댓글과 나눠 씀 | 중간 | 심사 기간엔 데모 계정 1개만 (`INSIGHTS_API_ENABLED=False` 유지) |

---

## 6. 승인 이후 할 일 (별도 작업)

1. [`services.py:134`](../../apps/integrations/services.py) 주석 해제 (전역 스코프 반영)
2. `INSIGHTS_API_ENABLED=True` + [`base.py:698-712`](../../config/settings/base.py) beat 4개 복원
3. **기존 연동 계정 전원 재연동 유도** — 스코프는 연동 시점 고정 저장이므로 UX 작업 필요
4. [`collect_apify.py`](../../apps/insta_reports/pipeline/collect_apify.py) 교체
   - ⚠️ **수치 불연속 주의**: Apify `videoPlayCount` ≠ Graph `views`
     ([`normalize.py:5`](../../apps/insta_reports/pipeline/normalize.py) — "1.5~11배 차이")
   - ⚠️ **호출량**: `/{media-id}/insights` 는 미디어당 1호출·batch 미지원
     ([`services.py:6`](../../apps/insights/services.py)) → 200건 = 205호출.
     Meta 쿼터는 **앱 단위로 DM·댓글 수집과 공유**하므로 라이브 경로를 굶길 수 있다.
5. `instagram_business_content_publish` — 게시물 예약/자동 발행 로드맵이 생기면 그때 별도 신청.
   현재 IG Login 스코프 중 남는 건 이것 하나뿐.

---

## 7. 관련 코드 위치

| 위치 | 내용 |
|---|---|
| [`apps/integrations/services.py:130-137`](../../apps/integrations/services.py) | `REQUIRED_SCOPES` — **스코프 단일 소스**. insights 줄이 주석 처리 |
| [`apps/integrations/services.py:162`](../../apps/integrations/services.py) | authorize URL 의 `scope` 조립 |
| [`apps/integrations/services.py:176`](../../apps/integrations/services.py) | 토큰 응답의 `permissions` (현재 버려짐) |
| [`apps/integrations/views.py:179-186`](../../apps/integrations/views.py) | Swagger scope 목록 — 자동 동기화 안 됨, 수동 갱신 |
| [`apps/integrations/views.py:712`](../../apps/integrations/views.py) | `connection.scopes` 저장 지점 (Step 1 수정 대상) |
| [`apps/insights/`](../../apps/insights/) | 인사이트 앱 전체 — 구현 완료·킬스위치로 잠김 |
| [`apps/insights/services.py:45-79`](../../apps/insights/services.py) | 미디어 타입별 metric 카탈로그 |
| [`apps/insights/services.py:290-294`](../../apps/insights/services.py) | 계정 단위 `breakdown=follow_type` |
| [`apps/insights/tasks.py:235`](../../apps/insights/tasks.py) | 런타임 스코프 보유 판정 |
| [`config/settings/base.py:930-934`](../../config/settings/base.py) | `INSIGHTS_API_ENABLED` 킬스위치 |
| [`config/settings/base.py:698-712`](../../config/settings/base.py) | insights beat 4개 (주석 처리) |
| [`apps/insta_reports/pipeline/collect_apify.py`](../../apps/insta_reports/pipeline/collect_apify.py) | 대체 대상 모듈 |

---

## 8. 참고 링크

- [App Review - Instagram Platform](https://developers.facebook.com/docs/instagram-platform/app-review/)
- [Insights - Instagram Platform](https://developers.facebook.com/docs/instagram-platform/insights/)
- [Instagram Platform Overview](https://developers.facebook.com/docs/instagram-platform/overview)
- [Business Login for Instagram](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login)
- [Media Insights Reference](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights)
- [Access Levels (Standard vs Advanced)](https://developers.facebook.com/docs/graph-api/overview/access-levels/)
- [Developer Community — instagram_manage_insights 반려 사례](https://developers.facebook.com/community/threads/1418343268305761)
- [Meta Advanced Access: Which Permissions Need App Review](https://singhamandeep.com/what-is-meta-advanced-access/)
- [Why Meta App Review Keeps Disapproving Your App](https://www.postmoo.re/blogs/meta-app-review-disapproved-how-to-get-approved)
