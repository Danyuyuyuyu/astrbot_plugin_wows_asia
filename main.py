"""AstrBot entry point.

The query logic, cards, snapshot tracking, the ship picker and the structured LLM tool live in ./wows
(framework-free, tested directly). This file wires them to AstrBot: config, the /wows and /这把玩什么船
commands, the LLM tool and when to offer it, the background jobs and cleanup.
"""
import asyncio
import re
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.provider.register import llm_tools
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .wows.api import WowsApi
from .wows.app import WowsApp
from .wows.commands import WowsError
from .wows.hints import HINTS
from .wows.picker.service import Picker, PickerSettings
from .wows.renderer import CardRenderer
from .wows.ship_names import SHIP_NAMES
from .wows.storage import Storage
from .wows.tracker import Tracker
from .wows import tool as wows_tool

PLUGIN_NAME = "astrbot_plugin_wows_asia"
COMMANDS = ("wows", "窝窝战绩")
PICK_COMMANDS = ("这把玩什么船", "选船", "随机船")
PICKER_TICK_SECONDS = 3600

HINTS.update(
    app_id="请管理员在 AstrBot 管理面板 → 插件 → WoWS 亚服战绩与选船 的配置里填写 WG application_id",
    browser="在插件配置里填写浏览器路径",
    max_tracked="在插件配置里调大“最多记录的玩家数”",
    prefix="指令前缀以 AstrBot 的唤醒词为准（默认 /），群聊里也可以 @机器人 后发送。",
)

# messages that may be about WoWS keep the tool; other chats don't pay for its definition
_TOPIC = re.compile(r"wows|窝窝|战舰|战绩|船|场次|单场|胜率|伤害|击沉|经验|排位|随机|单野|单排|双排|三排|组队|开黑|绑定|生涯|"
                    r"打得|打的|几把|几场|一把|那把|最近|军团", re.I)
FOLLOW_UP_SECONDS = 600

_AT_TEXT = re.compile(r"@[^\s@()（）]*\((\d+)\)")  # aiocqhttp writes @others into message_str as "@昵称(QQ号)"


def _doc(text: str):
    """llm_tool reads the tool description from the docstring at decoration time."""
    def deco(func):
        func.__doc__ = text
        return func
    return deco


def _schema(name: str, parameters: dict):
    """Docstring-declared tools only support plain types; swap in the full JSON schema (enums, required)."""
    def deco(func):
        for tool in reversed(llm_tools.func_list):
            if tool.name == name:
                tool.parameters = parameters
                break
        return func
    return deco


def _picker_settings(config) -> PickerSettings:
    group = config.get("ship_picker") if hasattr(config, "get") else None
    group = group if isinstance(group, dict) else {}
    lo = _num(group, "tier_min", 5, 1, 11)
    hi = _num(group, "tier_max", 10, 1, 11)
    style = str(group.get("image_style", "large"))
    return PickerSettings(
        enabled=bool(group.get("enabled", True)),
        cooldown=_num(group, "cooldown_seconds", 5, 0, 3600),
        tier_min=min(lo, hi), tier_max=max(lo, hi),
        default_pool="catalog" if str(group.get("default_pool", "auto")) == "catalog" else "auto",
        image_style=style if style in ("large", "small", "contour", "none") else "large",
        auto_update=bool(group.get("auto_update", True)),
        names_download=bool(group.get("names_auto_download", False)),
    )


def _num(config, key: str, default, lo, hi, cast=int):
    try:
        value = cast(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, lo), hi)


