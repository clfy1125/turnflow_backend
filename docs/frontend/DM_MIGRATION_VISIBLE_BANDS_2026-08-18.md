# DM 이전 — 후보 노출 범위 축소 (2026-08-18)

> **한 줄 요약: 후보는 이제 `auto_draft` 한 종류만 내려갑니다.** `needs_review`·`excluded`·
> `template_only` 는 DB 에는 남지만 **API 로 안 나옵니다.** 밴드 탭·검수 UI·`confirm-link`
> 버튼은 당장 렌더될 일이 없습니다.

배포: prod `0ad4760` (2026-08-18). 마이그레이션 없음. 되돌리기는 서버 설정 한 줄.

---

## 왜 바뀌었나

이전에는 판정이 애매한 후보를 `needs_review` 로 내려보내 **사용자가 검수**하게 했습니다.
그런데 그 목록이 계정당 100건이 넘었습니다 — "우리가 덜 조사한 것을 사용자에게 떠넘기는"
형태였습니다.

판정을 고쳐 실측으로 이만큼 줄였습니다(@highestlevel33, 게시물 493개).

```
              이전        지금
확실한 캠페인   19건   →   178건
검수 필요      108건   →     6건
```

남은 6건도 **자료가 없어서** 판정이 안 되는 것들입니다(예: 댓글러가 5명뿐인 게시물).
사용자에게 물어도 답이 안 나오므로, 제품 결정으로 **확실한 것만 넘깁니다.**

지우지는 않습니다 — 우리 운영 리포트가 봐야 판정을 계속 고칠 수 있고, 판정이 좋아지면
설정만 바꿔 다시 열 수 있습니다.

---

## 1. 후보 목록 — `GET /dm-migration/jobs/{id}/candidates/`

`band` 는 실질적으로 `auto_draft` 하나만 나옵니다.

```jsonc
{
  "count": 178,          // 이전 계약이라면 210 이었을 값
  "results": [
    { "band": "auto_draft", "confirm_required": false, /* ... */ }
  ]
}
```

**`?band=needs_review` 로 직접 물어도 빈 목록입니다** (에러가 아니라 `200` + `count: 0`).

```
GET .../candidates/?workspace_id=…&band=needs_review
→ 200 { "count": 0, "results": [] }
```

> 필터 파라미터 자체는 그대로 받습니다. 400 을 던지지 않으니 기존 코드가 깨지지는 않지만,
> **탭을 눌러도 0건**이 됩니다.

---

## 2. 요약 — `GET /dm-migration/jobs/{id}/candidates/summary/`

`total`·`by_band` 도 **보이는 것만** 셉니다.

```jsonc
{
  "total": 178,
  "by_band": { "auto_draft": 178 },   // 다른 키는 아예 안 나옵니다
  "by_status": { "detected": 178 },
  "needs_confirm": 0,                 // ← 항상 0 이 됩니다 (§4 참조)
  "with_offer_url": 175
}
```

---

## 3. 잡 응답의 카운터 — `GET /dm-migration/jobs/{id}/`

**두 필드가 목록과 같은 수를 냅니다.**

```jsonc
{
  "candidate_count": 178,
  "counters": { "candidates_created": 178, /* ... */ }
}
```

이전에는 `candidates_created` 가 **만들어진 전체 수**(210)였습니다. 그대로 두면 배너에
"210개 찾음" 을 띄우고 목록에는 178개만 나와, 사용자가 32개가 사라졌다고 봅니다.
→ **「N개 찾음」 배너는 이 값을 그대로 쓰면 맞습니다.**

---

## 4. ⚠️ `confirm-link` 는 당장 도달 불가입니다

`confirm_required` 는 정의상 **`auto_draft` 가 아닌 후보에만** 붙습니다(확실한 건 확인이
필요 없으므로). 그래서:

```
confirm_required: true 인 후보가 응답에 나오지 않습니다
→ "이 링크가 맞나요?" 버튼은 렌더될 일이 없습니다
→ needs_confirm 카운트는 항상 0
```

**코드는 지우지 마세요.** 밴드를 다시 열면 그대로 살아납니다. 렌더 조건
(`confirm_required === true`)만 유지하면 됩니다.

---

## 5. 숨은 후보를 직접 건드리면 — **404**

`needs_review` 후보의 id 를 어떻게든 알아내 호출하면 **403 이 아니라 404** 입니다.

```
POST /dm-migration/candidates/{숨은id}/apply/     → 404 후보를 찾을 수 없습니다
POST /dm-migration/candidates/{숨은id}/dismiss/   → 404
POST /dm-migration/candidates/{숨은id}/confirm-link/ → 404
```

403 을 주면 "있긴 있다" 가 새고, 프론트는 처리 못 하는 오류를 받습니다. **존재 자체를
알리지 않는** 쪽을 택했습니다.

### 일괄 적용은 조용히 무시합니다

```
POST /dm-migration/jobs/{id}/apply-all/
{ "bands": ["needs_review"] }

→ 200 { "applied": [], "failed": [], "skipped": 0 }    // 아무것도 안 만들어집니다
```

`bands` 는 서버가 보이는 밴드와 교집합을 취합니다. 400 이 아니라 **빈 결과**입니다.
(`bands` 를 생략하면 기본값 `["auto_draft"]` — 지금은 이게 유일하게 의미 있는 값입니다.)

---

## 6. 프론트에서 할 일

- [ ] **밴드 탭 UI** — `by_band` 에 키가 없는 밴드는 탭을 감추거나, 0건일 때 숨김 처리.
      탭 코드를 **삭제하지는 마세요**(다시 열립니다).
- [ ] **「N개 찾음」 배너** — `counters.candidates_created` 또는 `candidate_count` 를 그대로
      쓰면 목록 수와 일치합니다. 별도로 합산하지 마세요.
- [ ] **검수 화면** — 진입점을 감춥니다. `needs_confirm: 0` 이므로 "검수할 항목 N개" 배지도
      자연히 0 이 됩니다.
- [ ] **`confirm-link` 버튼** — 렌더 조건 유지, 코드 삭제 금지.
- [ ] **404 처리** — 후보 상세/적용에서 404 는 "이미 처리됐거나 대상이 아닙니다" 로 안내하고
      목록을 새로고침. (기존 404 처리가 있으면 그대로 두면 됩니다.)

---

## 7. 되돌릴 때

서버 설정 한 줄이고 **배포 없이** 반영됩니다.

```python
DM_MIGRATION_VISIBLE_BANDS = ["auto_draft", "needs_review"]
```

되돌리면 위 모든 응답이 예전 계약으로 복귀합니다(밴드 탭·검수 화면·`confirm-link`
전부 다시 동작). 그래서 프론트 코드를 지우지 말라고 부탁드립니다.

---

## 참고 — 이번에 함께 좋아진 것

같은 배포에 판정 개선이 들어갔습니다. 프론트 계약과는 무관하지만 숫자가 달라 보일 수
있어 적어둡니다.

| | 이전 | 지금 |
|---|---|---|
| 확실한 캠페인 | 19건 | **178건** |
| 문구·링크 둘 다 복원 | — | **175건** (178건 중 98%) |
| 분석 소요 | 4시간(예산 소진으로 중단) | **70분**(완료) |

관련 문서: `DM_CAMPAIGN_MIGRATION_FRONTEND.md`(본 계약) ·
`DM_MIGRATION_LONG_RUN_2026-08-17.md`(폴링·소요시간·`resume_at` 주의)
