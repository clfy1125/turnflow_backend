"""리포트 생성 파이프라인 (S1 수집 → S8 렌더).

⚠️ 이 패키지의 모듈들은 `insta_report_lab/pipeline/` 에서 검증된 코드를 그대로 이식한 것이다.
    Django ORM 을 쓰지 않고 dict in / dict out + 파일 IO 만 한다(경계 모듈은 config.py 뿐).
    랩에서 개선이 생기면 재복사할 수 있도록 **원본과의 diff 를 최소로 유지**할 것.
    (파일 경로는 config.bind_run() 이 잡별 임시 디렉터리로 갈아끼운다.)
"""
