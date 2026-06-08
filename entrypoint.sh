#!/bin/bash

# 데이터베이스가 준비될 때까지 대기
echo "Waiting for PostgreSQL..."
python << END
import time
import psycopg2
import os

max_retries = 30
retry = 0
while retry < max_retries:
    try:
        conn = psycopg2.connect(
            dbname=os.environ.get('DB_NAME', 'instagram_service'),
            user=os.environ.get('DB_USER', 'postgres'),
            password=os.environ.get('DB_PASSWORD', 'postgres'),
            host=os.environ.get('DB_HOST', 'db'),
            port=os.environ.get('DB_PORT', '5432')
        )
        conn.close()
        print("PostgreSQL is ready!")
        break
    except psycopg2.OperationalError:
        retry += 1
        print(f"PostgreSQL not ready yet... ({retry}/{max_retries})")
        time.sleep(1)
END

# 마이그레이션 — 게이트화 (RUN_MIGRATIONS=1 일 때만).
# 3-tier web + 다중 celery 워커가 동시 startup 하므로 migrate 를 모든 컨테이너에서
# 돌리면 race 가 난다. deploy.sh 가 단일 one-shot 으로 명시 실행한다.
# 레거시(단일 컨테이너) 호환: RUN_MIGRATIONS 미설정 시 기본 1 로 두려면 아래 default 를 바꾼다.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    echo "Running migrations..."
    python manage.py migrate --noinput
fi

# 정적 파일 수집 — 게이트화 (RUN_COLLECTSTATIC=1 일 때만; 공유 static 볼륨이라 1회면 충분).
if [ "${RUN_COLLECTSTATIC:-0}" = "1" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

# 전달된 명령어 실행
exec "$@"
