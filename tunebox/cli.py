"""Commands besides the server: `python -m tunebox admin setup [--reset]`
sets up the admin account from a terminal, so nothing about setting it up
has to be reachable over the network."""

from __future__ import annotations

import getpass
import os
import time
from collections.abc import Callable
from pathlib import Path

from tunebox import crypto
from tunebox.accounts import AccountError, Accounts
from tunebox.config import ConfigError, load
from tunebox.settings import Settings, SettingsUnavailable, SettingsWriteError

USAGE = "usage: python -m tunebox admin setup [--reset]"


def run(
    argv: list[str],
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    out: Callable[[str], None] = print,
    clock: Callable[[], float] = time.time,
) -> int:
    if argv[:2] != ["admin", "setup"] or argv[2:] not in ([], ["--reset"]):
        out(USAGE)
        return 2
    try:
        config = load(Path(os.environ.get("TUNEBOX_CONFIG", "/data/tunebox.toml")))
        settings = Settings(config.server.data_dir / "tunebox.db")
    except (ConfigError, SettingsUnavailable) as error:
        out(f"tunebox: {error}")
        return 2
    try:
        accounts = Accounts(settings, clock=clock)
        return _setup(accounts, argv[2:] == ["--reset"], ask, ask_secret, out, clock)
    except (SettingsUnavailable, SettingsWriteError) as error:
        out(f"tunebox: {error}")
        return 2
    finally:
        settings.close()


def _setup(
    accounts: Accounts,
    reset: bool,
    ask: Callable[[str], str],
    ask_secret: Callable[[str], str],
    out: Callable[[str], None],
    clock: Callable[[], float],
) -> int:
    if accounts.exists() and not reset:
        out("An admin account exists. Run again with --reset to replace it.")
        out("Replacing it signs everyone out.")
        return 2
    password = ask_secret(f"New admin password (at least {crypto.MIN_PASSWORD} characters): ")
    if len(password) < crypto.MIN_PASSWORD:
        out(f"The password needs at least {crypto.MIN_PASSWORD} characters.")
        return 2
    if ask_secret("The same password again: ") != password:
        out("The passwords differ.")
        return 2
    secret = crypto.new_totp_secret()
    out("Add this to your authenticator app (open the link as a QR code, or type the secret):")
    out(crypto.otpauth_uri(secret))
    out(f"Secret: {crypto.secret_text(secret)}")
    step = crypto.matching_step(secret, ask("The 6-digit code it shows now: ").strip(), clock(), -1)
    if step is None:
        out("That code does not match. Nothing was changed; run the command again.")
        return 2
    codes = crypto.new_recovery_codes()
    try:
        accounts.create(password, secret, codes, step, replace=reset)
    except (AccountError, SettingsWriteError) as error:
        out(f"tunebox: {error}")
        return 2
    out("Done. Keep these recovery codes somewhere safe; each one replaces a code once:")
    for code in codes:
        out(f"  {code}")
    return 0
