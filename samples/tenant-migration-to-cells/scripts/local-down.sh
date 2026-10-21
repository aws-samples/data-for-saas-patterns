#!/usr/bin/env bash
# Stop the local stack. Add --wipe to delete its data.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD/.local"
WIPE="${1:-}"
for pair in "v1-messaging 15" "v1-auth 15" "v2-cell-messaging 18" "v2-management 18"; do
  set -- $pair
  [ -d "$ROOT/$1" ] && /opt/homebrew/opt/postgresql@$2/bin/pg_ctl -D "$ROOT/$1" stop -m fast >/dev/null 2>&1 || true
done
[ -f "$ROOT/minio.pid" ] && kill "$(cat "$ROOT/minio.pid")" 2>/dev/null || true
[ "$WIPE" = "--wipe" ] && rm -rf "$ROOT"
echo "Stopped."
