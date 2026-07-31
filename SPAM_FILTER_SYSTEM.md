# 스팸 필터 시스템 현황 — 운영 구현 정리 + 연구소(spam_filter_lab) 대조

작성: 2026-07-31 · 대상: `apps/integrations` 인스타그램 댓글 스팸 필터
비교 기준: 운영 `turnflow_backend@main` vs 랩 `../spam_filter_lab@main(747b153)`
**최종 갱신 2026-07-31 — 프롬프트 v3 → v5 교체·배포 완료(§3)**

---

## 0. 결론 먼저

1. **랩 승격분 + v5 까지 전부 이식돼 배포됐다.** 프롬프트는 랩 `_V5_SPAM_ONLY` 와 바이트 동일
   (sha `69ef69bbd8fb`, 1781자, 실측 1038 tokens), 규칙 2-티어·ASCII 단어경계도 동일.
2. **랩에 있는데 운영에 없는 것은 2개뿐이고, 둘 다 판정에 영향이 없다** —
   퍼지 URL SOFT 신호(2-3, 진단 전용), 키워드 lint(2-2 후반, advisory). §7 참조.
3. **v5 는 운영자 지시로 §2 토큰 예산(~600)을 초과한 채 채택됐다**(1038 tokens, 1.76배).
   golden_v1 에서 v3 와 성능 동등(FLIP 0)이지만 난독화 복원·간접 유인 판정 축이 추가되고,
   `abuse` enum 제거로 v3 의 잠재 FP 구멍이 닫혔다.

| 랩 항목 | 랩 상태 | 운영 반영 | 배포 |
|---|---|---|---|
| **프롬프트 v5 (`2026.07.31-v5`)** | variants 등록(랩 자체 승격은 미완) | ✅ **바이트 동일** | ✅ 2026-07-31 |
| 프롬프트 v3 (`2026.07.23-v3`) | ✅ 승격 | ⬅ v5 로 교체됨(롤백 대상) | ✅ `41fe0ed`(07-24) |
| 규칙 키워드 2-티어 (2-1) | ✅ 완료 | ✅ 동일 | ✅ + 데이터 마이그 `0045` |
| ASCII 단어경계 매칭 (2-2 전반) | ✅ 완료 | ✅ 동일 | ✅ |
| 퍼지/난독 URL SOFT 신호 (2-3) | ✅ 완료 | ❌ 미이식(진단 전용) | — |
| `validate_keywords()` lint (2-2 후반) | ✅ 완료 | ❌ 미이식 | — |
| 상수 4종(3/500/0.9/64) | ✅ | ✅ 값 동일 | ✅ |
| 프롬프트 v2 / v4 | ❌ 폐기 | ❌ (정상) | — |
| 2-5 출력포맷 강화 | ⏸ 보류 | ❌ (정상) | — |

> `41fe0ed`(v3 이식) 배포 확인 근거: `git merge-base --is-ancestor 41fe0ed 7b79be7` = true —
> 07-30·07-31 배포 시점의 `main`에 포함되고 마이그레이션 `0045`도 적용됐다.
> (구 메모리 노트의 "41fe0ed 미배포"는 07-24 당시 기준이며 **현재는 배포됨**.)

> ⚠️ **랩 `kim` 브랜치는 이식하지 않았다.** origin/kim(07-31)에는 main 에 없는 3커밋이 있는데,
> 그 프롬프트는 **"혐오·협박·괴롭힘도 SPAM"** 이라 POLICY 규칙 3(악플 비검출)과 **정반대**이고,
> 본문(abuse 카테고리 사용 지시)과 출력 스키마(abuse 제거)가 **서로 상충**한다 — 연구원 본인이
> 커밋 메시지에 "상충이 남아 있다 · 기존 7,000회 실험은 이 프롬프트의 값이 아니다 · 재측정 필요"
> 라고 명시했다. 또 `cdb898a` 는 규칙 pre-filter 를 기본 비활성(gemma 단독)으로 바꿔
> CONSTRAINTS §3(판정 흐름 불변)·PORT.md(흐름 재배열 금지)에 걸린다. 채택한 v5 는 main 것이다.

---

## 1. 판정 정책 3원칙 (POLICY.md — 운영 확정)

| # | 정책 | 구현 위치 |
|---|---|---|
| 대원칙 | 가장 비싼 오류는 **정상 댓글 숨김(FP)**. 애매하면 통과(fail-open) | 전 계층 |
| 규칙 1 | **짧은 댓글은 스팸으로 안 잡는다** (리드젠 팬 댓글 = 원하는 반응) | `MIN_LEN_FOR_LLM=3` + 프롬프트 |
| 규칙 2 | **게시물 작성자(계정 본인) 댓글은 무조건 clean** | 웹훅 self 가드 |
| 규칙 3 | **악플(욕설·조롱·혐오)은 스팸이 아니다 → 절대 숨기지 않는다** | 프롬프트 v5 (본문 + enum 에서 `abuse` 제거) |

스팸의 정의 = **사기(scam) · 성인 유인(adult) · 피싱(phishing) · 무관 광고(promo) · 외부 유인** 뿐.

> 규칙 2는 랩 POLICY.md가 "**패치 필요** — 랩 `classify_comment`는 작성자 여부를 입력받지 않는다"고
> 적어둔 항목이다. **운영은 이미 구현돼 있다**(웹훅 진입부에서 `from_user_id == entry_id`면 즉시
> skip). 즉 이 항목은 운영이 랩보다 앞서 있다 — 랩 문서의 "패치 필요"는 랩 자신에 대한 얘기다.

---

## 2. 현재 운영 판정 파이프라인 (전체 흐름)

