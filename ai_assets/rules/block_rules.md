# Turnflow 블록 데이터 스펙

> AI가 블록 JSON 데이터를 기반으로 링크인바이오 웹페이지를 렌더링할 때 참조하는 문서.

---

## 블록 구조

```typescript
interface Block {
  id: number;
  type: string;            // DB 블록 타입. "profile" | "single_link" | "contact" (3개 중 하나)
  order: number;           // 표시 순서 (1부터)
  is_enabled: boolean;     // false면 비표시
  data: BlockData;         // 타입별 데이터. single_link인 경우 data._type으로 서브타입 분기 (아래 참조)
  schedule_enabled?: boolean;
  publish_at?: string;     // ISO 날짜. 이 시각 이후 표시
  hide_at?: string;        // ISO 날짜. 이 시각 이후 숨김
}
```

---

## ⚠️ type vs data._type — 반드시 이해해야 하는 구조

DB에 저장되는 `Block.type`은 **3가지**만 존재합니다 (모델: `BlockType`):

| DB `type` 값 | 설명 |
|---|---|
| `profile` | 프로필 블록. `data`에 프로필 필드가 직접 들어감 (`_type` 없음) |
| `single_link` | **대부분의 블록이 이 타입.** 실제 서브타입은 `data._type`으로 결정 |
| `contact` | 연락처 블록 |

### data._type — 실제 렌더링을 결정하는 서브타입

`type: "single_link"`인 블록은 `data` 내부의 `_type` 필드로 **어떤 UI를 렌더링할지** 분기합니다.

```typescript
// type: "single_link" 블록의 data 구조
interface SingleLinkBlockData {
  _type: string;    // 아래 14개 서브타입 중 하나 — 이 값이 실제 렌더링 분기점
  label: string;    // 블록 라벨 (프론트엔드 편집 UI용)
  layout: string;   // "small" | "medium" | "large"
  url: string;      // 블록 URL (서브타입에 따라 의미가 다름)
  // ... 서브타입별 추가 필드
}
```

| `data._type` 값 | 설명 | 문서 내 섹션 |
|---|---|---|
| `single_link` | 단일 URL 링크 버튼 | §2 |
| `group_link` | 여러 링크 그룹 (폴더형) | §3 |
| `social` | SNS 아이콘 모음 | §4 |
| `video` | 동영상 임베드 | §5 |
| `text` | 텍스트 블록 | §6 |
| `gallery` | 이미지 갤러리 | §7 |
| `spacer` | 구분선/여백 | §8 |
| `map` | 지도 | §9 |
| `notice` | 공지 배너/팝업 | §10 |
| `inquiry` | 고객문의 폼 | §11 |
| `customer` | 고객정보 수집 폼 | §12 |
| `search` | 블록 내 검색 | §13 |
| `folder` | 하위 블록 컨테이너 | §14 |
| `schedule` | 일정 캘린더 | §15 |

### 예시: 실제 블록 JSON

```jsonc
// ✅ profile 블록 — type이 직접 "profile"이며, data에 _type 없음
{
  "id": 1,
  "type": "profile",
  "order": 1,
  "data": {
    "headline": "BLACK NOISE",
    "subline": "서울 기반 얼터너티브 록 밴드",
    "avatar_url": "https://...",
    "profile_layout": "cover"
  }
}

// ✅ 공지 블록 — type은 "single_link"이고, data._type이 "notice"
{
  "id": 2,
  "type": "single_link",
  "order": 2,
  "data": {
    "_type": "notice",
    "label": "공지",
    "title": "2026 TOUR 티켓 오픈",
    "content": "서울 · 부산 공연 예매 시작",
    "notice_layout": "banner",
    "link_url": "https://example.com/tour"
  }
}

// ✅ 갤러리 블록 — type은 "single_link"이고, data._type이 "gallery"
{
  "id": 3,
  "type": "single_link",
  "order": 3,
  "data": {
    "_type": "gallery",
    "label": "갤러리",
    "images": ["https://..."],
    "gallery_layout": "carousel",
    "auto_slide": true
  }
}
```

> **핵심:** 블록의 `type`이 `"single_link"`이면 반드시 `data._type`을 확인해야 실제 블록 종류를 알 수 있습니다.
> `type`이 `"profile"`이면 `data._type` 없이 바로 프로필 데이터입니다.

