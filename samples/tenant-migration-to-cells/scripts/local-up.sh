#!/usr/bin/env bash
# Local stack without Docker: PG 15 (V1 messaging, auth), PG 18 (V2 cell messaging, management), MinIO.
# Same ports as docker-compose.yml. Data lives in ./.local (git-ignored). Stop with scripts/local-down.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD/.local"
PG15=/opt/homebrew/opt/postgresql@15/bin
PG18=/opt/homebrew/opt/postgresql@18/bin
mkdir -p "$ROOT"

export PGPASSWORD=local   # the admin password on the local stack, for the psql and createdb calls below

start_pg() {  # name bin port db initsql
  local name=$1 bin=$2 port=$3 db=$4 init=$5 dir="$ROOT/$1"
  if [ ! -d "$dir" ]; then
    # SCRAM over TCP, as on RDS: postgres_fdw refuses a non-superuser connection unless the remote
    # side asks for a password, so trust auth would hide the very path the isolation relies on.
    echo local > "$ROOT/.pwfile"
    "$bin/initdb" -D "$dir" -U postgres --auth-local=trust --auth-host=scram-sha-256 --pwfile="$ROOT/.pwfile" \
        -E UTF8 --locale=C >/dev/null
  fi
  if ! "$bin/pg_ctl" -D "$dir" status >/dev/null 2>&1; then
    "$bin/pg_ctl" -D "$dir" -o "-p $port -k /tmp" -l "$ROOT/$name.log" -w start >/dev/null
  fi
  if ! "$bin/psql" -h localhost -p "$port" -U postgres -lqt | cut -d'|' -f1 | grep -qw "$db"; then
    "$bin/createdb" -h localhost -p "$port" -U postgres "$db"
    "$bin/psql" -h localhost -p "$port" -U postgres -d "$db" -q -v ON_ERROR_STOP=1 -f "$init"
  fi
  "$bin/psql" -h localhost -p "$port" -U postgres -qc "ALTER USER postgres PASSWORD 'local'" >/dev/null
  echo "  $name on :$port"
}

echo "Starting local stack:"
start_pg v1-messaging      "$PG15" 5415 messaging  sql/v1/messaging.sql
start_pg v1-auth           "$PG15" 5416 auth       sql/v1/auth.sql
start_pg v2-cell-messaging "$PG18" 5418 messaging  sql/v2/cell_messaging.sql
start_pg v2-management     "$PG18" 5419 management sql/v2/management.sql

if ! curl -sf http://localhost:9000/minio/health/live >/dev/null; then
  MINIO_ROOT_USER=local MINIO_ROOT_PASSWORD=localsecret nohup minio server "$ROOT/minio" --address :9000 \
    --console-address :9001 >"$ROOT/minio.log" 2>&1 &
  echo $! >"$ROOT/minio.pid"
  for _ in $(seq 30); do curl -sf http://localhost:9000/minio/health/live >/dev/null && break; sleep 1; done
fi
echo "  s3 (MinIO) on :9000"

# Outside Docker the management DB reaches V1 on localhost ports, not service names.
cat >"$ROOT/env" <<EOF
export FDW_V1_MESSAGING_HOST=localhost FDW_V1_MESSAGING_PORT=5415
export FDW_V1_AUTH_HOST=localhost FDW_V1_AUTH_PORT=5416
export TENANTMIG_SANDBOX=1                      # allows reset / purge-buckets on the local stack
export PLACEMENT_CACHE_TTL_SECONDS=2            # local demo: pretend the router caches placement for 2 s
EOF
# Read-only fdw role and catalogue tables (idempotent).
if [ -x .venv/bin/tenantmig ]; then (source "$ROOT/env" && .venv/bin/tenantmig init >/dev/null) && echo "  schemas, roles and policies ready"; fi
echo "Ready. Run: source .local/env && tenantmig demo"
