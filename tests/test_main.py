"""AstrBot entry point: loaded the way AstrBot loads plugins (<plugin dir>.main), driven with real event types."""
import importlib
import re
from pathlib import Path

import pytest

astrbot = pytest.importorskip("astrbot")

import astrbot.api.message_components as Comp  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember  # noqa: E402
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.provider.register import llm_tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
main = importlib.import_module(f"{ROOT.name}.main")
core = importlib.import_module(f"{ROOT.name}.wows.app")
Tracker = importlib.import_module(f"{ROOT.name}.wows.tracker").Tracker
commands = importlib.import_module(f"{ROOT.name}.wows.commands")

from .fakes import Clock, FakeWG  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake"


class StaticRenderer:
    async def render(self, html):
        return PNG

    async def close(self):
        pass


class Event(AstrMessageEvent):
    def __init__(self, text: str, components=None, sender="10001", self_id="999"):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = self_id
        msg.session_id = "g1"
        msg.message_id = "m1"
        msg.sender = MessageMember(sender, "tester")
        msg.message = components or [Comp.Plain(text)]
        msg.message_str = text
        msg.raw_message = None
        super().__init__(text, msg, PlatformMetadata("aiocqhttp", "test", "qq"), "g1")
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


def make_plugin(renderer=StaticRenderer()):
    plugin = main.WowsAsiaPlugin(None, {"application_id": "", "render_mode": "text"})
    fake, clock = FakeWG(), Clock()
    import httpx
    api_mod = importlib.import_module(f"{ROOT.name}.wows.api")
    api = api_mod.WowsApi("test-app-id", transport=httpx.MockTransport(fake.handler), min_interval=0)
    tracker = Tracker(api, plugin.storage, clock=clock)
    plugin.api, plugin.tracker = api, tracker
    plugin.app = core.WowsApp(api, plugin.storage, renderer, tracker=tracker, cache_seconds=0, clock=clock)
    return plugin, fake


async def collect(gen):
    return [r async for r in gen]


def test_hints_and_help_use_astrbot_wording():
    help_text = commands.help_text()
    assert "唤醒词" in help_text and "COMMAND_START" not in help_text
    assert "插件 → WoWS 亚服战绩 的配置" in main.HINTS["app_id"] and ".env" not in main.HINTS["app_id"]


def test_args_and_mentions():
    p = main.WowsAsiaPlugin
    at = Comp.At(qq="20002", name="小明 (二号)")
    ev = Event("wows 最近  @小明 (二号)(20002) ", [Comp.Plain("wows 最近 "), at])
    assert p._args(ev.message_str, ev) == "最近"
    assert p._mentions(ev, ev.message_str) == ["aiocqhttp:20002"]
    assert p._args("/wows 场次 昨天") == "场次 昨天" and p._args("窝窝战绩") == "" and p._args("单船 大和") == "单船 大和"
    self_at = Event("wows", [Comp.At(qq="999", name="bot"), Comp.Plain("wows")])
    assert p._mentions(self_at, "") == []  # waking the bot with @ is not a target
    assert p._user_key(ev) == "aiocqhttp:10001"


async def test_command_text_and_image():
    plugin, fake = make_plugin()
    try:
        out = await collect(plugin.wows(Event("wows 帮助")))
        assert "WoWS 亚服战绩查询" in out[0].get_plain_text()
        out = await collect(plugin.wows(Event("wows 绑定 Example_Player")))
        assert "已绑定：Example_Player" in out[0].get_plain_text()
        out = await collect(plugin.wows(Event("wows")))
        assert isinstance(out[0].chain[0], Comp.Image)
    finally:
        await plugin.terminate()


async def test_llm_tool_sends_card_and_returns_data():
    plugin, fake = make_plugin()
    try:
        ev = Event("帮我看看我的总战绩")
        await plugin.app.handle("aiocqhttp:10001", "绑定 Example_Player")
        result = await plugin.wows_query(ev, "wows")
        assert len(ev.sent) == 1 and isinstance(ev.sent[0].chain[0], Comp.Image)
        assert "卡片上的数据" in result and "Example_Player 总战绩：随机 1,000 场" in result
        ev2 = Event("查一下")
        result = await plugin.wows_query(ev2, "最近 Pi")
        assert ev2.sent == [] and "还没有发给用户" in result and "至少 3 个字符" in result
    finally:
        await plugin.terminate()


def test_llm_tool_registered_with_guide():
    tool = llm_tools.get_func("wows_query")
    assert tool is not None and "选指令" in tool.description and "TOOL_DOC" not in tool.description
    assert tool.parameters["properties"]["command"]["type"] == "string"
    assert len(tool.description) <= 1100  # sent with every LLM request


def test_tool_guide_examples_parse():
    line = next(x for x in main.TOOL_DOC.splitlines() if x.startswith("例："))
    examples = re.findall(r"→([^；。]+)", line)
    assert len(examples) >= 8
    actions = {"最近": "recent", "场次": "battles", "单场": "battle", "绑定": "bind", "单船": "ship", "战舰": "ship"}
    for example in examples:
        args = "" if example == "（留空）" else example
        cmd = commands.parse_command(args)
        assert cmd.action == actions.get(args.split()[0] if args else "", "total"), example


async def test_initialize_without_app_id_and_terminate():
    plugin = main.WowsAsiaPlugin(None, {})
    await plugin.initialize()
    assert plugin._poller is None
    await plugin.terminate()
    plugin = main.WowsAsiaPlugin(None, {"application_id": "x", "render_mode": "text", "poll_minutes": 1})
    assert plugin.tracker.poll_minutes == 3 and plugin.renderer is None  # clamped like the NoneBot config
    await plugin.initialize()
    assert plugin._poller is not None
    await plugin.terminate()
    assert plugin._poller.cancelled()
