# 프론트엔드 연동 — 한 게시물 = 활성 캠페인 1개 (중복 방지)

> 백엔드에 "같은 Instagram 게시물에 활성(active) 캠페인을 둘 이상 둘 수 없다"는 검사가 추가되었습니다.
> 위반 시 **HTTP 409** 로 거부됩니다. 프론트는 아래 3가지만 처리하면 됩니다.

## 0. 문서는 MCP로 이미 최신
api-mcp 는 `https://dev-api.turnflow.link/api/schema/` (live OpenAPI) 를 읽습니다.
`get_endpoint("integrations_auto_dm_campaigns_create")` 등으로 조회하면 **409 응답이 이미 문서화**되어 있습니다.
요청/응답 스키마(필드)는 **바뀐 게 없습니다** — 새 에러 케이스만 추가됐습니다.

## 1. 409 에러 처리 (생성/수정/재개/예약활성화)
대상 엔드포인트: 캠페인 **생성**, **PUT/PATCH 수정(활성화 전환 시)**, **`{id}/resume/`**, **`{id}/schedule/` (activate=true)**.

응답 형식(표준 에러 포맷):
```json
{
  "success": false,
  "error": {
    "code": 409,
    "message": "이 게시물에는 이미 활성 상태인 캠페인 'XXX' 이(가) 있습니다. ...",
    "details": {
      "code": "duplicate_active_campaign",
      "conflict_campaign_id": "8f3c...uuid",
      "conflict_campaign_name": "여름 이벤트",
      "media_id": "18418812427189917"
    }
  }
}
```

해야 할 일:
- `status === 409 && error.details.code === "duplicate_active_campaign"` 로 분기.
- `error.message` 를 그대로 토스트/모달로 노출 (이미 사용자 친화 문구).
- (권장) `details.conflict_campaign_id` / `conflict_campaign_name` 로 **"기존 캠페인 보기 / 일시정지"** CTA 제공 →
  사용자가 기존 캠페인을 pause 하면 같은 게시물로 다시 생성/활성화 가능.

## 2. 일괄 재개(bulk-resume)의 새 실패 사유
`POST .../auto-dm-campaigns/bulk-resume/` 응답의 `failed[]` 에 새 reason 이 추가됩니다:
```json
{ "succeeded": ["id1"], "failed": [{ "id": "id2", "reason": "duplicate_active_campaign" }] }
```
- 전체 실패가 아니라 **건별 격리**입니다(나머지는 정상 재개).
- `reason === "duplicate_active_campaign"` 인 항목은 "이미 활성 캠페인이 있어 재개하지 못했습니다" 로 표시.

## 3. (선택) 생성 폼에서 선제 안내
- `trigger_type` 이 `specific_media` / `story_reply` 일 때만 적용됩니다.
  (`any_media`, 아직 attach 안 된 `next_media` 는 제한 없음.)
- 이미 활성 캠페인이 있는 게시물을 사용자가 고르면, 제출 전에 "이 게시물엔 이미 활성 캠페인이 있어요" 를
  보여주고 싶다면: 해당 IG 계정의 캠페인 목록을
  `GET .../auto-dm-campaigns/?ig_connection_id=<id>&status=active` 로 받아
  `media_id` 일치 + `trigger_type in (specific_media, story_reply)` 로 클라이언트 선제 체크 가능(서버 검사는 그대로 유지).
- 통합 가이드 `GET .../auto-dm-campaigns/guide/` 응답에 `duplicate_prevention` 블록(v4.2)이 추가됐습니다(폼 안내 문구용).

---
**요약**: 새 필드 없음. ① 4개 활성화 경로에서 409+`duplicate_active_campaign` 처리, ② bulk-resume 의 새 failed reason 처리, ③ (선택) 폼 선제 안내. 끝.
