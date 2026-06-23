# Turnflow AI 페이지 생성 플레이북 (저비용 모델 대응판)

> 목적: 백엔드의 AI 페이지 생성(현재 DeepSeek)이 **수작업급 퀄리티**(다채로운 CSS, 블록의 적절한 활용, 서로 다른 느낌)를 내도록, 모델에게 그대로 떠먹여 줄 수 있는 **출력 계약 + 지식베이스 + 복붙 CSS 모듈 + 프롬프트 템플릿**.
>
> 저비용 모델은 "알아서 예쁘게"가 안 됩니다. 대신 **① 정확한 출력 스키마 ② 진짜로 먹히는 CSS 셀렉터(훅) ③ 골라 쓰는 미감 레시피 ④ 복붙 CSS 스니펫 ⑤ 자가검증 체크리스트**를 주면 똑같이 나옵니다. 이 문서는 그 5가지를 전부 담았습니다.
>
> 사용법: §10 프롬프트 템플릿을 system 으로 넣고, §1~§9를 지식으로 붙여서 모델에 전달 → 모델이 `{ design_settings, custom_css, blocks[] }` JSON 을 뱉음 → 백엔드가 그대로 페이지/블록 생성 API에 POST.

---

## 0. TL;DR — 왜 저급 모델로도 되는가

핵심은 **"창작을 시키지 말고 조립을 시킨다"** 입니다.

1. **미감을 직접 고르게 한다** (§6 레시피 10종 중 1개 선택). 색/폰트/카드스타일을 모델이 발명하지 않음.
2. **CSS는 검증된 모듈을 복붙**시킨다 (§7). 변수만 치환.
3. **셀렉터는 정해진 훅만** 쓰게 한다 (§3). 모델이 클래스를 지어내면 100% 안 먹힘.
4. **블록 순서를 고정**하고 그 순서대로 `nth-child` CSS를 쓰게 한다 (§5).
5. **자가검증 체크리스트**(§8)로 스스로 점검 후 출력.

---

## 1. 출력 계약 (Output Contract)

AI는 정확히 아래 3가지를 담은 JSON 하나를 출력한다. 그 외 텍스트 금지.

```json
{
  "design_settings": { ...§4 },
  "custom_css": "≈ 1500~4000자의 CSS 문자열 (§3 훅 + §7 모듈)",
  "blocks": [ { "type": "...", "data": { ... } }, ... ]   // §2, 위에서부터 화면 순서대로
}
```

- `blocks` 배열의 **순서 = 화면 표시 순서**. 백엔드가 `order`를 0,1,2…로 부여.
- 첫 블록은 거의 항상 `profile`.
- 폴더(`folder`)는 특수 처리 → §2.8.

> 백엔드 매핑: 페이지의 `data.design_settings`, `data.custom_css`에 저장. 블록은 각각 `POST /api/v1/pages/multipages/{id}/blocks/` (`{type, order, is_enabled:true, data}`). 커스텀 CSS는 `PATCH .../{id}/css/` 로도 반영.

---

## 2. 블록 카탈로그 (정확한 필드)

**중요 규칙:** 거의 모든 블록은 `type: "single_link"` 로 저장하고, **실제 종류는 `data._type`** 로 구분한다. **프로필만 `type: "profile"`**. (렌더러가 `data._type`을 보고 컴포넌트를 고름.)
또 하나: 모든 블록의 최상위 `data.url`은 **반드시 http/https**. 링크가 아닌 블록은 `"https://text"` 같은 더미라도 넣어야 함(아니면 400). `tel:`/`mailto:`는 금지 → 전화·이메일은 **social 블록**이 자동 생성.

| _type | 용도 | 필수/주요 data 필드 |
|---|---|---|
| `profile`(type=profile) | 헤더(이름·소개·아바타·커버) | `profile_layout`('center'\|'cover'\|'cover_bg'\|'left'\|'right'), `headline`, `subline`, `avatar_url`, `cover_image_url`, `font_size`('sm'\|'md'\|'lg') |
| `text` | 인사말·상태·인용·아이브로우·구분 | `content`, `headline`(선택), `text_layout`('plain'\|'default'\|'toggle'), `text_align`, `text_size`('xs'~'lg') |
| `single_link` | 버튼·CTA·대표 상품 카드 | `label`, `url`, `layout`('large'\|'medium'\|'small'), `description`, `thumbnail_url`, `price`, `original_price`, `badge`(콤마구분), `custom_bg_color`, `custom_text_color`, `text_align` |
| `group_link` | **상품/메뉴 그리드** | `label`, `group_layout`('grid-2'\|'grid-3'\|'list'\|'carousel-1'), `display_mode`('all'\|'collapse'), `links:[{id,title,url,thumbnail_url,description,price,original_price,badge,is_enabled:true}]` |
| `gallery` | 사진 갤러리 | `images:[url...]`, `gallery_layout`('carousel'\|'thumbnail'\|'free'\|'single'), `keep_ratio`, `auto_slide` |
| `schedule` | **달력 + 자동 D-day** | `label`, `schedule_layout`('calendar'\|'list'), `schedule_items:[{id,title,start_date:'YYYY-MM-DD',start_hour:0-23,end_date,end_hour}]` |
| `map` | 구글맵 임베드 + 길찾기 | `address`, `map_name` |
| `social` | 연락/SNS 아이콘 행 | `socials_visible:['phone','email','kakao_talk','homepage','instagram','linkedin'...]`, 각 키 값(`phone`,`email`,`instagram` 등). phone/email은 자동 tel:/mailto: |
| `folder` | 접이식 묶음(과정·FAQ) | §2.8 (2단계 생성) |
| `spacer` | 여백 | `height`(px) |

