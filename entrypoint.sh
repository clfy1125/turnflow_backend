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

# 마이그레이션 실행
echo "Running migrations..."
python manage.py migrate --noinput

# 정적 파일 수집 (production)
if [ "$DJANGO_SETTINGS_MODULE" = "config.settings.prod" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

# 전달된 명령어 실행
exec "$@"
