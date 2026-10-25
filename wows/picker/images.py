"""Ship image file cache: downloaded once from the WG CDN, embedded into cards as data URIs."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from pathlib import Path

from ..commands import WowsError
from .models import Ship

STYLES = {"large": ("image_large", "image_medium", "image_small", "image_contour"),
          "small": ("image_small", "image_medium", "image_large", "image_contour"),
          "contour": ("image_contour",), "none": ()}
RETRY_SECONDS = 600
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


def image_type(raw: bytes) -> str | None:
    for magic, kind in _MAGIC:
        if raw.startswith(magic):
            return kind
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


class ImageStore:
    def __init__(self, api, directory: Path, *, clock=time.monotonic):
        self.api = api
        self.dir = Path(directory)
        self.clock = clock
        self._failed: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def path_for(self, url: str) -> Path:
        return self.dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".img")

    def cached(self, url: str) -> bytes | None:
        try:
            raw = self.path_for(url).read_bytes()
        except OSError:
            return None
        return raw if image_type(raw) else None

    async def get(self, url: str) -> bytes:
        hit = self.cached(url)
        if hit is not None:
            return hit
        if self._failed.get(url, 0) > self.clock():
            raise WowsError("舰图暂时下载不了，稍后再试。")
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            hit = self.cached(url)
            if hit is not None:
                return hit
            try:
                raw = await self.api.fetch_image(url)
                if image_type(raw) is None:
                    raise WowsError("舰图内容无效。")
            except WowsError:
                self._failed[url] = self.clock() + RETRY_SECONDS
                raise
            finally:
                self._locks.pop(url, None)
            self._write(url, raw)
            return raw

    def _write(self, url: str, raw: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(url)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)  # the card still gets the image this time

    async def for_ship(self, ship: Ship, style: str = "large") -> tuple[str | None, bool]:
        """(data URI or None, is_contour). Falls through large -> medium -> small -> contour."""
        for attr in STYLES.get(style, STYLES["large"]):
            url = getattr(ship, attr)
            if not url:
                continue
            try:
                raw = await self.get(url)
            except WowsError:
                continue
            uri = f"data:{image_type(raw)};base64," + base64.b64encode(raw).decode()
            return uri, attr == "image_contour"
        return None, False

    def stats(self) -> tuple[int, int]:
        """(files, bytes) in the cache."""
        files = size = 0
        try:
            for path in self.dir.glob("*.img"):
                files += 1
                size += path.stat().st_size
        except OSError:
            pass
        return files, size

    def verify(self) -> int:
        """Delete unreadable / corrupt cached files; returns how many were removed."""
        removed = 0
        for path in list(self.dir.glob("*.img")) + list(self.dir.glob("*.tmp")):
            try:
                ok = path.suffix == ".img" and image_type(path.read_bytes()) is not None
            except OSError:
                ok = False
            if not ok:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    async def prefetch(self, ships: list[Ship], style: str = "large") -> tuple[int, int]:
        """Download the preferred image of every ship that has none cached: (downloaded, failed)."""
        done = failed = 0
        attrs = STYLES.get(style, STYLES["large"])
        for ship in ships:
            urls = [getattr(ship, a) for a in attrs if getattr(ship, a)]
            if not urls or any(self.cached(u) is not None for u in urls):
                continue
            try:
                await self.get(urls[0])
                done += 1
            except WowsError:
                failed += 1
            await asyncio.sleep(0.2)
        return done, failed
