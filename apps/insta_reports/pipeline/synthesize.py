"""S6 인사이트 합성 — DeepSeek V4 Pro 1콜, 집계 JSON만 입력, 가드레일 고정.

프롬프트는 계정 무관 고정 텍스트(범용성) — 계정 정보는 input JSON 으로만 진입.
출력 = 서술 슬롯 JSON (검증 게이트 통과 후에만 렌더러로).
"""

import json
import re

import requests

from . import config
from .costs import CostLedger

PROMPT_VERSION = "synth_v3"

SLOTS_SPEC_V3 = """아래는 **출력 형식 예시**입니다. 이 구조 그대로, 값만 바꿔서 순수 JSON 하나만 출력하세요.
주석·설명·"×3" 같은 표기를 JSON 안에 절대 쓰지 마세요. 배열 개수는 그 아래 [개수 규칙]을 따르세요.

{
 "top3": [
   {"headline": "한 줄 제목", "body": "설명 문장", "tone": "good",
    "evidence_keys": ["views_stats.median"],
    "numbers_used": [{"value": 1.8, "unit": "배", "metric_key": "derived.ratios"}]}
 ],
 "monthly_observation": {"text": "문장", "evidence_keys": ["monthly"], "numbers_used": []},
 "success_formula": {
   "persona": {"text": "문장", "evidence_keys": ["comment_stats"], "numbers_used": []},
   "winning_pattern": {"text": "문장", "evidence_keys": ["hook_table"], "numbers_used": []}},
 "hook_note": {"text": "문장", "evidence_keys": ["hook_table"], "numbers_used": []},
 "opening_note": {"text": "문장", "evidence_keys": ["opening_table"], "numbers_used": []},
 "top_posts_why": [ {"rank": 1, "why": "첫 3초에 왜 멈춰 볼 수밖에 없었는지"} ],
 "low_line": {"text": "문장", "evidence_keys": ["low_posts_features"], "numbers_used": []},
 "fans_wants": [ {"title": "제목", "quote_id": "입력의 quote_id 그대로", "note": "문장"} ],
 "opportunities": [ {"text": "문장", "evidence_keys": ["comment_stats"], "numbers_used": []} ],
 "motivation_descs": [ {"key": "practical", "desc": "문장"} ],
 "strengths": [ {"value": "9.2배", "title": "짧은 제목", "desc": "문장",
                 "evidence_keys": ["derived.ratios"], "numbers_used": []} ],
 "positioning": {"oneliner": "한 문장", "body": "설명"},
 "formula_rows": [ {"phase": "0~3초", "action": "할 것", "detail": "부연", "example": "실제 예"} ],
 "recommendations": [
   {"title": "제목", "what_to_do": "무엇을 어떻게 할지",
    "why": "왜 그래야 하는지 (이 계정 숫자로)",
    "evidence_line": "항목A 숫자(영상 N개) vs 항목B 숫자(영상 M개)",
    "priority": "high", "basis": "data_observation",
    "evidence_keys": ["opening_table"], "numbers_used": []}
 ],
 "checklist": [ "~나요?로 끝나는 질문" ]
}

[개수 규칙] top3=3개 / top_posts_why=입력 top_posts_meta의 모든 rank / fans_wants=3개 /
opportunities=1~2개 / motivation_descs=4개(practical·question·wow·fan 각 1개) /
strengths=3개 / formula_rows=3~4개 / recommendations=5~6개 / checklist=6~8개
[길이 규칙] headline≤30 body≤140 / monthly_observation≤200 / persona≤230 winning_pattern≤260 /
hook_note·opening_note≤170 / why 130~210자(짧으면 반려) / low_line≤150 / fans_wants.title≤16 note≤130 /
opportunities≤190 / motivation_descs.desc≤95 / strengths value≤9 title≤17 desc≤135 /
positioning.oneliner≤42(따옴표 없이) body≤230 / formula_rows action≤42 detail≤64 example≤64 /
recommendations title≤22 what_to_do≤150 why≤200 evidence_line≤130 / checklist 각 ≤45자
[tone] good(긍정) | warn(주의) | neutral(중립)
[priority] high(먼저 할 것) | mid(추천)
[basis] data_observation(내 데이터에서 확인) | experiment_suggestion(해보고 확인) | general_guide(일반 조언)"""