```
IG 댓글 웹훅 (field=comments)
  └─ views.py:4670  run_spam_filter_check.delay(webhook_data)      ← DM 발송과 병렬·독립 태스크
       │
       ├─ [G1] field != "comments"            → skipped: unsupported_field
       ├─ [G2] comment_id/from_user_id/entry_id 누락 → error: missing_fields
       ├─ [G3] ★ self 가드: from_user_id == entry_id → skipped: self_comment   ← POLICY 규칙 2
       ├─ [G4] 활성 IG 연결 조회 (external_account_id=entry_id, status=ACTIVE, is_active=True)
       │        · 같은 IG 계정이 여러 워크스페이스에 연결될 수 있어 **연결 전부** 순회
       │        · 소프트 비활성(is_active=False) 계정은 제외 → skipped: no_active_connection
       ├─ [G5] SpamFilterConfig 없음 or status != active → skipped: filter_inactive
       ├─ [G6] 플랜 게이트 owner_has_feature(ws, "spam_filter") (fail-closed, 프로 전용)
       │                                        → skipped: plan_not_allowed
       │
       └─ 연결별로 _run_spam_for_connection()
            │
            ├─ [S1] 멱등 claim: SpamCommentLog.get_or_create(spam_filter, comment_id)
            │        · UNIQUE(spam_filter, comment_id) 경합 → 최초 1회만 분류
            │        · 잠정 status=CLEAN (크래시해도 오탐 집계 안 됨 = 안전 기본값)
            │        · 이미 있으면 → skipped: already_processed (재분류·재숨김 없음)
            │
            ├─ [S2] ★ 캠페인 트리거 면제 — 규칙/LLM보다 우선 (ecf9d6b)
            │        활성 AutoDMCampaign 중 matches_media(media_id) && matches_keyword(text)
            │        → engine="campaign_trigger_exempt", CLEAN 확정, LLM 미호출
            │        · media_id 는 original_media_id 우선(광고 유입 댓글 정규화) ← 이게 없으면
            │          광고로 들어온 정상 트리거 댓글이 면제를 못 받고 숨겨진다
            │
            ├─ [S3] classify_comment()  ← spam_classifier.py (§3)
            │
            ├─ not is_spam → CLEAN 유지 + 판정 메타(confidence/category/engine)만 기록
            │
            └─ is_spam → status=DETECTED 승격 + spam_reasons/webhook_payload 보존
                          + spam_filter.increment_spam_detected()
                 ├─ auto_hide_enabled=False (기본) → detected 로 끝. 유저 수동 숨김 대기
                 ├─ mock 토큰 → hidden_mock (Meta 미호출)
                 ├─ Meta hide_comment 성공 → HIDDEN + increment_hidden()
                 └─ 실패 → FAILED (재분류 없음, 유저가 모더레이션 API로 재시도 가능)
```

### `classify_comment()` 내부 (하이브리드 4단)

```
text.strip()
 ①  SpamDetectionService.is_spam(text, spam_keywords, check_urls)   ← 규칙, 0초·LLM 없음
     hit → SpamVerdict(is_spam=True, category="rule", confidence=1.0, engine="rule")   [authoritative]
 ②  len(text) < MIN_LEN_FOR_LLM(3)  → engine="rule_trivial", clean   ← POLICY 규칙 1
 ③  use_llm=False (계정 kill-switch) → engine="rule_only", clean
 ④  _classify_with_gemma(text)      → §3
```

핵심 설계 3원칙 (`spam_classifier.py` 상단):
1. **하이브리드** — 명백한 스팸은 규칙으로 0초 차단, 애매한 것만 gemma(≈14 tok/s 부하 최소화)
2. **fail-open** — LLM 예외/타임아웃/파싱실패/저신뢰도는 **전부 "스팸 아님"**
3. **규칙 히트는 authoritative** — 규칙에서 스팸이면 LLM 호출 안 함

---

## 3. 시스템 프롬프트 (운영 현행 전문)

- 파일: `apps/integrations/spam_classifier.py` `_SPAM_SYSTEM_PROMPT`
- 버전: **`2026.07.31-v5`** / sha256[:12] = **`69ef69bbd8fb`** / 1781자 / **실측 1038 tokens**
- 출처: 랩 `spam_filter/prompt_variants.py` 의 `_V5_SPAM_ONLY` (main `747b153`) — **바이트 동일**
- 모델: **gemma-4 고정** (`resolve_model("gemma")`) — 자체 호스팅, 비용 0. **교체 금지**
- 호출 파라미터: `temperature=0.0`, `max_tokens=64`, user content = `text[:500]`