---

### 공통 스타일 필드 (모든 블록의 data 내부)

```typescript
// data 안에 선택적으로 포함 가능
custom_bg_color?: string;      // 블록 배경색
custom_border_color?: string;  // 블록 테두리색
custom_text_color?: string;    // 블록 텍스트색
custom_button_color?: string;  // 블록 버튼색
```

---

## 블록 순서 규칙

| 블록 (`data._type`) | 위치 |
|------|------|
| `profile` (type 자체가 profile) | 항상 최상단 (1번) |
| `notice` (`_type: "notice"`) | profile 바로 아래 (2번) |
| 나머지 | 자유 정렬 |

---

## 블록 서브타입 상세 스펙 (총 15종: profile 1개 + single_link 서브타입 14개)

### 1. profile — `type: "profile"` (DB 타입이 직접 profile)

> ⚠️ 유일하게 `data._type`이 없는 블록. `type` 자체가 `"profile"`입니다.

프로필 소개. 이름, 소개문구, 프로필 사진 표시.

```json
{
  "headline": "이름",
  "subline": "소개 문구",
  "avatar_url": "프로필 이미지 URL",
  "cover_image_url": "커버 이미지 URL (cover/cover_bg 레이아웃에서만)",
  "profile_layout": "center | left | right | cover | cover_bg",
  "font_size": "sm | md | lg",
  "business_proposal_enabled": false,
  "country_code": "",
  "phone": "",
  "whatsapp": false
}
```

**레이아웃별 렌더링:**
- `center` — 아바타 중앙, 텍스트 중앙 정렬
- `left` — 아바타 좌측, 텍스트 우측
- `right` — 아바타 우측, 텍스트 좌측
- `cover` — 커버 이미지 위에 아바타+텍스트 오버레이
- `cover_bg` — 커버 이미지 배경, 아바타+텍스트 분리

---

### 2. single_link — `type: "single_link"` + `data._type: "single_link"`

> DB `type`과 `data._type`이 동일하게 `"single_link"`인 경우. 순수한 단일 URL 링크 버튼.

단일 URL 링크 버튼. 클릭 시 해당 URL로 이동.

```json
{
  "url": "https://example.com",
  "label": "버튼 텍스트",
  "description": "설명",
  "layout": "small | medium | large",
  "thumbnail_url": "썸네일 이미지 URL",
  "text_align": "left | center | right",
  "badge": "태그1,태그2",
  "price": "12000",
  "original_price": "15000"
}
```

**레이아웃별 렌더링:**
- `small` — 한 줄 버튼. 좌측 썸네일(40×40), 라벨, 우측 화살표
- `medium` — 좌측 큰 썸네일(80×80), 우측 라벨+설명+가격+태그
- `large` — 상단 와이드 썸네일, 하단 라벨+설명+가격+태그

**태그(badge):** 쉼표 구분 문자열. 각 태그를 pill 형태로 표시. small은 최대 2개, medium/large는 최대 3개, 초과분은 `+N`으로 표시. **각 태그는 짧게(6자 이내, 예: `BEST`·`40%`·`무료`·`신상`·`D-3`)** — 긴 문구(`선착순 100명`)는 pill 안에서 잘리니 금지.

**가격:** `price` 표시, `original_price`가 있으면 취소선으로 할인 전 가격 표시.

> ⚠️ **URL 규칙 (위반 시 저장 거부 또는 렌더 누락):**
> - `single_link` 의 `url` 은 **반드시 비어 있지 않은 `https://` URL**. **빈 url 단일링크는 렌더가 통째로 생략된다.** 진짜를 모르면 **그럴듯한 실제형 URL**(예: `https://instagram.com/브랜드핸들`, `https://pf.kakao.com/_xxx`)을 넣어라 — 사용자가 나중에 교체한다.
> - `thumbnail_url`·`image_url` 등 **보조** URL 은 모르면 **빈 문자열 `""` 로 두거나 생략**(빈 값 허용, 이미지가 안 뜰 뿐 렌더는 됨).
> - 공통: `#`·`javascript:`·스킴 없는 값 금지(서버 검증에서 거부 → 페이지 저장 실패). 이미지 자리는 실제 URL 대신 `{{image:영문키워드}}` / `{{user_image:N}}`.

