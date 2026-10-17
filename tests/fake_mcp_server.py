"""A fake MCP server for tests: both protocol generations, over stdio or Streamable HTTP.

Since Phase 3 the fake lives in holdco/bench/mocks.py, where Holdco Bench uses the same scripted behaviour; this
module re-exports it for the tests and stays the script the stdio tests start (see that module for the tools, the
two protocol generations and the test hooks).

    fake = FakeMCPHttp(port=0, old=False, sse=False, auth_base=None).start()   # fake.url, fake.mcp_url
    ...; fake.stop()
    python tests/fake_mcp_server.py --stdio [--old]
    python tests/fake_mcp_server.py --http PORT [--old] [--sse] [--auth URL]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run as a script: holdco is one folder up

from holdco.bench.mocks import (CURRENT, META_CAPABILITIES, META_CLIENT, META_SERVER, META_VERSION,  # noqa: E402,F401
                                OLD, PNG_1X1, TOOLS, Call, FakeMCPCore, FakeMCPHttp, Headers, mcp_main, run_stdio)

main = mcp_main

if __name__ == "__main__":
    mcp_main()