### 2.1 profile
- `center`: 아바타+이름 세로 중앙. 가장 안전.
- `cover`/`cover_bg`: `cover_image_url` 배너 사용. **함정**: cover 계열은 아바타원과 이름이 한 래퍼에 묶임 → 아바타를 CSS로 숨기면 이름이 커버 위로 겹침. → **아바타를 숨기지 말고 `avatar_url`에 실제 이미지를 넣어** 자리를 채울 것.

### 2.2 text — 3가지 레이아웃을 적극 활용 (다양성의 원천)
- `plain`: 테두리 없음. **인사말, 섹션 아이브로우("✦ MENU"), 마무리 문구**.
- `default`: 카드(박스). **상태배너, 인용/후기, 안내 박스**.
- `toggle`: 아코디언. **폴더의 자식**(과정 STEP, FAQ 항목)으로 주로 사용.

### 2.3 single_link layout
- `large`: 이미지 위 + 제목/설명/가격 아래(대표 상품·피처드).
- `medium`: 제목/설명 좌 + 16x16 썸네일 우(일반 버튼·링크).
- 색 버튼(CTA)은 `custom_bg_color`+`custom_text_color`로 채우거나 CSS로 처리.

### 2.4 group_link — 가격/할인 자동
- `price`,`original_price`를 **숫자 문자열**('39900')로 주면 **할인%가 자동 계산**되어 빨강으로 표시되고 원가에 취소선.
- `badge`는 콤마구분 → 태그칩. 예: `"인기,마감임박"`.
- 썸네일 없으면 회색 박스. **반드시 thumbnail_url 채울 것.**
- `grid-2`(2열, 기본), `grid-3`(3열, 촘촘), `carousel-1`(가로 스와이프).

### 2.5 gallery
- `carousel`(가로 스와이프), `thumbnail`(격자), `free`(메이슨리), `single`.
- 동영상 임베드는 불안정 → 갤러리/이미지 위주.

### 2.6 schedule — 달력은 초대장/예약/마감에 강력
- `schedule_layout:'calendar'` → 달력 + 행사일에 점 + 오늘 강조 + **클라이언트가 실시간 계산하는 D-day 뱃지**.
- 강조색은 `design.buttonColor` 사용.
- **함정 ①**: 공개뷰 달력은 **오늘 달**로 열림. 그 달의 일정만 점·리스트 표시 → **행사일을 현재 달(가까운 미래)** 로 둘 것. 먼 달이면 사용자가 직접 넘겨야 함.
- **함정 ②**: 행사일을 **말일(30/31)로 두지 말 것**. 앱이 `new Date('YYYY-MM-30')`을 UTC로 파싱해 KST에선 월말을 넘겨 리스트에서 누락("이 달 일정 없음"). **28~29일** 권장.

### 2.7 map
- `address`만 있으면 구글맵 임베드 + "길찾기" 버튼 자동. 온라인 서비스(커미션 등)엔 불필요하면 생략.

### 2.8 folder (2단계 + 전역 order 주의) ★
폴더는 자식을 먼저 만들고 그 id를 폴더가 참조한다. AI 출력 JSON에서는 **중첩 구조로 표현**하고, 백엔드가 아래 절차로 생성:

```
1) 폴더의 children(각각 text/toggle 등)을 먼저 POST → id 수집
2) 폴더 블록 POST: data = { url:'https://folder', _type:'folder', label, folder_icon,
     child_block_ids:[수집한 id들], folder_display_mode:'toggle', is_collapsed_default:true|false }
```

- 자식은 `text`(`text_layout:'toggle'`)가 일반적. **profile/notice는 자식 불가.**
- **★ 다중 폴더 함정(매우 중요)**: 블록 `order`는 **page당 UNIQUE**. 폴더마다 자식 order를 600부터 다시 시작하면 둘째 폴더 자식이 첫 폴더 자식(600,601…)과 충돌해 **500 IntegrityError**. → **자식 order는 모든 폴더를 통틀어 단조 증가**시킬 것(전역 카운터).
- 폴더 child_block_ids를 나중에 PATCH로 고칠 땐 **data 객체 전체를 다시 구성**해 보낼 것. 개별 블록 GET이 빈 응답이라, `{...GET결과.data, child_block_ids}` 식으로 하면 `_type:'folder'`가 유실되어 폴더로 인식 안 됨(자식이 본문 맨 아래에 따로 렌더됨).

