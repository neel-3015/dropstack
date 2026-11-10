#!/usr/bin/env bash
# DropStack smoke tests. Boots the server on a scratch DB, exercises every
# route, and asserts core invariants. Usage: bash tests/smoke.sh
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

PORT="${SMOKE_PORT:-8099}"
BASE="http://127.0.0.1:$PORT"
WORK="$(mktemp -d /tmp/dropstack-smoke.XXXXXX)"
DB="$WORK/test.db"
export ADMIN_PASSWORD="correct horse battery staple"
export DROPSTACK_DB="$DB"
export PORT="$PORT"
export NOTIFICATIONS_LOG="$WORK/notifications.log"
export DRAW_ALTERNATES=2

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok  - $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  - $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected [$2] got [$3])"; fi; }
wf() { printf '%s' "$1" > "$WORK/b"; echo "$WORK/b"; }
contains() { if grep -qF -- "$3" "$2"; then ok "$1"; else bad "$1 (missing '$3')"; fi; }

cleanup() { [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

phone() { echo "555$(printf '%07d' "$1")"; }   # deterministic 10-digit US number

echo "== booting server =="
python3 server.py >"$WORK/server.log" 2>&1 &
SRV_PID=$!
for i in $(seq 1 40); do
  curl -sf --max-time 2 -o /dev/null "$BASE/" && break
  sleep 0.25
  kill -0 "$SRV_PID" 2>/dev/null || { echo "server died:"; cat "$WORK/server.log"; exit 1; }
done

CURL="curl -s -S --max-time 10 -o - -w \n%{http_code}"

# ---------------------------------------------------------------- 1. routes
echo "== public routes =="
$CURL "$BASE/" >"$WORK/home" 2>/dev/null; code=$(tail -n1 "$WORK/home")
check "GET / returns 200" 200 "$code"
contains "home renders 21+ disclaimer" "$WORK/home" "21 years of age or older"
contains "home renders license number" "$WORK/home" "QUOTA-LIQ-000-EXAMPLE"
contains "home has restock form" "$WORK/home" "Restock alerts"

code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/nope")
check "GET unknown route returns 404" 404 "$code"
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/drops/999")
check "unauthenticated drop request redirects to age-gate" 303 "$code"

# ---------------------------------------------------------------- 2. admin auth
echo "== admin auth =="
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/admin")
check "GET /admin without session redirects" 302 "$code"
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -d "password=wrong" "$BASE/admin/login")
check "wrong password rejected" 200 "$code"

curl -s --max-time 5 -D "$WORK/login-headers" -o /dev/null -c "$WORK/cookies" \
  --data-urlencode "password=$ADMIN_PASSWORD" "$BASE/admin/login"
if grep -qi "Set-Cookie.*ds_session.*HttpOnly" "$WORK/login-headers"; then ok "session cookie HttpOnly"; else bad "session cookie HttpOnly"; fi
if grep -qi "Set-Cookie.*SameSite=Strict" "$WORK/login-headers"; then ok "session cookie SameSite=Strict"; else bad "session cookie SameSite=Strict"; fi

A="curl -s -b $WORK/cookies"

# ---------------------------------------------------------------- 3. create drop
echo "== admin creates drop =="
DL=$(python3 -c "import datetime;print((datetime.datetime.now()+datetime.timedelta(days=2)).strftime('%Y-%m-%dT%H:%M'))")
out=$($CURL -b "$WORK/cookies" \
  --data-urlencode "title=Pappy VanWinkle 23yr" \
  --data-urlencode "description=One bottle of the holy grail." \
  --data-urlencode "photo_url=https://example.com/pappy.jpg" \
  --data-urlencode "price=399.99" \
  --data-urlencode "quantity=1" \
  --data-urlencode "rules_text=Winner must pick up in store within 48 hours." \
  --data-urlencode "state_code=CA" \
  --data-urlencode "deadline=$DL" \
  "$BASE/admin/drops" 2>/dev/null)
code=$(tail -n1 <<<"$out")
check "POST /admin/drops creates drop" 303 "$code"
DID=1

out=$($CURL "$BASE/drops/$DID")
code=$(tail -n1 <<<"$out")
check "public drop page redirects to age-gate when unverified" 303 "$code"
out=$($CURL "$BASE/age-gate?next=/drops/$DID")
code=$(tail -n1 <<<"$out")
contains "GET /age-gate shows interstitial" "$(wf "$(sed '$d' <<<"$out")")" "Are you 21 or older"
check "GET /age-gate returns 200" 200 "$code"

out=$($CURL -c "$WORK/pub" -d "next=/drops/$DID" "$BASE/age-gate")
code=$(tail -n1 <<<"$out")
check "age gate POST sets cookie + redirects" 303 "$code"
P="curl -s --max-time 10 -b $WORK/pub -c $WORK/pub -w \n%{http_code}"
out=$($P "$BASE/drops/$DID")
code=$(tail -n1 <<<"$out")
body=$(sed '$d' <<<"$out")
check "verified adult sees drop page" 200 "$code"
B="$(wf "$body")"
contains "drop page: title" "$B" "Pappy VanWinkle 23yr"
contains "drop page: no purchase necessary" "$B" "NO PURCHASE NECESSARY"
contains "drop page: state rules (CA)" "$B" "significant element of chance"
contains "drop page: drop rules text" "$B" "pick up in store within 48 hours"
contains "drop page: license in footer" "$B" "QUOTA-LIQ-000-EXAMPLE"
contains "drop page: 21+ disclaimer footer" "$B" "21 years of age or older"

# ---------------------------------------------------------------- 4. entries
echo "== 5 entries + dedupe =="
entry() { # entry <name> <phone> <email>
  $P -o /dev/null -w '%{http_code}' --data-urlencode "name=$1" \
    --data-urlencode "phone=$2" --data-urlencode "email=$3" "$BASE/drops/$DID/entry"
}
ok5=1
for i in 1 2 3 4 5; do
  c=$(entry "Customer $i" "$(phone $i)" "c$i@example.com")
  [ "$c" = "303" ] || ok5=0
done
check "5 distinct entries accepted" 1 "$ok5"

c=$(entry "Customer 3 Again" "$(phone 3)" "other3@example.com")
check "duplicate PHONE rejected (409)" 409 "$c"
c=$(entry "Other Name" "$(phone 999)" "c3@example.com")
check "duplicate EMAIL rejected (409)" 409 "$c"
c=$(entry "X" "$(phone 1)" "fresh1@example.com")
check "too-short name rejected" 303 "$c"
c=$(entry "Valid Name" "12345" "fresh2@example.com")
check "bad phone rejected" 303 "$c"
c=$(entry "Valid Name" "$(phone 777)" "not-an-email")
check "bad email rejected" 303 "$c"

n=$(python3 -c "import sqlite3;print(sqlite3.connect('$DB').execute('SELECT COUNT(*) FROM entries').fetchone()[0])")
check "exactly 5 entries in DB" 5 "$n"

# ---------------------------------------------------------------- 5. security (while drop still open)
echo "== security =="
# SQL injection attempt in email + XSS in name
c=$(entry "Sql Test" "$(phone 8)" "bob');DROPTABLEentries;--@example.com")
check "SQL-injection payload handled safely" 303 "$c"
n=$(python3 - "$DB" <<'PYEOF'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print(db.execute("SELECT COUNT(*) FROM entries WHERE email LIKE 'bob%'AND name='Sql Test'").fetchone()[0])
PYEOF
)
check "SQL payload stored as literal text, table intact" 1 "$n"
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/")
check "site still healthy after injection attempt" 200 "$code"

c=$(entry "Eve <script>alert(1)</script>" "$(phone 10)" "xss@example.com")
check "entry name with script tag rejected by validation" 303 "$c"
# stored HTML must be escaped at render time (defense in depth): create a drop
# whose title contains a payload, then check every page that echoes it.
$CURL -b "$WORK/cookies" --data-urlencode "title=XSS <script>alert(1)</script>" \
  --data-urlencode "price=10" --data-urlencode "quantity=1" --data-urlencode "state_code=FL" \
  "$BASE/admin/drops" >/dev/null 2>&1
for url in "/" "/drops/2" "/drops/2/winners"; do
  out=$(curl -s --max-time 5 "$BASE$url")
  if grep -qF "<script>alert(1)</script>" <<<"$out"; then bad "XSS raw payload reflected at $url"; else ok "payload escaped at $url"; fi
done
out=$($P "$BASE/drops/2"); body=$(sed '$d' <<<"$out")
contains "escaped form (&lt;script&gt;) renders" "$(wf "$body")" "&lt;script&gt;alert(1)&lt;/script&gt;"
out=$(curl -s -b "$WORK/cookies" "$BASE/admin/drops/2")
contains "admin page escapes stored payload" "$(wf "$out")" "&lt;script&gt;"
if grep -qF "<script>alert(1)</script>" <<<"$out"; then bad "admin page raw payload"; else ok "admin page XSS-free"; fi

# open-redirect guard on age gate
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -D "$WORK/ag" -d "next=//evil.example.com" "$BASE/age-gate")
if grep -qi "Location: //evil" "$WORK/ag"; then bad "open redirect possible"; else ok "open-redirect guard on age gate"; fi

