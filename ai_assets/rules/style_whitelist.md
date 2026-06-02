# 스타일 패치 화이트리스트

> `apps/ai_jobs/services/style_patcher._STYLE_WHITELIST` 와 정확히 일치해야 한다.
> 두 곳이 어긋나면 코드가 진실 — 이 문서를 따라 코드를 수정하지 말고, 코드를 바꾼 뒤 이 문서를 업데이트할 것.

bio_remake 의 두 모드(`full_restyle`, `style_only`) 에서 LLM 이 변경할 수 있는 `Block.data` 키 목록.
화이트리스트 외 키는 백엔드가 silently drop 한다.

## 공통 (모든 _type 적용 가능)

- `custom_bg_color`
- `custom_border_color`
- `custom_text_color`
- `custom_button_color`

## _type 별 추가 허용 키

| _type | 허용 키 |
|---|---|
| `profile` | `profile_layout`, `font_size` |
| `single_link` | `layout`, `text_align` |
| `group_link` | `group_layout`, `display_mode`, `text_align` |
| `social` | `custom_icon_color` |
| `video` | `video_layout`, `autoplay` |
| `text` | `text_layout`, `text_align`, `text_size`, `custom_sub_text_color` |
| `gallery` | `gallery_layout`, `auto_slide`, `keep_ratio` |
| `spacer` | `divider_style`, `divider_width`, `divider_color`, `spacing` |
| `notice` | `notice_layout` |
| `customer` | `custom_input_bg_color` |
| `folder` | `folder_icon`, `folder_icon_color`, `is_collapsed_default`, `folder_display_mode`, `text_align`, `folder_toggle_bg`, `folder_popup_bg`, `folder_popup_text`, `folder_popup_accent` |
| `schedule` | `schedule_layout` |
| `map` / `search` / `inquiry` / `contact` / `music` | (공통만) |

## 페이지 레벨

- `page.data` (특히 `design_settings.*`) — 통째로 교체.
- `page.custom_css` — 통째로 교체.
- `style_only` 모드에서는 `title` / `is_public` 변경 무시.

## 새 블록 생성 시 텍스트 콘텐츠 (full_restyle 모드, `_new: true`)

새 블록은 위 스타일 키 + 아래 텍스트 키를 LLM 이 직접 작성할 수 있다. URL/이미지/연락처 필드는 비워둠.

| _type | 허용 텍스트 키 |
|---|---|
| `profile` | `headline`, `subline` |
| `single_link` | `label`, `description` |
| `group_link` | `label`, `description` |
| `text` | `headline`, `content` |
| `notice` | `title`, `content` |
| `map` | `map_name` |
| `inquiry` | `inquiry_title`, `button_text` |
| `customer` | `customer_headline`, `customer_description`, `button_text` |
| `search` | `search_placeholder` |
| `folder` | `label` |
| `schedule` | `label` |