V3_PLAIN_RULES = """
## ★ 가장 중요한 규칙 — 쉬운 말로만 쓰기 (위반하면 전부 반려됩니다)
이 리포트를 읽는 사람은 **마케팅·데이터 용어를 전혀 모르는 인스타 운영자**입니다.
중학생이 읽어도 바로 이해되는 문장만 쓰세요. 한 번 읽고 무슨 말인지 모르면 실패입니다.

**절대 쓰지 마세요 (전문용어·업계용어·한자어)**
CTA, 캡션, 훅/후킹, 오프닝, 인트로, 썸네일, 표본, 중앙값, 평균값, 잠재력, 파괴력,
폭발력, 최적화, 고도화, 제고, 증대, 극대화, 의무화, 필수화, 패턴화, 포지셔닝,
페르소나, 퍼널, 리텐션, 인게이지먼트, 바이럴, 리드, 컨버전, 트래픽, 니즈, 타깃팅,
벤치마킹, 프레임워크, 시너지, 유의미, 상관관계, 변동성, 시그널, 알고리즘, 전환율,
도달, 저장수, 시청시간
**분류 줄임말도 쓰지 마세요**: "말하는 얼굴", "결과제시형", "손실회피형", "질문형",
"설명형", "리스트형" 등 — 반드시 입력의 name 을 통째로 쓰세요
(예: "사람이 카메라를 보고 말하며 시작하는 영상").
**영어 단어 금지**: 문장에 영어를 섞지 마세요. 도구 이름(ChatGPT 등)만 예외입니다.

**이렇게 바꿔 쓰세요**
- "CTA를 넣으세요" → "영상 끝에 '댓글에 OO 남겨주세요'라고 부탁하세요"
- "훅이 강한 영상" → "첫 마디가 궁금하게 만드는 영상"
- "표본이 적어" → "영상이 6개뿐이라"
- "대박 잠재력이 9.2배" → "가장 잘됐을 때 조회수가 9.2배 높았어요"
- "의무화하세요" → "모든 영상에 꼭 넣어보세요"
- "인물 행동 오프닝" → "손동작이나 시연 장면으로 시작하는 영상"

**분류 이름은 입력에 적힌 이름을 그대로 쓰세요.**
입력의 hook_table·opening_table 각 항목에는 `name`(사람말 이름)이 들어 있습니다.
그 `name`을 문장에 그대로 쓰고, 영어 키(loss_aversion 등)를 번역해서 쓰지 마세요.

**실제 문구를 인용할 때는 반드시 따옴표로 감싸세요.**
영상 제목·첫 문장·댓글을 인용할 때 '이렇게' 따옴표 안에 넣으세요. 따옴표 안은 원문이라
검사에서 제외됩니다. 따옴표 없이 쓰면 반려될 수 있습니다.

**어떤 영상인지 알 수 있게 쓰기 (필수)**
특정 영상을 가리킬 때 '이 영상'·'그 영상'이라고만 쓰면 읽는 사람은 어느 게시물인지 찾을 수 없습니다.
→ **'1위 영상'처럼 순위로 지목**하세요(리포트의 잘된 게시물 카드에 붙은 순위와 같습니다).
   필요하면 올린 날짜(예: 10월 20일)를 덧붙여도 됩니다.
   ⚠️ 순위·날짜 말고 다른 숫자를 새로 만들어 쓰면 반려됩니다.
"""

