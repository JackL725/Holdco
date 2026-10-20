"""The github connector (Phase 4, docs/phases/phase-4-scale.md section 3).

GitHub's official MCP server, in one of two modes chosen on the card (the instance's transport follows its mode,
through the definition's kind_for):
- hosted (mcp-http): GitHub's remote server, the fine-grained personal access token sent as a Bearer header;
- docker (mcp-stdio): the official image on this PC, started with the server's read-only flag. The token reaches the
  container through an environment variable, never the command line. Nobody can change anything in this mode.

The address, the image, the flag, the environment variable and the toolset header are named in the constants below
as the GitHub MCP server README described them when the plan was written; verify them against the vendor docs before
connecting.

Rules Holdco adds on top of the risk gate:
- Classification (first match wins): merge_pull_request and delete_* irreversible (locked to the owner's approval);
  get_*, list_*, search_* and the server's combined issue_read and pull_request_read read; the issue, comment, pull
  request, branch and push tools write (connector_write, so the trust ladder applies); anything else is unclassified
  and asks like a publish.
- Role allowlist: every role may load the read tools; engineering also the write tools and merge_pull_request (which
  always asks). No role gets a deletion tool or an unclassified one.
- Repositories: a call that is not read must name one of the repositories on the card (owner and repo); with none
  listed, agents can read but not change anything. Reads may look at any repository the token can see.
- Taint: issue, pull request, comment, commit, file and search results are someone else's text, so reading one
  taints the session (Phase 3).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil

# Verify these against the GitHub MCP server README before connecting.
HOSTED_URL = "https://api.githubcopilot.com/mcp/"         # the remote server
DOCKER_IMAGE = "ghcr.io/github/github-mcp-server"         # the official image (stdio)
READ_ONLY_FLAG = "--read-only"                            # the server's flag that leaves only the read tools
TOKEN_ENV = "GITHUB_PERSONAL_ACCESS_TOKEN"                # how the image reads the token
TOOLSETS = "context,repos,issues,pull_requests"           # the toolsets Holdco asks for (fewer tools to choose from)
TOOLSETS_ENV = "GITHUB_TOOLSETS"                          # ... for the image
TOOLSETS_HEADER = "X-MCP-Toolsets"                        # ... for the remote server
DOCKER_COMMAND = ["docker", "run", "-i", "--rm", "-e", TOKEN_ENV, "-e", TOOLSETS_ENV, DOCKER_IMAGE, "stdio",
                  READ_ONLY_FLAG]
MODES = ("hosted", "docker")
HEALTH_TIMEOUT = 120   # the first docker start may pull the image

READ_GLOBS = ["get_*", "list_*", "search_*", "issue_read", "pull_request_read"]
# The spec's write tools, plus the newer server's combined issue_write and update_pull_request (same kind of change).
WRITE_TOOLS = ["create_issue", "update_issue", "add_issue_comment", "create_pull_request", "create_branch",
               "push_files", "issue_write", "update_pull_request"]
IRREVERSIBLE_GLOBS = ["merge_pull_request", "delete_*"]
# Ordered (glob, tag); the first match wins, so the irreversible rules come first.
CLASSIFY = ([(g, "irreversible") for g in IRREVERSIBLE_GLOBS] + [(g, "read") for g in READ_GLOBS]
            + [(n, "write") for n in WRITE_TOOLS])
# Read tools for every role (the framework lets any role load a read tool); engineering also changes things. A merge
# is in engineering's belt because it always asks (irreversible is locked); deletions and unclassified tools are in
# nobody's.
ALLOW = {"*": list(READ_GLOBS), "engineering": list(WRITE_TOOLS) + ["merge_pull_request"]}
# Tools whose results are other people's text: reading one taints the session (Phase 3).
EXTERNAL = ["get_issue*", "list_issues", "list_sub_issues", "issue_read", "search_*", "get_pull_request*",
            "list_pull_requests", "pull_request_read", "*_comments", "get_file_contents", "list_commits",
            "get_commit", "*discussion*", "*notification*"]

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _cfg(inst: dict | None) -> dict:
    c = (inst or {}).get("config") or {}
    if isinstance(c, str):
        try:
            c = json.loads(c)
        except ValueError:
            c = {}
    return c if isinstance(c, dict) else {}


def _token(inst: dict | None) -> str:
    if not inst:
        return ""
    from . import connectors   # imported here: connectors imports the conn_* modules
    return str((connectors.secrets(inst) or {}).get("token") or "").strip()


def mode(inst: dict | None) -> str:
    """hosted or docker from the card's mode field (blank is hosted); anything else is returned as typed, and the
    health check names it."""
    m = str(_cfg(inst).get("mode") or "").strip().lower()
    if m in ("", "hosted", "remote", "http"):
        return "hosted"
    if m in ("docker", "stdio", "local"):
        return "docker"
    return m


def kind_for(inst: dict | None) -> str:
    return "mcp-stdio" if mode(inst) == "docker" else "mcp-http"


def repos(inst: dict | None) -> list[str]:
    """The repositories agents may change, as owner/name (a github.com address is accepted too)."""
    out = []
    for part in re.split(r"[\s,;]+", str(_cfg(inst).get("repos") or "")):
        part = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", part.strip(), flags=re.I)
        part = part.strip("/").removesuffix(".git")
        if _REPO_RE.match(part) and part.lower() not in {r.lower() for r in out}:
            out.append(part)
    return out


def classify(tool: str) -> str | None:
    for glob, tag in CLASSIFY:
        if fnmatch.fnmatchcase(tool, glob):
            return tag
    return None


# ---------------------------------------------------------------- the server's address or command line

def launch(inst: dict | None) -> tuple[str, list[str]]:
    """(command, args) for docker mode: the official image with the read-only flag. The hidden command field (tests
    only) replaces the whole line."""
    line = str(_cfg(inst).get("command") or "").strip()
    if not line:
        return DOCKER_COMMAND[0], list(DOCKER_COMMAND[1:])
    try:
        parts = shlex.split(line, posix=os.name != "nt")
    except ValueError:
        parts = line.split()
    parts = [p.strip('"') for p in parts] or list(DOCKER_COMMAND)
    return parts[0], parts[1:]


def mcp_url(inst: dict | None) -> str:
    return str(_cfg(inst).get("mcp_url") or "").strip() or HOSTED_URL


def mcp_headers(inst: dict | None) -> dict:
    token = _token(inst)
    return {"Authorization": f"Bearer {token}", TOOLSETS_HEADER: TOOLSETS} if token else {}


# ---------------------------------------------------------------- calls

def _instance() -> dict | None:
    """The GitHub instance (global: one per install)."""
    from . import connectors
    try:
        return connectors.instance("github", None)
    except Exception:
        return None


def _named_repo(args: dict | None) -> str:
    """owner/name from a call's arguments: owner and repo, or a repo given as owner/name."""
    args = args if isinstance(args, dict) else {}
    owner, repo = str(args.get("owner") or "").strip(), str(args.get("repo") or "").strip()
    if not owner and "/" in repo:
        owner, _, repo = repo.partition("/")
    return f"{owner}/{repo}" if owner and repo else ""


