# AI 페이지 생성 — 프론트엔드 연동 가이드

> 대상: TurnflowLink 프론트엔드 개발자.
> 상세 스키마/예시는 **api-mcp**(사내 API 문서 검색 MCP — `https://dev-api.turnflow.link/api/schema/` 라이브 스펙)에서
> "AI 페이지 생성", "ai/jobs" 로 검색하면 항상 최신으로 나온다. 이 문서는 흐름 요약본.

---

## TL;DR — 새 페이지 생성 (권장 흐름)

```
0) 빈 페이지 생성         POST /api/v1/pages/ ...                → slug (예: page-xxxx)
1) (선택) 이미지 업로드   POST /api/v1/ai/source-images/        → image id 목록
2) 작업 시작              POST /api/v1/ai/jobs/                  → { id }
                          { concept, category, apply_to_slug: slug }   ⭐ apply_to_slug = 0)의 slug
3) 폴링 (1~2초)           GET  /api/v1/ai/jobs/{id}/             → status/stage/progress
                          status == "succeeded" 이면 그 페이지에 이미 적용 완료 → 페이지로 이동
```

> **변경점 (2026-06):** `apply_to_slug` 를 보내면 백엔드가 성공 시 결과를 그 페이지에 **자동 적용**한다.
> 프론트는 4단계 적용 호출(`POST /pages/ai/@{slug}/`)을 따로 할 필요가 없다 —
> `succeeded` = 페이지 완성. (안 보내면 아래 "수동 적용" 흐름으로 fallback.)
>
> ⚠️ **`succeeded` 인데 페이지가 비어 보이던 버그의 원인이 이것**: 예전엔 프론트가 별도 적용 호출을
> 빠뜨리면(또는 실패해도 에러를 안 띄우면) job 은 성공인데 페이지는 빈 채로 남았다. 이제 apply_to_slug
> 로 백엔드가 적용을 책임진다. 적용까지 실패하면 job 이 `failed` 로 떨어지므로 프론트는 그때 에러를 띄우면 된다.

### (fallback) 수동 적용 4단계 — apply_to_slug 미사용 / 리메이크

```
1) (선택) 이미지 업로드   POST /api/v1/ai/source-images/        → image id 목록
2) 작업 시작              POST /api/v1/ai/jobs/                  → { id }
3) 폴링 (1~2초)           GET  /api/v1/ai/jobs/{id}/             → status/stage/progress/result_json
4) 적용                   POST /api/v1/pages/ai/@{slug}/         ← result_json 그대로 전달
```

인증은 전부 `Authorization: Bearer <access_token>`.

## 2) 작업 시작 — 요청 바디

```jsonc
POST /api/v1/ai/jobs/
{
  "concept": "성수동 모임공간 '레이어드' 대여. 시간당 요금, 네이버 예약, 주차 안내.",
  "category": "space-booking",       // ⭐ 명시 권장 (아래 표) — 카테고리 전용 레시피 적용
  "apply_to_slug": "page-xxxx",      // ⭐ 새 페이지: 0)에서 만든 빈 페이지 slug → 성공 시 자동 적용
  "image_ids": ["<uuid>", "..."],    // 선택: 1단계에서 받은 id (최대 10장)
  "reference_page_slug": ""          // 선택: 디자인 톤 레퍼런스. 비우면 카테고리 기본값 자동
}
```

### `category` 값 (카테고리 선택 UI 의 슬러그 그대로)
`GET /api/v1/ai/categories/` 응답의 슬러그와 1:1:

| 슬러그 | 카테고리 |
|---|---|
| `profile-link` | 프로필 링크 |
| `digital-card` | 디지털 명함 |
| `landing` | 랜딩/홈페이지 |
| `portfolio` | 포트폴리오 |
| `brochure` | 브로슈어 |
| `space-booking` | 공간 대여/예약 |
| `group-buy` | 공동구매 |
| `invitation` | 모바일 초대장(청첩장) |
| `affiliate` | 제휴 마케팅 |
| `commission` | 커미션 |
| `promotion` | 홍보/프로모션 |

