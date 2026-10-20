"""The one admin account and its sessions, on the settings database.

login() decides a login completely: the password is always hashed (also
when there is no account), the TOTP step is claimed by a conditional update
in the same transaction that opens the session, so two logins racing with
one code cannot both win, and wrong codes after a correct password are
counted for the whole account."""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from tunebox import crypto
from tunebox.settings import Settings, SettingsUnavailable, SettingsWriteError

IDLE_S = 1800
# last_seen is written at most this often, so most session checks only read.
TOUCH_S = 60
ABSOLUTE_S = 43_200
# Wrong codes after a correct password, within a day, before logins slow down.
TOTP_SUSPECT = 10
TOTP_WINDOW_S = 86_400
MAX_BACKOFF_S = 3600
# A sensitive change needs a code typed this recently in the same session.
REAUTH_S = 300
_SESSION_ID = re.compile(r"[0-9a-f]{16}")


# The log and the CrowdSec scenarios match these words. "unavailable": the
# login was right but could not be saved.
Reason = Literal["password", "totp", "recovery", "backoff", "unavailable"]


class AccountError(Exception):
    pass


class PasswordTooShort(AccountError):
    pass


class SessionEnded(AccountError):
    """The session asking for a change ended before the change committed."""


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf: str
    address: str | None
    # When a code was last typed in this session to confirm a change.
    reauth_at: float | None = None


@dataclass(frozen=True)
class SessionInfo:
    # Enough of the token hash to name the session; never the token.
    id: str
    created_at: float
    last_seen: float
    address: str | None
    user_agent: str