class WowsAsiaPlugin(Star):
    """WoWS 亚服战绩查询：总战绩、单船、最近战绩、场次和单场详情（/wows 帮助）。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        config = config if config is not None else {}
        self.config = config
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        names = str(config.get("ship_names_path", "") or "").strip()
        SHIP_NAMES.path = Path(names) if names else None  # bundled table (+ optional download) otherwise
        self.api = WowsApi(str(config.get("application_id", "") or ""),
                           timeout=_num(config, "timeout_seconds", 15, 1, 60, float))
        self.storage = Storage(data_dir / "wows_asia.sqlite3")
        poll = _num(config, "poll_minutes", 10, 3, 720)
        self.tracker = Tracker(self.api, self.storage, track_days=_num(config, "track_days", 14, 1, 90),
                               max_tracked=_num(config, "max_tracked", 200, 1, 5000),
                               record_days=_num(config, "record_days", 30, 15, 365), poll_minutes=poll,
                               fast_minutes=_num(config, "fast_poll_minutes", 3, 0, 60))
        self.renderer = None
        if str(config.get("render_mode", "image")) != "text":
            self.renderer = CardRenderer(str(config.get("browser_path", "") or ""),
                                         _num(config, "render_scale", 1.5, 1, 3, float))
        self.picker = Picker(self.api, self.storage, self.renderer, data_dir=data_dir, names=SHIP_NAMES,
                             settings=_picker_settings(config), logger=logger)
        self.app = WowsApp(self.api, self.storage, self.renderer, tracker=self.tracker,
                           cache_seconds=_num(config, "cache_seconds", 60, 0, 3600),
                           min_battles=_num(config, "min_battles", 50, 0, 1000), poll_minutes=poll, logger=logger,
                           picker=self.picker)
        self._poller: asyncio.Task | None = None
        self._picker_job: asyncio.Task | None = None
        self.cards_only = str(config.get("ai_reply", "点评")) == "只发卡片"
        self.on_demand = bool(config.get("tool_on_demand", True))
        extra = re.split(r"[\s,，、]+", str(config.get("tool_keywords", "") or ""))
        self.keywords = [w for w in extra if w]
        self._tool_used: dict[str, float] = {}  # session -> last time the tool ran

    async def initialize(self):
        if self.api.configured:
            self._poller = asyncio.create_task(self._poll_loop())
            if self.picker.settings.enabled:
                self._picker_job = asyncio.create_task(self._picker_loop())
        else:
            logger.warning("WoWS 亚服战绩：还没有配置 WG application_id，查询和战绩记录暂不可用。")

    async def _poll_loop(self):
        await asyncio.sleep(15)  # let AstrBot finish starting up
        while True:
            try:
                count = await self.tracker.poll_all()
                if count:
                    logger.debug(f"WoWS snapshot poll recorded {count} new battle rows")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one bad poll stop the loop
                logger.warning(f"WoWS snapshot poll failed: {type(exc).__name__}")
            await asyncio.sleep(self.tracker.tick_minutes * 60)

    async def _picker_loop(self):
        await asyncio.sleep(20)
        while True:
            try:
                await self.picker.maintenance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"WoWS ship picker maintenance failed: {type(exc).__name__}")
            await asyncio.sleep(PICKER_TICK_SECONDS)

    # ---- helpers ----
    @staticmethod
    def _user_key(event: AstrMessageEvent) -> str | None:
        sender = event.get_sender_id()
        return f"{event.get_platform_name()}:{sender}" if sender else None

    @staticmethod
    def _mentions(event: AstrMessageEvent, text: str = "") -> list[str]:
        """@-ed users (not the bot) -> user keys, so “/wows 最近 @某人” reads that user's binding."""
        platform, self_id = event.get_platform_name(), str(event.get_self_id())
        ids = [m.group(1) for m in _AT_TEXT.finditer(text)]
        ids += [str(c.qq) for c in event.get_messages() if isinstance(c, Comp.At)]
        out = []
        for qq in ids:
            if qq not in ("all", self_id, "") and f"{platform}:{qq}" not in out:
                out.append(f"{platform}:{qq}")
        return out

    @staticmethod
    def _args(text: str, event: AstrMessageEvent | None = None, commands=COMMANDS) -> str:
        """Drop the command word and “@昵称(QQ号)” texts (the @ itself is read from the message components)."""
        text = text or ""
        for c in event.get_messages() if event is not None else []:
            if isinstance(c, Comp.At):  # exact first: nicknames may contain spaces or brackets
                text = text.replace(f"@{c.name}({c.qq})", " ")
        text = _AT_TEXT.sub(" ", text).strip()
        text = re.sub(r"^[/／]", "", text)
        for name in commands:
            if text.lower() == name or text.lower().startswith(name + " "):
                return text[len(name):].strip()
        return text

    # ---- handlers ----
    @filter.command("wows", alias={"窝窝战绩"})
    async def wows(self, event: AstrMessageEvent):
        """WoWS 亚服战绩查询，发送 /wows 帮助 查看用法"""
        reply = await self.app.handle(self._user_key(event), self._args(event.message_str, event),
                                      mentions=self._mentions(event, event.message_str),
                                      requester=self._requester(event), is_admin=self._is_admin(event))
        yield self._result(event, reply)

    @filter.command("这把玩什么船", alias={"选船", "随机船"})
    async def pick_ship(self, event: AstrMessageEvent):
        """随机选一艘船：/这把玩什么船 [我的/全库] [国家] [舰种] [分类] [等级]，例如 /选船 日本 战列 8-10"""
        outcome = await self.picker.pick(self._user_key(event),
                                         self._args(event.message_str, event, PICK_COMMANDS),
                                         self._requester(event))
        yield self._result(event, outcome.reply())

    @staticmethod
    def _result(event: AstrMessageEvent, reply):
        if isinstance(reply, tuple):
            reply = reply[0]
        if isinstance(reply, bytes):
            return event.chain_result([Comp.Image.fromBytes(reply)])
        return event.plain_result(reply)

    @staticmethod
    def _requester(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_name() or event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @_schema(wows_tool.NAME, wows_tool.SCHEMA)
    @filter.llm_tool(name=wows_tool.NAME)
    @_doc(wows_tool.DESCRIPTION)
    async def wows_query(self, event: AstrMessageEvent, **args):
        self._tool_used[event.unified_msg_origin] = time.monotonic()
        try:
            cmd, mention, notes = wows_tool.parse_args(args)
        except wows_tool.InvalidArgs as exc:
            return wows_tool.invalid(str(exc))
        except WowsError as exc:  # e.g. a ship or player name the core rejects
            return wows_tool.invalid(str(exc))
        mentions = []
        if mention == "@":
            mentions = self._mentions(event, event.message_str)[:1]
            if not mentions:
                return wows_tool.invalid("消息里没有@任何人；查提问者本人时 mention_id 留空")
        elif mention:
            mentions = [f"{event.get_platform_name()}:{mention}"]
        outcome = await self.app.execute(self._user_key(event), cmd, mentions=mentions)
        if outcome.image is not None:
            await event.send(event.chain_result([Comp.Image.fromBytes(outcome.image)]))
            if self.cards_only:
                return None  # AstrBot ends the turn here: no extra model call, the card is the answer
        return wows_tool.result(outcome, sent=outcome.image is not None, notes=notes)

    @filter.on_llm_request()
    async def _offer_tool(self, event: AstrMessageEvent, req):
        """Only offer wows_query when the message looks WoWS-related (or right after it was used)."""
        if not self.on_demand or req.func_tool is None or req.func_tool.get_tool(wows_tool.NAME) is None:
            return
        if self._relevant(event):
            return
        req.func_tool.remove_tool(wows_tool.NAME)

    def _relevant(self, event: AstrMessageEvent) -> bool:
        used = self._tool_used.get(event.unified_msg_origin)
        if used is not None and time.monotonic() - used < FOLLOW_UP_SECONDS:
            return True
        text = event.message_str or ""
        if _TOPIC.search(text) or any(w in text for w in self.keywords):
            return True
        names = self.api.known_ship_names() | SHIP_NAMES.names()
        return any(len(n) >= 2 and n in text for n in names)

    async def terminate(self):
        for task in (self._poller, self._picker_job):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self.picker.close()
        if self.renderer is not None:
            await self.renderer.close()
        await self.api.close()
        self.storage.close()
