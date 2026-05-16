"""
FFGlory - Flask backend (app.py)
================================
Serves the existing static frontend (login.html, client.html, shared.css, shared.js)
and exposes the JSON API consumed by it.

Quick start
-----------
    pip install -r requirements.txt
    export FLASK_SECRET="change-me"
    export FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
    python app.py                       # dev server on :5000
    gunicorn app:app -w 2 -b 0.0.0.0:8000  # production

Deploy: Render / Railway. See Procfile + render.yaml shipped alongside.

IMPORTANT — Free Fire integration
---------------------------------
The CS / glory-push automation is intentionally LEFT AS A PLUGIN.
Look for the `# >>> FF_PLUGIN` markers in `ff_runner.py` (created next to this
file). Drop your existing reverse-engineered guest-login + lobby-create +
match-start logic there. The web layer already orchestrates queue, retries,
status updates and logs.
"""
from __future__ import annotations

import os
import json
import time
import secrets
import sqlite3
import threading
import datetime as dt
from functools import wraps
from typing import Any, Optional

from flask import Flask, request, jsonify, g, send_from_directory, abort
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
from cryptography.fernet import Fernet

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR  = os.environ.get("STATIC_DIR", os.path.join(BASE_DIR, "public"))
DB_PATH     = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "ffglory.db"))
SECRET      = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
FERNET_KEY  = os.environ.get("FERNET_KEY") or Fernet.generate_key().decode()
JWT_ALG     = "HS256"
JWT_TTL     = 60 * 60 * 24 * 7  # 7 days

fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)

app = Flask(__name__, static_folder=None)
app.config["SECRET_KEY"] = SECRET
CORS(app, supports_credentials=True)

# ──────────────────────────────────────────────────────────────────────────────
# Database (SQLite — swap to Postgres on Render by switching connector)
# ──────────────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    email         TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'client',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS guest_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    uid             TEXT NOT NULL,
    password_enc    TEXT NOT NULL,        -- Fernet-encrypted
    nickname        TEXT,
    region          TEXT DEFAULT 'IND',
    glory_current   INTEGER DEFAULT 0,
    glory_target    INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'idle',  -- idle|queued|running|paused|error
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(owner_id, uid)
);
CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    captain_uid TEXT,                     -- main guest account UID
    status      TEXT DEFAULT 'idle',      -- idle|lobby|running|stopped|error
    config_json TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS group_members (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    guest_id  INTEGER NOT NULL REFERENCES guest_accounts(id) ON DELETE CASCADE,
    role      TEXT DEFAULT 'member',      -- captain|member|invited
    UNIQUE(group_id, guest_id)
);
CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    group_id   INTEGER,
    kind       TEXT NOT NULL,              -- login|cs_start|cs_win|glory|error|...
    message    TEXT NOT NULL,
    meta_json  TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS inbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS coupons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT UNIQUE NOT NULL,
    discount   INTEGER NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS redeemed_coupons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    coupon_id  INTEGER NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
    redeemed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, coupon_id)
);
CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount     INTEGER NOT NULL,
    currency   TEXT NOT NULL DEFAULT 'INR',
    status     TEXT NOT NULL DEFAULT 'pending',
    method     TEXT,
    ref        TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL DEFAULT 'info',  -- info|warn|critical
    message    TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

def db() -> sqlite3.Connection:
    conn = getattr(g, "_db", None)
    if conn is None:
        conn = g._db = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    return conn

@app.teardown_appcontext
def _close_db(_):
    conn = getattr(g, "_db", None)
    if conn is not None: conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def encrypt(s: str) -> str: return fernet.encrypt(s.encode()).decode()
def decrypt(s: str) -> str: return fernet.decrypt(s.encode()).decode()

def make_token(user_id: int, role: str) -> str:
    payload = {"uid": user_id, "role": role,
               "exp": int(time.time()) + JWT_TTL,
               "iat": int(time.time())}
    return jwt.encode(payload, SECRET, algorithm=JWT_ALG)

