import base64
import re
from pathlib import Path

import pytest

from tunebox import crypto
from tunebox.accounts import Accounts
from tunebox.cli import run
from tunebox.settings import Settings

PASSWORD = "a long enough password"
NOW = 1_000_000.0


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "tunebox.toml"
    path.write_text("[server]\n", encoding="utf-8")
    monkeypatch.setenv("TUNEBOX_CONFIG", str(path))
    return path


def setup(argv: list[str], passwords: list[str], printed: list[str], code: str = "") -> int:
    def ask(_prompt: str) -> str:
        if code:
            return code
        line = next(line for line in printed if line.startswith("Secret: "))
        text = line.removeprefix("Secret: ")
        secret = base64.b32decode(text + "=" * (-len(text) % 8))
        return crypto.totp(secret, int(NOW // 30))

    answers = iter(passwords)
    return run(
        argv,
        ask=ask,
        ask_secret=lambda _prompt: next(answers),
        out=printed.append,
        clock=lambda: NOW,
    )


def test_setup_creates_the_account_and_prints_codes(config: Path) -> None:
    printed: list[str] = []
    assert setup(["admin", "setup"], [PASSWORD, PASSWORD], printed) == 0
    assert any(line.startswith("otpauth://totp/") for line in printed)
    pattern = r"\s*[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}"
    assert len([line for line in printed if re.fullmatch(pattern, line)]) == 10
    settings = Settings(config.parent / "tunebox.db")
    assert Accounts(settings).exists()
    settings.close()


def test_setup_refuses_a_second_account_without_reset(config: Path) -> None:
    assert setup(["admin", "setup"], [PASSWORD, PASSWORD], []) == 0
    printed: list[str] = []
    assert setup(["admin", "setup"], [PASSWORD, PASSWORD], printed) == 2
    assert any("--reset" in line for line in printed)
    assert setup(["admin", "setup", "--reset"], [PASSWORD, PASSWORD], []) == 0


def test_passwords_must_match_and_be_long(config: Path) -> None:
    assert setup(["admin", "setup"], ["short", "short"], []) == 2
    assert setup(["admin", "setup"], [PASSWORD, PASSWORD + "x"], []) == 2


def test_a_wrong_code_changes_nothing(config: Path) -> None:
    printed: list[str] = []
    assert setup(["admin", "setup"], [PASSWORD, PASSWORD], printed, code="000000") == 2
    settings = Settings(config.parent / "tunebox.db")
    assert not Accounts(settings).exists()
    settings.close()


def test_usage_and_a_missing_config(config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    printed: list[str] = []
    assert run(["admin"], out=printed.append) == 2
    assert "admin setup" in printed[0]
    monkeypatch.setenv("TUNEBOX_CONFIG", str(config.parent / "missing.toml"))
    assert run(["admin", "setup"], out=printed.append) == 2
    assert "missing.toml" in printed[-1]
