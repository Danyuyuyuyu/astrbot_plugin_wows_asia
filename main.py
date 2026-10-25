"""AstrBot entry point.

The query logic, cards and snapshot tracking live in ./wows, a framework-free core shared verbatim with
nonebot-plugin-wows-asia (scripts/sync_core.py). This file only wires it to AstrBot: config, the /wows
command, the LLM tool, the background poller and cleanup.
"""
import asyncio
import re
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .wows.api import WowsApi
from .wows.app import WowsApp
from .wows.hints import HINTS
from .wows.renderer import CardRenderer
from .wows.ship_names import SHIP_NAMES
from .wows.storage import Storage
from .wows.tracker import Tracker

PLUGIN_NAME = "astrbot_plugin_wows_asia"
COMMANDS = ("wows", "窝窝战绩")

HINTS.update(
    app_id="请管理员在 AstrBot 管理面板 → 插件 → WoWS 亚服战绩 的配置里填写 WG application_id",
    browser="在插件配置里填写浏览器路径",
    max_tracked="在插件配置里调大“最多记录的玩家数”",
    prefix="指令前缀以 AstrBot 的唤醒词为准（默认 /），群聊里也可以 @机器人 后发送。",
)

TOOL_DOC = """查询窝窝/WoWS（战舰世界）亚服战绩。结果是一张卡片图片，会直接发到聊天里，工具返回卡片上的数据。用户明确要查战绩时才调用，一个请求只调用一次；数据以本次调用结果为准，不要凭之前的结果回答。
选指令（command 的写法，参数空格分隔、顺序随意）：总战绩/生涯→只写谁（不加“生涯”等字）；近况/最近战绩→最近 [谁]；某艘船整体打得怎样→单船 船名 [谁]（船名放第一个，看生涯数据）；某段时间/打了哪些场（可限定船）→场次 [谁] [筛选]（列出全部，默认近7天）；只要其中一把的细节→单场 [谁] [筛选] [第N把]（默认最新一把，第N把=从最新往前数）；拿不准时用 场次。
谁：查提问者本人或被@的群友时不写（插件按提问者的绑定、消息里的@查找）；其他人写玩家完整昵称或 id 账号ID。不要猜测或编造昵称。
筛选：今天 昨天 前天 近N天 本周 10-15 10-12~10-15｜上午 下午 晚上 凌晨 今天下午 14点后 14-18点｜船=船名（错字、同音字会自动纠正）｜随机 排位 单野 组队 双排 三排｜胜 负｜按伤害/击沉/经验/侦察/潜在（从高到低），伤害最低、击沉最少等（从低到高），最好/最差（按经验）｜前N。
例：查查我最近的战绩→最近；我昨天打得怎么样→场次 昨天；看看张三三今天下午打的伊吹→场次 张三三 今天下午 船=伊吹；张三三伤害最高的一把→单场 张三三 按伤害；近3天最差的5把→场次 近3天 最差5把；我的总战绩→（留空）；张三三总战绩→张三三；张三三的大和打得怎么样→单船 大和 张三三；绑定我的号 ABC→绑定 ABC。
看结果：卡片已发到聊天里，简短点评即可，不用逐项复述；“按时间第2/共2场”指发生顺序。最近战绩还没有数据时卡片已附生涯概要，不要再自动查总战绩；提示未绑定时请用户发“/wows 绑定 昵称”（只有用户给出昵称并要求绑定才代为绑定）；提示“是不是想查”时可换成建议的船名重查一次；只能查到开始记录之后的场次。

Args:
    command(string): 查询参数，即 /wows 后面的部分，如 "最近"、"场次 昨天 船=大和"、"单船 大和 张三三"；查本人总战绩时传空字符串
"""

_AT_TEXT = re.compile(r"@[^\s@()（）]*\((\d+)\)")  # aiocqhttp writes @others into message_str as "@昵称(QQ号)"


def _doc(text: str):
    """llm_tool reads the tool description from the docstring at decoration time."""
    def deco(func):
        func.__doc__ = text
        return func
    return deco


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
        SHIP_NAMES.path = Path(names) if names else Path(get_astrbot_data_path()) / "wows_ship" / "names" / "zh_sg.json"
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
        self.app = WowsApp(self.api, self.storage, self.renderer, tracker=self.tracker,
                           cache_seconds=_num(config, "cache_seconds", 60, 0, 3600),
                           min_battles=_num(config, "min_battles", 50, 0, 1000), poll_minutes=poll, logger=logger)
        self._poller: asyncio.Task | None = None

    async def initialize(self):
        if self.api.configured:
            self._poller = asyncio.create_task(self._poll_loop())
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
    def _args(text: str, event: AstrMessageEvent | None = None) -> str:
        """Drop the command word and “@昵称(QQ号)” texts (the @ itself is read from the message components)."""
        text = text or ""
        for c in event.get_messages() if event is not None else []:
            if isinstance(c, Comp.At):  # exact first: nicknames may contain spaces or brackets
                text = text.replace(f"@{c.name}({c.qq})", " ")
        text = _AT_TEXT.sub(" ", text).strip()
        text = re.sub(r"^[/／]", "", text)
        for name in COMMANDS:
            if text.lower() == name or text.lower().startswith(name + " "):
                return text[len(name):].strip()
        return text

    # ---- handlers ----
    @filter.command("wows", alias={"窝窝战绩"})
    async def wows(self, event: AstrMessageEvent):
        """WoWS 亚服战绩查询，发送 /wows 帮助 查看用法"""
        reply = await self.app.handle(self._user_key(event), self._args(event.message_str, event),
                                      mentions=self._mentions(event, event.message_str))
        if isinstance(reply, tuple):
            reply = reply[0]
        if isinstance(reply, bytes):
            yield event.chain_result([Comp.Image.fromBytes(reply)])
        else:
            yield event.plain_result(reply)

    @filter.llm_tool(name="wows_query")
    @_doc(TOOL_DOC)
    async def wows_query(self, event: AstrMessageEvent, command: str = ""):
        reply = await self.app.handle(self._user_key(event), self._args(command, event),
                                      mentions=self._mentions(event, command), ai=True)
        if isinstance(reply, tuple):
            image, caption = reply
            await event.send(event.chain_result([Comp.Image.fromBytes(image)]))
            return f"战绩卡片图片已发到聊天里，用户能看到。卡片上的数据：\n{caption}"
        if isinstance(reply, bytes):
            await event.send(event.chain_result([Comp.Image.fromBytes(reply)]))
            return "战绩卡片图片已发到聊天里，用户能看到。"
        return f"查询结果（还没有发给用户，请据此回答）：\n{reply}"

    async def terminate(self):
        if self._poller is not None:
            self._poller.cancel()
            try:
                await self._poller
            except (asyncio.CancelledError, Exception):
                pass
        if self.renderer is not None:
            await self.renderer.close()
        await self.api.close()
        self.storage.close()
