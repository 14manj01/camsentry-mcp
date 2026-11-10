import pytest
from keyring.backends.fail import Keyring as FailKeyring

from conftest import MemoryKeyring
from camsentry_mcp.credentials import BridgeAuth, CredentialStoreError, SecretStore, Tokens

# Windows Credential Manager rejects any single secret longer than this.
WINDOWS_LIMIT = 1280


def test_long_tokens_round_trip_within_the_windows_secret_limit(store: SecretStore, keyring_backend: MemoryKeyring) -> None:
    tokens = Tokens("a" * 3500, "r" * 2100)
    store.save_tokens(tokens)
    assert store.load_tokens() == tokens
    assert max(len(value) for value in keyring_backend.entries.values()) <= WINDOWS_LIMIT


def test_replacing_long_tokens_with_short_ones_leaves_no_stale_pieces(store: SecretStore, keyring_backend: MemoryKeyring) -> None:
    store.save_tokens(Tokens("a" * 3500, "r" * 3500))
    short = Tokens("access", "refresh")
    store.save_tokens(short)
    assert store.load_tokens() == short
    assert not any("a" * 100 in value for value in keyring_backend.entries.values())


def test_clearing_tokens_removes_every_entry_but_keeps_bridge_auth(store: SecretStore, keyring_backend: MemoryKeyring) -> None:
    store.save_tokens(Tokens("a" * 3500, "refresh"))
    store.save_bridge_auth(BridgeAuth("viewer", "pw"))
    store.clear_tokens()
    assert store.load_tokens() is None
    assert store.load_bridge_auth() == BridgeAuth("viewer", "pw")
    store.clear_bridge_auth()
    assert keyring_backend.entries == {}


def test_partially_deleted_tokens_read_as_signed_out(store: SecretStore, keyring_backend: MemoryKeyring) -> None:
    store.save_tokens(Tokens("a" * 3500, "refresh"))
    keyring_backend.delete_password("camsentry-mcp", "wyze-tokens.1")
    assert store.load_tokens() is None


def test_missing_keychain_is_refused() -> None:
    with pytest.raises(CredentialStoreError, match="secure OS keychain"):
        SecretStore(FailKeyring())


def test_plaintext_keychain_is_refused() -> None:
    class PlaintextKeyring(MemoryKeyring):
        pass

    with pytest.raises(CredentialStoreError, match="unencrypted"):
        SecretStore(PlaintextKeyring())


def test_secrets_stay_out_of_reprs() -> None:
    assert "s3cret" not in repr(Tokens("s3cret-access", "s3cret-refresh"))
    assert "s3cret" not in repr(BridgeAuth("viewer", "s3cret"))
