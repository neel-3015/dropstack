# DropStack

Allocation-drop raffle / pre-order manager for independent liquor stores.
Zero-dependency MVP: Python 3 stdlib (`http.server` + `sqlite3`), server-rendered HTML.

## Run

```bash
cd dropstack
ADMIN_PASSWORD='pick-a-long-random-password' python3 server.py
# open http://localhost:8000  (admin at /admin)
```

Env vars:

| Var | Default | Purpose |
|---|---|---|
| `ADMIN_PASSWORD` | (none → admin disabled, fail closed) | single admin password |
| `PORT` / `HOST` | `8000` / `127.0.0.1` | bind (set `HOST=0.0.0.0` only behind a TLS proxy; macOS firewall may block 0.0.0.0 binds) |
| `DROPSTACK_DB` | `./dropstack.db` | SQLite path |
| `STORE_ID` | `store1` | active store (multi-store ready) |
| `DRAW_ALTERNATES` | `3` | alternates stored after winners |
| `SMS_PROVIDER` / `EMAIL_PROVIDER` | `mock` | `mock` or `twilio` / `postmark` (stubs) |
| `NOTIFICATIONS_LOG` | `notifications.log.jsonl` | where the mock provider writes |
| `ENTRY_RATE_LIMIT` | `15` | entry POSTs per IP per minute |
| `SECURE_COOKIES` | `0` | set `1` behind HTTPS so cookies get `Secure` |

## Tests

```bash
bash tests/smoke.sh          # 62 assertions: every route, dedupe, draw determinism, XSS/SQLi, rate limit
bash tests/concurrency.sh    # 200 parallel entries + 2 parallel draws
```

Both boot their own server on a scratch DB in /tmp and exit non-zero on failure.

## Architecture

- `server.py` — routing, auth, templates, rate limiter, SQLite (WAL, one shared
  connection + `RLock`, `ThreadingHTTPServer` with a 128 backlog).
- `draw.py` — the verifiable draw (documented in the module docstring).
  Summary: CSPRNG seed generated at draw time and stored in `draws`;
  full Fisher–Yates shuffle of the sorted entry-ID snapshot using
  `random.Random(seed)`; first `quantity` IDs are winners, next N are
  alternates. `GET /drops/<id>/verify-draw` recomputes from the stored seed
  and entry snapshot so anyone can audit a result. (Determinism is tied to
  CPython's stable MT randrange; the Python version used is stored per draw.)
- `providers.py` — `SmsProvider`/`EmailProvider` interfaces; console+file mock
  (default) and Twilio/Postmark production stubs.
- `seeds/state_rules.json` — per-state compliance copy seeded into
  `state_rules`; rendered on every entry form alongside "NO PURCHASE
  NECESSARY" and the 21+ disclaimer + store license on every page footer.
- Every table carries `store_id`; the UI runs one store (`STORE_ID`) today,
  multi-store = per-request store resolution later.
- Mobile CSS is first-class; entries are deduped per drop by unique indexes on
  normalized phone and lowercased email.

Security posture (all covered by smoke tests): parameterized SQL everywhere,
HTML-escaping of every user string, name field rejects `<`/`>`, http(s)-only
photo URLs, CSV formula-injection guard, per-IP sliding-window rate limits
(entries/login/restock), 64 KB body cap (413), HttpOnly + SameSite=Strict
session cookies with 24h expiry and server-side revocation on logout,
`Secure` flag via env, open-redirect guard, fail-closed admin auth with
constant-time password compare.

## Wiring for production (not built)

- **Twilio SMS**: implement `TwilioSmsProvider.send` (POST Messages API, Basic
  auth `sid:token`); need a US number with A2P 10DLC registration for
  compliant SMS; implement inbound STOP webhook → set `restock.unsubscribed=1`.
- **Postmark email**: implement `PostmarkEmailProvider.send` (POST /email with
  `X-Postmark-Server-Token`); honor List-Unsubscribe.
- **TLS/deploy**: run behind Caddy/nginx, `HOST=127.0.0.1 SECURE_COOKIES=1`;
  set a real `ADMIN_PASSWORD`; consider per-IP trusted proxy header config.
- **Stripe paid pre-orders (documented, deliberately NOT built)**: if you ever
  sell rather than raffle, charge *after* winner selection only — prepayment
  for a chance to win converts a sweepstakes into an illegal lottery in most
  states. Flow would be: draw → winner gets single-use Stripe Checkout link →
  charge `price` + tax → pickup with ID. Keep the free raffle path available.
- **Real DB**: SQLite WAL is fine for one store; move to Postgres if adding
  many stores/concurrent admins.

## Legal notes (not legal advice)

- Raffles/sweepstakes: **no purchase necessary** and no payment may be
  required to enter or win; purchase must not improve odds. Text is enforced
  on every entry form; keep it that way.
- Alcohol promotions are additionally regulated: some states restrict or ban
  retailer bottle raffles/allocation lotteries outright (and liquor-license
  rules may limit "consumer loyalty" programs). **Verify per state with the
  state ABC/liquor-control authority and a local attorney before running any
  drop** — FL/CA/TX templates in `seeds/state_rules.json` are placeholders.
- Sweepstakes vs lottery vs "bona fide chance to win a prize" disclosure
  requirements (official rules, odds, MAP, winner list) vary by state.
- SMS requires TCPA consent (kept: signups are explicit opt-in; opt-out is
  stored and honored — never send to opted-out rows).
- 21+ only; winners must present ID at pickup; employees/families excluded
  (stated in the rendered rules).

## Known limitations / honest list

- Single admin password, no 2FA, no per-admin accounts.
- SameSite=Strict is the CSRF mitigation; no per-form CSRF tokens.
- Mock providers only *log*; nothing sends. Twilio/Postmark classes raise
  `NotImplementedError` by design.
- Draw determinism is CPython-dependent (version stored per draw).
- Naive local-time deadlines; no email reminders before deadline.
- No image upload — photo URL only; no analytics, no pagination (fine at MVP volume).
- Age gate is self-attested (cookie), as with any web age gate.
- `X-Forwarded-For` is trusted for rate limiting — only safe behind your own proxy.

## Next milestone candidates

1. Stripe checkout-after-win (see above), tax + pickup-window handling.
2. Twilio/Postmark real providers + inbound STOP/unsubscribe webhooks.
3. Multi-store auth (store scoping per admin account), per-store branding.
4. Entry cap on prize pickups (block phones that won within N days).
5. Admin: edit drop after creation, delete/void drop, PDF audit receipt.
6. Postgres + proper migrations; scheduled auto-draw at deadline.
