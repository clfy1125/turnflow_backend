.PHONY: help build up down logs shell test migrate makemigrations createsuperuser clean lint format

help:  ## 사용 가능한 명령어 목록 표시
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

build:  ## Docker 이미지 빌드
	docker compose build

up:  ## 서비스 시작 (포그라운드)
	docker compose up

up-d:  ## 서비스 시작 (백그라운드)
	docker compose up -d

down:  ## 서비스 중지
	docker compose down

down-v:  ## 서비스 중지 및 볼륨 삭제
	docker compose down -v

logs:  ## 로그 확인 (전체)
	docker compose logs -f

logs-web:  ## 웹 서버 로그 확인
	docker compose logs -f web

logs-celery:  ## Celery 워커 로그 확인
	docker compose logs -f celery_worker

shell:  ## Django shell 실행
	docker compose exec web python manage.py shell_plus

bash:  ## 웹 컨테이너 bash 접속
	docker compose exec web bash

db-shell:  ## PostgreSQL 접속
	docker compose exec db psql -U postgres -d instagram_service

migrate:  ## 마이그레이션 적용
	docker compose exec web python manage.py migrate

makemigrations:  ## 마이그레이션 파일 생성
	docker compose exec web python manage.py makemigrations

createsuperuser:  ## 슈퍼유저 생성
	docker compose exec web python manage.py createsuperuser

test:  ## 테스트 실행
	docker compose exec web pytest

test-cov:  ## 테스트 실행 (커버리지 포함)
	docker compose exec web pytest --cov=apps --cov-report=html

lint:  ## 코드 린팅 (ruff)
	docker compose exec web ruff check apps/ config/

lint-fix:  ## 코드 린팅 및 자동 수정
	docker compose exec web ruff check apps/ config/ --fix

format:  ## 코드 포맷팅 (black, isort)
	docker compose exec web black apps/ config/
	docker compose exec web isort apps/ config/

clean:  ## Python 캐시 파일 삭제
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.pyo" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true

restart:  ## 서비스 재시작
	docker compose restart

restart-web:  ## 웹 서버만 재시작
	docker compose restart web

ps:  ## 실행 중인 컨테이너 확인
	docker compose ps

init:  ## 초기 설정 (빌드 + 실행 + 마이그레이션)
	make build
	make up-d
	@echo "Waiting for services to start..."
	@sleep 5
	make migrate
	@echo "Setup complete! Visit http://localhost:8000/api/v1/healthz"