AI 출력 예(폴더):
```json
{ "type":"single_link", "data":{ "_type":"folder", "label":"🎨 작업 과정", "folder_icon":"palette",
  "is_collapsed_default":false,
  "children":[
    {"type":"single_link","data":{"_type":"text","text_layout":"toggle","headline":"STEP 1 · 신청","content":"..."}},
    {"type":"single_link","data":{"_type":"text","text_layout":"toggle","headline":"STEP 2 · 입금","content":"..."}}
  ]}}
```
(백엔드가 children를 풀어 위 절차대로 생성)

---

## 3. 공개 페이지 DOM 훅 — CSS를 "먹히게" 하는 핵심 ★★★

> 이 앱은 **Tailwind 유틸리티** 기반이라 시맨틱 클래스가 거의 없다. 모델이 `.commission-card` 같은 클래스를 지어내면 **무조건 안 먹힌다.** 아래 **실제 훅만** 사용한다.

| 대상 | 셀렉터 | 비고 |
|---|---|---|
| 배경 캔버스 | `.page-container` | 전체 배경. 스크롤 컨테이너. |
| **(함정) 주입 style** | — | custom_css가 `<style>`로 `.page-container`의 **첫 자식**이 됨 → `> div:first-child`는 프로필을 못 잡음. **`:first-of-type` 사용.** |
| 프로필 영역 | `.page-container > div:first-of-type` | 그 안에 `h2`(헤드라인), `p`(서브라인), `.rounded-full`(아바타), `img`(아바타/커버) |
| 블록 리스트 | `.mt-6.space-y-3` | 최상위 블록들의 컨테이너 |
| 개별 블록(공통 래퍼) | `.mt-6.space-y-3 > .block-link` | **모든** 블록이 이걸로 감싸짐. `:nth-child(N)` 안정적(생성 순서 = N) |
| 카드형 앵커(버튼/링크/그룹/단일카드) | `.block-link a.w-full` | large=직계 `a.flex-col`, medium/CTA=중첩 `div.relative > a.w-full.p-4` 모두 포함. **소셜 아이콘은 .w-full 아님 → 자동 제외** |
| 이미지 있는 카드(갤러리/썸네일) | `.block-link:has(img)` | |
| 박스 블록(schedule/map/text-default/folder) | `.mt-6.space-y-3 > .block-link:nth-child(N) > *` | 이들은 `div`로 렌더 → 직계 자식 `> *`을 스타일 |
| 폴더 토글 | `.block-link:has(.folder-chevron)` | |
| plain 텍스트 | (박스 없음) | 배경/테두리 주지 말 것(아이브로우·인사말로) |

**`nth-child` 규칙(가장 중요한 레버):**
- `.mt-6.space-y-3`의 자식은 **최상위 블록만** 셈(폴더 자식은 폴더 안으로 들어가 제외됨).
- 즉 `nth-child(N)` = "내가 N번째로 넣은 블록".
- 따라서 **블록 순서를 고정**하고(§5), 그 번호로 CSS를 작성한다.

**지원되는 CSS(크로미엄):** `:has()`, `backdrop-filter`, `@keyframes`, `@import`(Google Fonts), SVG `data:` URI, `counter`, `background-clip:text`, 다중 `radial/linear-gradient`. → 화려한 효과 전부 가능.

**텍스트 색 자동:** 앱이 `design.backgroundColor`가 어두우면 글자를 흰색, 밝으면 검정으로 자동 결정. **그래서 custom_css로 배경을 그라데이션으로 덮더라도, `backgroundColor`는 베이스 톤(밝/어둠)에 맞게 설정**해야 글자색이 맞는다.

---

## 4. design_settings 레버

```json
{
  "backgroundColor": "#0D0A26",       // 베이스 톤(밝/어둠) — 자동 글자색 결정. custom_css가 시각적 배경은 덮어씀
  "frameBackgroundColor": "#0D0A26",  // 보통 backgroundColor와 동일
  "blockBgColor": "#1C163E",          // 카드 기본색. ''(빈값)=반투명 글래스(밝은 배경에선 너무 옅음). 보통 솔리드 지정 후 CSS로 frosted 처리
  "buttonColor": "#C9A24B",           // schedule 오늘강조/ D-day뱃지/ map 길찾기/ group 강조색
  "buttonShape": "rounded",           // 'rounded' | 'pill' | 'square'
  "buttonApplyMode": "partial",
  "fontFamily": "Nanum Myeongjo",     // 앱 기본 폰트(아래 목록). 디스플레이 폰트는 custom_css @import로 덮어씀
  "logoStyle": "hidden",
  "backgroundImage": "",              // 사진 배경은 카드 뒤로 지저분 → 비울 것. 단색/그라데만
  "backgroundImageEnabled": false,
  "shareButtonVisible": true,
  "subscribeButtonVisible": false
}
```

- 앱 기본 `fontFamily` 후보: `Pretendard`(산세리프 기본), `Nanum Myeongjo`(명조/세리프), `IBM Plex Sans KR`. → 한글 본문용 베이스로 지정하고, **영문 디스플레이/장식 폰트는 §7-A의 `@import`로 헤딩/아이브로우에만** 적용.
- **사진을 배경(backgroundImage)으로 깔지 말 것.** 단색 또는 CSS 그라데이션만.

