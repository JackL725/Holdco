"""The buffer connector (Phase 1, docs/phases/phase-1-connect.md section 7). BETA.

Buffer's GraphQL API (https://api.buffer.com, Bearer personal access token) is in beta. The query and mutation
texts below follow the shapes Buffer described for the beta and are placeholders until checked against Buffer's
API docs; they live in the QUERY_* constants so they can be corrected in one place. The card ships disabled by
default with a beta note.

Tools: list_channels (read) and create_post (publish). actions._social posts through Buffer when the company has a
healthy instance with a channel for the post's platform (post_for_platform); otherwise the webhook or the manual
link as before.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.buffer.com"
TIMEOUT = 30
HEALTH_TIMEOUT = 15

# Placeholders until verified against Buffer's docs (the API is in beta).
QUERY_ACCOUNT = "query HoldcoAccount { account { id email organizations { id name } } }"
QUERY_CHANNELS = ("query HoldcoChannels($input: ChannelsInput!) { channels(input: $input) "
                  "{ id name displayName service } }")
MUTATION_CREATE_POST = (
    "mutation HoldcoCreatePost($input: CreatePostInput!) { createPost(input: $input) { "
    "... on PostActionSuccess { post { id text dueAt } } ... on MutationError { message } } }")

# Platform names agents use, and the service names a channel may carry for them.
SERVICES = {"x": ("x", "twitter"), "twitter": ("x", "twitter"), "linkedin": ("linkedin",),
            "instagram": ("instagram",), "facebook": ("facebook",), "threads": ("threads",),
            "bluesky": ("bluesky",), "mastodon": ("mastodon",), "tiktok": ("tiktok",), "youtube": ("youtube",),
            "pinterest": ("pinterest",), "google": ("googlebusiness", "google"),
            "googlebusiness": ("googlebusiness", "google")}

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_proxied = urllib.request.build_opener()


class BufferError(RuntimeError):
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


def _key(inst: dict | None) -> str:
    if not inst:
        return ""
    from . import connectors   # imported here: connectors imports the conn_* modules
    return str((connectors.secrets(inst) or {}).get("api_key") or "").strip()


def _base(inst: dict | None) -> str:
    return (str(_cfg(inst).get("api_base") or "").strip() or API_BASE).rstrip("/")


def _ids(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r"[,\s]+", str(value or ""))
    return [str(x).strip() for x in items if str(x).strip()]


def default_channels(inst: dict | None) -> list[str]:
    return _ids(_cfg(inst).get("default_channels"))


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _gql(inst: dict, query: str, variables: dict | None = None, timeout: float = TIMEOUT) -> dict:
    """POST one GraphQL request; returns `data`. HTTP errors and GraphQL `errors` raise BufferError."""
    key = _key(inst)
    if not key:
        raise BufferError("No Buffer token is set. Add one on the Buffer card in the company's Settings.")
    url = _base(inst) + "/"
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "Holdco"})
    opener = _direct if _is_loopback(url) else _proxied
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(1_000_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(50_000)
    except urllib.error.URLError as e:
        raise BufferError(f"Cannot reach Buffer: {e.reason}") from None
    except (TimeoutError, socket.timeout):
        raise BufferError("Buffer did not answer in time.") from None
    except (OSError, ValueError) as e:
        raise BufferError(f"Cannot reach Buffer: {e}") from None
    text = raw.decode("utf-8", "replace").replace(key, "[key]")
    try:
        parsed = json.loads(text or "{}")
    except ValueError:
        parsed = {}
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if status >= 400 or errors:
        msgs = "; ".join(str(e.get("message") if isinstance(e, dict) else e) for e in (errors or [])[:3])
        raise BufferError(f"Buffer answered HTTP {status}: {msgs or text[:200]}", status)
    return (parsed.get("data") or {}) if isinstance(parsed, dict) else {}


def _account(inst: dict, timeout: float = TIMEOUT) -> dict:
    return _gql(inst, QUERY_ACCOUNT, timeout=timeout).get("account") or {}


def channels(inst: dict) -> list[dict]:
    """[{id, name, service}] for every organization on the account."""
    orgs = _account(inst).get("organizations") or [{}]
    out, seen = [], set()
    for org in orgs:
        variables = {"input": {"organizationId": org.get("id")}} if org.get("id") else {}
        for ch in _gql(inst, QUERY_CHANNELS, variables).get("channels") or []:
            cid = str(ch.get("id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append({"id": cid, "name": str(ch.get("displayName") or ch.get("name") or ""),
                        "service": str(ch.get("service") or "").lower()})
    return out


def _service_matches(platform: str, service: str) -> bool:
    p = (platform or "").strip().lower()
    return (service or "").lower() in SERVICES.get(p, (p,))


def _post_one(inst: dict, channel_id: str, text: str, schedule: str) -> str:
    post_input = {"channelId": channel_id, "text": text, "schedulingType": "automatic",
                  "mode": "customScheduled" if schedule else "addToQueue"}
    if schedule:
        post_input["dueAt"] = schedule
    result = _gql(inst, MUTATION_CREATE_POST, {"input": post_input}).get("createPost") or {}
    if result.get("message") and not result.get("post"):
        raise BufferError(f"Buffer refused the post for channel {channel_id}: {result['message']}")
    return str((result.get("post") or {}).get("id") or "?")


def _text_with_link(text: str, link: str) -> str:
    text, link = (text or "").strip(), (link or "").strip()
    return f"{text}\n\n{link}" if link and link not in text else text


# ---------------------------------------------------------------- tools

def list_channels(inst: dict, company: dict) -> str:
    rows = channels(inst)
    if not rows:
        return "No channels connected in Buffer yet. The owner connects them in Buffer itself."
    defaults = set(default_channels(inst))
    lines = [f"{len(rows)} Buffer channel(s):"]
    for ch in rows:
        mark = " (default)" if ch["id"] in defaults else ""
        lines.append(f"- {ch['id']}: {ch['service'] or '?'} {ch['name']}{mark}")
    return "\n".join(lines)


def create_post(inst: dict, company: dict, text: str, channel_ids: str = "", link: str = "",
                schedule: str = "") -> str:
    body = _text_with_link(text, link)
    if not body:
        raise ValueError("text is required")
    ids = _ids(channel_ids) or default_channels(inst)
    if not ids:
        raise ValueError("No channel: pass channel_ids (see list_channels) or set default channels on the card")
    schedule = (schedule or "").strip()
    if schedule and not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", schedule):
        raise ValueError("schedule must be an ISO time such as 2026-10-20T15:00:00Z, or empty to add to the queue")
    done = [f"{cid} (post {_post_one(inst, cid, body, schedule)})" for cid in ids]
    when = f"scheduled for {schedule}" if schedule else "added to the queue"
    return f"Buffer: {when} on {', '.join(done)}."


def post_for_platform(inst: dict, company: dict, platform: str, content: str, link: str = "") -> str | None:
    """actions._social: post through the channels whose service matches the platform (the default channels first
    when any of them match). None when no channel matches, so the caller falls back to the webhook or the link."""
    rows = channels(inst)
    matching = [c for c in rows if _service_matches(platform, c["service"])]
    if not matching:
        return None
    defaults = set(default_channels(inst))
    preferred = [c for c in matching if c["id"] in defaults] or matching
    body = _text_with_link(content, link)
    done = [f"{c['service']} {c['name'] or c['id']} (post {_post_one(inst, c['id'], body, '')})" for c in preferred]
    return f"Added to the Buffer queue: {', '.join(done)}."


TOOLS = [
    {"name": "list_channels", "tag": "read", "fn": list_channels,
     "description": "List the social channels connected in Buffer: id, service (x, linkedin, ...) and name.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "create_post", "tag": "publish", "fn": create_post,
     "description": "Queue or schedule a social post in Buffer on one or more channels.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string", "description": "The post text"},
         "channel_ids": {"type": "string", "description": "Comma-separated channel ids (default: the card's "
                                                          "default channels)"},
         "link": {"type": "string", "description": "Optional URL to add"},
         "schedule": {"type": "string", "description": "ISO time to publish at; empty adds it to the queue"}},
         "required": ["text"]}},
]


def tools(inst: dict | None) -> list[dict]:
    return [dict(t) for t in TOOLS]


def health(inst: dict) -> tuple[str, str]:
    if not _key(inst):
        return "bad", "No token: paste a Buffer personal access token"
    try:
        account = _account(inst, timeout=HEALTH_TIMEOUT)
    except BufferError as e:
        if e.status in (401, 403):
            return "bad", "token rejected"
        return "bad", str(e)[:300]
    if not account:
        return "warn", "Buffer answered without account details (the beta API may have changed)"
    who = account.get("email") or account.get("id") or "account"
    return "ok", f"signed in as {who} (beta API)"


DEFINITION = {
    "label": "Buffer (beta)",
    "category": "Social",
    "kind": "api",
    "scope": "company",
    "beta": True,
    "enabled_by_default": False,   # saved disabled until the owner has pressed Test and turned it on
    "setup": {
        "where_to_get_key": ("In Buffer, open the developer settings and create a personal access token. Connect "
                             "the company's social accounts as channels in Buffer itself first."),
        "permissions": ["Read channels", "Create posts"],
        "fields": [
            {"key": "api_key", "label": "Personal access token", "secret": True, "hint": "From Buffer's developer "
             "settings", "placeholder": ""},
            {"key": "default_channels", "label": "Default channels", "secret": False,
             "hint": "Comma-separated channel ids (Test lists them); blank posts to every channel of the platform",
             "placeholder": "id1, id2"},
            {"key": "api_base", "label": "API address", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": API_BASE},
        ],
        "note": ("Beta: Buffer's GraphQL API is in beta and Holdco's queries are not verified against it yet. "
                 "The card is saved turned off; press Test, then turn it on."),
        "test_mode": "Buffer has no test mode. Keep the social_post policy on approve.",
        "docs_url": "https://developers.buffer.com/",
        "agents_can": ["Queue the social posts you approve in Buffer (social_post)",
                       "List channels and queue or schedule posts (each post asks you first)"],
    },
    "health": health,
    "tools": tools,
}
