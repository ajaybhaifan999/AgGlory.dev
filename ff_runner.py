"""
ff_runner.py — Free Fire automation plugin
==========================================
app.py queues jobs onto a thread-safe Queue and calls `handle_job` for each.

Replace the bodies marked `# >>> FF_PLUGIN` with your existing
reverse-engineered Free Fire calls (guest login, lobby create, invite,
match start, result poll). Everything around them — DB updates, activity
logs, retries — is already wired.

Job shapes
----------
    {"type": "cs_run",  "group_id": int, "user_id": int}
    {"type": "refetch", "group_id": int, "user_id": int}
"""
from __future__ import annotations
import json, time, sqlite3
from cryptography.fernet import Fernet


# ─── helpers ─────────────────────────────────────────────────────────────────
def _conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path); c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON"); return c

def _log(c, user_id, group_id, kind, message, meta=None):
    c.execute("INSERT INTO activity_log(user_id,group_id,kind,message,meta_json) "
              "VALUES(?,?,?,?,?)",
              (user_id, group_id, kind, message, json.dumps(meta or {})))
    c.commit()

def _set_group_status(c, gid, status):
    c.execute("UPDATE groups SET status=? WHERE id=?", (status, gid)); c.commit()


# ─── Free Fire plugin surface ─────────────────────────────────────────────────
# Implement these four functions using your existing FF code.

def ff_login_guest(uid: str, password: str) -> dict:
    """Authenticate a Free Fire guest account. Return session dict."""
    # >>> FF_PLUGIN: call your guest login endpoint, return tokens/cookies
    raise NotImplementedError("Plug your FF guest login here")

def ff_create_lobby(session: dict, mode: str = "cs") -> str:
    """Create a CS lobby and return the lobby/room id."""
    # >>> FF_PLUGIN
    raise NotImplementedError

def ff_invite(session: dict, lobby_id: str, invitee_uid: str) -> bool:
    """Invite a player UID into the lobby."""
    # >>> FF_PLUGIN
    raise NotImplementedError

def ff_start_match(session: dict, lobby_id: str) -> dict:
    """Start the match and block until result. Return {'win': bool, 'glory_delta': int}."""
    # >>> FF_PLUGIN
    raise NotImplementedError


# ─── job dispatcher ──────────────────────────────────────────────────────────
def handle_job(job: dict, db_path: str, fernet_key: str):
    f = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
    c = _conn(db_path)
    try:
        if job["type"] == "cs_run":   return _run_cs(c, f, job)
        if job["type"] == "refetch":  return _refetch(c, f, job)
    finally:
        c.close()


def _run_cs(c, f, job):
    gid, uid = job["group_id"], job["user_id"]
    grp = c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    if not grp: return
    captain = c.execute(
        "SELECT ga.* FROM group_members m JOIN guest_accounts ga ON ga.id=m.guest_id "
        "WHERE m.group_id=? AND m.role='captain' LIMIT 1", (gid,)).fetchone()
    if not captain:
        _log(c, uid, gid, "error", "No captain guest set"); _set_group_status(c, gid, "error"); return

    try:
        _log(c, uid, gid, "cs_login", f"Logging in {captain['uid']}")
        session = ff_login_guest(captain["uid"], f.decrypt(captain["password_enc"].encode()).decode())

        _set_group_status(c, gid, "lobby")
        lobby = ff_create_lobby(session)
        _log(c, uid, gid, "cs_lobby", f"Lobby {lobby} created")

        members = c.execute(
            "SELECT ga.uid FROM group_members m JOIN guest_accounts ga ON ga.id=m.guest_id "
            "WHERE m.group_id=? AND m.role<>'captain'", (gid,)).fetchall()
        for m in members:
            try: ff_invite(session, lobby, m["uid"])
            except Exception as e: _log(c, uid, gid, "invite_fail", f"{m['uid']}: {e}")

        _set_group_status(c, gid, "running")
        for round_i in range(int(json.loads(grp["config_json"] or "{}").get("rounds", 5))):
            result = ff_start_match(session, lobby)
            delta = int(result.get("glory_delta", 0))
            c.execute("UPDATE guest_accounts SET glory_current=glory_current+? WHERE id=?",
                      (delta, captain["id"])); c.commit()
            _log(c, uid, gid, "cs_win" if result.get("win") else "cs_loss",
                 f"Round {round_i+1}: {'WIN' if result.get('win') else 'LOSS'} ({delta:+} glory)",
                 result)
            _log(c, uid, gid, "glory", f"Glory delta {delta:+}", {"delta": delta})
            time.sleep(2)

        _set_group_status(c, gid, "stopped")
        _log(c, uid, gid, "cs_done", "CS session complete")
    except NotImplementedError as e:
        _set_group_status(c, gid, "error")
        _log(c, uid, gid, "error", f"FF plugin missing: {e}")
    except Exception as e:
        _set_group_status(c, gid, "error")
        _log(c, uid, gid, "error", f"Run failed: {e}")


def _refetch(c, f, job):
    gid, uid = job["group_id"], job["user_id"]
    captain = c.execute(
        "SELECT ga.* FROM group_members m JOIN guest_accounts ga ON ga.id=m.guest_id "
        "WHERE m.group_id=? AND m.role='captain' LIMIT 1", (gid,)).fetchone()
    if not captain:
        _log(c, uid, gid, "refetch_fail", "no captain"); return
    try:
        session = ff_login_guest(captain["uid"],
                                 f.decrypt(captain["password_enc"].encode()).decode())
        # >>> FF_PLUGIN: read current glory from FF profile and persist
        _log(c, uid, gid, "refetch_ok", "Clan data refreshed")
    except Exception as e:
        _log(c, uid, gid, "refetch_fail", str(e))