````text
너는 Instagram 댓글 1개의 spam/scam 분류기다(Korean+English). COMMENTER가 다른 독자를 상대로 사기, phishing, 무관 광고, 외부 유인, 성인 미끼를 하는지 판정하라. 단지 문체가 홍보처럼 들린다는 이유로 차단하지 마라.
입력은 COMMENT 하나뿐이다. 게시물 맥락·작성자 신원·숨은 패러디를 추측하지 마라. 따라서 도메인형 문자열과 유인 문구가 함께 있으면 농담투·ㅋㅋ·이모지여도 SPAM이다.
판정 순서:
1) 난독화를 먼저 복원하라. ',COM', '. C 0 M', '。net', 분리된 자모·공백·점·쉼표·기호·숫자를 문맥상 URL/도메인/이름/검색코드로 읽는다. 사이트의 실재 여부는 확인하지 말고 도메인처럼 보이면 목적지로 취급한다.
2) CLEAN: 이 계정들은 '댓글 키워드→DM으로 가이드/링크/자료 제공' 캠페인을 한다. 한 단어, 짧은 요청, 칭찬, 질문, 친구 태그, 이벤트 참여, 이모지는 CLEAN. '가이드', '🔥', 'DM 주세요', '설치링크 부탁드려요', '자료 공유 부탁'처럼 본인이 받으려는 요청도 CLEAN.
★욕설·조롱·혐오·협박·비방·괴롭힘은 무례해도 SPAM이 아니다 → is_spam=false, category=clean. '관종짓 그만해라', '너 완전 사기꾼이야 여기 사람들 다 낚였다', '얼굴도 실력도 없다' 같은 악플·안티는 링크·유인이 없으면 절대 숨기지 마라(스팸만 잡는다).
스팸을 인용·비판·조롱하거나 목적지를 명백히 부정하고 다른 보상·추천이 없으면 CLEAN: '주소창에 XXX 입력하라는 댓글 왜 달려?', '426금.Net 입력하면 아무것도 안 나온다. 강대성이나 봐라😍'. 목적지·행동 지시가 없는 팬 농담은 CLEAN: '김달봉 원본 U출됨🔥 보고서 귀여움 느꼈다❤️'. 친구 태그나 같은 인물의 관련 게시물 안내도 CLEAN: '김달봉 입양 사연은 @chill.dalbong 첫 고정 게시물'.
3) SPAM: 직접 명령은 필요 없다. URL·도메인·검색어·숫자코드·제3자 계정·전화번호·Telegram·오픈채팅·프로필을 노출하고, 그곳의 기능/콘텐츠, 접속 후기, 추천, 재미, 중독, 효과, 수익, 원본, 유출, VIP, 성인물, 도박·투자 등 보상이나 호기심을 붙이면 간접 유인이므로 SPAM. 검색/클릭/방문/입장/DM/연락/결제/가입/개인정보 제공을 시켜도 SPAM. fake support·giveaway 사칭, 무관 상품·서비스 광고, 성적 제안도 SPAM.
SPAM examples: '캬,COM 마두루도 안에서 이걸로 사주 보고 있을 듯'→promo; '=캬 ,C 0 m= 들어갔다가 중독 되가지구 찹츄 찹찹츄 했네'→promo; '진용진19 . C O m。검색😍 후방주의'→adult; '인스타에 여 배우 S양 쳐봐, 직접 확인하면 레전드'→adult; '송 하 리 6,9,2,5 원본 구글로 검색해봐'→adult; '주소창에 XXX 입력하면 원본 나옴'→adult.
목적지와 유인이 모두 불명확하면 CLEAN. is_spam=false면 category=clean. true면 scam(사기/도박/투자), adult(성인 유인), phishing(사칭/정보수집), promo(무관 광고) 중 하나. abuse(악플) 카테고리는 쓰지 마라 — 악플은 clean 이다.
오직 compact JSON만 출력하고 설명·코드펜스는 금지:
{"is_spam":<true|false>,"category":"<clean|scam|adult|phishing|promo>","reason":"8 words 이하","confidence":<0.0-1.0>}
````

> 위 블록은 가독성을 위해 개행 이스케이프 기준으로 펼친 것이다. 실제 리터럴은 파이썬 문자열
> 연결 형태이며 **랩 `_V5_SPAM_ONLY` 와 바이트 동일**해야 한다(회귀 가드:
> `test_prompt_byte_identical_to_lab`).

### v5 가 강제하는 것

1. **판정축 = "작성자가 *다른 독자를 상대로* 유인/사기하는가"** — "홍보처럼 들리는가"가 아님.
2. **난독화 먼저 복원** — `',COM'`·`'. C 0 M'`·`'。net'`·분리된 자모/공백/점/기호/숫자를
   URL·도메인·검색코드로 읽는다. 사이트 실재 여부는 확인하지 않고 도메인처럼 보이면 목적지로 취급.
3. **간접 유인도 SPAM** — 직접 명령("클릭해") 없이도 목적지 노출 + 후기·추천·재미·중독·수익·
   원본·유출·VIP 등 보상/호기심이 결합되면 SPAM. v3 에 없던 축이다.
4. **악플은 clean** — "욕설·조롱·혐오·협박·비방·괴롭힘은 무례해도 SPAM이 아니다 →
   is_spam=false, category=clean", "절대 숨기지 마라". 나아가 **출력 enum 에서 `abuse` 를 제거**해
   구조적으로 막았다(§6 — v3 에 남아 있던 잠재 FP 구멍을 v5 가 닫았다).
5. **리드젠 팬 댓글은 CLEAN** — 한 단어·짧은 요청·칭찬·질문·친구 태그·이벤트 참여·이모지.
   `'DM 주세요'`·`'설치링크 부탁드려요'` 처럼 **본인이 받으려는 요청**도 CLEAN.
6. **스팸 인용·비판·조롱은 CLEAN** — `'주소창에 XXX 입력하라는 댓글 왜 달려?'`,
   `'426금.Net 입력하면 아무것도 안 나온다. 강대성이나 봐라😍'`.
7. **애매하면 CLEAN** — "목적지와 유인이 모두 불명확하면 CLEAN".

### v3 → v5 교체 기록 (2026-07-31)

| | v3 (구) | v5 (현행) |
|---|---|---|
| sha256[:12] | `5e6b06680d30` | **`69ef69bbd8fb`** |
| 언어 | 영어 | **한국어** |
| 문자수 | 1989자 | 1781자 |
| **실측 prefill** | **590 tokens** | **1038 tokens (1.76배)** |
| 난독화 복원 지시 | 없음 | 있음 |
| 간접 유인 판정 | 없음 | 있음 |
| `abuse` enum | 잔존(잠재 FP 구멍) | **제거** |
| CONSTRAINTS §2 (~600 토큰) | 충족 | **초과 — 운영 결정으로 수용** |

**측정 근거**

