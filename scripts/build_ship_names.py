"""Refresh the bundled client ship names (wows/picker/assets/ship_names.json) from wows-replay-data.

Usage: python scripts/build_ship_names.py
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wows.picker.names_source import fetch_upstream  # noqa: E402

OUT = ROOT / "wows" / "picker" / "assets" / "ship_names.json"


async def main() -> None:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        async def fetch(url: str, limit: int) -> bytes:
            response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > limit:
                raise RuntimeError("response too large")
            return response.content

        bundle = await fetch_upstream(fetch)
    OUT.write_text(json.dumps(bundle.payload(), ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{bundle.build}: {len(bundle.names)} names, {len(bundle.english)} english -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