---

## 5. 페이지 골격 (블록 구성 레시피)

블록을 **다양하게, 용도에 맞게** 쓰는 게 핵심. 아래 표준 골격을 기본으로 하고, 분야에 따라 가감/재배치한다. **순서를 고정**하고 그 번호로 CSS를 단다.

표준 골격(예: 커미션/예약형):
```
0) profile                      (헤더)
1) text/plain                   인사말·소개 한두 줄
2) text/default                 상태 배너 ("🟢 OPEN · 잔여 N")  ← 펄싱 점(§7-K)
3) schedule/calendar            마감/예약 D-day 달력
4) text/plain                   섹션 아이브로우 "✦ MENU"
5) group_link/grid-2            메뉴/상품 그리드(가격·할인·뱃지)
6) text/plain                   아이브로우 "✦ PORTFOLIO"
7) gallery/carousel             포트폴리오/샘플
8) text/plain                   아이브로우 "✦ HOW IT WORKS"
9) folder/toggle                작업 과정 (STEP 1~4)
10) folder/toggle               FAQ
11) text/default                후기 인용 카드
12) single_link                 주 CTA(신청/주문) ← 색 채움
13) single_link                 보조(카톡/문의)
14) social                      SNS/연락
15) text/plain                  마무리 문구
```

**분야별 변주(구조도 다르게 = 더 다양해 보임):**
- 디자인/포트폴리오형 → **갤러리를 맨 앞**으로, schedule 생략, "원칙" statement 카드 추가.
- 음악형 → "데모 듣기" featured 링크 추가.
- 쇼핑/제휴형 → 카테고리별 group_link 여러 개 + 쿠폰 카드.
- 초대장형 → schedule(D-day) + map(오시는 길) + folder(계좌/안내) 강조.

**아이브로우(섹션 라벨)는 plain 텍스트**로 만들고 §7-G로 꾸미면 페이지에 리듬이 생긴다(중요).

---

## 6. 미감 레시피 10종 — "다양한 느낌"의 핵심 ★

> 모델은 **요청 분야/브랜드에 어울리는 레시피 1개를 고른다**(또는 사용자가 지정). 색/폰트/카드를 발명하지 말고 이 표를 그대로 쓴다. 같은 골격이라도 레시피가 다르면 완전히 다른 페이지가 된다.

| # | 이름 | 베이스 bg / 글자 | 액센트 | @import 폰트 | 카드 | 시그니처 모듈 |
|---|---|---|---|---|---|---|
| R1 | 드리미 파스텔 글래스 | `#F6F3FF` 라벤더 / 어두운 글자 | 바이올렛 `#7C5CFF` + 피치/민트 | `Caveat`+`Gowun Dodum` | 프로스티드 글래스(§7-C) 라운드 22px | 메시 그라데(§7-B) + 도트, 손글씨 아이브로우 |
| R2 | 미드나잇 코스믹 | `#0D0A26` 인디고 / 흰 글자 | 골드 `#C9A24B` + 바이올렛 | `Marcellus`+`Gowun Batang` | 다크 글래스 + 골드 보더 | 스타필드(§7-H), ☾ 아이브로우 |
| R3 | 스위스 브루탈리스트 | `#EFEDE6` 본 / 검정 글자 | 라임 `#C6F432` + 블랙 | `Archivo`(800/900) | 흰 카드 + 1.5px 블랙 + 하드섀도(§7-F) | 라임 블록 넘버 아이브로우(01—) |
| R4 | 선셋 그라데+웨이브폼 | `#190E22` 플럼 / 흰 글자 | 코랄 `#FF5C7A`→앰버 | `Sora` | 다크 글래스 + 코랄 보더 | 이퀄라이저 바(§7-J) |
| R5 | 어시 크래프트 | `#EBE3D4` 크라프트 / 갈색 글자 | 테라코타 `#A8552A` + 세이지 `#7E8C5A` | `Nanum Pen Script`+`Gowun Dodum` | 크림 카드 + 비대칭 라운드(26px 18px) | 유기적 블롭 아바타(§7-I), 그레인 |
| R6 | 로맨틱 에디토리얼 | `#FBF3EC` 크림 / 갈색 글자 | 골드 `#C8A24B` + 블러시 `#DA8FA4` | `Parisienne`+`Cormorant Garamond` | 흰 카드 + 골드 헤어라인 | 꽃잎 낙하(§7-L), 명조 |
| R7 | 네온 Y2K | `#07010E` 블랙 / 흰 글자 | 마젠타 `#FF2E97` + 시안 `#00E5FF` | `Orbitron`+`Share Tech Mono` | 다크 + 네온 글로우 보더 | 글로우 텍스트(§7-E), 스캔라인, 플리커 |
| R8 | 볼드 애슬레틱 | `#EFEFE7` 본 / 검정 | 볼트 `#16E07A` + 블랙 | `Anton`+`Archivo` | 다크 카드 + 하드섀도 | 마퀴 티커(§7-D), 초대형 대문자 |
| R9 | 아르데코 럭스 | `#0E0C0A` 차콜 / 아이보리 | 샴페인골드 `#C9A24B` | `Cinzel`+`Cormorant Garamond` | 다크 + 골드 헤어라인 + inset | 핀스트라이프 bg, 플랭킹 룰 아이브로우(§7-G) |
| R10 | 클린 테크 | `#0B0F1A` 슬레이트 / 흰 글자 | 일렉트릭 블루 `#3B82F6`/시안 | `Space Grotesk`+`JetBrains Mono` | 다크 슬레이트 + 블루 보더 | 그리드 라인 bg, 모노 [브래킷] 아이브로우 |
| R11 | 데일리 커머스(시끌) | `#FFF7ED` 앰버 / 갈색 | 레드 `#E11D1D` + 앰버 | `Black Han Sans` | 흰 카드 + 컬러 보더 | 특가 마퀴, 대형 할인% |
| R12 | K-뷰티 글로시 | `#FFF1F3` 로제 / 로즈 글자 | 로즈 `#E8788E` | `Playfair Display` | 흰 카드 + 핑크 소프트섀도 | 상단 글로스, 세리프 아이브로우 |

