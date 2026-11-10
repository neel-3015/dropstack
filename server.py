#!/usr/bin/env python3
"""DropStack — allocation-drop raffle manager for independent liquor stores.

Zero-dependency Python 3 stdlib server: http.server + sqlite3, server-rendered HTML.
Run:  ADMIN_PASSWORD=... python3 server.py
"""
import csv
import io
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import draw as draw_mod
from providers import get_sms_provider, get_email_provider

DB_PATH = os.environ.get("DROPSTACK_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "dropstack.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
HOST = os.environ.get("HOST", "127.0.0.1")
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "0") == "1"
PORT = int(os.environ.get("PORT", "8000"))
ALTERNATES = int(os.environ.get("DRAW_ALTERNATES", "3"))
ENTRY_RATE_LIMIT = int(os.environ.get("ENTRY_RATE_LIMIT", "15"))  # entries per IP per minute
SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds")
AGE_COOKIE = "ds_age"
SESSION_COOKIE = "ds_session"
STORE_ID = os.environ.get("STORE_ID", "store1")
DISCLAIMER = "All alcohol products are for persons 21 years of age or older. ID required at pickup. Please drink responsibly."
NO_PURCHASE = "NO PURCHASE NECESSARY. A purchase will not increase your chances of winning. Void where prohibited."

DB_LOCK = threading.RLock()
conn = None  # set in init_db

# ---------------------------------------------------------------- database

def db():
    return conn

