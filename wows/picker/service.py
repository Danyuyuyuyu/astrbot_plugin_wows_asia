"""Ship picker orchestration: pick / owned-list / catalog admin commands and background maintenance."""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shlex
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .. import views
from ..commands import WowsError
from ..outcome import Card, Outcome
from ..ship_match import match_ships, normalize
from ..ship_names import ShipNameMap
from ..storage import Storage
from . import names_source
from .card import Picked, pick_html, pick_text
from .catalog import BusyError, Catalog
from .images import ImageStore
from .models import POOL_LABELS, PickError, PickRequest
from .parser import parse_pick
from .randomizer import pick as pick_ships
from .store import PickerStore

NAMES_CHECK_SECONDS = 86400
PLAYED_TTL = 600
OWNED_USAGE = ("用法：/wows 持有（查看）｜持有 添加 船名…｜持有 删除 船名…｜持有 设置 船名…（整体替换）"
               "｜持有 导入（用玩过的船生成）｜持有 清空｜持有 关闭（改回用玩过的船）。船名之间用空格或逗号分隔")
_ADD, _DEL, _SET = {"添加", "加", "增加", "add", "+"}, {"删除", "移除", "去掉", "删", "del", "remove", "-"}, {"设置", "设为", "set"}
_LIST, _CLEAR, _IMPORT, _OFF = {"查看", "清单", "列表", "list"}, {"清空", "clear"}, {"导入", "同步", "import"}, {"关闭", "停用", "off"}


@dataclass
class PickerSettings:
    enabled: bool = True
    cooldown: int = 5
    tier_min: int = 5
    tier_max: int = 10
    default_pool: str = "auto"  # auto | catalog
    image_style: str = "large"  # large | small | contour | none
    auto_update: bool = True
    names_download: bool = False
    max_count: int = 5


async def _http_fetch(url: str, limit: int) -> bytes:
    import httpx

    # GitHub downloads honour the host's proxy settings (unlike the WG API client, which must go direct)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise names_source.NamesError(f"下载失败（HTTP {response.status_code}）")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise names_source.NamesError("下载内容过大")
            return bytes(body)


