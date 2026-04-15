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

**태그(badge):** 쉼표 구분 문자열. 각 태그를 pill 형태로 표시. small은 최대 2개, medium/large는 최대 3개, 초과분은 `+N`으로 표시.

**가격:** `price` 표시, `original_price`가 있으면 취소선으로 할인 전 가격 표시.

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

---

### 7. gallery — `type: "single_link"` + `data._type: "gallery"`

이미지 갤러리. 최대 10장.

```json
{
  "images": ["url1", "url2", ...],
  "gallery_layout": "single | carousel | list | thumbnail | free",
  "gallery_url": "클릭 시 이동 URL (선택)",
  "auto_slide": false,
  "keep_ratio": true
}
```

**레이아웃:**
- `single` — 1장씩 좌우 스와이프
- `carousel` — 가로 드래그 스크롤
- `list` — 세로 나열
- `thumbnail` — 작은 그리드 썸네일
- `free` — 자유 배치

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

---

## 페이지 디자인 설정

블록 외에 페이지 전체에 적용되는 디자인 설정:

```typescript
interface DesignSettings {
  theme: string;              // 테마명
  bgColor: string;            // 배경색
  textColor: string;          // 텍스트색
  buttonColor: string;        // 버튼색
  buttonTextColor: string;    // 버튼 텍스트색
  buttonStyle: 'filled' | 'outline' | 'soft';
  buttonRadius: number;       // 0~30
  fontFamily: string;         // 웹폰트명
  bgImage?: string;           // 배경 이미지 URL
  bgImageOpacity?: number;    // 0~1
  ctaColor?: string;          // 강조색 (일정 D-day 등에 사용)
}
```
