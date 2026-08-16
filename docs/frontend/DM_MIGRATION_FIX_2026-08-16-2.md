# [백엔드 → 프론트] 2건 처리 완료 — `(code=190)` 꼬리 · `excluded` 더미

작성: 2026-08-16 (백엔드) · 대상: **dev** — 반영 완료

---

## 1. `(code=190)` — **더미 시드에 박제된 문자열이었습니다.** 지웠습니다

정확히 짚으셨습니다. **실제 실패 경로는 08-16 에 고쳤는데, 더미 시드의 문자열은 옛날 것 그대로**
남겨 뒀습니다. 제가 "없앴습니다" 라고만 회신하고 시드를 확인하지 않은 탓입니다.

레포 전체를 다시 훑어 이 문장을 만드는 곳이 어디인지 확인했습니다.

| 위치 | 이전 | 지금 |
|---|---|---|
| 실제 파이프라인 (`pipeline.run_migration`) | `... (code=190).` | **08-16 에 이미 교체됨** |
| 더미 시드 (`_state_failed`) | `... (code=190).` ← **여기만 남아 있었음** | **교체 완료** |

다른 생산지는 없습니다(`grep` 결과 0건).

### 지금 dev 응답 (`mig-failed@turnflow.dev` 실측)

```json
"error": {
  "code": "token_expired",
  "message": "인스타 연결이 만료되었거나 권한이 없습니다. 계정을 다시 연결해주세요."
}
```

`code=` 포함 여부 검사 → **false**.

> ✅ **정규식 지우셔도 됩니다.** `error.message` 는 그대로 노출 가능한 사용자 문장이고,
> 기술 코드는 `error.code`(머신 키) + 서버 로그에만 남습니다.
> 시드가 실제 문장에서 다시 어긋나지 않도록 **"파이프라인과 글자까지 같게 유지"** 주석을
> 시드 코드에 박아 뒀습니다.

---

## 2. `excluded` 후보를 `mig-ready` 에 3건 섞었습니다

새 계정을 만들지 않고 **`mig-ready`** 에 넣었습니다 — 원래 "목록 화면의 모든 분기를 한 계정에서"
용도로 만든 계정이라 밴드 3종이 한 곳에 있는 게 맞습니다.

```
mig-ready@turnflow.dev   workspace_id = 2eb847c1-6b0b-5c0c-8402-db9707c1dcc0
→ total 16   by_band = { auto_draft: 8, needs_review: 5, excluded: 3 }
```

⚠️ **후보 개수가 13 → 16 으로 늘었고 `job_id` 가 바뀌었습니다.** 재로그인 후 다시 조회해 주세요.

### ⚠️ 중요 — `excluded` 후보는 **이름도 문구도 비어 있습니다**

이게 실물을 보셔야 했던 진짜 이유입니다. 초안 생성(LLM) 대상이 `auto_draft`·`needs_review`
뿐이라, `excluded` 후보에는 **`draft_name` · `draft_description` · `draft_opening_message` 가
전부 빈 문자열**로 옵니다.

dev 실측(`?band=excluded&view=list`):

```json
{
  "band": "excluded",
  "draft_name": "",                     // ← 비어 있음. 카드 제목 폴백 필요
  "draft_opening_message": "",          // ← 비어 있음
  "offer":  { "url": "", "button_label": "", "confirmed": false, "edited": false },
  "gate_detected": false,
  "confirm_required": false,            // 확인받을 링크 자체가 없음
  "support": { "hits": 0, "probed": 6, "score": 0.0 },
  "media_caption_excerpt": "'룩북' 댓글 남겨주세요! (DM 기록을 못 찾은 더미 게시물 0)",
  "suggested_keywords": ["룩북"]
}
```

**남아 있는 재료는 `media_caption_excerpt` · `suggested_keywords` · `media_permalink` ·
`media_timestamp` 뿐**입니다. 카드 제목은 캡션 발췌로 폴백하셔야 합니다
(`draft_name` 을 그대로 쓰면 제목이 빈 카드가 나옵니다).

### 이 밴드의 의미와 권장 처리

> "이 게시물에서 캠페인을 돌리신 것 같은데(캡션에 트리거 문구가 있고 댓글 패턴이 반복),
> **보내셨던 DM 기록을 못 찾았어요.**"

- **자동 적용 대상 아님** — `apply-all` 의 기본 밴드(`auto_draft`)에 포함되지 않고,
  `bands` 파라미터로도 지정할 수 없습니다(`auto_draft`/`needs_review` 만 허용).
- 개별 `apply` 는 됩니다만 **문구가 비어 있어 그대로 만들면 빈 캠페인**이 됩니다.
  권한다면 "직접 문구를 쓰시겠어요?" 로 유도하는 정도입니다.
- **정황이 아예 없는 게시물은 후보로 만들지도 않습니다** — `excluded` 로 내려온 건
  "정황은 있다" 는 뜻이라 사용자에게 보여줄 가치는 있습니다.

숨기든 별도 섹션이든 프론트 판단에 맡기겠습니다. 저희 의견은 **목록 맨 아래 별도 섹션**
("DM 기록을 못 찾은 게시물 3건") 입니다 — 숨기면 사용자가 "내 캠페인이 왜 안 나오지" 를
확인할 방법이 없어집니다.

### 회귀 방지

파이프라인이 `excluded` 후보를 실제로 만든다는 것과 **초안이 비어 있다**는 것을 테스트로
고정했습니다(`test_excluded_band_is_created_when_signal_but_no_dm_found`).
"밴드는 2종" 이라고 잘못 답한 일이 다시 없도록, 시드도 실제 산출물과 같은 모양으로 만들었습니다.

---

## 검증

```
apps/integrations                       355 passed (신규 1)
mig-failed   error.message = "인스타 연결이 만료되었거나 …"   ("code=" 미포함) ✔
mig-ready    total=16  by_band={auto_draft:8, needs_review:5, excluded:3} ✔
             excluded 후보 draft_name=""  offer.url=""  support=0/6 ✔
```

`mig-applied` 재시드는 하지 않았습니다 — §B 재적용으로 자급자족하신다니 그 편이 낫습니다.
