"""AstrBot entry point: loaded the way AstrBot loads plugins (<plugin dir>.main), driven with real event types."""
import importlib
import json
import time
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
picker_tool = importlib.import_module(f"{ROOT.name}.wows.picker.tool")

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
    assert "插件 → WoWS 亚服战绩与选船 的配置" in main.HINTS["app_id"] and ".env" not in main.HINTS["app_id"]


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


def tool_json(text):
    return json.loads(text)


async def test_llm_tool_sends_card_and_returns_status():
    plugin, fake = make_plugin()
    try:
        await plugin.app.handle("aiocqhttp:10001", "绑定 Example_Player")
        ev = Event("帮我看看我的总战绩")
        data = tool_json(await plugin.wows_query(ev, action="总战绩"))
        assert len(ev.sent) == 1 and isinstance(ev.sent[0].chain[0], Comp.Image)
        assert data["status"] == "ok" and data["card_sent_to_user"] is True
        assert "Example_Player 总战绩：随机 1,000 场" in data["data"] and "点评" in data["next"]
        ev2 = Event("查一下 Pi")
        data = tool_json(await plugin.wows_query(ev2, action="最近", player="Pi"))
        assert ev2.sent == [] and data["status"] == "error" and "至少 3 个字符" in data["message"]
        ev3 = Event("帮我看看他的")
        data = tool_json(await plugin.wows_query(ev3, action="单船"))
        assert ev3.sent == [] and data["status"] == "invalid" and "ship" in data["message"]
    finally:
        await plugin.terminate()


async def test_llm_tool_mentions_and_unbound():
    plugin, fake = make_plugin()
    try:
        ev = Event("查查 @小明(20002) 的战绩", [Comp.Plain("查查 "), Comp.At(qq="20002", name="小明")])
        data = tool_json(await plugin.wows_query(ev, action="总战绩", mention_id="20002"))
        assert data["status"] == "mention_not_bound" and data["card_sent_to_user"] is False
        await plugin.app.handle("aiocqhttp:20002", "绑定 Example_Player")
        data = tool_json(await plugin.wows_query(ev, action="总战绩", mention_id="@"))
        assert data["status"] == "ok" and len(ev.sent) == 1
        data = tool_json(await plugin.wows_query(ev, action="总战绩", player="@小明(20002)"))
        assert data["status"] == "ok"
        data = tool_json(await plugin.wows_query(Event("查他"), action="总战绩", mention_id="@"))
        assert data["status"] == "invalid"
        data = tool_json(await plugin.wows_query(Event("我的战绩", sender="30003"), action="最近"))
        assert data["status"] == "not_bound" and "/wows 绑定" in data["next"]
    finally:
        await plugin.terminate()


async def test_cards_only_mode_ends_turn():
    plugin, fake = make_plugin()
    plugin.cards_only = True
    try:
        await plugin.app.handle("aiocqhttp:10001", "绑定 Example_Player")
        ev = Event("我的总战绩")
        assert await plugin.wows_query(ev, action="总战绩") is None and len(ev.sent) == 1
        data = tool_json(await plugin.wows_query(Event("x"), action="绑定"))
        assert data["status"] == "invalid"  # text results still go back to the model
    finally:
        await plugin.terminate()


def test_llm_tool_registered_with_schema():
    tool = llm_tools.get_func("wows_query")
    assert tool is not None and tool.description == main.wows_tool.DESCRIPTION
    params = tool.parameters
    assert params["required"] == ["action"] and "单船" in params["properties"]["action"]["enum"]
    assert params["properties"]["limit"]["type"] == "integer"
    size = len(tool.description) + len(json.dumps(params, ensure_ascii=False))
    assert size <= 1300, size  # sent with every WoWS-related LLM request


