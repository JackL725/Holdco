"""Holdco Bench (Phase 3, docs/phases/phase-3-verify.md section 1): scripted scenarios that seed a company, run one
agent session against mocks and assert on what happened, scoring any model on the owner's GPU.

RUNNING is set while a bench runs: the scheduler then skips task dispatch and CEO cycles so timing is not distorted
(chat still works). Bench companies have status 'bench' and are never dispatched or listed.

The pieces: scenarios.py (the 33 scenarios), mocks.py (the scripted outside world, also the test suite's fakes),
runner.py (run, check, score, record a bench_runs row) and __main__.py (python -m holdco.bench). The dashboard API
lives here:

    POST /api/bench/run {"model": "...", "only": [...]}   start a bench in the background; 409 while one runs
    GET  /api/bench/status                                {"running", "scenario", "done", "total", "run_id", ...}
    GET  /api/bench/results?limit=20                      {"runs": [...]}, newest first
    GET  /api/bench/results/<id>                          one run, its per-scenario results parsed

A bench started from the dashboard uses the live data folder with temporary bench companies; the mocks answer only in
the bench's own thread, so the owner's companies never meet them.
"""
from __future__ import annotations

import sys
import threading
import traceback

RUNNING = False
STATUS: dict = {"running": False, "scenario": "", "done": 0, "total": 0, "run_id": None, "model": "", "error": ""}
_start_lock = threading.Lock()


def _progress(scenario: str, done: int, total: int) -> None:
    STATUS.update(scenario=scenario, done=done, total=total)


def _work(model: str, only, settings: dict, run_id: int) -> None:
    from . import runner   # imported here: the runner loads every agent module
    try:
        runner.run_all(model, only=only, settings=settings, progress=_progress, run_id=run_id)
    except Exception as e:   # the row says what went wrong; the dashboard keeps working
        traceback.print_exc()
        STATUS["error"] = f"{type(e).__name__}: {e}"
    finally:
        STATUS.update(running=False, scenario="")


def _register_routes() -> None:
    from ..routing import HTTPError, route, to_int

    @route("POST", "/api/bench/run")
    def api_bench_run(h, m, body):
        from .. import db
        from . import runner, scenarios
        b = body if isinstance(body, dict) else {}
        model = str(b.get("model") or "").strip() or str(db.get_settings().get("default_model") or "")
        only = b.get("only") or None
        if only is not None and (not isinstance(only, list) or not all(isinstance(x, str) for x in only)):
            raise HTTPError(400, "only must be a list of scenario names.")
        try:
            chosen = scenarios.select(only)
        except ValueError as e:
            raise HTTPError(400, str(e)) from None
        if not model:
            raise HTTPError(400, "Pick a model to test.")
        with _start_lock:
            if RUNNING or STATUS["running"]:
                raise HTTPError(409, "A bench is already running. Wait for it to finish.")
            run_id = runner.new_row(model, {}, len(chosen))
            STATUS.update(running=True, scenario="", done=0, total=len(chosen), run_id=run_id, model=model, error="")
        threading.Thread(target=_work, args=(model, only, {}, run_id), daemon=True, name="holdco-bench").start()
        return {"run_id": run_id}

    @route("GET", "/api/bench/status")
    def api_bench_status(h, m, body):
        return {**STATUS, "running": bool(STATUS["running"] or RUNNING)}

    @route("GET", "/api/bench/results")
    def api_bench_results(h, m, body):
        from . import runner
        limit = max(1, min(to_int((getattr(h, "query", None) or {}).get("limit", [20])[0], 20), 200))
        return {"runs": runner.list_runs(limit)}

    @route("GET", r"/api/bench/results/(\d+)")
    def api_bench_result(h, m, body):
        from . import runner
        row = runner.get_run(to_int(m.group(1)))
        if not row:
            raise HTTPError(404, "Bench run not found")
        return row


# `python -m holdco.bench` imports this package before its __main__ can point HOLDCO_DATA at the bench's own data
# folder, and the routes would load holdco.config with the owner's folder first. While Python looks for that __main__,
# sys.argv[0] is "-m". The command line has no dashboard, so it needs no routes.
if sys.argv[:1] != ["-m"]:
    _register_routes()