# oversized body rejected
big=$(python3 -c "print('A'*200000)")
code=$(curl -s --max-time 10 -b "$WORK/pub" -o /dev/null -w '%{http_code}' --data-urlencode "name=$big" --data-urlencode "phone=$(phone 11)" --data-urlencode "email=big@example.com" "$BASE/drops/$DID/entry")
check "oversized POST body rejected (413)" 413 "$code"

# ---------------------------------------------------------------- 6. winners before draw
echo "== winners before draw =="
out=$($CURL "$BASE/drops/$DID/winners")
code=$(tail -n1 <<<"$out")
check "winners page before draw returns 200 placeholder" 200 "$code"
contains "winners placeholder says not yet" "$(wf "$out")" "not happened yet"

# ---------------------------------------------------------------- 7. draw + determinism
echo "== draw =="
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -b "$WORK/cookies" -X POST "$BASE/admin/drops/$DID/close")
check "admin closes drop" 303 "$code"
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -b "$WORK/cookies" -X POST "$BASE/admin/drops/$DID/draw")
check "admin runs draw" 303 "$code"
curl -s --max-time 5 -o /dev/null -b "$WORK/cookies" -X POST "$BASE/admin/drops/$DID/draw"
outbody=$(curl -s -b "$WORK/cookies" "$BASE/admin/drops/$DID")
if grep -q "Draw audit" <<<"$outbody"; then ok "draw audit present (idempotent second draw ignored)"; else bad "draw audit missing"; fi
nd=$(python3 -c "import sqlite3;print(sqlite3.connect('$DB').execute('SELECT COUNT(*) FROM draws').fetchone()[0])")
check "only one draw row exists" 1 "$nd"

