"""
LLM 모델 선택.

AiJob.llm_model 값을 실제 LiteLLM 모델명으로 변환한다.
"""

from apps.ai_jobs.models import AiJob

DEFAULT_MODEL = "gemma-4"


def resolve_model(llm_model: str) -> str:
    """llm_model 코드에 해당하는 실제 LLM 모델 이름 반환."""
    return AiJob.LLM_MODEL_MAP.get(llm_model, DEFAULT_MODEL)