- 랩 A/B `--compare v3-spam-only v5-spam-only --batch datasets/golden_v1.jsonl`(103행):
  CFPR **0/63 = 0.0% 동일**, recall **100% 동일**, F1 1.000 동일, **FLIP 0건**
  (clean→spam 신규 FP 0 / NEW-MISS 0). 즉 이 데이터셋에서는 **v3 와 v5 가 동등**하고
  v5 의 난독화 이득은 여기에 잡히지 않는다.
- 운영 코드 경로 라이브 스모크 11건(실 gemma): **11/11 정확** — 악플 2건 clean(conf 1.0),
  리드젠 5건 clean, 난독 스팸 3건 spam(adult/promo, conf 1.0), 스팸 비판 인용 clean.
  판정 지연 **1.55~2.39초**, prefill in 약 1039~1056 tokens.

**주의 — 알려진 비용**: prefill 1.76배. gemma 는 자체 호스팅이라 **금전 비용은 0**이지만,
랩 CONSTRAINTS §2(prefill 부담·지연)와 §10(단건 2초 이내)에 걸린다 — 실측에서 일부 호출이
2.1~2.4초로 §10 을 살짝 넘었다. 스팸 검사는 DM 발송과 **독립된 비동기 Celery 태스크**라
사용자 응답을 막지 않지만, 댓글 트래픽이 급증하면 큐 부하로 나타날 수 있다.
**롤백**: `_SPAM_SYSTEM_PROMPT` 를 v3(`5e6b06680d30`, 590 tokens)로 되돌리고 테스트의
sha 핀·한국어 단언을 함께 되돌린다.

## 4. 규칙 pre-filter (`SpamDetectionService`, `services.py:1489`)

### 4-1. URL / 도메인 (하드블록)

```python
url_pattern    = r"https?://[^\s]+"
domain_pattern = r"\b[a-zA-Z0-9-]+\.(com|net|org|co\.kr|asia|io|app|xyz|info|biz)\b"
```
→ 히트 시 `reasons=["contains_url"]`. `block_urls=False`면 검사 생략. **랩과 동일.**

### 4-2. 키워드 2-티어 (랩 2-1 이식)

```python
HARD_BLOCK_KEYWORDS   = ["원본영상", "실시간검색"]        # 즉시차단(LLM 미호출)
SOFT_SIGNAL_KEYWORDS  = ["아이돌", "주소창", "사건"]      # ★ 차단 안 함 — gemma 위임
DEFAULT_SPAM_KEYWORDS = HARD_BLOCK + SOFT_SIGNAL          # 하위호환 별칭(차단에 안 쓰임)
```

- 기본 티어는 **HARD만 차단**. SOFT 일상어는 차단하지 않고 gemma로 흘린다
  (threshold 0.9 + fail-open이 지배).
- **왜**: 규칙 오탐은 LLM 이전 short-circuit이라 fail-open으로 **구제 불가능**한 가장 비싼 오류였다.
  "무슨 사건이에요?" 같은 정상 댓글이 즉시 숨겨졌다 — 실측 **rule precision 50% → 100%**,
  recall 100% 유지, hard_cases rule-FP 10→1.
- **계정별 `spam_keywords`가 있으면 그대로 authoritative 하드블록** (오너의 명시적 선택).
  즉 오너가 '사건'을 직접 넣으면 여전히 즉시차단된다.

### 4-3. ASCII 단어경계 매칭 (랩 2-2 이식)

```python
_ASCII_WORDLIKE_RE = re.compile(r"^\w(?:.*\w)?$", re.ASCII)

def _keyword_hit(low_text, keyword):
    k = keyword.lower()
    if k.isascii():
        if _ASCII_WORDLIKE_RE.match(k):
            return re.search(rf"\b{re.escape(k)}\b", low_text) is not None   # 'ad' ≠ 'download'
        return k in low_text        # '18+', '@vip' — \b 성립 불가 → substring
    return k in low_text            # CJK: 한국어는 공백 경계 없음 → substring
```

**명문화된 한계**: Python `\b`는 유니코드 기준이라 `'bet모집'`처럼 ASCII+한글이 붙으면 매치되지
않는다 → 규칙 miss. gemma가 이어받는 **값싼 오류라 수용**(랩 §4).

### 4-4. 마이그레이션 `0045` — 시드 키워드 리셋 (이식 동반 데이터 작업)

프론트가 config 생성 시 **옛 기본 5개**(아이돌·주소창·사건·원본영상·실시간검색)를
`spam_keywords` 행에 복사해 왔다. 계정 키워드는 authoritative라서, 코드 기본값만 HARD로
좁혀도 이 시드 행들은 여전히 '사건'을 즉시차단한다.
→ **정확히 옛 기본 5개 세트인 행만** `[]`로 리셋(순서 무관). 오너가 추가/삭제한 행은 건드리지 않음.
역방향은 no-op.

---

## 5. 동작 상수 (랩 `config.py` ↔ 운영 `spam_classifier.py` — 값 동일)

| 상수 | 값 | 의미 / 근거 |
|---|---|---|
| `MIN_LEN_FOR_LLM` | `3` | 3자 미만은 LLM 없이 clean(이모지·"👍"). ⚠️ 더 올리면 `캬,COM` 같은 짧은 난독 도메인을 놓친다 |
| `CHAR_CAP` | `500` | LLM 입력 상한. 스팸은 대개 짧고 잘라도 판정 무해 |
| `SPAM_CONFIDENCE_THRESHOLD` | `0.9` | 스팸이라도 이 미만은 숨기지 않음. 2026-07-22 상향(0.7→0.9) — "설치링크 부탁드려요"를 phishing 0.85로 오탐한 사례 |
| `GEMMA_MAX_TOKENS` | `64` | 판정 JSON은 짧다 → 지연 방어. **늘리지 말 것** |
| `GEMMA_MODEL` | `gemma-4` | ★ 교체 금지(비용) |

