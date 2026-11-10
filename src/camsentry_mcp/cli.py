"""Command line: run the MCP server, or manage the Wyze session held in the OS keychain."""

from __future__ import annotations

import argparse
import os
import sys
from getpass import getpass

import httpx
import requests
from wyze_sdk import Client
from wyze_sdk.errors import WyzeApiError, WyzeClientError

from . import __version__
from .config import ConfigError, Settings
from .credentials import BridgeAuth, CredentialStoreError, SecretStore, Tokens
from .server import quiet_library_logging, serve
from .wyze import NotLoggedIn, WyzeCloud, WyzeError

_API_KEY_URL = "https://developer-api-console.wyze.com/#/apikey/view"


def _require_terminal() -> bool:
    if sys.stdin.isatty():
        return True
    print("This command asks for secrets and must be run in an interactive terminal.", file=sys.stderr)
    return False


def _camera_summary(store: SecretStore) -> str:
    with httpx.Client(timeout=30) as http:
        cameras = WyzeCloud(store, http).cameras()
    names = ", ".join(camera.name for camera in cameras) or "none"
    return f"{len(cameras)} camera(s): {names}"


def _login(store: SecretStore) -> int:
    if not _require_terminal():
        return 2
    print(f"You need a Wyze API key. Create one at {_API_KEY_URL}")
    print("Your password and API key are used once to sign in and are not saved anywhere.\n")
    email = input("Wyze email: ").strip()
    password = getpass("Wyze password (hidden): ")
    key_id = input("Wyze API Key ID: ").strip()
    api_key = getpass("Wyze API Key (hidden): ").strip()
    try:
        # wyze-sdk prompts here for a 2FA code if the account requires one.
        response = Client().login(email=email, password=password, key_id=key_id, api_key=api_key)
        tokens = Tokens(response["access_token"], response["refresh_token"])
    except WyzeApiError as err:
        # Only the SDK's own first line: the rest of the message is the raw server response.
        print(f"Login failed: {str(err).splitlines()[0]}", file=sys.stderr)
        return 1
    except (WyzeClientError, requests.RequestException, KeyError, TypeError) as err:
        print(f"Login failed ({type(err).__name__}).", file=sys.stderr)
        return 1
    store.save_tokens(tokens)
    print(f"\nSigned in. Session tokens saved to {store.backend_name}.")
    try:
        print(f"Found {_camera_summary(store)}")
    except (WyzeError, NotLoggedIn) as err:
        print(f"Signed in, but listing cameras failed: {err}", file=sys.stderr)
        return 1
    return 0


def _logout(store: SecretStore) -> int:
    tokens = store.load_tokens()
    if tokens is None:
        print("Not signed in.")
        return 0
    try:
        Client(token=tokens.access_token, refresh_token=tokens.refresh_token).logout()
        print("Wyze session revoked.")
    except (WyzeClientError, requests.RequestException):
        print("Could not revoke the session with Wyze; it will expire on its own.", file=sys.stderr)
    store.clear_tokens()
    print(f"Tokens removed from {store.backend_name}.")
    return 0


def _bridge_auth(store: SecretStore, clear: bool) -> int:
    if clear:
        store.clear_bridge_auth()
        print("Bridge credentials removed.")
        return 0
    if not _require_terminal():
        return 2
    username = input("wyze-bridge username: ").strip()
    password = getpass("wyze-bridge password (hidden): ")
    store.save_bridge_auth(BridgeAuth(username, password))
    print(f"Bridge credentials saved to {store.backend_name}.")
    return 0


def _status(store: SecretStore) -> int:
    settings = Settings.from_env(os.environ)
    print(f"Keychain:      {store.backend_name}")
    healthy = True
    try:
        print(f"Wyze:          signed in, {_camera_summary(store)}")
    except (NotLoggedIn, WyzeError) as err:
        healthy = False
        print(f"Wyze:          {err}")
    try:
        with httpx.Client(trust_env=False, timeout=10) as http:
            listing = http.get(f"{settings.vision_url}/models")
        listing.raise_for_status()
        models = {entry.get("id") for entry in listing.json().get("data", [])}
        found = "available" if settings.vision_model in models else "NOT FOUND on that server"
        healthy = healthy and settings.vision_model in models
        print(f"Vision model:  {settings.vision_model} at {settings.vision_url} ({found})")
    except (httpx.HTTPError, ValueError, AttributeError):
        healthy = False
        print(f"Vision model:  {settings.vision_model} at {settings.vision_url} (server unreachable)")
    print(f"Live bridge:   {settings.bridge_url or 'not configured (event images only)'}")
    print(f"Armed hours:   {settings.armed_hours or 'not set'}")
    return 0 if healthy else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="camsentry-mcp",
        description="MCP server that lets a local model watch Wyze cameras. Runs the server when no command is given.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the MCP server over stdio (default)")
    commands.add_parser("login", help="sign in to Wyze and store session tokens in the OS keychain")
    commands.add_parser("logout", help="revoke the Wyze session and delete the stored tokens")
    commands.add_parser("status", help="check the Wyze session, vision model and settings")
    bridge = commands.add_parser("bridge-auth", help="store the username/password of a protected wyze-bridge")
    bridge.add_argument("--clear", action="store_true", help="delete the stored bridge credentials")
    args = parser.parse_args(argv)

    quiet_library_logging()
    try:
        if args.command in (None, "serve"):
            serve(os.environ)
            return 0
        store = SecretStore()
        if args.command == "login":
            return _login(store)
        if args.command == "logout":
            return _logout(store)
        if args.command == "bridge-auth":
            return _bridge_auth(store, args.clear)
        return _status(store)
    except (ConfigError, CredentialStoreError) as err:
        print(f"camsentry-mcp: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
