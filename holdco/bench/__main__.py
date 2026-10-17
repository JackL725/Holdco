"""python -m holdco.bench --model gpt-oss:20b [--only name ...] [--json] [--data DIR] [--base-url URL]

Runs Holdco Bench against a model and prints a table (name, pass, steps, seconds, failed assertions) and the four
scores, or the bench_runs row as JSON with --json. Exits 1 when any scenario fails.

Without --data the bench uses a new temporary data folder, deleted afterwards, so it never touches your companies;
the model server is the default one (http://127.0.0.1:11434) unless --base-url names another. With --data it uses
that folder and its settings.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import tempfile


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m holdco.bench", description="Score a model on Holdco's own work.")
    ap.add_argument("--model", required=True, help="the model to test, as the model server names it")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="run only these scenarios")
    ap.add_argument("--json", action="store_true", help="print the bench_runs row as JSON")
    ap.add_argument("--data", metavar="DIR", help="data folder to use (default: a temporary one)")
    ap.add_argument("--base-url", metavar="URL", help="model server address (default: the data folder's setting)")
    ap.add_argument("--backend", choices=("ollama", "openai"), help="model server API (default: the setting)")
    a = ap.parse_args(argv)

    temp = None
    if a.data:
        os.environ["HOLDCO_DATA"] = os.path.abspath(a.data)
    else:
        temp = tempfile.mkdtemp(prefix="holdco-bench-")
        os.environ["HOLDCO_DATA"] = temp
    if "holdco.config" in sys.modules:   # someone imported Holdco first: the bench would use their data folder
        print("Holdco was loaded before the bench could choose its data folder; run it as python -m holdco.bench.",
              file=sys.stderr)
        return 2
    try:
        from .. import db
        db.init()
        from . import runner, scenarios
        try:
            scenarios.select(a.only)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        settings = {}
        if a.base_url:
            settings["llm_base_url"] = a.base_url
        if a.backend:
            settings["llm_backend"] = a.backend

        def progress(name, done, total):
            if name and not a.json:
                print(f"[{done + 1}/{total}] {name} ...", file=sys.stderr, flush=True)

        with contextlib.redirect_stdout(sys.stderr):   # Holdco's warnings print; stdout carries only the result
            row = runner.run_all(a.model, only=a.only, settings=settings, progress=progress)
        if a.json:
            print(json.dumps(row, indent=2, default=str))
        else:
            print(table(row))
        return 0 if row.get("scenarios") and row.get("passed") == row.get("scenarios") else 1
    finally:
        if temp:
            try:
                from .. import db
                if db._conn is not None:
                    db._conn.close()
            except Exception:
                pass
            shutil.rmtree(temp, ignore_errors=True)


def _pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def table(row: dict) -> str:
    """The results as text: one line per scenario, then the four scores."""
    results = row.get("results") or []
    width = max([len(r["name"]) for r in results] + [8])
    lines = [f"Holdco Bench: {row.get('model')}", "",
             f"{'scenario':<{width}}  pass  steps  seconds  failed"]
    for r in results:
        failed = "; ".join(f["assertion"] for f in r.get("failed") or [])
        if r.get("error"):
            failed = (failed + "; " if failed else "") + f"error: {r['error']}"
        lines.append(f"{r['name']:<{width}}  {'yes' if r['passed'] else 'NO':<4}  {r['steps']:>5}  "
                     f"{r['seconds']:>7.1f}  {failed}")
    tps = row.get("tokens_per_second")
    lines += ["", f"Pass {row.get('passed')} of {row.get('scenarios')} ({_pct(row.get('pass_rate'))}), "
                  f"tool calls valid {_pct(row.get('tool_validity'))}, "
                  f"{'n/a' if tps is None else f'{tps:.0f}'} tokens/s, "
                  f"{row.get('steps_avg') if row.get('steps_avg') is not None else 'n/a'} steps per scenario"]
    if row.get("notes"):
        lines.append(row["notes"])
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
