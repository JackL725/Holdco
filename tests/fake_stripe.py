"""A fake Stripe API, so the Stripe connector and the Phase 1 gate run without network or a real account.

Since Phase 3 the fake lives in holdco/bench/mocks.py, where Holdco Bench uses the same scripted behaviour; this
module re-exports it for the tests (see that module for the endpoints, the test hooks and what it records).

    stripe = FakeStripe().start()
    stripe.add_charge(amount=1900, currency="usd", paid=True)   # a test purchase
    stripe.stop()
    python tests/fake_stripe.py 12001   # standalone, prints the URL
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # run as a script: holdco is one folder up

from holdco.bench.mocks import FakeStripe, _lists, unflatten  # noqa: E402,F401

if __name__ == "__main__":
    srv = FakeStripe(int(sys.argv[1]) if len(sys.argv) > 1 else 0).start()
    print(f"Fake Stripe at {srv.url} (keys containing _test_ are accepted)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
