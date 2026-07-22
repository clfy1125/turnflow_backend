"""DM 캠페인 이전(매니챗 등 → TurnFlow) 분석 파이프라인.

연동된 IG 계정의 최근 게시물·댓글·발신 DM 이력을 분석해, 기존 DM 캠페인으로 보이는
게시물을 찾고 TurnFlow 비활성(INACTIVE) 초안 캠페인 후보로 재구성한다.

모듈:
    collect   — Graph API 수집기(mock 분기·페이서·레이트리밋 분류)
    analyze   — 순수 파이썬(정규화·댓글 증거·DM 템플릿 군집화·매칭)
    llm       — deepseek LLM 4단계(분류/검증/적합도/초안) + FAKE_LLM 휴리스틱
    pipeline  — 오케스트레이터(단계 실행·체크포인트 재개·취소·rate-pause·후보 생성)

설계: 저장소 루트 DM_CAMPAIGN_MIGRATION_FRONTEND.md, CLAUDE.md 문서 인덱스.
"""