class Picker:
    def __init__(self, api, storage: Storage, renderer, *, data_dir: Path, names: ShipNameMap,
                 settings: PickerSettings | None = None, clock=time.time, logger=None, rng: random.Random | None = None,
                 fetch=None):
        self.api = api
        self.storage = storage
        self.renderer = renderer
        self.settings = settings or PickerSettings()
        self.clock = clock
        self.logger = logger
        self.rng = rng
        self.fetch = fetch or _http_fetch
        self.data_dir = Path(data_dir)
        self.store = PickerStore(storage)
        self.names = names
        names.downloaded = self.data_dir / "ship_names.json"
        self.catalog = Catalog(api, self.store, on_change=self._codes_changed, clock=clock)
        self.images = ImageStore(api, self.data_dir / "ship_images")
        self._codes_changed()
        self._cooldown: dict[str, float] = {}
        self._played: dict[int, tuple[float, set[int] | None]] = {}
        self._names_lock = asyncio.Lock()
        self._prefetch: asyncio.Task | None = None

    def _codes_changed(self) -> None:
        self.names.set_codes(self.store.codes())

    def _log(self, level: str, text: str) -> None:
        if self.logger is not None:
            getattr(self.logger, level)(text)

    # ---- 选船 ----
    async def pick(self, user_key: str | None, text: str, requester: str = "", *, again: str = "选船") -> Outcome:
        if not self.settings.enabled:
            return Outcome("disabled", "随机选船功能已被管理员关闭。")
        try:
            req = parse_pick(text, default_tiers=(self.settings.tier_min, self.settings.tier_max),
                             max_count=self.settings.max_count)
        except PickError as exc:
            return Outcome("invalid", exc.message)
        wait = self._cooldown_left(user_key)
        if wait > 0:
            return Outcome("cooldown", f"抽得太快啦，{wait} 秒后再试。")
        try:
            return await self._pick(user_key, req, views.clean(requester or "", 24).strip() or "这位舰长", again)
        except PickError as exc:
            return Outcome(exc.code, exc.message, options=exc.options)
        except WowsError as exc:
            return Outcome("error", str(exc))

    def _cooldown_left(self, user_key: str | None) -> int:
        if not user_key or self.settings.cooldown <= 0:
            return 0
        now = time.monotonic()
        until = self._cooldown.get(user_key, 0)
        return int(until - now) + 1 if until > now else 0

    def _start_cooldown(self, user_key: str | None) -> None:
        if not user_key or self.settings.cooldown <= 0:
            return
        now = time.monotonic()
        if len(self._cooldown) > 1024:
            self._cooldown = {k: v for k, v in self._cooldown.items() if v > now}
        self._cooldown[user_key] = now + self.settings.cooldown

    async def _ensure_catalog(self) -> None:
        if self.store.count():
            return
        if not self.api.configured:
            raise WowsError(f"尚未配置 WG API，船库还是空的：{views_hint()}。")
        if self.catalog.busy:
            raise PickError("catalog_busy", "船库正在初始化，请稍后再试。")
        try:
            await self.catalog.sync()
        except BusyError:
            raise PickError("catalog_busy", "船库正在初始化，请稍后再试。") from None

    async def played_ids(self, account_id: int) -> set[int] | None:
        """Ships the account has played (random or ranked). None = hidden profile."""
        ids = self.store.played(account_id)
        if ids:
            return ids
        hit = self._played.get(account_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        ships = await self.api.ship_stats(account_id)
        result = None if ships is None else {s["ship_id"] for s in views.played_ships(ships)}
        self._played[account_id] = (time.monotonic() + PLAYED_TTL, result)
        return result

    async def resolve_pool(self, user_key: str | None, requested: str) -> tuple[str, set[int] | None, list[str]]:
        """(pool, allowed ship IDs or None for the whole catalog, notes)."""
        if requested == "auto" and self.settings.default_pool == "catalog":
            requested = "catalog"
        if requested == "catalog":
            return "catalog", None, []
        bound = self.storage.binding(user_key) if user_key else None
        if bound is None:
            if requested == "auto":
                return "catalog", None, []
            raise PickError("not_bound", "还没有绑定账号：先发 /wows 绑定 你的昵称，或者加上“全库”从全部船里抽。")
        account_id = bound[0]
        if requested in ("auto", "mine", "owned"):
            owned = self.store.owned(account_id)
            if owned:
                return "owned", owned, []
            if requested == "owned":
                raise PickError("no_owned", "你的持有清单还是空的：用 /wows 持有 添加 船名，或 /wows 持有 导入。")
        try:
            played = await self.played_ids(account_id)
        except WowsError:
            if requested == "auto":
                return "catalog", None, ["暂时拿不到你玩过的船，这次从全库抽"]
            raise PickError("stats_unavailable", "暂时拿不到你的战绩，稍后再试，或者加上“全库”。") from None
        if played:
            return "played", played, []
        if requested == "auto":
            reason = "账号隐藏了战绩" if played is None else "还没有玩过的船记录"
            return "catalog", None, [f"{reason}，这次从全库抽"]
        if played is None:
            raise PickError("hidden", "你的账号隐藏了战绩，看不到玩过的船：设置持有清单（/wows 持有），或者加上“全库”。")
        raise PickError("no_history", "还没有你玩过的船的记录，可以加上“全库”。")

    async def _pick(self, user_key: str | None, req: PickRequest, requester: str, again: str) -> Outcome:
        await self._ensure_catalog()
        pool, allowed, notes = await self.resolve_pool(user_key, req.pool)
        try:
            ships = pick_ships(self.store.ships(), req.filter, allowed=allowed, count=req.count, rng=self.rng)
        except PickError:
            where = POOL_LABELS[pool]
            hint = "，换个条件或者加上“全库”试试" if pool != "catalog" else "，换个条件试试"
            raise PickError("empty", f"{where}里没有符合条件（{req.filter.describe()}）的船{hint}。") from None
        if len(ships) < req.count:
            notes.append(f"符合条件的只有 {len(ships)} 艘")
        self._start_cooldown(user_key)
        picks = []
        for ship in ships:
            image, contour = (None, False)
            if self.renderer is not None and self.settings.image_style != "none":
                image, contour = await self.images.for_ship(ship, self.settings.image_style)
            picks.append(Picked(ship, self.names.name_for(ship.ship_id, ship.name),
                                self.names.english_for(ship.ship_id), image, contour))
        text = pick_text(picks, requester, pool, notes)
        card = Card(pick_html(picks, requester, pool, notes, again), text, text)
        return await self._output(card)

    async def _output(self, card: Card) -> Outcome:
        out = Outcome(card.status, card.text, caption=card.caption or card.text, options=list(card.options))
        if self.renderer is None:
            return out
        try:
            out.image = await self.renderer.render(card.html)
        except Exception as exc:
            self._log("warning", f"WoWS pick card render failed, falling back to text: {type(exc).__name__}: {exc}")
        return out

    # ---- 持有清单 ----
    def display_name(self, ship_id: int) -> str:
        ship = self.store.ship(ship_id)
        return self.names.name_for(ship_id, ship.name if ship else str(ship_id))

    def find_ships(self, words: list[str]) -> tuple[set[int], list[str]]:
        """Ship names -> IDs over the active catalog (typos and homophones allowed). Returns (ids, problems)."""
        table: dict[int, list[str]] = {}
        for s in self.store.ships():
            names = [self.names.name_for(s.ship_id, s.name), s.name]
            if english := self.names.english_for(s.ship_id):
                names.append(english)
            table[s.ship_id] = names
        ids: set[int] = set()
        problems: list[str] = []
        for word in words:
            key = normalize(word)
            exact = {sid for sid, names in table.items() if any(normalize(n) == key for n in names)}
            found = exact or match_ships(word, table).ids
            if len(found) == 1:
                ids |= found
                continue
            if not found:
                suggestions = match_ships(word, table).suggestions
                problems.append(f"找不到“{word}”" + (f"（是不是：{'、'.join(suggestions)}）" if suggestions else ""))
            else:
                names = sorted({self.display_name(i) for i in found})
                problems.append(f"“{word}”对应多艘船：{'、'.join(names[:6])}{' 等' if len(names) > 6 else ''}，请写完整")
        return ids, problems

    @staticmethod
    def _words(text: str) -> list[str]:
        text = unicodedata.normalize("NFKC", text)
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        return [w for p in parts for w in re.split(r"[,，、;；]+", p) if w.strip()]

    async def owned(self, user_key: str | None, text: str) -> Outcome:
        bound = self.storage.binding(user_key) if user_key else None
        if bound is None:
            return Outcome("not_bound", "持有清单跟着绑定的账号走：先发 /wows 绑定 你的昵称。")
        account_id, nickname = bound
        words = self._words(text)
        action = words[0].lower() if words else "查看"
        args = words[1:]
        try:
            await self._ensure_catalog()
        except (PickError, WowsError) as exc:
            return Outcome("error", getattr(exc, "message", None) or str(exc))
        now = int(self.clock())
        if action in _LIST:
            return Outcome("ok", self._owned_text(account_id, nickname))
        if action in _OFF:
            self.store.forget_owned(account_id)
            return Outcome("ok", "已关闭持有清单：之后“我的”改为从你玩过的船里抽。")
        if action in _CLEAR:
            self.store.set_owned(account_id, [], now)
            return Outcome("ok", "持有清单已清空（“我的”会改用玩过的船；彻底关闭用 /wows 持有 关闭）。")
        if action in _IMPORT:
            self._played.pop(account_id, None)
            try:
                ships = await self.api.ship_stats(account_id)
            except WowsError as exc:
                return Outcome("error", str(exc))
            if ships is None:
                return Outcome("hidden", "账号隐藏了战绩，没法导入玩过的船，请手动添加。")
            known = {s.ship_id for s in self.store.ships()}
            ids = {s["ship_id"] for s in views.played_ships(ships)} & known
            self.store.set_owned(account_id, ids, now, source="played")
            return Outcome("ok", f"已用玩过的 {len(ids)} 艘船生成持有清单（卖掉的船可以用 /wows 持有 删除 船名 去掉）。")
        if action not in _ADD | _DEL | _SET:
            return Outcome("invalid", OWNED_USAGE)
        if not args:
            return Outcome("invalid", OWNED_USAGE)
        ids, problems = self.find_ships(args)
        if problems:
            return Outcome("invalid", "；".join(problems) + "。没有改动持有清单。")
        current = self.store.owned(account_id) or set()
        if action in _ADD:
            new = current | ids
        elif action in _DEL:
            new = current - ids
        else:
            new = set(ids)
        self.store.set_owned(account_id, new, now)
        changed = "、".join(sorted(self.display_name(i) for i in ids))
        verb = "添加" if action in _ADD else "删除" if action in _DEL else "设置为"
        return Outcome("ok", f"已{verb}：{changed}。持有清单现在共 {len(new)} 艘。")

    def _owned_text(self, account_id: int, nickname: str) -> str:
        owned = self.store.owned(account_id)
        if owned is None:
            return (f"{nickname} 还没有持有清单，“我的”会从玩过的船里抽。\n" + OWNED_USAGE)
        if not owned:
            return f"{nickname} 的持有清单是空的。\n" + OWNED_USAGE
        by_tier: dict[int, list[str]] = {}
        for sid in owned:
            ship = self.store.ship(sid)
            by_tier.setdefault(ship.tier if ship else 0, []).append(self.display_name(sid))
        lines = [f"{nickname} 的持有清单（{len(owned)} 艘）："]
        for tier in sorted(by_tier, reverse=True):
            names = sorted(by_tier[tier])
            label = f"{tier} 级" if tier else "已下架"
            shown = "、".join(names[:30]) + (f" 等 {len(names)} 艘" if len(names) > 30 else "")
            lines.append(f"{label}：{shown}")
        return "\n".join(lines)

    # ---- admin ----
    async def admin(self, action: str) -> Outcome:
        if action == "状态":
            return Outcome("ok", self.status_text())
        if not self.api.configured:
            return Outcome("error", f"尚未配置 WG API：{views_hint()}。")
        if action == "更新":
            try:
                report = await self.catalog.sync()
            except WowsError as exc:
                return Outcome("error", str(exc))
            lines = [report.text()]
            if self.settings.names_download:
                lines.append(await self.update_names(force=True))
            else:
                lines.append("客户端船名使用内置表（自动下载未开启）。")
            return Outcome("ok", "\n".join(lines))
        if action == "校验":
            removed = self.images.verify()
            if self._prefetch is not None and not self._prefetch.done():
                return Outcome("ok", f"已删除 {removed} 个损坏的舰图缓存；补全舰图的任务还在进行中。")
            self._prefetch = asyncio.create_task(self._prefetch_all())
            return Outcome("ok", f"已删除 {removed} 个损坏的舰图缓存，开始在后台补全缺少的舰图（可用 /wows 船库状态 查看）。")
        return Outcome("invalid", "用法：/wows 船库更新｜船库校验｜船库状态")

    async def _prefetch_all(self) -> None:
        try:
            done, failed = await self.images.prefetch(self.store.ships(), self.settings.image_style)
            self._log("info", f"WoWS ship images prefetched: {done} downloaded, {failed} failed")
        except Exception as exc:  # background task: never raise into the loop
            self._log("warning", f"WoWS ship image prefetch failed: {type(exc).__name__}")

    def status_text(self) -> str:
        count = self.store.count()
        synced = int(self.store.meta("synced_at", 0) or 0)
        version = self.store.meta("game_version") or "未知"
        lines = [f"船库：{count} 艘，游戏版本 {version}，" + (f"同步于 {_fmt(synced)}" if synced else "尚未同步")]
        if self.catalog.busy:
            lines.append("船库正在更新中")
        names = self.names.status()
        builds = names["builds"]
        source = ("自定义文件" if "custom" in builds else f"自动下载（{builds['downloaded']}）" if "downloaded" in builds
                  else f"内置（{builds.get('bundled') or '未知版本'}）" if "bundled" in builds else "无（使用 API 船名）")
        lines.append(f"客户端船名：{names['count']} 条，来源 {source}；自动下载{'已开启' if self.settings.names_download else '未开启'}")
        files, size = self.images.stats()
        lines.append(f"舰图缓存：{files} 张，{size / 1048576:.1f} MB" +
                     ("（后台补全中）" if self._prefetch is not None and not self._prefetch.done() else ""))
        lines.append(f"自动更新船库：{'开启' if self.settings.auto_update else '关闭'}")
        return "\n".join(lines)

    # ---- client names ----
    async def update_names(self, *, force: bool = False) -> str:
        if self._names_lock.locked():
            return "客户端船名正在下载中。"
        async with self._names_lock:
            current = None if force else self.store.meta("names_build")
            self.store.set_meta("names_checked_at", int(self.clock()))
            try:
                bundle = await names_source.fetch_upstream(self.fetch, current)
            except (names_source.NamesError, OSError, ValueError) as exc:
                return f"客户端船名下载失败，继续使用现有船名：{exc}"
            except Exception as exc:  # network errors from httpx etc.
                return f"客户端船名下载失败，继续使用现有船名（{type(exc).__name__}）。"
            if bundle is None:
                return f"客户端船名已是最新（{current}）。"
            path = self.names.downloaded
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(bundle.payload(), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            self.store.set_meta("names_build", bundle.build)
            return f"客户端船名已更新到 {bundle.build}（{len(bundle.names)} 条）。"

    # ---- background ----
    async def maintenance(self) -> None:
        """Called periodically: keep the catalog fresh and (opt-in) the client names current."""
        if not self.settings.enabled or not self.api.configured:
            return
        try:
            if self.settings.auto_update or self.store.count() == 0:
                report = await self.catalog.ensure_fresh()
                if report is not None:
                    self._log("info", f"WoWS {report.text()}")
        except BusyError:
            pass
        except WowsError as exc:
            self._log("warning", f"WoWS ship catalog sync failed: {exc}")
        if self.settings.names_download:
            checked = int(self.store.meta("names_checked_at", 0) or 0)
            if self.clock() - checked >= NAMES_CHECK_SECONDS:
                self._log("info", f"WoWS {await self.update_names()}")

    async def close(self) -> None:
        if self._prefetch is not None and not self._prefetch.done():
            self._prefetch.cancel()
            try:
                await self._prefetch
            except (asyncio.CancelledError, Exception):
                pass


def views_hint() -> str:
    from ..hints import HINTS
    return HINTS["app_id"]


def _fmt(ts: int) -> str:
    return views.fmt_time(ts, "%Y-%m-%d %H:%M")