> 모든 @import 폰트는 Google Fonts에서 `@import url('https://fonts.googleapis.com/css2?family=...&display=swap');`로 로드 검증됨. 한글은 Pretendard/Nanum 계열로 폴백되므로 영문 폰트는 헤딩/아이브로우/영문라벨에만 적용.

---

## 7. CSS 모듈 스니펫 (복붙용)

> CSS 맨 위에 항상 `@import`(폰트) → 그다음 모듈들. `.page-container > *{position:relative;z-index:1;}`을 넣어 배경 pseudo가 콘텐츠를 가리지 않게 한다.

### A. 폰트 + 캔버스 기본
```css
@import url('https://fonts.googleapis.com/css2?family=Gowun+Dodum&family=Caveat:wght@700&display=swap');
.page-container{ color:#4B4163 !important; }      /* 베이스 글자색(밝은 배경 기준) */
.page-container > *{ position:relative; z-index:1; }
```

### B. 메시 그라데이션 배경 (R1)
```css
.page-container{
  background:
    radial-gradient(40% 26% at 12% 6%, rgba(167,139,250,.55), transparent 70%),
    radial-gradient(38% 24% at 90% 10%, rgba(255,170,140,.48), transparent 70%),
    radial-gradient(44% 30% at 84% 84%, rgba(110,231,196,.45), transparent 72%),
    linear-gradient(180deg,#F7F3FF 0%,#F1F8FF 100%) !important;
}
```

### C. 프로스티드 글래스 카드 (밝은 배경)
```css
.block-link a.w-full,
.mt-6.space-y-3 > .block-link:nth-child(2) > *,   /* 박스블록(상태/스케줄/폴더 등)은 nth-child로 추가 */
.mt-6.space-y-3 > .block-link:nth-child(9) > *{
  background:rgba(255,255,255,.74)!important; border:1px solid rgba(255,255,255,.95)!important;
  border-radius:22px!important; -webkit-backdrop-filter:blur(14px) saturate(150%); backdrop-filter:blur(14px) saturate(150%);
  box-shadow:0 18px 40px -24px rgba(124,92,255,.5)!important;
}
.block-link a.w-full{ overflow:hidden; transition:transform .3s, box-shadow .3s; }
.block-link a.w-full:hover{ transform:translateY(-4px); }
.block-link a.w-full img{ transition:transform .5s; }
.block-link a.w-full:hover img{ transform:scale(1.05); }
```
> 다크 글래스(R2/R4/R10)는 배경을 `rgba(28,22,62,.72)`처럼 어둡게, 보더를 액센트색 반투명으로.

### D. 마퀴 티커 (R8/R11) — plain 텍스트 블록 1개에 적용
```css
.mt-6.space-y-3 > .block-link:nth-child(1){ overflow:hidden!important; background:#15161A!important; border-radius:6px!important; padding:9px 0!important; }
.mt-6.space-y-3 > .block-link:nth-child(1) > *{ white-space:nowrap!important; display:inline-block!important; color:#16E07A!important; font-family:'Anton',sans-serif!important; font-size:16px!important; animation:tf-marq 12s linear infinite; }
@keyframes tf-marq{ from{transform:translateX(10%)} to{transform:translateX(-65%)} }
```
> 콘텐츠는 문구를 길게(반복) 넣어 끊김 없이.

### E. 네온 글로우 텍스트 (R7) — 헤드라인
```css
.page-container > div:first-of-type h2{
  color:#fff!important; text-shadow:0 0 6px #FF2E97,0 0 16px #FF2E97,0 0 30px rgba(255,46,151,.7),0 0 2px #fff;
}
```

### F. 하드 오프셋 섀도 (R3/R8) — 스위스/브루탈
```css
.block-link a.w-full{ background:#fff!important; border:1.5px solid #15140F!important; border-radius:3px!important; box-shadow:6px 6px 0 rgba(21,20,15,.12)!important; transition:transform .18s, box-shadow .18s; }
.block-link a.w-full:hover{ transform:translate(-2px,-2px); box-shadow:9px 9px 0 #C6F432!important; }
```

