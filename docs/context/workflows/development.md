# Development workflow

## Set up and run

```
git clone https://github.com/JackL725/Holdco.git && cd Holdco
python run.py                     # http://127.0.0.1:8420 ; data in ./data (git-ignored)
python run.py --port 8431 --no-browser   # a second instance; give it its own data folder:
HOLDCO_DATA=/tmp/holdco-sandbox python run.py --port 8431 --no-browser
```

No installs: Python 3.10+ only. A model server is needed for real runs (Ollama with `gpt-oss:20b` by default); to
click around without one, run `python tests/fake_ollama.py 11999` and set the server address in Settings to
`http://127.0.0.1:11999`.

Environment variables that matter while developing: `HOLDCO_DATA` (data folder), `HOLDCO_PORT`, `HOLDCO_HOST` with
`HOLDCO_PASSWORD` (required beyond localhost), `HOLDCO_TICK_SECONDS` and `HOLDCO_ROUTINE_CHECK_SECONDS` (speed the
scheduler up), `HOLDCO_SANDBOX=off`, `HOLDCO_VAULT_BACKEND=file`, and the test switches `HOLDCO_ALLOW_LOCAL`
(allow 127.0.0.1 fakes and test-only API bases) and `HOLDCO_TEST_LOCAL_NOW` (fix the local clock for routines).

**Never** point a development run, a test or a bench at the owner's `data/` folder or at port 8420 while the owner's
Holdco is running.

## Test

```
python -m unittest discover -s tests -t .            # everything, about 2.5 minutes, no network
python -m unittest tests.test_phase2_brain -v        # one module
python -m unittest tests.test_e2e.EndToEnd.test_31_rhythms_and_briefs   # one end-to-end test
```

- Run the full suite on Python 3.10, 3.12 and 3.13 before pushing (CI runs Windows and Linux for each).
  With `uv`: `uv python install 3.10 3.13`, then call those interpreters directly.
- Suites by area: `test_unit.py` (checks, tools, presets, safety basics), `test_phase0_*` to `test_phase4_*` per
  phase and module, `*_review.py` (defects found by reviews), `*_seams.py` (integration fixes), `*_realmodel.py`
  (replays of real-model runs), `test_e2e.py` (a real server subprocess with fakes, numbered tests 01 onward;
  new ones continue the numbering).
- Fakes: `tests/fake_ollama.py` (scripted model, bench scripts, lazy mode, embeddings, a SearXNG stand-in at
  `/search`, pages at `/pages/<name>`), `holdco/bench/mocks.py` (Stripe, Telegram, MCP, search, fetch), and
  `tests/fake_<service>.py` for the rest.
- Rules that keep CI green on Windows:
  - dates in UTC (`db.now()`), never `date.today()` in assertions about stored dates;
  - `time.perf_counter` for short durations (Windows' monotonic clock ticks in about 16 ms);
  - generous timeouts in end-to-end tests (Windows runners are about three times slower; founding can take 120 s);
  - no thread attributes named `_handle` or `_stop`; normalise CRLF when reading text; no `resolve()` on files that
    may vanish;
  - a test that patches something background threads also use must fire only on its own thread;
  - an end-to-end test that changes global state (kill switch, settings) restores it in `addCleanup`;
  - tests archive or delete the companies they create, so scheduler-wide checks elsewhere never see them.

## Check with a real model

For behaviour the scripted model cannot show: start a sandbox Holdco on a spare port (8431-8433) with a scratch
`HOLDCO_DATA`, point it at the owner's Ollama, and drive it through the API (see the Phase 2 to 4 pull requests for
the scripts used). Or score the model: `python -m holdco.bench --model gpt-oss:20b [--only name ...]`. One triage
pass with traces, general fixes with replay tests, at most one confirming full run; never tune to one run's score.

## Change something

- **Read first:** the component file in `docs/context/components/`, the module docstring, and the phase spec section
  it names.
- **Add a tool:** `@tool(...)` with a risk tag in the owning module, belts in `presets.py` (caps: executors 18, CEO
  24), short description and actionable `ERROR:` texts, tests for the happy path and each refusal.
- **Add an outbound capability:** go through `actions.gate`; test that it queues by default, is refused under `off`,
  asks when tainted, and is audited.
- **Add a connector:** `conn_<name>.py` with `DEFINITIONS`, listed in `connectors.MODULES`; secrets as secret fields
  (vault); health, tools with risk tags, metrics with `source`; `tests/fake_<name>.py`; card fields in `app.js`
  only if the generic card is not enough.
- **Add a setting or column:** `config.py` defaults, `db.MIGRATIONS` for columns, coercion in
  `server.api_company_patch`, the field in `app.js`.
- **Change a prompt or preset:** run `test_unit.TestPresets`, the bench tests (`tests/test_phase3_bench.py`) and,
  for anything a small model reads, a real-model check.
- **Dashboard:** plain JS in `holdco/static/app.js`; escape with `esc()`; check with
  `node --check holdco/static/app.js`; test at phone width.

## Ship

- One pull request per phase or topic, against `main`. Phase work follows ADR-006: scaffold, parallel builders on
  disjoint files, integration, UI, independent review, real-model check.
- Update the phase spec's Status line and record deviations at the top of the spec; update `README.md` for anything
  the owner sees; update `docs/context/` when a module's responsibilities, an extension point or a rule changes.
- Commit messages: what changed and why, in plain English; end with the co-author line the session specifies.
- Watch CI (`gh pr checks <n> --watch`); a Windows-only failure is usually timing (see the rules above).
