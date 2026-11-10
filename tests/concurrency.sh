#!/usr/bin/env bash
# DropStack concurrency check: 200 parallel entries + 2 parallel draws.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${SMOKE_PORT:-8098}"
BASE="http://127.0.0.1:$PORT"
WORK="$(mktemp -d /tmp/dropstack-conc.XXXXXX)"
export ADMIN_PASSWORD=cadmin
export DROPSTACK_DB="$WORK/test.db"
export PORT="$PORT"
export NOTIFICATIONS_LOG="$WORK/notify.log"
export ENTRY_RATE_LIMIT=100000
export DRAW_ALTERNATES=3

cleanup() { [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

python3 server.py >"$WORK/server.log" 2>&1 & SRV_PID=$!
for i in $(seq 1 40); do curl -sf --max-time 2 -o /dev/null "$BASE/" && break; sleep 0.25; done

# login + create drop + age gate
curl -s -o /dev/null -c "$WORK/adm" --data-urlencode "password=cadmin" "$BASE/admin/login"
curl -s -o /dev/null -b "$WORK/adm" -d "title=Concurrency Drop" -d "price=50" -d "quantity=2" -d "state_code=FL" "$BASE/admin/drops"
curl -s -o /dev/null -c "$WORK/pub" -d "next=/drops/1" "$BASE/age-gate"

echo "== 200 parallel entries =="
seq 1 200 | xargs -P 50 -I{} sh -c 'curl -s --max-time 15 -b "'"$WORK"'/pub" -o /dev/null -w "%{http_code}\n" \
  --data-urlencode "name=Person $1" --data-urlencode "phone=555$(printf "%07d" "$1")" \
  --data-urlencode "email=p$1@example.com" "'"$BASE"'/drops/1/entry"' x {} > "$WORK/codes"
s200=$(grep -c '^303$' "$WORK/codes" || true)
s409=$(grep -c '^409$' "$WORK/codes" || true)
n=$(python3 -c "import sqlite3;print(sqlite3.connect('$WORK/test.db').execute('SELECT COUNT(*) FROM entries').fetchone()[0])")
dupe=$(python3 -c "import sqlite3;print(sqlite3.connect('$WORK/test.db').execute('SELECT COUNT(*) FROM (SELECT phone FROM entries GROUP BY phone HAVING COUNT(*)>1)').fetchone()[0])")
echo "303 accepted: $s200  409 rejected: $s409  rows: $n  duplicate phones: $dupe"
[ "$s200" -eq 200 ] && [ "$s409" -eq 0 ] && [ "$n" -eq 200 ] && [ "$dupe" -eq 0 ] \
  && echo "  ok  - 200 parallel entries: all stored, none lost, none duplicated" \
  || { echo "FAIL  - parallel entry invariant broken"; exit 1; }

echo "== 2 parallel draws =="
( curl -s -o /dev/null -w '%{http_code}\n' -b "$WORK/adm" -X POST "$BASE/admin/drops/1/draw" &
  curl -s -o /dev/null -w '%{http_code}\n' -b "$WORK/adm" -X POST "$BASE/admin/drops/1/draw" &
  wait )
nd=$(python3 -c "import sqlite3;print(sqlite3.connect('$WORK/test.db').execute('SELECT COUNT(*) FROM draws').fetchone()[0])")
[ "$nd" -eq 1 ] && echo "  ok  - exactly one draw row from 2 parallel draws" || { echo "FAIL  - $nd draw rows"; exit 1; }

code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/drops/1/verify-draw")
[ "$code" -eq 200 ] && echo "  ok  - draw still verifiable after concurrency" || { echo "FAIL  - verify-draw $code"; exit 1; }

echo "CONCURRENCY OK"
