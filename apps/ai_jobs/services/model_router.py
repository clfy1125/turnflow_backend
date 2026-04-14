"""
작업 유형별 모델 선택.

job_type에 따라 사용할 LLM 모델을 결정한다.
나중에 사용자 플랜별 모델 분기도 여기서 처리.
"""

# job_type → LiteLLM model name
JOB_MODEL_MAP: dict[str, str] = {
    "bio_remake": "gemma-4",
    "theme_generation": "gemma-4",
    "copy_generation": "gemma-4",
}

# 나중에 프리미엄 플랜용 매핑
# PREMIUM_MODEL_MAP = {
#     "bio_remake": "gpt-4.1",
# }


def resolve_model(job_type: str) -> str:
    """job_type에 해당하는 LLM 모델 이름 반환."""
    return JOB_MODEL_MAP.get(job_type, "gemma-4")