def refuse_call(tool: str, args: dict | None) -> str | None:
    """Checked by connectors.call before the risk gate: a change must name one of the card's repositories."""
    if classify(tool) == "read":
        return None
    allowed = repos(_instance())
    if not allowed:
        return ("No repositories are open to changes yet: the owner lists them under Repositories on the GitHub "
                "card. Reading works meanwhile.")
    named = _named_repo(args)
    if not named:
        return f"{tool} must name the repository (owner and repo)."
    if named.lower() not in {r.lower() for r in allowed}:
        return f"{named} is not a repository agents may change (allowed: {', '.join(allowed)})."
    return None


# ---------------------------------------------------------------- health

def health(inst: dict) -> tuple[str, str]:
    if not _token(inst):
        return "bad", "No token: paste a fine-grained personal access token limited to the company's repositories"
    m = mode(inst)
    if m not in MODES:
        return "bad", f"Mode must be hosted or docker, not '{m}'"
    if m == "docker":
        command, _ = launch(inst)
        if shutil.which(command) is None:
            return "bad", f"{command} not found: install Docker Desktop, or use the hosted mode"
    from . import connectors, mcp   # imported here: connectors imports the conn_* modules
    try:
        listed = connectors.client(inst).list_tools() or []
    except mcp.AuthRequired:
        return "bad", "GitHub refused the token: check that it has not expired and covers the repositories"
    except Exception as e:
        return "bad", f"GitHub MCP failed: {str(e)[-300:] or type(e).__name__}"
    names = [str(t.get("name")) for t in listed if isinstance(t, dict) and t.get("name")]
    reads = sum(1 for n in names if classify(n) == "read")
    writes = sum(1 for n in names if classify(n) == "write")
    allowed = repos(inst)
    where = (f"changes allowed in {', '.join(allowed)}" if allowed
             else "no repositories listed, so agents can read but not change anything")
    kind = "hosted" if m == "hosted" else "Docker, read only"
    return "ok", f"{len(names)} tools ({reads} read, {writes} write), {kind}; {where}"


