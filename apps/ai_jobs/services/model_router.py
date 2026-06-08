"""
LLM 모델 선택.

AiJob.llm_model 값을 실제 LiteLLM 모델명으로 변환한다.
"""

from decouple import config

from apps.ai_jobs.models import AiJob

DEFAULT_MODEL = "deepseek"

# 이미지 라벨링(비전)용 모델.
# deepseek-v4-flash 는 image_url 을 무시하는 사실상 텍스트 전용이라(모델이 "이미지가 없다"고 응답),
# 라벨링에 쓰면 role/usable/summary/mood_notes 가 전부 텍스트만으로 환각된다.
# 자체 호스팅 gemma-4 vLLM 은 멀티모달이 동작하므로 비전이 필요한 호출은 항상 이 모델로 보낸다.
VISION_MODEL = config("AI_VISION_MODEL", default="gemma-4")


def resolve_model(llm_model: str) -> str:
    """llm_model 코드에 해당하는 실제 LLM 모델 이름 반환."""
    return AiJob.LLM_MODEL_MAP.get(llm_model, DEFAULT_MODEL)


def resolve_vision_model() -> str:
    """이미지 라벨링 등 비전 입력이 필요한 호출용 모델명.

    사용자가 고른 생성 모델(deepseek 등)과 무관하게 항상 멀티모달 지원 모델을 쓴다.
    """
    return VISION_MODEL
