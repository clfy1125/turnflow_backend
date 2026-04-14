"""
작업 유형별 모델 선택.

job_type에 따라 사용할 LLM 모델을 결정한다.
"""

# job_type → LiteLLM model name
JOB_MODEL_MAP: dict[str, str] = {
    "bio_remake": "gemma-4",
    "theme_generation": "gemma-4",
    "copy_generation": "gemma-4",
}

DEFAULT_MODEL = "gemma-4"


def resolve_model(job_type: str) -> str:
    """작업 유형에 해당하는 LLM 모델 이름 반환."""
    return JOB_MODEL_MAP.get(job_type, DEFAULT_MODEL)
