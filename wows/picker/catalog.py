"""Sync the ship catalog from WG encyclopedia/ships into SQLite."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ..commands import WowsError
from .models import TYPES, Ship
from .store import PickerStore

VERSION_CHECK_SECONDS = 6 * 3600
MAX_AGE_SECONDS = 7 * 86400
MAX_PAGES = 50
MIN_KEEP_RATIO = 0.5  # a fetch with far fewer ships than we have is treated as broken, not as removals


class BusyError(WowsError):
    pass


@dataclass
class SyncReport:
    count: int
    added: int
    updated: int
    removed: int
    skipped: int
    game_version: str | None

    def text(self) -> str:
        parts = [f"船库已同步：共 {self.count} 艘"]
        changes = [f"新增 {self.added}" if self.added else "", f"更新 {self.updated}" if self.updated else "",
                   f"下架 {self.removed}" if self.removed else ""]
        changes = [c for c in changes if c]
        parts.append("（" + "，".join(changes) + "）" if changes else "（没有变化）")
        if self.game_version:
            parts.append(f"，游戏版本 {self.game_version}")
        if self.skipped:
            parts.append(f"；{self.skipped} 条数据格式异常已跳过")
        return "".join(parts) + "。"


def _url(images: dict, key: str) -> str | None:
    value = images.get(key)
    return value if isinstance(value, str) and value.startswith("https://") else None


def parse_ship(key: str, row) -> Ship | None:
    if not isinstance(row, dict):
        return None
    try:
        ship_id = int(row.get("ship_id") or key)
        tier = int(row["tier"])
    except (KeyError, TypeError, ValueError):
        return None
    name, nation, kind = row.get("name"), row.get("nation"), row.get("type")
    if str(ship_id) != str(key) or not 1 <= tier <= 11 or kind not in TYPES:
        return None
    if not isinstance(name, str) or not name.strip() or not isinstance(nation, str) or not nation:
        return None
    images = row.get("images") if isinstance(row.get("images"), dict) else {}
    code = row.get("ship_id_str")
    return Ship(ship_id, name.strip(), tier, nation, kind, row.get("is_premium") is True,
                row.get("is_special") is True, code if isinstance(code, str) and code.isascii() and code else None,
                _url(images, "small"), _url(images, "medium"), _url(images, "large"), _url(images, "contour"))


class Catalog:
    def __init__(self, api, store: PickerStore, *, on_change=None, clock=time.time):
        self.api = api
        self.store = store
        self.on_change = on_change  # called after the catalog changed (e.g. refresh ship-name codes)
        self.clock = clock
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def ships(self) -> list[Ship]:
        return self.store.ships()

    async def sync(self) -> SyncReport:
        if self._lock.locked():
            raise BusyError("船库正在更新，请稍后再试。")
        async with self._lock:
            return await self._sync()

    async def _sync(self) -> SyncReport:
        version = None
        try:
            version = await self.api.game_version()
        except WowsError:
            pass  # cosmetic: the full fetch below decides success
        ships: list[Ship] = []
        skipped = 0
        page, pages = 1, 1
        while page <= pages and page <= MAX_PAGES:
            data, pages = await self.api.catalog_page(page)
            for key, row in data.items():
                ship = parse_ship(str(key), row)
                if ship is None:
                    skipped += 1
                else:
                    ships.append(ship)
            page += 1
        if not ships:
            raise WowsError("WG API 返回的舰船目录是空的，船库保持不变。")
        current = self.store.count()
        if current and len(ships) < current * MIN_KEEP_RATIO:
            raise WowsError(f"WG API 只返回了 {len(ships)} 艘船（本地 {current} 艘），疑似数据不完整，船库保持不变。")
        now = int(self.clock())
        added, updated, removed = self.store.replace_catalog(ships, now)
        self.store.set_meta("synced_at", now)
        self.store.set_meta("version_checked_at", now)
        if version:
            self.store.set_meta("game_version", version)
        if self.on_change is not None:
            self.on_change()
        return SyncReport(self.store.count(), added, updated, removed, skipped, version)

    async def ensure_fresh(self) -> SyncReport | None:
        """Full sync when the catalog is empty, older than a week, or the game version changed."""
        now = int(self.clock())
        if self.store.count() == 0 or now - int(self.store.meta("synced_at", 0)) > MAX_AGE_SECONDS:
            return await self.sync()
        if now - int(self.store.meta("version_checked_at", 0)) < VERSION_CHECK_SECONDS:
            return None
        version = await self.api.game_version()
        self.store.set_meta("version_checked_at", now)
        if version and version != self.store.meta("game_version"):
            return await self.sync()
        return None