### G. 섹션 아이브로우 (plain 텍스트) — 양옆 라인/장식
```css
/* 4,6,8번이 아이브로우라고 가정 */
.mt-6.space-y-3 > .block-link:nth-child(4) > *,
.mt-6.space-y-3 > .block-link:nth-child(6) > *,
.mt-6.space-y-3 > .block-link:nth-child(8) > *{
  font-family:'Caveat',cursive!important; color:#7C5CFF!important; font-size:27px!important; font-weight:700!important;
  display:flex!important; align-items:center; justify-content:center; gap:12px;
}
.mt-6.space-y-3 > .block-link:nth-child(4) > *::before,.mt-6.space-y-3 > .block-link:nth-child(4) > *::after{ content:"✦"; color:#FFB39A; font-size:14px; }
/* (6,8도 동일 ::before/::after 반복) */
```
> 모노 넘버형(R3): `content:""` + 라임 블록 `::before{width:20px;height:11px;background:#C6F432;border:1.5px solid #000;}` + 텍스트 "01 — SERVICES".

### H. 스타필드 (R2) — 배경에 반짝이는 별
```css
.page-container::after{ content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.55;
  background-image:
    radial-gradient(1.2px 1.2px at 20px 30px,#fff,transparent),
    radial-gradient(1px 1px at 120px 80px,rgba(255,255,255,.8),transparent),
    radial-gradient(1.4px 1.4px at 200px 160px,#E3C36B,transparent),
    radial-gradient(1px 1px at 80px 220px,#fff,transparent);
  background-size:320px 280px; animation:tf-tw 5s ease-in-out infinite alternate; }
@keyframes tf-tw{ from{opacity:.35} to{opacity:.7} }
```

### I. 그레인 텍스처 + 유기적 블롭 아바타 (R5)
```css
.page-container::after{ content:""; position:fixed; inset:0; z-index:0; pointer-events:none; opacity:.06; mix-blend-mode:multiply;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='110' height='110'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E"); }
.page-container > div:first-of-type .rounded-full{ border-radius:52% 48% 56% 44% / 50% 52% 48% 50%!important; border:4px solid #FBF6EC!important; box-shadow:0 0 0 2px #C9B79A!important; }
```

### J. 애니메이션 이퀄라이저 바 (R4) — 상태 카드 우상단
```css
.mt-6.space-y-3 > .block-link:nth-child(2){ position:relative; }
.mt-6.space-y-3 > .block-link:nth-child(2)::after{ content:""; position:absolute; right:18px; top:18px; width:50px; height:20px; z-index:4;
  background:linear-gradient(#FF5C7A,#FF5C7A) 0 100%/4px 40% no-repeat, linear-gradient(#FF8A5B,#FF8A5B) 9px 100%/4px 80% no-repeat, linear-gradient(#FFC15B,#FFC15B) 18px 100%/4px 55% no-repeat, linear-gradient(#FF5C7A,#FF5C7A) 27px 100%/4px 100% no-repeat, linear-gradient(#B36BFF,#B36BFF) 36px 100%/4px 60% no-repeat, linear-gradient(#FF8A5B,#FF8A5B) 45px 100%/4px 85% no-repeat;
  animation:tf-eq .85s ease-in-out infinite alternate; }
@keyframes tf-eq{ from{background-size:4px 40%,4px 80%,4px 55%,4px 100%,4px 60%,4px 85%;} to{background-size:4px 95%,4px 30%,4px 100%,4px 45%,4px 90%,4px 35%;} }
```

### K. 펄싱 상태 점 (상태 배너) — "OPEN" 등
```css
.mt-6.space-y-3 > .block-link:nth-child(2){ position:relative; }
.mt-6.space-y-3 > .block-link:nth-child(2)::before{ content:""; position:absolute; top:16px; left:18px; width:9px; height:9px; border-radius:50%; background:#22C55E; box-shadow:0 0 0 0 rgba(34,197,94,.6); animation:tf-pulse 2s infinite; z-index:3; }
@keyframes tf-pulse{ 0%{box-shadow:0 0 0 0 rgba(34,197,94,.5)} 70%{box-shadow:0 0 0 10px rgba(34,197,94,0)} 100%{box-shadow:0 0 0 0 rgba(34,197,94,0)} }
```

### L. 꽃잎/꽃가루 낙하 (R6 꽃잎 / 파스텔 꽃가루)
```css
.page-container::before{ content:""; position:fixed; inset:-12% 0 0 0; z-index:0; pointer-events:none; opacity:.7;
  background-image:
    radial-gradient(circle,#FF9EC4 0 4px,transparent 5px),
    radial-gradient(circle,#8FC9FF 0 3px,transparent 4px),
    radial-gradient(circle,#FFD27A 0 3.5px,transparent 4.5px);
  background-size:150px 230px,200px 280px,175px 320px; animation:tf-fall 13s linear infinite; }
@keyframes tf-fall{ to{ background-position:35px 230px,70px 280px,165px 320px; } }
```

