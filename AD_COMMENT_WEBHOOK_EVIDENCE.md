# 광고(Paid partnership) 유입 댓글 웹훅 — 실측 증거 보존

**작성 2026-07-30.** 이 문서는 **실험 원본 데이터의 영구 보존**이 목적이다.
계측 행은 `EventInbox`(일별 파티션)에 들어가고 `drop_old_eventinbox_partitions` 가
**7일 후 DROP** 하므로, 증거를 여기에 옮겨 적는다. 다시 광고를 돌려 재현하기 어렵다.

---

## 1. 왜 조사했나

`@ellisa_levelup` 의 게시물에서 **"댓글은 달렸는데 DM 이 안 갔다"** 는 의문이 제기됐다.
조사 결과 광고로 배포된 게시물에서만 발생했고, 원인은 아래와 같다.

| 게시물 | 성격 | API vs UI | 놓친 요청자 |
|---|---|---|---|
| 자격증 `DbDg3H7zDuX` | organic | **완전 일치** (likes 71/71, comments 186/186) | **0명** |
| 치트키 `Dam6TJ_T0DY` (스파르타) | Paid partnership | 불일치 (댓글 44만 보임) | **14명 이상** |
| 칼퇴 `DbXpG8pToKs` (AI Note2) | Paid partnership | 불일치 (UI 38 likes vs API 21) | **3명 이상** |

광고 게시물은 **Graph API 의 organic `comments` edge 에 광고 유입 댓글이 나오지 않는다**
(단건 노드 조회도 `code=100 does not exist`). 따라서 `poll_missed_comments` 보정도 불가능하고,
**웹훅이 유일한 회수 경로**다.

---

## 2. 실측 원본 (2026-07-30 11:48:22 UTC)

직원 계정으로 광고 배포된 AI Note2 릴스에 트리거 키워드 `칼퇴` 를 댓글로 남겨 확보.
`views.py::_capture_comment_webhook_raw` 가 `EventInbox(event_type="comment_raw_ad")` 로 캡처.

```json
{
  "id": "17841400006862718",
  "time": 1785412106,
  "changes": [
    {
      "field": "comments",
      "value": {
        "id": "18220742947333329",
        "from": { "id": "742084212153673", "username": "ks.___.hyeon" },
        "text": "칼퇴",
        "media": {
          "id": "18083495273654753",
          "ad_id": "120250784238480294",
          "original_media_id": "18085701743661167",
          "media_product_type": "AD"
        }
      }
    }
  ]
}
```

**핵심 3가지**

1. `value.media.id` = **광고 카피의 미디어 id** (`18083495273654753`). 캠페인의
   `media_id`(`18085701743661167`) 와 다르다 → 기존 코드는 매칭 실패 → **DM·SeenComment·
   로그 어디에도 흔적 없이 드롭**(실측: `SentDMLog` 0건 / `SeenComment` 0건).
2. 원본 게시물은 **`value.media.original_media_id`** 로만 내려온다 → 이걸로 회수 가능.
3. 형태는 **`entry.changes[]`** (flat 아님). `value.id`(=comment_id 키가 아님) + `media` 에
   광고 필드. `ad_title` 은 **오지 않았다**.

부가 확인:
- 광고 댓글의 단건 노드는 살아 있고 permalink 가 **별개**다 →
  `https://www.instagram.com/p/DbX6RrpsDSk/` (원본은 `/reel/DbXpG8pToKs/`)
- organic edge 21건에 `@ks.___.hyeon` 없음. API 등장 username 11명 전부 organic 댓글자.

---

## 3. Meta 공식 문서와의 차이 (오판 기록)

[Webhook Notification Examples](https://developers.facebook.com/docs/instagram-platform/webhooks/examples/)
는 로그인 방식별로 다른 payload 를 제시한다.

| | Business Login for Instagram (우리) | Facebook Login for Business |
|---|---|---|
| 구조 | `entry.field` / `entry.value` (changes 없음) | `entry.changes[].value` |
| comment id 키 | `value.id` | `value.comment_id` |
| `media` | `{id, media_product_type}` | `{id, ad_id, ad_title, original_media_id, media_product_type}` |

우리 스코프는 `instagram_business_basic` / `instagram_business_manage_comments` =
**Business Login**. 문서대로면 광고 필드가 없다. 그래서 처음엔 "우리 방식엔 안 온다" 고
결론냈다 — **틀렸다.**

**왜 틀렸나 (재발 방지용):** 근거로 쓴 `SentDMLog.webhook_payload` 18,966건 전수 스캔에서
`ad_id`/`original_media_id` 가 0건이었는데, **광고 댓글은 매칭 실패로 `SentDMLog` 자체가
생기지 않으므로 그 corpus 에 원리적으로 들어갈 수 없었다.** 편향을 인지하고 있었음에도
"문서와 100% 일치" 라는 사실에 무게를 뒀다. 실제 형태는 **Business Login 구조 + 광고 필드
혼합**(문서 예제가 불완전)이었다.

교훈: 부재 증명은 **그 사건이 기록될 수 있는 저장소**에서만 유효하다. 매칭 실패 건이
기록되지 않는 테이블로 "매칭 실패 건이 없다" 를 주장하면 안 된다.

---

## 4. 조치

- **`0706678`** — `original_media_id` 폴백 매칭 (`apps/integrations/tasks.py`).
  `media_id_candidates`(광고+원본) 로 캠페인 매칭, `canonical_media_id`(원본 우선) 를
  `SeenComment`·`next_media` attach·복구 라우팅·스팸 트리거 면제 판정에 사용.
  회귀 테스트 `apps/integrations/tests_ad_comment_matching.py` 10건이 **이 원본 payload 를
  그대로 픽스처로 사용**한다.
- **`fb566bc`** — 웹훅 원문 계측(`_capture_comment_webhook_raw`). 판정을 끝냈으므로
  이후 **광고/비정상 형태 전용**으로 축소했다(일반 댓글은 더 이상 적재하지 않는다).

## 5. 남은 미확인 사항

**광고 댓글에 Private Reply(DM)와 공개 대댓글이 실제로 허용되는지는 아직 검증되지 않았다.**
매칭은 이제 되므로 발송이 시도되고, Meta 가 거부하면 `SentDMLog.error_subcode` 에 사유가
남는다. 광고를 다시 돌릴 때 `@ks.___.hyeon` 같은 광고 유입 요청자의 로그를 확인할 것.