V3_EXTRA_RULES = """
## v3 추가 규칙
- fans_wants.quote_id: 입력 comment_stats.quote_pool 에 있는 quote_id 를 그대로 복사하세요. 원문은 코드가 끼워 넣습니다. 없는 id·직접 쓴 인용문은 자동 반려.
- top_posts_why.chips: 입력 top_posts_meta[해당 rank].allowed_chips 목록 안에서만 고르세요(그 게시물에서 실제 관찰된 특징만). why 문장도 그 게시물의 hook_text/opening_desc 관찰에 근거하세요.
- motivation_descs: pct 숫자는 쓰지 마세요(코드가 표시). 각 동기가 어떤 댓글에서 보였고 어떤 의미인지 1문장.
- 댓글 관련 주장(비율·개수)은 comment_stats 의 counts/pcts/save_mentions 값만 인용.
- has_features=false 인 top 게시물의 why 는 화면·첫 문장 언급 금지(캡션·반응 수치만).
- formula_rows.example: 입력의 hook_text·good_hooks·cta 인용에 실제로 나온 표현을 활용하세요. 지어내지 마세요.
"""

SLOTS_SPEC = """{
 "top3": [ {"headline": "≤30자", "body": "≤140자", "tone": "good|warn|neutral",
            "evidence_keys": ["집계 경로 ≥1"], "numbers_used": [{"value": 1.8, "unit": "만|천|%|배|개|회|raw", "metric_key": "경로"}] } ×3 ],
 "monthly_observation": {"text": "≤180자", "evidence_keys": [], "numbers_used": []},
 "low_line": {"text": "≤100자", "evidence_keys": [], "numbers_used": []},
 "hook_note": {"text": "≤120자", "evidence_keys": [], "numbers_used": []},
 "kw_recommendation": {"text": "≤160자", "evidence_keys": [], "numbers_used": []},
 "account_themes": [ {"id": "t1", "label": "≤10자"} ×3~5 ],
 "top_post_themes": [ {"rank": 1, "theme_id": "t1"} — top_posts 전 rank 커버 ],
 "recommendations": [ {"title": "≤22자", "body": "≤160자", "priority": "high|mid",
                       "basis": "data_observation|experiment_suggestion|general_guide",
                       "evidence_keys": [], "numbers_used": []} ×4~6 ],
 "checklist": [ "≤40자, 반드시 '~나요?' 또는 '~까요?'로 끝" ×6~8 ]
}"""