### M. 그라데이션 텍스트 헤드라인 (R1 등)
```css
.page-container > div:first-of-type h2{
  background:linear-gradient(95deg,#fff,#cdc4ff 46%,#8ee9ff)!important;
  -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; color:transparent;
}
```

### N. 색 채운 CTA (그라데이션 + 글로우)
```css
.mt-6.space-y-3 > .block-link:nth-child(12) a.w-full{
  background:linear-gradient(100deg,#7C5CFF,#B36BFF 50%,#FF7EB3)!important; border:none!important; color:#fff!important; font-weight:700!important;
  box-shadow:0 18px 38px -12px rgba(124,92,255,.65)!important;
}
.mt-6.space-y-3 > .block-link:nth-child(12) a.w-full:hover{ transform:translateY(-4px) scale(1.01); }
```

---

## 8. 절대 규칙 & 함정 (자가검증 체크리스트)

출력 전에 모델이 스스로 점검:

- [ ] 블록 `data.url`이 모두 http/https인가? (비링크 블록도 `https://text` 등 더미)
- [ ] 전화/이메일을 `single_link`에 tel:/mailto:로 넣지 않았는가? → **social 블록** 사용
- [ ] custom_css의 셀렉터가 **§3의 실제 훅만** 쓰는가? (지어낸 클래스 금지)
- [ ] 프로필 셀렉터는 `:first-child`가 아니라 **`:first-of-type`**인가?
- [ ] `nth-child(N)` 번호가 실제 블록 순서와 **정확히** 일치하는가?
- [ ] `design.backgroundColor`가 베이스 톤(밝/어둠)과 맞는가? (자동 글자색)
- [ ] `backgroundImage`는 비웠는가? (사진 배경 금지)
- [ ] group_link 항목에 **thumbnail_url**이 다 있는가? price는 **숫자 문자열**인가?
- [ ] schedule 행사일이 **현재 달 + 28~29일 이하**인가? (말일·먼 달 금지)
- [ ] 폴더가 2개 이상이면 자식 order가 **전역 단조 증가**인가? (백엔드 처리지만 구조상 children 명시)
- [ ] CSS에 `@import`가 맨 위, `.page-container > *{position:relative;z-index:1;}` 포함?
- [ ] plain 텍스트(아이브로우/인사말)에 카드 배경을 주지 않았는가?
- [ ] 미감 레시피 1개를 골라 **색·폰트·카드·시그니처 모듈**을 일관되게 적용했는가?
- [ ] 블록을 **최소 7종 이상**(profile/text plain/text default/group_link/gallery/schedule/folder/single_link/social) 다양하게 썼는가?

---

## 9. 완전한 예시 (압축본)

> 분야: 일러스트 커미션 / 레시피: R1(드리미 파스텔 글래스). 실제로는 더 길게.

```json
{
  "design_settings": {
    "backgroundColor":"#F6F3FF","frameBackgroundColor":"#F6F3FF","blockBgColor":"#FFFFFF",
    "buttonColor":"#7C5CFF","buttonShape":"rounded","fontFamily":"Pretendard",
    "logoStyle":"hidden","backgroundImage":"","backgroundImageEnabled":false
  },
  "custom_css": "@import url('https://fonts.googleapis.com/css2?family=Caveat:wght@700&family=Gowun+Dodum&display=swap');\n.page-container{background:radial-gradient(40% 26% at 12% 6%,rgba(167,139,250,.55),transparent 70%),radial-gradient(44% 30% at 84% 84%,rgba(110,231,196,.45),transparent 72%),linear-gradient(180deg,#F7F3FF,#F1F8FF)!important;color:#4B4163!important;}\n.page-container>*{position:relative;z-index:1;}\n.page-container>div:first-of-type h2{font-family:'Gowun Dodum',sans-serif!important;color:#6B4FD6!important;}\n.block-link a.w-full,.mt-6.space-y-3>.block-link:nth-child(2)>*,.mt-6.space-y-3>.block-link:nth-child(9)>*{background:rgba(255,255,255,.74)!important;border:1px solid #fff!important;border-radius:22px!important;backdrop-filter:blur(14px);box-shadow:0 18px 40px -24px rgba(124,92,255,.5)!important;}\n.mt-6.space-y-3>.block-link:nth-child(4)>*{font-family:'Caveat',cursive!important;color:#7C5CFF!important;font-size:27px!important;display:flex!important;align-items:center;justify-content:center;gap:12px;}\n.mt-6.space-y-3>.block-link:nth-child(4)>*::before,.mt-6.space-y-3>.block-link:nth-child(4)>*::after{content:'✦';color:#FFB39A;}\n.mt-6.space-y-3>.block-link:nth-child(2)::before{content:'';position:absolute;top:16px;left:18px;width:9px;height:9px;border-radius:50%;background:#22C55E;box-shadow:0 0 0 0 rgba(34,197,94,.6);animation:tf-pulse 2s infinite;}\n@keyframes tf-pulse{70%{box-shadow:0 0 0 10px rgba(34,197,94,0)}}\n.mt-6.space-y-3>.block-link:nth-child(7) a.w-full{background:linear-gradient(100deg,#7C5CFF,#FF7EB3)!important;border:none!important;color:#fff!important;}",
  "blocks": [
    {"type":"profile","data":{"profile_layout":"center","headline":"하루 일러스트 커미션 ☆","subline":"캐릭터·수채화 일러스트레이터 HARU","avatar_url":"<media url>","font_size":"lg"}},
    {"type":"single_link","data":{"_type":"text","text_layout":"plain","text_align":"center","content":"따뜻하고 몽글한 그림체로 그려드려요 ☁️","url":"https://text"}},
    {"type":"single_link","data":{"_type":"text","text_layout":"default","text_align":"center","headline":"🟢 지금 커미션 OPEN","content":"6월 회차 잔여 3/10","url":"https://text"}},
    {"type":"single_link","data":{"_type":"schedule","label":"신청 마감까지","schedule_layout":"calendar","schedule_items":[{"id":"1","title":"6월 마감","start_date":"2026-06-28","start_hour":23,"end_date":"2026-06-28","end_hour":23}],"url":"https://schedule"}},
    {"type":"single_link","data":{"_type":"text","text_layout":"plain","text_align":"center","content":"Commission Menu","url":"https://text"}},
    {"type":"single_link","data":{"_type":"group_link","group_layout":"grid-2","links":[
      {"id":"1","title":"아이콘","url":"https://example.com/i","thumbnail_url":"<url>","price":"15000","badge":"입문"},
      {"id":"2","title":"반신","url":"https://example.com/h","thumbnail_url":"<url>","price":"30000","badge":"인기"}
    ],"url":"https://group"}},
    {"type":"single_link","data":{"_type":"single_link","layout":"medium","text_align":"center","label":"✏️ 커미션 신청하기","url":"https://example.com/apply"}},
    {"type":"single_link","data":{"_type":"social","is_social":true,"instagram":"https://instagram.com/haru","socials_visible":["instagram"],"url":"https://social"}}
  ]
}
```

