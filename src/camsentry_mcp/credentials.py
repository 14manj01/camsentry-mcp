"""Secret storage in the operating system keychain.

Nothing in this module reads or writes files. The Wyze password and API key are
never stored anywhere: only the session tokens they are exchanged for. Notification
destinations (phone number, push topic, webhook URL) are kept here too, so that
personal contact details never sit in a config file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import keyring
from keyring.backend import KeyringBackend

SERVICE = "camsentry-mcp"
_TOKENS = "wyze-tokens"
_NOTIFY = "notify"
# Windows Credential Manager rejects secrets longer than 1280 UTF-16 code units.
_CHUNK = 1000


class CredentialStoreError(RuntimeError):
    """The OS keychain is missing, insecure, or refused an operation."""


@dataclass(frozen=True)
class Tokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)


def _require_secure(backend: KeyringBackend) -> None:
    kind = type(backend)
    insecure = kind.__module__.startswith(("keyring.backends.fail", "keyring.backends.null"))
    if insecure or "plaintext" in kind.__name__.lower():
        raise CredentialStoreError(
            f"No secure OS keychain is available (found {kind.__module__}.{kind.__name__}). "
            "camsentry-mcp refuses to store Wyze tokens unencrypted. Use Windows Credential Manager, "
            "macOS Keychain, or a Secret Service provider such as GNOME Keyring or KWallet."
        )


class SecretStore:
    def __init__(self, backend: KeyringBackend | None = None) -> None:
        self._backend = backend if backend is not None else keyring.get_keyring()
        _require_secure(self._backend)

    @property
    def backend_name(self) -> str:
        return type(self._backend).__name__

    def _count(self, name: str) -> int:
        raw = self._backend.get_password(SERVICE, f"{name}.n")
        return int(raw) if raw and raw.isdigit() else 0

    def _read(self, name: str) -> str | None:
        count = self._count(name)
        if count == 0:
            return None
        chunks = [self._backend.get_password(SERVICE, f"{name}.{i}") for i in range(count)]
        if any(chunk is None for chunk in chunks):
            return None
        return "".join(chunks)  # type: ignore[arg-type]

    def _write(self, name: str, value: str) -> None:
        stale = self._count(name)
        chunks = [value[i : i + _CHUNK] for i in range(0, len(value), _CHUNK)]
        try:
            for index, chunk in enumerate(chunks):
                self._backend.set_password(SERVICE, f"{name}.{index}", chunk)
            self._backend.set_password(SERVICE, f"{name}.n", str(len(chunks)))
        except Exception as err:
            raise CredentialStoreError(f"The OS keychain refused the write ({type(err).__name__}).") from None
        for index in range(len(chunks), stale):
            self._delete(f"{name}.{index}")

    def _delete(self, key: str) -> None:
        try:
            self._backend.delete_password(SERVICE, key)
        except Exception:
            # Already absent: every backend raises its own error type for that.
            pass

    def _erase(self, name: str) -> None:
        for index in range(self._count(name)):
            self._delete(f"{name}.{index}")
        self._delete(f"{name}.n")

    def _read_json(self, name: str) -> dict[str, str]:
        raw = self._read(name)
        if raw is None:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def load_tokens(self) -> Tokens | None:
        data = self._read_json(_TOKENS)
        if not data.get("access_token") or not data.get("refresh_token"):
            return None
        return Tokens(data["access_token"], data["refresh_token"])

    def save_tokens(self, tokens: Tokens) -> None:
        self._write(_TOKENS, json.dumps({"access_token": tokens.access_token, "refresh_token": tokens.refresh_token}))

    def clear_tokens(self) -> None:
        self._erase(_TOKENS)

    def load_notify(self) -> dict[str, str]:
        """Notification destinations, keyed as in notify.py (sms_phone, sms_key, ntfy_topic, webhook_url)."""
        return self._read_json(_NOTIFY)

    def save_notify(self, destinations: dict[str, str]) -> None:
        if destinations:
            self._write(_NOTIFY, json.dumps(destinations))
        else:
            self._erase(_NOTIFY)
