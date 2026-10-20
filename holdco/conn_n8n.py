"""The n8n connector (Phase 4, docs/phases/phase-4-scale.md section 3): the catch-all for services without a
connector of their own.

n8n (the webhook): one Webhook node in the owner's n8n, protected by a header check. n8n__trigger(workflow,
payload) posts {"workflow", "payload", "company", "company_id", "approval_id", "sent_at"} to it with the secret in
the X-Holdco-Secret header; the workflow routes on "workflow" (a Switch node). It is tagged publish, so it goes
through connector_publish and asks the owner until they or the trust ladder say otherwise. The health check posts
{"ping": true} and wants a 2xx.

n8n_mcp (optional): n8n's MCP server as a hosted MCP connector. Holdco has no classification map for the tools an
owner exposes there, so every one of them is unclassified and asks like a publish (connector_publish); every role
may load them. It is a card of its own, like Stripe's hosted MCP server, because one card has one kind.

Both are global: one n8n serves every company, and each trigger names the company it is for.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30
HEALTH_TIMEOUT = 15
SECRET_HEADER = "X-Holdco-Secret"
MAX_ANSWER = 2000        # characters of the workflow's answer handed back to the agent

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # n8n on this PC skips any proxy
_proxied = urllib.request.build_opener()


class N8nError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _cfg(inst: dict | None) -> dict:
    c = (inst or {}).get("config") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def _secret(inst: dict | None, field: str) -> str:
    if not inst:
        return ""
    from . import connectors   # imported here: connectors imports the conn_* modules
    return str((connectors.secrets(inst) or {}).get(field) or "").strip()


def webhook_url(inst: dict | None) -> str:
    return str(_cfg(inst).get("webhook_url") or "").strip()


def _scrub(text: str, secret: str = "") -> str:
    text = str(text or "")
    return text.replace(secret, "[secret]") if secret and len(secret) >= 6 else text


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _post(inst: dict, body: dict, timeout: float = TIMEOUT) -> tuple[int, str]:
    """POST the body to the webhook with the secret header. Returns (status, answer text)."""
    url = webhook_url(inst)
    if not url:
        raise N8nError("No webhook address is set. Paste the Webhook node's Production URL on the n8n card.")
    if not re.match(r"^https?://", url, re.I):
        raise N8nError("The webhook address must start with http:// or https://")
    secret = _secret(inst, "webhook_secret")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Holdco"}
    if secret:
        headers[SECRET_HEADER] = secret
    req = urllib.request.Request(url, data=json.dumps(body, default=str).encode(), method="POST", headers=headers)
    opener = _direct if _is_loopback(url) else _proxied
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(200_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(20_000)
    except urllib.error.URLError as e:
        raise N8nError(_scrub(f"Cannot reach n8n: {e.reason}", secret)) from None
    except (TimeoutError, socket.timeout):
        raise N8nError("n8n did not answer in time.") from None
    except (OSError, ValueError) as e:
        raise N8nError(_scrub(f"Cannot reach n8n: {e}", secret)) from None
    return status, _scrub(raw.decode("utf-8", "replace"), secret)


def _payload(value):
    """The payload as the model sent it: an object, or a JSON text of one; any other text goes as {"text": ...}."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"text": value}
        return parsed
    return value