---

## 10. 모델 프롬프트 템플릿 (백엔드가 DeepSeek에 넣을 것)

**system** (이 문서 §1~§9를 압축해 넣거나 통째로):
```
당신은 Turnflow 링크인바이오 페이지를 생성하는 디자이너다. 아래 플레이북을 반드시 따른다.
출력은 오직 하나의 JSON 객체({design_settings, custom_css, blocks})이며 그 외 텍스트·설명·마크다운 금지.
[여기에 §1 출력계약, §2 블록필드, §3 DOM훅, §4 design, §6 레시피표, §7 CSS모듈, §8 체크리스트를 붙인다]
```

**user** (요청):
```
분야: {예: 타로 상담 / 로고 디자인 / 핸드메이드 공예 ...}
브랜드/이름: {예: 랄라}
톤/원하는 느낌: {예: 신비로운 / 미니멀 / 따뜻한}  (없으면 분야에 맞는 레시피 자동 선택)
포함할 정보: {가격, 메뉴, 연락처, 일정 등 사용자 입력}
이미지: {사용 가능한 media URL 목록 — 없으면 빈 썸네일 자리 비워두기}
```

**모델 내부 사고 순서(지시):**
```
1. 분야에 맞는 §6 레시피 1개 선택(또는 톤 지정 반영).
2. §5 골격에서 분야에 맞게 블록 순서를 확정(7종 이상). 순서를 메모.
3. 각 블록의 content/price/링크를 사용자 정보로 채움. 정보가 없으면 그 분야의 자연스러운 기본값.
4. 확정된 블록 순서에 맞춰 custom_css 작성: @import → 캔버스(§7-B등) → 글래스/카드(§7-C/F) → 아이브로우 nth-child(§7-G) → CTA nth-child(§7-N) → 시그니처 모듈(레시피의 H/I/J/K/L 중 1~2개).
5. §8 체크리스트 전 항목 확인.
6. JSON만 출력.
```

> **요약 지시 한 줄(가장 중요):** "색·폰트·카드·CSS는 발명하지 말고 §6 레시피와 §7 모듈을 골라 조립한다. 셀렉터는 §3 훅만 쓴다. nth-child 번호는 블록 순서와 일치시킨다."

---

## 부록. 검증/디버깅 메모 (백엔드용)
- 새 페이지 공개뷰 `/@{slug}` 첫 로드가 "페이지 없음"으로 뜨면 캐시 레이스 → 잠시 후/캐시버스트 재요청하면 정상(`GET /api/v1/pages/@{slug}/`는 200).
- 미디어 업로드 multipart 필드명은 **`file`**.
- 블록 POST가 가끔 502(Cloudflare) → 5xx 백오프 재시도 권장.
- `:has()`/`backdrop-filter` 미지원 구형 브라우저는 그냥 효과만 빠지고 레이아웃은 유지(우아한 폴백). 단색 폴백을 먼저 깔면 안전.
- 공개뷰는 모바일 폭 기준으로 디자인(폭 ~430px). 데스크탑은 가운데 정렬 폰 프레임.
