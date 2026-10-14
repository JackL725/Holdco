"""Start Holdco: the dashboard plus the 24/7 agent scheduler.

    python run.py              # opens the dashboard in your browser
    python run.py --no-browser # for autostart / services
"""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

if sys.version_info < (3, 10):
    sys.exit("Holdco needs Python 3.10 or newer. Get it from https://www.python.org/downloads/")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from holdco import config, db, llm  # noqa: E402
from holdco.scheduler import Scheduler  # noqa: E402
from holdco.server import make_server  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Holdco: AI-run companies on your own GPU")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.host not in ("127.0.0.1", "localhost") and not config.PASSWORD:
        sys.exit("Set HOLDCO_PASSWORD before exposing Holdco beyond this computer.")

    db.init()
    try:
        httpd = make_server(args.host, args.port)
    except OSError as e:
        sys.exit(f"Couldn't start on port {args.port} ({e}). Is Holdco already running? "
                 f"Open http://127.0.0.1:{args.port} or use --port.")

    scheduler = Scheduler()
    scheduler.start()

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}"
    s = db.get_settings()
    print(f"Holdco is running at {url}")
    print(f"Data folder: {config.DATA_DIR}")
    try:
        models = llm.list_models(s)
        print(f"Model server OK at {s['llm_base_url']} ({len(models)} models installed).")
        if s["default_model"] not in models and models:
            print(f"  Note: the default model '{s['default_model']}' isn't installed. "
                  f"Run: ollama pull {s['default_model']}  (or pick another model in Settings)")
    except llm.LLMError as e:
        print(f"Model server not reachable yet: {e}")
    print("Leave this window open; agents keep working while it runs. Press Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping. Work in progress will resume next time you start Holdco.")
    finally:
        scheduler.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
