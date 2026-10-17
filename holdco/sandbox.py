"""The code sandbox (Phase 3, docs/phases/phase-3-verify.md section 4): run_code inside a Docker or Podman container
with no network, a memory and process cap and a read-only root, the company folder mounted at /work.

    docker run --rm --name holdco-<company id>-<ms> -v <company folder>:/work -w /work --network none
      --memory 1g --cpus 1 --pids-limit 256 --read-only --tmpfs /tmp
      python:3.12-slim python /work/.runs/run_<ms>.py          (or: sh -c "<code>" for shell)

`--network none` is left out when the request allows the network (network=true in the task or the approval). The
script is written into the company's .runs/ folder as before, so it is inside the mount. A run that outlives its
timeout is killed by the container's name, because killing the docker client alone leaves the container running.

Without a runtime, actions.run_code keeps the old subprocess path and its result says "Sandbox: none (ran on this
PC)"; the Doctor shows a warning row. Every process this module starts goes through RUNNER (subprocess.run's
signature) and WHICH (shutil.which's), so the tests can fake Docker: CI never needs it installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from . import workspace

IMAGE = "python:3.12-slim"
CACHE_SECONDS = 600          # available() asks the runtime at most once per 10 minutes
INFO_TIMEOUT = 10            # seconds for `<runtime> info`
PULL_TIMEOUT = 900           # the first download of the image
KILL_TIMEOUT = 30
NONE = "Sandbox: none (ran on this PC)"   # the first line of an unsandboxed run_code result (actions.run_code)
# Windows only: no console window flashes up for each docker call when Holdco was started hidden.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

RUNNER = subprocess.run      # injectable: the tests replace these two with fakes
WHICH = shutil.which

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None}
_image_ready: set = set()    # runtimes that already have IMAGE, so the check is not repeated for every run


def _disabled() -> bool:
    """HOLDCO_SANDBOX=off turns detection off (tests and diagnostics); run_code then says it ran on this PC."""
    return os.environ.get("HOLDCO_SANDBOX", "").strip().lower() in ("off", "none", "0", "false", "no")


def _usable(runtime: str, path: str) -> bool:
    """`<runtime> info` answers and the engine runs Linux containers. Docker Desktop switched to Windows containers
    answers too, but cannot run the python:3.12-slim image."""
    fmt = "{{.OSType}}" if runtime == "docker" else "{{.Host.OS}}"
    try:
        proc = RUNNER([path, "info", "--format", fmt], capture_output=True, timeout=INFO_TIMEOUT,
                      stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return "linux" in _text(proc.stdout).lower()


def available(refresh: bool = False) -> str | None:
    """'docker' or 'podman' when the binary exists and answers `info` (cached for 10 minutes), else None."""
    if _disabled():
        return None
    with _lock:
        if not refresh and _cache["at"] and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["value"]
    found = None
    for runtime in ("docker", "podman"):   # Docker first; Podman when Docker is missing or not running
        path = WHICH(runtime)
        if path and _usable(runtime, path):
            found = runtime
            break
    with _lock:
        _cache.update(at=time.monotonic(), value=found)
    return found


def reset() -> None:
    """Forget the cached detection (tests; and after the owner installs Docker, the next check sees it)."""
    with _lock:
        _cache.update(at=0.0, value=None)
        _image_ready.clear()


def host_path(folder: str, windows: bool | None = None) -> str:
    """The company folder in the form the runtime's -v accepts. On Windows that is the drive form with forward
    slashes (C:/Users/me/Holdco/data/companies/acme), which Docker Desktop and Podman both accept; a backslash path
    is easy to mangle and a /c/... path is a Git Bash convention the plain client does not use."""
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        return folder
    p = folder.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def command(runtime: str, company_id: int, folder: str, ms: int, language: str = "python", code: str = "",
            network: bool = False, windows: bool | None = None) -> list[str]:
    """The container command line for one run. The python script must already be in <folder>/.runs/run_<ms>.py;
    shell code is passed to sh -c as it is."""
    cmd = [runtime, "run", "--rm", "--name", f"holdco-{company_id}-{ms}",
           "-v", f"{host_path(folder, windows)}:/work", "-w", "/work"]
    if not network:
        cmd += ["--network", "none"]
    cmd += ["--memory", "1g", "--cpus", "1", "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp", IMAGE]
    if language in ("python", "py"):
        cmd += ["python", f"/work/.runs/run_{ms}.py"]
    else:
        cmd += ["sh", "-c", code]
    return cmd


def _text(b) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace")
    return str(b or "")


def _ensure_image(runtime: str) -> tuple[bool, str]:
    """(ready, note). The first run downloads the image, which can take a minute; the result says so."""
    if runtime in _image_ready:
        return True, ""
    try:
        have = RUNNER([runtime, "image", "inspect", IMAGE], capture_output=True, timeout=60,
                      stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        if have.returncode == 0:
            _image_ready.add(runtime)
            return True, ""
        pulled = RUNNER([runtime, "pull", IMAGE], capture_output=True, timeout=PULL_TIMEOUT,
                        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, f"Downloading {IMAGE} took longer than {PULL_TIMEOUT // 60} minutes."
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{runtime} could not be started: {e}"
    if pulled.returncode != 0:
        err = " ".join((_text(pulled.stderr) or _text(pulled.stdout)).split())[-300:]
        return False, f"Downloading {IMAGE} failed: {err or f'exit code {pulled.returncode}'}"
    _image_ready.add(runtime)
    return True, f"Downloaded the {IMAGE} image first (a one-time step that can take a minute)."


def _kill(runtime: str, name: str) -> None:
    try:
        RUNNER([runtime, "kill", name], capture_output=True, timeout=KILL_TIMEOUT, stdin=subprocess.DEVNULL,
               creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass   # --rm removes it once it stops; a container that will not die is the runtime's problem


def run(company: dict, language: str, code: str, timeout: int = 60, network: bool = False) -> str | None:
    """The run_code result from inside the container, or None to fall back to the unsandboxed path."""
    runtime = available()
    if not runtime:
        return None
    timeout = max(5, min(int(timeout or 60), 600))
    lang = "python" if (language or "python").lower().startswith("py") else "shell"
    ws = workspace.root(company)
    runs = ws / ".runs"
    if runs.is_symlink():
        # Code in an earlier container can turn .runs into a link to any folder of this PC (the mount shows the host
        # path); Holdco writes the next script here on the host, so the link is removed, never followed.
        runs.unlink()
    runs.mkdir(parents=True, exist_ok=True)
    head = f"Sandbox: {runtime}" + (" (network allowed)" if network else "")
    ready, note = _ensure_image(runtime)
    if not ready:
        return (f"{head}\n{note} Nothing ran. Check that {runtime} is running and can reach the internet once, "
                f"then run it again.")
    ms = int(time.time() * 1000)
    if lang == "python":
        while True:
            try:   # "x": a name that already exists (a link planted from inside a container) is never written through
                with open(runs / f"run_{ms}.py", "x", encoding="utf-8", newline="\n") as f:
                    f.write(code)
                break
            except FileExistsError:
                ms += 1
    cmd = command(runtime, company["id"], os.path.abspath(str(ws)), ms, lang, code, network)
    name = f"holdco-{company['id']}-{ms}"
    prefix = head + (f"\n{note}" if note else "")
    try:
        proc = RUNNER(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                      creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        _kill(runtime, name)
        return f"{prefix}\nTimed out after {timeout}s; the container was stopped."
    except (OSError, subprocess.SubprocessError) as e:
        return f"{prefix}\nCouldn't start the container: {e}"
    out, err = _text(proc.stdout), _text(proc.stderr)
    res = f"{prefix}\nExit code {proc.returncode}\n"
    if out:
        res += f"--- stdout ---\n{out[-6000:]}\n"
    if err:
        res += f"--- stderr ---\n{err[-3000:]}\n"
    return res
