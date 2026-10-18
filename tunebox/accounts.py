"""The one admin account and its sessions, on the settings database.

login() decides a login completely: the password is always hashed (also
when there is no account), the TOTP step is claimed by a conditional update
in the same transaction that opens the session, so two logins racing with
one code cannot both win, and wrong codes after a correct password are
counted for the whole account."""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from tunebox import crypto
from tunebox.settings import Settings, SettingsWriteError

IDLE_S = 1800
# last_seen is written at most this often, so most session checks only read.
TOUCH_S = 60
ABSOLUTE_S = 43_200
# Wrong codes after a correct password, within a day, before logins slow down.
TOTP_SUSPECT = 10
TOTP_WINDOW_S = 86_400
MAX_BACKOFF_S = 3600


# The log and the CrowdSec scenarios match these words. "unavailable": the
# login was right but could not be saved.
Reason = Literal["password", "totp", "recovery", "backoff", "unavailable"]


class AccountError(Exception):
    pass


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf: str
    address: str | None


@dataclass(frozen=True)
class Outcome:
    ok: bool
    reason: Reason | None = None
    token: str | None = None
    csrf: str | None = None


@dataclass(frozen=True)
class _Admin:
    password_hash: str
    secret: bytes
    last_step: int
    fail_count: int
    fail_since: float | None
    fail_last: float | None