def tools(inst: dict | None) -> list[dict]:
    return []   # an MCP connector: its tools come from the server's tools/list


DEFINITION = {
    "label": "GitHub",
    "category": "Code",
    "kind": "mcp-http",          # the default; each instance's mode picks its transport (kind_for)
    "kind_for": kind_for,
    "scope": "global",
    "oauth": False,              # the token is the sign-in
    "health_timeout": HEALTH_TIMEOUT,
    "setup": {
        "where_to_get_key": (
            "On GitHub: Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new "
            "token. Under Repository access choose Only select repositories and pick the company's repositories, "
            "then give it the permissions below. Choose hosted (GitHub's own MCP server) or docker (the official "
            "image on this PC, read only: run docker pull " + DOCKER_IMAGE + " once first)."),
        "permissions": ["Repository access: only the company's repositories",
                        "Contents: read and write", "Issues: read and write", "Pull requests: read and write",
                        "Metadata: read (always on)"],
        "fields": [
            {"key": "token", "label": "Fine-grained personal access token", "secret": True,
             "hint": "Limited to the company's repositories", "placeholder": "github_pat_..."},
            {"key": "mode", "label": "Mode", "secret": False,
             "hint": "hosted (engineering can file issues and open pull requests after your approval) or docker "
                     "(runs on this PC, read only)",
             "placeholder": "hosted"},
            {"key": "repos", "label": "Repositories agents may change", "secret": False,
             "hint": "owner/name, separated by commas. Agents read any repository the token can see; issues, pull "
                     "requests and pushes go only to these",
             "placeholder": "your-name/pawplan-site"},
            {"key": "mcp_url", "label": "Server address", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": HOSTED_URL},
            {"key": "command", "label": "Command", "secret": False, "hidden": True,
             "hint": "Only for tests", "placeholder": " ".join(DOCKER_COMMAND)},
        ],
        "test_mode": ("None at GitHub: point it at a test repository first. Changes ask you until the trust ladder "
                      "promotes them; merges and deletions always ask."),
        "docs_url": "https://github.com/github/github-mcp-server",
        "agents_can": ["Read repositories, issues and pull requests for research (every role)",
                       "Engineering: file issues, comment, create branches, push files and open pull requests in "
                       "the listed repositories, after your approval",
                       "Merges always ask you; no agent can delete anything",
                       "Issue and pull request text is someone else's writing: a session that read it asks you "
                       "before anything goes out"],
    },
    "health": health,
    "tools": tools,
    "external": list(EXTERNAL),
    "launch": launch,
    "mcp_url": mcp_url,
    "mcp_headers": mcp_headers,
    "mcp": {"url": HOSTED_URL, "headers": {}, "command": DOCKER_COMMAND[0], "args": list(DOCKER_COMMAND[1:]),
            "env": {TOKEN_ENV: "{token}", TOOLSETS_ENV: TOOLSETS},
            "classify": CLASSIFY, "allow": ALLOW, "blocked": []},
    "refuse_call": refuse_call,
}

# The definition this module provides, by key (connectors.DEFINITIONS merges it).
DEFINITIONS = {"github": DEFINITION}
