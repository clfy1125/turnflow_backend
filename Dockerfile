# Python 3.11 slim 이미지 사용
FROM python:3.11-slim

# 환경 변수 설정
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 작업 디렉터리 설정
WORKDIR /app

# 시스템 의존성 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    postgresql-client \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python 의존성 설치
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# 애플리케이션 코드 복사
COPY . .
# GeoLite2 Country DB 다운로드 (MaxMind 무료 DB)
RUN mkdir -p /app/geoip && \
    apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -sSL -o /tmp/GeoLite2-Country.tar.gz \
      "https://github.com/P3TERX/GeoLite.mmdb/releases/latest/download/GeoLite2-Country.mmdb" && \
    mv /tmp/GeoLite2-Country.tar.gz /app/geoip/GeoLite2-Country.mmdb && \
    rm -rf /var/lib/apt/lists/*
# 로그 디렉토리 생성
RUN mkdir -p /app/logs

# 포트 노출
EXPOSE 8000

# 기본 명령어 (프로덕션: docker-compose.prod.yml에서 gunicorn으로 오버라이드)
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