SYSTEM_PROMPT = (
    """당신은 인스타그램 계정주에게 보내는 "내 인스타 성장 리포트"의 해석 문장 작성자입니다. 숫자 계산과 표는 이미 코드가 끝냈고, 당신은 입력 집계 JSON만 보고 "그래서 뭐가 중요한지"를 짧은 한국어 문장으로 씁니다. 영상 원본은 볼 수 없고, 입력에 없는 사실은 존재하지 않는 것으로 간주하세요.

## 1. 말투
- 존댓말 해요체: "~했어요 / ~이에요 / ~해보세요". 짧고 구체적으로, 한 문장에 주장 하나.
- 매 비교 주장에 숫자와 표본 수(영상 N개)를 붙이세요.
- 실행 가능한 다음 행동으로 끝나는 문장을 선호하세요.
- 이모지·HTML·줄바꿈·마크다운 금지.

## 2. 용어 사전 (왼쪽 금지 → 오른쪽 사용)
중앙값/median → 평소 조회수 | 평균/mean → 터진 영상 포함 평균 | CTA → 댓글 유도 | 훅/hook → 첫 문장 | 분위수/P50 → (언급 금지) | 참여율 → 조회 100회당 좋아요·댓글 | 알고리즘 최적화/도달 → (언급 금지)
큰 수는 "만" 표기: 467359 → 46.7만. 1만 미만은 원값 그대로.

## 3. 숫자 규칙 (위반 시 자동 반려)
- 문장 속 모든 수치는 입력 JSON에 있는 값 또는 derived.ratios/pcts 의 사전 계산값만. 직접 계산·추정·외부 벤치마크 금지.
- 배수(N배)·퍼센트(%)는 derived 에 있는 것만 인용.
- 쓴 숫자는 전부 numbers_used 에 {value, unit, metric_key}로 신고. 신고 없는 숫자는 반려됩니다.
- 한글 수량어 금지: "절반"→"50%", "두 배"→"2배", "수십만"·"과반" 사용 금지. 범위 표기("1~2만") 금지, 단일 값만.
- low_sample=true 인 셀은 비교 근거로 쓰지 마세요(언급하려면 "영상 수가 적어 참고용" 단서 필수).

## 4. 가드레일 (위반 시 반려)
1) 상관≠인과: "때문에/덕분에/원인은" 단정 금지. "~영향도 섞여 있을 수 있어요", "~와 함께 나타났어요" 헤지 어법 필수(monthly_observation·recommendations 의 인과성 주장에 최소 1회).
2) 표본 수 명시: 그룹 비교 시 각 그룹 영상 개수를 문장 안에.
3) 진단 금지: 섀도밴·알고리즘 처벌/제한·계정 제재·노출 억제 언급 금지. 미포함 지표(도달·저장·시청시간·팔로우 전환) 기반 주장 금지.
4) basis 정확히: data_observation(이 데이터에서 직접 관찰) / experiment_suggestion(암시되나 미검증→실험 제안) / general_guide(일반 조언, 숫자 인용 금지). data_observation 최소 2개.
5) 홍보 금지: 턴플로우/타 서비스 언급 금지.
6) 계정 특성 하드코딩 금지: 입력 데이터에 실제로 등장한 주제·키워드만 사용.

## 5. 슬롯별 지시
- top3: 가장 중요한 발견 3가지. 서로 다른 근거 영역(성과 격차/추세/행동 제안). tone: 긍정=good, 주의=warn.
- monthly_observation: 평소 조회수가 가장 크게 변한 구간 1곳의 관찰 + 실험 제안 1개. immature=true 인 달은 "아직 집계 중"으로 취급하고 하락 근거로 쓰지 마세요.
- low_line: 하위 영상들의 공통점 1가지 (video 피처 근거).
- hook_note: 첫 문장 스타일 표의 핵심 대비 1가지.
- kw_recommendation: 댓글 키워드 분포(집중/분산)를 읽고 운영 제안.
- account_themes: 이 계정 콘텐츠 테마 3~5개를 topics_pool·캡션에서 생성(라벨 ≤10자). top_post_themes 로 모든 top_posts rank에 배정(distinct ≤4).
- recommendations: 4~6개, priority high(먼저 할 것) 2~3개. 이 계정 데이터 근거를 앞에, general_guide 는 마지막 1개 이내.
- checklist: 이 계정의 발견과 연결된 업로드 전 자가 점검 질문 6~8개.

출력은 아래 구조의 JSON만. 다른 텍스트 금지.
"""
    + SLOTS_SPEC
)