> 랩 2-7 실측: gemma가 뱉는 distinct confidence가 **0.95 / 1.0 뿐**(degenerate) →
> **threshold 튜닝 헤드룸이 사실상 없다**. 0.9는 안전 여유값이며, 숫자를 흔들어 개선할 여지는
> 없다는 게 측정 결론. 개선은 프롬프트 예시 교체(2-6) 쪽.

---

## 6. 판정 결과 계약 (변경 금지 — 대시보드/로그 파싱이 의존)

### `SpamVerdict` (dataclass)

| 필드 | 타입 | 비고 |
|---|---|---|
| `is_spam` | bool | 최종 판정 |
| `category` | str | `clean\|scam\|adult\|phishing\|promo` + 규칙 히트 시 `"rule"` (v5 에서 `abuse` 제거) |
| `reasons` | list | `contains_url`, `keyword:<kw>`, `llm:<category>`, `<reason 문구>` |
| `confidence` | float | 규칙=1.0, LLM=모델 출력 |
| `engine` | str | 아래 표 |
| `error` | str | fail-open 시 예외 문자열 200자 |

### `engine` 값 사전

| 값 | 의미 | is_spam |
|---|---|---|
| `rule` | 규칙(URL/키워드) 즉시차단 — LLM 미호출 | True |
| `rule_trivial` | 3자 미만 → LLM 스킵 | False |
| `rule_only` | 계정 `use_llm=False` kill-switch | False |
| `llm` | gemma 판정(스팸/정상 양쪽) | True/False |
| `llm_lowconf` | 스팸이라 했지만 conf < 0.9 → **숨기지 않음** | False |
| `llm_failopen` | 예외·타임아웃·파싱실패 → **숨기지 않음** | False |
| `campaign_trigger_exempt` | ★ **운영 전용** — 활성 캠페인 트리거 댓글이라 분류 자체를 스킵 | False |

> ⚠️ **랩 계약 테스트와의 불일치**: 랩 `tests/test_contract.py`의
> `ENGINE_SET`은 6개만 hard-pin하고 있어 `campaign_trigger_exempt`를 모른다.
> 랩의 "이식 드리프트 가드"(1-6)는 이 값에 대해서는 **작동하지 않는다**.

> ✅ **`abuse` enum 구멍 — v5 에서 닫힘(2026-07-31)**: v3 는 "악플은 절대 숨기지 마라"라고
> 지시하면서도 출력 enum 에 `abuse`를 남겨둬서, gemma 가 `category=abuse, is_spam=true,
> conf≥0.9`를 뱉으면 **정책과 반대로 숨겨지는** 경로가 열려 있었다. v5 는 enum 을
> `<clean|scam|adult|phishing|promo>` 로 좁히고 "abuse(악플) 카테고리는 쓰지 마라"를 명시해
> 이를 구조적으로 막았다. 회귀 가드: `test_prompt_policy_abuse_is_not_spam`.
>
> 운영 영향 없음: `spam_category` 는 자유 CharField(`str(...)[:32]`)이고, 운영에서 `abuse` 를
> 참조하는 곳은 `help_text`/docstring 의 "…promo/abuse 등" 표기뿐이라 파싱·대시보드가 깨지지
> 않는다. 다만 랩 `tests/test_contract.py` 의 `CATEGORY_ENUM` 은 여전히 `abuse` 를 hard-pin
> 하므로, 랩에서 v5 를 정식 승격할 때 그 핀을 함께 정리해야 한다.

### `SpamCommentLog.Status` 상태 머신

```
CLEAN  ─(스팸 확정)→ DETECTED ─(auto_hide + Meta 성공)→ HIDDEN
 │                       │
 │                       └─(Meta 실패)→ FAILED ──(유저 수동 재시도)→ HIDDEN
 └─ 48h TTL 후 삭제 (멱등 장부 용도라 오래 보관 불필요, 통계 제외)
```
- `CLEAN`은 **잠정 기본값**. 크래시로 중단돼도 오탐 감지로 집계되지 않는다.
- `DETECTED/HIDDEN/FAILED`는 통계·감사용으로 **보존**(TTL 없음).
- 어드민 집계 정의: `SPAM_DETECTED_STATUSES = detected/hidden/failed` (clean 제외).

---

## 7. 랩에 있는데 운영에 없는 것 (전부 의도적)

### 7-1. 퍼지/난독화 URL SOFT 신호 (랩 2-3) — 미이식

랩 `rules.py`에 있고 운영에 없다:

```python
_FUZZY_DOT_ASCII_RE  = r"[0-9a-zA-Z가-힣-]{2,}[점쩜]\s*(?:com|net|kr|ly|io|xyz)\b"   # 'bit점ly'
_FUZZY_DOT_HANGUL_RE = r"[0-9a-zA-Z가-힣-]{2,}[점쩜](?:컴|넷)(?=[^가-힣]|$)"
_SPACED_TLD_RE       = r"…[0-9가-힣]…{2,}\.\s+(?:com|net|kr|io|xyz)\b"              # '426금. Net'
def soft_signals(text) -> list[str]   # ["soft:주소창", "soft:fuzzy_url", ...]
```

**미이식이 무해한 이유**: `soft_signals()`는 설계상 **차단에 쓰지 않는 진단/감사용 메타데이터**다
(랩 주석: "차단 판단에 쓰지 말 것. 프롬프트 주입도 금지"). 운영에 넣어도 판정 결과가 바뀌지
않는다. 넣을 가치는 "왜 이 댓글이 gemma로 갔는지"를 로그에서 보는 관측성뿐.
→ **판정 동등성에는 영향 없음.** 필요해지면 `SpamCommentLog`에 진단 필드 추가가 선행돼야 함.

