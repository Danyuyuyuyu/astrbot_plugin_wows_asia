"""Wording that depends on the host bot framework.

Everything else in this package is framework-free and shared verbatim with the AstrBot port; each entry
point (NoneBot's __init__.py, AstrBot's main.py) may override these strings at startup."""
HINTS = {
    "app_id": "请管理员在 .env 中设置 WOWS_APPLICATION_ID 后重启机器人",
    "browser": "配置 WOWS_BROWSER_PATH",
    "max_tracked": "调大 WOWS_MAX_TRACKED",
    "prefix": "指令前缀以机器人的 COMMAND_START 配置为准。",
}
