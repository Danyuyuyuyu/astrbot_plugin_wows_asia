"""Copy the shared, framework-free core from the NoneBot plugin into ./wows.

Usage: python scripts/sync_core.py [path/to/nonebot-plugin-wows-asia]   (default: ../nonebot-plugin-wows-asia)

Every module except the NoneBot entry point (__init__.py) and its pydantic settings (config.py) is copied
verbatim; framework-specific wording lives in hints.py and is overridden by main.py at startup.
"""
import re
import shutil
import sys
from pathlib import Path

SKIP = {"__init__.py", "config.py"}
# tests of the NoneBot entry point itself; the AstrBot entry point has its own tests (tests/test_main.py)
NONEBOT_TESTS = ("test_plugin_loaded", "test_ai_guide_examples_parse")


def _drop_tests(text: str, names) -> str:
    for name in names:
        text = re.sub(rf"\n\n\n(?:async )?def {name}\(.*?(?=\n\n\n(?:@|def |async def |class )|\Z)", "", text,
                      flags=re.S)
    return text
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    src_repo = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "nonebot-plugin-wows-asia"
    src = src_repo / "nonebot_plugin_wows_asia"
    if not (src / "app.py").is_file():
        print(f"找不到核心代码：{src}")
        return 1
    dst = ROOT / "wows"
    dst.mkdir(exist_ok=True)
    wanted = {p.name for p in src.glob("*.py") if p.name not in SKIP}
    for stale in dst.glob("*.py"):
        if stale.name != "__init__.py" and stale.name not in wanted:
            stale.unlink()
            print(f"删除 {stale.name}")
    for name in sorted(wanted):
        shutil.copyfile(src / name, dst / name)
    print(f"已同步 {len(wanted)} 个核心模块：{src} -> {dst}")
    # tests for the core: same fixtures, imports pointed at the copied package
    for name in ("fakes.py", "test_plugin.py", "test_ship_names.py"):
        path = src_repo / "tests" / name
        if path.is_file():
            text = _drop_tests(path.read_text(encoding="utf-8"), NONEBOT_TESTS).replace("nonebot_plugin_wows_asia", "wows")
            target = ROOT / "tests" / ("test_core.py" if name == "test_plugin.py" else name)
            target.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
