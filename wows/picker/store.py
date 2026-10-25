"""Picker tables inside the plugin's SQLite database (catalog, per-account owned lists, metadata)."""
from __future__ import annotations

import json

from ..storage import Storage
from .models import Ship

SCHEMA = """
CREATE TABLE IF NOT EXISTS pick_ship (ship_id INTEGER PRIMARY KEY, code TEXT, name TEXT NOT NULL,
    tier INTEGER NOT NULL, nation TEXT NOT NULL, type TEXT NOT NULL, premium INTEGER NOT NULL DEFAULT 0,
    special INTEGER NOT NULL DEFAULT 0, image_small TEXT, image_medium TEXT, image_large TEXT, image_contour TEXT,
    active INTEGER NOT NULL DEFAULT 1, first_seen INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pick_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pick_owned (account_id INTEGER NOT NULL, ship_id INTEGER NOT NULL,
    PRIMARY KEY (account_id, ship_id));
CREATE TABLE IF NOT EXISTS pick_owned_meta (account_id INTEGER PRIMARY KEY, updated_at INTEGER NOT NULL,
    source TEXT NOT NULL);
"""
_SHIP_COLS = ("ship_id, name, tier, nation, type, premium, special, code, image_small, image_medium, image_large, "
              "image_contour")


class PickerStore:
    def __init__(self, storage: Storage):
        self.db = storage
        storage.script(SCHEMA)
        self._ships: list[Ship] | None = None

    # ---- metadata ----
    def meta(self, key: str, default=None):
        rows = self.db.query("SELECT value FROM pick_meta WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0][0])
        except ValueError:
            return default

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO pick_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    # ---- catalog ----
    def ships(self) -> list[Ship]:
        """Active ships (cached in memory until the catalog changes)."""
        if self._ships is None:
            rows = self.db.query(f"SELECT {_SHIP_COLS} FROM pick_ship WHERE active=1 ORDER BY ship_id")
            self._ships = [Ship(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]), *r[7:]) for r in rows]
        return self._ships

    def ship(self, ship_id: int) -> Ship | None:
        return next((s for s in self.ships() if s.ship_id == ship_id), None)

    def codes(self) -> dict[int, str]:
        return {r[0]: r[1] for r in self.db.query("SELECT ship_id, code FROM pick_ship WHERE code IS NOT NULL")}

    def replace_catalog(self, ships: list[Ship], now: int) -> tuple[int, int, int]:
        """Upsert a complete catalog; ships missing from it are deactivated. Returns (added, updated, removed)."""
        def work(db):
            known = {r[0]: r[1:] for r in db.execute(f"SELECT {_SHIP_COLS}, active FROM pick_ship")}
            added = updated = 0
            for s in ships:
                row = (s.ship_id, s.name, s.tier, s.nation, s.type, int(s.premium), int(s.special), s.code,
                       s.image_small, s.image_medium, s.image_large, s.image_contour)
                old = known.get(s.ship_id)
                if old is None:
                    added += 1
                elif tuple(old[:-1]) != row[1:] or not old[-1]:
                    updated += 1
                else:
                    continue
                db.execute(
                    "INSERT INTO pick_ship (ship_id, name, tier, nation, type, premium, special, code, image_small,"
                    " image_medium, image_large, image_contour, active, first_seen, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?) ON CONFLICT(ship_id) DO UPDATE SET"
                    " name=excluded.name, tier=excluded.tier, nation=excluded.nation, type=excluded.type,"
                    " premium=excluded.premium, special=excluded.special, code=excluded.code,"
                    " image_small=excluded.image_small, image_medium=excluded.image_medium,"
                    " image_large=excluded.image_large, image_contour=excluded.image_contour, active=1,"
                    " updated_at=excluded.updated_at", row + (now, now))
            fetched = {s.ship_id for s in ships}
            gone = [sid for sid, old in known.items() if old[-1] and sid not in fetched]
            db.executemany("UPDATE pick_ship SET active=0, updated_at=? WHERE ship_id=?", [(now, sid) for sid in gone])
            return added, updated, len(gone)

        result = self.db.transaction(work)
        self._ships = None
        return result

    def count(self) -> int:
        return len(self.ships())

    # ---- owned lists (keyed by WG account, shared by everyone bound to it) ----
    def owned(self, account_id: int) -> set[int] | None:
        """None = no list set up yet (the picker then uses the ships the account has played)."""
        if not self.db.query("SELECT 1 FROM pick_owned_meta WHERE account_id=?", (account_id,)):
            return None
        return {r[0] for r in self.db.query("SELECT ship_id FROM pick_owned WHERE account_id=?", (account_id,))}

    def owned_meta(self, account_id: int) -> tuple[int, str] | None:
        rows = self.db.query("SELECT updated_at, source FROM pick_owned_meta WHERE account_id=?", (account_id,))
        return (rows[0][0], rows[0][1]) if rows else None

    def set_owned(self, account_id: int, ship_ids, now: int, source: str = "manual") -> None:
        ids = sorted(set(ship_ids))

        def work(db):
            db.execute("DELETE FROM pick_owned WHERE account_id=?", (account_id,))
            db.executemany("INSERT INTO pick_owned VALUES (?,?)", [(account_id, sid) for sid in ids])
            db.execute("INSERT INTO pick_owned_meta VALUES (?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                       "updated_at=excluded.updated_at, source=excluded.source", (account_id, now, source))
        self.db.transaction(work)

    def forget_owned(self, account_id: int) -> bool:
        def work(db):
            db.execute("DELETE FROM pick_owned WHERE account_id=?", (account_id,))
            return db.execute("DELETE FROM pick_owned_meta WHERE account_id=?", (account_id,)).rowcount > 0
        return self.db.transaction(work)

    # ---- played ships (from the snapshot tracker's ship_state table) ----
    def played(self, account_id: int) -> set[int]:
        return {r[0] for r in self.db.query(
            "SELECT DISTINCT ship_id FROM ship_state WHERE account_id=? AND battles>0", (account_id,))}
