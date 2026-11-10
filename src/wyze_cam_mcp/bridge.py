"""Optional live snapshots from a self-hosted wyze-bridge (https://github.com/IDisposable/docker-wyze-bridge)."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from .credentials import BridgeAuth
from .images import download_image


def bridge_camera_name(nickname: str) -> str:
    """The bridge exposes cameras lowercased with spaces replaced by underscores."""
    return nickname.strip().lower().replace(" ", "_")


class LiveBridge:
    def __init__(self, base_url: str, http: httpx.Client, auth: BridgeAuth | None = None) -> None:
        self._base_url = base_url
        self._http = http
        self._auth = (auth.username, auth.password) if auth else None

    def snapshot(self, nickname: str) -> tuple[bytes, str]:
        url = f"{self._base_url}/api/snapshot/{quote(bridge_camera_name(nickname), safe='')}"
        return download_image(self._http, url, auth=self._auth)
