"""Passwords, sessions and CSRF tokens, following the OWASP Session Management, CSRF Prevention
and Password Storage cheat sheets.

- A session is a row on the server. The cookie holds a random 256-bit token; the database holds
  only its SHA-256, so a copied database cannot be replayed as cookies.
- Every session, signed in or not, carries its own CSRF token, which every POST must return (the
  synchronizer token pattern). Signing in replaces the session with a new one (no fixation).
- Timeouts are enforced here: 30 minutes idle, 8 hours in all.
- Passwords use scrypt at OWASP's minimums. Five failures lock the account for 15 minutes.

Functions take a database connection and the time, so tests can drive the clock.
"""

import hashlib
import hmac
import secrets
import sqlite3
from typing import Any

COOKIE = "__Host-session"
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024  # N=2^17, r=8 needs 128 MiB; Python's default allows 32 MiB


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def password_matches(password: str, stored: str) -> bool:
    _, n, r, p, salt, key = stored.split("$")
    found = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
        maxmem=SCRYPT_MAXMEM,
    )  # fmt: skip
    return hmac.compare_digest(found.hex(), key)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(connection: sqlite3.Connection, user: str | None, now: float) -> str:
    """A new session (signed out when `user` is None); returns the cookie's token."""
    token = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        (token_hash(token), user, secrets.token_urlsafe(32), now, now),
    )
    connection.commit()
    return token


def find_session(connection: sqlite3.Connection, token: str, now: float) -> dict[str, Any] | None:
    """The live session for a cookie token, renewed for idle time; None if missing or expired."""
    row = connection.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (token_hash(token),)
    ).fetchone()
    if row is None:
        return None
    if now - row["seen"] > IDLE_SECONDS or now - row["created"] > ABSOLUTE_SECONDS:
        end_session(connection, token)
        return None
    connection.execute(
        "UPDATE sessions SET seen = ? WHERE token_hash = ?", (now, row["token_hash"])
    )
    connection.commit()
    return dict(row)


def end_session(connection: sqlite3.Connection, token: str) -> None:
    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
    connection.commit()


def csrf_ok(session: dict[str, Any] | None, submitted: str) -> bool:
    return session is not None and hmac.compare_digest(session["csrf"], submitted)


def check_password(connection: sqlite3.Connection, user_id: str, password: str, now: float) -> str:
    """ "ok", "wrong" or "locked". Each failure counts; the fifth locks the account."""
    user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or user["password"] is None:
        return "wrong"
    if user["locked_until"] > now:
        return "locked"
    if password_matches(password, user["password"]):
        connection.execute("UPDATE users SET failures = 0 WHERE id = ?", (user_id,))
        connection.commit()
        return "ok"
    failures = user["failures"] + 1
    locked_until = now + LOCKOUT_SECONDS if failures >= FAILURES else 0
    connection.execute(
        "UPDATE users SET failures = ?, locked_until = ? WHERE id = ?",
        (0 if locked_until else failures, locked_until, user_id),
    )
    connection.commit()
    return "locked" if locked_until else "wrong"
