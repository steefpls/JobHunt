"""Secrets: API keys and site logins, kept in Windows Credential Manager.

Values go in through the UI and come out only at the moment code needs them
(an HTTP header, or typing into a password box). They are never written to
disk by this app, never put into a prompt, and never logged: anything that
might echo them back (page text, error messages, log lines) goes through
`Redactor` first.
"""
import re

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "JobHunt"
_LOGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def api_key_name(provider: str) -> str:
    return f"api_key:{provider}"


def login_secret_name(login: str, field: str) -> str:
    """field is 'username' or 'password'."""
    return f"login:{login}:{field}"


def valid_login_name(name: str) -> bool:
    return bool(_LOGIN_NAME_RE.match(name or ""))


def set_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def get_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


def delete_secret(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass


def known_secret_names(settings: dict) -> list[str]:
    names = [api_key_name(p) for p in ("opencode_go", "openrouter", "anthropic", "openai_compatible")]
    for login in settings.get("logins", []):
        names.append(login_secret_name(login["name"], "username"))
        names.append(login_secret_name(login["name"], "password"))
    return names


class Redactor:
    """Replaces any known secret value in a piece of text with [secret:name]."""

    MIN_LEN = 4  # don't redact "a" or "1" everywhere

    def __init__(self, values: dict[str, str] | None = None):
        self._values = {}
        for name, value in (values or {}).items():
            if value and len(value) >= self.MIN_LEN:
                self._values[name] = value

    @classmethod
    def from_store(cls, settings: dict) -> "Redactor":
        values = {}
        for name in known_secret_names(settings):
            try:
                v = get_secret(name)
            except Exception:  # a broken keyring backend must not stop redaction
                v = None
            if v:
                values[name] = v
        return cls(values)

    def __call__(self, text):
        if not text or not self._values:
            return text
        text = str(text)
        # longest first, so a password containing a shorter secret is fully hidden
        for name, value in sorted(self._values.items(), key=lambda kv: -len(kv[1])):
            if value in text:
                text = text.replace(value, f"[secret:{name}]")
        return text