out=$($CURL "$BASE/drops/$DID/verify-draw")
code=$(tail -n1 <<<"$out")
check "server-side draw verification endpoint passes" 200 "$code"
contains "verify endpoint reports ok:true" "$(wf "$out")" '"ok": true'

python3 - "$DB" "$DID" <<'PYEOF'
import json, sqlite3, sys
sys.path.insert(0, ".")
import draw
db, did = sqlite3.connect(sys.argv[1]), int(sys.argv[2])
r = db.execute("SELECT seed, entry_ids, winners, alternates FROM draws WHERE drop_id=?", (did,)).fetchone()
q = db.execute("SELECT quantity FROM drops WHERE id=?", (did,)).fetchone()[0]
_, w, a = draw.perform_draw(r[0], json.loads(r[1]), q, 2)
assert w == json.loads(r[2]) and a == json.loads(r[3]), "recomputation mismatch"
print("RECOMPUTE_OK")
PYEOF
if [ $? -eq 0 ]; then ok "winner recomputation from stored seed matches"; else bad "winner recomputation from stored seed matches"; fi

out=$($CURL "$BASE/drops/$DID/winners")
contains "winners page lists winners" "$(wf "$out")" "Winners"
contains "winners page shows audit seed prefix" "$(wf "$out")" "Audit seed"