---

### 3. group_link — `type: "single_link"` + `data._type: "group_link"`

여러 링크를 폴더처럼 그룹화.

```json
{
  "label": "그룹 제목",
  "description": "설명",
  "links": [
    {
      "id": "uuid",
      "url": "https://...",
      "title": "링크 제목",
      "thumbnail_url": "썸네일 URL",
      "price": "",
      "original_price": "",
      "badge": "태그",
      "is_enabled": true
    }
  ],
  "group_layout": "list | grid-2 | grid-3 | carousel-1 | carousel-2",
  "display_mode": "all | collapse",
  "text_align": "left | center | right"
}
```

**레이아웃:**
- `list` — 세로 리스트
- `grid-2` — 2열 그리드
- `grid-3` — 3열 그리드
- `carousel-1` — 캐러셀 (아이템 75% 너비)
- `carousel-2` — 캐러셀 (아이템 48% 너비)

> ⚠️ **썸네일은 사실상 필수:** `list` 레이아웃도 항목 좌측에 48×48 썸네일을 렌더한다 — `grid`/`carousel` 만 이미지 레이아웃이 아니다. **레이아웃과 무관하게 모든 `links` 항목에 `thumbnail_url`**(실제 이미지나 `{{image:영문키워드}}`)을 채워라. 썸네일 없는 그룹링크는 "사진이 빠진 페이지"로 보인다(사용자 핵심 불만). **유일한 예외 2가지**: ① 후기 리스트(제목 "이름 ★★★★★") ② 텍스트 가격표(아이콘/반신/전신 등) — 이 둘만 썸네일 생략하고 `list` 사용. (썸네일 없는 grid/carousel 은 자동으로 list 로 강등된다.)

**display_mode:** `collapse`이면 처음 2개만 표시 + "더보기" 버튼.

---

### 4. social — `type: "single_link"` + `data._type: "social"`

SNS 및 연락처 아이콘 모음. 값이 있는 플랫폼만 아이콘 표시.

```json
{
  "instagram": "@username 또는 URL",
  "youtube": "채널 URL",
  "twitter": "@username 또는 URL",
  "tiktok": "@username 또는 URL",
  "phone": "010-1234-5678",
  "email": "user@example.com",
  "custom_icon_color": "#hex (선택)"
}
```

**렌더링:** 가로 중앙 정렬 아이콘 행. 각 아이콘은 원형 40×40 영역.

**클릭 동작:**
- `instagram`, `youtube`, `twitter`, `tiktok` → 새 탭에서 URL 열기
- `phone` → `tel:` 링크 (숫자만 추출)
- `email` → `mailto:` 링크

