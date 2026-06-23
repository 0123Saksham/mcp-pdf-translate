"""Verify pipeline works when called via asyncio.to_thread (MCP pattern)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import run_pipeline_path


async def main() -> None:
    inp = sys.argv[1] if len(sys.argv) > 1 else ""
    if not inp:
        print("usage: python tests/test_mcp_async.py <input.pdf> [output.pdf]")
        raise SystemExit(1)
    out = sys.argv[2] if len(sys.argv) > 2 else None
    result = await asyncio.to_thread(run_pipeline_path, inp, "en", out)
    print(result.get("ok"), result.get("stats", {}).get("timing_s"))


if __name__ == "__main__":
    asyncio.run(main())
