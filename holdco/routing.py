"""Route registry for the dashboard API, shared by server.py and the feature modules.

A feature module registers its own JSON routes with the decorator:

    from .routing import route, HTTPError, company_or_404

    @route("GET", r"/api/companies/(\\d+)/things")
    def api_things(h, m, body):
        c = company_or_404(m.group(1))
        return {"things": []}

server.py imports every feature module so the routes exist before the first request.
"""
from __future__ import annotations

import re

from . import db

ROUTES: list[tuple[str, re.Pattern, callable]] = []


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def to_int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def company_or_404(cid) -> dict:
    c = db.get_company(to_int(cid))
    if not c:
        raise HTTPError(404, "Company not found")
    return c