V3_SLOT_GUIDE = """## 5. 슬롯별 지시 (v3)
- top3: 가장 중요한 발견 3가지, 서로 다른 근거 영역. tone: 긍정=good, 주의=warn.
- monthly_observation: 평소 조회수가 가장 크게 변한 구간 1곳 관찰 + 실험 제안. immature=true 달은 하락 근거 금지. 헤지 표현 필수.
- success_formula.persona: 댓글 내용(comment_stats)과 주제에서 "누가 왜 보는지"를 구체적으로. bio 복붙 금지.
- success_formula.winning_pattern: 상위 영상들의 공통 구성(첫 문장→화면→CTA)을 숫자와 함께.
- hook_note/opening_note: 각 표에서 가장 큰 대비 1개 + 다음 행동.
- top_posts_why: ★가장 공들여 쓸 슬롯입니다. 제공된 모든 rank 에 대해
  **"이 영상은 첫 3초에 시청자가 넘길 수 없게 무엇을 했나"** 를 그 영상만의 구체적 내용으로 쓰세요.
  입력 top_posts_meta 의 first_words(첫 마디 원문), first_screen(첫 화면에 보인 것),
  on_screen_text(화면 자막), value_shown_in_2s, segments(구간 흐름), cta_quotes(요청 문구),
  transcript_short(발화 요약), cut_count_first_10s 를 근거로 삼으세요.
  형식: [첫 3초에 실제로 무엇이 보이고 들렸는지] → [그래서 시청자가 어떤 궁금증·불안·기대를
  느껴 멈췄는지] → [그 뒤 어떻게 끝까지 붙잡고 행동까지 이끌었는지].
  **뻔한 말 금지**: "호기심을 자극했다", "궁금증을 유발했다", "시각적으로 강렬했다",
  "관심을 끌었다", "신뢰를 주었다" 처럼 어느 영상에나 붙는 문장만 쓰면 반려됩니다.
  그 영상의 **실제 문구나 화면**을 최소 1개 따옴표로 인용하세요.
  (좋은 예: "첫 화면부터 천원짜리 옷이 명품처럼 바뀐 결과가 먼저 뜨고 '이게 진짜 AI로 된
   거예요?'라는 자막이 겹쳐요. 결과를 먼저 보여줘 '어떻게 했지'가 궁금해질 수밖에 없고,
   방법은 영상 뒤쪽에 있어 끝까지 보게 됩니다. 마지막에 '댓글에 최고수준' 요청으로 이어져요.")
- low_line: 하위 영상 공통점 1가지.
- fans_wants: 댓글에서 반복된 요구 3가지 — quote_id 로 대표 댓글 지정.
- opportunities: 댓글·영상 데이터가 보여주는 미활용 기회 1~2개 (예: 저장 언급 vs 저장 유도 영상 수).
- motivation_descs: 4개 동기 각각 1문장 설명 (pct 는 코드가 표시).
- strengths: 이 계정이 잘하고 있는 것 3개. value 는 숫자·배수만("27.7배", "+135%").
  desc 는 "무엇이 무엇보다 얼마나 높다"는 사실만 쓰세요. "잠재력·파괴력·강력한·효율" 같은
  꾸밈말 금지 — 숫자와 비교 대상만으로 문장을 끝내세요.
  (좋은 예: "손동작으로 시작한 영상이 말하며 시작한 영상보다 평소 조회수가 27.7배 높았어요.")
- positioning: 경쟁 계정과 다른 이 계정만의 자리 한 문장 + 부연.
- formula_rows: 다음 영상에 바로 쓰는 구성 공식 3~4구간 — example 은 이 계정 영상에서 관찰된 실제 표현으로.
- recommendations: 5~6개, data_observation 최소 3개, high 2~3개.
  각 항목은 3부분으로 나눠 쓰세요 —
  ① what_to_do: 다음 영상에서 손으로 할 수 있는 구체적 행동 (예: "영상 첫 2초를 완성된
     결과물 화면으로 채우고, 그 위에 '이거 10분이면 됩니다' 같은 자막을 넣으세요")
  ② why: 왜 그래야 하는지를 이 계정 숫자로 설명. 무엇과 비교해 얼마나 차이 났는지 반드시 포함.
  ③ evidence_line: 근거 수치만 짧게 나열. 형식 예 —
     "결과물을 먼저 보여주는 영상 12.4만(8개) vs 컴퓨터 화면부터 시작한 영상 4,050(9개)"
  숫자 없이 "좋아요/중요해요" 같은 말만 있으면 반려됩니다(일반 가이드 1개만 예외).
- checklist: 이 계정의 발견과 연결된 질문 6~8개.
"""

SYSTEM_PROMPT_V3 = (
    SYSTEM_PROMPT.split("## 5.")[0]
    + V3_PLAIN_RULES
    + V3_SLOT_GUIDE
    + V3_EXTRA_RULES
    + "\n출력은 아래 구조의 JSON만. 다른 텍스트 금지.\n"
    + SLOTS_SPEC_V3
)