class Accounts:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._clock = clock
        self._key: bytes | None = None

    def exists(self) -> bool:
        return self._admin() is not None

    def generation(self) -> int:
        """Rises with every account replacement; long-lived grants name it."""
        with self._settings.read() as db:
            row = db.execute("SELECT generation FROM admin").fetchone()
        return 0 if row is None else cast(int, row[0])

    def server_key(self) -> bytes:
        """The key that signs long-lived cookies; made on first use."""
        if self._key is None:
            with self._settings.transaction() as db:
                row = db.execute("SELECT value FROM secrets WHERE key = 'hmac'").fetchone()
                if row is None:
                    key = secrets.token_bytes(32)
                    db.execute("INSERT INTO secrets VALUES ('hmac', ?)", (key,))
                else:
                    key = cast(bytes, row[0])
            self._key = key
        return self._key

    def create(
        self, password: str, secret: bytes, recovery: list[str], step: int, replace: bool = False
    ) -> None:
        """Sets up the account. step is the TOTP step the setup code used,
        so that code cannot log in afterwards. Replacing ends every session
        and every known-browser grant."""
        if len(password) < crypto.MIN_PASSWORD:
            raise AccountError(f"the password needs at least {crypto.MIN_PASSWORD} characters")
        stored = crypto.hash_password(password)
        now = self._clock()
        with self._settings.transaction() as db:
            row = db.execute("SELECT generation FROM admin").fetchone()
            if row is not None and not replace:
                raise AccountError("an account exists; reset it to replace it")
            generation = (cast(int, row[0]) if row else 0) + 1
            db.execute("DELETE FROM admin")
            db.execute("DELETE FROM recovery_codes")
            db.execute("DELETE FROM sessions")
            db.execute(
                "INSERT INTO admin VALUES (1, ?, ?, ?, 0, NULL, NULL, ?, ?, ?)",
                (stored, secret, step, generation, now, now),
            )
            db.executemany(
                "INSERT INTO recovery_codes VALUES (?, NULL)",
                [(crypto.code_hash(code),) for code in recovery],
            )

    def login(self, password: str, code: str, address: str, user_agent: str) -> Outcome:
        admin = self._admin()
        stored = admin.password_hash if admin else crypto.dummy_hash()
        if not crypto.verify_password(stored, password) or admin is None:
            return Outcome(False, "password")
        now = self._clock()
        if crypto.is_recovery_code(code):
            # Checked before the backoff: it is the owner's way in.
            return self._login_recovery(code, now, address, user_agent)
        if self._backing_off(admin, now):
            return Outcome(False, "backoff")
        step = crypto.matching_step(admin.secret, code.strip(), now, admin.last_step)
        if step is None:
            self._count_failure(now)
            return Outcome(False, "totp")
        try:
            with self._settings.transaction() as db:
                claimed = db.execute(
                    "UPDATE admin SET totp_last_step = ?, totp_fail_count = 0,"
                    " totp_fail_since = NULL, totp_fail_last = NULL WHERE totp_last_step < ?",
                    (step, step),
                ).rowcount
                if not claimed:
                    return Outcome(False, "totp")
                token, csrf = self._open(db, now, address, user_agent)
        except SettingsWriteError:
            return Outcome(False, "unavailable")
        return Outcome(True, token=token, csrf=csrf)

    def session(self, token: str) -> Session | None:
        """The live session for a cookie value; asking keeps it alive. The
        check is a read; the keep-alive and clean-up writes are best-effort,
        so a session still works while the database cannot be written."""
        now = self._clock()
        hashed = crypto.token_hash(token)
        with self._settings.read() as db:
            row = db.execute(
                "SELECT csrf_token, last_seen, absolute_expiry, address FROM sessions"
                " WHERE token_hash = ?",
                (hashed,),
            ).fetchone()
        if row is None:
            return None
        csrf, last_seen, expiry, address = cast("tuple[str, float, float, str | None]", row)
        if now - last_seen > IDLE_S or now > expiry:
            self._try_write("DELETE FROM sessions WHERE token_hash = ?", (hashed,))
            return None
        if now - last_seen > TOUCH_S:
            self._try_write("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, hashed))
        return Session(hashed, csrf, address)

    def _try_write(self, sql: str, params: tuple[object, ...]) -> None:
        try:
            with self._settings.transaction() as db:
                db.execute(sql, params)
        except SettingsWriteError:
            pass

    def logout(self, token: str) -> None:
        with self._settings.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (crypto.token_hash(token),))

    def totp_suspected(self) -> bool:
        admin = self._admin()
        return admin is not None and self._suspect(admin, self._clock())

    def _admin(self) -> _Admin | None:
        with self._settings.read() as db:
            row = db.execute(
                "SELECT password_hash, totp_secret, totp_last_step, totp_fail_count,"
                " totp_fail_since, totp_fail_last FROM admin"
            ).fetchone()
        if row is None:
            return None
        return _Admin(*cast("tuple[str, bytes, int, int, float | None, float | None]", row))

    def _suspect(self, admin: _Admin, now: float) -> bool:
        recent = admin.fail_since is not None and now - admin.fail_since <= TOTP_WINDOW_S
        return recent and admin.fail_count >= TOTP_SUSPECT

    def _backing_off(self, admin: _Admin, now: float) -> bool:
        if not self._suspect(admin, now) or admin.fail_last is None:
            return False
        wait = min(MAX_BACKOFF_S, 60 * 2 ** (admin.fail_count - TOTP_SUSPECT))
        return now < admin.fail_last + wait

    def _count_failure(self, now: float) -> None:
        """Counted in SQL, so two wrong codes at once both count."""
        fresh = "totp_fail_since IS NULL OR ? - totp_fail_since > ?"
        with self._settings.transaction() as db:
            db.execute(
                f"UPDATE admin SET"
                f" totp_fail_count = CASE WHEN {fresh} THEN 1 ELSE totp_fail_count + 1 END,"
                f" totp_fail_since = CASE WHEN {fresh} THEN ? ELSE totp_fail_since END,"
                " totp_fail_last = ?",
                (now, TOTP_WINDOW_S, now, TOTP_WINDOW_S, now, now),
            )

    def _login_recovery(self, code: str, now: float, address: str, user_agent: str) -> Outcome:
        token = csrf = ""
        try:
            with self._settings.transaction() as db:
                used = db.execute(
                    "UPDATE recovery_codes SET used_at = ? WHERE hash = ? AND used_at IS NULL",
                    (now, crypto.code_hash(code)),
                ).rowcount
                if used:
                    db.execute(
                        "UPDATE admin SET totp_fail_count = 0, totp_fail_since = NULL,"
                        " totp_fail_last = NULL"
                    )
                    token, csrf = self._open(db, now, address, user_agent)
        except SettingsWriteError:
            return Outcome(False, "unavailable")
        if not used:
            # A wrong recovery code after a correct password counts like a
            # wrong code: someone may have the password.
            self._count_failure(now)
            return Outcome(False, "recovery")
        return Outcome(True, token=token, csrf=csrf)

    def _open(
        self, db: sqlite3.Connection, now: float, address: str, user_agent: str
    ) -> tuple[str, str]:
        token = crypto.new_token()
        csrf = crypto.new_token()
        db.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (crypto.token_hash(token), csrf, now, now, now + ABSOLUTE_S, address, user_agent[:200]),
        )
        return token, csrf
