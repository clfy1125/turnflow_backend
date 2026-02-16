# Instagram Service Backend

인스타그램 비즈니스 계정 자동화 서비스 백엔드

## 🎯 프로젝트 개요

Instagram Business 계정의 댓글 수집, 분류, 자동 DM 발송을 통한 리드 관리 및 고객 참여 자동화 서비스

### MVP 기능

- 인스타그램 비즈니스 계정 연동 (OAuth Token 관리)
- 게시물 댓글 자동 수집 및 분류 (관심/스팸/악플 등)
- 키워드 기반 댓글 감지 및 필터링
- 규칙 기반 자동 DM 발송 (24시간 정책 준수)
- DM 템플릿 및 시나리오 관리
- 성과 대시보드 및 지표 추적
- 요금제별 사용량 제한 (Starter/Pro/Enterprise)

## 🛠 기술 스택

- **Framework**: Django 5.0, Django REST Framework
- **Database**: PostgreSQL 16
- **Cache/Queue**: Redis 7, Celery
- **Container**: Docker, Docker Compose
- **Code Quality**: Black, Ruff, isort, pre-commit

## 📁 프로젝트 구조

```
.
├── apps/                    # Django 앱 디렉터리
│   └── core/               # 핵심 앱 (healthcheck 등)
├── config/                 # Django 설정
│   ├── settings/          # 환경별 설정 분리
│   │   ├── base.py       # 공통 설정
│   │   ├── local.py      # 로컬 개발 설정
│   │   └── prod.py       # 프로덕션 설정
│   ├── urls.py           # 메인 URL 설정
│   ├── api_urls.py       # API v1 URL 설정
│   ├── celery.py         # Celery 설정
│   ├── wsgi.py
│   └── asgi.py
├── docker-compose.yml      # Docker Compose 설정
├── Dockerfile             # Docker 이미지 정의
├── entrypoint.sh          # 컨테이너 엔트리포인트
├── requirements.txt       # Python 의존성
├── manage.py             # Django 관리 명령
├── .env.example          # 환경변수 예시
├── .gitignore
├── .pre-commit-config.yaml
├── pyproject.toml        # 프로젝트 메타데이터 및 도구 설정
└── README.md
```

## 🚀 시작하기

### 사전 요구사항

- Docker Desktop (Windows/Mac) 또는 Docker Engine + Docker Compose (Linux)
- Git

### 로컬 환경 설정

1. **저장소 클론**

```bash
git clone <repository-url>
cd 08_인스타서비스백엔드
```

2. **환경변수 설정**

```bash
# .env.example을 .env로 복사
cp .env.example .env

# .env 파일을 열어 필요한 값 수정 (선택사항)
# 기본값으로 로컬 개발 가능
```

3. **Docker Compose로 실행**

```bash
# 컨테이너 빌드 및 실행
docker compose up --build

# 백그라운드 실행
docker compose up -d
```

4. **서비스 확인**

- 웹 서버: http://localhost:8000
- Health Check: http://localhost:8000/api/v1/healthz
- Admin: http://localhost:8000/admin

### 초기 데이터베이스 설정

```bash
# 마이그레이션은 entrypoint.sh에서 자동 실행됩니다
# 수동 실행이 필요한 경우:
docker compose exec web python manage.py migrate

# 슈퍼유저 생성
docker compose exec web python manage.py createsuperuser
```

## 🔧 개발 명령어

### Docker Compose 명령어

```bash
# 서비스 시작
docker compose up

# 서비스 중지
docker compose down

# 볼륨까지 삭제 (DB 초기화)
docker compose down -v

# 로그 확인
docker compose logs -f web
docker compose logs -f celery_worker

# 특정 컨테이너 접속
docker compose exec web bash
docker compose exec db psql -U postgres -d instagram_service
```

### Django 명령어

```bash
# 마이그레이션 생성
docker compose exec web python manage.py makemigrations

# 마이그레이션 적용
docker compose exec web python manage.py migrate

# 슈퍼유저 생성
docker compose exec web python manage.py createsuperuser

# Django Shell
docker compose exec web python manage.py shell_plus

# 테스트 실행
docker compose exec web pytest
```

### 코드 품질 도구

```bash
# Pre-commit 설치 (로컬에서)
pip install pre-commit
pre-commit install

# 수동 실행
pre-commit run --all-files

# Black (formatting)
black apps/ config/

# Ruff (linting)
ruff check apps/ config/ --fix

# isort (import sorting)
isort apps/ config/
```

## 🧪 테스트

```bash
# 전체 테스트 실행
docker compose exec web pytest

# 특정 테스트 실행
docker compose exec web pytest apps/core/tests/

# 커버리지 리포트
docker compose exec web pytest --cov=apps --cov-report=html
```

## 📊 API 문서

서버 실행 후 다음 URL에서 API 문서 확인:

- Swagger UI: http://localhost:8000/api/schema/swagger-ui/
- ReDoc: http://localhost:8000/api/schema/redoc/

## 🔐 보안

- `.env` 파일은 **절대 커밋하지 마세요**
- 프로덕션에서는 반드시 `SECRET_KEY` 변경
- 프로덕션 환경에서는 `DEBUG=False` 설정
- HTTPS 사용 및 보안 설정 활성화

## 📝 개발 가이드라인

### 브랜치 전략

- `main`: 프로덕션 배포 브랜치
- `develop`: 개발 통합 브랜치
- `feature/*`: 기능 개발 브랜치
- `hotfix/*`: 긴급 수정 브랜치

### 커밋 메시지

```
feat: 새로운 기능 추가
fix: 버그 수정
docs: 문서 수정
style: 코드 포맷팅
refactor: 코드 리팩토링
test: 테스트 추가/수정
chore: 빌드/설정 변경
```

### 코드 스타일

- Black (line-length: 100)
- isort (profile: black)
- Ruff (Python linting)
- Pre-commit hooks 사용

## 🏗 아키텍처 원칙

- **API-First**: 모든 기능은 API로 먼저 구현
- **멀티 테넌시**: Workspace 단위 데이터 분리
- **비동기 처리**: Celery를 활용한 백그라운드 작업
- **Idempotency**: 중복 요청 방지
- **관측성**: 로깅, 모니터링, 감사 추적

## 📦 배포

(추후 CI/CD 파이프라인 구성 예정)

## 🤝 기여하기

1. Feature 브랜치 생성
2. 변경사항 커밋
3. Pre-commit hooks 통과 확인
4. Pull Request 생성

## 📄 라이선스

Private Project

## 📞 문의

프로젝트 관련 문의사항은 이슈로 등록해주세요.

---

**Status**: ✅ Step 1 완료 (프로젝트 스캐폴딩)