def synthesize(
    agg_input: dict,
    ledger: CostLedger,
    error_feedback: str = "",
    v3: bool = False,
    only_slots: list | None = None,
) -> dict:
    prompt = SYSTEM_PROMPT_V3 if v3 else SYSTEM_PROMPT
    return _synthesize_impl(agg_input, ledger, error_feedback, prompt, only_slots)


# 계정 규모별 조언 방향 — 이게 없으면 모든 계정에 **중간 규모용 조언 하나**만 나간다.
_SCALE_GUIDANCE = {
    "starting": (
        "이 계정은 **막 시작한 단계**입니다(평소 조회수 1,000 미만). "
        "'분석'보다 **횟수와 실험**이 답입니다. 표본이 작아 어떤 비교도 우연일 수 있다는 점을 "
        "분명히 하고, 무엇을 몇 편 더 올려서 무엇을 확인할지의 **실험 계획**을 주세요. "
        "정교한 최적화(올리는 시간·자막 스타일 미세조정) 조언은 지금 단계에서 무의미합니다."
    ),
    "growing": (
        "이 계정은 **성장 중**입니다. 잘된 소수의 영상에서 **반복 가능한 공식**을 찾아내 "
        "그것을 굳히는 데 집중시키세요. 아직 규모가 작아 한두 편의 대박이 평균을 흔든다는 점을 "
        "감안해 '가운데 값' 기준으로 말하세요."
    ),
    "established": (
        "이 계정은 **자리 잡은 단계**입니다. 평균을 올리는 것보다 **하위 영상의 이유를 없애는 것**이 "
        "효율적입니다. 잘되는 패턴은 이미 있으니, 편차를 줄이는 쪽으로 조언하세요."
    ),
    "large": (
        "이 계정은 **대형**입니다(평소 조회수 10만 이상). 이미 도달은 충분하므로 "
        "'조회수를 늘리세요' 류의 조언은 **쓸모없습니다**. 대신 ①이 도달을 무엇으로 전환할지"
        "(팔로워·저장·외부 링크·판매) ②편차와 리스크 관리 ③재현 가능한 제작 공정 "
        "관점으로 조언하세요."
    ),
}
_REACH_GUIDANCE = {
    "explore_driven": (
        "**도달 방식이 핵심 진단입니다**: 평소 조회수가 팔로워 수보다 훨씬 많습니다 — "
        "즉 팔로워 밖(탐색·추천)에서 대부분 보고 있고, **본 사람이 팔로워로 남지 않고 있습니다**. "
        "조회수 늘리기가 아니라 **팔로워 전환**(프로필 첫인상·고정 게시물·시리즈화·영상 안 팔로우 "
        "이유 제시)을 최우선으로 조언하세요."
    ),
    "follower_driven": (
        "**도달 방식이 핵심 진단입니다**: 평소 조회수가 팔로워 수보다 적습니다 — "
        "새 시청자에게 퍼지지 않고 기존 팔로워 안에서만 돌고 있습니다. "
        "**초반 이탈을 줄이고 저장·공유를 유도**하는 쪽으로 조언하세요."
    ),
    "balanced": "",
    "unknown": "",
}


_CONFLICT_RULE = (
    "**갈등을 일부러 만들라고 조언하지 마세요(금지).** 논쟁 댓글이 많다는 관찰은 써도 되지만, "
    "'찬반으로 나뉠 주제를 만들라'·'남녀/세대/지역 갈등 소재를 다뤄라' 류의 제안은 반려됩니다. "
    "댓글이 늘어도 욕설·비방이 함께 늘고 협업·판매에 해가 됩니다. 참여를 늘리려면 "
    "'의견을 묻는 질문'이나 '경험 공유 요청'처럼 갈등 없이 말하게 하는 방법으로 쓰세요."
)