def auth_required(role: Optional[str] = None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            hdr = request.headers.get("Authorization", "")
            tok = hdr[7:] if hdr.startswith("Bearer ") else request.cookies.get("ff_token")
            if not tok: return jsonify(error="auth required"), 401
            try:
                data = jwt.decode(tok, SECRET, algorithms=[JWT_ALG])
            except jwt.PyJWTError:
                return jsonify(error="invalid token"), 401
            if role and data.get("role") != role and data.get("role") != "admin":
                return jsonify(error="forbidden"), 403
            g.user_id = data["uid"]; g.role = data["role"]
            return fn(*a, **kw)
        return wrapper
    return deco

def log(kind: str, message: str, group_id: int | None = None, meta: dict | None = None):
    db().execute(
        "INSERT INTO activity_log(user_id, group_id, kind, message, meta_json) VALUES(?,?,?,?,?)",
        (getattr(g, "user_id", None), group_id, kind, message, json.dumps(meta or {})),
    )
    db().commit()

def row_to_dict(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r else None

# ──────────────────────────────────────────────────────────────────────────────
# Static frontend
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index_html():
    target = "client.html" if request.cookies.get("ff_token") else "login.html"
    return send_from_directory(STATIC_DIR, target)

@app.route("/<path:fname>")
def static_files(fname: str):
    full = os.path.join(STATIC_DIR, fname)
    if not os.path.isfile(full): abort(404)
    return send_from_directory(STATIC_DIR, fname)

# ──────────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/api/auth/register")
def register():
    j = request.get_json(force=True) or {}
    u, p, e = (j.get("username") or "").strip(), j.get("password") or "", j.get("email")
    if len(u) < 3 or len(p) < 6:
        return jsonify(error="username>=3, password>=6"), 400
    try:
        cur = db().execute(
            "INSERT INTO users(username,email,password_hash) VALUES(?,?,?)",
            (u, e, generate_password_hash(p)))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="user exists"), 409
    uid = cur.lastrowid
    return jsonify(token=make_token(uid, "client"), user={"id": uid, "username": u, "role": "client"})

@app.post("/api/auth/login")
def login():
    j = request.get_json(force=True) or {}
    u, p = (j.get("username") or "").strip(), j.get("password") or ""
    row = db().execute("SELECT * FROM users WHERE username=? OR email=?", (u, u)).fetchone()
    if not row or not check_password_hash(row["password_hash"], p):
        return jsonify(error="invalid credentials"), 401
    token = make_token(row["id"], row["role"])
    resp = jsonify(token=token, user={"id": row["id"], "username": row["username"], "role": row["role"]})
    resp.set_cookie("ff_token", token, max_age=JWT_TTL, httponly=True, samesite="Lax",
                    secure=not app.debug)
    return resp

@app.post("/api/auth/logout")
def logout():
    resp = jsonify(ok=True); resp.delete_cookie("ff_token"); return resp

@app.post("/api/auth/change-password")
@auth_required()
def change_password():
    j = request.get_json(force=True) or {}
    old, new = j.get("oldPassword") or "", j.get("newPassword") or ""
    if len(new) < 6: return jsonify(error="weak password"), 400
    row = db().execute("SELECT password_hash FROM users WHERE id=?", (g.user_id,)).fetchone()
    if not check_password_hash(row["password_hash"], old):
        return jsonify(error="wrong current password"), 401
    db().execute("UPDATE users SET password_hash=? WHERE id=?",
                 (generate_password_hash(new), g.user_id)); db().commit()
    return jsonify(ok=True)

@app.get("/api/auth/me")
@auth_required()
def me():
    row = db().execute("SELECT id,username,email,role,created_at FROM users WHERE id=?",
                       (g.user_id,)).fetchone()
    return jsonify(user=row_to_dict(row))

# ──────────────────────────────────────────────────────────────────────────────
# Guest accounts (the FF guest UID + password the user wants to push)
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/client/guests")
@auth_required()
def list_guests():
    rows = db().execute(
        "SELECT id,uid,nickname,region,glory_current,glory_target,status,last_error,created_at "
        "FROM guest_accounts WHERE owner_id=? ORDER BY id DESC", (g.user_id,)).fetchall()
    return jsonify(guests=[dict(r) for r in rows])

@app.post("/api/client/guests")
@auth_required()
def add_guest():
    j = request.get_json(force=True) or {}
    uid, pw = (j.get("uid") or "").strip(), j.get("password") or ""
    if not uid or not pw: return jsonify(error="uid+password required"), 400
    try:
        db().execute(
            "INSERT INTO guest_accounts(owner_id,uid,password_enc,nickname,region,glory_target) "
            "VALUES(?,?,?,?,?,?)",
            (g.user_id, uid, encrypt(pw), j.get("nickname"),
             j.get("region", "IND"), int(j.get("gloryTarget") or 0)))
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="guest already added"), 409
    log("guest_add", f"Added guest {uid}")
    return jsonify(ok=True)