def init_db():
    global conn
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with DB_LOCK:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS stores(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, license_number TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS drops(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          store_id TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT DEFAULT '',
          photo_url TEXT DEFAULT '',
          price REAL DEFAULT 0,
          quantity INTEGER DEFAULT 1,
          rules_text TEXT DEFAULT '',
          state_code TEXT DEFAULT 'FL',
          deadline TEXT DEFAULT '',
          status TEXT DEFAULT 'open',
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entries(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          drop_id INTEGER NOT NULL,
          store_id TEXT NOT NULL,
          name TEXT NOT NULL,
          phone TEXT NOT NULL,
          email TEXT NOT NULL,
          customer_since INTEGER,
          ip TEXT,
          created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_entries_phone ON entries(drop_id, phone);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_entries_email ON entries(drop_id, email);
        CREATE TABLE IF NOT EXISTS draws(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          drop_id INTEGER UNIQUE NOT NULL,
          store_id TEXT NOT NULL,
          seed TEXT NOT NULL,
          entry_ids TEXT NOT NULL,
          winners TEXT NOT NULL,
          alternates TEXT NOT NULL,
          python_version TEXT NOT NULL,
          drawn_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS restock(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          store_id TEXT NOT NULL,
          phone TEXT,
          email TEXT,
          unsub_token TEXT UNIQUE NOT NULL,
          unsubscribed INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_restock_phone ON restock(store_id, phone) WHERE phone IS NOT NULL AND phone != '';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_restock_email ON restock(store_id, email) WHERE email IS NOT NULL AND email != '';
        CREATE TABLE IF NOT EXISTS state_rules(
          store_id TEXT NOT NULL, state_code TEXT NOT NULL, rules_text TEXT NOT NULL,
          PRIMARY KEY(store_id, state_code));
        """)
        if not conn.execute("SELECT 1 FROM stores WHERE id=?", (STORE_ID,)).fetchone():
            conn.execute("INSERT INTO stores(id,name,license_number) VALUES(?,?,?)",
                         (STORE_ID, "Sunset Spirits & Wine", "QUOTA-LIQ-000-EXAMPLE"))
        with open(os.path.join(SEED_DIR, "state_rules.json"), encoding="utf-8") as f:
            rules = json.load(f)
        for code, text in rules.items():
            conn.execute("INSERT OR IGNORE INTO state_rules(store_id,state_code,rules_text) VALUES(?,?,?)",
                         (STORE_ID, code, text))
        conn.commit()

# ---------------------------------------------------------------- helpers

def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def esc(v):
    return html.escape("" if v is None else str(v), quote=True)

def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else None

def norm_email(e):
    e = (e or "").strip().lower()
    if len(e) > 254 or not re.match(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$", e):
        return None
    return e

def valid_url(u):
    return bool(u) and bool(re.match(r"^https?://[^\s\"'<>]+$", u.strip()))

def store():
    return db().execute("SELECT * FROM stores WHERE id=?", (STORE_ID,)).fetchone()

def deadline_passed(d):
    dl = (d["deadline"] or "").strip()
    if not dl:
        return False
    try:
        return datetime.now() > datetime.fromisoformat(dl)
    except ValueError:
        return False

def drop_is_open(d):
    return d["status"] == "open" and not deadline_passed(d)

# rate limiter: sliding window per bucket key
_rate_lock = threading.Lock()
_rate_buckets = {}

def rate_limited(key, max_calls, window=60):
    now = time.time()
    with _rate_lock:
        last = _rate_buckets.get("_last_sweep", 0)
        if now - last > window * 5:
            _rate_buckets.clear()
            _rate_buckets["_last_sweep"] = now
        dq = _rate_buckets.setdefault(key, deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= max_calls:
            return True
        dq.append(now)
        return False

# ---------------------------------------------------------------- templates

CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,system-ui,'Segoe UI',Roboto,sans-serif;margin:0;background:#f4f5f9;color:#181a20;line-height:1.5}
.wrap{max-width:640px;margin:0 auto;padding:16px}
header{background:#101828;color:#fff;padding:14px 0}
header .wrap{display:flex;align-items:center;justify-content:space-between;padding:8px 16px}
.logo{font-weight:800;font-size:20px;letter-spacing:.5px;color:#fff;text-decoration:none}
.logo span{color:#e8b04b}
nav a{color:#cfd4e0;margin-left:14px;font-size:14px;text-decoration:none}
.store-name{color:#8b93a7;font-size:12px;display:none}
@media(min-width:560px){.store-name{display:inline}}
.card{background:#fff;border-radius:12px;padding:18px;margin:14px 0;box-shadow:0 1px 4px rgba(16,24,40,.08)}
h1{font-size:22px;margin:0 0 8px}h2{font-size:18px;margin:0 0 8px}h3{font-size:15px;margin:12px 0 6px}
label{display:block;font-size:13px;font-weight:600;margin:10px 0 2px;color:#344054}
input[type=text],input[type=tel],input[type=email],input[type=url],input[type=number],input[type=datetime-local],select,textarea{
 width:100%;padding:12px;font-size:16px;border:1px solid #cbd2dd;border-radius:8px;background:#fff}
textarea{min-height:80px}
button,.btn{display:inline-block;width:100%;margin:12px 0 4px;padding:13px;font-size:16px;font-weight:700;color:#fff;background:#b42318;border:0;border-radius:8px;text-align:center;text-decoration:none;cursor:pointer}
.btn.secondary,a.btn.secondary{background:#344054}
.btn.small,form.inline{display:inline-block;width:auto;margin:4px}
.flash{background:#eef6ee;border:1px solid #b7d9b7;color:#1d5427;border-radius:8px;padding:10px 12px;margin:10px 0;font-size:14px}
.error{background:#fdecea;border:1px solid #f1b0a7;color:#8a1c11;border-radius:8px;padding:10px 12px;margin:10px 0;font-size:14px}
.meta{font-size:13px;color:#667085}
.chip{display:inline-block;font-size:12px;font-weight:700;padding:2px 10px;border-radius:999px;background:#fef0c7;color:#7a5b00}
.chip.open{background:#d1fadf;color:#08522b}.chip.closed{background:#fdecea;color:#8a1c11}.chip.drawn{background:#e0eaff;color:#1849a9}
.badge{font-size:11px;font-weight:800;letter-spacing:1px;color:#b42318;border:2px solid #b42318;border-radius:6px;padding:1px 6px}
.dropimg{width:100%;max-height:260px;object-fit:cover;border-radius:10px;background:#eef0f4}
footer{margin-top:24px;padding:16px 0;font-size:12px;color:#667085;border-top:1px solid #e4e7ec}
table{width:100%;border-collapse:collapse;font-size:13px}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #eaecf0;word-break:break-word}
.compliance{background:#fffaeb;border:1px solid #f2c94c;border-radius:8px;padding:10px 12px;font-size:12px;margin:12px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
@media(max-width:480px){.grid{grid-template-columns:1fr}.card{padding:14px}}
"""

def page(title, content, code=200, extra_head=""):
    st = store()
    body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · DropStack</title><style>{CSS}</style>{extra_head}</head>
<body>
<header><div class="wrap">
<a class="logo" href="/">Drop<span>Stack</span></a>
<nav><span class="store-name">{esc(store()['name'])}</span> <a href="/">Drops</a> <a href="/admin">Admin</a></nav>
</div></header>
<div class="wrap">
{content}
<footer>
<p><span class="badge">21+ ONLY</span> {esc(DISCLAIMER)}</p>
<p>{esc(st['name'])} · License No. {esc(st['license_number'])}</p>
</footer>
</div></body></html>"""
    return code, body.encode("utf-8")

def status_chip(status, d=None):
    label = status
    if d is not None and status == "open" and deadline_passed(d):
        label = "closed"
    return f'<span class="chip {esc(label)}">{esc(label.upper())}</span>'

def drop_card(d):
    return f"""<div class="card">
<h2><a href="/drops/{d['id']}" style="color:inherit;text-decoration:none">{esc(d['title'])}</a> {status_chip(d['status'], d)}</h2>
<p class="meta">{esc(d['description'][:140])}</p>
<p class="meta">${esc('%.2f' % d['price'])} · Qty available: {esc(d['quantity'])} · Entries close: {esc(fmt_dt(d['deadline']))} · <a href="/drops/{d['id']}/winners">Winners</a></p>
</div>"""

def compliance_block(drop=None):
    rules = ""
    if drop:
        row = db().execute("SELECT rules_text FROM state_rules WHERE store_id=? AND state_code=?",
                           (STORE_ID, drop["state_code"])).fetchone()
        state_rules = row["rules_text"] if row else ""
        rules = f"<p>{esc(state_rules)}</p>"
        if drop["rules_text"]:
            rules += f"<p><strong>Drop rules:</strong> {esc(drop['rules_text'])}</p>"
    return f"""<div class="compliance"><strong>{esc(NO_PURCHASE)}</strong>{rules}
<p>Open to legal residents of the applicable state, 21+. Employees of the store and their immediate families are ineligible. Winner will be contacted and must present valid government ID proving 21+ to claim.</p></div>"""

def csv_safe(v):
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s

def fmt_dt(iso):
    iso = (iso or "").strip()
    if not iso:
        return "no deadline"
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso

# ---------------------------------------------------------------- http base

class Handler(BaseHTTPRequestHandler):
    server_version = "DropStack/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def client_ip(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

    def cookies(self):
        c = SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            c.load(raw)
        return {k: m.value for k, m in c.items()}

    def send_resp(self, code, body, headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, loc, headers=None):
        h = {"Location": loc}
        h.update(headers or {})
        self.send_resp(303, "", h)

    def set_cookie(self, name, value, max_age, http_only=True, path="/"):
        parts = [f"{name}={urllib.parse.quote(value)}", f"Max-Age={max_age}",
                 f"Path={path}", "SameSite=Strict"]
        if SECURE_COOKIES:
            parts.append("Secure")
        if http_only:
            parts.append("HttpOnly")
        return ("Set-Cookie", "; ".join(parts))

    def form(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            self.send_resp(413, '<div class="card"><h1>Payload too large</h1></div>')
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0]
        if ctype == "application/x-www-form-urlencoded":
            return {k: v for k, v in urllib.parse.parse_qsl(raw, keep_blank_values=True)}
        return {}

    def is_admin(self):
        tok = self.cookies().get(SESSION_COOKIE, "")
        if not tok:
            return False
        with DB_LOCK:
            row = db().execute("SELECT * FROM sessions WHERE token=?", (tok,)).fetchone()
            if not row:
                return False
            if row["expires_at"] < time.time():
                db().execute("DELETE FROM sessions WHERE token=?", (tok,))
                db().commit()
                return False
            return True

    def require_admin(self):
        if self.is_admin():
            return True
        self.send_resp(302, "", {"Location": "/admin/login"})
        return False

    def page(self, title, content, code=200):
        c, b = page(title, content, code)
        return c, b

# ---------------------------------------------------------------- public routes

    def route_home(self, qs=None):
        qs = qs or {}
        with DB_LOCK:
            drops = db().execute("SELECT * FROM drops WHERE store_id=? ORDER BY id DESC", (STORE_ID,)).fetchall()
        cards = "".join(drop_card(d) for d in drops) or \
            '<div class="card"><h2>No drops yet</h2><p class="meta">Check back soon — new allocation drops are posted here first. Sign up below for a text when something lands.</p></div>'
        flash = ""
        if qs.get("subscribed") == ["1"]:
            flash = '<div class="flash">You are on the list! We will text or email you when something restocks.</div>'
        elif qs.get("error"):
            flash = f'<div class="error">{esc(qs["error"][0][:200])}</div>'
        content = f"""{flash}
<div class="card">
<h1>Allocation drops, fairly.</h1>
<p class="meta">Enter the raffle for limited-release bottles. No purchase necessary. Winners are drawn verifiably at random.</p>
</div>
{cards}
<div class="card" id="restock">
<h2>Restock alerts</h2>
<p class="meta">Get a text or email when we restock. Msg&amp;data rates may apply. Reply STOP to opt out any time.</p>
<form method="post" action="/restock">
<label for="rs-phone">Mobile phone (for SMS)</label>
<input type="tel" id="rs-phone" name="phone" placeholder="(555) 123-4567" autocomplete="tel">
<label for="rs-email">Email (optional)</label>
<input type="email" id="rs-email" name="email" placeholder="you@example.com" autocomplete="email">
<button type="submit">Notify me</button>
</form>
</div>"""
        return self.page("Home", content)

    def route_agegate(self, qs=None):
        nxt = qs.get("next", ["/"])[0]
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        content = f"""<div class="card" style="text-align:center">
<h1>Are you 21 or older?</h1>
<p class="meta">You must be of legal drinking age to view or enter drops.</p>
<form method="post" action="/age-gate">
<input type="hidden" name="next" value="{esc(nxt)}">
<button type="submit">Yes, I am 21+</button>
</form>
<a class="btn secondary" href="/">I am under 21 — take me back</a>
</div>"""
        return self.page("Age verification", content)

    def route_agegate_post(self):
        f = self.form()
        if f is None:
            return
        nxt = f.get("next", "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        self.redirect(nxt, headers=[self.set_cookie(AGE_COOKIE, "1", 86400, http_only=False)])

    def route_drop(self, drop_id, qs=None):
        qs = qs or {}
        if self.cookies().get(AGE_COOKIE) != "1":
            return self.redirect(f"/age-gate?next={urllib.parse.quote('/drops/' + str(drop_id))}")
        with DB_LOCK:
            d = db().execute("SELECT * FROM drops WHERE id=? AND store_id=?", (drop_id, STORE_ID)).fetchone()
        if not d:
            return self.page("Not found", '<div class="card"><h1>Drop not found</h1></div>', 404)
        n_entries = db().execute("SELECT COUNT(*) c FROM entries WHERE drop_id=?", (drop_id,)).fetchone()["c"]
        drawn = d["status"] == "drawn"
        img = f'<img class="dropimg" src="{esc(d["photo_url"])}" alt="Photo of {esc(d["title"])}">' if valid_url(d["photo_url"]) else ""
        flash = ""
        if qs.get("entered") == ["1"]:
            flash = '<div class="flash">You are entered. Good luck! Winners are posted on the winners page and you will be contacted directly.</div>'
        err = qs.get("error") or [""]
        if err[0]:
            flash = f'<div class="error">{esc(err[0])}</div>'
        entry_form = ""
        if drop_is_open(d):
            entry_form = f"""<div class="card">
<h2>Enter this raffle</h2>
<p class="meta">{n_entries} entries so far · one entry per person (phone and email)</p>
<form method="post" action="/drops/{d['id']}/entry" id="entry-form">
<label for="e-name">Full name *</label>
<input type="text" id="e-name" name="name" required maxlength="100" autocomplete="name" placeholder="Jane Doe">
<label for="e-phone">Mobile phone *</label>
<input type="tel" id="e-phone" name="phone" required autocomplete="tel" placeholder="(555) 123-4567">
<label for="e-email">Email *</label>
<input type="email" id="e-email" name="email" required autocomplete="email" placeholder="you@example.com">
<label style="display:flex;align-items:center;gap:8px;font-weight:400">
<input type="checkbox" id="e-cust" name="customer_since" value="yes" style="width:auto">
I have been a customer of this store (helps tie-break, optional)
</label>
{compliance_block(d)}
<button type="submit" id="submit-btn" onclick="this.disabled=true;this.value='Submitting...';this.form.submit()">Submit free entry</button>
</form>
</div>"""
        elif d["status"] == "open" and deadline_passed(d):
            entry_form = '<div class="card"><p class="meta">Entry deadline has passed. The draw has not run yet — check the <a href="/drops/' + str(d["id"]) + '/winners">winners page</a>.</p></div>'
        elif not drawn:
            entry_form = '<div class="card"><p class="meta">Entries are closed for this drop.</p></div>'
        winners_link = f'<a class="btn secondary" href="/drops/{d["id"]}/winners">See winners</a>' if drawn else ""
        content = f"""
<div class="card">
{img}
<h1>{esc(d['title'])} {status_chip(d['status'], d)}</h1>
<p>{esc(d['description'])}</p>
<p class="meta"><strong>${esc('%.2f' % d['price'])}</strong> · Bottles allocated: {esc(d['quantity'])} · Entries close: {esc(fmt_dt(d['deadline']))}</p>
{winners_link}
</div>
{flash}
{entry_form}
{compliance_block(d)}"""
        return self.page(d["title"], content)

    def route_entry(self, drop_id):
        ip = self.client_ip()
        if rate_limited("entry:" + ip, ENTRY_RATE_LIMIT):
            return self.send_resp(429, '<div class="card"><h1>Too many requests</h1><p class="meta">Please wait a minute and try again.</p></div>')
        if self.cookies().get(AGE_COOKIE) != "1":
            return self.redirect(f"/age-gate?next={urllib.parse.quote('/drops/' + str(drop_id))}")
        f = self.form()
        if f is None:
            return
        def fail(msg, code=400):
            return self.redirect(f"/drops/{drop_id}?error={urllib.parse.quote(msg)}") if code == 400 else \
                self.send_resp(code, self.page("Error", f'<div class="card"><h1>Error</h1><p class="meta">{esc(msg)}</p></div>')[1])
        with DB_LOCK:
            d = db().execute("SELECT * FROM drops WHERE id=? AND store_id=?", (drop_id, STORE_ID)).fetchone()
        if not d:
            return self.send_resp(404, self.page("Not found", '<div class="card"><h1>Drop not found</h1></div>', 404)[1])
        if not drop_is_open(d):
            return fail("This drop is not accepting entries.", 410)
        name = (f.get("name") or "").strip()
        phone = norm_phone(f.get("phone"))
        email = norm_email(f.get("email"))
        if not (2 <= len(name) <= 100) or re.search(r"[<>]", name):
            return fail("Please enter a valid full name (2–100 characters).")
        if not phone:
            return fail("Please enter a valid 10-digit US mobile number.")
        if not email:
            return fail("Please enter a valid email address.")
        customer_since = datetime.now().year if (f.get("customer_since") or "") == "yes" else None
        try:
            with DB_LOCK:
                cur = db().execute(
                    "INSERT INTO entries(drop_id,store_id,name,phone,email,customer_since,ip,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (d["id"], STORE_ID, name, phone, email, customer_since, ip, now_iso()))
                db().commit()
                entry_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return self.send_resp(409, self.page("Already entered",
                f'<div class="card"><h1>You are already entered</h1>'
                f'<p class="meta">We already have an entry for that phone or email on this drop — '
                f'one entry per person keeps it fair for everyone.</p>'
                f'<a class="btn secondary" href="/drops/{d["id"]}">Back to the drop</a></div>')[1])
        return self.redirect(f"/drops/{drop_id}?entered=1")

    def route_winners(self, drop_id):
        with DB_LOCK:
            d = db().execute("SELECT * FROM drops WHERE id=? AND store_id=?", (drop_id, STORE_ID)).fetchone()
            dr = db().execute("SELECT * FROM draws WHERE drop_id=?", (drop_id,)).fetchone()
        if not d:
            return self.page("Not found", '<div class="card"><h1>Drop not found</h1></div>', 404)
        if not dr:
            return self.page("Winners", f'<div class="card"><h1>{esc(d["title"])} — Winners</h1><p class="meta">The draw has not happened yet. Entries '
                                     + ("closed" if d["status"] != "open" else "are still open")
                                     + "; check back after the deadline.</p></div>")
        winners = json.loads(dr["winners"])
        alts = json.loads(dr["alternates"])

        def rows(ids):
            out = ""
            for i, eid in enumerate(ids, 1):
                e = db().execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
                if e:
                    out += f"<tr><td>{i}</td><td>{esc(e['name'])}</td><td>{esc(e['email'])}</td></tr>"
            return out or '<tr><td colspan="3">None</td></tr>'
        content = f"""<div class="card">
<h1>{esc(d['title'])} — Winners</h1>
<p class="meta">Drawn {esc(dr['drawn_at'])}. Audit seed: <code>{esc(dr['seed'][:16])}…</code> <a href="/drops/{d['id']}/verify-draw">verify draw</a></p>
<h3>Winner{'s' if len(winners) > 1 else ''}</h3>
<div class="tablewrap"><table><tr><th>#</th><th>Name</th><th>Email</th></tr>{rows(winners)}</table></div>
<h3>Alternates (in order)</h3>
<div class="tablewrap"><table><tr><th>#</th><th>Name</th><th>Email</th></tr>{rows(alts)}</table></div>
<p class="meta">Winners must show valid ID proving 21+ to claim. Unclaimed bottles roll to alternates in order.</p>
{compliance_block(d)}
</div>"""
        return self.page("Winners", content)

    def route_verify_draw(self, drop_id):
        with DB_LOCK:
            d = db().execute("SELECT * FROM drops WHERE id=? AND store_id=?", (drop_id, STORE_ID)).fetchone()
            dr = db().execute("SELECT * FROM draws WHERE drop_id=?", (drop_id,)).fetchone()
        if not d or not dr:
            return self.send_resp(404, "not found", {"Content-Type": "text/plain"})
        entry_ids = json.loads(dr["entry_ids"])
        ok = draw_mod.verify(dr["seed"], entry_ids, d["quantity"], ALTERNATES,
                             json.loads(dr["winners"]), json.loads(dr["alternates"]))
        # also confirm no phantom entries
        placeholders = ",".join("?" * len(entry_ids))
        live = [r["id"] for r in db().execute(
            f"SELECT id FROM entries WHERE drop_id=? AND id IN ({placeholders})", [d["id"]] + entry_ids)] if entry_ids else []
        ok = ok and sorted(live) == sorted(entry_ids)
        payload = {"drop_id": d["id"], "ok": bool(ok), "seed": dr["seed"],
                   "entry_ids": entry_ids, "winners": json.loads(dr["winners"]),
                   "alternates": json.loads(dr["alternates"])}
        return self.send_resp(200 if ok else 500, json.dumps(payload, indent=2),
                              {"Content-Type": "application/json"})

    def route_restock_post(self):
        ip = self.client_ip()
        if rate_limited("restock:" + ip, 5):
            return self.send_resp(429, self.page("Slow down", '<div class="card"><h1>Too many requests</h1></div>', 429)[1], )
        f = self.form()
        if f is None:
            return
        phone = norm_phone(f.get("phone"))
        email = norm_email(f.get("email"))
        if not phone and not email:
            return self.redirect("/?error=" + urllib.parse.quote("Please provide a valid phone or email."))
        tok = secrets.token_urlsafe(24)
        try:
            with DB_LOCK:
                db().execute("INSERT INTO restock(store_id,phone,email,unsub_token,created_at) VALUES(?,?,?,?,?)",
                             (STORE_ID, phone, email, tok, now_iso()))
                db().commit()
        except sqlite3.IntegrityError:
            return self.redirect("/?subscribed=1#restock")
        if phone:
            get_sms_provider().send(phone, "You're subscribed to restock alerts at Sunset Spirits. Reply STOP to opt out.")
        if email:
            get_email_provider().send(email, "You're subscribed to restock alerts.", "Restock alerts signup")
        return self.redirect("/?subscribed=1#restock")

    def route_unsub(self, token):
        with DB_LOCK:
            cur = db().execute("UPDATE restock SET unsubscribed=1 WHERE unsub_token=?", (token,))
            db().commit()
        found = cur.rowcount > 0
        return self.page("Unsubscribed", f'<div class="card"><h1>{"Opted out" if found else "Link not found"}</h1>'
                         f'<p class="meta">{"You will no longer receive restock alerts." if found else "That unsubscribe link is not valid."}</p></div>')

# ---------------------------------------------------------------- admin

    def route_admin_login(self, error=False):
        err = '<div class="error">Invalid password.</div>' if error else ""
        content = f"""<div class="card">
<h1>Admin sign in</h1>{err}
<form method="post" action="/admin/login">
<label for="pw">Password</label>
<input type="password" id="pw" name="password" required autocomplete="current-password">
<button type="submit">Sign in</button>
</form></div>"""
        return self.page("Admin login", content)

    def route_admin_login_post(self):
        ip = self.client_ip()
        if rate_limited("login:" + ip, 5):
            return self.page("Admin login", '<div class="card"><h1>Too many attempts</h1><p class="meta">Wait a minute and try again.</p></div>', 429)
        f = self.form()
        if f is None:
            return
        pw = f.get("password", "")
        ok = bool(ADMIN_PASSWORD) and secrets.compare_digest(pw, ADMIN_PASSWORD)
        if not ok:
            return self.route_admin_login(error=True)
        tok = secrets.token_urlsafe(32)
        with DB_LOCK:
            db().execute("INSERT INTO sessions(token,expires_at) VALUES(?,?)", (tok, time.time() + 86400))
            db().commit()
        return self.redirect("/admin", headers=[self.set_cookie(SESSION_COOKIE, tok, 86400)])

    def route_admin_logout_post(self):
        tok = self.cookies().get(SESSION_COOKIE, "")
        with DB_LOCK:
            db().execute("DELETE FROM sessions WHERE token=?", (tok,))
            db().commit()
        return self.redirect("/admin/login", headers=[self.set_cookie(SESSION_COOKIE, "", 0)])

    def route_admin(self):
        if not self.require_admin():
            return
        with DB_LOCK:
            drops = db().execute("SELECT * FROM drops WHERE store_id=? ORDER BY id DESC", (STORE_ID,)).fetchall()
            states = [r["state_code"] for r in db().execute(
                "SELECT state_code FROM state_rules WHERE store_id=? ORDER BY state_code", (STORE_ID,)).fetchall()]
        rows = ""
        for d in drops:
            n = db().execute("SELECT COUNT(*) c FROM entries WHERE drop_id=?", (d["id"],)).fetchone()["c"]
            rows += (f'<tr><td><a href="/admin/drops/{d["id"]}">{d["id"]}</a></td>'
                     f'<td>{esc(d["title"])}</td><td>{status_chip(d["status"], d)}</td>'
                     f'<td>{n}</td></tr>')
        rows = rows or '<tr><td colspan="4">No drops yet — create your first one below.</td></tr>'
        opts = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in states)
        content = f"""
<div class="card">
<h1>Drops</h1>
<form method="post" action="/admin/logout" style="margin:0"><button class="btn small secondary">Log out</button></form>
<div class="tablewrap"><table><tr><th>ID</th><th>Title</th><th>Status</th><th>Entries</th></tr>{rows}</table></div>
<p><a class="btn secondary" href="/admin/restock">Restock alert list</a></p>
</div>
<div class="card">
<h2>Create a drop</h2>
<form method="post" action="/admin/drops">
<div class="grid">
<div><label for="t">Title *</label><input type="text" id="t" name="title" required maxlength="120"></div>
<div><label for="p">Price (USD) *</label><input type="number" id="p" name="price" step="0.01" min="0" required value="0"></div>
<div><label for="q">Bottles allocated *</label><input type="number" id="q" name="quantity" min="1" required value="1"></div>
<div><label for="d">Entry deadline</label><input type="datetime-local" id="d" name="deadline"></div>
<div><label for="s">State rules</label><select id="s" name="state_code">{opts}</select></div>
<div><label for="u">Photo URL (https)</label><input type="url" id="u" name="photo_url" placeholder="https://..."></div>
</div>
<label for="desc">Description</label><textarea id="desc" name="description"></textarea>
<label for="r">Rules text (shown on entry form)</label><textarea id="r" name="rules_text"></textarea>
<button type="submit">Create drop</button>
</form>
</div>"""
        return self.page("Admin", content)

    def route_admin_create_drop(self):
        f = self.form()
        if f is None:
            return
        title = (f.get("title") or "").strip()
        try:
            price = float(f.get("price") or 0)
            quantity = int(f.get("quantity") or 1)
        except ValueError:
            return self.page("Error", '<div class="card"><h1>Invalid price or quantity</h1><p class="meta">Go back and fix the form.</p></div>', 400)
        if not (2 <= len(title) <= 120) or price < 0 or quantity < 1 or quantity > 1000:
            return self.page("Error", '<div class="card"><h1>Invalid drop data</h1><p class="meta">Title 2–120 chars, price >= 0, quantity 1–1000.</p></div>', 400)
        deadline = (f.get("deadline") or "").strip()
        if deadline:
            try:
                datetime.fromisoformat(deadline)
            except ValueError:
                return self.page("Error", '<div class="card"><h1>Invalid deadline</h1></div>', 400)
        state = (f.get("state_code") or "FL").strip().upper()[:2]
        photo = (f.get("photo_url") or "").strip()
        if photo and not valid_url(photo):
            return self.page("Error", '<div class="card"><h1>Invalid photo URL</h1><p class="meta">Must start with http:// or https://</p></div>', 400)
        with DB_LOCK:
            cur = db().execute(
                """INSERT INTO drops(store_id,title,description,photo_url,price,quantity,rules_text,state_code,deadline,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,'open',?)""",
                (STORE_ID, title, (f.get("description") or "")[:2000], photo, price, quantity,
                 (f.get("rules_text") or "")[:2000], state, deadline, now_iso()))
            db().commit()
        return self.redirect(f"/admin/drops/{cur.lastrowid}")

    def route_admin_drop(self, drop_id):
        if not self.require_admin():
            return
        with DB_LOCK:
            d = db().execute("SELECT * FROM drops WHERE id=? AND store_id=?", (drop_id, STORE_ID)).fetchone()
            dr = db().execute("SELECT * FROM draws WHERE drop_id=?", (drop_id,)).fetchone()
            entries = db().execute("SELECT * FROM entries WHERE drop_id=? ORDER BY id", (drop_id,)).fetchall()
        if not d:
            return self.page("Not found", '<div class="card"><h1>Drop not found</h1></div>', 404)
        erows = "".join(
            f'<tr><td>{e["id"]}</td><td>{esc(e["name"])}</td><td>{esc(e["phone"])}</td><td>{esc(e["email"])}</td>'
            f'<td>{"yes" if e["customer_since"] else ""}</td><td class="meta">{esc(e["created_at"])}</td></tr>'
            for e in entries) or '<tr><td colspan="6">No entries yet. Share the public link: /drops/' + str(d["id"]) + '</td></tr>'
        actions = ""
        if d["status"] == "open":
            actions += f'<form class="inline" method="post" action="/admin/drops/{d["id"]}/close"><button class="btn small secondary">Close entries</button></form>'
        if d["status"] in ("open", "closed"):
            actions += f'<form class="inline" method="post" action="/admin/drops/{d["id"]}/draw"><button class="btn small">Run draw now</button></form>'
        draw_info = ""
        if dr:
            win_names = ", ".join(str(db().execute("SELECT name FROM entries WHERE id=?", (w,)).fetchone()["name"]) for w in json.loads(dr["winners"]))
            alt_names = ", ".join(str(db().execute("SELECT name FROM entries WHERE id=?", (w,)).fetchone()["name"]) for w in json.loads(dr["alternates"])) or "none"
            draw_info = f"""<div class="card"><h3>Draw audit</h3>
<p class="meta">Seed: <code>{esc(dr['seed'])}</code><br>Winners: {esc(win_names)}<br>Alternates: {esc(alt_names)}<br>
Python used: {esc(dr['python_version'])} · Drawn: {esc(dr['drawn_at'])} ·
<a href="/drops/{d['id']}/verify-draw">verify</a></p></div>"""
        content = f"""
<div class="card">
<h1>Drop #{d['id']}: {esc(d['title'])} {status_chip(d['status'], d)}</h1>
<p class="meta">${esc('%.2f' % d['price'])} · alloc {esc(d['quantity'])} · state {esc(d['state_code'])} · deadline {esc(fmt_dt(d['deadline']))} ·
<a href="/drops/{d['id']}" target="_blank">public page</a> (<code>{esc(d['id'])}</code>) · <a href="/admin/drops/{d['id']}/entries.csv">CSV export</a> · <a href="/admin">all drops</a></p>
{actions}
</div>
{draw_info}
<div class="card"><h2>Entries ({len(entries)})</h2>
<div class="tablewrap"><table><tr><th>ID</th><th>Name</th><th>Phone</th><th>Email</th><th>Cust.</th><th>Entered</th></tr>{erows}</table></div>
</div>"""
        return self.page("Admin drop", content)

    def route_admin_close(self, drop_id):
        if not self.require_admin():
            return
        with DB_LOCK:
            cur = db().execute("UPDATE drops SET status='closed' WHERE id=? AND store_id=? AND status='open'", (drop_id, STORE_ID))
            db().commit()
        return self.redirect(f"/admin/drops/{drop_id}")

    def route_admin_draw(self, drop_id):
        if not self.require_admin():
            return
        with DB_LOCK:
            d = db().execute("SELECT * FROM drops WHERE id=? AND store_id=?", (drop_id, STORE_ID)).fetchone()
            if not d:
                return self.page("Not found", '<div class="card"><h1>Drop not found</h1></div>', 404)
            if d["status"] == "drawn":
                return self.redirect(f"/admin/drops/{drop_id}")
            entry_ids = [r["id"] for r in db().execute("SELECT id FROM entries WHERE drop_id=?", (drop_id,)).fetchall()]
            if not entry_ids:
                return self.page("No entries", '<div class="card"><h1>Cannot draw: no entries yet.</h1></div>', 400)
            seed = secrets.token_hex(32)
            _, winners, alts = draw_mod.perform_draw(seed, entry_ids, d["quantity"], ALTERNATES)
            db().execute(
                """INSERT INTO draws(drop_id,store_id,seed,entry_ids,winners,alternates,python_version,drawn_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (drop_id, STORE_ID, seed, json.dumps(sorted(entry_ids)), json.dumps(winners),
                 json.dumps(alts), sys.version.split()[0], now_iso()))
            db().execute("UPDATE drops SET status='drawn' WHERE id=?", (drop_id,))
            db().commit()
        sms, email = get_sms_provider(), get_email_provider()
        for eid in winners + alts:
            e = db().execute("SELECT * FROM entries WHERE id=?", (eid,)).fetchone()
            tag = "WINNER" if eid in winners else "ALTERNATE"
            msg = f"DropStack: you are a {tag} for '{d['title']}' at Sunset Spirits! Show this message + ID at the counter."
            try:
                sms.send(e["phone"], msg)
                email.send(e["email"], msg, f"You're a {tag}!")
            except NotImplementedError as ex:
                sys.stderr.write(f"provider not wired: {ex}\n")
        return self.redirect(f"/admin/drops/{drop_id}")

    def route_admin_csv(self, drop_id):
        if not self.require_admin():
            return
        with DB_LOCK:
            d = db().execute("SELECT * FROM drops WHERE id=? AND store_id=?", (drop_id, STORE_ID)).fetchone()
            entries = db().execute("SELECT * FROM entries WHERE drop_id=? ORDER BY id", (drop_id,)).fetchall()
        if not d:
            return self.send_resp(404, "not found", {"Content-Type": "text/plain"})
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["entry_id", "drop_id", "store_id", "name", "phone", "email", "customer_since", "ip", "created_at"])
        for e in entries:
            w.writerow([e["id"], e["drop_id"], e["store_id"], csv_safe(e["name"]), csv_safe(e["phone"]),
                        csv_safe(e["email"]), e["customer_since"], csv_safe(e["ip"]), e["created_at"]])
        self.send_resp(200, buf.getvalue().encode("utf-8"), {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="drop-{drop_id}-entries.csv"'})

    def route_admin_restock(self):
        if not self.require_admin():
            return
        with DB_LOCK:
            subs = db().execute("SELECT * FROM restock WHERE store_id=? ORDER BY id DESC", (STORE_ID,)).fetchall()
        rows = "".join(
            f'<tr><td>{s["id"]}</td><td>{esc(s["phone"] or "")}</td><td>{esc(s["email"] or "")}</td>'
            f'<td>{"OPTED OUT" if s["unsubscribed"] else "subscribed"}</td>'
            f'<td><a href="/restock/unsubscribe/{esc(s["unsub_token"])}">unsub</a></td></tr>'
            for s in subs) or '<tr><td colspan="5">Nobody signed up yet.</td></tr>'
        content = f"""<div class="card"><h1>Restock alerts ({len(subs)})</h1>
<p class="meta"><a href="/admin">back to drops</a></p>
<div class="tablewrap"><table><tr><th>ID</th><th>Phone</th><th>Email</th><th>Status</th><th></th></tr>{rows}</table></div>
<p class="meta">Opt-outs are honored: opted-out contacts are never sent alerts by any sending code path.</p></div>"""
        return self.page("Restock list", content)

# ---------------------------------------------------------------- dispatch

    PATS = [
        ("GET",  r"^/$", "route_home"),
        ("GET",  r"^/age-gate$", "route_agegate"),
        ("POST", r"^/age-gate$", "route_agegate_post"),
        ("GET",  r"^/drops/(?P<drop_id>\d+)$", "route_drop"),
        ("POST", r"^/drops/(?P<drop_id>\d+)/entry$", "route_entry"),
        ("GET",  r"^/drops/(?P<drop_id>\d+)/winners$", "route_winners"),
        ("GET",  r"^/drops/(?P<drop_id>\d+)/verify-draw$", "route_verify_draw"),
        ("POST", r"^/restock$", "route_restock_post"),
        ("GET",  r"^/restock/unsubscribe/(?P<token>[\w\-]+)$", "route_unsub"),
        ("GET",  r"^/admin/login$", "route_admin_login"),
        ("POST", r"^/admin/login$", "route_admin_login_post"),
        ("POST", r"^/admin/logout$", "route_admin_logout_post"),
        ("GET",  r"^/admin$", "route_admin"),
        ("POST", r"^/admin/drops$", "route_admin_create_drop"),
        ("GET",  r"^/admin/drops/(?P<drop_id>\d+)/entries\.csv$", "route_admin_csv"),
        ("GET",  r"^/admin/drops/(?P<drop_id>\d+)$", "route_admin_drop"),
        ("POST", r"^/admin/drops/(?P<drop_id>\d+)/close$", "route_admin_close"),
        ("POST", r"^/admin/drops/(?P<drop_id>\d+)/draw$", "route_admin_draw"),
        ("GET",  r"^/admin/restock$", "route_admin_restock"),
    ]

    QS_ROUTES = {"route_home", "route_agegate"}

    def handle_request(self, method):
        try:
            self._dispatch(method)
        except Exception:
            import traceback
            traceback.print_exc()
            try:
                self.send_resp(500, '<div class="card"><h1>Something went wrong</h1>'
                               '<p class="meta">Please try again or contact the store.</p></div>')
            except Exception:
                pass

    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        for m, pat, fn in self.PATS:
            if m != method:
                continue
            mt = re.match(pat, path)
            if mt:
                kw = {k: int(v) if k == "drop_id" else v for k, v in mt.groupdict().items()}
                handler = getattr(self, fn)
                res = handler(qs, **kw) if fn in self.QS_ROUTES else handler(**kw)
                if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], int):
                    self.send_resp(res[0], res[1])
                elif res is not None:
                    self.send_resp(500, "handler contract violation")
                return
        self.send_resp(404, '<div class="card"><h1>404 — page not found</h1><p><a href="/">Back to drops</a></p></div>')

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def do_HEAD(self):
        self.handle_request("GET")


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True

def main():
    if not ADMIN_PASSWORD:
        print("WARNING: ADMIN_PASSWORD not set — admin login is disabled (fail closed).", file=sys.stderr)
    init_db()
    srv = Server((HOST, PORT), Handler)
    print(f"DropStack listening on http://{HOST}:{PORT} (db: {DB_PATH}, store: {STORE_ID})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