def _audience_guidance(agg_input: dict) -> str:
    """집계에 담긴 계정 성격(scale/reach_mode)에 맞는 조언 방향을 프롬프트에 덧붙인다."""
    aud = (agg_input.get("audience") or {}) if isinstance(agg_input, dict) else {}
    parts = [
        _SCALE_GUIDANCE.get(aud.get("scale"), ""),
        _REACH_GUIDANCE.get(aud.get("reach_mode"), ""),
    ]
    parts.append(_CONFLICT_RULE)
    body = "\n".join(p for p in parts if p)
    return (
        "\n\n## 이 계정에 맞는 조언 방향 (반드시 반영)\n"
        + body
        + "\n위 방향과 어긋나는 일반론은 쓰지 마세요. 숫자는 여전히 입력에 있는 것만 씁니다."
    )


def _synthesize_impl(
    agg_input: dict,
    ledger: CostLedger,
    error_feedback: str,
    system_prompt: str,
    only_slots: list | None = None,
) -> dict:
    user = (
        "아래는 계정의 집계 JSON입니다. 규칙대로 슬롯 JSON을 출력하세요.\n```json\n"
        + json.dumps(agg_input, ensure_ascii=False)
        + "\n```"
    )
    user += _audience_guidance(agg_input)
    if error_feedback:
        user += "\n\n## 이전 시도 반려 사유 — 반드시 고쳐서 다시 쓰세요\n" + error_feedback
    if only_slots:
        user += (
            "\n\n## 이번에는 아래 슬롯만 출력하세요 (나머지는 이미 통과했으니 빼세요)\n"
            + ", ".join(only_slots)
            + "\n해당 키만 담긴 JSON 하나만 출력하세요. 반려 사유를 모두 반영해야 합니다."
        )
    # deepseek-v4-pro 는 reasoning 모델 — reasoning_content 를 먼저 소비하므로
    # max_tokens 를 넉넉히 주지 않으면 content 가 빈 문자열로 옴(finish_reason=length).
    r = requests.post(
        f"{config.DEEPSEEK_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
        json={
            "model": config.SYNTH_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 28000,
            "response_format": {"type": "json_object"},
        },
        timeout=600,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    ledger.record_llm(
        "S6_synthesize",
        config.SYNTH_MODEL,
        u.get("prompt_tokens", 0),
        u.get("completion_tokens", 0),
        note=f"retry={bool(error_feedback)} "
        f"reasoning={u.get('completion_tokens_details',{}).get('reasoning_tokens',0)}",
    )
    msg = d["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    fin = d["choices"][0].get("finish_reason")
    if not content:
        raise RuntimeError(
            f"DeepSeek content 비어있음 (finish_reason={fin}, "
            f"reasoning_tokens={u.get('completion_tokens_details',{}).get('reasoning_tokens')})"
        )
    if content.startswith("```"):
        content = content.strip("`")
        content = content[content.find("{") : content.rfind("}") + 1]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 흔한 오류 자동 교정: // 주석, 스펙 표기(×3), 트레일링 콤마
    cleaned = re.sub(r"//[^\n\"]*", "", content)
    cleaned = re.sub(r"[×xX]\s*\d+(~\d+)?", "", cleaned)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # "Extra data" — JSON 뒤에 잡텍스트가 붙은 경우: 첫 완전한 객체만 취한다
    try:
        start = cleaned.index("{")
        obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        if isinstance(obj, dict) and obj:
            return obj
    except (ValueError, json.JSONDecodeError):
        pass
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        try:  # 디버깅용 원문 보존
            (config.RUNS_DIR / "_last_synth_fail.txt").write_text(content, encoding="utf-8")
        except OSError:
            pass
        # 흔한 원인: 길이 초과로 잘림 → 마지막 완전한 객체까지 복구 시도
        cut = content.rfind("}")
        for end in (cut, content.rfind("]")):
            if end > 0:
                for closing in ("}", "]}", "}]}", '"}]}'):
                    try:
                        return json.loads(content[: end + 1] + closing)
                    except json.JSONDecodeError:
                        continue
        raise RuntimeError(f"JSON 파싱 실패(finish_reason={fin}, {len(content)}자): {e}") from e
