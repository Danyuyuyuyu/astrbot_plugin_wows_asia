"""Resolve WoWS API ship IDs to the official client names.

Names come from up to three files, merged with the later ones winning:
  1. the bundled table (wows/picker/assets/ship_names.json, refreshed with every plugin release)
  2. the downloaded table (opt-in auto download from wows-replay-data, see picker.names_source)
  3. a file the admin points at (``ship_names_path``): our JSON, a zh_sg.json export from the zhenxun
     wows_ship plugin, or a raw client global.mo
Names are keyed by the client ship code (PJSB018); ship ID -> code comes from the ship catalog
(``set_codes``) or from a legacy file's ``ship_ids``.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .picker.names_source import NamesError, load_names_file

logger = logging.getLogger(__name__)
BUNDLED_PATH = Path(__file__).resolve().parent / "picker" / "assets" / "ship_names.json"


class ShipNameMap:
    """Map API numeric ship IDs to names from the client localization bundle."""

    def __init__(self, path: Path | str | None = None, *, downloaded: Path | None = None,
                 bundled: Path | None = BUNDLED_PATH):
        self.path = Path(path) if path else None  # admin-provided file (optional)
        self.downloaded = downloaded
        self.bundled = bundled
        self._codes: dict[int, str] = {}
        self._signature: tuple | None = None
        self._names: dict[str, str] = {}
        self._english: dict[str, str] = {}
        self._file_ids: dict[int, str] = {}
        self._builds: dict[str, str | None] = {}
        self._warned: set[tuple] = set()

    # ---- sources ----
    def _sources(self) -> list[tuple[str, Path]]:
        out = []
        for label, path in (("bundled", self.bundled), ("downloaded", self.downloaded), ("custom", self.path)):
            if path is not None:
                out.append((label, Path(path)))
        return out

    @staticmethod
    def _stat(path: Path):
        try:
            info = path.stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    def _reload_if_needed(self) -> None:
        sources = self._sources()
        signature = tuple((label, str(path), self._stat(path)) for label, path in sources)
        if signature == self._signature:
            return
        names: dict[str, str] = {}
        english: dict[str, str] = {}
        file_ids: dict[int, str] = {}
        builds: dict[str, str | None] = {}
        for (label, path), (_, _, stat) in zip(sources, signature):
            if stat is None:
                if label == "custom" and (label, str(path), None) not in self._warned:
                    logger.warning("WoWS 客户端船名文件不存在: %s", path)
                    self._warned.add((label, str(path), None))
                continue
            try:
                data = load_names_file(path)
            except (OSError, ValueError, NamesError) as exc:
                key = (label, str(path), stat)
                if key not in self._warned:
                    logger.warning("WoWS 客户端船名文件无效，已跳过: %s (%s)", path, exc)
                    self._warned.add(key)
                continue
            names.update(data["names"])
            english.update(data["english"])
            file_ids.update(data["ship_ids"])
            builds[label] = data["build"]
        self._names, self._english, self._file_ids, self._builds = names, english, file_ids, builds
        self._signature = signature

    # ---- catalog codes ----
    def set_codes(self, codes: dict[int, str]) -> None:
        """ship_id -> client code (ship_id_str) from the synced ship catalog."""
        self._codes = dict(codes)

    def _code(self, ship_id: int) -> str | None:
        return self._codes.get(ship_id) or self._file_ids.get(ship_id)

    # ---- lookups ----
    def names(self) -> set[str]:
        """Client names of every known ship (used to spot ship names in chat)."""
        self._reload_if_needed()
        ids = set(self._codes) | set(self._file_ids)
        return {self._names[c] for c in (self._code(i) for i in ids) if c in self._names}

    def name_for(self, ship_id: int, fallback: str) -> str:
        self._reload_if_needed()
        try:
            code = self._code(int(ship_id))
        except (TypeError, ValueError):
            return fallback
        return self._names.get(code or "") or fallback

    def english_for(self, ship_id: int) -> str | None:
        self._reload_if_needed()
        try:
            code = self._code(int(ship_id))
        except (TypeError, ValueError):
            return None
        return self._english.get(code or "")

    def status(self) -> dict:
        self._reload_if_needed()
        return {"count": len(self._names), "builds": dict(self._builds)}


SHIP_NAMES = ShipNameMap()
