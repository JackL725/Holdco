"""MCP client, standard library only (Phase 1, docs/phases/phase-1-connect.md section 2).

Speaks the 2026-07-28 stateless generation (server/discover, namespaced _meta keys, Mcp-Method and Mcp-Name
headers) and falls back to the 2025-11-25 initialize handshake, over stdio (a child process) or Streamable HTTP.

    client = mcp.Client("playwright", "stdio", command="npx", args=["-y", "@playwright/mcp@0.0.83"]).connect()
    tools = client.list_tools()
    text = mcp.text_of(client.call_tool("browser_navigate", {"url": "https://example.com"}))
    client.close()

Every failure is an MCPError (AuthRequired when an HTTP server wants a sign-in). The connector framework is the
only caller.
"""
from __future__ import annotations

import base64
import codecs
import itertools
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client

from . import __version__

# Protocol generations. The current one is stateless; the older one starts with an initialize handshake.
CURRENT = "2026-07-28"
FALLBACK = "2025-11-25"                                   # what Holdco asks for on initialize
OLDER_ACCEPTED = ("2025-11-25", "2025-06-18", "2025-03-26")

# The namespaced _meta keys every current-generation request carries, and the one results carry.
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"

UNSUPPORTED_VERSION = -32022                              # JSON-RPC error: the server does not speak our version
# server/discover errors that mean "this server is from the older generation": method not found, invalid request,
# and invalid params (what the Python SDK answers for a method it does not know).
FALLBACK_CODES = (-32601, -32600, -32602)

# Seconds. Module constants so tests can patch them.
CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
KILL_AFTER = 5.0           # close(): seconds between terminate and kill
STDERR_KEEP = 4000         # characters of a stdio server's stderr kept for error messages
MAX_REPLY = 5_000_000      # bytes: the most an HTTP reply, or one event of a reply stream, may hold
READ_CHUNK = 65536         # bytes per read, so the call's deadline is checked while a reply arrives
ERROR_BODY_KEEP = 65536    # bytes of an HTTP error's body read for its message
# Holdco's own settings (HOLDCO_VAULT_PASSPHRASE, HOLDCO_PASSWORD, ...) and the model server's never reach a stdio
# server's environment, unless its definition sets one on purpose.
PRIVATE_ENV_PREFIXES = ("HOLDCO", "OLLAMA")

INPUT_REQUIRED = "the server needs input Holdco cannot provide"
CLIENT_INFO = {"name": "holdco", "version": __version__}


class MCPError(Exception):
    pass


class AuthRequired(MCPError):
    def __init__(self, resource_metadata_url: str, scope: str | None = None):
        super().__init__(f"authorization required ({resource_metadata_url})")
        self.resource_metadata_url = resource_metadata_url
        self.scope = scope


