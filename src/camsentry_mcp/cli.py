"""Command line: run the MCP server, or set up and manage what it needs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from getpass import getpass

import httpx
import requests
from wyze_sdk import Client
from wyze_sdk.errors import WyzeApiError, WyzeClientError

from . import __version__
from .config import ConfigError, Settings
from .credentials import CredentialStoreError, SecretStore, Tokens
from .notify import FREE_SMS_KEY, Message, Notifier, NotifyError, new_ntfy_topic, normalize_phone
from .server import create_watch, quiet_library_logging, serve
from .store import Store
from .vision import RECOMMENDED_MODEL, VisionDetector, VisionError
from .watcher import Watcher
from .wyze import NotLoggedIn, WyzeCloud, WyzeError

_API_KEY_URL = "https://developer-api-console.wyze.com/#/apikey/view"


def _require_terminal() -> bool:
    if sys.stdin.isatty():
        return True
    print("This command asks questions and must be run in an interactive terminal.", file=sys.stderr)
    return False


def _camera_summary(secret_store: SecretStore) -> str:
    with httpx.Client(timeout=30) as http:
        cameras = WyzeCloud(secret_store, http).cameras()
    names = ", ".join(camera.name for camera in cameras) or "none"
    return f"{len(cameras)} camera(s): {names}"


def _notifier(secret_store: SecretStore, settings: Settings, http: httpx.Client) -> Notifier:
    return Notifier(secret_store, http, settings.ntfy_server)


# -- Wyze account ----------------------------------------------------------


def _login(secret_store: SecretStore) -> int:
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
    secret_store.save_tokens(tokens)
    print(f"\nSigned in. Session tokens saved to {secret_store.backend_name}.")
    try:
        print(f"Found {_camera_summary(secret_store)}")
    except (WyzeError, NotLoggedIn) as err:
        print(f"Signed in, but listing cameras failed: {err}", file=sys.stderr)
        return 1
    return 0


def _logout(secret_store: SecretStore) -> int:
    tokens = secret_store.load_tokens()
    if tokens is None:
        print("Not signed in.")
        return 0
    try:
        Client(token=tokens.access_token, refresh_token=tokens.refresh_token).logout()
        print("Wyze session revoked.")
    except (WyzeClientError, requests.RequestException):
        print("Could not revoke the session with Wyze; it will expire on its own.", file=sys.stderr)
    secret_store.clear_tokens()
    print(f"Tokens removed from {secret_store.backend_name}.")
    return 0


# -- notifications ---------------------------------------------------------


def _save_destination(secret_store: SecretStore, **values: str) -> None:
    secret_store.save_notify({**secret_store.load_notify(), **values})


def _notify_sms(secret_store: SecretStore) -> int:
    if not _require_terminal():
        return 2
    print("Text messages are sent through textbelt.com, which charges a few cents per text: buy a key")
    print("there first. Its free key does not send to US numbers. Some antivirus products block the site.")
    print("For free phone notifications use `camsentry-mcp notify ntfy` instead.\n")
    try:
        phone = normalize_phone(input("Mobile number to text (with country code if outside the US): "))
    except ValueError as err:
        print(str(err), file=sys.stderr)
        return 1
    key = getpass("Textbelt key (hidden; press Enter to try the free key, outside the US only): ").strip() or FREE_SMS_KEY
    _save_destination(secret_store, sms_phone=phone, sms_key=key)
    print(f"Saved to {secret_store.backend_name}. Send a test with `camsentry-mcp notify test`.")
    return 0


def _notify_ntfy(secret_store: SecretStore, settings: Settings) -> int:
    topic = secret_store.load_notify().get("ntfy_topic") or new_ntfy_topic()
    _save_destination(secret_store, ntfy_topic=topic)
    print("Free phone notifications through ntfy:")
    print("  1. Install the ntfy app (Android or iPhone).")
    print(f"  2. In the app, subscribe to this topic on {settings.ntfy_server}:")
    print(f"\n       {topic}\n")
    print(f"     Or open this address on the phone: {settings.ntfy_server}/{topic}")
    print("The topic name works like a password: anyone who knows it can read your alerts.")
    print("Send a test with `camsentry-mcp notify test`.")
    return 0


def _notify_webhook(secret_store: SecretStore) -> int:
    if not _require_terminal():
        return 2
    url = getpass("Webhook URL to POST alerts to (hidden): ").strip()
    if not url.startswith(("https://", "http://")):
        print("That is not an http(s) URL.", file=sys.stderr)
        return 1
    _save_destination(secret_store, webhook_url=url)
    print(f"Saved to {secret_store.backend_name}. Each alert is posted as JSON with title, body and urgent.")
    return 0


def _notify_test(secret_store: SecretStore, settings: Settings) -> int:
    with httpx.Client(timeout=30) as http:
        try:
            delivery = _notifier(secret_store, settings, http).send(
                Message("camsentry test", "Notifications are working. Camera alerts will arrive like this.")
            )
        except NotifyError as err:
            print(f"Test failed: {err}", file=sys.stderr)
            return 1
    print(f"Test sent via: {', '.join(delivery.delivered)}")
    for failure in delivery.failures:
        print(f"Not delivered: {failure}", file=sys.stderr)
    return 1 if delivery.failures else 0


def _notify_off(secret_store: SecretStore) -> int:
    secret_store.save_notify({})
    print("Notification destinations removed.")
    return 0


# -- setup, status, watching -----------------------------------------------


def _vision_line(settings: Settings) -> tuple[bool, str]:
    with httpx.Client(trust_env=False, timeout=30) as http:
        try:
            model = VisionDetector(settings.vision_url, settings.vision_model, http).model()
            if settings.vision_model:
                listing = http.get(f"{settings.vision_url}/models", timeout=10)
                listing.raise_for_status()
                if model not in {entry.get("id") for entry in listing.json().get("data", [])}:
                    return False, f"{model} at {settings.vision_url} (NOT FOUND on that server)"
                return True, f"{model} at {settings.vision_url}"
            return True, f"{model} at {settings.vision_url} (chosen automatically)"
        except VisionError as err:
            return False, str(err)
        except (httpx.HTTPError, ValueError, AttributeError):
            return False, f"the model server at {settings.vision_url} is unreachable"


def _status(secret_store: SecretStore, settings: Settings) -> int:
    print(f"Keychain:      {secret_store.backend_name}")
    healthy = True
    try:
        print(f"Wyze:          signed in, {_camera_summary(secret_store)}")
    except (NotLoggedIn, WyzeError) as err:
        healthy = False
        print(f"Wyze:          {err}")
    vision_ok, vision = _vision_line(settings)
    healthy = healthy and vision_ok
    print(f"Vision model:  {vision}")
    with httpx.Client() as http:
        channels = _notifier(secret_store, settings, http).channels()
    print(f"Notifications: {', '.join(channels) if channels else 'not set up (run `camsentry-mcp setup`)'}")
    print(f"Alerts sent:   {', '.join(sorted(settings.notify_types)) or 'none'}")
    print(f"Armed hours:   {settings.armed_hours or 'not set'}")
    watching = f"every {settings.poll_seconds} s while the server or `camsentry-mcp watch` runs" if settings.poll_seconds else "off"
    print(f"Watching:      {watching}")
    print(f"Memory:        {settings.data_dir / 'camsentry.db'}")
    return 0 if healthy else 1


def _mcp_config() -> str:
    command = shutil.which("camsentry-mcp") or "camsentry-mcp"
    return json.dumps({"mcpServers": {"camsentry": {"command": command}}}, indent=2)


def _setup(secret_store: SecretStore, settings: Settings) -> int:
    if not _require_terminal():
        return 2
    print("camsentry-mcp setup\n")
    print("Step 1 of 3: Wyze account")
    if secret_store.load_tokens() is None:
        if _login(secret_store) != 0:
            return 1
    else:
        print("Already signed in.")

    print("\nStep 2 of 3: local vision model")
    vision_ok, vision = _vision_line(settings)
    print(vision)
    if not vision_ok:
        print(f"Install Ollama from https://ollama.com, run `ollama pull {RECOMMENDED_MODEL}`, then run setup again.")
        return 1

    print("\nStep 3 of 3: how should camera alerts reach you?")
    print("  1. Free notifications in the ntfy phone app")
    print("  2. Text message (SMS)")
    print("  3. Not now")
    choice = input("Choose 1, 2 or 3: ").strip()
    if choice == "1":
        _notify_ntfy(secret_store, settings)
        input("Press Enter once you have subscribed in the app, and a test will be sent...")
    elif choice == "2" and _notify_sms(secret_store) != 0:
        return 1
    if choice in ("1", "2"):
        _notify_test(secret_store, settings)

    print("\nDone. Add this to your chat app's MCP servers and your model can use the cameras:\n")
    print(_mcp_config())
    print("\nCameras are watched whenever that app is running. To watch without it, run `camsentry-mcp watch`.")
    return 0


def _watch(settings: Settings) -> int:
    if not settings.poll_seconds:
        print("CAMSENTRY_POLL_SECONDS is 0, so there is nothing to watch.", file=sys.stderr)
        return 2
    watcher = Watcher(create_watch(settings), settings.poll_seconds)
    print(f"Watching your cameras every {settings.poll_seconds} seconds. Press Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            sent = watcher.cycle()
            if sent:
                print(f"{time.strftime('%H:%M:%S')} sent {sent} notification(s)", file=sys.stderr)
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        return 0


def _forget(settings: Settings) -> int:
    removed = Store(settings.data_dir / "camsentry.db").forget()
    print(f"Forgot {removed} analysed still(s). Watch rules and settings were kept.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="camsentry-mcp",
        description="MCP server that lets a local model watch Wyze cameras. Runs the server when no command is given.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the MCP server over stdio (default)")
    commands.add_parser("setup", help="guided setup: Wyze sign-in, vision model check, notifications")
    commands.add_parser("status", help="check the Wyze session, vision model, notifications and settings")
    commands.add_parser("watch", help="watch the cameras and send notifications without a chat app running")
    commands.add_parser("login", help="sign in to Wyze and store session tokens in the OS keychain")
    commands.add_parser("logout", help="revoke the Wyze session and delete the stored tokens")
    notify = commands.add_parser("notify", help="choose where alerts are sent")
    notify.add_argument("action", choices=["sms", "ntfy", "webhook", "test", "off"])
    commands.add_parser("forget", help="delete the remembered history of what the cameras saw")
    args = parser.parse_args(argv)

    quiet_library_logging()
    try:
        if args.command in (None, "serve"):
            serve(os.environ)
            return 0
        settings = Settings.from_env(os.environ)
        if args.command == "watch":
            return _watch(settings)
        if args.command == "forget":
            return _forget(settings)
        secret_store = SecretStore()
        if args.command == "setup":
            return _setup(secret_store, settings)
        if args.command == "login":
            return _login(secret_store)
        if args.command == "logout":
            return _logout(secret_store)
        if args.command == "notify":
            actions = {
                "sms": lambda: _notify_sms(secret_store),
                "ntfy": lambda: _notify_ntfy(secret_store, settings),
                "webhook": lambda: _notify_webhook(secret_store),
                "test": lambda: _notify_test(secret_store, settings),
                "off": lambda: _notify_off(secret_store),
            }
            return actions[args.action]()
        return _status(secret_store, settings)
    except (ConfigError, CredentialStoreError) as err:
        print(f"camsentry-mcp: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