**호버 브랜드 색상:**
| 플랫폼 | 배경 |
|--------|------|
| instagram | 그래디언트 (#f09433→#bc1888) |
| youtube | #FF0000 |
| twitter | #000000 |
| tiktok | 그래디언트 (#00f2ea→#ff0050) |
| phone | #22c55e |
| email | #3b82f6 |

---

### 5. video — `type: "single_link"` + `data._type: "video"`

동영상 임베드. YouTube, TikTok, Vimeo, Dailymotion 지원.

```json
{
  "video_urls": ["https://youtube.com/watch?v=..."],
  "video_layout": "default | carousel | grid-2",
  "autoplay": true
}
```

**레이아웃:**
- `default` — 세로 스택
- `carousel` — 가로 드래그 스크롤
- `grid-2` — 2열 그리드

> ⚠️ **AI 생성 시 video URL 은 사용자 컨셉에 주어진 실제 영상 URL 만** 넣어라. 임의로 만든 youtube URL 은 "재생할 수 없음" 깨진 임베드가 되어 백엔드가 블록째 제거한다.

---

### 6. text — `type: "single_link"` + `data._type: "text"`

텍스트 블록. 대표문구(headline) + 상세문구(content).

```json
{
  "headline": "대표 문구",
  "content": "상세 내용 (최대 5000자)",
  "text_layout": "plain | default | toggle",
  "text_align": "left | center | right",
  "text_size": "sm | md | lg",
  "custom_sub_text_color": "#hex (상세문구 색상, 선택)"
}
```

**레이아웃:**
- `plain` — 테두리/배경 없이 텍스트만 표시
- `default` — 카드 형태 (배경 + 테두리)
- `toggle` — 접기/펼치기. headline 클릭 시 content 토글

> 💡 **디자인 권장**: 텍스트 블록은 대부분 `plain`(테두리·배경 없음)이 더 깔끔하고 예쁘다. 카드로 강조해야 할 분명한 이유가 없으면 `text_layout: "plain"` 을 기본으로 써라. 또한 **한 블록에 문장이 많아질수록 안 예쁘다 — 짧고 간결하게**(헤드라인 한 줄, 본문 1~3문장) 쓰고, 긴 내용은 압축하거나 여러 블록으로 나눠라.
>
> ⚠️ **마크다운/HTML 미지원**: `content` 는 `whitespace-pre-wrap` 으로 **순수 텍스트만** 렌더된다. `**볼드**`·`<b>`·`#제목` 은 그 문자 그대로 노출되니 절대 쓰지 마라. 구조는 **이모지(줄 맨 앞 1개) + 줄바꿈(\n) + 빈 줄 문단 구분**으로 만든다. 예: `"⏰ 평일 15,000원/시간\n🌙 심야 12,000원/시간\n\n🅿️ 주차 2시간 무료"`.
>
> ⚠️ **중요 정보는 `toggle` 로 숨기지 마라**: 요금·쿠폰코드·일시·계좌는 `plain` 으로 바로 보이게. `toggle` 은 이용안내·주의사항·FAQ·환불규정 같은 길어도 되는 보조 정보 전용. 같은 성격의 안내(이용안내/주차안내)는 같은 포맷으로 나란히 배치하라.

---

### 7. gallery — `type: "single_link"` + `data._type: "gallery"`

이미지 갤러리. 최대 10장.

```json
{
  "images": ["url1", "url2", ...],
  "gallery_layout": "single | carousel | list | thumbnail | free",
  "gallery_url": "클릭 시 이동 URL (선택)",
  "auto_slide": false,
  "keep_ratio": false
}
```

**레이아웃:**
- `single` — 1장씩 좌우 스와이프
- `carousel` — 가로 드래그 스크롤
- `list` — 세로 나열
- `thumbnail` — 작은 그리드 썸네일
- `free` — 자유 배치

> 💡 **디자인 권장**: 갤러리는 대부분 `keep_ratio: false`(비율 유지 끔 — 이미지를 칸에 꽉 채워 crop)가 더 예쁘게 정렬된다. 특별한 이유가 없으면 `keep_ratio: false` 를 기본으로 써라.

---

### 8. spacer — `type: "single_link"` + `data._type: "spacer"`

구분선 + 여백.

```json
{
  "divider_style": "none | dashed | solid | wave | zigzag",
  "divider_width": 1.5,
  "divider_color": "#hex",
  "spacing": 24
}
```

- `spacing`: 0~100px 여백
- `divider_width`: 1~5 (0.5 단위)
- `none`이면 선 없이 여백만

---

### 9. map — `type: "single_link"` + `data._type: "map"`

지도 블록. 주소 기반 지도 표시.

```json
{
  "address": "서울시 강남구 ...",
  "map_name": "장소 이름 (선택)"
}
```

렌더링: OpenStreetMap 기반 지도 임베드.

---

### 10. notice — `type: "single_link"` + `data._type: "notice"`

상단 공지 배너 또는 팝업.

```json
{
  "title": "공지 제목",
  "content": "공지 내용",
  "notice_layout": "banner | popup",
  "link_url": "클릭 시 이동 URL",
  "image_url": "공지 이미지 (popup에서만)"
}
```

**레이아웃:**
- `banner` — 상단 마퀴(흐르는 텍스트) + 메가폰 아이콘. 닫기(X) 버튼. `link_url`이 있으면 클릭 시 이동.
- `popup` — 하단 시트 모달. `image_url` 이미지 + `title` 텍스트 + 확인/링크 버튼.

---

### 11. inquiry — `type: "single_link"` + `data._type: "inquiry"`

고객문의 접수 폼.

```json
{
  "inquiry_title": "문의 제목",
  "options": ["옵션1", "옵션2"],
  "button_text": "문의하기",
  "email": "수신 이메일",
  "collect_email": false,
  "collect_phone": false
}
```

`options`는 문자열 배열 또는 `{ title: string, form_template?: string }` 객체 배열.

**렌더링:** 옵션을 라디오 버튼으로 표시. 선택 후 버튼 클릭 → 모달 폼 (이름*, 전화*, 이메일(선택), 제목*, 내용) + 동의 체크박스.

---

### 12. customer — `type: "single_link"` + `data._type: "customer"`

고객정보 수집 폼.

```json
{
  "customer_headline": "제목 (최대 24자)",
  "customer_description": "설명 (최대 1000자)",
  "collect_email": false,
  "collect_phone": false,
  "collect_name": false,
  "button_text": "제출하기",
  "custom_input_bg_color": "#hex (입력 필드 배경색, 선택)"
}
```

**렌더링:** 활성화된 수집 항목(이름/이메일/전화)만 입력 필드로 표시 + 제출 버튼.

---

### 13. search — `type: "single_link"` + `data._type: "search"`

페이지 내 블록 검색.

```json
{
  "search_placeholder": "검색어를 입력하세요"
}
```

검색 실행 시 같은 페이지의 블록 label/description을 필터링.

---

### 14. folder — `type: "single_link"` + `data._type: "folder"`

다른 블록들을 하위에 포함하는 컨테이너. 토글(접기/펼치기) 또는 팝업 모드.

```json
{
  "label": "폴더 제목",
  "child_block_ids": [123, 456, 789],
  "folder_icon": "folder",
  "folder_icon_color": "#hex (아이콘 색상, 선택)",
  "is_collapsed_default": true,
  "folder_display_mode": "toggle | popup",
  "text_align": "left | center | right",
  "folder_toggle_bg": "#hex (토글 모드 배경색, 선택)",
  "folder_popup_bg": "#hex (팝업 배경색, 선택)",
  "folder_popup_text": "#hex (팝업 텍스트색, 선택)",
  "folder_popup_accent": "#hex (팝업 강조색, 선택)"
}
```

- `child_block_ids`: 이 폴더에 속한 블록 ID 배열 (해당 블록들은 최상위에서 숨겨지고 폴더 안에 렌더링)
- `folder_icon`: Material Symbols 아이콘 이름
- `is_collapsed_default`: true면 닫힌 상태로 시작

> 💡 **새 페이지 생성에서 folder 쓰는 법**: 하위로 넣을 블록들에 **임시 정수 `id`** (예: 101, 102)를 부여하고, folder 블록의 `child_block_ids: [101, 102]` 로 참조하라 — 백엔드가 저장 시 실제 ID로 재매핑한다. **'분야별 보기'·'모아보기' 같은 폴더형 라벨을 쓸 거면 실제 folder(또는 group_link) 블록을 그 자리에 써라** — 라벨만 폴더처럼 달고 일반 링크를 나열하면 안 된다.

**표시 모드:**
- `toggle` — 폴더 헤더 클릭 시 하위 블록 인라인 펼침/접힘. `folder_toggle_bg`로 배경색 지정.
- `popup` — 폴더 클릭 시 모달 팝업. 하위 블록을 6개씩 페이징. `folder_popup_bg`/`folder_popup_text`/`folder_popup_accent`로 팝업 스타일 지정.

---

### 15. schedule — `type: "single_link"` + `data._type: "schedule"`

일정 블록. 캘린더 또는 리스트로 일정 표시.

```json
{
  "label": "일정 제목",
  "schedule_items": [
    {
      "id": "uuid",
      "title": "일정명",
      "start_date": "2025-07-01",
      "start_hour": 10,
      "end_date": "2025-07-01",
      "end_hour": 18,
      "link_url": "관련 URL (선택)"
    }
  ],
  "schedule_layout": "calendar | list"
}
```

**레이아웃:**
- `calendar` — 월별 캘린더 그리드 + 하단 일정 리스트. 일정 있는 날짜에 ctaColor 점 표시.
- `list` — 일정 리스트만 표시

**D-day 뱃지:** 진행 중인 일정에 `D-day` 또는 `D-N` 뱃지 표시 (ctaColor 사용).

> ⚠️ **AI 생성 시에는 `schedule_layout: "list"` 만 써라.** calendar 는 **현재 달**로 열리기 때문에 다음 달 이후의 일정은 사용자가 달을 넘기기 전까지 안 보인다(빈 캘린더처럼 보임). list 는 모든 일정을 D-day 뱃지와 함께 바로 보여준다. 날짜(`start_date`/`end_date`)는 컨셉에 주어진 실제 날짜만 — 지난 날짜는 'End' 뱃지가 떠 보기 싫으니 넣지 마라.

---

## 페이지 디자인 설정

블록 외에 페이지 전체에 적용되는 디자인 설정:

```typescript
interface DesignSettings {
  backgroundColor: string;       // 페이지 전체 배경색
  frameBackgroundColor: string;  // 프레임/베젤 배경색 — 보통 backgroundColor 와 동일하게 (아래 설명)
  backgroundImage?: string;      // 배경 이미지 URL
  blockBgColor: string;          // 카드/블록 기본 배경색 (빈 문자열이면 흰 카드)
  buttonColor: string;           // 버튼색
  buttonShape: 'rounded' | 'pill' | 'square';
  buttonApplyMode?: 'partial' | 'full';
  fontFamily: string;            // 아래 "폰트(fontFamily) 정책"의 5개 값만 허용
  topMenuStyle: string;          // 상단 메뉴 스타일 (예: "icons")
  logoStyle?: string;            // 예: "hidden"
  customLogoImage?: string;
  // 일부 컨셉 한정 보조: ctaColor(강조색), buttonTextColor 등 — 위 키가 1순위.
}
```

> ⚠️ **키 이름 주의**: 위 키 이름을 **정확히 그대로** 써라. `bgColor`/`textColor`/`bgImage` 같은 옛 이름은 렌더되지 않는다.

### frameBackgroundColor — 프레임/베젤 배경
페이지가 모바일 프레임 안에 렌더될 때 프레임(베젤) 영역의 배경색. **`backgroundColor` 와 같은 값으로 채우면** 배경과 프레임이 하나로 이어져 통일감이 생긴다. 비워두면 베젤이 기본색으로 남아 배경과 따로 놀아 어색하다. → 색 컨셉을 잡을 땐 `backgroundColor` 와 `frameBackgroundColor` 를 **둘 다** 채워라.

### 히어로 패턴 — 컨셉에 맞게 (cover_bg 남발 금지)
profile 레이아웃은 컨셉에 따라 **다양하게** 고른다 — 모든 페이지를 cover_bg 로 찍어내면 단조롭다.
- **cover_bg + cover_image_url**: 대표 비주얼이 강한 타입(공간/제품/작품/사진/랜딩/초대장/카페)에 풀블리드 배경 히어로로. `cover_image_url` 에 대표 이미지를 반드시 넣는다(비우면 빈 회색 박스).
- **center / left 아바타**: 사람·개인 브랜드가 주인공인 타입(인플루언서/디지털 명함/제휴 추천)에. `avatar_url` 을 반드시 채운다.
- ⚠️ **profile 의 cover_image_url·avatar_url 은 채울 거면 채우고, 못 채울 거면 그 레이아웃을 쓰지 마라.** 빈 히어로/빈 아바타는 깨진 화면이다. (이미지 자리는 `{{image:영문키워드}}` 또는 `{{user_image:N}}`.)
- gallery `images` 와 grid/carousel group_link 의 `thumbnail_url` 도 **절대 빈 채로 두지 마라** — 빈 배열 갤러리, 빈 썸네일 그리드는 깨져 보인다.

### custom_css — page 레벨만 렌더된다 (⚠️ 블록 custom_css 는 무시됨)
공개페이지는 **`page.custom_css` 만** `<style>` 로 주입한다. **블록(Block)의 `custom_css` 는 렌더되지 않으니 만들지 마라.**
- `page.custom_css` — **body 배경(은은한 그래디언트) 한두 줄만** 쓰면 된다. 예: `body{background:linear-gradient(180deg,#FFF8F0,#F5EDE3);}`
- 카드 라운드/그림자/등장 애니메이션/강조 같은 **디자인 폴리시는 백엔드가 자동으로 입힌다**(디자인 킷). 그러니 복잡한 카드 CSS 를 직접 쓰려고 하지 마라 — 자주 깨진다.
- 단 **깨끗함 > 화려함**. 네온/무지개 텍스트 금지.
- (상세 선택자/패턴은 `ai_assets/rules/custom_css_guide.md` 참고.)

### 링크 카드 크기 — 3단계 정책 ⚠️ (에디터 명칭: small=컴팩트, medium=스탠다드, large=쇼케이스)
- **기본은 `layout:"small"`(컴팩트)** — 보조 링크 전부(SNS 유도·계좌·다운로드·전체보기 등). 페이지가 깔끔하고 정보 밀도가 높아진다.
- **주요 전환 CTA 딱 1개는 `layout:"medium"`(스탠다드)** — 카톡 문의하기·무료체험 시작·예약하기·주문하기처럼 **비즈니스 소통/전환에 직결되는 대표 버튼**. 한 줄짜리 small 로 묻히면 안 되고, large 쇼케이스는 과하다. 썸네일 없이 라벨+설명만으로도 정상 렌더된다(텍스트형 스탠다드 카드).
- **`large`(쇼케이스)는 상단 와이드 이미지가 본체** — 진짜 대표 상품·이벤트 1개에만, `thumbnail_url` 필수(없으면 자동으로 small 강등).
- 백엔드가 후처리로 이 정책을 강제한다(첫 전환 CTA→medium, 보조 연락 버튼→small, 과잉 쇼케이스→small).

### 색 조합 & 대비 — ⚠️ 가독성 필수
페이지 맥락(업종/무드) + 주요색 + 배경색(backgroundColor) + 카드색(blockBgColor) + 텍스트색 + 버튼색(buttonColor)을 **하나의 조화로운 팔레트**로 함께 설계한다. 특히 **텍스트색과 카드색은 페이지 배경 위에서 또렷이 읽히도록 충분한 명도 대비**를 확보하라.
- 어두운 배경 → 밝은 텍스트(예: bg `#0b0b14` + text `#f5f5f5`).  밝은 배경/카드 → 어두운 텍스트.
- 배경과 비슷한 톤의 텍스트/버튼(예: 남색 배경에 남색 버튼)은 안 보이므로 금지.
- 버튼색(buttonColor)은 배경·카드와 또렷이 구분되는 강조색으로.

---

## 폰트(fontFamily) 정책 — ⚠️ 반드시 준수

`design_settings.fontFamily` 에는 **아래 5개 값만** 쓸 수 있다. 서비스에 탑재된 웹폰트가 이게 전부다.

| fontFamily 값 (정확히 이대로) | 표시명 | 성격 | 사용 |
|---|---|---|---|
| `Pretendard` | Pretendard | 모던 산세리프 | **기본 · 최우선 권장** |
| `Noto Sans KR` | Noto Sans | 보편 산세리프 | **기본 · 권장** |
| `IBM Plex Sans KR` | IBM Plex | 또박또박·중립·테크 | 컨셉 필요시만 |
| `Nanum Gothic` | 나눔고딕 | 친근한 고딕 | 컨셉 필요시만 |
| `Nanum Myeongjo` | 나눔명조 | **유일한 명조(세리프)** | 컨셉 필요시만 |

**규칙:**
1. **기본값은 `Pretendard`** 다. 거의 모든 페이지는 `Pretendard` 또는 `Noto Sans KR` 를 쓴다 — 이게 안전하고 가독성이 가장 좋다.
2. 나머지 3개(`IBM Plex Sans KR` / `Nanum Gothic` / `Nanum Myeongjo`)는 **컨셉상 분명한 이유가 있을 때만** 쓴다:
   - 고급·전통·에디토리얼·세리프 감성 → `Nanum Myeongjo`
   - 또박또박·중립·엔지니어링/테크 → `IBM Plex Sans KR`
   - 둥글고 친근한 고딕 → `Nanum Gothic`
3. **위 5개 외의 폰트명은 절대 쓰지 마라** (예: `Gmarket Sans`, `Noto Serif KR`, `Montserrat`, `Roboto` 등). 탑재돼 있지 않아 렌더링되지 않고 시스템 기본 폰트로 폴백된다.
4. **모노스페이스 폰트는 제공되지 않는다.** "사이버/코드" 같은 느낌이 필요하면 폰트가 아니라 `custom_css`(letter-spacing, text-transform: uppercase, font-feature-settings 등)로 표현하라.
