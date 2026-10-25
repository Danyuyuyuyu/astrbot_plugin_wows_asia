"""Client ship names (``IDS_PXXX000`` entries of the game's ``global.mo``) and the optional upstream download.

Upstream is the third-party repository landaire/wows-replay-data. Each build directory holds symlinks into a
content-addressed store ``common/<2 hex>/<18 hex>`` whose name is the first 20 hex digits of the object's
SHA-256, so every downloaded object can be verified.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

RAW_BASE = "https://raw.githubusercontent.com/landaire/wows-replay-data/main/"
SOURCE = "landaire/wows-replay-data"
SHIP_KEY = re.compile(r"IDS_(P[A-Z]{3}[0-9]{3})")
CODE = re.compile(r"P[A-Z]{3}[0-9]{3}")
_BUILD_DIR = re.compile(r'^\s*dir\s*=\s*"(\d+(?:\.\d+)+_\d+)"\s*$', re.M)
_OBJECT = re.compile(r"common/([0-9a-f]{2})/([0-9a-f]{18})")
MAX_MO_BYTES = 30 * 1024 * 1024
LANGUAGES = ("zh_sg", "en")

Fetch = Callable[[str, int], Awaitable[bytes]]  # (url, max_bytes) -> body


class NamesError(Exception):
    pass


def read_mo_ship_names(raw: bytes) -> dict[str, str]:
    """GNU MO -> {ship code: name}. The game's MO files have no charset header; they are UTF-8."""
    if len(raw) < 28 or len(raw) > MAX_MO_BYTES:
        raise NamesError("语言文件大小无效")
    if raw[:4] == b"\xde\x12\x04\x95":
        order = "<"
    elif raw[:4] == b"\x95\x04\x12\xde":
        order = ">"
    else:
        raise NamesError("不是 MO 格式的语言文件")
    revision, count, originals, translations = struct.unpack_from(order + "IIII", raw, 4)
    if revision >> 16 > 1 or count > 500000:
        raise NamesError("语言文件版本或条数无效")
    for table in (originals, translations):
        if table < 28 or table + count * 8 > len(raw):
            raise NamesError("语言文件偏移表无效")
    out: dict[str, str] = {}
    for i in range(count):
        length, offset = struct.unpack_from(order + "II", raw, originals + i * 8)
        size, position = struct.unpack_from(order + "II", raw, translations + i * 8)
        if offset + length > len(raw) or position + size > len(raw):
            raise NamesError("语言文件条目越界")
        key = raw[offset:offset + length].split(b"\0")[0].decode("utf-8", "replace")
        match = SHIP_KEY.fullmatch(key)
        if match is None:
            continue
        name = raw[position:position + size].split(b"\0")[0].decode("utf-8", "replace").strip()
        if name and name != key:
            out[match.group(1)] = name
    if not out:
        raise NamesError("语言文件里没有舰船名称")
    return out


def _version_key(build_dir: str) -> tuple:
    version, build = build_dir.split("_")
    return tuple(int(p) for p in version.split(".")), int(build)


def latest_build(builds_toml: str) -> str:
    dirs = _BUILD_DIR.findall(builds_toml)
    if not dirs:
        raise NamesError("上游 builds.toml 里没有版本目录")
    return max(dirs, key=_version_key)


async def _object(fetch: Fetch, build: str, language: str) -> bytes:
    path = f"{build}/translations/{language}/LC_MESSAGES/global.mo"
    body = await fetch(RAW_BASE + path, MAX_MO_BYTES)
    if body[:4] in (b"\xde\x12\x04\x95", b"\x95\x04\x12\xde"):
        return body  # a checkout without symlinks: the file itself
    target = body.decode("ascii", "replace").strip()
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    match = _OBJECT.fullmatch(resolved)
    if len(body) > 200 or match is None:
        raise NamesError("上游语言文件链接格式无法识别")
    raw = await fetch(RAW_BASE + resolved, MAX_MO_BYTES)
    if hashlib.sha256(raw).hexdigest()[:20] != match.group(1) + match.group(2):
        raise NamesError("上游语言文件校验失败（哈希不一致）")
    return raw


@dataclass
class NameBundle:
    build: str
    names: dict[str, str]
    english: dict[str, str]
    source: str = SOURCE

    def payload(self) -> dict:
        return {"schema": 1, "source": self.source, "build": self.build, "language": "zh_sg",
                "names": dict(sorted(self.names.items())), "english": dict(sorted(self.english.items()))}


async def fetch_upstream(fetch: Fetch, current_build: str | None = None) -> NameBundle | None:
    """Download the newest build's zh_sg + en ship names; None when ``current_build`` is already the newest."""
    build = latest_build((await fetch(RAW_BASE + "builds.toml", 2 * 1024 * 1024)).decode("utf-8", "replace"))
    if build == current_build:
        return None
    names = read_mo_ship_names(await _object(fetch, build, "zh_sg"))
    english = read_mo_ship_names(await _object(fetch, build, "en"))
    return NameBundle(build, names, english)


def load_names_file(path: Path) -> dict:
    """Read a names file: our JSON bundle, a legacy zh_sg.json export (names + ship_ids), or a raw global.mo.

    Returns {"names": {code: name}, "english": {...}, "ship_ids": {id: code}, "build": str|None}.
    """
    raw = path.read_bytes()
    if raw[:4] in (b"\xde\x12\x04\x95", b"\x95\x04\x12\xde"):
        return {"names": read_mo_ship_names(raw), "english": {}, "ship_ids": {}, "build": None}
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("names"), dict):
        raise NamesError("船名文件缺少 names")
    names = {k: v.strip() for k, v in payload["names"].items()
             if isinstance(k, str) and CODE.fullmatch(k) and isinstance(v, str) and v.strip()}
    english_raw = payload.get("english") or payload.get("english_names") or {}
    english = {k: v.strip() for k, v in english_raw.items()
               if isinstance(k, str) and isinstance(v, str) and v.strip()} if isinstance(english_raw, dict) else {}
    ids_raw = payload.get("ship_ids") or {}
    ship_ids = {}
    if isinstance(ids_raw, dict):
        for key, code in ids_raw.items():
            if isinstance(key, str) and key.isdecimal() and isinstance(code, str) and CODE.fullmatch(code):
                ship_ids[int(key)] = code
    if not names:
        raise NamesError("船名文件是空的")
    build = payload.get("build") if isinstance(payload.get("build"), str) else None
    return {"names": names, "english": english, "ship_ids": ship_ids, "build": build}