@app.delete("/api/client/guests/<int:gid>")
@auth_required()
def delete_guest(gid: int):
    db().execute("DELETE FROM guest_accounts WHERE id=? AND owner_id=?", (gid, g.user_id))
    db().commit(); return jsonify(ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# Groups / Clans (team builder)
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/client/groups")
@auth_required()
def list_groups():
    rows = db().execute(
        "SELECT g.*, (SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id) AS members "
        "FROM groups g WHERE owner_id=? ORDER BY id DESC", (g.user_id,)).fetchall()
    return jsonify(groups=[dict(r) for r in rows])

@app.post("/api/client/groups")
@auth_required()
def create_group():
    j = request.get_json(force=True) or {}
    name = (j.get("name") or "").strip()
    captain = j.get("captainGuestId")
    if not name: return jsonify(error="name required"), 400
    cur = db().execute("INSERT INTO groups(owner_id,name,config_json) VALUES(?,?,?)",
                       (g.user_id, name, json.dumps(j.get("config") or {})))
    gid = cur.lastrowid
    if captain:
        cap = db().execute("SELECT uid FROM guest_accounts WHERE id=? AND owner_id=?",
                           (captain, g.user_id)).fetchone()
        if cap:
            db().execute("INSERT INTO group_members(group_id,guest_id,role) VALUES(?,?, 'captain')",
                         (gid, captain))
            db().execute("UPDATE groups SET captain_uid=? WHERE id=?", (cap["uid"], gid))
    db().commit(); log("group_create", f"Created group {name}", gid)
    return jsonify(id=gid)

@app.post("/api/client/groups/<int:gid>/invite")
@auth_required()
def invite_to_group(gid: int):
    """Invite another player's UID into this group's CS lobby."""
    j = request.get_json(force=True) or {}
    guest_id = j.get("guestId")          # one of the user's own guest accounts to invite via
    invitee_uid = (j.get("inviteeUid") or "").strip()
    if not guest_id or not invitee_uid:
        return jsonify(error="guestId + inviteeUid required"), 400
    own = db().execute(
        "SELECT id FROM groups WHERE id=? AND owner_id=?", (gid, g.user_id)).fetchone()
    if not own: return jsonify(error="not found"), 404
    # Queue the invite for the runner; the runner sends it from the lobby.
    log("invite_queued", f"Invite {invitee_uid} queued", gid,
        {"viaGuestId": guest_id, "invitee": invitee_uid})
    return jsonify(ok=True)

@app.post("/api/client/groups/<int:gid>/action")
@auth_required()
def group_action(gid: int):
    """Start / stop / pause a CS push session for this group."""
    j = request.get_json(force=True) or {}
    action = j.get("action")
    if action not in {"start", "stop", "pause", "resume"}:
        return jsonify(error="bad action"), 400
    row = db().execute("SELECT id FROM groups WHERE id=? AND owner_id=?",
                       (gid, g.user_id)).fetchone()
    if not row: return jsonify(error="not found"), 404
    status = {"start": "lobby", "stop": "stopped", "pause": "paused", "resume": "running"}[action]
    db().execute("UPDATE groups SET status=? WHERE id=?", (status, gid))
    db().commit(); log(f"group_{action}", f"Group {gid} -> {status}", gid)
    if action == "start":
        runner_queue.put({"type": "cs_run", "group_id": gid, "user_id": g.user_id})
    return jsonify(ok=True, status=status)

@app.post("/api/admin/group-action")
@auth_required(role="admin")
def admin_group_action():
    j = request.get_json(force=True) or {}
    db().execute("UPDATE groups SET status=? WHERE id=?",
                 (j.get("status", "stopped"), j.get("groupId")))
    db().commit(); return jsonify(ok=True)

@app.get("/api/client/glory-progression")
@auth_required()
def glory_progression():
    gid = request.args.get("group_id")
    rows = db().execute(
        "SELECT created_at, message, meta_json FROM activity_log "
        "WHERE user_id=? AND group_id=? AND kind IN ('glory','cs_win','cs_loss') "
        "ORDER BY id DESC LIMIT 200", (g.user_id, gid)).fetchall()
    return jsonify(progression=[dict(r) for r in rows])

@app.get("/api/client/refetch-clan-data")
@auth_required()
def refetch_clan():
    gid = request.args.get("group_id")
    runner_queue.put({"type": "refetch", "group_id": int(gid), "user_id": g.user_id})
    return jsonify(ok=True, queued=True)

# ──────────────────────────────────────────────────────────────────────────────
# Misc endpoints expected by client.html
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/api/client/activity-log")
@auth_required()
def activity_log():
    rows = db().execute(
        "SELECT kind,message,meta_json,created_at FROM activity_log "
        "WHERE user_id=? ORDER BY id DESC LIMIT 200", (g.user_id,)).fetchall()
    return jsonify(log=[dict(r) for r in rows])

@app.get("/api/client/active-alert")
def active_alert():
    row = db().execute(
        "SELECT id,level,message FROM alerts WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    return jsonify(alert=row_to_dict(row))

@app.get("/api/client/inbox")
@auth_required()
def inbox_list():
    rows = db().execute(
        "SELECT * FROM inbox WHERE user_id=? ORDER BY id DESC LIMIT 100", (g.user_id,)).fetchall()
    return jsonify(messages=[dict(r) for r in rows])

@app.get("/api/client/notifications")
@auth_required()
def notif_list():
    rows = db().execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",
        (g.user_id,)).fetchall()
    return jsonify(notifications=[dict(r) for r in rows])

@app.get("/api/client/coupons")
@auth_required()
def coupon_list():
    rows = db().execute(
        "SELECT * FROM coupons WHERE expires_at IS NULL OR expires_at > datetime('now')").fetchall()
    return jsonify(coupons=[dict(r) for r in rows])

@app.get("/api/client/redeemed-coupons")
@auth_required()
def redeemed_list():
    rows = db().execute(
        "SELECT c.* , rc.redeemed_at FROM redeemed_coupons rc "
        "JOIN coupons c ON c.id=rc.coupon_id WHERE rc.user_id=? ORDER BY rc.id DESC",
        (g.user_id,)).fetchall()
    return jsonify(coupons=[dict(r) for r in rows])

@app.get("/api/client/transactions")
@auth_required()
def tx_list():
    rows = db().execute(
        "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC", (g.user_id,)).fetchall()
    return jsonify(transactions=[dict(r) for r in rows])

@app.get("/api/client/pricing")
def pricing():
    return jsonify(plans=[
        {"id": "starter",   "name": "Starter",   "price": 99,  "glory": 1000,  "duration_h": 12},
        {"id": "pro",       "name": "Pro",       "price": 249, "glory": 3000,  "duration_h": 24},
        {"id": "elite",     "name": "Elite",     "price": 499, "glory": 7000,  "duration_h": 48},
    ])

@app.get("/api/client/vapid-public-key")
def vapid_key():
    return jsonify(key=os.environ.get("VAPID_PUBLIC_KEY", ""))

# ──────────────────────────────────────────────────────────────────────────────
# Background CS runner (Free Fire plugin lives in ff_runner.py)
# ──────────────────────────────────────────────────────────────────────────────
from queue import Queue, Empty
runner_queue: "Queue[dict]" = Queue()

def _runner_loop():
    # Lazy import so the web layer boots even if ff_runner has missing deps.
    try:
        from ff_runner import handle_job
    except Exception as e:
        print(f"[runner] ff_runner.py not loaded: {e}")
        def handle_job(job, db_path, fernet_key): print("[runner] noop:", job)
    while True:
        try:
            job = runner_queue.get(timeout=1)
        except Empty:
            continue
        try:
            handle_job(job, DB_PATH, FERNET_KEY)
        except Exception as e:
            print(f"[runner] job failed: {e}")

def start_runner():
    t = threading.Thread(target=_runner_loop, daemon=True, name="cs-runner")
    t.start()

# ──────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ──────────────────────────────────────────────────────────────────────────────
init_db()
start_runner()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