### 7-2. `validate_keywords()` 키워드 안전 lint (랩 2-2 후반) — 미이식

계정 키워드에 일상어를 넣으면 그 계정 정상 댓글이 **구제 불가로 숨는다**. 랩은 advisory 경고
목록을 반환한다(1자 키워드 위험, 중복, SOFT 티어 일상어 경고).
→ 운영에는 이 lint가 없어서, 오너가 스팸 키워드 UI에 '사건'·'ㅎㅇ'·'검색' 같은 걸 넣으면
**경고 없이 하드블록이 된다.** 실제로 `0045` 주석이 "오너가 '검색'·'ㅎㅇ' 추가한 행"을 언급하고
있으니 이미 발생한 패턴이다.
→ **이식 가치 있는 유일한 미결 항목.** 판정 로직 변경 없이 `SpamFilterViewSet` config 저장
응답에 `warnings[]`만 얹으면 되고(차단·거부 아님), 프론트가 표시하면 끝.

### 7-3. 프롬프트 v5 — **운영자 지시로 채택·배포됨 (2026-07-31)**

v5 는 랩 `prompt_variants.py` 의 `_V5_SPAM_ONLY` 에만 있고 랩 `prompt.py` 의
`SPAM_SYSTEM_PROMPT` 로 **정식 승격되지는 않은 상태**였다(랩 주석: *"v4 계열이라 여전히
§2(≤600토큰)를 넘는다 → 프롬프트 단독 비교/탐색용, **운영 즉시 이식용 아님**"*).
그래도 `POLICY.md` 가 v5 를 최선으로 평가하고 있어, **운영자 판단으로 예산 초과를 수용하고
채택**했다.

| | v3 (구) | v5 (현행 운영) |
|---|---|---|
| 정책(악플→clean) | ✅ | ✅ (+ enum 에서 `abuse` 제거) |
| 난독화 복원 few-shot (`캬,COM`, `진용진19 . C O m。`, `송 하 리 6,9,2,5`) | ❌ | ✅ |
| 간접 유인 판정(목적지+보상 결합) | ❌ | ✅ |
| 토큰 예산 §2 (~600) | ✅ 590 | ❌ **1038 (초과, 수용)** |
| 랩 자체 승격 | ✅ `SPAM_SYSTEM_PROMPT` | ⚠️ variants 에만 등록 |
| 운영 적용 | ⬅ 교체됨 | ✅ **현행** |

**남은 정리 과제(랩 측)** — 운영은 이미 v5로 돌지만 랩은 아직 v3 기준이다:
1. 랩 `prompt.py` 의 `SPAM_SYSTEM_PROMPT` 를 v5 로 승격 + `PROMPT_VERSION`/CHANGELOG 갱신
   (그래야 `--ping`·`--batch` 가 운영과 같은 프롬프트를 측정한다. 현재 랩 `--ping` 은 여전히
   v3 590 tokens 를 보고한다).
2. `tests/test_contract.py` 의 `CATEGORY_ENUM` 에서 `abuse` 핀 정리.
3. `runs/experiments.jsonl` 에 v5 기준 배치 기록 추가.
4. 여력이 되면 v5 를 600 토큰 이하로 압축(few-shot 6종 축약) → §2 복귀.

**롤백 절차**: `_SPAM_SYSTEM_PROMPT` 를 v3(`5e6b06680d30`)로 되돌리고,
`test_prompt_byte_identical_to_lab` 의 sha 핀과 `test_prompt_biases_leadgen_short_comments_clean`
/`test_prompt_policy_abuse_is_not_spam` 의 한국어 단언을 영어판으로 함께 되돌린다.
`test_prompt_v5_deobfuscation_and_indirect_lure` 는 삭제.

### 7-4. 폐기·보류 (참고 — 운영에 없어야 정상)

- **v2** (`v2-abuse-contrast`): abuse 검출 방향 → 운영자 정책과 **반대**라 같은 날 폐기.
- **v4** (`v4-prompt-only`): 난독화 복원은 강하지만 abuse 과차단으로 FP 급증 → 폐기.
- **2-5** 출력 포맷 강화: 녹화 전 구간에서 **파싱 실패 0건** → 측정되는 이득 없이 토큰만 소모.
  보류. `llm_failopen`이 실측되면 재개.

---

## 8. 운영에만 있는 것 (랩에 없음 — 랩 결과로 판단하면 안 되는 부분)

랩은 **판정 함수 단독**만 재현한다. 운영 파이프라인의 앞단 가드는 랩 데이터셋 측정에 들어가지
않으므로, "랩에서 이 댓글이 스팸으로 나왔다"가 곧 "운영에서 숨겨진다"는 뜻이 아니다.

| # | 운영 전용 장치 | 효과 |
|---|---|---|
| 1 | **캠페인 트리거 면제** (`campaign_trigger_exempt`) | 활성 캠페인을 실제 발동시키는 댓글은 분류 자체를 스킵. `ecf9d6b` — 2026-07-21 3dragon_pd에서 detected 36건 중 최소 10건이 **실제 DM 발송(read/delivered)된 정상 팬 댓글**이었던 회귀 방지 |
| 2 | **광고 media_id 정규화** (`original_media_id` 우선) | 없으면 광고 유입 정상 트리거 댓글이 면제를 못 받고 숨겨진다 |
| 3 | **self 가드** (`from_user_id == entry_id`) | POLICY 규칙 2 구현. 우리 자동 답글도 같은 계정에서 나오므로 함께 걸러짐 |
| 4 | **플랜 게이트** `owner_has_feature(ws,"spam_filter")` (fail-closed) | 프로 전용. 어드민 유저는 뷰 계층에서 우회 허용 |
| 5 | **`is_active=False` 소프트 비활성 계정 제외** | 비활성 IG 계정은 검사 안 함 |
| 6 | **`auto_hide_enabled` 기본 False** | 계정 전체 검사로 전환됐으므로 기본 OFF — 감지만 기록, 유저 수동 숨김 |
| 7 | **`use_llm` 계정별 kill-switch** | gemma 롤아웃 안전장치 → `engine="rule_only"` |
| 8 | **멱등 claim** `UNIQUE(spam_filter, comment_id)` | 중복 웹훅에도 1회만 분류/숨김 |
| 9 | **연결 다중 순회 + 예외 격리** | 같은 IG 계정이 여러 워크스페이스에 연결된 경우 각각 적용, 한 연결 실패가 다른 연결을 막지 않음 |
| 10 | **모더레이션 연타 제한** (`rate_governor.moderation_action_check`) | 수동 hide/unhide 스로틀, `moderation_rate_limited` + `retry_after` |

**해석 주의**: 위 1·2·3·6 때문에 운영의 실효 FP는 랩 CFPR보다 **더 낮다**. 반대로 4·5·7 때문에
**아예 판정이 안 도는 계정**도 있다. 랩 수치를 운영 지표로 직접 읽으면 안 된다.

---

## 9. 데이터 모델

### `SpamFilterConfig` (`spam_filter_configs`) — IG 연결당 1개(OneToOne)

| 필드 | 기본값 | 비고 |
|---|---|---|
| `status` | `inactive` | `active`일 때만 검사 |
| `spam_keywords` | `[]` | JSON. 비어있으면 코드 `HARD_BLOCK_KEYWORDS` 폴백. **값이 있으면 authoritative 하드블록** |
| `block_urls` | `True` | URL/도메인 규칙 on/off |
| `auto_hide_enabled` | **`False`** | off면 감지 기록만 → 수동 숨김 대기 |
| `use_llm` | `True` | off면 규칙만(gemma kill-switch) |
| `total_spam_detected` / `total_hidden` | 0 | 누적 카운터 |

제약: `UniqueConstraint(ig_connection)` / 인덱스 `(ig_connection, status)`

### `SpamCommentLog` (`spam_comment_logs`)

`comment_id`, `comment_text`, `commenter_user_id`, `commenter_username`, `media_id`,
`status`, `spam_reasons`, `spam_category`, `confidence`, `engine`, `webhook_payload`,
`hidden_at`, `created_at`

- 제약: `UniqueConstraint(spam_filter, comment_id)` = `uq_spam_log_filter_comment` (멱등 핵심)
- 인덱스: `(spam_filter,status)`, `comment_id`, `created_at`, `hidden_at`
- `webhook_payload`는 **스팸 확정 시에만** 저장(감사용)

---

## 10. API 표면

### 사용자 (`/api/v1/integrations/spam-filters/`, JWT + 워크스페이스 멤버십 + 프로 게이트)

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `ig-connections/{ig_connection_id}/` | 설정 조회(없으면 생성) |
| PUT/PATCH | `ig-connections/{ig_connection_id}/` | 키워드·URL차단·auto_hide·use_llm 수정 |
| POST | `ig-connections/{ig_connection_id}/activate/` | 활성화 |
| POST | `ig-connections/{ig_connection_id}/deactivate/` | 비활성화 |
| GET | `ig-connections/{ig_connection_id}/logs/` | 감지 로그(원본 payload 포함) |
| POST | `logs/{log_id}/hide/` | 수동 숨김 (연타 제한) |
| POST | `logs/{log_id}/unhide/` | 숨김 해제 (연타 제한) |

미보유 플랜 → **403** `{"feature":"spam_filter"}` / 연타 → **`moderation_rate_limited` + `retry_after`**

### 어드민 (`GET /api/v1/admin/spam/logs/`, `IsAdminUser`)

- 운영 대시보드 `스팸 방어` 카드의 "자세히 보기". 키셋 커서 페이지네이션(base64 `(created_at,id)`).
- **정합성 계약**: `status` 미지정 시 `total`이 같은 기간 `dashboard/operations`의 `spam.detected`와
  **정확히 일치**(양쪽 `SPAM_DETECTED_STATUSES` + `[since, until)`). 기간 헬퍼는 `dashboard_ops` 재사용(복제 금지).
- RBAC: `marketing_viewer`는 미들웨어에서 **403** — 댓글 원문·오너 이메일이 담기므로 외주 계정 차단.

---

## 11. 배치 / 유지보수

| 태스크 | 주기 | 스팸 관련 동작 |
|---|---|---|
| `integrations.cleanup_comment_ledger` | 매일 | `SpamCommentLog(status=CLEAN)` **48h** 경과분 삭제(5000건 배치 루프). detected/hidden/failed는 보존 |

---

## 12. ⚠️ 함정: TikTok / YouTube는 **다른 엔진**을 쓴다

| 플랫폼 | 판정 엔진 | LLM |
|---|---|---|
| **Instagram** | `apps/integrations/services.py::SpamDetectionService` + `spam_classifier.py`(gemma) | ✅ gemma-4 |
| **TikTok / YouTube** | `apps/core/spam_detection.py::detect_spam()` (순수 휴리스틱) | ❌ 없음 |

`apps/core/spam_detection.py`는 **완전히 별개 구현**이다 — 자체 `_URL_RE`(TLD 목록이 더 넓음:
`me/ly/tv/to/shop/store/dev` 포함), URL 단축기 도메인 목록(`bit.ly`·`t.co`·…), 이모지 비율,
@멘션/#해시태그 개수 휴리스틱, `unicodedata` 정규화. **랩의 2-티어·단어경계·v3 프롬프트가
전혀 적용되지 않았고**, 랩 연구 범위도 아니다(랩은 IG 댓글 전용).

→ "스팸 필터 고쳤다"고 할 때 **어느 엔진인지 반드시 구분**할 것. IG 개선이 TikTok/YouTube에
자동 반영되지 않으며, `apps/core/spam_detection.py`는 IG 경로에서 **호출되지 않는다**.

---

## 13. 테스트 커버리지

| 파일 | 줄 | 범위 |
|---|---|---|
| `apps/integrations/test_spam_filter_decouple.py` | 758 | `classify_comment` 4경로, 2-티어, 단어경계, self_comment, 캠페인 면제, 플랜 게이트, 멱등 |
| `apps/admin_api/tests_rbac_and_spam.py` | 734 | 어드민 로그 API + RBAC 403 + 대시보드 정합성 |
| `apps/core/tests/test_spam_detection.py` | 83 | **TikTok/YouTube용** 휴리스틱 엔진(IG 무관) |
| 랩 `tests/` (8파일) | — | 계약 hard-pin, 데이터셋 lint, 오프라인 replay CI |

---

## 14. 재검증 절차 (랩 → 운영 이식 체크리스트, PORT.md)

```bash
cd ../spam_filter_lab
python cli.py --ping                                          # 게이트웨이 + 실측 토큰 ≤600
python -m pytest tests/ -q                                    # 계약·데이터셋 lint 전부 green
python cli.py --batch datasets/samples.jsonl --max-clean-fp 0  # CORE Clean-FP 게이트
python cli.py --batch datasets/golden_v1.jsonl                # CFPR 비악화
python cli.py --batch datasets/hard_cases.jsonl
python cli.py --no-rules --batch datasets/samples.jsonl        # gemma 단독 성능
```

프롬프트 변경이면 → `PROMPT_VERSION` 올리고 CHANGELOG(**문자열 밖!**) 갱신 →
운영 `_SPAM_SYSTEM_PROMPT`에 **문자열만** 복사 → sha256 대조로 바이트 동일 확인.
상수 변경이면 → 근거 실험(`runs/experiments.jsonl`의 ts/sha)을 PR 설명에 첨부. **근거 없는 숫자 변경 금지.**

### 절대 하지 말 것

- ❌ gemma-4 → 외부 유료 모델 교체 (댓글 웹훅마다 과금)
- ❌ `soft_signals()`를 차단 판단에 사용 (2-티어가 무의미해짐)
- ❌ `soft_signals()`/버전 문자열을 프롬프트에 주입 (매 댓글 prefill 증가, §2 위반)
- ❌ `engine` 값 / `category` enum / `SpamVerdict` 필드 변경 (로그·대시보드 계약)
- ❌ 운영 프롬프트를 랩 A/B 없이 직접 수정
- ❌ 하드블록 표면 확대 (규칙 오탐은 fail-open으로 구제 불가)

---

## 부록 A. 파일 맵

| 역할 | 경로 |
|---|---|
| 하이브리드 판정 + 시스템 프롬프트 | `apps/integrations/spam_classifier.py` |
| 규칙 pre-filter (2-티어·단어경계·URL) | `apps/integrations/services.py:1489` `SpamDetectionService` |
| 웹훅 파이프라인 + 캠페인 면제 | `apps/integrations/tasks.py:869` `run_spam_filter_check`, `:987` `_run_spam_for_connection`, `:965` `_comment_triggers_active_campaign` |
| 웹훅 디스패치 | `apps/integrations/views.py:4670` |
| 사용자 API | `apps/integrations/views.py:4703~` `SpamFilterViewSet` |
| 어드민 API | `apps/admin_api/views/spam.py`, `apps/admin_api/serializers/spam.py` |
| 모델 | `apps/integrations/models.py:1785` `SpamFilterConfig`, `:1876` `SpamCommentLog` |
| 마이그레이션 | `0005`(생성) · `0031`(LLM 분리) · `0045`(시드 키워드 리셋) |
| TTL 정리 | `apps/integrations/tasks.py::cleanup_comment_ledger` |
| **별개 엔진**(TikTok/YouTube) | `apps/core/spam_detection.py` |
| 랩 | `../spam_filter_lab/` — `POLICY.md`(정책) · `CONSTRAINTS.md`(제약) · `PORT.md`(이식) · `ROADMAP.md`(전체) |

## 부록 B. 관련 커밋 연혁

| 커밋 | 날짜 | 내용 |
|---|---|---|
| `852963b` | — | 스팸 필터 시스템 최초 구현 |
| `0edb44b` | — | 휴리스틱 룰 엔진 `apps/core/spam_detection.py` 추가 (TikTok/YouTube용) |
| `45e0db9` | — | 스팸 댓글 필터(LLM 하이브리드) + 수동 모더레이션 연타 제한 |
| `ecf9d6b` | — | **활성 캠페인 트리거 댓글 분류 면제** |
| `11be1dc` | 07-22 | 리드젠 짧은 트리거 오탐 방지 — 프롬프트 재작성 + **문턱 0.7→0.9** |
| `41fe0ed` | 07-24 | **spam-lab 이식 — 프롬프트 v3 + 키워드 2-티어** (+ 마이그 `0045`) |
| (이 변경) | **07-31** | **프롬프트 v3 → v5 교체** (sha `69ef69bbd8fb`, 1038 tokens, `abuse` enum 제거) |
| `7d4593f` | 07-27 | 어드민 스팸 로그 API + RBAC (OPS-3, RBAC-2) |
