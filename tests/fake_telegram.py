"""A fake Telegram Bot API (plus a fake ntfy endpoint) so the phone side of Holdco runs in tests without network.

Since Phase 3 the fake lives in holdco/bench/mocks.py, where Holdco Bench uses the same scripted behaviour; this
module re-exports it for the tests (see that module for what it records and the test hooks).

    tg = FakeTelegram().start()          # tg.url is the telegram_api_base, tg.token the bot token, tg.chat_id 42
    tg.push_callback("a:12:approve", message_id=tg.sent[-1]["message_id"])
    ...; tg.stop()
    python tests/fake_telegram.py 11998  # standalone, prints the URL
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run as a script: holdco is one folder up

from holdco.bench.mocks import MAX_WAIT, FakeTelegram  # noqa: E402,F401

if __name__ == "__main__":
    srv = FakeTelegram(int(sys.argv[1]) if len(sys.argv) > 1 else 11998)
    print(f"Fake Telegram on {srv.url} (token {srv.token}, chat id {srv.chat_id}); ntfy at {srv.url}/ntfy/<topic>",
          flush=True)
    srv.httpd.serve_forever()