nsent=$(python3 - <<PYEOF
import json, sqlite3
db = sqlite3.connect("$DB")
r = db.execute("SELECT winners, alternates FROM draws WHERE drop_id=$DID").fetchone()
ids = json.loads(r[0]) + json.loads(r[1])
emails = {db.execute("SELECT email FROM entries WHERE id=?", (i,)).fetchone()[0] for i in ids}
sent = {json.loads(l)["to"] for l in open("$NOTIFICATIONS_LOG") if json.loads(l)["channel"] == "email"}
print(1 if emails.issubset(sent) else 0)
PYEOF
)
check "mock email provider notified all winners/alternates" 1 "$nsent"

# ---------------------------------------------------------------- 8. CSV + restock
echo "== CSV + restock =="
csv_body=$(curl -s -b "$WORK/cookies" "$BASE/admin/drops/$DID/entries.csv")
nrows=$(grep -c "@example.com" <<<"$csv_body")
check "CSV export contains 6 entries (5 + sql-payload entry)" 6 "$nrows"
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/admin/drops/$DID/entries.csv")
check "CSV export requires admin session" 302 "$code"

code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' --data-urlencode "phone=(555) 111-2222" --data-urlencode "email=sub@example.com" "$BASE/restock")
check "restock signup accepted" 303 "$code"
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' --data-urlencode "phone=5551112222" --data-urlencode "email=other@example.com" "$BASE/restock")
check "duplicate restock phone handled gracefully" 303 "$code"
TOKEN=$(python3 -c "import sqlite3;print(sqlite3.connect('$DB').execute('SELECT unsub_token FROM restock LIMIT 1').fetchone()[0])")
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "$BASE/restock/unsubscribe/$TOKEN")
check "unsubscribe link works" 200 "$code"
uo=$(python3 -c "import sqlite3;print(sqlite3.connect('$DB').execute('SELECT unsubscribed FROM restock LIMIT 1').fetchone()[0])")
check "opt-out flag persisted" 1 "$uo"
out=$(curl -s -b "$WORK/cookies" "$BASE/admin/restock")
contains "admin restock list shows OPTED OUT" "$(wf "$out")" "OPTED OUT"

# ---------------------------------------------------------------- 9. rate limiting (last: floods entries)
echo "== rate limiting =="
got429=0
for i in $(seq 1 30); do
  c=$(entry "Rate Test $i" "$(phone $((100 + i)))" "rate${i}body@example.com")
  [ "$c" = "429" ] && got429=1
done
check "entry rate limiting kicks in (429 observed)" 1 "$got429"

# admin actions require auth
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -X POST "$BASE/admin/drops/2/draw")
check "draw without auth redirects (no unauthenticated state change)" 302 "$code"

# ---------------------------------------------------------------- 10. logout
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -b "$WORK/cookies" -X POST "$BASE/admin/logout")
check "logout works" 303 "$code"
code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' -b "$WORK/cookies" "$BASE/admin")
check "session invalid after logout" 302 "$code"

# ---------------------------------------------------------------- done
echo
echo "=============================="
echo "PASS: $PASS   FAIL: $FAIL"
echo "=============================="
[ "$FAIL" -eq 0 ] || exit 1
