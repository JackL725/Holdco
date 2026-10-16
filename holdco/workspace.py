"""Each company gets its own folder on disk. Agents can't read or write outside it."""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from . import config

SKIP = {".trash", ".runs", ".cache", ".qa", "__pycache__", "node_modules", ".git"}   # .cache: fetched pages (truth.py)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    return s[:40] or "company"


def root(company: dict) -> Path:
    return config.COMPANIES_DIR / company["slug"]


def create(company: dict) -> Path:
    r = root(company)
    for d in config.WORKSPACE_DIRS:
        (r / d).mkdir(parents=True, exist_ok=True)
    return r


class PathError(Exception):
    pass


def safe_path(company: dict, rel: str) -> Path:
    base = root(company).resolve()
    rel = (rel or ".").strip().replace("\\", "/")
    rel = re.sub(r"^(\./)+", "", rel).lstrip("/")
    if re.match(r"^[a-zA-Z]:", rel):
        raise PathError("Use paths relative to the company folder, e.g. site/index.html")
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise PathError("That path is outside the company folder.")
    return p


def rel(company: dict, p: Path) -> str:
    base = root(company).resolve()
    try:
        return p.resolve().relative_to(base).as_posix()
    except ValueError:
        # Windows: resolving a file that vanished a moment ago (an atomic rewrite's temp file, for example) can
        # return a \\?\ path that no longer matches the folder; the plain absolute path still does.
        return Path(os.path.abspath(p)).relative_to(Path(os.path.abspath(base))).as_posix()


def tree(company: dict, sub: str = ".", depth: int = 3, limit: int = 200) -> list[dict]:
    start = safe_path(company, sub)
    out: list[dict] = []
    if not start.exists():
        return out

    def walk(d: Path, level: int):
        if len(out) >= limit or level > depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in SKIP:
                continue
            if len(out) >= limit:
                return
            try:
                st = e.stat()
                path = rel(company, e)
            except (OSError, ValueError):   # gone or renamed while we looked (records are rewritten atomically)
                continue
            out.append({"path": path, "dir": e.is_dir(), "size": st.st_size if e.is_file() else 0,
                        "modified": st.st_mtime})
            if e.is_dir():
                walk(e, level + 1)

    walk(start, 1)
    return out


def tree_text(company: dict, limit: int = 60) -> str:
    items = [i for i in tree(company, ".", 3, 400) if not i["dir"]]
    if not items:
        return "(no files yet)"
    items.sort(key=lambda i: -i["modified"])
    lines = [f"- {i['path']} ({i['size']} bytes)" for i in items[:limit]]
    if len(items) > limit:
        lines.append(f"- ... and {len(items) - limit} more")
    return "\n".join(lines)


def trash(company: dict, p: Path) -> str:
    tdir = root(company) / ".trash" / time.strftime("%Y%m%d-%H%M%S")
    tdir.mkdir(parents=True, exist_ok=True)
    dest = tdir / p.name
    shutil.move(str(p), str(dest))
    return rel(company, dest)


def remove_company_folder(company: dict) -> None:
    src = root(company)
    if src.exists():
        config.TRASH_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(config.TRASH_DIR / f"{company['slug']}-{int(time.time())}"))


# ---------------- Phase 2: file leases (two agents never edit the same file at once)

def lease(company: dict, rel_path: str, ctx) -> str | None:
    """Take or renew this run's lease on a file before writing it; the refusal text when another live run holds it.
    Scaffold: no leases yet."""
    return None


def release(run_id: int) -> None:
    """Free every lease a run holds (agents._finalize)."""
    return None
