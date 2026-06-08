# GATE-0 Layer-2 — Postgres image WITH pgBackRest, so archive_command can run inside the container.
# Only needed when enabling WAL archiving/PITR (step A6). Layer-1 daily dumps work on stock postgres:16-alpine.
#
# To use: in docker-compose.prod.yml db service, replace
#     image: postgres:16-alpine
# with
#     build: { context: ., dockerfile: deploy/postgres/Dockerfile }
# then mount /etc/pgbackrest/pgbackrest.conf into the container.
FROM postgres:16-alpine
RUN apk add --no-cache pgbackrest