- **비우면** concept 문구에서 자동 추론한다(동작은 하지만, 사용자가 카테고리를 골랐다면 꼭 보내라 — 품질이 다르다).
- 리메이크(`slug` 전달) 시에는 무시된다.

## 3) 폴링

`status == "succeeded"` 가 되면 `result_json` 에 페이지 전체 JSON(블록 포함)이 들어 있다.
`stage`/`progress`/`message` 로 진행 UI 표시 (queued → labeling_images → preparing_prompt →
calling_model → parsing_response → resolving_images → completed). 통상 60~150초.

- 실패: `status == "failed"` + `error_message`. **실패 시 토큰 차감 없음** → 재시도 버튼 노출 권장.
- 토큰: 1회 성공당 1토큰. 잔액 `GET /api/v1/ai/tokens/`, 부족 시 402.

## 4) 적용 / 롤백

- **자동 적용 (새 페이지, `apply_to_slug` 전달 시)**: 백엔드가 성공 시 그 페이지에 결과를 적용하므로
  프론트의 별도 적용 호출이 **불필요**. `succeeded` 면 페이지로 이동만 하면 된다.
- **수동 적용 (`apply_to_slug` 미사용 / 리메이크)**: `result_json` 을 **그대로**
  `POST /api/v1/pages/ai/@{slug}/` 에 전달 (기존 블록 전체 교체).
- 롤백: `POST /api/v1/ai/jobs/{job_id}/rollback/` — 해당 작업의 result_json 으로 복구.
- 페이지별 작업 이력: `GET /api/v1/ai/pages/{slug}/jobs/`.

---

## 프론트가 알아야 할 변경점 (2026-06 품질 개선)

**API 계약 변경은 `category` 필드 추가 1건뿐** (선택 필드라 기존 호출도 그대로 동작).
나머지는 전부 백엔드 자동 처리라 프론트 코드 수정 불필요 — 다만 결과물 성격이 달라졌다:

1. **빈 이미지 없음** — 히어로/아바타/갤러리/그룹링크 썸네일을 전부 채워서 내려준다
   (Pixabay 검색 + 비전 모델 검수 + 2차 보강). placeholder 아이콘 케이스 방어 코드가 있다면 거의 안 탄다.
2. **블록 수 증가** — 평균 14~24블록(이전 8~12). 결과 미리보기 영역 스크롤/로딩 고려.
3. **`page.custom_css` 가 항상 채워짐** — 카테고리별 디자인 킷(카드 라운드/그림자/등장 애니메이션,
   커미션은 카툰 잉크 보더). 공개페이지/미리보기 모두 page-level custom_css 렌더 필수.
4. **카드 크기 정책** — 보조 링크 small, 주요 전환 CTA(카톡 문의/무료체험 등) 1개 medium,
   쇼케이스 large 1개. 상품 카드(썸네일+가격) medium 은 여러 개 가능.
5. **후기 형식** — group_link 나열 대신 text 블록 1개(`text_layout:"toggle"`)에
   "아이디 ★★★★★ + 한줄평" 줄들. 별도 렌더 대응 불필요(기존 text 블록).
6. **청첩장(invitation)** — 레퍼런스 페이지(@wedding) 톤을 자동 학습(아이보리+골드+나눔명조).

## 자주 하는 실수

- `result_json` 을 가공해서 보내지 마라 — 적용 엔드포인트에 **그대로** 전달.
- `image_ids` 는 업로드 직후 1개 작업에만 소비된다(재사용 불가). 작업 실패 후 재시도면 다시 업로드.
- 폴링 간격 1~2초 권장. `calling_model` 단계가 가장 길다(수십 초) — progress 30에서 머무는 게 정상.