def session_id(token_hash: str) -> str:
    return token_hash[:16]


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
            raise PasswordTooShort(f"the password needs at least {crypto.MIN_PASSWORD} characters")
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
                "INSERT INTO admin (id, password_hash, totp_secret, totp_last_step,"
                " totp_fail_count, totp_fail_since, totp_fail_last, generation, created_at,"
                " updated_at) VALUES (1, ?, ?, ?, 0, NULL, NULL, ?, ?, ?)",
                (stored, secret, step, generation, now, now),
            )
            db.executemany(
                "INSERT INTO recovery_codes (hash, used_at) VALUES (?, NULL)",
                [(crypto.code_hash(code),) for code in recovery],
            )

    def login(self, password: str, code: str, address: str, user_agent: str) -> Outcome:
        admin = self._admin()
        stored = admin.password_hash if admin else crypto.dummy_hash()
        if not crypto.verify_password(stored, password) or admin is None:
            return Outcome(False, "password")
        token = csrf = ""

        def open_session(db: sqlite3.Connection, now: float) -> None:
            nonlocal token, csrf
            token, csrf = self._open(db, now, address, user_agent)

        reason = self._confirm(admin, code, open_session)
        if reason is not None:
            return Outcome(False, reason)
        return Outcome(True, token=token, csrf=csrf)

    def reauth(self, session: Session, code: str) -> Reason | None:
        """Marks the session as having typed a code just now; None when done.
        The code is claimed like a login's, so neither can use it twice."""
        admin = self._admin()
        if admin is None:
            return "totp"

        def mark(db: sqlite3.Connection, now: float) -> None:
            db.execute(
                "UPDATE sessions SET reauth_at = ? WHERE token_hash = ?", (now, session.token_hash)
            )

        return self._confirm(admin, code, mark)

    def fresh(self, session: Session) -> bool:
        return session.reauth_at is not None and self._clock() - session.reauth_at <= REAUTH_S

    def _confirm(
        self, admin: _Admin, code: str, then: Callable[[sqlite3.Connection, float], None]
    ) -> Reason | None:
        """Checks a TOTP or recovery code and, in the transaction that claims
        it, runs then. Wrong codes are counted for the whole account.
        "unavailable" only when the code was right: after a right password,
        a database fault must not answer a wrong code differently from a
        wrong password."""
        now = self._clock()
        recovery = crypto.is_recovery_code(code)
        # A recovery code is checked before the backoff: it is the owner's way in.
        if not recovery and self._backing_off(admin, now):
            return "backoff"
        step = None
        if recovery:
            if not self._recovery_unused(code):
                self._count_failure(now)
                return "recovery"
        else:
            step = crypto.matching_step(admin.secret, code.strip(), now, admin.last_step)
            if step is None:
                self._count_failure(now)
                return "totp"
        try:
            with self._settings.transaction() as db:
                if recovery:
                    claimed = db.execute(
                        "UPDATE recovery_codes SET used_at = ? WHERE hash = ? AND used_at IS NULL",
                        (now, crypto.code_hash(code)),
                    ).rowcount
                    if claimed:
                        db.execute(
                            "UPDATE admin SET totp_fail_count = 0, totp_fail_since = NULL,"
                            " totp_fail_last = NULL"
                        )
                else:
                    claimed = db.execute(
                        "UPDATE admin SET totp_last_step = ?, totp_fail_count = 0,"
                        " totp_fail_since = NULL, totp_fail_last = NULL WHERE totp_last_step < ?",
                        (step, step),
                    ).rowcount
                if claimed:
                    then(db, now)
        except SettingsWriteError:
            return "unavailable"
        if not claimed:
            if recovery:
                # A wrong recovery code after a correct password counts like
                # a wrong code: someone may have the password.
                self._count_failure(now)
                return "recovery"
            return "totp"
        return None

    def session(self, token: str) -> Session | None:
        """The live session for a cookie value; asking keeps it alive. The
        check is a read; the keep-alive and clean-up writes are best-effort,
        so a session still works while the database cannot be written."""
        now = self._clock()
        hashed = crypto.token_hash(token)
        with self._settings.read() as db:
            row = db.execute(
                "SELECT csrf_token, last_seen, absolute_expiry, address, reauth_at FROM sessions"
                " WHERE token_hash = ?",
                (hashed,),
            ).fetchone()
        if row is None:
            return None
        csrf, last_seen, expiry, address, reauth_at = cast(
            "tuple[str, float, float, str | None, float | None]", row
        )
        if now - last_seen > IDLE_S or now > expiry:
            self._try_write("DELETE FROM sessions WHERE token_hash = ?", (hashed,))
            return None
        if now - last_seen > TOUCH_S:
            self._try_write("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, hashed))
        return Session(hashed, csrf, address, reauth_at)

    def _try_write(self, sql: str, params: tuple[object, ...]) -> None:
        try:
            with self._settings.transaction() as db:
                db.execute(sql, params)
        except SettingsWriteError:
            pass

    def change_password(self, session: Session, password: str) -> int:
        if len(password) < crypto.MIN_PASSWORD:
            raise PasswordTooShort(f"the password needs at least {crypto.MIN_PASSWORD} characters")
        # Hashed before the transaction: scrypt takes 0.1 s and must not hold
        # the database lock.
        stored = crypto.hash_password(password)
        return self._replace(session, "password_hash = ?", (stored,))

    def replace_totp(self, session: Session, secret: bytes, step: int) -> int:
        """step is the one the confirming code used, so it cannot log in."""
        return self._replace(session, "totp_secret = ?, totp_last_step = ?", (secret, step))

    def replace_recovery_codes(self, session: Session) -> tuple[list[str], int]:
        codes = crypto.new_recovery_codes()

        def store(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM recovery_codes")
            db.executemany(
                "INSERT INTO recovery_codes (hash, used_at) VALUES (?, NULL)",
                [(crypto.code_hash(code),) for code in codes],
            )

        return codes, self._replace(session, "", (), store)

    def _replace(
        self,
        session: Session,
        assignments: str,
        params: tuple[object, ...],
        also: Callable[[sqlite3.Connection], None] | None = None,
    ) -> int:
        """Changes the account and, in the same transaction, ends every other
        session and raises the generation, which voids every known-browser
        grant; returns the new generation. Someone who had the old secret is
        out once this commits. The caller's session is checked inside the
        transaction: a session ended meanwhile (a sign-out from another
        browser) must not get its change through."""
        # assignments is one of the constant strings above, never input.
        sets = f"{assignments}, " if assignments else ""
        with self._settings.transaction() as db:
            alive = db.execute(
                "SELECT 1 FROM sessions WHERE token_hash = ?", (session.token_hash,)
            ).fetchone()
            if alive is None:
                raise SessionEnded("the session ended")
            db.execute(
                f"UPDATE admin SET {sets}generation = generation + 1, updated_at = ?,"
                " totp_fail_count = 0, totp_fail_since = NULL, totp_fail_last = NULL",
                (*params, self._clock()),
            )
            if also is not None:
                also(db)
            db.execute("DELETE FROM sessions WHERE token_hash != ?", (session.token_hash,))
            row = db.execute("SELECT generation FROM admin").fetchone()
        return cast(int, row[0])

    def sessions(self) -> list[SessionInfo]:
        now = self._clock()
        with self._settings.read() as db:
            rows = cast(
                "list[tuple[str, float, float, str | None, str | None]]",
                db.execute(
                    "SELECT token_hash, created_at, last_seen, address, user_agent FROM sessions"
                    " WHERE last_seen >= ? AND absolute_expiry >= ? ORDER BY created_at DESC",
                    (now - IDLE_S, now),
                ).fetchall(),
            )
        return [
            SessionInfo(session_id(token_hash), created, seen, address, agent or "")
            for token_hash, created, seen, address, agent in rows
        ]

    def end_session(self, wanted: str) -> str | None:
        """The token hash of the session it ended, or None."""
        if not _SESSION_ID.fullmatch(wanted):
            return None
        with self._settings.transaction() as db:
            row = db.execute(
                "SELECT token_hash FROM sessions WHERE substr(token_hash, 1, 16) = ?", (wanted,)
            ).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (row[0],))
        return str(row[0])

    def logout(self, token: str) -> None:
        with self._settings.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (crypto.token_hash(token),))

    def totp_suspected(self) -> bool:
        """False when the account cannot be read: it is asked after a
        password check, where a database fault must not change the answer."""
        try:
            admin = self._admin()
        except SettingsUnavailable:
            return False
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

    def _recovery_unused(self, code: str) -> bool:
        """A read, so a wrong recovery code is refused without a write that
        could fail; a database that cannot be read refuses it too."""
        try:
            with self._settings.read() as db:
                row = db.execute(
                    "SELECT 1 FROM recovery_codes WHERE hash = ? AND used_at IS NULL",
                    (crypto.code_hash(code),),
                ).fetchone()
        except SettingsUnavailable:
            return False
        return row is not None

    def _count_failure(self, now: float) -> None:
        """Counted in SQL, so two wrong codes at once both count. Best-effort:
        while the database cannot be written the code is still refused."""
        fresh = "totp_fail_since IS NULL OR ? - totp_fail_since > ?"
        self._try_write(
            f"UPDATE admin SET"
            f" totp_fail_count = CASE WHEN {fresh} THEN 1 ELSE totp_fail_count + 1 END,"
            f" totp_fail_since = CASE WHEN {fresh} THEN ? ELSE totp_fail_since END,"
            " totp_fail_last = ?",
            (now, TOTP_WINDOW_S, now, TOTP_WINDOW_S, now, now),
        )

    def _open(
        self, db: sqlite3.Connection, now: float, address: str, user_agent: str
    ) -> tuple[str, str]:
        token = crypto.new_token()
        csrf = crypto.new_token()
        # Expired rows are only ever removed here and when they are next used.
        db.execute(
            "DELETE FROM sessions WHERE last_seen < ? OR absolute_expiry < ?", (now - IDLE_S, now)
        )
        db.execute(
            "INSERT INTO sessions (token_hash, csrf_token, created_at, last_seen,"
            " absolute_expiry, address, user_agent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (crypto.token_hash(token), csrf, now, now, now + ABSOLUTE_S, address, user_agent[:200]),
        )
        return token, csrf