def trigger(inst: dict, company: dict | None, workflow: str, payload=None, approval_id: int | None = None) -> str:
    name = " ".join(str(workflow or "").split())[:120]
    if not name:
        raise ValueError("workflow is required: the name the n8n workflow routes on, e.g. 'add_to_crm'")
    body = {"workflow": name, "payload": _payload(payload),
            "company": (company or {}).get("name") or "", "company_id": (company or {}).get("id"),
            "approval_id": approval_id,   # the same on a retried approval, so the workflow can skip a repeat
            "sent_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status, text = _post(inst, body)
    if status >= 300:
        raise N8nError(f"The n8n webhook answered HTTP {status}: {' '.join(text.split())[:300]}", status)
    answer = text.strip()
    if len(answer) > MAX_ANSWER:
        answer = answer[:MAX_ANSWER] + " [cut]"
    return f"n8n ran '{name}' (HTTP {status}). Its answer: {answer or '(empty)'}"


TOOLS = [
    {"name": "trigger", "tag": "publish", "fn": trigger, "takes_approval_id": True,
     "description": "Start a workflow in the owner's n8n (the catch-all for services without a connector: CRMs, "
                    "spreadsheets, chat). Name the workflow and pass its data. Waits for the owner's approval "
                    "unless they allow it.",
     "parameters": {"type": "object", "properties": {
         "workflow": {"type": "string", "description": "The workflow's name as the owner set it up in n8n"},
         "payload": {"type": "object", "description": "The data the workflow needs, as an object"}},
         "required": ["workflow"]}},
]


def tools(inst: dict | None) -> list[dict]:
    return [dict(t) for t in TOOLS]


def health(inst: dict) -> tuple[str, str]:
    if not webhook_url(inst):
        return "bad", "No webhook address: paste the Webhook node's Production URL"
    try:
        status, text = _post(inst, {"ping": True}, timeout=HEALTH_TIMEOUT)
    except N8nError as e:
        return "bad", str(e)[:300]
    if not 200 <= status < 300:
        if status in (401, 403):
            return "bad", f"the webhook refused the secret (HTTP {status}); check the Header Auth credential"
        if status == 404:
            return "bad", "the webhook is not listening (HTTP 404): activate the workflow and use its Production URL"
        return "bad", f"the webhook answered HTTP {status}: {' '.join(text.split())[:200]}"
    if not _secret(inst, "webhook_secret"):
        return "warn", (f"the webhook answered HTTP {status}, but no secret is set: anyone who learns the address "
                        "can start it")
    return "ok", f"the webhook answered HTTP {status}"


DEFINITION = {
    "label": "n8n",
    "category": "Automation",
    "kind": "api",
    "scope": "global",
    "setup": {
        "where_to_get_key": (
            "In n8n, create a workflow that starts with a Webhook node (method POST). Set its Authentication to "
            f"Header Auth with a new credential: name {SECRET_HEADER}, value a long random secret, and paste the "
            "same secret here. Route on the body's \"workflow\" field (a Switch node), answer {\"ping\": true} "
            "with any 2xx, activate the workflow and paste its Production URL here. This is the catch-all for "
            "services without a connector of their own."),
        "permissions": ["Only what the workflows you wire up can do: agents reach nothing else through n8n"],
        "fields": [
            {"key": "webhook_url", "label": "Webhook address", "secret": False,
             "hint": "The Webhook node's Production URL", "placeholder": "http://localhost:5678/webhook/holdco"},
            {"key": "webhook_secret", "label": "Webhook secret", "secret": True,
             "hint": f"Sent in the {SECRET_HEADER} header; the same value as the Header Auth credential",
             "placeholder": "a long random secret"},
        ],
        "test_mode": ("n8n has no test mode. Every trigger asks you (connector_publish) until you or the trust "
                      "ladder allow it; point the webhook at a test workflow while you try it."),
        "docs_url": "https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/",
        "agents_can": ["Start a workflow you wired up in n8n, with your approval until trust promotes it"],
    },
    "health": health,
    "tools": tools,
}


# ---------------------------------------------------------------- n8n's MCP server (optional)

def mcp_url(inst: dict) -> str:
    return str(_cfg(inst).get("mcp_url") or "").strip()


def mcp_headers(inst: dict) -> dict:
    token = _secret(inst, "mcp_token")
    return {"Authorization": f"Bearer {token}"} if token else {}


def mcp_health(inst: dict) -> tuple[str, str]:
    if not mcp_url(inst):
        return "bad", "No server address: paste n8n's MCP server URL"
    from . import connectors, mcp   # imported here: only this card needs the MCP client
    try:
        listed = connectors.client(inst).list_tools() or []
    except mcp.AuthRequired:
        return "bad", "n8n asked for sign-in: paste the MCP access token from n8n's settings"
    except Exception as e:
        return "bad", _scrub(connectors._error_text(e), _secret(inst, "mcp_token"))[:300]
    n = len(listed)
    return "ok", f"{n} tool{'s' if n != 1 else ''} available; each one asks you before it runs"


MCP_DEFINITION = {
    "label": "n8n MCP server",
    "category": "Automation",
    "kind": "mcp-http",
    "scope": "global",
    "oauth": False,                   # a token from n8n's settings, not a sign-in
    "setup": {
        "where_to_get_key": ("Optional, beside the webhook: in n8n turn on its MCP server (instance-level MCP "
                             "access, or a workflow with an MCP Server Trigger node), copy the server URL and its "
                             "access token, and paste them here."),
        "permissions": ["Expose only the workflows agents may use"],
        "fields": [
            {"key": "mcp_url", "label": "MCP server address", "secret": False,
             "hint": "The server URL n8n shows", "placeholder": "http://localhost:5678/mcp-server/http"},
            {"key": "mcp_token", "label": "Access token", "secret": True,
             "hint": "Sent as a Bearer token", "placeholder": "n8n MCP token"},
        ],
        "test_mode": "None. Holdco has no rule for these tools, so every call asks you first (connector_publish).",
        "docs_url": "https://docs.n8n.io/advanced-ai/accessing-n8n-mcp-server/",
        "agents_can": ["Use the workflows your n8n MCP server exposes: every call asks you first"],
    },
    "health": mcp_health,
    "mcp_url": mcp_url,
    "mcp_headers": mcp_headers,
    # No classification map: every tool is unclassified and asks like a publish; every role may load them.
    "mcp": {"url": "", "headers": {}, "classify": [], "allow": {"*": ["*"]}, "blocked": []},
}

DEFINITIONS = {"n8n": DEFINITION, "n8n_mcp": MCP_DEFINITION}