async def test_tool_offered_on_demand():
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.provider.entities import ProviderRequest

    plugin, fake = make_plugin()
    other = FunctionTool(name="weather", description="d", parameters={"type": "object", "properties": {}})

    async def offered(text, **kw):
        req = ProviderRequest(prompt=text, func_tool=ToolSet([llm_tools.get_func("wows_query"), other]))
        await plugin._offer_tool(Event(text, **kw), req)
        assert req.func_tool.get_tool("weather") is not None
        return req.func_tool.get_tool("wows_query") is not None

    try:
        assert not await offered("今天中午吃什么")
        assert await offered("我昨天打得怎么样")
        assert await offered("看看张三三的战绩")
        plugin.api._ships[1] = (float("inf"), importlib.import_module(f"{ROOT.name}.wows.api").ShipInfo(1, "大和", 10, "Battleship"))
        assert await offered("张三三的大和呢")
        plugin.keywords = ["上分"]
        assert await offered("今晚一起上分")
        await plugin.wows_query(Event("x", sender="7"), action="绑定")
        assert await offered("那他呢")  # follow-up in the same session
        plugin._tool_used.clear()
        plugin.on_demand = False
        assert await offered("今天中午吃什么")
    finally:
        await plugin.terminate()


async def test_initialize_without_app_id_and_terminate():
    plugin = main.WowsAsiaPlugin(None, {})
    await plugin.initialize()
    assert plugin._poller is None
    await plugin.terminate()
    plugin = main.WowsAsiaPlugin(None, {"application_id": "x", "render_mode": "text", "poll_minutes": 1})
    assert plugin.tracker.poll_minutes == 3 and plugin.renderer is None  # clamped to the allowed range
    await plugin.initialize()
    assert plugin._poller is not None
    await plugin.terminate()
    assert plugin._poller.cancelled()


def test_schema_converts_for_providers():
    from astrbot.core.agent.tool import ToolSet

    tools = ToolSet([llm_tools.get_func("wows_query")])
    openai = tools.openai_schema()[0]["function"]
    assert openai["parameters"]["properties"]["sort"]["enum"][1] == "伤害"
    assert tools.anthropic_schema()[0]["input_schema"]["required"] == ["action"]
    gemini = json.dumps(tools.google_schema(), ensure_ascii=False)
    assert "单船" in gemini and "nth" in gemini

    pick_tools = ToolSet([llm_tools.get_func(picker_tool.NAME)])
    pick_openai = pick_tools.openai_schema()[0]["function"]
    assert pick_openai["parameters"]["properties"]["types"]["items"]["enum"][2] == "战列"
    assert pick_tools.anthropic_schema()[0]["input_schema"]["properties"]["count"]["maximum"] == 5


def test_picker_settings_from_config():
    s = main._picker_settings({"ship_picker": {"enabled": False, "cooldown_seconds": -5, "tier_min": 9,
                                               "tier_max": 6, "default_pool": "catalog", "image_style": "???",
                                               "names_auto_download": True}})
    assert (s.enabled, s.cooldown, s.tier_min, s.tier_max) == (False, 0, 6, 9)
    assert s.default_pool == "catalog" and s.image_style == "large" and s.names_download is True
    assert main._picker_settings({}).enabled is True


async def test_pick_command_and_wows_subcommand(tmp_path):
    plugin, fake = make_plugin()
    picker_mod = importlib.import_module(f"{ROOT.name}.wows.picker.service")
    names_mod = importlib.import_module(f"{ROOT.name}.wows.ship_names")
    plugin.picker = picker_mod.Picker(plugin.api, plugin.storage, StaticRenderer(), data_dir=tmp_path,
                                      names=names_mod.ShipNameMap(),
                                      settings=picker_mod.PickerSettings(cooldown=0))
    plugin.app.picker = plugin.picker
    try:
        assert plugin._args("选船 日本 战列", None, main.PICK_COMMANDS) == "日本 战列"
        assert plugin._args("/这把玩什么船", None, main.PICK_COMMANDS) == ""
        out = await collect(plugin.pick_ship(Event("选船 日本 战列 10")))
        assert isinstance(out[0].chain[0], Comp.Image)
        out = await collect(plugin.pick_ship(Event("选船 乱写")))
        assert "看不懂" in out[0].get_plain_text()
        out = await collect(plugin.wows(Event("wows 船库更新")))
        assert "只有管理员" in out[0].get_plain_text()
        admin = Event("wows 船库更新")
        admin.role = "admin"
        out = await collect(plugin.wows(admin))
        assert "船库已同步" in out[0].get_plain_text()
        out = await collect(plugin.wows(Event("wows 持有", sender="10099")))
        assert "先发 /wows 绑定" in out[0].get_plain_text()
    finally:
        await plugin.terminate()


