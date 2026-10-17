"""The brave connector (Phase 1, docs/phases/phase-1-connect.md section 7).

Brave Search is one provider in web.py's search chain (Phase 0); this card only gives its key a home with a Test
button. The key stays the global setting brave_api_key (kept in the vault as settings.brave_api_key), so the field
carries "setting": the framework saves it there instead of as a connector secret. Health is one real search through
web._brave, and its outcome is recorded for the Doctor's search row too. No tools of its own.
"""
from __future__ import annotations

TEST_QUERY = "small business software pricing"


def _key(inst: dict | None) -> str:
    from . import db
    key = str(db.get_settings().get("brave_api_key") or "").strip()
    if key or not inst:
        return key
    try:   # a framework that stored the field as a connector secret instead
        from . import connectors
        s = connectors.secrets(inst) or {}
        return str(s.get("brave_api_key") or s.get("api_key") or "").strip()
    except Exception:
        return ""


def health(inst: dict | None) -> tuple[str, str]:
    from . import web
    key = _key(inst)
    if not key:
        return "warn", "No key: paste a Brave Search API key, or leave search on the other providers"
    try:
        rows = web._brave(TEST_QUERY, 1, {"brave_api_key": key})
    except web.ProviderError as e:
        web._record("brave", False, str(e))
        return "bad", f"Brave search failed: {e}"
    except Exception as e:   # a bug must not break the card
        return "bad", f"Brave search failed: {type(e).__name__}: {str(e)[:200]}"
    web._record("brave", True)
    return "ok", f"Brave answered with {len(rows)} result(s)"


def tools(inst: dict | None) -> list[dict]:
    return []


DEFINITION = {
    "label": "Brave Search",
    "category": "Search",
    "kind": "builtin",
    "scope": "global",
    "setup": {
        "where_to_get_key": ("Sign up at the Brave Search API dashboard, choose a plan (the free plan has a monthly "
                             "limit) and copy the subscription token. Then set Search provider to brave in "
                             "Settings to use it first."),
        "permissions": [],
        "fields": [
            {"key": "brave_api_key", "label": "API key", "secret": True, "setting": "brave_api_key",
             "hint": "The subscription token from the Brave Search API dashboard", "placeholder": "BSA..."},
        ],
        "test_mode": "Not applicable.",
        "docs_url": "https://api-dashboard.search.brave.com/",
        "agents_can": ["Search the web through Brave (web_search), with the other providers as fallback"],
    },
    "health": health,
    "tools": tools,
}
