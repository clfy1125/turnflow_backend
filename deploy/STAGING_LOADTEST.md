# 스테이징 + DM 부하 측정 가이드

prod 하드닝 스택이 **잘 동작하는지 + 처리량이 얼마나 올라가는지**를 API 승인 전에 안전하게 확인한다.
실 prod 와 완전 격리(별도 컨테이너명/네트워크/볼륨/포트), `INSTAGRAM_MOCK_MODE=True` 라 Meta 실호출 없음.

## 1. 기동
```bash
cd /opt/turnflow_backend     # 또는 dev 박스의 repo
cp .env.staging.example .env.staging        # 필요시 DB_PASSWORD 등 수정
git checkout hardening/dm-surge

docker compose -p turnflow_staging -f docker-compose.staging.yml --env-file .env.staging up -d --build
docker compose -p turnflow_staging -f docker-compose.staging.yml ps    # migrate 완료 + 헬시 확인
```

## 2. 측정
```bash
# 한 번만: 테스트 워크스페이스/연동/캠페인 20개 생성
docker compose -p turnflow_staging -f docker-compose.staging.yml --env-file .env.staging \
  run --rm celery_dm python manage.py loadtest_dm --seed

# 부하: DM 5000건을 dm_send 큐로 → threads 풀이 처리, 실시간 처리율 출력
docker compose -p turnflow_staging -f docker-compose.staging.yml --env-file .env.staging \
  run --rm celery_dm python manage.py loadtest_dm --count 5000 --campaigns 20
```
출력 예: `처리량 : 420.0 DM/s (≈ 25000 DM/분)`, `지연 p50/p95/max`, 실시간 `dm_send_lag`.

## 3. before/after 비교 (핵심 — "얼마나 올라갔나"를 숫자로)
같은 5000건을 **워커 풀만 바꿔** 두 번 돌려 비교한다.

**A) 하드닝 후 (threads 50):** 위 그대로. celery_dm 이 `--pool=threads --concurrency=50`.

**B) 현행 비슷하게 (prefork, 단일):** dm_send 워커를 prefork 소수로 바꿔 한 번:
```bash
docker compose -p turnflow_staging -f docker-compose.staging.yml --env-file .env.staging \
  run --rm -e DUMMY=1 celery_dm \
  sh -c "celery -A config worker --pool=prefork --concurrency=4 -Q dm_send -l warning & sleep 3 && python manage.py loadtest_dm --count 5000 --campaigns 20; kill %1"
```
→ A 의 DM/s 와 B 의 DM/s 를 비교하면 동시성 점프 효과가 바로 보인다. (replica 를 늘리려면 `--scale celery_dm=3`)

> ⚠️ mock 모드라 네트워크 latency 가 0 에 가까워 **서버측(Celery+DB+PgBouncer) 상한**을 잰다.
> 실제 Meta 왕복(수백 ms)이 들어가면 절대 처리량은 낮아지지만, threads 풀의 **상대적 우위(동시성)**는 더 커진다
> (prefork 4 는 4건 동시 = 네트워크 대기에 묶임 / threads 50 은 50건 동시 대기 가능).

## 4. 동시에 볼 것
```bash
# PgBouncer 뒤 실제 PG 커넥션이 풀 크기(~20)로 수렴하는지
docker exec turnflow_staging_db psql -U postgres -c "select count(*),state from pg_stat_activity group by state;"
# dm_send 큐 적체
docker exec turnflow_staging_redis redis-cli LLEN dm_send
# 컨테이너 CPU/RAM
docker stats --no-stream | grep turnflow_staging
```

## 5. 정리
```bash
docker compose -p turnflow_staging -f docker-compose.staging.yml --env-file .env.staging \
  run --rm celery_dm python manage.py loadtest_dm --cleanup     # 데이터만
docker compose -p turnflow_staging -f docker-compose.staging.yml down -v   # 스택+볼륨 전체
```

## 검증 포인트 (동작 확인)
- [ ] mock 부하에서 **failed=0** (전부 accepted) → 큐 라우팅·threads·PgBouncer·DB 경로가 정상 동작
- [ ] PgBouncer 뒤 PG 커넥션이 풀 크기로 수렴 (고갈 없음)
- [ ] threads(A) 처리량 ≫ prefork-4(B) 처리량 → 하드닝 효과 입증
- [ ] 웹훅 멱등성: 같은 comment 로 2번 트리거해도 idempotency_key UNIQUE 로 중복 발송 0