class RPCError(MCPError):
    """A JSON-RPC error reply. `code` is the JSON-RPC error code."""

    def __init__(self, code, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class HTTPStatusError(MCPError):
    """An HTTP error status (other than 401) from a Streamable HTTP server."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _StreamBroken(Exception):
    """An SSE reply stream ended before our reply arrived (the request is re-issued once)."""


# ---------------- helpers ----------------

def _meta_for(method: str, params: dict | None) -> dict:
    """The params of a current-generation request: the caller's params plus the three namespaced _meta keys."""
    out = dict(params or {})
    meta = dict(out.get("_meta") or {})
    meta.update({META_VERSION: CURRENT, META_CAPABILITIES: {}, META_CLIENT: dict(CLIENT_INFO)})
    out["_meta"] = meta
    return out


def _check_result(reply: dict) -> dict:
    """The result of a JSON-RPC reply, or the matching MCPError (resultType and -32022 handled here)."""
    if not isinstance(reply, dict):
        raise MCPError("the server sent a reply that is not a JSON object")
    err = reply.get("error")
    if err is not None:
        if not isinstance(err, dict):
            raise RPCError(None, f"the server returned an error: {str(err)[:300]}")
        code = err.get("code")
        if code == UNSUPPORTED_VERSION:
            raise RPCError(code, "unsupported protocol version", err.get("data"))
        message = str(err.get("message") or "error")[:500]
        raise RPCError(code, f"{message} (JSON-RPC error {code})", err.get("data"))
    result = reply.get("result")
    if result is None:
        result = {}
    if not isinstance(result, dict):
        raise MCPError("the server sent a result that is not a JSON object")
    kind = result.get("resultType")
    if kind in (None, "complete"):
        return result
    if kind == "input_required":
        raise MCPError(INPUT_REQUIRED)
    raise MCPError(f"the server sent an unfinished result ({str(kind)[:40]})")


def _target_name(method: str, params: dict | None) -> str | None:
    """What a request targets, for the Mcp-Name header: the tool or prompt name, or the resource URI."""
    params = params or {}
    if method in ("tools/call", "prompts/get"):
        name = params.get("name")
    elif method.startswith("resources/"):
        name = params.get("uri")
    else:
        name = None
    return None if name is None else str(name)


def _header_value(value: str) -> str:
    """HTTP header values must be plain ASCII on one line; anything else is percent-encoded."""
    value = str(value)
    if value.isascii() and value.isprintable():
        return value
    return urllib.parse.quote(value, safe="")


def _origin(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _www_authenticate_param(header: str, name: str) -> str | None:
    m = re.search(name + r'\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))', header or "", re.I)
    if not m:
        return None
    return m.group(1).replace('\\"', '"') if m.group(1) is not None else m.group(2)


def _auth_required(url: str, header: str | None) -> AuthRequired:
    """The AuthRequired for a 401: the metadata URL from the header, or the default well-known URL."""
    found = _www_authenticate_param(header or "", "resource_metadata")
    meta_url = urllib.parse.urljoin(url, found) if found else _origin(url) + "/.well-known/oauth-protected-resource"
    return AuthRequired(meta_url, _www_authenticate_param(header or "", "scope"))


def _parse_sse(lines, limit: int | None = None, too_big=None):
    """Messages from a text/event-stream: data lines are joined per event and parsed as JSON (others skipped).
    With limit, an event whose data grows past that many characters raises too_big() (an MCPError)."""
    data: list[str] = []
    size = 0
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                try:
                    yield json.loads("\n".join(data))
                except ValueError:
                    pass
                data, size = [], 0
            continue
        if line.startswith(":"):
            continue   # a comment (keep-alive)
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
            size += len(value) + 1
            if limit is not None and size > limit:
                raise too_big() if too_big else MCPError("an event in the reply stream is too large")
    if data:   # the stream ended without the blank line that closes an event: use it if it is whole
        try:
            yield json.loads("\n".join(data))
        except ValueError:
            pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would turn a POST into a GET and could carry a token to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
_proxied = urllib.request.build_opener(_NoRedirect)


def _read_timeout(resp, seconds: float) -> None:
    """Let the next read on an HTTP reply wait at most `seconds` (the time left before the call's deadline). The
    socket sits inside http.client's reader; when it cannot be reached the timeout set at connect stays."""
    try:
        sock = resp.fp.raw._sock
        sock.settimeout(max(0.05, seconds))
    except (AttributeError, OSError, ValueError):
        pass


def _open(req: urllib.request.Request, timeout: float):
    # Local servers (and the test fakes) never go through a proxy; remote ones use the system proxy settings.
    host = urllib.parse.urlsplit(req.full_url).hostname or ""
    opener = _direct if host in ("127.0.0.1", "localhost", "::1") else _proxied
    return opener.open(req, timeout=timeout)


def child_env(extra: dict | None = None) -> dict:
    """The environment of a stdio server: Holdco's own, without HOLDCO_* and OLLAMA_* variables (the vault
    passphrase and the dashboard password among them), with the definition's env on top (which may set one of
    those on purpose)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(PRIVATE_ENV_PREFIXES)}
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    return env


# ---------------- transports ----------------

_EXITED = object()   # put into waiting queues when a stdio server process ends


class _StdioTransport:
    """A child process speaking newline-delimited JSON-RPC on stdin and stdout."""

    def __init__(self, name: str, command: str, args=(), env: dict | None = None):
        self.name = name
        self.command = command
        self.args = [str(a) for a in (args or ())]
        self.env = child_env(env)
        self.proc: subprocess.Popen | None = None
        self._pending: dict = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._err_lock = threading.Lock()
        self._err = ""
        self._dead = False
        self._closed = False
        self._threads: list[threading.Thread] = []

    # -- life cycle --

    def start(self) -> None:
        exe = shutil.which(self.command) or shutil.which(self.command, path=self.env.get("PATH"))
        if not exe:
            raise MCPError(f"{self.name}: {self.command} not found. Install it, or check the command on the card.")
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True   # its own process group, so close() also ends what it started
        try:
            self.proc = subprocess.Popen([exe, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=self.env, **kwargs)
        except OSError as e:
            raise MCPError(f"{self.name}: could not start {self.command}: {e}") from e
        # Both threads exist before either starts: the stdout reader waits for the stderr reader at the end.
        self._threads = [threading.Thread(target=target, name=f"mcp-{self.name}-{label}", daemon=True)
                         for target, label in ((self._read_stdout, "out"), (self._read_stderr, "err"))]
        for t in self._threads:
            t.start()

    @property
    def alive(self) -> bool:
        return bool(self.proc) and not self._closed and not self._dead and self.proc.poll() is None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        if proc is None:
            return
        try:
            proc.stdin.close()   # a well-behaved server exits when its input ends
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.wait(0.5)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            self._signal(kill=False)
            try:
                proc.wait(KILL_AFTER)
            except subprocess.TimeoutExpired:
                self._signal(kill=True)
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
        for t in self._threads:
            t.join(2)
        # Close our ends of the pipes only once the reader threads are done (closing under a blocked read hangs).
        for stream, t in ((proc.stdout, self._threads[0] if self._threads else None),
                          (proc.stderr, self._threads[1] if len(self._threads) > 1 else None)):
            if stream is not None and (t is None or not t.is_alive()):
                try:
                    stream.close()
                except OSError:
                    pass
        self._wake_all()

    def _signal(self, kill: bool) -> None:
        proc = self.proc
        try:
            if os.name == "nt":
                # npx.cmd starts node under cmd.exe; end the whole tree, not just cmd.exe.
                r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                                   timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode != 0:
                    proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill() if kill else proc.terminate()
            except OSError:
                pass

    # -- reading --

    def _read_stdout(self) -> None:
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self._keep_err(f"stdout: {line}\n")   # not a protocol message; useful in error messages
                    continue
                self._dispatch(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._dead = True
            # Let the stderr reader catch the process's last words before waking anyone who is waiting.
            if len(self._threads) > 1:
                self._threads[1].join(1.0)
            self._wake_all()

    def _read_stderr(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = self.proc.stderr.read1(4096) if hasattr(self.proc.stderr, "read1") else \
                    self.proc.stderr.read(4096)
                if not chunk:
                    break
                self._keep_err(decoder.decode(chunk))
        except (OSError, ValueError):
            pass

    def _keep_err(self, text: str) -> None:
        with self._err_lock:
            self._err = (self._err + text)[-STDERR_KEEP:]

    def stderr_tail(self) -> str:
        with self._err_lock:
            return self._err.strip()

    def _dispatch(self, msg) -> None:
        if isinstance(msg, list):   # a batch (older servers): handle each message
            for m in msg:
                self._dispatch(m)
            return
        if not isinstance(msg, dict):
            return
        if "method" in msg:
            if "id" in msg:   # a request from the server: answer so it does not wait for us
                reply = {"jsonrpc": "2.0", "id": msg["id"]}
                if msg.get("method") == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "Method not found"}
                # Written from another thread, so this reader never blocks on a full stdin pipe.
                threading.Thread(target=self._write_quietly, args=(reply,), daemon=True).start()
            return   # notifications are ignored
        rid = msg.get("id")
        if isinstance(rid, str) and rid.isdigit():
            rid = int(rid)
        with self._pending_lock:
            q = self._pending.get(rid)
        if q is not None:
            q.put(msg)

    def _wake_all(self) -> None:
        with self._pending_lock:
            waiting = list(self._pending.values())
        for q in waiting:
            q.put(_EXITED)

    # -- writing --

    def _write(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as e:
                raise self._gone(f"could not write to it ({e})") from e

    def _write_quietly(self, msg: dict) -> None:
        try:
            self._write(msg)
        except MCPError:
            pass

    def _gone(self, what: str) -> MCPError:
        code = self.proc.poll() if self.proc else None
        if code is None and self.proc is not None and (self._dead or what.startswith("the server process")):
            try:   # the pipe closed first; on Windows the exit code arrives a moment later
                code = self.proc.wait(timeout=2)
            except Exception:
                code = None
        state = f"the server process exited (code {code})" if code is not None else what
        tail = self.stderr_tail()
        return MCPError(f"{self.name}: {state}." + (f" Its last output:\n{tail}" if tail else ""))

    def request(self, msg: dict, timeout: float, generation: str, new_id) -> dict:
        if self._closed:
            raise MCPError(f"{self.name}: the connection is closed")
        if self._dead or self.proc.poll() is not None:
            raise self._gone("the server process has stopped")
        q: queue.Queue = queue.Queue()
        rid = msg["id"]
        with self._pending_lock:
            self._pending[rid] = q
        try:
            if self._dead:   # the process ended between the check above and registering our queue
                raise self._gone("the server process has stopped")
            self._write(msg)
            try:
                reply = q.get(timeout=timeout)
            except queue.Empty:
                self._cancel(rid, generation)
                tail = self.stderr_tail()
                raise MCPError(f"{self.name}: no reply to {msg.get('method')} within {timeout:g} s"
                               + (f". Its last output:\n{tail}" if tail else "")) from None
            if reply is _EXITED:
                raise self._gone("the server process has stopped")
            return reply
        finally:
            with self._pending_lock:
                self._pending.pop(rid, None)

    def notify(self, msg: dict, timeout: float, generation: str) -> None:
        if self._closed or self._dead:
            raise self._gone("the server process has stopped")
        self._write(msg)

    def _cancel(self, rid, generation: str) -> None:
        """Tell the server we gave up on a request (best effort)."""
        params = {"requestId": rid, "reason": "timed out"}
        if generation == "new":
            params = _meta_for("notifications/cancelled", params)
        self._write_quietly({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params})

    def detail(self) -> str:
        return self.stderr_tail()


class _HttpTransport:
    """Streamable HTTP: one POST per message; the reply is JSON or an SSE stream."""

    def __init__(self, name: str, url: str, headers: dict | None = None, auth=None):
        self.name = name
        self.url = url
        p = urllib.parse.urlsplit(url)
        self.shown = f"{p.scheme}://{p.netloc}{p.path}"   # for messages: a query string may carry a key
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.auth = auth
        self.session_id: str | None = None     # older generation only
        self.protocol_header: str | None = None  # older generation only, after initialize
        self._closed = False

    def start(self) -> None:
        return None

    @property
    def alive(self) -> bool:
        return not self._closed

    def _token(self) -> str | None:
        """A bearer token from the auth source, or None when there is nothing to send (not signed in yet)."""
        if self.auth is None:
            return None
        if isinstance(self.auth, str):
            return self.auth
        getter = getattr(self.auth, "token", None) or self.auth
        try:
            return getter(self.url)
        except Exception as e:
            if getattr(e, "needs_sign_in", False):
                return None   # send nothing; the server answers 401 and the caller sees AuthRequired
            raise MCPError(f"{self.name}: sign-in token unavailable: {e}") from e

    def _request_headers(self, msg: dict, generation: str) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": f"Holdco/{__version__}"}
        h.update(self.headers)
        token = self._token()
        if token:
            h["Authorization"] = f"Bearer {token}"
        method = msg.get("method", "")
        if generation == "new":
            h["Mcp-Method"] = _header_value(method)
            target = _target_name(method, msg.get("params"))
            if target is not None:
                h["Mcp-Name"] = _header_value(target)
        else:
            if self.session_id:
                h["Mcp-Session-Id"] = self.session_id
            if self.protocol_header:
                h["MCP-Protocol-Version"] = self.protocol_header
        return h

    def request(self, msg: dict, timeout: float, generation: str, new_id) -> dict:
        if self._closed:
            raise MCPError(f"{self.name}: the connection is closed")
        deadline = time.monotonic() + timeout
        for attempt in (1, 2):
            try:
                reply = self._post(msg, deadline, timeout, generation)
            except _StreamBroken:
                if attempt == 2:
                    raise MCPError(f"{self.name}: the reply stream from {self.shown} broke twice before the reply")
                msg = {**msg, "id": new_id()}   # streams are not resumed: ask again with a new id
                continue
            if reply is None:
                raise MCPError(f"{self.name}: {self.shown} sent no reply to {msg.get('method')}")
            return reply
        raise MCPError(f"{self.name}: no reply")   # not reached

    def notify(self, msg: dict, timeout: float, generation: str) -> None:
        self._post(msg, time.monotonic() + timeout, timeout, generation)

    def _post(self, msg: dict, deadline: float, timeout: float, generation: str):
        remaining = deadline - time.monotonic()
        method = msg.get("method")
        if remaining <= 0:
            raise MCPError(f"{self.name}: no reply to {method} within {timeout:g} s")
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode("utf-8"), method="POST",
                                     headers=self._request_headers(msg, generation))
        try:
            resp = _open(req, remaining)
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None
        except (TimeoutError, socket.timeout):
            raise MCPError(f"{self.name}: no reply to {method} within {timeout:g} s") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise MCPError(f"{self.name}: no reply to {method} within {timeout:g} s") from None
            raise MCPError(f"{self.name}: cannot reach {self.shown}: {e.reason}") from None
        except (OSError, http.client.HTTPException) as e:
            raise MCPError(f"{self.name}: cannot reach {self.shown}: {e}") from None
        with resp:
            if generation == "old":
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid.strip()
            if "id" not in msg:
                return None   # a notification: the server answers 202 and nothing else matters
            try:
                if resp.headers.get_content_type() == "text/event-stream":
                    return self._read_sse(resp, msg["id"], deadline, method, timeout)
                body = b"".join(self._chunks(resp, deadline, method, timeout, "reply"))
            except (TimeoutError, socket.timeout):
                raise MCPError(f"{self.name}: no reply to {method} within {timeout:g} s") from None
            except (OSError, http.client.HTTPException) as e:
                raise MCPError(f"{self.name}: the reply from {self.shown} broke off: {e}") from None
        try:
            data = json.loads(body.decode("utf-8")) if body.strip() else None
        except ValueError:
            raise MCPError(f"{self.name}: {self.shown} sent something that is not JSON: "
                           f"{body[:200].decode('utf-8', 'replace')}") from None
        for candidate in (data if isinstance(data, list) else [data]):
            if isinstance(candidate, dict) and candidate.get("id") == msg["id"] and "method" not in candidate:
                return candidate
        return None

    def _too_big(self, what: str) -> MCPError:
        return MCPError(f"{self.name}: the {what} from {self.shown} is larger than {MAX_REPLY / 1_000_000:g} MB; "
                        "Holdco stopped reading it")

    def _chunks(self, resp, deadline: float, method, timeout: float, what: str):
        """The body in pieces of at most READ_CHUNK bytes. Each read may wait only until the call's deadline (not a
        fresh timeout per read, so a server that trickles bytes cannot hold the call open), and a plain reply over
        MAX_REPLY bytes raises (what="reply"; a reply stream is capped per event by its reader instead)."""
        total = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise MCPError(f"{self.name}: no reply to {method} within {timeout:g} s")
            _read_timeout(resp, left)
            chunk = resp.read1(READ_CHUNK) if hasattr(resp, "read1") else resp.read(READ_CHUNK)
            if not chunk:
                return
            total += len(chunk)
            if what == "reply" and total > MAX_REPLY:
                raise self._too_big("reply")
            yield chunk

    def _read_sse(self, resp, want_id, deadline: float, method, timeout: float):
        def lines():
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            pending = ""
            for chunk in self._chunks(resp, deadline, method, timeout, "stream"):
                pending += decoder.decode(chunk)
                *whole, pending = pending.split("\n")
                for line in whole:
                    yield line + "\n"
                if len(pending) > MAX_REPLY:   # one line (one event's data) that never ends
                    raise self._too_big("reply stream event")
            pending += decoder.decode(b"", final=True)
            if pending:
                yield pending
        try:
            for message in _parse_sse(lines(), limit=MAX_REPLY, too_big=lambda: self._too_big("reply stream event")):
                if isinstance(message, list):
                    message = next((m for m in message if isinstance(m, dict) and m.get("id") == want_id), None)
                if isinstance(message, dict) and message.get("id") == want_id and "method" not in message:
                    return message   # the first message with our id is the reply; the stream is closed after
        except (TimeoutError, socket.timeout):
            raise MCPError(f"{self.name}: no reply to {method} within {timeout:g} s") from None
        except (OSError, http.client.HTTPException):
            raise _StreamBroken() from None
        raise _StreamBroken()

    def _http_error(self, e: urllib.error.HTTPError) -> MCPError:
        try:
            body = e.read(ERROR_BODY_KEEP) or b""
        except (OSError, http.client.HTTPException):
            body = b""
        finally:
            e.close()
        if e.code == 401:
            return _auth_required(self.url, e.headers.get("WWW-Authenticate") if e.headers else None)
        try:   # a JSON-RPC error inside an HTTP error: an unsupported version is reported as such
            data = json.loads(body.decode("utf-8"))
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict) and err.get("code") == UNSUPPORTED_VERSION:
                return RPCError(UNSUPPORTED_VERSION, "unsupported protocol version", err.get("data"))
        except (ValueError, UnicodeDecodeError):
            pass
        snippet = body[:300].decode("utf-8", "replace").strip()
        detail = f": {snippet}" if snippet else ""
        return HTTPStatusError(e.code, f"{self.name}: HTTP {e.code} from {self.shown}{detail}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.session_id:   # older generation: end the session (best effort)
            try:
                headers = {"Mcp-Session-Id": self.session_id}
                if self.protocol_header:
                    headers["MCP-Protocol-Version"] = self.protocol_header
                token = self._token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                req = urllib.request.Request(self.url, method="DELETE", headers={**self.headers, **headers})
                _open(req, 5).close()
            except Exception:
                pass

    def detail(self) -> str:
        return ""


# ---------------- the client ----------------

class Client:
    """One MCP server. Thread-safe: several agents may call tools at the same time."""

    def __init__(self, name: str, transport: str, command: str | None = None, args=(), env: dict | None = None,
                 url: str | None = None, headers: dict | None = None, auth=None,
                 connect_timeout: float | None = None):
        if transport not in ("stdio", "http"):
            raise MCPError(f"{name}: unknown transport {transport!r} (use stdio or http)")
        if transport == "stdio" and not command:
            raise MCPError(f"{name}: a stdio server needs a command")
        if transport == "http":
            scheme = urllib.parse.urlsplit(url or "").scheme
            if scheme not in ("http", "https"):
                raise MCPError(f"{name}: an HTTP server needs an http(s) URL, not {url!r}")
        self.name = name
        self.transport = transport
        self.command, self.args, self.env = command, list(args or ()), dict(env or {})
        self.url, self.headers, self.auth = url, dict(headers or {}), auth
        # Seconds the protocol negotiation may take; None means CONNECT_TIMEOUT. A stdio server whose first start
        # downloads itself (Playwright) needs longer.
        self.connect_timeout = connect_timeout
        self.protocol: str | None = None       # "2026-07-28", or the version the server returned on initialize
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.server_versions: list = []
        self.tools_info: dict = {}              # ttlMs / cacheScope from the last tools/list (passed through)
        self._t = None
        self._ids = itertools.count(1)
        self._id_lock = threading.Lock()
        self._reinit_lock = threading.Lock()

    # -- life cycle --

    def __enter__(self):
        if not self.alive:
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def alive(self) -> bool:
        """True while connected (for stdio: the server process is still running)."""
        return self._t is not None and self.protocol is not None and self._t.alive

    def connect(self) -> "Client":
        """Start the transport and agree on a protocol generation. Raises MCPError (or AuthRequired)."""
        if self.alive:
            return self
        if self._t is not None:
            self._t.close()
        self.protocol = None
        if self.transport == "stdio":
            self._t = _StdioTransport(self.name, self.command, self.args, self.env)
        else:
            self._t = _HttpTransport(self.name, self.url, self.headers, self.auth)
        try:
            self._t.start()
            self._negotiate()
        except BaseException:
            self._t.close()
            self.protocol = None
            raise
        return self

    def close(self) -> None:
        if self._t is not None:
            self._t.close()

    # -- negotiation --

    def _connect_timeout(self) -> float:
        """This client's negotiation time limit (read at use, so tests can patch CONNECT_TIMEOUT)."""
        try:
            return float(self.connect_timeout) if self.connect_timeout else CONNECT_TIMEOUT
        except (TypeError, ValueError):
            return CONNECT_TIMEOUT

    def _negotiate(self) -> None:
        try:
            result = self._send("server/discover", {}, self._connect_timeout(), "new")
        except AuthRequired:
            raise
        except RPCError as e:
            if e.code not in FALLBACK_CODES:
                raise
            return self._initialize()
        except HTTPStatusError as e:
            if 400 <= e.status < 600:
                return self._initialize()
            raise
        versions = result.get("protocolVersions") or result.get("supportedVersions") or []
        if isinstance(versions, list) and versions and CURRENT not in versions:
            raise MCPError("unsupported protocol version")
        self.protocol = CURRENT
        self.server_versions = list(versions) if isinstance(versions, list) else []
        self.server_capabilities = result.get("capabilities") or {}
        if isinstance(result.get("serverInfo"), dict):
            self.server_info = result["serverInfo"]

    def _initialize(self) -> None:
        if isinstance(self._t, _HttpTransport):
            self._t.session_id = None
            self._t.protocol_header = None
        params = {"protocolVersion": FALLBACK, "capabilities": {}, "clientInfo": dict(CLIENT_INFO)}
        result = self._send("initialize", params, self._connect_timeout(), "old")
        version = result.get("protocolVersion")
        if version not in OLDER_ACCEPTED:
            raise MCPError("unsupported protocol version")
        self.protocol = version
        if isinstance(result.get("serverInfo"), dict):
            self.server_info = result["serverInfo"]
        self.server_capabilities = result.get("capabilities") or {}
        if isinstance(self._t, _HttpTransport):
            self._t.protocol_header = version
        self._t.notify({"jsonrpc": "2.0", "method": "notifications/initialized"}, self._connect_timeout(), "old")

    # -- requests --

    def _new_id(self) -> int:
        with self._id_lock:
            return next(self._ids)

    @property
    def _generation(self) -> str:
        return "new" if self.protocol in (None, CURRENT) else "old"

    def _send(self, method: str, params: dict | None, timeout: float, generation: str) -> dict:
        if self._t is None:
            raise MCPError(f"{self.name}: not connected")
        params = _meta_for(method, params) if generation == "new" else dict(params or {})
        msg = {"jsonrpc": "2.0", "id": self._new_id(), "method": method, "params": params}
        reply = self._t.request(msg, timeout, generation, self._new_id)
        result = _check_result(reply)
        info = (result.get("_meta") or {}).get(META_SERVER) if isinstance(result.get("_meta"), dict) else None
        if isinstance(info, dict):
            self.server_info = info
        return result

    def _request(self, method: str, params: dict | None, timeout: float) -> dict:
        if not self.alive:
            if self._t is not None and self.protocol is not None:
                raise MCPError(f"{self.name}: the server is not running"
                               + (f". Its last output:\n{self._t.detail()}" if self._t.detail() else ""))
            raise MCPError(f"{self.name}: not connected")
        generation = self._generation
        try:
            return self._send(method, params, timeout, generation)
        except HTTPStatusError as e:
            # Older generation: a 404 for our session id means the session expired; start a new one once.
            if not (e.status == 404 and generation == "old" and isinstance(self._t, _HttpTransport)
                    and self._t.session_id):
                raise
            with self._reinit_lock:
                self._initialize()
            return self._send(method, params, timeout, "old")

    def list_tools(self) -> list[dict]:
        """Every tool the server offers: {name, description, inputSchema, ...}."""
        tools: list[dict] = []
        cursor = None
        for page in range(100):
            result = self._request("tools/list", {"cursor": cursor} if cursor else {}, CONNECT_TIMEOUT)
            if page == 0:
                self.tools_info = {k: result[k] for k in ("ttlMs", "cacheScope") if k in result}
            for t in result.get("tools") or []:
                if isinstance(t, dict) and t.get("name"):
                    tools.append({**t, "name": str(t["name"]), "description": t.get("description") or "",
                                  "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}}})
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        """{"content": [...], "isError": bool, "structuredContent": ... or None}. Raises MCPError."""
        result = self._request("tools/call", {"name": name, "arguments": dict(args or {})}, CALL_TIMEOUT)
        content = result.get("content")
        return {"content": content if isinstance(content, list) else [],
                "isError": bool(result.get("isError")),
                "structuredContent": result.get("structuredContent")}


# ---------------- results as text ----------------

def _b64_size(data) -> int:
    s = re.sub(r"\s+", "", str(data or ""))
    return max(0, len(s) * 3 // 4 - (s[-2:].count("=") if s else 0))


def text_of(result: dict) -> str:
    """A tool result as the text an agent reads."""
    if not isinstance(result, dict):
        return str(result)
    if result.get("resultType") == "input_required":
        return f"ERROR: {INPUT_REQUIRED}"
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} {block.get('mimeType') or 'unknown'}, {_b64_size(block.get('data'))} bytes]")
        elif kind == "resource":
            res = block.get("resource") or {}
            if "blob" in res:
                n = _b64_size(res.get("blob"))
            else:
                n = len(str(res.get("text") or "").encode("utf-8"))
            parts.append(f"[resource {res.get('uri') or ''}, {res.get('mimeType') or 'unknown'}, {n} bytes]")
        elif kind == "resource_link":
            size = block.get("size")
            parts.append(f"[resource {block.get('uri') or ''}, {block.get('mimeType') or 'unknown'}"
                         + (f", {size} bytes]" if isinstance(size, int) else "]"))
    text = "\n".join(p for p in parts if p)
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], ensure_ascii=False)
    if result.get("isError"):
        text = f"ERROR: {text}"
    return text
