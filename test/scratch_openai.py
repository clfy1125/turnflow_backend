from openai import OpenAI
import os
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# .env 파일에서 OPENAI_API_KEY를 읽어옵니다.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# 검사할 텍스트
text_to_check = "주소창 yako.asia 적은 다음 아이돌A양 사건 원본영상 보면 됨 상당히 크더라 심징 실시간검색오름😍😍"

response = client.moderations.create(
    model="omni-moderation-latest",
    input=text_to_check,
)

# 결과 확인
result = response.results[0]

print(f"유해 콘텐츠 여부: {result.flagged}")
print(f"\n카테고리별 위반 여부:")
categories_dict = result.categories.model_dump()
for category, flagged in categories_dict.items():
    print(f"  {category}: {flagged}")

print(f"\n카테고리별 점수 (0~1):")
scores_dict = result.category_scores.model_dump()
for category, score in scores_dict.items():
    print(f"  {category}: {score:.4f}")