def test_pick_command_registered():
    from astrbot.core.star.star_handler import star_handlers_registry

    names = {h.handler_name for h in star_handlers_registry}
    assert "pick_ship" in names and "wows" in names


def test_pick_tool_registered_with_schema():
    tool = llm_tools.get_func(picker_tool.NAME)
    assert tool is not None and tool.description == picker_tool.DESCRIPTION
    assert tool.parameters["properties"]["nations"]["type"] == "array"
    size = len(tool.description) + len(json.dumps(tool.parameters, ensure_ascii=False))
    assert size <= 1200, size  # only offered for pick intents, must stay compact


async def test_pick_tool_offered_on_demand():
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.provider.entities import ProviderRequest

    plugin, fake = make_plugin()
    stats = llm_tools.get_func(main.wows_tool.NAME)
    pick = llm_tools.get_func(picker_tool.NAME)

    async def offered(text):
        req = ProviderRequest(prompt=text, func_tool=ToolSet([stats, pick]))
        await plugin._offer_tool(Event(text), req)
        return (req.func_tool.get_tool(stats.name) is not None,
                req.func_tool.get_tool(pick.name) is not None)

    try:
        assert await offered("帮我选艘日本战列舰") == (False, True)
        assert await offered("我昨天打得怎么样") == (True, False)
        assert await offered("帮我选艘船，再看看我昨天的战绩") == (True, True)
        plugin._tool_used[(Event("x").unified_msg_origin, picker_tool.NAME)] = time.monotonic()
        assert (await offered("好的"))[1] is True  # follow-up after a pick keeps the pick tool
        plugin.on_demand = False
        assert await offered("今天中午吃什么") == (True, True)
    finally:
        await plugin.terminate()


async def test_llm_tool_picks_ship(tmp_path):
    plugin, fake = make_plugin()
    picker_mod = importlib.import_module(f"{ROOT.name}.wows.picker.service")
    names_mod = importlib.import_module(f"{ROOT.name}.wows.ship_names")
    plugin.picker = picker_mod.Picker(plugin.api, plugin.storage, StaticRenderer(), data_dir=tmp_path,
                                      names=names_mod.ShipNameMap(),
                                      settings=picker_mod.PickerSettings(cooldown=0))
    plugin.app.picker = plugin.picker
    try:
        ev = Event("帮我抽一艘日本战列舰")
        data = tool_json(await plugin.wows_pick_ship(
            ev, pool="全库", nations=["日本"], types=["战列"], tier_min=8, tier_max=10))
        assert data["status"] == "ok" and data["card_sent_to_user"] is True
        assert "抽到" in data["data"] and "tester" in data["data"] and len(ev.sent) == 1
        bad = tool_json(await plugin.wows_pick_ship(Event("x"), types=["乱写"]))
        assert bad["status"] == "invalid" and "修正" in bad["next"]
        plugin.picker.settings.enabled = False
        disabled = tool_json(await plugin.wows_pick_ship(Event("x"), pool="全库"))
        assert disabled["status"] == "disabled" and "没有开启" in disabled["message"]
    finally:
        await plugin.terminate()
