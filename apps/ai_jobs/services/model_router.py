"""
작업 유형 + 모델 티어별 모델 선택.

job_type · model_tier에 따라 사용할 LLM 모델을 결정한다.
"""

# model_tier → LiteLLM model name
TIER_MODEL_MAP: dict[str, str] = {
    "basic": "gemma-4",
    "pro": "gpt-5.4",        # 개발 중
    "pro_plus": "gpt-5.4",   # 개발 중
}


def resolve_model(job_type: str, model_tier: str = "basic") -> str:
    """job_type + model_tier에 해당하는 LLM 모델 이름 반환."""
    return TIER_MODEL_MAP.get(model_tier, "gemma-4")
