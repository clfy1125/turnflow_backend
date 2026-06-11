# TurnflowLink 커스텀 CSS 가이드 (실측 기반)

> 공개페이지(`PublicLinkPage.tsx`) 라이브 DOM 을 직접 까서 검증한 사실. AI 생성 파이프라인과
> 사람이 직접 CSS 를 쓸 때 모두 이 문서를 기준으로 한다. (2026-06 검증)

---

## 1. 핵심 사실 (반드시 숙지)

1. **page-level `custom_css` 만 렌더된다.** 공개페이지는 `<style>{customCss}</style>` 를
   `.page-container` 안에 **raw 전역**으로 주입한다. 소스 우선순위:
   `raw.custom_css ?? page.custom_css ?? page.data.custom_css`.
2. **블록(Block)의 `custom_css` 필드는 공개페이지에서 렌더되지 않는다.** (모든 블록 컴포넌트에
   `custom_css` 주입 코드가 없음.) → **블록 custom_css 를 만들지 마라. page.custom_css 한 곳에서 다 한다.**
3. 공개페이지에서 각 블록 래퍼는 다음을 갖는다 (실측):
   ```html
   <div class="block-link" data-block-id="123" data-block-type="text|single_link|group_link|gallery|social|video|...">
   ```
   ⚠️ `data-block-type` 은 **DB type 이 아니라 서브타입(`data._type`)** 으로 들어온다 → 타입별 타겟 가능.
4. 카드 내부 요소(버튼/그리드 아이템)는 **인라인 style** 로 `borderRadius`(buttonShape),
   `backgroundColor`, `borderColor`, `color` 가 박혀 있다 → **CSS 로 이기려면 `!important` 필수.**

---

## 2. 작동하는 선택자 치트시트

```css
.page-container { }                                  /* 스크롤 영역(패딩 등) */
[data-block-container] { }                           /* 블록 묶음 래퍼(블록 간 간격) */
.block-link { }                                      /* 모든 블록 래퍼 */
.block-link[data-block-type="single_link"] > a { }   /* 단일 링크 버튼(카드) */
.block-link[data-block-type="group_link"] a { }      /* 그룹/그리드 항목 카드 */
.block-link[data-block-type="text"] > div[class*="border"],
.block-link[data-block-type="text"] > details { }    /* 텍스트 박스/토글 카드 */
.block-link[data-block-type="gallery"] img { }       /* 갤러리 이미지 */
.block-link[data-block-type="social"] a { }          /* 소셜 아이콘 */
div[data-block-id="123"] { }                          /* 특정 블록 1개 */
```

내부 단일 링크 카드의 실제 클래스(참고): `a.relative.flex.w-full.flex-col.border.shadow-sm...`
(고유 클래스가 없으므로 위의 `[data-block-type]` + 자식 선택자로 접근한다.)

---

## 3. 권장 사용 패턴 (= 코드의 `design_css.py` 디자인 킷)

밋밋함("10년 전 웹")의 원인은 `body{background}` 한 줄뿐이라서다. **카드/여백/등장/강조**를
page custom_css 로 입힌다. 검증된 안전 패턴:

```css
/* 1) 여백 리듬 */
.page-container{ padding-left:18px !important; padding-right:18px !important; }
[data-block-container]{ margin-top:18px !important; }

/* 2) 블록 등장(stagger) */
@keyframes tfUp{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.block-link{ animation:tfUp .5s cubic-bezier(.22,.61,.36,1) both; }
[data-block-container] > .block-link:nth-child(2){animation-delay:.10s}
[data-block-container] > .block-link:nth-child(3){animation-delay:.15s} /* ...n */

/* 3) 카드 폴리시(라운드 + 부드러운 그림자 + 헤어라인) */
.block-link[data-block-type="single_link"] > a,
.block-link[data-block-type="group_link"] a{
  border-radius:18px !important;
  box-shadow:0 12px 34px -18px rgba(17,17,26,.18) !important;
  border:1px solid rgba(17,17,26,.06) !important;
}
.block-link[data-block-type="single_link"] > a:hover{ transform:translateY(-2px); }

/* 4) 텍스트 카드 강조(컨셉 컬러 좌측 바) */
.block-link[data-block-type="text"] > div[class*="border"],
.block-link[data-block-type="text"] > details{
  border-radius:14px !important; border-left:3px solid <ACCENT> !important;
  box-shadow:0 12px 34px -18px rgba(17,17,26,.18) !important;
}

/* 5) 이미지 라운드 */
.block-link[data-block-type="gallery"] img{ border-radius:14px !important; }
```

**톤 변주(variant)** — 모든 페이지가 똑같이 보이지 않게 카테고리별로:
- `soft`(청첩장/프로필/제휴/커미션): radius 22px, 그림자 부드럽게
- `bold`(공구/프로모션): radius 14px, 그림자 진하게, 강조 또렷
- `editorial`(포폴/브로슈어): radius 8px, 그림자 최소, 얇은 라인
- `clean`(명함/랜딩/공간): radius 16px, 중간 그림자

**다크 배경**이면 그림자 대신 글로우(`rgba(0,0,0,.55)`) + 밝은 헤어라인(`rgba(255,255,255,.08)`).

---

## 4. 금지 / 주의

- ❌ 블록 `custom_css` 작성(렌더 안 됨). page.custom_css 로만.
- ❌ 네온/무지개 텍스트, 과한 애니메이션(깨끗함 > 화려함).
- ❌ 인라인 스타일을 이기려다 `* { ... !important }` 같은 광범위 규칙(레이아웃 깨짐).
- ⚠️ `!important` 는 위 치트시트의 구체 선택자에만. 폰트/색 기본은 디자인 설정(design_settings)으로.

---

## 5. AI 생성 시 역할 분담

- **모델(deepseek)**: `page.custom_css` 에는 **body 배경(은은한 그래디언트) 한두 줄만**. 복잡한 카드
  CSS 를 모델이 직접 쓰면 자주 깨진다.
- **백엔드(`design_css.enhance_page_css`)**: 위 디자인 킷을 팔레트+카테고리에 맞게 **자동 생성해
  page.custom_css 뒤에 합친다**(킷이 `!important` 라 우선). → 모든 페이지가 기본적으로 세련되게.
