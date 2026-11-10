"""Bounded image download. Error messages never contain the URL (Wyze URLs are pre-signed)."""

from __future__ import annotations

import httpx

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ImageError(Exception):
    """An image could not be fetched or is not an image."""


def image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ImageError("The camera returned data that is not a JPEG, PNG or WebP image.")


def download_image(
    http: httpx.Client,
    url: str,
    *,
    auth: tuple[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    """Return (bytes, mime type) for the image at `url`."""
    buffer = bytearray()
    try:
        with http.stream("GET", url, auth=auth, headers=headers) as response:
            if response.status_code != 200:
                raise ImageError(f"The image request returned HTTP {response.status_code}.")
            for chunk in response.iter_bytes():
                buffer += chunk
                if len(buffer) > MAX_IMAGE_BYTES:
                    raise ImageError("The image is larger than 10 MB.")
    except httpx.HTTPError as err:
        raise ImageError(f"Could not download the image ({type(err).__name__}).") from None
    data = bytes(buffer)
    return data, image_mime(data)
